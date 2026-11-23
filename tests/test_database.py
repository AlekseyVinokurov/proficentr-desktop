import sqlite3

import pytest

import proficentr.database as module
from proficentr.database import Database


def test_create_reopen_and_close(tmp_path):
    path = tmp_path / "nested" / "test.sqlite3"
    with Database(path) as database:
        assert path.is_file()
        assert database.connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        first = database.connection.execute("SELECT * FROM schema_migrations").fetchall()
        assert [row[0] for row in first] == list(range(1, len(module.MIGRATIONS) + 1))
        assert database.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("schema_migrations",), ("training_center",), ("students",), ("sqlite_sequence",), ("counterparties",), ("programs",), ("training_groups",), ("trainings",), ("document_templates",), ("contracts",), ("contract_trainings",), ("contract_sequences",), ("enrollment_orders",), ("enrollment_order_trainings",), ("enrollment_order_sequences",), ("training_enrollments",), ("attestation_protocols",), ("attestation_results",), ("attestation_sequences",), ("completion_orders",), ("completion_order_trainings",), ("completion_sequences",), ("training_completions",), ("issued_documents",), ("issued_sequences",), ("document_transfers",), ("transfer_evidence",), ("frdo_templates",), ("frdo_training_details",), ("frdo_change_history",), ("frdo_packages",), ("frdo_package_documents",)]
    with pytest.raises(sqlite3.ProgrammingError):
        database.connection.execute("SELECT 1")
    with Database(path) as reopened:
        assert reopened.connection.execute("SELECT * FROM schema_migrations").fetchall() == first


def test_next_migration_applies_once(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite3"
    with Database(path):
        pass
    monkeypatch.setattr(module, "MIGRATIONS", module.MIGRATIONS + ((
        "CREATE TABLE migration_probe (value TEXT)",
        "INSERT INTO migration_probe VALUES ('synthetic')",
    ),))
    for _ in range(2):
        with Database(path) as database:
            assert database.connection.execute("SELECT * FROM migration_probe").fetchall() == [("synthetic",)]
            assert database.connection.execute("SELECT version FROM schema_migrations").fetchall() == [(v,) for v in range(1, len(module.MIGRATIONS) + 1)]


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite3"
    with Database(path):
        pass
    monkeypatch.setattr(module, "MIGRATIONS", module.MIGRATIONS + ((
        "CREATE TABLE migration_probe (value TEXT)", "INVALID SQL",
    ),))
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(v,) for v in range(1, len(module.MIGRATIONS))]
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='migration_probe'").fetchall() == []


def test_newer_schema_is_rejected(tmp_path):
    path = tmp_path / "test.sqlite3"
    with Database(path) as database:
        with database.connection:
            database.connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (len(module.MIGRATIONS) + 1,))
    with pytest.raises(RuntimeError, match="новой"):
        Database(path)


def test_migration_backup_wal_and_preserved_records(tmp_path, monkeypatch):
    path = tmp_path / 'test.sqlite3'
    with Database(path) as db:
        db.connection.execute('PRAGMA journal_mode=WAL')
        db.connection.execute("INSERT INTO training_center(id,full_name) VALUES (1,'Synthetic center')")
        db.connection.commit()
        monkeypatch.setattr(module, 'MIGRATIONS', module.MIGRATIONS + (("CREATE TABLE new_feature (id INTEGER)",),))
        with Database(path) as upgraded:
            assert upgraded.connection.execute('SELECT full_name FROM training_center').fetchone() == ('Synthetic center',)
    backups=list((tmp_path/'migration-backups').glob('*.sqlite3'))
    assert len(backups)==1
    with sqlite3.connect(backups[0]) as copy:
        assert copy.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
        assert copy.execute('SELECT max(version) FROM schema_migrations').fetchone()==(len(module.MIGRATIONS) - 1,)
        assert copy.execute('SELECT full_name FROM training_center').fetchone()==('Synthetic center',)


def test_failed_trial_does_not_migrate_working_database(tmp_path,monkeypatch):
    path=tmp_path/'test.sqlite3'
    with Database(path):pass
    before=path.read_bytes()
    monkeypatch.setattr(module,'MIGRATIONS',module.MIGRATIONS+(("DROP TABLE training_center", "invalid SQL"),))
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    assert path.read_bytes()==before
    assert len(list((tmp_path/'migration-backups').glob('*.sqlite3')))==1
