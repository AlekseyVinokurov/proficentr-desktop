"""Вымышленные завершения по неизменяемым подтверждённым основаниям."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import hashlib
import json
import sqlite3
from uuid import uuid4

from docx import Document
import pytest

from test_contracts import records
from test_enrollment_orders import orders
from test_attestations import attestations, draft as protocol_draft, register as protocol_register, sign as protocol_sign
from proficentr.attestations import AttestationProtocol, AttestationResult
from proficentr.completion_orders import CompletionOrder, CompletionEntry, CompletionError
from proficentr.completion_repository import CompletionRepository
from proficentr.database import Database
from proficentr.program_repository import ProgramRepository
from proficentr.students import Student
from proficentr.trainings import Training, TrainingGroup
from proficentr.storage import relocate_storage
import proficentr.completion_repository as module
import proficentr.database as db_module


@pytest.fixture
def completions(attestations):
    r=attestations
    r.protocols=r.repo
    r.protocol=protocol_sign(r,protocol_register(r,protocol_draft(r,ids=[t.id for t in r.members],group_id=r.group.id)))
    r.repo=CompletionRepository(r.db.connection,r.storage)
    doc=Document(); doc.add_paragraph('{{ document.number }} {{ document.date }} {{ order.student_count }} {{ program.direction }} {{ program.kind }}')
    doc.add_paragraph('{{ center.full_name }} {{ center.director_name }} {{ program.name }} {{ completion.date }} {{ completion.basis }}')
    table=doc.add_table(rows=3,cols=3)
    table.cell(0,0).text='{%tr for row in students %}'
    table.cell(1,0).text='{{ row.full_name }} {{ row.start_date }} {{ row.end_date }}'
    table.cell(1,1).text='{{ row.completion_date }} {{ row.attestation_result }} {{ row.protocol_number }} {{ row.protocol_date }} {{ row.attestation_date }}'
    table.cell(1,2).text='{{ row.qualification }} {{ row.qualification_grade }} {{ row.completion_basis }}'
    table.cell(2,0).text='{%tr endfor %}'
    doc.save(r.source)
    for direction in ('dpo','po'):
        for kind in ('individual','group'):
            r.repo.templates.register(r.source,'completion_order',direction+'-'+kind,'Вымышленный приказ о завершении')
    return r


def draft(r, ids=None, dates=None, **changes):
    ids=ids if ids is not None else [r.members[0].id]
    entries=[CompletionEntry(key,(dates or {}).get(key,'2020-04-01')) for key in ids]
    return r.repo.save(replace(CompletionOrder(order_date='2020-04-02'),**changes),entries)


def register(r, order): return r.repo.register(order.id,r.repo.prepare(order.id).signature)


def sign(r, order, **changes):
    args=dict(signed_date='2025-01-02',confirmed_by='Ответственный Примеров',conditions_checked=True); args.update(changes)
    return r.repo.confirm_signing(order.id,**args)


def attempt(r, result, day='2021-01-01', ids=None):
    ids=ids or [r.members[0].id]
    group=r.group.id if len(ids)>1 else None
    p=r.protocols.save(AttestationProtocol(protocol_date=day,attestation_date=day,group_id=group),[AttestationResult(key,result) for key in ids])
    p=r.protocols.register(p.id,r.protocols.prepare(p.id).signature)
    return r.protocols.confirm_signing(p.id,signed_date='2025-01-01',confirmed_by='Тестовый ответственный')


@pytest.mark.parametrize('kind,count',[('individual',1),('group',1),('group',2)])
def test_dpo_individual_group_subset_states_and_docx(completions,kind,count):
    r=completions; ids=[t.id for t in r.members[:count]]
    before={name:r.db.connection.execute(f'SELECT * FROM {name}').fetchall() for name in ('trainings','training_enrollments','attestation_protocols','attestation_results','enrollment_orders')}
    p=draft(r,ids,kind=kind,group_id=r.group.id if kind=='group' else None)
    assert all(r.trainings.load(key).state=='enrolled' for key in ids)
    path,review=r.repo.preview(p.id)
    assert 'ПРОЕКТ ПРИКАЗА О ЗАВЕРШЕНИИ' in Document(path).paragraphs[0].text
    assert not r.db.connection.execute('SELECT * FROM completion_sequences').fetchall()
    p=r.repo.register(p.id,review.signature)
    assert p.state=='registered' and p.number=='О-2020-0001' and p.registered_at
    assert not r.db.connection.execute('SELECT * FROM training_completions').fetchall()
    original=r.repo.verify_document(p.id).read_bytes(); rows=Document(r.repo.verify_document(p.id)).tables[0].rows
    assert len(rows)==count and '2020-04-01' in rows[0].cells[1].text
    signed=sign(r,p)
    assert signed.state=='signed' and signed.conditions_checked==1 and signed.confirmed_at and signed.confirmed_by
    assert signed.snapshot==p.snapshot and r.repo.verify_document(p.id).read_bytes()==original
    for i,t in enumerate(r.members):
        assert r.trainings.load(t.id).state==('completed' if i<count else 'enrolled')
        fact=r.repo.completion(t.id)
        if i<count: assert fact[:3]==(p.id,r.protocol.id,'2020-04-01')
        else: assert fact is None
    assert [row[-1] for row in r.trainings.list_trainings(group_id=r.group.id)].count('completed')==count
    for name,values in before.items(): assert r.db.connection.execute(f'SELECT * FROM {name}').fetchall()==values
    assert not r.db.connection.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('outcome',['failed','absent'])
def test_reject_latest_unsuccessful_and_never_exclude(completions,outcome):
    r=completions; attempt(r,outcome)
    with pytest.raises(CompletionError,match='успешной'): draft(r)
    assert r.trainings.load(r.members[0].id).state=='enrolled'
    assert r.repo.completion(r.members[0].id) is None


@pytest.mark.parametrize('state',['draft','registered','none'])
def test_no_signed_protocol_is_not_a_basis(completions,state):
    r=completions
    t=r.trainings.save(Training(student_id=r.first.id,program_id=r.program.id,start_date='2022-01-01',end_date='2022-02-01'))
    from proficentr.enrollment_orders import EnrollmentOrder
    o=r.orders.save(EnrollmentOrder(order_date='2022-01-01'),[t.id]); o=r.orders.register(o.id,r.orders.prepare(o.id).signature)
    r.orders.confirm_signing(o.id,signed_date='2022-01-02',confirmed_by='Тест',grounds_checked=True,education_checked=True)
    if state!='none':
        p=r.protocols.save(AttestationProtocol(protocol_date='2022-02-01',attestation_date='2022-02-01'),[AttestationResult(t.id,'passed')])
        if state=='registered': r.protocols.register(p.id,r.protocols.prepare(p.id).signature)
    with pytest.raises(CompletionError): draft(r,[t.id],dates={t.id:'2022-02-01'})


def test_reject_without_enrollment_and_excluded(completions):
    r=completions
    t=r.trainings.save(Training(student_id=r.first.id,program_id=r.program.id,start_date='2022-01-01',end_date='2022-02-01'))
    with pytest.raises(CompletionError,match='зачисление'): draft(r,[t.id])
    group=r.trainings.save_group(TrainingGroup(name='Исключённая группа',program_id=r.program.id,start_date='2023-01-01',end_date='2023-02-01'))
    t=r.trainings.add_students(group.id,[r.first.id])[0]; r.trainings.exclude(t.id)
    with pytest.raises(CompletionError): draft(r,[t.id])


@pytest.mark.parametrize('result',['failed','absent','passed'])
def test_changed_basis_after_registration_requires_new_order(completions,result):
    r=completions; p=register(r,draft(r,dates={r.members[0].id:'2021-02-01'}))
    original=r.repo.verify_document(p.id).read_bytes(); attempt(r,result)
    with pytest.raises(CompletionError,match='оформление'): sign(r,p)
    assert r.repo.load(p.id).state=='registered' and r.repo.verify_document(p.id).read_bytes()==original
    assert not r.db.connection.execute('SELECT * FROM training_completions').fetchall()


def test_retries_historical_and_stable_basis_after_completion(completions):
    r=completions; attempt(r,'failed','2021-01-01'); passed=attempt(r,'passed','2021-02-01')
    historical=attempt(r,'failed','2019-01-01')
    p=sign(r,register(r,draft(r,dates={r.members[0].id:'2021-02-01'})))
    before=r.repo.completion(r.members[0].id); snapshot=r.repo.entries(p.id)[0].basis_snapshot
    assert before[1]==passed.id and before[1]!=historical.id
    attempt(r,'failed','2022-01-01')
    assert r.protocols.current_confirmed_result(r.members[0].id)[4]=='failed'
    assert r.repo.completion(r.members[0].id)==before
    assert r.repo.entries(p.id)[0].basis_snapshot==snapshot
    assert r.trainings.load(r.members[0].id).state=='completed'


@pytest.mark.parametrize('day',['2019-01-01','2020-03-31','2020-02-30',''])
def test_invalid_actual_dates(completions,day):
    r=completions
    with pytest.raises(ValueError): draft(r,dates={r.members[0].id:day})


def test_mixed_actual_dates_are_per_training_and_not_order_date(completions):
    r=completions; ids=[t.id for t in r.members]
    p=register(r,draft(r,ids,dates={ids[0]:'2020-04-03',ids[1]:'2020-04-10'},kind='group',group_id=r.group.id))
    c=json.loads(p.snapshot)['context']; assert c['completion']['date']==''
    assert [s['completion_date'] for s in c['students']]==['2020-04-03','2020-04-10']
    assert c['document']['date']=='2020-04-02'
    rows=Document(r.repo.verify_document(p.id)).tables[0].rows
    assert '2020-04-03' in rows[0].cells[1].text and '2020-04-10' in rows[1].cells[1].text
    sign(r,p)
    assert r.repo.completion(ids[1])[2]=='2020-04-10'
    assert all(r.trainings.load(key).end_date=='2020-04-01' for key in ids)


def test_mixed_dates_require_template_row_dates(completions):
    r=completions; doc=Document(); doc.add_paragraph('{{ completion.date }}'); doc.save(r.source)
    r.repo.templates.register(r.source,'completion_order','dpo-group','Форма без индивидуальных дат')
    p=draft(r,[t.id for t in r.members],kind='group',group_id=r.group.id,dates={r.members[1].id:'2020-04-10'})
    with pytest.raises(CompletionError,match='row.completion_date'): register(r,p)


@pytest.mark.parametrize('changes',[{'conditions_checked':False},{'conditions_checked':1},{'confirmed_by':''},{'signed_date':'2019-01-01'},{'signed_date':'9999-01-01'},{'signed_date':'2020-04-02'}])
def test_signing_requires_checks_and_dates(completions,changes):
    r=completions; p=register(r,draft(r,dates={r.members[0].id:'2020-04-03'}))
    with pytest.raises(ValueError): sign(r,p,**changes)
    assert not r.db.connection.execute('SELECT * FROM training_completions').fetchall()


def test_draft_cannot_sign_and_duplicate_completion(completions):
    r=completions; p=draft(r)
    with pytest.raises(CompletionError): sign(r,p)
    first=register(r,p); second=register(r,draft(r)); sign(r,first)
    with pytest.raises(CompletionError): sign(r,first)
    with pytest.raises(CompletionError): sign(r,second)
    with pytest.raises(CompletionError): draft(r)


@pytest.mark.parametrize('state',['registered','signed'])
@pytest.mark.parametrize('sql',["UPDATE completion_orders SET note='other' WHERE id=?",'DELETE FROM completion_orders WHERE id=?',
    "UPDATE completion_order_trainings SET completion_date='2021-01-01' WHERE order_id=?",'DELETE FROM completion_order_trainings WHERE order_id=?',
    "UPDATE completion_order_trainings SET basis_snapshot='{}' WHERE order_id=?",
    "INSERT OR REPLACE INTO completion_orders(id,order_date,kind,direction) VALUES (?,'2020-01-01','individual','dpo')"])
def test_registered_sql_immutable(completions,state,sql):
    r=completions; p=register(r,draft(r))
    if state=='signed': p=sign(r,p)
    with pytest.raises(sqlite3.IntegrityError),r.db.connection: r.db.connection.execute(sql,(p.id,))
    with pytest.raises(CompletionError): r.repo.save(p,r.repo.entries(p.id))


def test_completed_training_and_fact_protected(completions):
    r=completions; p=sign(r,register(r,draft(r))); key=r.members[0].id
    for sql in ("UPDATE trainings SET end_date='2022-01-01' WHERE id=?",'DELETE FROM trainings WHERE id=?',
        'DELETE FROM training_completions WHERE training_id=?',"UPDATE training_completions SET completion_date='2022-01-01' WHERE training_id=?"):
        with pytest.raises(sqlite3.IntegrityError),r.db.connection: r.db.connection.execute(sql,(key,))
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute('INSERT OR REPLACE INTO training_completions(training_id,order_id,protocol_id,completion_date) VALUES (?,?,?,?)',(key,p.id,r.protocol.id,'2020-04-01'))
    with pytest.raises(ValueError): r.trainings.save(replace(r.members[0],end_date='2021-01-01'))
    with pytest.raises(ValueError): r.trainings.exclude(key)


def test_sql_cannot_forge_signing_or_unsigned_completion(completions):
    r=completions; p=register(r,draft(r))
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute("UPDATE completion_orders SET state='signed',signed_date='2025-01-02',confirmed_at=CURRENT_TIMESTAMP,confirmed_by='Тест',conditions_checked=1,revision=revision+1 WHERE id=?",(p.id,))
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute('INSERT INTO training_completions(training_id,order_id,protocol_id,completion_date) VALUES (?,?,?,?)',(r.members[0].id,p.id,r.protocol.id,'2020-04-01'))
    attempt(r,'failed')
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute('INSERT INTO training_completions(training_id,order_id,protocol_id,completion_date) VALUES (?,?,?,?)',(r.members[0].id,p.id,r.protocol.id,'2020-04-01'))


def test_po_qualification_snapshot_and_individual_training(completions):
    r=completions
    program=ProgramRepository(r.db.connection).save(replace(r.program,id=None,direction='po',kind='po_retraining',full_name='Вымышленная ПО'))
    t=r.trainings.save(Training(student_id=r.first.id,program_id=program.id,start_date='2021-01-01',end_date='2021-02-01'))
    from proficentr.enrollment_orders import EnrollmentOrder
    source=r.orders.templates.source(r.orders.templates.list_versions('enrollment_order')[0].id).path
    r.orders.templates.register(source,'enrollment_order','po-individual','Вымышленный приказ ПО')
    o=r.orders.save(EnrollmentOrder(order_date='2021-01-01'),[t.id]); o=r.orders.register(o.id,r.orders.prepare(o.id).signature)
    r.orders.confirm_signing(o.id,signed_date='2021-01-02',confirmed_by='Тест',grounds_checked=True,education_checked=True)
    p=r.protocols.save(AttestationProtocol(protocol_date='2021-02-01',attestation_date='2021-02-01',direction='po'),[AttestationResult(t.id,'passed','зачёт','Вымышленная квалификация','3 разряд')])
    p=r.protocols.register(p.id,r.protocols.prepare(p.id).signature)
    r.protocols.confirm_signing(p.id,signed_date='2021-02-02',confirmed_by='Тест')
    o=sign(r,register(r,draft(r,[t.id],direction='po',dates={t.id:'2021-02-01'},order_date='2021-02-03')))
    assert r.trainings.load(t.id).state=='completed'
    c=json.loads(o.snapshot)['context']; assert c['program']['direction']=='po'
    assert c['students'][0]['qualification_grade']=='3 разряд'
    assert c['students'][0]['qualification']=='Вымышленная квалификация'
    assert c['protocol']['kind']=='Квалификационный экзамен'


def test_numbers_history_uniqueness_and_independence(completions):
    r=completions
    other={table:r.db.connection.execute(f'SELECT * FROM {table}').fetchall() for table in ('contract_sequences','enrollment_order_sequences','attestation_sequences')}
    register(r,draft(r,historical_number='О-2020-0010'))
    assert register(r,draft(r)).number=='О-2020-0011'
    register(r,draft(r,historical_number='О-2020-0002'))
    assert register(r,draft(r)).number=='О-2020-0012'
    assert register(r,draft(r,order_date='2021-01-01')).number=='О-2021-0001'
    register(r,draft(r,historical_number='Архив / 22'))
    with pytest.raises(CompletionError): register(r,draft(r,historical_number='архив   / 22'))
    for sql in ('DELETE FROM completion_sequences','UPDATE completion_sequences SET last_value=1','INSERT OR REPLACE INTO completion_sequences VALUES (2020,1)'):
        with pytest.raises(sqlite3.IntegrityError),r.db.connection: r.db.connection.execute(sql)
    for table,rows in other.items(): assert r.db.connection.execute(f'SELECT * FROM {table}').fetchall()==rows


def test_simultaneous_registration_numbers(completions):
    r=completions; drafts=[draft(r),draft(r)]; barrier=Barrier(2)
    def run(key):
        with Database(r.storage.database_path) as db:
            repo=CompletionRepository(db.connection,r.storage); review=repo.prepare(key); barrier.wait(timeout=10)
            return repo.register(key,review.signature).number
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(run,[p.id for p in drafts]))=={'О-2020-0001','О-2020-0002'}


@pytest.mark.parametrize('change',['student','center','template','date','basis'])
def test_review_rejects_changes(completions,change):
    r=completions; p=draft(r,dates={r.members[0].id:'2021-02-01'}); review=r.repo.prepare(p.id)
    if change=='student': r.students.save(replace(r.first,surname='Изменённый'))
    elif change=='center':
        with r.db.connection: r.db.connection.execute("UPDATE training_center SET director_name='Другой директор'")
    elif change=='template': r.repo.templates.register(r.source,'completion_order','dpo-individual','Новая версия')
    elif change=='date': r.repo.save(p,[CompletionEntry(r.members[0].id,'2021-02-02')])
    else: attempt(r,'passed')
    with pytest.raises(CompletionError): r.repo.register(p.id,review.signature)
    assert r.repo.load(p.id).state=='draft'


def test_registered_snapshot_survives_reference_edits_and_search(completions):
    r=completions; p=register(r,draft(r,kind='group',group_id=r.group.id)); original=r.repo.verify_document(p.id).read_bytes()
    r.students.save(replace(r.first,surname='Изменённый после регистрации'))
    ProgramRepository(r.db.connection).save(replace(r.program,full_name='Другая надпись программы',edition='2'))
    with r.db.connection: r.db.connection.execute("UPDATE training_center SET full_name='Другое название',director_name='Другой директор'")
    r.trainings.save_group(replace(r.group,name='Новый номер группы'))
    signed=sign(r,p)
    assert signed.snapshot==p.snapshot and r.repo.verify_document(p.id).read_bytes()==original
    for query in ('Тестов','ТЕСТ-01','О-2020','2020-04-02'): assert r.repo.search(query)[0][0]==p.id
    c=json.loads(p.snapshot)['context']; assert c['program']['name']=='Тестовая ДПО'
    copy=r.repo.document_copy(p.id); copy.write_bytes(b'edited copy')
    assert hashlib.sha256(r.repo.verify_document(p.id).read_bytes()).hexdigest()==p.document_sha256


@pytest.mark.parametrize('failure',['file','db','commit'])
def test_registration_rollback_file_and_number(completions,failure,monkeypatch):
    r=completions; p=draft(r); review=r.repo.prepare(p.id)
    if failure=='file':
        def fail(*a): raise OSError('synthetic')
        monkeypatch.setattr(module,'generate_document',fail)
    elif failure=='db':
        with r.db.connection: r.db.connection.execute("CREATE TRIGGER fail_completion BEFORE UPDATE ON completion_orders WHEN NEW.state='registered' BEGIN SELECT RAISE(ABORT,'test'); END")
    else:
        with r.db.connection:
            r.db.connection.execute('CREATE TABLE deferred_completion_test(id INTEGER REFERENCES students(id) DEFERRABLE INITIALLY DEFERRED)')
            r.db.connection.execute("CREATE TRIGGER fail_completion_commit AFTER UPDATE ON completion_orders WHEN NEW.state='registered' BEGIN INSERT INTO deferred_completion_test VALUES (999999); END")
    with pytest.raises(CompletionError): r.repo.register(p.id,review.signature)
    assert r.repo.load(p.id).state=='draft' and not r.db.connection.execute('SELECT * FROM completion_sequences').fetchall()
    assert not list((r.storage.root/'documents').glob('*/*/orders/completion-order-*.docx'))
    if failure!='file': assert list((r.storage.root/'exports'/'unregistered-completion-orders').glob('*/*.docx'))


def test_second_completion_failure_rolls_back_signing(completions):
    r=completions; p=register(r,draft(r,[t.id for t in r.members],kind='group',group_id=r.group.id))
    with r.db.connection:
        r.db.connection.execute(f"CREATE TRIGGER fail_second_completion BEFORE INSERT ON training_completions WHEN NEW.training_id={r.members[1].id} BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(CompletionError): sign(r,p)
    assert r.repo.load(p.id).state=='registered' and not r.db.connection.execute('SELECT * FROM training_completions').fetchall()
    assert all(r.trainings.load(t.id).state=='enrolled' for t in r.members)


def test_archive_damage_and_orphan_recovery_do_not_touch_enrollment_orders(completions):
    r=completions; p=register(r,draft(r)); path=r.repo.verify_document(p.id)
    enrollment=r.orders.search()[0][0]; original_order=r.orders.verify_document(enrollment).read_bytes()
    orphan=path.parent/('completion-order-'+uuid4().hex+'.docx'); orphan.write_bytes(path.read_bytes())
    assert r.repo.recover_unregistered()==1 and not orphan.exists()
    assert r.orders.recover_unregistered()==0
    assert r.orders.verify_document(enrollment).read_bytes()==original_order
    path.write_bytes(b'corrupt')
    with pytest.raises(CompletionError,match='SHA-256'): r.repo.document_copy(p.id)
    with pytest.raises(CompletionError): sign(r,p)
    assert r.repo.completion(r.members[0].id) is None


def test_template_direction_kind_and_missing_active(completions):
    r=completions; p=draft(r)
    wrong=next(t for t in r.repo.templates.list_versions('completion_order') if t.variant=='po-individual')
    p=r.repo.save(replace(p,template_id=wrong.id),r.repo.entries(p.id))
    with pytest.raises(CompletionError,match='Настройки'): register(r,p)
    with r.db.connection: r.db.connection.execute("UPDATE document_templates SET active=0 WHERE document_type='completion_order' AND variant='dpo-individual'")
    p=r.repo.save(replace(p,template_id=None),r.repo.entries(p.id))
    with pytest.raises(CompletionError,match='dpo-individual'): register(r,p)


def test_mixed_program_revision_and_wrong_direction_rejected(completions):
    r=completions
    with pytest.raises(CompletionError): draft(r,direction='po')
    with pytest.raises(CompletionError): draft(r,[t.id for t in r.members])
    with pytest.raises(CompletionError): draft(r,[r.members[0].id]*2)
    with pytest.raises(CompletionError): draft(r,kind='group',group_id=999)
    ProgramRepository(r.db.connection).save(replace(r.program,edition='2'))
    student=r.students.save(Student(surname='Третий',first_name='Пример',birth_date='1990-01-01'))
    t=r.trainings.add_students(r.group.id,[student.id])[0]
    from proficentr.enrollment_orders import EnrollmentOrder
    o=r.orders.save(EnrollmentOrder(order_date='2020-02-10',kind='group',group_id=r.group.id),[t.id]); o=r.orders.register(o.id,r.orders.prepare(o.id).signature)
    r.orders.confirm_signing(o.id,signed_date='2020-02-11',confirmed_by='Тест',grounds_checked=True,education_checked=True)
    attempt(r,'passed','2020-04-01',[t.id])
    with pytest.raises(CompletionError,match='редакция'): draft(r,[r.members[0].id,t.id],kind='group',group_id=r.group.id)


def test_migration11_preserves_v10_data_and_backup(completions,tmp_path,monkeypatch):
    r=completions; path=tmp_path/'v10.sqlite3'; migrations=db_module.MIGRATIONS
    monkeypatch.setattr(db_module,'MIGRATIONS',migrations[:10])
    with Database(path) as old:
        r.db.connection.backup(old.connection)
        with old.connection:
            for name, in old.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'completion%'").fetchall(): old.connection.execute(f'DROP TRIGGER {name}')
            for table in ('training_completions','completion_order_trainings','completion_orders','completion_sequences'): old.connection.execute(f'DROP TABLE {table}')
            old.connection.execute('DELETE FROM schema_migrations WHERE version=11')
        tables=[row[0] for row in old.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        before={table:old.connection.execute(f'SELECT * FROM {table}').fetchall() for table in tables}
    monkeypatch.setattr(db_module,'MIGRATIONS',migrations)
    with Database(path) as db:
        assert db.connection.execute('SELECT max(version) FROM schema_migrations').fetchone()==(11,)
        for table,rows in before.items():
            if table!='schema_migrations': assert db.connection.execute(f'SELECT * FROM {table}').fetchall()==rows
        assert db.connection.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
        assert not db.connection.execute('PRAGMA foreign_key_check').fetchall()
        assert CompletionRepository(db.connection,r.storage).attestations.verify_document(r.protocol.id).read_bytes()==r.protocols.verify_document(r.protocol.id).read_bytes()
    with sqlite3.connect(next((tmp_path/'migration-backups').glob('*.sqlite3'))) as db:
        assert db.execute('SELECT max(version) FROM schema_migrations').fetchone()==(10,)


def test_storage_move_preserves_completion(completions,tmp_path):
    r=completions; p=sign(r,register(r,draft(r))); fact=r.repo.completion(r.members[0].id); original=r.repo.verify_document(p.id).read_bytes()
    moved=relocate_storage(r.storage,r.db.connection,tmp_path/'moved')
    with Database(moved.database_path) as db:
        repo=CompletionRepository(db.connection,moved)
        assert repo.load(p.id)==p and repo.completion(r.members[0].id)==fact
        assert repo.verify_document(p.id).read_bytes()==original
        assert repo.entries(p.id)==r.repo.entries(p.id)
