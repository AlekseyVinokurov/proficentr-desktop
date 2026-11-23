"""Искусственные шаблоны и вымышленные сведения; портал не вызывается."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
import sqlite3

from openpyxl import load_workbook
import pytest

from frdo_fixtures import preparation_template
from test_contracts import records
from test_frdo_preparation import setup, save, changes
from test_issued_documents import draft, register
from proficentr.frdo_export import FrdoExportRepository, PreviousExportError, fill_xlsx, capacity
import proficentr.frdo_export as module
from proficentr.frdo_templates import FrdoError, inspect_template
from proficentr.services import open_database


@pytest.fixture
def ready(records):
    with records.db.connection:
        records.db.connection.execute("UPDATE training_center SET ogrn='1000000000000',kpp='000000001' WHERE id=1")
    r = setup(records)
    r.review = save(r, **changes(r))
    assert r.review.ready, r.review.messages()
    r.exports = FrdoExportRepository(r.db.connection, r.storage)
    return r


def selection(r):
    return r.exports.select([r.frdo.review(r.document.id)])


def create(r):
    return r.exports.create(selection(r))[0]


@pytest.mark.parametrize('kind', ['dpo', 'po'])
def test_write_exact_columns_preserves_every_other_part_and_profile(kind):
    source = preparation_template(kind)
    before = inspect_template(source, kind)
    columns = {module.get_column_letter(i + 1): f'значение {i}' for i in range(len(before.headers))}
    columns.update(G='000001', I='000007', **({'AA':'001-001-998 90', 'AE':'004'} if kind=='dpo' else {'V':'001-001-998 90', 'W':'004'}))
    fields = module.FIELDS[kind]
    for col in ('H',fields['birth']):
        columns[col] = '01.11.2026'
    for col in (fields['start'],fields['end'],fields['hours']):
        columns[col] = '2020'
    columns['F'] = '=1+1'
    output = fill_xlsx(source, kind, [SimpleNamespace(values=columns)])
    after = inspect_template(output, kind)
    changed = [name for name in before.ooxml if before.ooxml[name] != after.ooxml[name]]
    assert changed == ['xl/worksheets/sheet1.xml']
    assert before.dictionaries == after.dictionaries
    book = load_workbook(BytesIO(output))
    original = load_workbook(BytesIO(source))
    try:
        for column, value in columns.items():
            numeric = column in {'H',fields['birth'],fields['start'],fields['end'],fields['hours']}
            expected = module.to_excel(module.datetime(2026,11,1)) if column in {'H',fields['birth']} else (int(value) if numeric else value)
            assert book['Шаблон'][column+'2'].value == expected
            assert book['Шаблон'][column+'2'].data_type == ('n' if numeric else 's')
        assert book['Шаблон']['F2'].data_type != 'f'
        assert book['Шаблон'].max_row == original['Шаблон'].max_row
        assert book['Шаблон'].cell(capacity(kind)+1,len(columns))._style == original['Шаблон'].cell(capacity(kind)+1,len(columns))._style
        assert book['Проверки'].sheet_state == 'hidden'
        assert list(book.defined_names) == list(original.defined_names)
    finally:
        book.close(); original.close()


@pytest.mark.parametrize('kind,limit', [('dpo',1000),('po',1001)])
def test_full_capacity_and_excess_rejected(kind, limit):
    source = preparation_template(kind)
    row = SimpleNamespace(values={'A':'Тест', 'G':'000001'})
    assert capacity(kind) == limit
    result = fill_xlsx(source, kind, [row]*limit)
    book = load_workbook(BytesIO(result))
    try:
        assert book['Шаблон'][f'G{limit+1}'].value == '000001'
    finally:
        book.close()
    with pytest.raises(FrdoError, match='вместимость'):
        fill_xlsx(source, kind, [row]*(limit+1))
    with pytest.raises(FrdoError):
        fill_xlsx(source, kind, [])


def test_export_persists_and_retrieves_exact_bytes_without_changing_documents(ready, tmp_path):
    r = ready
    protected = asdict(r.repo.load(r.document.id))
    archive = r.repo.verify_document(r.document.id).read_bytes()
    p = create(r)
    assert (p.kind,p.issue_year,p.number,p.row_count,p.template_version) == ('dpo',2026,1,1,1)
    assert p.filename == '1000000000000_000000001_26_1.xlsx'
    assert p.created_at.endswith('Z')
    data = r.exports.file(p.id).read_bytes()
    assert sha256(data).hexdigest() == p.sha256
    assert r.exports.create(selection(r)) == [p]
    assert asdict(r.repo.load(r.document.id)) == protected
    assert r.repo.verify_document(r.document.id).read_bytes() == archive
    with open_database(r.storage) as db:
        restarted = FrdoExportRepository(db.connection,r.storage)
        assert restarted.packages() == [p]
        assert restarted.file(p.id).read_bytes() == data
        assert restarted.copy(p.id,tmp_path/'copy.xlsx').read_bytes() == data
    item = r.db.connection.execute('SELECT document_id,source_signature FROM frdo_package_documents').fetchone()
    assert item == (r.document.id,r.review.signature)


def test_changes_require_explicit_new_action_and_keep_old_file(ready):
    r = ready
    first = create(r)
    data = r.exports.file(first.id).read_bytes()
    save(r, training={'funding':'Местный бюджет'})
    with pytest.raises(PreviousExportError, match='уже включались'):
        create(r)
    second = r.exports.create(selection(r),allow_reexport=True)[0]
    assert second.number == 2 and second.sha256 != first.sha256
    assert r.exports.file(first.id).read_bytes() == data
    assert create(r) == second  # Идентичный запрос возвращает сохранённый файл.


@pytest.mark.parametrize('change', ['student','template','invalid','forged_cells'])
def test_stale_or_forged_selection_cannot_export(ready, change):
    r = ready
    selected = selection(r)
    if change == 'template':
        r.frdo.templates.register(r.source.parent/'dpo.xlsx','dpo')
    elif change == 'student':
        r.students.save(replace(r.students.load(r.first.id), surname='Другаяфамилия'))
    elif change == 'invalid':
        save(r, personal={'sex':''})
    else:
        forged = replace(selected.records[0], cells=(('A','=1+1'),))
        selected = replace(selected,records=(forged,))
    with pytest.raises(FrdoError,match='изменились'):
        r.exports.create(selected)
    assert not r.exports.packages()
    assert not list((r.storage.root/'exports').rglob('*.xlsx'))


def second_ready(r):
    document = register(r, draft(r,index=1,blank_series='ТЕСТ',blank_number='000002',document_date='2026-11-02'))
    review = r.frdo.review(document.id)
    args = changes(r)
    # У второго вымышленного слушателя отчества также нет.
    r.frdo.save_details(document.id, expected_signature=review.signature, **args)
    return document


def test_invalid_selection_is_atomic_and_reports_document_and_field(ready):
    r = ready
    other = register(r,draft(r,index=1,document_date='2026-11-02'))
    with pytest.raises(FrdoError,match='Пол получателя'):
        r.exports.select([r.review,r.frdo.review(other.id)])
    assert not r.exports.packages()


def test_split_registers_separate_numbers_atomically(ready, monkeypatch):
    r = ready
    other = second_ready(r)
    monkeypatch.setattr(module,'capacity',lambda kind:1)
    selected = r.exports.select([r.review,r.frdo.review(other.id)])
    packages = r.exports.create(selected)
    assert [p.number for p in packages] == [1,2]
    assert [p.row_count for p in packages] == [1,1]
    assert len({p.request_key for p in packages}) == 1
    assert r.exports.create(selected) == packages


@pytest.mark.parametrize('stage',['write','second_part','commit'])
def test_failed_batch_leaves_no_registered_package_or_reserved_number(ready,monkeypatch,stage):
    r = ready
    selected = selection(r)
    if stage == 'second_part':
        other = second_ready(r)
        selected = r.exports.select([r.review,r.frdo.review(other.id)])
        monkeypatch.setattr(module,'capacity',lambda kind:1)
    original = module._write_file
    count = 0
    def fail(path,data):
        nonlocal count
        count += 1
        original(path,data[:20] if stage=='write' else data)
        if stage=='write' or (stage=='second_part' and count==2):
            raise OSError('synthetic disk full')
    if stage=='commit':
        # Отложенный FK падает именно на COMMIT после успешной записи XLSX.
        r.db.connection.execute('CREATE TABLE commit_failure(x REFERENCES students(id) DEFERRABLE INITIALLY DEFERRED)')
        def fail(path,data):
            original(path,data)
            r.db.connection.execute('INSERT INTO commit_failure VALUES (-999)')
    with monkeypatch.context() as patch:
        patch.setattr(module,'_write_file',fail)
        with pytest.raises(FrdoError,match='завершить формирование'):
            r.exports.create(selected)
    assert not r.exports.packages()
    assert not list((r.storage.root/'exports').rglob('*.xlsx'))
    assert create(r).number == 1


def test_missing_corrupt_and_restore_identical_file(ready,tmp_path):
    r = ready
    p = create(r)
    path = r.exports.file(p.id)
    original = path.read_bytes()
    backup = tmp_path/'backup.xlsx'; backup.write_bytes(original)
    path.write_bytes(b'broken')
    with pytest.raises(FrdoError,match='повреждён'):
        r.exports.file(p.id)
    with pytest.raises(FrdoError,match='уже существует'):
        r.exports.restore(p.id,backup)
    with pytest.raises(FrdoError):
        create(r)  # Повтор не скрывает повреждение новой генерацией/номером.
    path.rename(path.with_suffix('.damaged'))
    with pytest.raises(FrdoError,match='недоступен'):
        r.exports.file(p.id)
    assert r.exports.restore(p.id,backup).read_bytes() == original
    assert create(r) == p
    assert len(r.exports.packages()) == 1


def test_crash_orphan_does_not_collide_with_reused_number(ready):
    r = ready
    folder = r.storage.root/'exports'/'frdo'/('f'*32)
    folder.mkdir(parents=True)
    orphan = folder/'1000000000000_000000001_26_1.xlsx'
    orphan.write_bytes(b'not committed')
    with open_database(r.storage) as db:
        restarted = FrdoExportRepository(db.connection,r.storage)
        assert restarted.unregistered_files() == (orphan,)
        p = restarted.create(restarted.select([restarted.preparation.review(r.document.id)]))[0]
        assert p.number == 1
        assert restarted.file(p.id) != orphan
        assert orphan.read_bytes() == b'not committed'


@pytest.mark.parametrize('different', [False,True])
def test_simultaneous_requests_are_serialized(ready,different):
    r = ready
    selected = selection(r)
    second = second_ready(r) if different else r.document
    other = r.exports.select([r.frdo.review(second.id)])
    barrier = Barrier(2)
    def worker(chosen):
        with open_database(r.storage) as db:
            repo = FrdoExportRepository(db.connection,r.storage)
            barrier.wait(timeout=20)
            return repo.create(chosen)[0]
    with ThreadPoolExecutor(max_workers=2) as executor:
        a,b = list(executor.map(worker,[selected,other]))
    if different:
        assert {a.number,b.number} == {1,2}
    else:
        assert a == b and a.number == 1
    assert len(r.exports.packages()) == (2 if different else 1)


def test_journal_cannot_be_changed_deleted_or_replaced(ready):
    r = ready
    p = create(r)
    for sql in ["UPDATE frdo_packages SET number=9", "DELETE FROM frdo_packages",
                "INSERT OR REPLACE INTO frdo_packages SELECT * FROM frdo_packages",
                "UPDATE frdo_package_documents SET source_signature='tamper'", "DELETE FROM frdo_package_documents",
                "INSERT OR REPLACE INTO frdo_package_documents SELECT * FROM frdo_package_documents"]:
        with pytest.raises(sqlite3.IntegrityError),r.db.connection:
            r.db.connection.execute(sql)
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute('INSERT INTO frdo_package_documents VALUES (?,?,?,?)',(p.id,1,r.document.id,'tamper'))
    assert r.exports.load(p.id) == p


def test_symlink_destination_rejected(ready,tmp_path):
    r = ready
    try:
        (r.storage.root/'exports'/'frdo').symlink_to(tmp_path,target_is_directory=True)
    except OSError:
        pytest.skip('Создание symlink недоступно в этой среде')
    with pytest.raises(FrdoError,match='пределы'):
        create(r)
    assert not r.exports.packages()


def test_dpo_and_po_share_sequence_but_keep_separate_template_and_rows(ready):
    r = ready
    first = create(r)
    original = r.exports.file(first.id).read_bytes()
    r.group = r.trainings.save_group(replace(r.group,id=None,name='Вторая вымышленная группа'))
    r.members = r.trainings.add_students(r.group.id,[r.first.id,r.second.id],allow_overlap=True)
    from docx import Document
    source = Document(); source.add_paragraph('{{ document.number }}'); source.save(r.source)
    r = setup(r,'po','po_initial_training')
    review = save(r,**changes(r))
    assert review.ready, review.messages()
    second = create(r)
    assert (first.number,second.number) == (1,2)
    assert second.kind == 'po'
    assert first.template_id != second.template_id
    assert r.exports.file(first.id).read_bytes() == original
    book = load_workbook(r.exports.file(second.id))
    try:
        assert book['Шаблон']['L2'].value == 'Тестовая профессия'
        assert book['Шаблон']['M2'].value == '2'
        assert book['Шаблон']['W2'].value == '000'
        assert book['Шаблон']['G2'].value == '000001'
    finally:
        book.close()


def test_one_year_only_and_preview_never_reserves_number(ready):
    r = ready
    old = register(r,draft(r,index=1,document_date='2020-04-04',blank_series='TEST',blank_number='000002'))
    r.frdo.save_details(old.id,expected_signature=r.frdo.review(old.id).signature,**changes(r))
    for _ in range(2):
        selection(r)
    assert not r.exports.packages()
    with pytest.raises(FrdoError,match='один год'):
        r.exports.select([r.review,r.frdo.review(old.id)])
    p = r.exports.create(r.exports.select([r.frdo.review(old.id)]))[0]
    assert p.number == 1 and p.issue_year == 2020
    assert create(r).number == 1


def test_current_organization_edits_do_not_replace_historical_identity(ready):
    r = ready
    with r.db.connection:
        r.db.connection.execute("UPDATE training_center SET ogrn='2000000000000',kpp='999999999'")
    p = create(r)
    assert (p.ogrn,p.kpp) == ('1000000000000','000000001')


@pytest.mark.parametrize('prefix',['=','+','-','@'])
def test_user_text_is_never_a_formula(prefix):
    result = fill_xlsx(preparation_template('po'),'po',[SimpleNamespace(values={'F':prefix+'SUM(1,1)'})])
    book = load_workbook(BytesIO(result))
    try:
        assert book['Шаблон']['F2'].value == prefix+'SUM(1,1)'
        assert book['Шаблон']['F2'].data_type == 's'
    finally:
        book.close()


def test_migration15_preserves_preparation_and_relocation_preserves_packages(ready,tmp_path,monkeypatch):
    from proficentr.database import Database
    from proficentr.storage import relocate_storage
    r = ready
    legacy = tmp_path/'legacy14.sqlite3'
    with sqlite3.connect(legacy) as copy:
        r.db.connection.backup(copy)
        copy.execute('DROP TABLE frdo_package_documents')
        copy.execute('DROP TABLE frdo_packages')
        copy.execute('DELETE FROM schema_migrations WHERE version=15')
    with Database(legacy) as db:
        repo = FrdoExportRepository(db.connection,r.storage)
        assert repo.preparation.review(r.document.id) == r.review
        package = repo.create(repo.select([r.review]))[0]
        data = repo.file(package.id).read_bytes()
        monkeypatch.setattr('proficentr.storage.settings.save_directory',lambda path:None)
        target = relocate_storage(r.storage,db.connection,tmp_path/'moved')
    with open_database(target) as db:
        moved = FrdoExportRepository(db.connection,target)
        assert moved.load(package.id) == package
        assert moved.file(package.id).read_bytes() == data
        assert moved.preparation.review(r.document.id) == r.review
        assert moved.create(moved.select([r.review])) == [package]
