from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest
from PySide6.QtGui import QImage

from proficentr import database as database_module
from proficentr.database import Database
from proficentr.organization import (
    MAX_LOGO_BYTES, TrainingCenter, TrainingCenterRepository, read_logo, validate_center,
)
from proficentr.services import open_database, prepare_storage
from proficentr.storage import relocate_storage


def make_logo(path, color="blue"):
    image = QImage(24, 16, QImage.Format.Format_RGB32)
    image.fill(color)
    assert image.save(str(path))
    return path


def test_save_reload_partial_and_no_duplicates(tmp_path):
    storage = prepare_storage(tmp_path / "archive")
    center = TrainingCenter(full_name="Учебный центр Пример", email="info@example.org")
    with open_database(storage) as db:
        repo = TrainingCenterRepository(db.connection, storage)
        assert repo.load() == TrainingCenter()
        assert repo.save(center) == center
        repo.save(replace(center, short_name="Пример"))
        assert db.connection.execute("SELECT count(*) FROM training_center").fetchone() == (1,)
    with open_database(storage) as db:
        assert TrainingCenterRepository(db.connection, storage).load() == replace(center, short_name="Пример")


def test_upgrade_p01_database(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite3"
    migrations = database_module.MIGRATIONS
    with monkeypatch.context() as patch:
        patch.setattr(database_module, "MIGRATIONS", migrations[:1])
        with Database(path) as db:
            with db.connection:
                db.connection.execute("CREATE TABLE legacy_probe(value TEXT)")
                db.connection.execute("INSERT INTO legacy_probe VALUES ('fictional')")
    for _ in range(2):
        with Database(path) as db:
            assert db.connection.execute("SELECT value FROM legacy_probe").fetchone() == ("fictional",)
            assert db.connection.execute("SELECT count(*) FROM training_center").fetchone() == (0,)
            assert db.connection.execute("SELECT version FROM schema_migrations").fetchall() == [(1,), (2,), (3,)]


def test_logo_upload_replace_relocate_and_remove(tmp_path):
    source = prepare_storage(tmp_path / "source")
    png = make_logo(tmp_path / "logo.png")
    jpg = make_logo(tmp_path / "replacement.jpg", "red")
    with open_database(source) as db:
        repo = TrainingCenterRepository(db.connection, source)
        first = repo.save(TrainingCenter(short_name="Пример"), logo_source=png)
        old_file = repo.logo_file(first.logo_path)
        assert old_file.read_bytes() == png.read_bytes()
        assert not Path(first.logo_path).is_absolute()
        second = repo.save(first, logo_source=jpg)
        assert second.logo_path != first.logo_path
        assert not old_file.exists()
        target = relocate_storage(source, db.connection, tmp_path / "Новый архив")
    with open_database(target) as db:
        repo = TrainingCenterRepository(db.connection, target)
        loaded = repo.load()
        assert loaded == second
        file = repo.logo_file(loaded.logo_path)
        assert file.read_bytes() == jpg.read_bytes()
        removed = repo.save(loaded, remove_logo=True)
        assert removed.logo_path == ""
        assert not file.exists()
        assert repo.load() == removed


def test_failed_save_rolls_back_records_and_new_logo(tmp_path):
    storage = prepare_storage(tmp_path / "archive")
    logo = make_logo(tmp_path / "logo.png")
    with open_database(storage) as db:
        repo = TrainingCenterRepository(db.connection, storage)
        original = repo.save(TrainingCenter(full_name="До изменения"), logo_source=logo)
        files = set((storage.root / "organization").iterdir())
        db.connection.execute("""CREATE TRIGGER reject_center BEFORE UPDATE ON training_center
            BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        with pytest.raises(sqlite3.IntegrityError):
            repo.save(replace(original, full_name="После изменения"), logo_source=logo)
        assert repo.load() == original
        assert set((storage.root / "organization").iterdir()) == files
        with pytest.raises(sqlite3.IntegrityError):
            repo.save(original, remove_logo=True)
        assert repo.logo_file(original.logo_path).is_file()


@pytest.mark.parametrize("field,value", [
    ("inn", "123"), ("kpp", "bad"), ("ogrn", "12"), ("email", "wrong@email"),
    ("bik", "12"), ("settlement_account", "letters"), ("correspondent_account", "12"),
    ("license_date", "2025-02-30"),
])
def test_validation(field, value):
    with pytest.raises(ValueError):
        validate_center(replace(TrainingCenter(), **{field: value}))


def test_valid_optional_formats():
    center = TrainingCenter(
        inn="0" * 12, kpp="0000AB000", ogrn="0" * 15,
        bik="0" * 9, settlement_account="0" * 20, correspondent_account="0" * 20,
        email="example+test@example.org", license_date="1998-01-02",
    )
    assert validate_center(center) == center
    assert validate_center(TrainingCenter()) == TrainingCenter()


def test_invalid_logo_and_limit(tmp_path):
    file = tmp_path / "fake.png"
    file.write_text("not an image")
    with pytest.raises(ValueError):
        read_logo(file)
    file.write_bytes(b"x" * (MAX_LOGO_BYTES + 1))
    with pytest.raises(ValueError, match="5 МиБ"):
        read_logo(file)
    with pytest.raises(ValueError):
        read_logo(tmp_path / "fake.svg")


def test_logo_write_error_preserves_center(tmp_path, monkeypatch):
    storage = prepare_storage(tmp_path / "archive")
    image = make_logo(tmp_path / "logo.png")
    with open_database(storage) as db:
        repo = TrainingCenterRepository(db.connection, storage)
        before = repo.save(TrainingCenter(short_name="До"))
        original_open = Path.open

        def fail(path, mode="r", *args, **kwargs):
            if mode == "xb":
                raise PermissionError("synthetic denied")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", fail)
        with pytest.raises(PermissionError):
            repo.save(TrainingCenter(short_name="После"), logo_source=image)
        assert repo.load() == before
