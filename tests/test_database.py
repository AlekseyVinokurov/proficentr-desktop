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
        assert [row[0] for row in first] == [1]
        assert database.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == [("schema_migrations",)]
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
            assert database.connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,), (2,)]


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
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='migration_probe'").fetchall() == []


def test_newer_schema_is_rejected(tmp_path):
    path = tmp_path / "test.sqlite3"
    with Database(path) as database:
        with database.connection:
            database.connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
    with pytest.raises(RuntimeError, match="новой"):
        Database(path)
