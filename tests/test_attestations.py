"""Вымышленные результаты, неизменяемый архив и несколько попыток."""
from dataclasses import replace
import hashlib
import json
import sqlite3
from uuid import uuid4

from docx import Document
import pytest

from test_contracts import records
from test_enrollment_orders import orders, draft as order_draft, register as order_register, sign as order_sign
from proficentr.attestations import AttestationProtocol, AttestationResult, AttestationError
from proficentr.attestation_repository import AttestationRepository
from proficentr.database import Database
from proficentr.program_repository import ProgramRepository
from proficentr.students import Student
from proficentr.trainings import Training
from proficentr.storage import relocate_storage
import proficentr.database as db_module
import proficentr.attestation_repository as module


@pytest.fixture
def attestations(orders):
    r = orders
    r.orders = r.repo
    order_sign(r, order_register(r, order_draft(r, ids=[t.id for t in r.members],kind='group',group_id=r.group.id)))
    doc = Document(); doc.add_paragraph('{{ protocol.number }} {{ protocol.date }} {{ protocol.attestation_date }} {{ protocol.kind }}')
    doc.add_paragraph('{{ protocol.chair }} / {{ protocol.members }} / {{ program.name }}')
    table = doc.add_table(rows=3,cols=3)
    table.cell(0,0).text = '{%tr for row in students %}'
    table.cell(1,0).text = '{{ row.full_name }} {{ row.start_date }} {{ row.end_date }}'
    table.cell(1,1).text = '{{ row.attestation_result }} {{ row.grade }}'
    table.cell(1,2).text = '{{ row.qualification }} {{ row.qualification_grade }} {{ row.attestation_note }}'
    table.cell(2,0).text = '{%tr endfor %}'
    doc.save(r.source)
    r.repo = AttestationRepository(r.db.connection,r.storage)
    r.repo.templates.register(r.source,'protocol_dpo','standard','Вымышленный протокол ДПО')
    r.repo.templates.register(r.source,'protocol_po','standard','Вымышленный протокол ПО')
    return r


def draft(r, *, ids=None, results=None, **changes):
    results = results if results is not None else [AttestationResult(key,'passed') for key in (ids if ids is not None else [r.members[0].id])]
    return r.repo.save(replace(AttestationProtocol(protocol_date='2020-04-02',attestation_date='2020-04-01'),**changes),results)


def register(r, protocol):
    return r.repo.register(protocol.id,r.repo.prepare(protocol.id).signature)


def sign(r, protocol, **changes):
    args = dict(signed_date='2025-01-01',confirmed_by='Ответственный Примеров'); args.update(changes)
    return r.repo.confirm_signing(protocol.id,**args)


@pytest.mark.parametrize('count,group',[(1,False),(1,True),(2,True)])
def test_dpo_individual_group_subset_preview_register_sign(attestations,count,group):
    r=attestations
    p=draft(r,ids=[t.id for t in r.members[:count]],group_id=r.group.id if group else None,chair='Председатель Тестов',members='Члены комиссии')
    path,review=r.repo.preview(p.id)
    assert 'ПРОЕКТ ПРОТОКОЛА' in Document(path).paragraphs[0].text
    assert r.repo.load(p.id).number is None
    assert not r.db.connection.execute('SELECT * FROM attestation_sequences').fetchall()
    p=r.repo.register(p.id,review.signature)
    assert p.number=='П-2020-0001' and p.state=='registered'
    assert p.created_at and p.registered_at and not p.confirmed_at
    assert r.repo.current_confirmed_result(r.members[0].id) is None
    doc=Document(r.repo.verify_document(p.id))
    assert len(doc.tables[0].rows)==count
    assert 'Успешно прошёл' in doc.tables[0].cell(0,1).text
    snapshot=json.loads(p.snapshot)
    assert snapshot['context']['protocol']['chair']=='Председатель Тестов'
    assert snapshot['template']['id']==p.template_id
    before=r.repo.verify_document(p.id).read_bytes()
    signed=sign(r,p)
    assert signed.state=='signed' and signed.confirmed_at and signed.confirmed_by
    assert signed.snapshot==p.snapshot and r.repo.verify_document(p.id).read_bytes()==before
    assert r.repo.current_confirmed_result(r.members[0].id)[4]=='passed'
    assert not r.db.connection.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('result',['passed','failed','absent'])
def test_all_results_optional_grade_no_automatic_qualification(attestations,result):
    r=attestations
    p=sign(r,register(r,draft(r,results=[AttestationResult(r.members[0].id,result)])))
    row=r.repo.results(p.id)[0]
    assert row.result==result and row.grade==row.qualification==row.qualification_grade==''
    assert r.repo.current_confirmed_result(row.training_id)[4]==result


def test_po_qualification_and_individual_training(attestations):
    r=attestations
    program=ProgramRepository(r.db.connection).save(replace(r.program,id=None,direction='po',kind='po_retraining',full_name='Вымышленная профессия'))
    t=r.trainings.save(Training(student_id=r.first.id,program_id=program.id,start_date='2021-01-01',end_date='2021-02-01'))
    old=r.repo; r.repo=r.orders
    source=r.orders.templates.source(r.orders.templates.list_versions('enrollment_order')[0].id).path
    r.orders.templates.register(source,'enrollment_order','po-individual','Вымышленный приказ ПО')
    order_sign(r,order_register(r,order_draft(r,ids=[t.id])))
    r.repo=old
    p=sign(r,register(r,draft(r,direction='po',results=[AttestationResult(t.id,'passed','зачёт','Вымышленная профессия','3 разряд','Пример')],protocol_date='2021-02-01',attestation_date='2021-02-01')))
    assert p.kind=='Квалификационный экзамен'
    assert json.loads(p.snapshot)['template']['document_type']=='protocol_po'
    assert '3 разряд' in Document(r.repo.verify_document(p.id)).tables[0].cell(0,2).text
    assert r.repo.results(p.id)[0].qualification=='Вымышленная профессия'


@pytest.mark.parametrize('mode',['not_enrolled','excluded','wrong_direction','wrong_group','too_many_individual','duplicate','mixed_edition','mixed_program'])
def test_invalid_rosters(attestations,mode):
    r=attestations; ids=[r.members[0].id]; changes={}
    if mode in ('not_enrolled','excluded'):
        t=r.trainings.save(Training(student_id=r.first.id,program_id=r.program.id,start_date='2022-01-01',end_date='2022-02-01'))
        if mode=='excluded':
            group=r.trainings.save_group(replace(r.group,id=None,name='Исключённая группа',start_date='2023-01-01',end_date='2023-02-01'))
            t=r.trainings.add_students(group.id,[r.first.id])[0]
            r.trainings.exclude(t.id)
        ids=[t.id]
    elif mode=='wrong_direction': changes['direction']='po'
    elif mode=='wrong_group': changes['group_id']=999
    elif mode=='too_many_individual': ids=[t.id for t in r.members]
    elif mode=='duplicate': ids*=2
    else:
        if mode=='mixed_edition': program=ProgramRepository(r.db.connection).save(replace(r.program,edition='2'))
        else: program=ProgramRepository(r.db.connection).save(replace(r.program,id=None,full_name='Другая программа'))
        if mode=='mixed_edition':
            student=r.students.save(Student(surname='Третий',first_name='Пример',birth_date='1990-01-01'))
            t=r.trainings.add_students(r.group.id,[student.id])[0]
        else:
            t=r.trainings.save(Training(student_id=r.second.id,program_id=program.id,start_date='2022-01-01',end_date='2022-02-01'))
        old=r.repo; r.repo=r.orders
        order_sign(r,order_register(r,order_draft(r,ids=[t.id])))
        r.repo=old
        # Revision compatibility is checked against the immutable training snapshots.
        p=AttestationProtocol(direction='dpo',group_id=r.group.id)
        with pytest.raises(AttestationError,match='редакция' if mode=='mixed_edition' else 'группы'):
            r.repo._trainings(p,[AttestationResult(ids[0]),AttestationResult(t.id)])
        return
    with pytest.raises(ValueError): draft(r,ids=ids,**changes)


def test_blank_draft_and_registered_results_cannot_be_signed_or_edited(attestations):
    r=attestations; p=draft(r,results=[AttestationResult(r.members[0].id)])
    with pytest.raises(AttestationError): sign(r,p)
    p=register(r,p)
    with pytest.raises(AttestationError,match='результат'): sign(r,p)
    with pytest.raises(AttestationError): r.repo.save(p,r.repo.results(p.id))
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute("UPDATE attestation_protocols SET state='signed',signed_date='2025-01-01',confirmed_at=CURRENT_TIMESTAMP,confirmed_by='Тест',revision=revision+1 WHERE id=?",(p.id,))
    assert r.repo.current_confirmed_result(r.members[0].id) is None


@pytest.mark.parametrize('state',['registered','signed'])
@pytest.mark.parametrize('sql',["UPDATE attestation_results SET result='failed' WHERE protocol_id=?", "DELETE FROM attestation_results WHERE protocol_id=?", "UPDATE attestation_results SET protocol_id=999 WHERE protocol_id=?", "INSERT INTO attestation_results(protocol_id,training_id,position) VALUES (?,999,9)", "UPDATE attestation_protocols SET snapshot='{}' WHERE id=?", "UPDATE attestation_protocols SET number='OTHER' WHERE id=?", "DELETE FROM attestation_protocols WHERE id=?", "UPDATE attestation_protocols SET attestation_date='2019-01-01' WHERE id=?"])
def test_sql_protection(attestations,state,sql):
    r=attestations; p=register(r,draft(r))
    if state=='signed': p=sign(r,p)
    with pytest.raises(sqlite3.IntegrityError),r.db.connection: r.db.connection.execute(sql,(p.id,))


def test_repeated_attempts_and_stable_latest_confirmed(attestations):
    r=attestations; key=r.members[0].id
    first=sign(r,register(r,draft(r,results=[AttestationResult(key,'failed')])) )
    newer=sign(r,register(r,draft(r,attestation_date='2021-01-01',protocol_date='2021-01-02')))
    sign(r,register(r,draft(r,attestation_date='2019-01-01',protocol_date='2019-01-02')))
    assert r.repo.current_confirmed_result(key)[0]==newer.id
    tied=sign(r,register(r,draft(r,attestation_date='2021-01-01',protocol_date='2021-01-02',results=[AttestationResult(key,'absent')])))
    register(r,draft(r,attestation_date='2022-01-01',protocol_date='2022-01-02'))
    assert r.repo.current_confirmed_result(key)[0]==tied.id
    assert r.repo.results(first.id)[0].result=='failed' and len(r.repo.history(key))==5
    assert r.repo.load(first.id).snapshot==first.snapshot


def test_numbers_history_years_uniqueness_no_reuse(attestations):
    r=attestations
    p=register(r,draft(r,historical_number='П-2020-0010'))
    assert register(r,draft(r)).number=='П-2020-0011'
    register(r,draft(r,historical_number='П-2020-0003'))
    assert register(r,draft(r)).number=='П-2020-0012'
    assert register(r,draft(r,protocol_date='2021-01-01')).number=='П-2021-0001'
    register(r,draft(r,historical_number='Архив / 12'))
    with pytest.raises(AttestationError): register(r,draft(r,historical_number='архив   / 12'))
    for sql in ('DELETE FROM attestation_sequences','UPDATE attestation_sequences SET last_value=1','UPDATE attestation_sequences SET year=1990','INSERT OR REPLACE INTO attestation_sequences(year,last_value) VALUES (2020,1)'):
        with pytest.raises(sqlite3.IntegrityError),r.db.connection: r.db.connection.execute(sql)
    with pytest.raises(sqlite3.IntegrityError),r.db.connection: r.db.connection.execute('DELETE FROM attestation_protocols WHERE id=?',(p.id,))
    assert not r.db.connection.execute('SELECT * FROM contract_sequences').fetchall()


@pytest.mark.parametrize('change',['student','program','center','template','results'])
def test_review_changes_and_immutable_snapshot(attestations,change):
    r=attestations; p=draft(r); review=r.repo.prepare(p.id)
    if change=='student': r.students.save(replace(r.first,surname='Изменённый'))
    elif change=='program': ProgramRepository(r.db.connection).save(replace(r.program,full_name='Изменённая программа'))
    elif change=='center':
        with r.db.connection: r.db.connection.execute("UPDATE training_center SET full_name='Изменённый центр'")
    elif change=='template': r.repo.templates.register(r.source,'protocol_dpo','standard','Новая версия')
    else: r.repo.save(p,[AttestationResult(r.members[0].id,'failed')])
    if change=='program':
        # Training snapshot is authoritative and unchanged by an edit of the current program card.
        p=r.repo.register(p.id,review.signature)
    else:
        with pytest.raises(AttestationError): r.repo.register(p.id,review.signature)
        p=register(r,r.repo.load(p.id))
    before=r.repo.verify_document(p.id).read_bytes(); snapshot=p.snapshot
    r.students.save(replace(r.first,surname='После регистрации'))
    p=sign(r,p)
    assert p.snapshot==snapshot and r.repo.verify_document(p.id).read_bytes()==before


def test_template_direction_and_missing_active(attestations):
    r=attestations; p=draft(r)
    po=next(t for t in r.repo.templates.list_versions('protocol_po') if t.active)
    p=r.repo.save(replace(p,template_id=po.id),r.repo.results(p.id))
    with pytest.raises(AttestationError,match='Настройки'): register(r,p)
    with r.db.connection: r.db.connection.execute("UPDATE document_templates SET active=0 WHERE document_type='protocol_dpo'")
    p=r.repo.save(replace(p,template_id=None),r.repo.results(p.id))
    with pytest.raises(AttestationError,match='protocol_dpo'): register(r,p)


@pytest.mark.parametrize('failure',['file','db','commit'])
def test_registration_failure_rolls_back(attestations,monkeypatch,failure):
    r=attestations; p=draft(r); review=r.repo.prepare(p.id)
    if failure=='file':
        def fail(*a): raise OSError('synthetic')
        monkeypatch.setattr(module,'generate_document',fail)
    elif failure=='db':
        with r.db.connection: r.db.connection.execute("CREATE TRIGGER fail_protocol BEFORE UPDATE ON attestation_protocols WHEN NEW.state='registered' BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    else:
        # Deferred FK fails at COMMIT, after file publication and protocol UPDATE.
        with r.db.connection:
            r.db.connection.execute('CREATE TABLE deferred_test(id INTEGER REFERENCES students(id) DEFERRABLE INITIALLY DEFERRED)')
            r.db.connection.execute("CREATE TRIGGER fail_commit AFTER UPDATE ON attestation_protocols WHEN NEW.state='registered' BEGIN INSERT INTO deferred_test VALUES (999999); END")
    with pytest.raises(AttestationError): r.repo.register(p.id,review.signature)
    assert r.repo.load(p.id).state=='draft'
    assert not r.db.connection.execute('SELECT * FROM attestation_sequences').fetchall()
    assert not list((r.storage.root/'documents').glob('*/*/protocols/*.docx'))
    if failure!='file': assert list((r.storage.root/'exports'/'unregistered-protocols').glob('*/*.docx'))


def test_archive_copy_hash_orphan_recovery_and_search(attestations):
    r=attestations; p=register(r,draft(r)); original=r.repo.verify_document(p.id)
    copy=r.repo.document_copy(p.id); copy.write_bytes(b'edited copy')
    assert hashlib.sha256(original.read_bytes()).hexdigest()==p.document_sha256
    orphan=original.parent/('protocol-'+uuid4().hex+'.docx'); orphan.write_bytes(original.read_bytes())
    assert r.repo.recover_unregistered()==1 and not orphan.exists()
    assert r.repo.search('Тестов')[0][0]==p.id and r.repo.search('2020-04-01')
    r.students.save(replace(r.first,surname='Другая фамилия'))
    assert r.repo.search('Тестов')
    original.write_bytes(b'corrupted')
    with pytest.raises(AttestationError,match='SHA-256'): r.repo.document_copy(p.id)
    with pytest.raises(AttestationError): sign(r,p)


@pytest.mark.parametrize('changes',[{'confirmed_by':''},{'signed_date':'2019-01-01'},{'signed_date':'9999-01-01'},{'signed_date':'2020-02-30'}])
def test_sign_validation(attestations,changes):
    r=attestations; p=register(r,draft(r))
    with pytest.raises(ValueError): sign(r,p,**changes)
    assert r.repo.load(p.id).state=='registered'


def test_stale_draft_and_empty_protocol(attestations):
    r=attestations; p=draft(r)
    r.repo.save(p,r.repo.results(p.id))
    with pytest.raises(AttestationError): r.repo.save(p,r.repo.results(p.id))
    p=draft(r,ids=[])
    with pytest.raises(AttestationError): register(r,p)


def test_migration10_preserves_existing_v9_and_backup(attestations,tmp_path,monkeypatch):
    r=attestations; path=tmp_path/'v9.sqlite3'
    all_migrations=db_module.MIGRATIONS
    monkeypatch.setattr(db_module,'MIGRATIONS',all_migrations[:9])
    with Database(path) as old:
        # An actual version 9 database with a signed order and its protected enrollment.
        r.db.connection.backup(old.connection)
        with old.connection:
            for name, in old.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'attestation%'").fetchall(): old.connection.execute(f'DROP TRIGGER {name}')
            for table in ('attestation_results','attestation_protocols','attestation_sequences'): old.connection.execute(f'DROP TABLE {table}')
            old.connection.execute('DELETE FROM schema_migrations WHERE version=10')
        tables=[row[0] for row in old.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        before={table:old.connection.execute(f'SELECT * FROM {table}').fetchall() for table in tables}
    monkeypatch.setattr(db_module,'MIGRATIONS',all_migrations)
    with Database(path) as upgraded:
        assert upgraded.connection.execute('SELECT max(version) FROM schema_migrations').fetchone()==(10,)
        for table in tables:
            if table!='schema_migrations': assert upgraded.connection.execute(f'SELECT * FROM {table}').fetchall()==before[table]
        assert upgraded.connection.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
        assert not upgraded.connection.execute('PRAGMA foreign_key_check').fetchall()
    backups=list((tmp_path/'migration-backups').glob('*.sqlite3')); assert backups
    with sqlite3.connect(backups[0]) as db: assert db.execute('SELECT max(version) FROM schema_migrations').fetchone()==(9,)


def test_storage_move_preserves_attempts(attestations,tmp_path):
    r=attestations; p=sign(r,register(r,draft(r))); original=r.repo.verify_document(p.id).read_bytes()
    moved=relocate_storage(r.storage,r.db.connection,tmp_path/'moved')
    with Database(moved.database_path) as db:
        repo=AttestationRepository(db.connection,moved)
        assert repo.load(p.id)==p and repo.verify_document(p.id).read_bytes()==original
        assert repo.current_confirmed_result(r.members[0].id)==r.repo.current_confirmed_result(r.members[0].id)


def test_simultaneous_registrations_use_distinct_numbers(attestations):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    r=attestations
    drafts=[draft(r),draft(r)]
    barrier=Barrier(2)
    def run(key):
        with Database(r.storage.database_path) as db:
            repo=AttestationRepository(db.connection,r.storage)
            review=repo.prepare(key)
            barrier.wait(timeout=10)
            return repo.register(key,review.signature).number
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(run,[p.id for p in drafts]))=={'П-2020-0001','П-2020-0002'}


@pytest.mark.parametrize('state',['registered','signed'])
def test_sql_replace_cannot_bypass_protocol_protection(attestations,state):
    r=attestations; p=register(r,draft(r))
    if state=='signed': p=sign(r,p)
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute("INSERT OR REPLACE INTO attestation_protocols(id,protocol_date,attestation_date,direction,kind) VALUES (?,'2020-04-01','2020-04-01','dpo','Итоговая аттестация')",(p.id,))
    assert r.repo.load(p.id)==p
