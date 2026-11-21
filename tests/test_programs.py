from dataclasses import replace
import sqlite3

import pytest

from legacy_schema import use_legacy_card_fields

from proficentr import database as module
from proficentr.programs import DIRECTIONS, KINDS, Program, hours_from_text
from proficentr.program_repository import ProgramRepository, DuplicateProgramError, ProgramRepositoryError
from proficentr.database import Database
from proficentr.counterparties import Counterparty
from proficentr.counterparty_repository import CounterpartyRepository
from proficentr.students import Student
from proficentr.student_repository import StudentRepository


def sample(**changes):
    return replace(Program(full_name='Учебная программа — пример', direction='dpo', kind='dpo_upskilling', hours=72), **changes)


@pytest.fixture
def repo(tmp_path):
    with Database(tmp_path/'test.sqlite3') as db:
        yield ProgramRepository(db.connection)


@pytest.mark.parametrize('direction,kind', [(d,k) for d,kinds in KINDS.items() for k in kinds])
def test_create_each_structured_program_type(repo,direction,kind):
    saved=repo.save(sample(direction=direction,kind=kind))
    assert repo.load(saved.id)==saved
    assert saved.active and not saved.qualification and not saved.grade


def test_persist_all_fields_reload_and_edit(tmp_path):
    path=tmp_path/'test.sqlite3'
    original=sample(full_name='  Учебная   программа ',short_name='Пример',code='КОД-1',
                    qualification='Пример квалификации',grade='Тестовая категория',approval_date='2024-02-29',
                    approval_number='Уч-1',approval_details='Тестовое утверждение',edition='2026.1',note='  Строка  1\n Строка 2 ')
    with Database(path) as db:
        repo=ProgramRepository(db.connection)
        saved=repo.save(original)
        changed=repo.save(replace(saved,hours=1000000))  # No arbitrary maximum training duration.
        assert changed.id==saved.id
        assert len(repo.search())==1
        assert original.id is None
    with Database(path) as db:
        assert ProgramRepository(db.connection).load(changed.id)==changed
        assert changed.full_name=='Учебная программа' and changed.note=='Строка 1\nСтрока 2'


@pytest.mark.parametrize('changes', [
    {'full_name':' \t '},{'direction':''},{'direction':'other'},{'kind':''},
    {'kind':'po_retraining'},{'direction':'po','kind':'dpo_retraining'},
    {'hours':0},{'hours':-1},{'hours':1.5},{'hours':'72'},{'hours':True},{'active':'false'},
    {'approval_date':'2025-02-29'},{'approval_date':'2024-2-29'},{'approval_date':'20240229'},
])
def test_invalid_fields_do_not_write(repo,changes):
    with pytest.raises(ValueError):repo.save(sample(**changes))
    assert repo.search(active=None)==[]


@pytest.mark.parametrize('text', ['', '0', '-1', '1.5', '12 ч', '1e3', '١٢'])
def test_hours_input_rejects_non_positive_integers(text):
    with pytest.raises(ValueError):hours_from_text(text)


def test_hours_input_and_sqlite_integer_capacity(repo):
    assert hours_from_text(' 0072 ')==72
    assert repo.save(sample(hours=2**63-1)).hours==2**63-1
    with pytest.raises(ProgramRepositoryError,match='часов'):
        repo.save(sample(full_name='Слишком большое значение',hours=2**63))
    assert len(repo.search())==1 and not repo.connection.in_transaction


def test_search_and_combined_filters(repo):
    dpo=repo.save(sample(full_name='Управление Ёлкой',short_name='Пример краткого',code='Уч-01'))
    po=repo.save(sample(full_name='Подготовка — пример',direction='po',kind='po_initial_training',code='По-02'))
    archived=repo.save(sample(full_name='Архивный пример',code='АР-3',active=False))
    for query in ['ёлк','УПРАВЛЕНИЕ   ёлк','КРАТКОГО','уч-01']:
        assert repo.search(query)==[dpo]
    assert repo.search(direction='dpo')==[dpo]
    assert repo.search(direction='po')==[po]
    assert repo.search('подготов',direction='dpo')==[]
    assert repo.search(active=False)==[archived]
    assert len(repo.search(active=None))==3
    assert repo.search('ар-3',active=None)==[archived]
    assert repo.search("' OR 1=1 --",active=None)==[]
    assert repo.search('%',active=None)==[]
    for args in [dict(direction='bad'),dict(active='False')]:
        with pytest.raises(ValueError):repo.search(**args)


@pytest.mark.parametrize('changes', [dict(full_name='  УЧЕБНАЯ   программа — пример '),dict(full_name='Другое название',code=' код-1 ')])
def test_duplicate_warning_includes_archived_and_allows_distinct_editions(repo,changes):
    original=repo.save(sample(code='КОД-1',edition='1',active=False))
    candidate=sample(**changes,edition='2',hours=108)
    with pytest.raises(DuplicateProgramError) as error:repo.save(candidate)
    assert error.value.matches==[original]
    assert not repo.connection.in_transaction
    separate=repo.save(candidate,allow_duplicates=True)
    assert separate.id!=original.id and separate.edition=='2'
    assert repo.load(original.id)==original
    assert len(repo.search(active=None))==2


def test_archive_restore_preserves_id_and_latest_details(repo):
    original=repo.save(sample())
    edited=repo.save(replace(original,edition='Новая редакция'))
    archived=repo.set_active(original.id,False)
    assert archived==replace(edited,active=False)
    assert repo.search()==[] and repo.load(original.id)==archived
    assert repo.search(active=False)==[archived]
    restored=repo.set_active(original.id,True)
    assert restored==edited and repo.search()==[edited]
    assert repo.connection.execute('SELECT count(*) FROM programs').fetchone()==(1,)


def test_missing_record_outer_transaction_and_status_validation(repo):
    for operation in [lambda:repo.load(999),lambda:repo.save(sample(id=999)),lambda:repo.set_active(999,False)]:
        with pytest.raises(ProgramRepositoryError):operation()
    with pytest.raises(ValueError):repo.set_active(999,'false')
    repo.connection.execute('BEGIN')
    for operation in [lambda:repo.save(sample()),lambda:repo.set_active(999,False)]:
        with pytest.raises(ProgramRepositoryError):operation()
    assert repo.connection.in_transaction
    repo.connection.rollback()
    assert repo.search(active=None)==[]


def test_failed_write_and_archive_are_atomic_and_diagnostics_are_neutral(repo,caplog,capsys):
    saved=repo.save(sample())
    repo.connection.execute("CREATE TRIGGER fail_program BEFORE UPDATE ON programs BEGIN SELECT RAISE(ABORT, 'private diagnostic'); END")
    for operation in [lambda:repo.save(replace(saved,note='Тестовые закрытые сведения')),lambda:repo.set_active(saved.id,False)]:
        with pytest.raises(ProgramRepositoryError) as error:operation()
        assert 'private diagnostic' not in str(error.value)
        assert repo.load(saved.id)==saved and not repo.connection.in_transaction
    assert saved.full_name not in repr(saved)
    output=capsys.readouterr()
    assert not caplog.text and not output.out and not output.err


@pytest.mark.parametrize('direction,kind,hours', [('dpo','po_retraining',72),('po','dpo_retraining',72),('dpo','dpo_upskilling',0),('po','po_upskilling',1.5)])
def test_database_rejects_incompatible_types_and_non_integer_hours(repo,direction,kind,hours):
    with pytest.raises(sqlite3.IntegrityError):
        with repo.connection:
            repo.connection.execute('INSERT INTO programs(full_name,direction,kind,hours) VALUES (?,?,?,?)',('Тест',direction,kind,hours))
    assert repo.search()==[]


def test_migration_five_preserves_existing_registers_and_backup(tmp_path,monkeypatch):
    path=tmp_path/'old.sqlite3'
    migrations=module.MIGRATIONS
    with monkeypatch.context() as old:
        old.setattr(module,'MIGRATIONS',migrations[:4])
        use_legacy_card_fields(old)
        with Database(path) as db:
            student=StudentRepository(db.connection).save(Student(surname='Тестовый',first_name='Пример',birth_date='1990-01-01'))
            customer=CounterpartyRepository(db.connection).save(Counterparty(full_name='Тестовый заказчик'))
            db.connection.execute("INSERT INTO training_center(id,full_name) VALUES (1,'Тестовый центр')")
            db.connection.commit()
            stamps=db.connection.execute('SELECT * FROM schema_migrations').fetchall()
    for _ in range(2):
        with Database(path,create=False) as db:
            assert StudentRepository(db.connection).load(student.id)==student
            assert CounterpartyRepository(db.connection).load(customer.id)==customer
            assert db.connection.execute('SELECT full_name FROM training_center').fetchone()==('Тестовый центр',)
            assert db.connection.execute('SELECT * FROM schema_migrations WHERE version<=4').fetchall()==stamps
            assert db.connection.execute('SELECT max(version) FROM schema_migrations').fetchone()==(len(migrations),)
            repo=ProgramRepository(db.connection)
            if not repo.search():saved=repo.save(sample())
            assert repo.load(saved.id)==saved
    backups=list((tmp_path/'migration-backups').glob('*.sqlite3'))
    assert len(backups)==1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
        assert backup.execute('SELECT max(version) FROM schema_migrations').fetchone()==(4,)
        assert backup.execute('SELECT full_name FROM counterparties').fetchall()==[('Тестовый заказчик',)]
