from dataclasses import asdict, replace
import sqlite3

import pytest

from proficentr import database as module
from proficentr.counterparties import Counterparty, validate_counterparty
from proficentr.counterparty_repository import CounterpartyRepository, CounterpartyRepositoryError, DuplicateCounterpartyError
from proficentr.database import Database
from proficentr.students import Student
from proficentr.student_repository import StudentRepository


def sample(**kwargs):
    return replace(Counterparty(full_name='ООО Учебный пример', inn='1234567890'), **kwargs)


@pytest.fixture
def repo(tmp_path):
    with Database(tmp_path/'test.sqlite3') as db:
        yield CounterpartyRepository(db.connection)


@pytest.mark.parametrize('kind', ['legal_entity', 'sole_proprietor'])
def test_minimal_card(kind, repo):
    card = repo.save(Counterparty(kind=kind, full_name='Тестовый контрагент'))
    assert card.id and repo.load(card.id) == card
    assert not card.inn and not card.kpp and not card.ogrn


def test_full_card_normalization_and_all_fields_persist(repo):
    data = {field: '  Учебный   пример  ' for field in asdict(Counterparty()) if field not in ('id', 'kind')}
    data.update(inn='1234567890', kpp='1234AB789', ogrn='1234567890123', bik='123456789',
                settlement_account='1'*20, correspondent_account='2'*20, email='sample@example.invalid',
                note='  Первая   строка\n Вторая  строка ')
    original = Counterparty(**data)
    saved = repo.save(original)
    assert saved.full_name == 'Учебный пример'
    assert saved.note == 'Первая строка\nВторая строка'
    assert repo.load(saved.id) == saved
    assert original.id is None and original.full_name.startswith('  ')


@pytest.mark.parametrize('field,value', [
    ('kind', 'person'), ('full_name', ' \t '), ('inn', '123456789012'), ('inn', '１２３４５６７８９０'),
    ('kpp', '12345678'), ('kpp', 'AB3456789'), ('kpp', '1234АБ789'), ('ogrn', '123456789012345'),
    ('bik', '12345678x'), ('settlement_account', '1'*19), ('correspondent_account', '2'*21), ('email', 'bad@address'),
])
def test_invalid_filled_details(field, value, repo):
    with pytest.raises(ValueError):
        repo.save(sample(**{field:value}))
    assert repo.search() == []


def test_ip_without_kpp_and_type_specific_lengths(repo):
    ip = repo.save(sample(kind='sole_proprietor', inn='123456789012', ogrn='123456789012345'))
    assert not ip.kpp
    for change in [dict(inn='1234567890'), dict(ogrn='1234567890123'), dict(kpp='123456789')]:
        with pytest.raises(ValueError):
            repo.save(replace(ip, **change))
    assert repo.load(ip.id) == ip


def test_search_names_inn_and_formatted_phone(repo):
    saved = repo.save(sample(full_name='ООО Ёлка Пример', short_name='Учебная организация', phone='+7 (000) 123-45-67'))
    for query in ['ёлКА', 'ООО   ёлка', 'УЧЕБНАЯ', '1234567890', ' 12345 ', '+7 000 123-45-67', '123 45 67']:
        assert repo.search(query) == [saved]
    for query in ["' OR 1=1 --", '%', '_', 'Нет такого контрагента']:
        assert repo.search(query) == []


@pytest.mark.parametrize('change,reason', [
    (dict(full_name='Другое имя'), 'inn'),
    (dict(full_name='  ооо   учебный ПРИМЕР ', inn=''), 'name'),
    (dict(full_name='Иное полное', short_name='пример', inn=''), 'name'),
    (dict(full_name='ПРИМЕР', inn=''), 'name'),
])
def test_duplicate_detection_and_explicit_separate_record(repo, change, reason):
    existing = repo.save(sample(short_name='Пример'))
    candidate = sample(**change)
    with pytest.raises(DuplicateCounterpartyError) as error:
        repo.save(candidate)
    assert [(m.counterparty.id, m.reason) for m in error.value.matches] == [(existing.id, reason)]
    assert len(repo.search()) == 1
    assert not repo.connection.in_transaction
    separate = repo.save(candidate, allow_duplicates=True)
    assert separate.id != existing.id and len(repo.search()) == 2
    assert repo.load(existing.id) == existing


def test_update_keeps_id_and_excludes_itself_from_duplicates(repo):
    saved = repo.save(sample())
    changed = repo.save(replace(saved, phone='000-11-22'))
    assert changed.id == saved.id
    assert len(repo.search()) == 1
    assert repo.load(saved.id).phone == '000-11-22'


def test_update_duplicate_is_not_applied(repo):
    first = repo.save(sample())
    second = repo.save(sample(full_name='Другой пример', inn='9876543210'))
    with pytest.raises(DuplicateCounterpartyError):
        repo.save(replace(second, inn=first.inn))
    assert repo.load(second.id) == second


def test_missing_record_and_outer_transaction_are_not_silently_saved(repo):
    with pytest.raises(CounterpartyRepositoryError):
        repo.save(sample(id=999))
    with pytest.raises(CounterpartyRepositoryError):
        repo.load(999)
    repo.connection.execute('BEGIN')
    with pytest.raises(CounterpartyRepositoryError):
        repo.save(sample())
    assert repo.connection.in_transaction
    repo.connection.rollback()
    assert repo.search() == []


def test_storage_failure_rolls_back_without_details_in_logs(repo, caplog, capsys):
    marker = 'Конфиденциальный тестовый контрагент'
    card = sample(full_name=marker)
    repo.connection.execute("CREATE TRIGGER fail_counterparty BEFORE INSERT ON counterparties BEGIN SELECT RAISE(ABORT, 'private database diagnostic'); END")
    with pytest.raises(CounterpartyRepositoryError) as error:
        repo.save(card)
    assert marker not in str(error.value) and 'private database diagnostic' not in str(error.value)
    assert marker not in repr(card)
    assert repo.search() == [] and not repo.connection.in_transaction
    output = capsys.readouterr()
    assert not caplog.text and not output.out and not output.err
    repo.connection.close()
    with pytest.raises(CounterpartyRepositoryError) as error:
        repo.search(marker)
    assert marker not in str(error.value)


def test_reload_and_training_center_remain_independent(tmp_path):
    path = tmp_path/'test.sqlite3'
    with Database(path) as db:
        db.connection.execute("INSERT INTO training_center(id,full_name) VALUES (1,'Учебный центр — пример')")
        db.connection.commit()
        saved = CounterpartyRepository(db.connection).save(sample())
    with Database(path) as db:
        assert CounterpartyRepository(db.connection).load(saved.id) == saved
        assert db.connection.execute('SELECT full_name FROM training_center').fetchone() == ('Учебный центр — пример',)


def test_upgrade_from_schema_three_preserves_records_and_backup(tmp_path, monkeypatch):
    path = tmp_path/'old.sqlite3'
    migrations = module.MIGRATIONS
    with monkeypatch.context() as previous:
        previous.setattr(module, 'MIGRATIONS', migrations[:3])
        with Database(path) as db:
            student = StudentRepository(db.connection).save(Student(surname='Тестовый', first_name='Пример', birth_date='1990-01-01'))
            db.connection.execute("INSERT INTO training_center(id,full_name) VALUES (1,'Пример центра')")
            db.connection.commit()
            old_stamps = db.connection.execute('SELECT * FROM schema_migrations').fetchall()
    for _ in range(2):
        with Database(path, create=False) as db:
            assert StudentRepository(db.connection).load(student.id) == student
            assert db.connection.execute('SELECT full_name FROM training_center').fetchone() == ('Пример центра',)
            assert db.connection.execute('SELECT * FROM schema_migrations WHERE version<=3').fetchall() == old_stamps
            assert db.connection.execute('SELECT max(version) FROM schema_migrations').fetchone() == (4,)
            if not CounterpartyRepository(db.connection).search():
                saved = CounterpartyRepository(db.connection).save(sample())
            assert CounterpartyRepository(db.connection).load(saved.id) == saved
    backups = list((tmp_path/'migration-backups').glob('*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert backup.execute('SELECT max(version) FROM schema_migrations').fetchone() == (3,)
        assert backup.execute('SELECT surname FROM students').fetchall() == [('Тестовый',)]
    with monkeypatch.context() as previous:
        previous.setattr(module, 'MIGRATIONS', migrations[:3])
        with pytest.raises(RuntimeError, match='новой'):
            Database(path, create=False)
