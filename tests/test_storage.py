from datetime import date
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from proficentr import settings, storage as module
from proficentr.database import Database
from proficentr.services import open_database, prepare_storage
from proficentr.storage import DIRECTORIES, Storage, relocate_storage


def test_first_creation_and_reinitialization(tmp_path):
    root = tmp_path / "Учебный центр с пробелами"
    storage = prepare_storage(root)
    assert {p.name for p in root.iterdir()} == set(DIRECTORIES)
    assert list((root / "documents").iterdir()) == []
    file = root / "templates" / "synthetic.txt"
    file.write_text("fictional", encoding="utf-8")
    with open_database(storage) as db:
        db.connection.execute("CREATE TABLE probe(value TEXT)")
        with db.connection:
            db.connection.execute("INSERT INTO probe VALUES ('fictional')")
    same = prepare_storage()
    with open_database(same) as db:
        assert db.connection.execute("SELECT value FROM probe").fetchone() == ("fictional",)
    assert same.root == root
    assert file.read_text(encoding="utf-8") == "fictional"


def test_first_run_requires_selection(tmp_path, monkeypatch):
    root = tmp_path / "unselected"
    monkeypatch.setattr(settings, "default_data_directory", lambda: root)
    assert prepare_storage() is None
    assert not root.exists()
    assert not settings.config_file().exists()


def test_historical_document_path(tmp_path):
    storage = prepare_storage(tmp_path / "archive")
    path = storage.document_directory(date(2019, 2, 17), "contracts")
    assert path == storage.root / "documents" / "2019" / "02" / "contracts"
    assert path.is_dir()
    assert not (path.parent / "orders").exists()
    with pytest.raises(ValueError):
        storage.document_directory(date(2019, 2, 17), "../../escape")


def test_move_wal_database_and_archive(tmp_path):
    source = prepare_storage(tmp_path / "source")
    original = source.root / "attachments" / "synthetic.txt"
    original.write_text("fictional", encoding="utf-8")
    with open_database(source) as db:
        db.connection.execute("PRAGMA journal_mode=WAL")
        db.connection.execute("PRAGMA wal_autocheckpoint=0")
        with db.connection:
            db.connection.execute("CREATE TABLE probe(value TEXT)")
            db.connection.execute("INSERT INTO probe VALUES ('fictional')")
        assert Path(str(source.database_path) + "-wal").stat().st_size > 0
        target = relocate_storage(source, db.connection, tmp_path / "Новый архив")
        assert db.connection.execute("SELECT value FROM probe").fetchone() == ("fictional",)
    assert settings.configured_directory() == target.root
    assert source.database_path.is_file()
    assert original.is_file()
    assert (target.root / "attachments" / original.name).read_text() == "fictional"
    with open_database() as copied:
        assert copied.connection.execute("SELECT value FROM probe").fetchone() == ("fictional",)
        assert copied.connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_destination_conflict(tmp_path):
    source = prepare_storage(tmp_path / "source")
    target = tmp_path / "target"
    (target / "database").mkdir(parents=True)
    conflict = target / "database" / "proficentr.sqlite3"
    conflict.write_bytes(b"do not overwrite")
    with open_database(source) as db, pytest.raises(FileExistsError):
        relocate_storage(source, db.connection, target)
    assert conflict.read_bytes() == b"do not overwrite"
    assert settings.configured_directory() == source.root


@pytest.mark.parametrize("failure", ["configuration", "backup", "space"])
def test_transfer_failure_preserves_configuration(tmp_path, monkeypatch, failure):
    source = prepare_storage(tmp_path / "source")
    previous = settings.config_file().read_bytes()
    target = tmp_path / "target"

    def fail(*args):
        raise OSError("synthetic failure")

    if failure == "configuration":
        monkeypatch.setattr(settings, "save_directory", fail)
    elif failure == "backup":
        monkeypatch.setattr(module, "backup_database", fail)
    else:
        monkeypatch.setattr(module.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with open_database(source) as db:
        with pytest.raises(OSError):
            relocate_storage(source, db.connection, target)
        assert db.connection.execute("SELECT version FROM schema_migrations").fetchone() == (1,)
    assert settings.config_file().read_bytes() == previous
    assert not target.exists()
    assert source.database_path.is_file()


def test_config_write_failure_is_atomic(tmp_path, monkeypatch):
    old = prepare_storage(tmp_path / "source")
    before = settings.config_file().read_bytes()

    def fail(*args):
        raise PermissionError("synthetic denied")

    monkeypatch.setattr(settings.os, "replace", fail)
    with pytest.raises(PermissionError):
        settings.save_directory(tmp_path / "other")
    assert settings.config_file().read_bytes() == before
    assert settings.configured_directory() == old.root


def test_missing_configured_storage_does_not_create_empty_database(tmp_path):
    storage = prepare_storage(tmp_path / "source")
    storage.database_path.unlink()
    with pytest.raises(OSError, match="Новая база не создавалась"):
        prepare_storage()
    assert not storage.database_path.exists()


def test_unwritable_storage(tmp_path, monkeypatch):
    storage = prepare_storage(tmp_path / "source")

    def denied(*args, **kwargs):
        raise PermissionError("synthetic denied")

    monkeypatch.setattr(module.tempfile, "TemporaryFile", denied)
    with pytest.raises(PermissionError):
        prepare_storage()
    assert storage.database_path.exists()


@pytest.mark.parametrize("folder", ["OneDrive", "OneDrive - Company", "Dropbox", "Google Drive"])
def test_synchronized_path_rejected(tmp_path, folder):
    with pytest.raises(ValueError, match="синхронизации"):
        Storage(tmp_path / folder / "archive")


def test_relative_and_nested_paths_rejected(tmp_path):
    with pytest.raises(ValueError):
        Storage(Path("relative"))
    source = prepare_storage(tmp_path / "source")
    with open_database(source) as db, pytest.raises(ValueError):
        relocate_storage(source, db.connection, source.root / "nested")


def test_legacy_database_migration_preserves_original(tmp_path, monkeypatch):
    root = tmp_path / "legacy"
    monkeypatch.setattr(settings, "default_data_directory", lambda: root)
    old_path = root / "proficentr.sqlite3"
    with Database(old_path) as db:
        with db.connection:
            db.connection.execute("CREATE TABLE probe(value TEXT)")
            db.connection.execute("INSERT INTO probe VALUES ('fictional')")
    adopted = prepare_storage()
    assert old_path.is_file()
    assert adopted.database_path != old_path
    with open_database(adopted) as db:
        assert db.connection.execute("SELECT value FROM probe").fetchone() == ("fictional",)
    assert prepare_storage().root == root


def test_invalid_configuration_does_not_fall_back(tmp_path):
    settings.config_file().parent.mkdir(parents=True)
    settings.config_file().write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="настройки"):
        prepare_storage()


def test_override_legacy_reopens(tmp_path, monkeypatch):
    root = tmp_path / "override"
    monkeypatch.setenv("PROFICENTR_DATA_DIR", str(root))
    with Database(root / "proficentr.sqlite3"):
        pass
    for _ in range(2):
        with open_database() as db:
            assert db.path == root / "database" / "proficentr.sqlite3"


def test_missing_override_storage_does_not_recreate_database(tmp_path, monkeypatch):
    monkeypatch.setenv("PROFICENTR_DATA_DIR", str(tmp_path / "override"))
    storage = prepare_storage()
    storage.database_path.unlink()
    with pytest.raises(OSError):
        prepare_storage()
    assert not storage.database_path.exists()


def test_new_initialization_failure_cleans_owned_files(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError("synthetic failure")

    monkeypatch.setattr(settings, "save_directory", fail)
    root = tmp_path / "new"
    with pytest.raises(OSError):
        prepare_storage(root)
    assert not root.exists()


def test_no_create_connection_does_not_recreate_missing_database(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        Database(path, create=False)
    assert not path.exists()
