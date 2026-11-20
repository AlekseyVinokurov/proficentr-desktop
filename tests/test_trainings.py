from dataclasses import replace
from datetime import date
import json
import sqlite3
from types import SimpleNamespace

import pytest

import proficentr.database as database_module
from proficentr.database import Database
from proficentr.programs import Program
from proficentr.program_repository import ProgramRepository
from proficentr.student_repository import StudentRepository
from proficentr.students import Student
from proficentr.trainings import Training, TrainingGroup, PREPARATION, EXCLUDED
from proficentr.training_repository import (
    TrainingRepository, TrainingRepositoryError, DuplicateTrainingError,
    OverlappingTrainingError, ProgramChangeRequired,
)


@pytest.fixture
def records(tmp_path):
    with Database(tmp_path / 'test.sqlite3') as db:
        repo = TrainingRepository(db.connection)
        program = repo.programs.save(Program(full_name='Тестовая ДПО', direction='dpo', kind='dpo_upskilling',
                                            hours=72, code='Д-1', edition='1', qualification='Специалист'))
        other = repo.programs.save(Program(full_name='Тестовая ПО', direction='po', kind='po_retraining',
                                          hours=144, qualification='Тестовая профессия', grade='3'))
        students = StudentRepository(db.connection)
        first = students.save(Student(surname='Тестовый', first_name='Первый', birth_date='1990-01-01'))
        second = students.save(Student(surname='Примеров', first_name='Второй', birth_date='1992-02-29'))
        group = repo.save_group(TrainingGroup(name='ДПО-01', program_id=program.id, start_date='2020-03-01', end_date='2020-04-01'))
        yield SimpleNamespace(db=db, repo=repo, program=program, other=other, first=first, second=second, group=group)


def individual(r, **changes):
    return replace(Training(student_id=r.first.id, program_id=r.program.id, start_date='2020-03-01', end_date='2020-04-01'), **changes)


def test_group_existing_program_search_and_reopen(records):
    r = records
    assert r.repo.load_group(r.group.id) == r.group
    assert r.repo.search_groups('дПо-01')[0][0] == r.group.id
    assert r.repo.search_groups('тестовая дпо')[0][0] == r.group.id
    assert not r.repo.search_groups("' OR 1=1 --")
    assert not r.repo.search_groups('нет такой группы')
    assert r.repo.search_students('тестовый')[0] == (r.first.id, r.first.full_name + ' ', r.first.birth_date)
    assert not r.repo.search_students('')


def test_group_single_and_batch_default_dates_and_ids(records):
    r = records
    first = r.repo.add_students(r.group.id, [r.first.id])[0]
    second = r.repo.add_students(r.group.id, [r.second.id])[0]
    assert first.id != second.id
    assert first.student_id == r.first.id and first.group_id == r.group.id
    assert first.program_id == r.program.id
    assert (first.start_date, first.end_date) == (r.group.start_date, r.group.end_date)
    assert first.state == PREPARATION
    assert len(r.repo.list_trainings(group_id=r.group.id)) == 2
    assert len(r.repo.list_trainings(group_id=r.group.id, query='примеров')) == 1


def test_batch_add_is_atomic_and_duplicate_protected(records):
    r = records
    with pytest.raises(ValueError, match='существующего слушателя'):
        r.repo.add_students(r.group.id, [r.first.id, 99999])
    assert not r.repo.list_trainings(group_id=r.group.id)
    r.repo.add_students(r.group.id, [r.first.id, r.second.id])
    with pytest.raises(DuplicateTrainingError):
        r.repo.add_students(r.group.id, [r.first.id])
    assert len(r.repo.list_trainings(group_id=r.group.id)) == 2


def test_individual_multiple_periods_and_programs(records):
    r = records
    one = r.repo.save(individual(r))
    two = r.repo.save(individual(r, start_date='2021-01-01', end_date='2021-02-01'))
    three = r.repo.save(individual(r, program_id=r.other.id))
    assert len({one.id, two.id, three.id}) == 3
    assert all(row.group_id is None and row.student_id == r.first.id for row in (one, two, three))
    assert len(r.repo.list_trainings()) == 3


def test_overlap_warning_explicit_override_and_edit_self(records):
    r = records
    one = r.repo.save(individual(r))
    with pytest.raises(OverlappingTrainingError):
        r.repo.save(individual(r, start_date='2020-04-01', end_date='2020-05-01'))
    two = r.repo.save(individual(r), allow_overlap=True)
    assert two.id != one.id
    with pytest.raises(OverlappingTrainingError):
        r.repo.add_students(r.group.id, [r.first.id, r.second.id])
    assert not r.repo.list_trainings(group_id=r.group.id)
    assert len(r.repo.add_students(r.group.id, [r.first.id, r.second.id], allow_overlap=True)) == 2


@pytest.mark.parametrize('born,start,allowed', [
    ('2000-01-01', '2017-12-31', False), ('2000-01-01', '2018-01-01', True),
    ('2000-02-29', '2018-02-28', False), ('2000-02-29', '2018-03-01', True),
    ('2000-02-29', '2020-02-29', True), ('2000-01-01', '1999-01-01', False),
])
def test_age_on_actual_training_start(records, born, start, allowed):
    r = records
    student = StudentRepository(r.db.connection).save(Student(surname='Граничный', first_name='Пример', birth_date=born))
    training = individual(r, student_id=student.id, start_date=start, end_date=start)
    if allowed:
        assert r.repo.save(training).start_date == start
    else:
        with pytest.raises(ValueError, match='18 лет'):
            r.repo.save(training)


@pytest.mark.parametrize('start,end', [('2020-02-30', '2020-03-01'), ('20200301', '2020-03-02'),
                                        ('2020-03-02', '2020-03-01'), ('', '2020-03-01')])
def test_invalid_calendar_dates(records, start, end):
    r = records
    with pytest.raises(ValueError):
        r.repo.save(individual(r, start_date=start, end_date=end))
    with pytest.raises(ValueError):
        r.repo.save_group(replace(r.group, start_date=start, end_date=end))


def test_group_program_mismatch_and_database_constraint(records):
    r = records
    with pytest.raises(ValueError, match='совпадать'):
        r.repo.save(individual(r, group_id=r.group.id, program_id=r.other.id))
    with pytest.raises(sqlite3.IntegrityError):
        with r.db.connection:
            r.db.connection.execute("INSERT INTO trainings(student_id,program_id,group_id,start_date,end_date,program_snapshot) VALUES (?,?,?,?,?,?)",
                                    (r.first.id, r.other.id, r.group.id, '2020-01-01', '2020-01-02', '{}'))
    assert not r.repo.list_trainings(group_id=r.group.id)


def test_archival_restore_preserves_records_and_prevents_add(records):
    r = records
    training = r.repo.add_students(r.group.id, [r.first.id])[0]
    archived = r.repo.set_active(r.group.id, False)
    assert archived.id == r.group.id
    assert not r.repo.search_groups()
    assert r.repo.search_groups(active=False)[0][0] == r.group.id
    with pytest.raises(ValueError, match='архива'):
        r.repo.add_students(r.group.id, [r.second.id])
    with pytest.raises(ValueError, match='архива'):
        r.repo.exclude(training.id)
    assert r.repo.load(training.id) == training
    assert r.repo.set_active(r.group.id, True) == r.group


def test_safe_exclusion_history_and_readmission(records):
    r = records
    individual_training = r.repo.save(individual(r, program_id=r.other.id))
    group_training = r.repo.add_students(r.group.id, [r.first.id])[0]
    excluded = r.repo.exclude(group_training.id)
    assert excluded.state == EXCLUDED and excluded.group_id == r.group.id
    assert not r.repo.list_trainings(group_id=r.group.id)
    assert len(r.repo.list_trainings(group_id=r.group.id, include_excluded=True)) == 1
    assert r.repo.load(individual_training.id) == individual_training
    assert StudentRepository(r.db.connection).load(r.first.id) == r.first
    assert r.repo.add_students(r.group.id, [r.first.id])[0].id != excluded.id
    with pytest.raises(ValueError):
        r.repo.save(replace(excluded, state=PREPARATION))
    with pytest.raises(ValueError):
        r.repo.exclude(individual_training.id)


def test_snapshot_independence_explicit_refresh_and_creation_timestamp(records):
    r = records
    saved = r.repo.save(individual(r, created_at='1900-01-01', program_snapshot='forged'))
    snap = json.loads(saved.program_snapshot)
    assert snap == dict(full_name=r.program.full_name, direction='dpo', kind='dpo_upskilling', hours=72,
                        code='Д-1', edition='1', qualification='Специалист', grade='')
    assert date.fromisoformat(saved.created_at[:10]) == date.today()
    changed = r.repo.programs.save(replace(r.program, full_name='Исправленная программа', hours=80))
    assert r.repo.load(saved.id) == saved
    edited = r.repo.save(replace(saved, note='Примечание', created_at='1900', program_snapshot='forged'))
    assert edited.program_snapshot == saved.program_snapshot and edited.created_at == saved.created_at
    refreshed = r.repo.save(edited, refresh_snapshot=True)
    assert json.loads(refreshed.program_snapshot)['full_name'] == changed.full_name
    assert json.loads(refreshed.program_snapshot)['hours'] == 80
    assert refreshed.id == saved.id and refreshed.created_at == saved.created_at


def test_controlled_program_change_and_group_atomic_propagation(records):
    r = records
    individual_training = r.repo.save(individual(r, start_date='2021-01-01', end_date='2021-02-01'))
    with pytest.raises(ProgramChangeRequired):
        r.repo.save(replace(individual_training, program_id=r.other.id))
    changed = r.repo.save(replace(individual_training, program_id=r.other.id), refresh_snapshot=True)
    assert json.loads(changed.program_snapshot)['direction'] == 'po'
    members = r.repo.add_students(r.group.id, [r.first.id, r.second.id])
    new_group = replace(r.group, program_id=r.other.id)
    with pytest.raises(ProgramChangeRequired):
        r.repo.save_group(new_group)
    assert r.repo.load_group(r.group.id) == r.group
    assert r.repo.load(members[0].id) == members[0]
    r.repo.save_group(new_group, change_program=True)
    for member in members:
        stored = r.repo.load(member.id)
        assert stored.student_id == member.student_id and stored.group_id == member.group_id
        assert stored.program_id == r.other.id and stored.created_at == member.created_at
        assert json.loads(stored.program_snapshot)['qualification'] == 'Тестовая профессия'
    assert r.db.connection.execute('PRAGMA foreign_key_check').fetchall() == []


def test_group_dates_do_not_silently_change_member_dates(records):
    r = records
    member = r.repo.add_students(r.group.id, [r.first.id])[0]
    r.repo.save_group(replace(r.group, start_date='2021-01-01', end_date='2021-02-01'))
    assert r.repo.load(member.id) == member
    assert r.repo.add_students(r.group.id, [r.second.id])[0].start_date == '2021-01-01'


def test_archived_and_missing_programs_rejected(records):
    r = records
    r.repo.programs.set_active(r.program.id, False)
    with pytest.raises(ValueError, match='активная'):
        r.repo.save(individual(r))
    with pytest.raises(ValueError, match='активная'):
        r.repo.save_group(replace(r.group, id=None))
    with pytest.raises(TrainingRepositoryError):
        r.repo.save(individual(r, program_id=99999))


def test_transaction_failure_rolls_back_group_and_member_snapshots(records):
    r = records
    member = r.repo.add_students(r.group.id, [r.first.id])[0]
    r.db.connection.execute("CREATE TRIGGER fail_group BEFORE UPDATE ON training_groups BEGIN SELECT RAISE(ABORT, 'private diagnostic'); END")
    with pytest.raises(TrainingRepositoryError) as error:
        r.repo.save_group(replace(r.group, program_id=r.other.id), change_program=True)
    assert 'private diagnostic' not in str(error.value)
    assert r.repo.load_group(r.group.id) == r.group and r.repo.load(member.id) == member


def test_migration_six_preserves_all_previous_tables_and_backup(tmp_path, monkeypatch):
    path = tmp_path / 'old.sqlite3'
    migrations = database_module.MIGRATIONS
    with monkeypatch.context() as old:
        old.setattr(database_module, 'MIGRATIONS', migrations[:5])
        with Database(path) as db:
            db.connection.execute("INSERT INTO training_center(id,full_name) VALUES (1,'Тестовый центр')")
            db.connection.execute("INSERT INTO students(surname,first_name,birth_date) VALUES ('Тест','Пример','1990-01-01')")
            db.connection.execute("INSERT INTO counterparties(kind,full_name) VALUES ('legal_entity','Тестовый заказчик')")
            db.connection.commit()
            ProgramRepository(db.connection).save(Program(full_name='Тестовая ПО', direction='po', kind='po_retraining', hours=80, active=False))
            before = {name: db.connection.execute(f'SELECT * FROM {name}').fetchall() for name in
                      ('schema_migrations', 'students', 'counterparties', 'programs', 'training_center')}
    for _ in range(2):
        with Database(path) as db:
            assert db.connection.execute('SELECT max(version) FROM schema_migrations').fetchone() == (len(migrations),)
            for table, rows in before.items():
                stored = db.connection.execute(f'SELECT * FROM {table}').fetchall()
                assert (stored[:5] if table == 'schema_migrations' else stored) == rows
            assert db.connection.execute('PRAGMA foreign_key_check').fetchall() == []
    backups = list((tmp_path / 'migration-backups').glob('*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('SELECT max(version) FROM schema_migrations').fetchone() == (5,)
        assert backup.execute('SELECT * FROM students').fetchall() == before['students']


def test_u01_switch_preserves_groups_trainings_and_snapshots(records, tmp_path, monkeypatch):
    from pathlib import Path
    from proficentr import updater

    r = records
    group_training = r.repo.add_students(r.group.id, [r.first.id])[0]
    other = r.repo.save(individual(r, program_id=r.other.id))
    r.repo.set_active(r.group.id, False)
    archived = r.repo.load_group(r.group.id)
    r.repo.programs.save(replace(r.program, full_name='Исправление после подготовки'))
    before = r.db.path.read_bytes()
    install = tmp_path / 'app'; install.mkdir()
    (install / 'old').write_text('old')
    staged = tmp_path / 'staged'; staged.mkdir()
    (staged / 'new').write_text('new')
    monkeypatch.setattr(updater.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    def launch(args, **kwargs):
        Path(args[-1]).write_text('ready')
        return SimpleNamespace(poll=lambda: None)
    updater.switch(install, staged, r.db.path, tmp_path, launch=launch)
    assert r.db.path.read_bytes() == before
    with Database(r.db.path) as reopened:
        repo = TrainingRepository(reopened.connection)
        assert repo.load_group(archived.id) == archived
        assert repo.load(group_training.id) == group_training
        assert repo.load(other.id) == other


def test_group_program_change_overlap_warning_rolls_back_until_confirmed(records):
    r = records
    member = r.repo.add_students(r.group.id, [r.first.id])[0]
    r.repo.save(individual(r, program_id=r.other.id))
    changed = replace(r.group, program_id=r.other.id)
    with pytest.raises(OverlappingTrainingError):
        r.repo.save_group(changed, change_program=True)
    assert r.repo.load_group(r.group.id) == r.group and r.repo.load(member.id) == member
    r.repo.save_group(changed, change_program=True, allow_overlap=True)
    assert r.repo.load(member.id).program_id == r.other.id
