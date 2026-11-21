"""Synthetic, local-only checks of enrollment and immutable archival evidence."""
from dataclasses import replace
from io import BytesIO
import hashlib
import json
import sqlite3
from uuid import uuid4

from docx import Document
import pytest

from legacy_schema import legacy_columns

from test_contracts import records, draft as contract_draft, register as register_contract
import proficentr.enrollment_order_repository as module
import proficentr.database as db_module
from proficentr.database import Database
from proficentr.enrollment_order_repository import EnrollmentOrderRepository
from proficentr.enrollment_orders import EnrollmentOrder, OrderError
from proficentr.program_repository import ProgramRepository
from proficentr.trainings import Training
from proficentr.storage import relocate_storage


@pytest.fixture
def orders(records):
    r = records
    r.contracts = r.repo
    r.repo = EnrollmentOrderRepository(r.db.connection, r.storage)
    for variant in ('dpo-individual','dpo-group'):
        r.repo.templates.register(r.source,'enrollment_order',variant,'Вымышленная форма приказа')
    return r


def draft(r, ids=None, **changes):
    return r.repo.save(replace(EnrollmentOrder(order_date='2020-02-10'), **changes), ids if ids is not None else [r.members[0].id])


def register(r, order):
    return r.repo.register(order.id, r.repo.prepare(order.id).signature)


def sign(r, order, **changes):
    args = dict(signed_date='2020-02-11',confirmed_by='Ответственный Примеров',grounds_checked=True,education_checked=True)
    args.update(changes)
    return r.repo.confirm_signing(order.id,**args)


@pytest.mark.parametrize('kind,count', [('individual',1),('group',1),('group',2)])
def test_register_separate_from_signing(orders,kind,count):
    r=orders
    order=draft(r,ids=[t.id for t in r.members[:count]],kind=kind,group_id=r.group.id if kind=='group' else None)
    preview,review=r.repo.preview(order.id)
    assert 'ПРОЕКТ ПРИКАЗА' in Document(preview).paragraphs[0].text
    assert r.repo.load(order.id).number is None
    assert r.db.connection.execute('SELECT * FROM enrollment_order_sequences').fetchall()==[]
    saved=r.repo.register(order.id,review.signature)
    original=r.repo.verify_document(saved.id).read_bytes()
    snapshot=json.loads(saved.snapshot)
    assert saved.number=='З-2020-0001' and saved.state=='registered'
    assert len(snapshot['context']['students'])==count
    assert len(snapshot['group_roster'])==(2 if kind=='group' else 0)
    assert snapshot['template']['id']==saved.template_id
    assert all(r.trainings.load(t.id).state=='preparation' for t in r.members)
    confirmed=sign(r,saved)
    assert confirmed.state=='signed' and confirmed.signed_date=='2020-02-11' and confirmed.confirmed_at
    assert confirmed.confirmed_by=='Ответственный Примеров' and confirmed.grounds_checked==confirmed.education_checked==1
    assert r.repo.verify_document(saved.id).read_bytes()==original
    assert confirmed.snapshot==saved.snapshot
    for i,t in enumerate(r.members): assert r.trainings.load(t.id).state==('enrolled' if i<count else 'preparation')
    assert [row[-1] for row in r.trainings.list_trainings(group_id=r.group.id)].count('enrolled')==count
    assert not r.db.connection.execute('PRAGMA foreign_key_check').fetchall()


def test_multiple_payers_package_and_warnings(orders):
    r=orders
    r.contracts.templates.register(r.source,'contract_person','dpo','Договор')
    saved=[]
    for member in r.members:
        c=r.contracts.save(contract_draft(replace_records(r),ids=[member.id],customer_type='person',student_id=None,person_data={'full_name':f'Плательщик {member.id}'}),[member.id])
        saved.append(register_contract(replace_records(r),c))
    order=register(r,draft(r,ids=[t.id for t in r.members],kind='group',group_id=r.group.id))
    before={c.id:r.contracts.verify_document(c.id).read_bytes() for c in saved}
    folder,warnings=r.repo.prepare_package(order.id)
    assert len(list(folder.glob('*.docx')))==3
    assert 'не учитывается' in warnings[0] and len(warnings)==1
    assert 'не является универсальным' in (folder/'Прочитайте.txt').read_text(encoding='utf-8')
    for c in saved: assert r.contracts.verify_document(c.id).read_bytes()==before[c.id]
    sign(r,order)
    assert all(r.contracts.load(c.id).state=='registered' for c in saved)


def replace_records(r):
    from types import SimpleNamespace
    return SimpleNamespace(**dict(vars(r),repo=r.contracts))


def test_missing_contract_is_warning_and_requires_manual_checks(orders):
    r=orders; order=register(r,draft(r))
    assert len(r.repo.warnings([r.members[0].id]))==2
    folder,warnings=r.repo.prepare_package(order.id)
    assert len(list(folder.glob('*.docx')))==1 and len(warnings)==2
    assert r.db.connection.execute('SELECT count(*) FROM contracts').fetchone()==(0,)
    sign(r,order)


@pytest.mark.parametrize('changes', [{'grounds_checked':False},{'education_checked':False},{'grounds_checked':1},{'confirmed_by':''},{'signed_date':'2019-01-01'},{'signed_date':'9999-01-01'},{'signed_date':'2020-02-30'}])
def test_sign_requires_explicit_checks_and_valid_dates(orders,changes):
    r=orders; order=register(r,draft(r))
    with pytest.raises(ValueError): sign(r,order,**changes)
    assert r.repo.load(order.id).state=='registered' and r.trainings.load(r.members[0].id).state=='preparation'
    assert r.db.connection.execute('SELECT * FROM training_enrollments').fetchall()==[]


def test_draft_cannot_be_signed_and_duplicate_enrollment(orders):
    r=orders; order=draft(r)
    with pytest.raises(OrderError): sign(r,order)
    first=register(r,order); second=register(r,draft(r))
    sign(r,first)
    with pytest.raises(OrderError): sign(r,first)
    with pytest.raises(OrderError): sign(r,second)
    with pytest.raises(OrderError): draft(r)


@pytest.mark.parametrize('field,value', [('end_date','2020-05-01'),('start_date','2020-03-02'),('program_snapshot','{}'),('group_id',None),('state','excluded'),('student_id',2)])
def test_sql_enrolled_training_locked(orders,field,value):
    r=orders; sign(r,register(r,draft(r)))
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute(f'UPDATE trainings SET {field} = ? WHERE id = ?', (value,r.members[0].id))
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute('DELETE FROM trainings WHERE id = ?', (r.members[0].id,))
    with pytest.raises(ValueError): r.trainings.save(replace(r.members[0],end_date='2020-06-01'))
    with pytest.raises(ValueError): r.trainings.exclude(r.members[0].id)


def test_group_program_locked_but_name_dates_do_not_rewrite_training(orders):
    r=orders; sign(r,register(r,draft(r,kind='group',group_id=r.group.id)))
    p=ProgramRepository(r.db.connection).save(replace(r.program,id=None,full_name='Другая'))
    with pytest.raises(ValueError): r.trainings.save_group(replace(r.group,program_id=p.id),change_program=True)
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute('UPDATE training_groups SET program_id = ? WHERE id = ?', (p.id,r.group.id))
    r.trainings.save_group(replace(r.group,name='Новое обозначение',end_date='2021-01-01'))
    assert r.trainings.load(r.members[0].id).end_date=='2020-04-01'


@pytest.mark.parametrize('statement', ["UPDATE enrollment_orders SET number='Другой' WHERE id=?", "UPDATE enrollment_orders SET snapshot='{}' WHERE id=?", "DELETE FROM enrollment_orders WHERE id=?", "DELETE FROM enrollment_order_trainings WHERE order_id=?", "UPDATE enrollment_order_trainings SET position=9 WHERE order_id=?"])
def test_registered_immutable_in_sql(orders,statement):
    r=orders; order=register(r,draft(r))
    with pytest.raises(sqlite3.IntegrityError),r.db.connection: r.db.connection.execute(statement,(order.id,))
    with pytest.raises(OrderError): r.repo.save(order,[r.members[0].id])


def test_cannot_forge_sql_signing_or_delete_assignment(orders):
    r=orders; order=register(r,draft(r))
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute("UPDATE enrollment_orders SET state='signed',signed_date='2020-02-11',confirmed_at=CURRENT_TIMESTAMP,confirmed_by='Тест',grounds_checked=1,education_checked=1 WHERE id=?",(order.id,))
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute('INSERT INTO training_enrollments(training_id,order_id) VALUES (?,?)',(r.members[0].id,order.id))
    sign(r,order)
    for statement in ('DELETE FROM training_enrollments','UPDATE training_enrollments SET order_id=2',"UPDATE enrollment_orders SET confirmed_by='Замена'"):
        with pytest.raises(sqlite3.IntegrityError),r.db.connection: r.db.connection.execute(statement)


@pytest.mark.parametrize('change', ['excluded','dates','age','snapshot','other_group','too_many','empty'])
def test_invalid_selection_and_live_data(orders,change):
    r=orders; ids=[t.id for t in r.members]; kwargs=dict(kind='group',group_id=r.group.id)
    if change=='excluded': r.trainings.exclude(ids[0])
    elif change=='dates':
        with r.db.connection: r.db.connection.execute("UPDATE trainings SET end_date='2020-99-99' WHERE id=?",(ids[0],))
    elif change=='age':
        with r.db.connection: r.db.connection.execute("UPDATE students SET birth_date='2010-01-01' WHERE id=?",(r.first.id,))
    elif change=='snapshot':
        snap=json.loads(r.members[0].program_snapshot); snap['edition']='Другая'
        with r.db.connection: r.db.connection.execute('UPDATE trainings SET program_snapshot=? WHERE id=?',(json.dumps(snap),ids[0]))
    elif change=='other_group':
        other=r.trainings.save(Training(student_id=r.first.id,program_id=r.program.id,start_date='2021-01-01',end_date='2021-02-01')); ids=[other.id]
    elif change=='too_many': kwargs={}
    elif change=='empty': ids=[]
    with pytest.raises(ValueError): register(r,draft(r,ids=ids,**kwargs))


def test_different_periods_preserved_in_docx(orders):
    r=orders
    r.trainings.save(replace(r.members[1],start_date='2020-03-10',end_date='2020-04-10'))
    order=register(r,draft(r,ids=[t.id for t in r.members],kind='group',group_id=r.group.id))
    context=json.loads(order.snapshot)['context']
    assert context['group']['start_date']==context['training']['start_date']==''
    rows=Document(r.repo.verify_document(order.id)).tables[0].rows
    assert len(rows)==2 and '2020-03-10' in rows[1].cells[1].text
    sign(r,order)


def test_mixed_dates_require_row_dates_in_template(orders):
    r=orders; r.trainings.save(replace(r.members[1],end_date='2020-04-10'))
    doc=Document(); doc.add_paragraph('{{ group.start_date }} — {{ group.end_date }}'); doc.save(r.source)
    r.repo.templates.register(r.source,'enrollment_order','dpo-group','Неподходящая форма')
    with pytest.raises(OrderError,match='Периоды различаются'): register(r,draft(r,ids=[t.id for t in r.members],kind='group',group_id=r.group.id))


def test_template_direction_kind_and_active_version(orders):
    r=orders
    for row in r.repo.templates.list_versions('enrollment_order'):
        r.db.connection.execute('UPDATE document_templates SET active=0 WHERE id=?',(row.id,))
    r.db.connection.commit()
    r.repo.templates.register(r.source,'enrollment_order','po-individual','ПО')
    r.repo.templates.register(r.source,'enrollment_order','dpo-group-other','Групповой')
    with pytest.raises(OrderError,match='шаблон'): register(r,draft(r))


def test_number_sequence_history_year_uniqueness_and_contract_independence(orders):
    r=orders
    historical=register(r,draft(r,historical_number='З-2020-0010'))
    assert historical.number=='З-2020-0010'
    assert register(r,draft(r)).number=='З-2020-0011'
    assert register(r,draft(r,order_date='2021-01-01')).number=='З-2021-0001'
    assert register(r,draft(r,historical_number='Архив / 123')).number=='Архив / 123'
    with pytest.raises(OrderError): register(r,draft(r,historical_number='архив   / 123'))
    assert r.db.connection.execute('SELECT * FROM contract_sequences').fetchall()==[]


@pytest.mark.parametrize('change',['training','student','center','template','roster','contract'])
def test_review_detects_changes(orders,change):
    r=orders; order=draft(r,kind='group',group_id=r.group.id); review=r.repo.prepare(order.id)
    if change=='training': r.trainings.save(replace(r.members[0],end_date='2020-04-10'))
    elif change=='student': r.students.save(replace(r.first,first_name='Пётр'))
    elif change=='center':
        with r.db.connection: r.db.connection.execute("UPDATE training_center SET full_name='Изменённый'")
    elif change=='template': r.repo.templates.register(r.source,'enrollment_order','dpo-group','Новая версия')
    elif change=='roster': r.trainings.exclude(r.members[1].id)
    else: r.contracts.save(contract_draft(replace_records(r)),[r.members[0].id])
    with pytest.raises(ValueError): r.repo.register(order.id,review.signature)
    assert r.repo.load(order.id).number is None


def test_changed_training_after_register_cannot_sign_but_snapshots_stable(orders):
    r=orders; order=register(r,draft(r)); data=r.repo.verify_document(order.id).read_bytes()
    r.trainings.save(replace(r.members[0],end_date='2020-04-10'))
    with pytest.raises(OrderError,match='изменились'): sign(r,order)
    assert r.repo.load(order.id).snapshot==order.snapshot and r.repo.verify_document(order.id).read_bytes()==data


def test_damaged_archive_refuses_copy_package_sign(orders):
    r=orders; order=register(r,draft(r)); path=r.repo.verify_document(order.id)
    path.write_bytes(b'changed')
    for action in (lambda:r.repo.document_copy(order.id),lambda:r.repo.prepare_package(order.id),lambda:sign(r,order)):
        with pytest.raises(ValueError): action()
    assert r.trainings.load(r.members[0].id).state=='preparation'


def test_failed_db_commit_quarantines_file_and_rolls_back_number(orders):
    r=orders; order=draft(r); review=r.repo.prepare(order.id)
    with r.db.connection:
        r.db.connection.execute("CREATE TRIGGER refuse_order BEFORE UPDATE ON enrollment_orders WHEN NEW.state='registered' BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(OrderError): r.repo.register(order.id,review.signature)
    assert r.repo.load(order.id).state=='draft'
    assert r.db.connection.execute('SELECT * FROM enrollment_order_sequences').fetchall()==[]
    assert list((r.storage.root/'exports'/'unregistered-orders').glob('*/*.docx'))
    assert not list((r.storage.root/'documents').glob('*/*/orders/*.docx'))


def test_file_failure_preserves_draft_and_sequence(orders,monkeypatch):
    r=orders; order=draft(r); review=r.repo.prepare(order.id)
    def fail(*args): raise OSError('synthetic unavailable storage')
    monkeypatch.setattr(module,'generate_document',fail)
    with pytest.raises(OrderError): r.repo.register(order.id,review.signature)
    assert r.repo.load(order.id).number is None
    assert r.db.connection.execute('SELECT * FROM enrollment_order_sequences').fetchall()==[]


def test_orphans_recovered_without_adoption_or_replacing_original(orders):
    r=orders; order=register(r,draft(r)); original=r.repo.verify_document(order.id)
    orphan=original.parent/('order-'+uuid4().hex+'.docx'); orphan.write_bytes(original.read_bytes())
    assert r.repo.recover_unregistered()==1
    assert not orphan.exists() and r.repo.verify_document(order.id)==original
    assert len(r.repo.search())==1


def test_search_snapshot_and_export_copy(orders):
    r=orders; order=register(r,draft(r,kind='group',group_id=r.group.id))
    copy=r.repo.document_copy(order.id); copy.write_bytes(b'edited independently')
    r.students.save(replace(r.first,surname='Изменённый'))
    for query in ('З-2020','2020-02-10','ТЕСТ-01','Тестов'):
        assert r.repo.search(query)[0][0]==order.id
    assert hashlib.sha256(r.repo.verify_document(order.id).read_bytes()).hexdigest()==order.document_sha256


def test_migration9_preserves_version8_and_backup(orders,tmp_path,monkeypatch):
    r=orders; path=tmp_path/'old.sqlite3'
    r.contracts.templates.register(r.source,'contract_person','dpo','Историческая форма')
    contract=register_contract(replace_records(r),contract_draft(replace_records(r)))
    original=r.contracts.verify_document(contract.id).read_bytes()
    tables=('training_center','students','counterparties','programs','training_groups','trainings','document_templates','contracts','contract_trainings','contract_sequences')
    columns = {name: legacy_columns(r.db.connection, name) for name in tables}
    before={name:r.db.connection.execute(f'SELECT {columns[name]} FROM {name}').fetchall() for name in tables}
    all_migrations=db_module.MIGRATIONS
    monkeypatch.setattr(db_module,'MIGRATIONS',all_migrations[:8])
    with Database(path) as old:
        # Restore a complete v8 fixture, including immutable registered links.
        link_triggers=old.connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'contract_links_%'").fetchall()
        for name,sql in link_triggers: old.connection.execute(f'DROP TRIGGER {name}')
        with old.connection:
            for name in tables:
                for row in before[name]:
                    old.connection.execute(f"INSERT INTO {name} VALUES ({','.join('?' for _ in row)})",row)
            for name,sql in link_triggers: old.connection.execute(sql)
    monkeypatch.setattr(db_module,'MIGRATIONS',all_migrations)
    with Database(path) as upgraded:
        assert upgraded.connection.execute('SELECT max(version) FROM schema_migrations').fetchone()==(len(all_migrations),)
        for name in tables: assert upgraded.connection.execute(f'SELECT {columns[name]} FROM {name}').fetchall()==before[name]
        assert EnrollmentOrderRepository(upgraded.connection,r.storage).contracts.verify_document(contract.id).read_bytes()==original
        assert upgraded.connection.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
    backups=list((tmp_path/'migration-backups').glob('*.sqlite3')); assert backups
    with sqlite3.connect(backups[0]) as backup: assert backup.execute('SELECT max(version) FROM schema_migrations').fetchone()==(8,)


def test_po_uses_snapshot_direction_and_po_template(orders):
    r=orders
    program=ProgramRepository(r.db.connection).save(replace(r.program,id=None,full_name='Вымышленная ПО',direction='po',kind='po_retraining'))
    training=r.trainings.save(Training(student_id=r.first.id,program_id=program.id,start_date='2022-01-01',end_date='2022-02-01'))
    r.repo.templates.register(r.source,'enrollment_order','po-individual','Форма ПО')
    order=register(r,draft(r,ids=[training.id]))
    assert json.loads(order.snapshot)['context']['program']['direction']=='po'
    sign(r,order)
    assert r.trainings.load(training.id).state=='enrolled'
    assert r.trainings.load(r.members[0].id).state=='preparation'


def test_package_refuses_damaged_contract_and_omits_annulled(orders):
    r=orders; r.contracts.templates.register(r.source,'contract_person','dpo','Договор')
    c=register_contract(replace_records(r),contract_draft(replace_records(r)))
    order=register(r,draft(r)); path=r.contracts.verify_document(c.id); data=path.read_bytes()
    path.write_bytes(b'damaged')
    with pytest.raises(ValueError): r.repo.prepare_package(order.id)
    path.write_bytes(data); r.contracts.annul(c.id,'Вымышленное основание')
    folder,warnings=r.repo.prepare_package(order.id)
    assert len(list(folder.glob('*.docx')))==1 and len(warnings)==2


def test_second_student_failure_rolls_back_entire_signing(orders):
    r=orders; order=register(r,draft(r,ids=[t.id for t in r.members],kind='group',group_id=r.group.id))
    with r.db.connection:
        r.db.connection.execute(f"CREATE TRIGGER fail_second BEFORE INSERT ON training_enrollments WHEN NEW.training_id={r.members[1].id} BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(OrderError): sign(r,order)
    assert r.db.connection.execute('SELECT * FROM training_enrollments').fetchall()==[]
    assert r.repo.load(order.id).state=='registered'


def test_storage_move_preserves_order_file_and_enrollment(orders,tmp_path):
    r=orders; order=sign(r,register(r,draft(r))); data=r.repo.verify_document(order.id).read_bytes()
    moved=relocate_storage(r.storage,r.db.connection,tmp_path/'moved')
    with Database(moved.database_path) as db:
        repo=EnrollmentOrderRepository(db.connection,moved)
        assert repo.load(order.id)==order and repo.verify_document(order.id).read_bytes()==data
        from proficentr.training_repository import TrainingRepository
        assert TrainingRepository(db.connection).load(r.members[0].id).state=='enrolled'
