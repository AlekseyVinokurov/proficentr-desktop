"""N02: только вымышленные PDF/DOCX, без документов реальной организации."""
from dataclasses import replace
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from docx import Document
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
import pytest

from proficentr.database import Database
from proficentr.services import prepare_storage, open_database
from proficentr.regulatory_documents import RegulatoryRepository, RegulatoryVersion, RegulatoryError, CATEGORIES
from proficentr.storage import relocate_storage
from test_issued_documents import pdf


@pytest.fixture
def registry(tmp_path):
    app = QApplication.instance() or QApplication([])
    existing_widgets = set(app.topLevelWidgets())
    storage = prepare_storage(tmp_path / 'data')
    source = pdf(tmp_path / 'fictional.pdf')
    docx = tmp_path / 'fictional.docx'; doc = Document(); doc.add_paragraph('Вымышленный акт'); doc.save(docx)
    with open_database(storage) as db:
        yield SimpleNamespace(repo=RegulatoryRepository(db.connection, storage), db=db, storage=storage,
                              pdf=source, docx=docx, app=app)
        # Dispose Qt windows while both QApplication and the repository are still alive.
        for widget in app.topLevelWidgets():
            if widget not in existing_widgets:
                widget.close(); widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def active(**kwargs):
    return RegulatoryVersion(title='Вымышленный акт', category='local', document_date='2020-01-02',
        effective_date='2020-02-01', approval_basis='Вымышленный приказ', status='active', **kwargs)


def test_import_reopen_and_copies_independent_of_external_sources(registry,tmp_path):
    r=registry
    version=r.repo.add(active(),pdf=r.pdf,docx=r.docx)
    pdf_bytes=r.pdf.read_bytes();docx_bytes=r.docx.read_bytes()
    r.pdf.unlink();r.docx.unlink()
    with Database(r.storage.database_path) as db:
        reopened=RegulatoryRepository(db.connection,r.storage)
        assert reopened.load(version.id)==version
        assert reopened.read_file(version.id)==pdf_bytes
        assert reopened.read_file(version.id,'docx')==docx_bytes
        out=tmp_path/'copy.pdf';reopened.copy(version.id,out)
        assert out.read_bytes()==pdf_bytes
        with pytest.raises(FileExistsError):reopened.copy(version.id,out)
        with pytest.raises(RegulatoryError):reopened.copy(version.id,r.storage.root/'organization'/'replace.pdf')
        assert reopened.editable_copy(version.id).read_bytes()==docx_bytes


def test_revision_history_statuses_and_filters(registry):
    r=registry;v1=r.repo.add(active(),pdf=r.pdf)
    draft=r.repo.add(replace(active(),status='draft'),pdf=r.pdf,previous_id=v1.id)
    assert r.repo.load(v1.id).status=='active'
    assert r.repo.search(status='active')==[v1]
    v2=r.repo.set_status(draft.id,'active',expected_status='draft',note='Вымышленное утверждение')
    assert r.repo.load(v1.id).status=='archived' and v2.revision==2
    assert r.repo.read_file(v1.id)==r.repo.read_file(v2.id)
    assert r.repo.search('ВЫМЫШЛЕННЫЙ',category='local',status='active')==[v2]
    assert not r.repo.search(category='license')
    assert len(r.repo.events(v1.id))==2
    assert [v.revision for v in r.repo.history(v1.document_id)]==[2,1]
    r.repo.set_status(v2.id,'archived',expected_status='active',note='Снят с действия')
    with pytest.raises(RegulatoryError):r.repo.set_status(v2.id,'active',expected_status='archived',note='Попытка')
    with pytest.raises(RegulatoryError):r.repo.add(active(),pdf=r.pdf,previous_id=v1.id)


def test_active_replacement_archives_prior_and_failed_replacement_preserves_it(registry,monkeypatch):
    r=registry;v1=r.repo.add(active(),pdf=r.pdf)
    def fail(*args):raise sqlite3.OperationalError('fictional failure')
    with monkeypatch.context() as patch:
        patch.setattr(r.repo,'_status',fail)
        with pytest.raises(sqlite3.Error):r.repo.add(active(),pdf=r.pdf,previous_id=v1.id)
    assert r.repo.history(v1.document_id)==[v1]
    assert len(list((r.storage.root/'organization'/'regulatory').iterdir()))==1
    v2=r.repo.add(active(),pdf=r.pdf,previous_id=v1.id)
    assert r.repo.load(v1.id).status=='archived' and v2.status=='active'


@pytest.mark.parametrize('category',list(CATEGORIES))
def test_all_categories_accept_drafts_without_invented_requisites(registry,category):
    v=registry.repo.add(RegulatoryVersion(title='Вымышленный проект',category=category))
    assert v.category==category and v.status=='draft'


def test_validation_depends_on_category_status_and_pdf(registry):
    r=registry
    for version in (RegulatoryVersion(),replace(active(),document_date=''),replace(active(),effective_date=''),
                    replace(active(),approval_basis=''),replace(active(),document_date='2020-02-30')):
        with pytest.raises(ValueError):r.repo.add(version,pdf=r.pdf)
    with pytest.raises(RegulatoryError):r.repo.add(active())
    with pytest.raises(RegulatoryError):r.repo.add(replace(active(),category='license'),pdf=r.pdf)
    license=r.repo.add(replace(active(),category='license',number='Вымышленный номер'),pdf=r.pdf)
    assert license.status=='active'
    # No mandatory pedagogical council protocol for a local act.
    assert r.repo.add(active(),pdf=r.pdf).status=='active'
    order=r.repo.add(replace(active(),category='orders',approval_basis='',effective_date=''),pdf=r.pdf)
    linked=r.repo.add(replace(active(),approval_basis=''),pdf=r.pdf,links=[order.id])
    assert r.repo.links(linked.id)==[order]
    next_order=r.repo.add(replace(active(),category='orders'),pdf=r.pdf,previous_id=order.id)
    assert r.repo.links(linked.id)[0].id==order.id
    assert next_order.id!=order.id


@pytest.mark.parametrize('failure',['invalid_pdf','invalid_docx','partial_write','sql','commit'])
def test_import_failure_rolls_back_rows_and_owned_files(registry,monkeypatch,failure):
    r=registry
    if failure=='invalid_pdf':r.pdf.write_bytes(b'%PDF-1.4\ninvalid\n%%EOF')
    elif failure=='invalid_docx':r.docx.write_bytes(b'not a DOCX')
    elif failure=='partial_write':
        real=r.repo._write
        def fail(data,kind,owned):
            result=real(data,kind,owned)
            raise OSError('fictional write failure')
        monkeypatch.setattr(r.repo,'_write',fail)
    elif failure=='sql':
        r.db.connection.execute("CREATE TRIGGER fail_n02 BEFORE INSERT ON regulatory_links BEGIN SELECT RAISE(ABORT,'fictional SQL failure'); END")
        target=r.repo.add(RegulatoryVersion(title='Связанный проект'))
    elif failure=='commit':
        # A deferred FK fails when leaving the transaction, after both files have been written.
        r.db.connection.execute('CREATE TABLE commit_failure(value INTEGER REFERENCES regulatory_documents(id) DEFERRABLE INITIALLY DEFERRED)')
        r.db.connection.execute('CREATE TRIGGER commit_failure_event AFTER INSERT ON regulatory_status_events BEGIN INSERT INTO commit_failure VALUES (-1); END')
    before=r.db.connection.execute('SELECT count(*) FROM regulatory_versions').fetchone()[0]
    with pytest.raises((ValueError,OSError,sqlite3.Error)):
        r.repo.add(active(),pdf=r.pdf,docx=r.docx,links=[target.id] if failure=='sql' else [])
    assert r.db.connection.execute('SELECT count(*) FROM regulatory_versions').fetchone()[0]==before
    assert not list((r.storage.root/'organization'/'regulatory').glob('*'))
    assert r.db.connection.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_tamper_missing_files_symlinks_and_path_traversal(registry,tmp_path):
    r=registry;v=r.repo.add(active(),pdf=r.pdf,docx=r.docx)
    path=r.storage.root/v.pdf_path;path.write_bytes(path.read_bytes()+b'changed')
    with pytest.raises(ValueError):r.repo.read_file(v.id)
    path.unlink()
    with pytest.raises(RegulatoryError):r.repo.read_file(v.id)
    with pytest.raises(RegulatoryError):r.repo._path('organization/regulatory/../../outside.pdf')
    if __import__('os').name!='nt':
        link=tmp_path/'linked.pdf';link.symlink_to(r.pdf)
        with pytest.raises(RegulatoryError):r.repo.add(active(),pdf=link)
        path.symlink_to(r.pdf)
        with pytest.raises(RegulatoryError):r.repo.read_file(v.id)


def test_access_hooks_cover_mutations_reads_and_exports(registry,tmp_path):
    r=registry;v=r.repo.add(active(),pdf=r.pdf)
    called=[]
    def employee(permission):
        called.append(permission)
        if permission=='regulatory.write':raise PermissionError('read only')
    repo=RegulatoryRepository(r.db.connection,r.storage,access_check=employee)
    before=r.db.connection.total_changes
    assert not repo.can_manage() and repo.search() and repo.read_file(v.id)
    repo.copy(v.id,tmp_path/'employee-copy.pdf')
    for action in (lambda:repo.add(active(),pdf=r.pdf),lambda:repo.set_status(v.id,'archived',expected_status='active',note='Archive'),repo.recover_unregistered):
        with pytest.raises(PermissionError):action()
    assert r.db.connection.total_changes==before
    assert {'regulatory.read','regulatory.write'}<=set(called)
    def deny(permission):raise PermissionError('denied')
    denied=RegulatoryRepository(r.db.connection,r.storage,access_check=deny)
    for action in (denied.search,lambda:denied.read_file(v.id),lambda:denied.copy(v.id,tmp_path/'denied.pdf')):
        with pytest.raises(PermissionError):action()
    assert not (tmp_path/'denied.pdf').exists()


def test_sqlite_prevents_rewrite_delete_and_replace(registry):
    r=registry;order=r.repo.add(RegulatoryVersion(title='Вымышленный приказ'))
    v=r.repo.add(active(),pdf=r.pdf,links=[order.id])
    for table in ('regulatory_documents','regulatory_versions','regulatory_status_events','regulatory_links'):
        for statement in (f'DELETE FROM {table}',f'UPDATE {table} SET '+('note=note' if table=='regulatory_status_events' else 'version_id=version_id' if table=='regulatory_links' else 'id=id'),
                          f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}'):
            with pytest.raises(sqlite3.IntegrityError):
                with r.db.connection:r.db.connection.execute(statement)
    assert r.repo.read_file(v.id)==r.pdf.read_bytes()


def test_recovery_and_relocation_preserve_files_links_and_history(registry,tmp_path):
    r=registry;v=r.repo.add(active(),pdf=r.pdf,docx=r.docx)
    extra=r.storage.root/'organization'/'regulatory'/('a'*32+'.pdf');extra.write_bytes(r.pdf.read_bytes())
    assert r.repo.recover_unregistered()==1 and not extra.exists()
    assert len(list((r.storage.root/'exports'/'regulatory-recovery').rglob('*.pdf')))==1
    moved=relocate_storage(r.storage,r.db.connection,tmp_path/'moved')
    with open_database(moved) as db:
        repo=RegulatoryRepository(db.connection,moved)
        assert repo.load(v.id)==v and repo.read_file(v.id)==r.pdf.read_bytes()
        assert repo.read_file(v.id,'docx')==r.docx.read_bytes()
    assert r.repo.read_file(v.id)==r.pdf.read_bytes()


def test_migration_from_p035_preserves_data_and_makes_backup(tmp_path,monkeypatch):
    import proficentr.database as module
    path=tmp_path/'database.sqlite3';migrations=module.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(module,'MIGRATIONS',migrations[:-1])
        with Database(path) as db:
            db.connection.execute("INSERT INTO training_center(id,full_name) VALUES (1,'Вымышленный центр')");db.connection.commit()
            old_rules=db.connection.execute('SELECT * FROM admission_rules').fetchall()
    with Database(path) as db:
        assert db.connection.execute('SELECT full_name FROM training_center').fetchone()[0]=='Вымышленный центр'
        assert db.connection.execute('SELECT * FROM admission_rules').fetchall()==old_rules
        assert db.connection.execute('PRAGMA integrity_check').fetchone()==('ok',)
        assert list((tmp_path/'migration-backups').glob('*.sqlite3'))
        assert db.connection.execute('SELECT count(*) FROM regulatory_versions').fetchone()[0]==0
