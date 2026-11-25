"""Проверки T06 на поставляемых формах и исключительно вымышленных данных."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
from zipfile import ZipFile

from docx import Document
from PySide6.QtCore import QCoreApplication,QEvent
from PySide6.QtGui import QImage,QColor
from PySide6.QtWidgets import QApplication
import pytest

from proficentr.docx_context import fictional_context,DocxError,ROW_WORK_FIELDS
from proficentr.docx_engine import validate_template
from proficentr.services import prepare_storage,open_database
from proficentr.working_library import WorkingLibrary,catalog,RESOURCE_ROOT
from proficentr.storage import relocate_storage
from test_contracts import records


def sample():
    def fill(value):
        if type(value) is dict:return {k:fill(v) for k,v in value.items()}
        if type(value) is list:return [fill(v) for v in value]
        return 'Вымышленное значение' if value=='' else value
    return fill(fictional_context())


@pytest.fixture
def library(tmp_path):
    app=QApplication.instance() or QApplication([]);before=set(app.topLevelWidgets())
    storage=prepare_storage(tmp_path/'storage')
    with open_database(storage) as db:
        yield SimpleNamespace(repo=WorkingLibrary(db.connection,storage),db=db,storage=storage,app=app)
        for widget in app.topLevelWidgets():
            if widget not in before:widget.close();widget.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);app.processEvents()


@pytest.mark.parametrize('item',catalog(),ids=lambda item:item['code'])
def test_all_24_source_layout_and_real_generation(library,item):
    data=(RESOURCE_ROOT/item['path']).read_bytes()
    assert sha256(data).hexdigest()==item['sha256']
    with ZipFile(BytesIO(data)) as z:xml=z.read('word/document.xml').decode()
    xml=re.sub(r'<w:tr><w:tc><w:p><w:r><w:t>{%tr .*? %}</w:t></w:r></w:p></w:tc></w:tr>','',xml)
    assert sha256(re.sub(r'{{.*?}}','',xml).encode()).hexdigest()==item['content_layout_sha256']
    library.repo.configure(item['code'],restore=True)
    path=library.repo.generate(item['code'],sample())
    with ZipFile(path) as z:output=z.read('word/document.xml').decode()
    assert '{{' not in output and '{%' not in output
    assert 'Учебный центр' in output
    assert library.db.connection.execute('SELECT context_snapshot FROM working_template_generations').fetchone()


def test_replace_restore_history_and_immutable_previous_results(library,tmp_path):
    r=library;item=catalog()[0];code=item['code']
    first=r.repo.configure(code,restore=True);output=r.repo.generate(code,sample());original=output.read_bytes()
    custom=tmp_path/'custom.docx';doc=Document();doc.add_paragraph('СОБСТВЕННЫЙ {{ center.full_name }}');doc.save(custom)
    changed=r.repo.configure(code,source=custom)
    assert changed['origin']=='custom' and changed['effective_version_id']!=first['effective_version_id']
    assert 'СОБСТВЕННЫЙ' in Document(r.repo.generate(code,sample())).paragraphs[0].text
    restored=r.repo.configure(code,restore=True)
    assert restored['origin']=='standard' and len(r.repo.history(code))==3
    assert output.read_bytes()==original
    assert r.repo.templates.source(first['effective_version_id']).path.read_bytes()==(RESOURCE_ROOT/item['path']).read_bytes()
    with pytest.raises(sqlite3.IntegrityError):
        with r.db.connection:r.db.connection.execute('DELETE FROM working_template_events')


def test_background_and_preprinted_modes_are_versioned(library,tmp_path):
    r=library;item=next(i for i in catalog() if i['document_type']=='dpo_upskilling_certificate');code=item['code']
    first=r.repo.configure(code,restore=True)
    image=QImage(64,64,QImage.Format.Format_RGB32);image.fill(QColor('blue'));source=tmp_path/'blank.png';assert image.save(str(source))
    background=r.repo.configure(code,background=source,mode='background')
    assert background['background_sha256']==sha256((r.storage.root/background['background_path']).read_bytes()).hexdigest()
    bg_version=r.repo.templates.source(background['effective_version_id']);validate_template(bg_version.path.read_bytes())
    with ZipFile(bg_version.path) as z:
        assert any('media/image' in name for name in z.namelist())
        assert any(b'behindDoc="1"' in z.read(name) for name in z.namelist() if 'header' in name and name.endswith('xml'))
    original=bg_version.path.read_bytes()
    for mode in ('preprinted','none'):
        current=r.repo.configure(code,mode=mode)
        with ZipFile(r.repo.templates.source(current['effective_version_id']).path) as z:
            assert not any('media/image' in n for n in z.namelist())
    assert bg_version.path.read_bytes()==original
    assert r.repo.templates.source(first['effective_version_id']).path.exists()


@pytest.mark.parametrize('failure',['invalid_docx','invalid_background','sql','commit'])
def test_failed_import_preserves_files_and_active_selection(library,tmp_path,failure):
    r=library;code=next(i['code'] for i in catalog() if i['document_type']=='dpo_retraining_diploma')
    initial=r.repo.configure(code,restore=True)
    original={p:p.read_bytes() for p in (r.storage.root/'templates').rglob('*') if p.is_file()}
    args={}
    if failure=='invalid_docx':
        bad=tmp_path/'bad.docx';bad.write_bytes(b'not a DOCX');args={'source':bad}
    elif failure=='invalid_background':
        bad=tmp_path/'bad.png';bad.write_bytes(b'not an image');args={'background':bad,'mode':'background'}
    elif failure=='sql':
        r.db.connection.execute("CREATE TRIGGER fictional_failure BEFORE INSERT ON working_template_events BEGIN SELECT RAISE(ABORT,'test'); END")
    else:
        r.db.connection.execute('CREATE TABLE deferred_failure(v INTEGER REFERENCES document_templates(id) DEFERRABLE INITIALLY DEFERRED)')
        r.db.connection.execute('CREATE TRIGGER deferred_event AFTER INSERT ON working_template_events BEGIN INSERT INTO deferred_failure VALUES (-1); END')
    with pytest.raises((ValueError,OSError,sqlite3.Error)):r.repo.configure(code,restore=not args,**args)
    assert r.repo.current(code)==initial
    assert {p:p.read_bytes() for p in (r.storage.root/'templates').rglob('*') if p.is_file()}==original


def test_facts_are_scoped_and_cannot_change_original_student_data(library):
    r=library;c=sample();c['work']['study_form']=''
    r.repo.save_supplement(c,{'work':{'study_form':'Очно'},'rows':[{'practical_result':'Выполнено'}]})
    completed=r.repo.templates.complete_context(c)
    assert completed['work']['study_form']=='Очно' and completed['students'][0]['practical_result']=='Выполнено'
    changed=deepcopy(c);changed['training']['end_date']='2020-12-01'
    assert r.repo.templates.complete_context(changed)['work']['study_form']==''
    with pytest.raises(DocxError):r.repo.save_supplement(c,{'rows':[{'full_name':'Подмена'}]})


def test_roles_and_relocation(library,tmp_path):
    r=library;code=catalog()[0]['code'];current=r.repo.configure(code,restore=True)
    def employee(permission):
        if permission=='templates.write':raise PermissionError('read only')
    employee_repo=WorkingLibrary(r.db.connection,r.storage,access_check=employee)
    assert not employee_repo.can_manage() and employee_repo.current(code)
    with pytest.raises(PermissionError):employee_repo.configure(code,restore=True)
    assert employee_repo.generate(code,sample()).exists()
    target=relocate_storage(r.storage,r.db.connection,tmp_path/'moved')
    with open_database(target) as db:
        moved=WorkingLibrary(db.connection,target)
        assert moved.current(code)==current
        assert moved.templates.source(current['effective_version_id']).path.exists()


def test_ui_registry_and_generation_fields(library,monkeypatch):
    from proficentr.ui.working_templates_page import WorkingTemplatesPage,WorkingDocumentDialog
    from PySide6.QtWidgets import QMessageBox,QDialog
    r=library;page=WorkingTemplatesPage(r.repo)
    assert page.table.rowCount()==24
    page.table.selectRow(0)
    monkeypatch.setattr(QMessageBox,'question',lambda *args:QMessageBox.StandardButton.Yes)
    page.buttons['standard'].click()
    assert r.repo.current(catalog()[0]['code'])
    page.table.selectRow(0)
    dialog=WorkingDocumentDialog(r.repo,catalog()[0]['code'])
    assert 'center.full_name' in dialog.edits
    dialog.edits['center.full_name'].setText('Вымышленный центр')
    import proficentr.ui.working_templates_page as ui
    opened=[];monkeypatch.setattr(ui,'open_local_docx',lambda path:opened.append(path))
    assert dialog.generate() and opened[0].exists()
    dialog.close();page.close()


def test_existing_contract_pipeline_uses_standard_and_scoped_facts(records):
    from test_contracts import draft,register
    from proficentr.docx_context import SCHEMA
    r=records;library=WorkingLibrary(r.db.connection,r.storage)
    item=next(i for i in catalog() if i['document_type']=='contract_person' and i['variant']=='dpo')
    library.configure(item['code'],restore=True)
    for field in SCHEMA['center']:
        r.db.connection.execute(f"UPDATE training_center SET {field}=? WHERE id=1",('2020-01-01' if field.endswith('date') else 'Вымышленные реквизиты',))
    r.db.connection.commit()
    d=draft(r);review=r.repo.prepare(d.id)
    missing={v.split('.')[1]:'Вымышленное подтверждение' for v in item['required_variables'] if v.startswith('work.') and not review.context['work'].get(v.split('.')[1])}
    library.save_supplement(review.context,{'work':missing})
    # The regular repository must see the saved facts and preserve them in its snapshot.
    ready=r.repo.prepare(d.id)
    assert all(ready.context['work'][k]==v for k,v in missing.items())
    preview,_=r.repo.preview(d.id);assert preview.exists()
    registered=register(r,r.repo.load(d.id));old_bytes=r.repo.verify_document(registered.id).read_bytes()
    library.configure(item['code'],restore=True)
    assert r.repo.verify_document(registered.id).read_bytes()==old_bytes


def test_standard_test_copy_and_existing_snapshot_context(library):
    r=library;item=next(i for i in catalog() if i['document_type']=='contract_person')
    current=r.repo.configure(item['code'],restore=True)
    assert r.repo.templates.test_document(current['effective_version_id']).exists()
    # An old immutable context is still accepted without the T06 supplemental roots.
    old=sample()
    for root in ('work','commission','modules','documents'):old.pop(root)
    assert r.repo.templates.complete_context(old)['work']


def test_admission_standard_uses_training_facts(records):
    from test_admission import admission,context,TODAY
    from proficentr.admission import AdmissionRepository
    r=records;r.admission=AdmissionRepository(r.db.connection,r.storage);r.key=r.members[0].id
    context(r)
    for field in ('director_name','director_position'):
        r.db.connection.execute(f'UPDATE training_center SET {field}=? WHERE id=1',('Вымышленный руководитель',))
    r.db.connection.commit()
    lib=WorkingLibrary(r.db.connection,r.storage)
    item=next(i for i in catalog() if i['document_type']=='application' and i['variant']=='admission-dpo')
    selected=lib.configure(item['code'],restore=True)
    original=lib.context('training',r.key)
    lib.save_supplement(original,{'work':{'study_form':'Очная','education_basis':'Вымышленный диплом СПО'}})
    key=r.admission.prepare_form(r.key,'application',template_id=selected['effective_version_id'],day=TODAY,
        responsible='Тестов',expected_signature=r.admission.review(r.key).signature)
    assert r.admission.file(key).exists()


def test_upgrade_from_n02_keeps_user_template_and_active_choice(tmp_path,monkeypatch):
    from proficentr import database as module
    from proficentr.document_templates import TemplateRepository
    migrations=module.MIGRATIONS
    source=tmp_path/'custom.docx';doc=Document();doc.add_paragraph('Собственный {{ center.full_name }}');doc.save(source)
    with monkeypatch.context() as patch:
        patch.setattr(module,'MIGRATIONS',migrations[:-1])
        # Версия N02 ещё не записывала события библиотеки T06.
        patch.setattr(TemplateRepository,'_record_library_selection',lambda *args,**kwargs:None)
        storage=prepare_storage(tmp_path/'old')
        with open_database(storage) as db:
            version=TemplateRepository(db.connection,storage).register(source,'application','admission-dpo','Пользовательская форма')
    with open_database(storage) as db:
        library=WorkingLibrary(db.connection,storage)
        item=next(i for i in catalog() if i['document_type']=='application' and i['variant']=='admission-dpo')
        assert library.current(item['code'])['origin']=='custom'
        assert library.templates.active_version('application','admission-dpo').id==version.id
        assert library.templates.source(version.id).path.read_bytes()==source.read_bytes()


def test_legacy_version_actions_are_visible_in_library_history(library,tmp_path):
    r=library;item=catalog()[0];code=item['code'];standard=r.repo.configure(code,restore=True)
    source=tmp_path/'other.docx';doc=Document();doc.add_paragraph('{{ center.full_name }}');doc.save(source)
    custom=r.repo.templates.register(source,item['document_type'],item['variant'],'Своя форма')
    assert r.repo.current(code)['origin']=='custom' and r.repo.current(code)['effective_version_id']==custom.id
    r.repo.templates.activate(standard['effective_version_id'])
    assert r.repo.current(code)['origin']=='standard'
    assert len(r.repo.history(code))==3
