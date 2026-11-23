"""Вымышленные документы; комплектность не подменяет юридическую экспертизу форм."""
from dataclasses import asdict,replace
from datetime import date
import json
import sqlite3
from pathlib import Path

from docx import Document
from PySide6.QtGui import QImage,QColor
import pytest

from test_contracts import records
from admission_fixtures import ready_for_admission
from proficentr.admission import AdmissionRepository,AdmissionRule,AdmissionError
from proficentr.database import Database,MIGRATIONS
from proficentr.enrollment_order_repository import EnrollmentOrderRepository
from proficentr.enrollment_orders import EnrollmentOrder
from proficentr.program_repository import ProgramRepository
from proficentr.trainings import Training
from proficentr.services import open_database
from proficentr.storage import relocate_storage

TODAY=date.today().isoformat()


@pytest.fixture
def admission(records):
    records.admission=AdmissionRepository(records.db.connection,records.storage)
    records.key=records.members[0].id
    return records


def context(r,key=None,basis='contract',scope='Приём и обучение: необходимые сведения',conditions=None,note=''):
    key=key or r.key
    return r.admission.save_context(key,pd_basis=basis,pd_reference='Вымышленное основание обработки: 152-ФЗ, ст. 6',pd_scope=scope,
        conditions=conditions or {},note=note,responsible='Тестов Ответственный',day=TODAY,expected_signature=r.admission.review(key).signature)


def record(r,key='application',training=None,**changes):
    training=training or r.key
    args=dict(state='verified',reason='Проверен вымышленный бумажный оригинал.',day=TODAY,responsible='Тестов Ответственный',signed=True,reusable=True,education_basis='spo' if key=='dpo_education' else '')
    args.update(changes)
    return r.admission.record(training,key,expected_signature=r.admission.review(training).signature,**args)


def rule(r,**kwargs):
    item=AdmissionRule('special_test','Вымышленное требование',reference='Вымышленный утверждённый локальный акт',**kwargs)
    return r.admission.save_rule(item,responsible='Тестов Ответственный',day=TODAY)


def form(r,key='application',typ='application'):
    source=r.source.parent/(typ+'.docx');doc=Document()
    doc.add_paragraph('TEST ONLY {{ student.full_name }} / {{ program.name }} / {{ admission.title }} / {{ admission.scope }}')
    doc.save(source)
    template=r.admission.templates.register(source,typ,'admission','Вымышленная тестовая форма')
    return r.admission.prepare_form(r.key,key,template_id=template.id,day=TODAY,responsible='Тестов',expected_signature=r.admission.review(r.key).signature)


def png(path):
    image=QImage(2,2,QImage.Format.Format_RGB32);image.fill(QColor('red'));assert image.save(str(path),'PNG');return path


@pytest.mark.parametrize('kind,hours,required',[
    ('dpo_upskilling',16,'dpo_education'),('dpo_retraining',250,'dpo_education'),
    ('po_initial_training',1,'po_approved'),('po_retraining',5,'po_profession_retraining'),('po_upskilling',7,'po_profession_upskilling')])
def test_program_kinds_requirements_and_real_hours(admission,kind,hours,required):
    r=admission
    program=ProgramRepository(r.db.connection).save(replace(r.program,id=None,full_name='Вымышленная '+kind,kind=kind,direction='dpo' if kind.startswith('dpo') else 'po',hours=hours))
    t=r.trainings.save(Training(student_id=r.first.id,program_id=program.id,start_date='2020-01-01',end_date='2020-01-02'))
    review=r.admission.review(t.id)
    assert any(i.key==required and i.required for i in review.items)
    ready_for_admission(r.db.connection,r.storage,[t.id])
    assert r.admission.review(t.id).ready


@pytest.mark.parametrize('kind,hours,minimum',[('dpo_upskilling',15,16),('dpo_retraining',249,250)])
def test_dpo_hours_cannot_be_confirmed_away(admission,kind,hours,minimum):
    r=admission
    p=ProgramRepository(r.db.connection).save(replace(r.program,id=None,full_name='Вымышленная '+kind,kind=kind,hours=hours))
    t=r.trainings.save(Training(student_id=r.first.id,program_id=p.id,start_date='2020-01-01',end_date='2020-01-02'))
    assert any(f'минимум {minimum}' in p for p in r.admission.review(t.id).problems)


def test_no_assumed_education_or_consent_and_all_states(admission):
    r=admission;initial=r.admission.review(r.key)
    assert not initial.ready
    assert any('правовое основание' in p for p in initial.problems)
    context(r)
    review=r.admission.review(r.key)
    consent=next(i for i in review.items if i.key=='pd_consent')
    assert consent.status=='not_required' and not consent.required and consent.reason
    record(r,state='received',signed=False)
    assert not r.admission.review(r.key).ready
    record(r,state='absent')
    assert next(i for i in r.admission.review(r.key).items if i.key=='application').status=='absent'
    with pytest.raises(AdmissionError,match='подписание'):
        record(r,signed=False)
    record(r,state='not_required',reason='Вымышленный локальный акт допускает обращение без отдельной формы.')
    record(r,'dpo_education',signed=False)
    assert r.admission.review(r.key).ready
    with pytest.raises(AdmissionError,match='не отменяется'):
        record(r,'dpo_education',state='not_required')


def test_separate_generated_document_unsigned_historical_snapshot_and_copy(admission,tmp_path):
    r=admission;context(r)
    before=asdict(r.trainings.load(r.key))
    doc_id=form(r);original=r.admission.file(doc_id).read_bytes()
    item=next(i for i in r.admission.review(r.key).items if i.key=='application')
    assert item.status=='prepared' and not r.admission.document_event(doc_id)['signed']
    assert not r.admission.review(r.key).ready
    assert r.admission.document(doc_id)['template_id']
    assert r.admission.document(doc_id)['template_sha256']
    snapshot=json.loads(r.admission.document(doc_id)['context_snapshot'])
    assert snapshot['context']['program']['name']==r.program.full_name
    with pytest.raises(AdmissionError,match='подписание'):
        record(r,document_id=doc_id,signed=False)
    record(r,document_id=doc_id)
    r.students.save(replace(r.students.load(r.first.id),surname='Другаяфамилия'))
    assert r.admission.file(doc_id).read_bytes()==original
    assert r.admission.copy(doc_id,tmp_path/'same.docx').read_bytes()==original
    assert asdict(r.trainings.load(r.key))==before


def test_stale_changes_rules_and_program_do_not_silently_reuse_application(admission):
    r=admission;context(r);record(r);record(r,'dpo_education')
    old=r.admission.review(r.key);assert old.ready
    t=r.trainings.load(r.key);r.trainings.save(replace(t,end_date='2020-04-03'))
    review=r.admission.review(r.key);assert not review.ready
    assert any('условия обучения изменились' in p for p in review.problems)
    old_doc=next(i.document_id for i in review.items if i.key=='application')
    with pytest.raises(AdmissionError,match='прежним условиям'):
        record(r,document_id=old_doc)
    with pytest.raises(AdmissionError,match='изменились'):
        r.admission.record(r.key,'application',state='verified',reason='Тест',day=TODAY,responsible='Тест',signed=True,expected_signature=old.signature)
    base=next(r for r in r.admission.rules() if r.rule_key=='application')
    updated=r.admission.save_rule(replace(base,reference='Новая редакция локального акта'),responsible='Тестов',day=TODAY,expected_version=base.version)
    assert updated.version==2
    assert r.db.connection.execute("SELECT count(*) FROM admission_rules WHERE rule_key='application'").fetchone()==(2,)


def test_program_specific_conditional_requirements_need_explicit_applicability(admission):
    r=admission
    rule(r,program_id=r.program.id,condition_tag='Вредные факторы',source='special',scope='medical',document_type='evidence')
    context(r)
    assert any('применимость' in p for p in r.admission.review(r.key).problems)
    with pytest.raises(AdmissionError,match='неприменимости'):
        context(r,conditions={'Вредные факторы':False})
    context(r,conditions={'Вредные факторы':False},note='Вымышленная работа не включает указанные факторы.')
    item=next(i for i in r.admission.review(r.key).items if i.key=='special_test')
    assert not item.required and item.status=='not_required'
    context(r,conditions={'Вредные факторы':True})
    item=next(i for i in r.admission.review(r.key).items if i.key=='special_test')
    assert item.required and item.status=='required'
    other=ProgramRepository(r.db.connection).save(replace(r.program,id=None,full_name='Другая программа'))
    t=r.trainings.save(Training(student_id=r.first.id,program_id=other.id,start_date='2020-01-01',end_date='2020-01-02'))
    assert not any(i.key=='special_test' for i in r.admission.review(t.id).items)


def test_reuse_same_student_without_scan_copy_and_reject_other_student(admission,tmp_path):
    r=admission;context(r)
    record(r,'dpo_education',source=png(tmp_path/'scan.png'))
    document=next(i.document_id for i in r.admission.review(r.key).items if i.key=='dpo_education')
    path=r.admission.file(document);data=path.read_bytes()
    t=r.trainings.save(Training(student_id=r.first.id,program_id=r.program.id,start_date='2021-01-01',end_date='2021-02-01'))
    context(r,t.id)
    assert any(d['id']==document for d in r.admission.reusable_documents(t.id,'dpo_education'))
    record(r,'dpo_education',training=t.id,document_id=document,signed=False)
    assert len(list((r.storage.root/'attachments'/'admission').glob('*')))==1
    assert r.admission.file(document).read_bytes()==data
    context(r,r.members[1].id)
    with pytest.raises(AdmissionError,match='другому слушателю'):
        record(r,'dpo_education',training=r.members[1].id,document_id=document)


def test_consent_scope_reuse_and_revocation_are_explicit(admission):
    r=admission;context(r,basis='consent')
    consent=form(r,'pd_consent','consent')
    record(r,'pd_consent',document_id=consent)
    record(r);record(r,'dpo_education');assert r.admission.review(r.key).ready
    t=r.trainings.save(Training(student_id=r.first.id,program_id=r.program.id,start_date='2021-01-01',end_date='2021-02-01'))
    context(r,t.id,basis='consent')
    record(r,'pd_consent',training=t.id,document_id=consent)
    r.admission.revoke(consent,day=TODAY,responsible='Тестов',reason='Получен вымышленный отзыв.')
    assert any('отозвано' in p for p in r.admission.review(r.key).problems)
    assert any('отозвано' in p for p in r.admission.review(t.id).problems)
    with pytest.raises(AdmissionError,match='не восстанавливается'):
        record(r,'pd_consent',document_id=consent)
    context(r,basis='law')
    assert not next(i.required for i in r.admission.review(r.key).items if i.key=='pd_consent')


def test_optional_extra_consent_does_not_block_enrollment_and_wrong_scope_rejected(admission):
    r=admission
    with pytest.raises(AdmissionError,match='не является условием'):
        rule(r,document_type='consent',scope='Публикация фотографий',sign_required=True)
    rule(r,document_type='consent',scope='Публикация фотографий',sign_required=True,required=False,reusable=True)
    ready_for_admission(r.db.connection,r.storage,[r.key]);assert r.admission.review(r.key).ready
    context(r,basis='consent',scope='Приём')
    doc=form(r,'pd_consent','consent');record(r,'pd_consent',document_id=doc)
    with pytest.raises(AdmissionError,match='другому требованию'):
        record(r,'special_test',document_id=doc)


def order(r):
    repo=EnrollmentOrderRepository(r.db.connection,r.storage)
    repo.templates.register(r.source,'enrollment_order','dpo-individual','Вымышленная форма')
    draft=repo.save(EnrollmentOrder(order_date='2020-02-01'),[r.key]);registered=repo.register(draft.id,repo.prepare(draft.id).signature)
    return repo,registered


def test_new_enrollment_blocked_without_checklist_and_proof_frozen_after_signing(admission):
    r=admission;repo,registered=order(r)
    protected=asdict(registered);data=repo.verify_document(registered.id).read_bytes()
    with pytest.raises(AdmissionError,match='комплект приёма не готов'):
        repo.confirm_signing(registered.id,signed_date='2020-02-02',confirmed_by='Тестов',grounds_checked=True,education_checked=True)
    assert not r.db.connection.execute('SELECT * FROM training_enrollments').fetchall()
    assert asdict(repo.load(registered.id))==protected and repo.verify_document(registered.id).read_bytes()==data
    ready_for_admission(r.db.connection,r.storage,[r.key])
    signed=repo.confirm_signing(registered.id,signed_date='2020-02-02',confirmed_by='Тестов',grounds_checked=True,education_checked=True)
    proof=r.db.connection.execute('SELECT assessment FROM admission_enrollment_checks').fetchone()[0]
    assert r.admission.review(r.key).historical and r.admission.review(r.key).ready
    old=next(v for v in r.admission.rules() if v.rule_key=='application')
    r.admission.save_rule(replace(old,reference='Изменённый акт'),responsible='Тестов',day=TODAY,expected_version=old.version)
    assert r.admission.review(r.key).ready
    assert r.db.connection.execute('SELECT assessment FROM admission_enrollment_checks').fetchone()[0]==proof
    assert asdict(repo.load(signed.id))==asdict(signed)
    assert repo.verify_document(signed.id).read_bytes()==data
    with pytest.raises(AdmissionError,match='завершён'):
        context(r)


def test_signed_records_cannot_be_replaced_and_corruption_is_detected(admission):
    r=admission;context(r);doc=form(r);record(r,document_id=doc)
    path=r.admission.file(doc)
    for table in ('admission_documents','admission_document_events','admission_checks','admission_context_events','admission_rules'):
        for sql in (f'DELETE FROM {table}',f'UPDATE {table} SET id=id',f'INSERT OR REPLACE INTO {table} SELECT * FROM {table}'):
            with pytest.raises(sqlite3.IntegrityError),r.db.connection:r.db.connection.execute(sql)
    path.write_bytes(b'corrupted')
    assert any('SHA-256' in p for p in r.admission.review(r.key).problems)
    with pytest.raises(AdmissionError,match='SHA-256'):r.admission.file(doc)


def test_restart_migration16_and_relocation_keep_documents(admission,tmp_path,monkeypatch):
    r=admission;context(r);doc=form(r);record(r,document_id=doc)
    before=r.admission.file(doc).read_bytes();review=r.admission.review(r.key)
    with open_database(r.storage) as db:
        restarted=AdmissionRepository(db.connection,r.storage)
        assert restarted.review(r.key)==review
        assert restarted.file(doc).read_bytes()==before
        monkeypatch.setattr('proficentr.storage.settings.save_directory',lambda path:None)
        target=relocate_storage(r.storage,db.connection,tmp_path/'moved')
    with open_database(target) as db:
        moved=AdmissionRepository(db.connection,target)
        assert moved.review(r.key)==review and moved.file(doc).read_bytes()==before


def test_migration15_to16_preserves_prior_records(tmp_path,monkeypatch):
    import proficentr.database as module
    path=tmp_path/'legacy.sqlite3'
    with monkeypatch.context() as patch:
        patch.setattr(module,'MIGRATIONS',MIGRATIONS[:15])
        with Database(path) as db:
            with db.connection:db.connection.execute("INSERT INTO students(surname,first_name,birth_date) VALUES ('Примеров','Иван','1990-01-01')")
            old=db.connection.execute('SELECT * FROM students').fetchall()
    with Database(path) as db:
        assert db.connection.execute('SELECT * FROM students').fetchall()==old
        assert db.connection.execute('SELECT count(*) FROM admission_documents').fetchone()==(0,)
        assert db.connection.execute('SELECT max(version) FROM schema_migrations').fetchone()==(16,)
    backup=next((tmp_path/'migration-backups').glob('*.sqlite3'))
    with sqlite3.connect(backup) as db:assert db.execute('SELECT max(version) FROM schema_migrations').fetchone()==(15,)


def test_write_and_sql_failures_leave_no_registered_document(admission,monkeypatch):
    r=admission;context(r)
    from proficentr import admission as module
    original=module.generate_document
    def fail(*args,**kwargs):raise OSError('Synthetic disk full')
    with monkeypatch.context() as patch:
        patch.setattr(module,'generate_document',fail)
        with pytest.raises(AdmissionError):form(r)
    assert not r.db.connection.execute('SELECT * FROM admission_documents').fetchall()
    r.db.connection.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON admission_checks BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(AdmissionError):form(r)
    assert not r.db.connection.execute('SELECT * FROM admission_documents').fetchall()
    assert not list((r.storage.root/'documents'/'admission').glob('*'))


@pytest.mark.parametrize('basis',['','ineligible'])
def test_dpo_ineligible_education_cannot_be_verified(admission,basis):
    r=admission;context(r)
    with pytest.raises(AdmissionError,match='СПО/ВО'):
        record(r,'dpo_education',education_basis=basis)
    assert not r.admission.review(r.key).ready
    assert not r.db.connection.execute('SELECT * FROM admission_documents').fetchall()


def test_expired_evidence_not_reused_and_consent_scope_must_match(admission):
    r=admission;context(r)
    r.admission.record(r.key,'dpo_education',state='verified',reason='Вымышленная справка об обучении',responsible='Тестов',day='2020-01-01',
        valid_until='2020-12-31',reusable=True,education_basis='studying_vo',expected_signature=r.admission.review(r.key).signature)
    assert any('Срок действия' in p for p in r.admission.review(r.key).problems)
    assert not r.admission.reusable_documents(r.key,'dpo_education')
    context(r,basis='consent',scope='Только приём')
    doc=form(r,'pd_consent','consent');record(r,'pd_consent',document_id=doc)
    t=r.trainings.save(Training(student_id=r.first.id,program_id=r.program.id,start_date='2021-01-01',end_date='2021-02-01'))
    context(r,t.id,basis='consent',scope='Другая цель обработки')
    with pytest.raises(AdmissionError,match='области действия'):
        record(r,'pd_consent',training=t.id,document_id=doc)


def test_preexisting_signed_order_remains_unknown_readonly_after_migration(admission,tmp_path):
    from legacy_schema import remove_admission_schema
    r=admission;repo,registered=order(r);ready_for_admission(r.db.connection,r.storage,[r.key])
    signed=repo.confirm_signing(registered.id,signed_date='2020-02-02',confirmed_by='Тестов',grounds_checked=True,education_checked=True)
    original=repo.verify_document(signed.id).read_bytes();legacy=tmp_path/'pre035.sqlite3'
    with sqlite3.connect(legacy) as copy:
        r.db.connection.backup(copy);remove_admission_schema(copy);copy.execute('DELETE FROM schema_migrations WHERE version=16')
    with Database(legacy) as db:
        upgraded=EnrollmentOrderRepository(db.connection,r.storage)
        assert upgraded.load(signed.id)==signed and upgraded.verify_document(signed.id).read_bytes()==original
        review=AdmissionRepository(db.connection,r.storage).review(r.key)
        assert review.historical and not review.items and 'не фиксировалась' in review.problems[0]
        assert db.connection.execute('SELECT * FROM admission_checks').fetchall()==[]
