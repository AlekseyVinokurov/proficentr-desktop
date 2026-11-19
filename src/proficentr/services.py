"""Сценарий запуска; место будущей бизнес-логики без зависимости от интерфейса."""

import os
from contextlib import closing
from pathlib import Path
import sqlite3

from proficentr import settings
from proficentr.database import Database
from proficentr.storage import DIRECTORIES, Storage, backup_database


def prepare_storage(selected: Path | None = None) -> Storage | None:
    """None означает первый запуск, требующий выбора папки в интерфейсе."""
    override = os.environ.get("PROFICENTR_DATA_DIR")
    configured = settings.configured_directory()
    root = selected if selected is not None else settings.data_directory()
    storage = Storage(root)
    root = storage.root
    if configured is not None and selected is None and (override is None or configured == root):
        storage.require_available()
        storage.initialize()
        return storage
    if override is not None and selected is None and storage.database_path.is_file():
        storage.require_available()
        storage.initialize()
        with Database(storage.database_path, create=False):
            pass
        settings.save_directory(root)
        return storage
    legacy = root / "proficentr.sqlite3"
    if selected is None and override is None and not legacy.is_file():
        return None
    if selected is not None and root.exists() and any(root.iterdir()):
        raise FileExistsError("Для нового хранилища выберите пустой каталог.")
    if storage.database_path.exists():
        raise FileExistsError("Обнаружены две базы. Автоматический выбор отменён.")
    new_root = not root.exists()
    new_folders = [root / name for name in DIRECTORIES if not (root / name).exists()]
    owned_database = False
    try:
        storage.initialize()
        if legacy.is_file():
            with closing(sqlite3.connect(f"{legacy.as_uri()}?mode=ro", uri=True)) as source:
                backup_database(source, storage.database_path)
            owned_database = True
        else:
            with storage.database_path.open("xb"):
                pass
            owned_database = True
        with Database(storage.database_path, create=False):
            pass
        settings.save_directory(storage.root)
    except Exception:
        if owned_database:
            storage.database_path.unlink(missing_ok=True)
        for folder in reversed(new_folders):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        if new_root and root.is_dir() and not any(root.iterdir()):
            root.rmdir()
        raise
    return storage


def open_database(storage: Storage | None = None) -> Database:
    storage = storage or prepare_storage()
    if storage is None:
        raise ValueError("Выберите локальный каталог хранения при первом запуске.")
    storage.require_available()
    return Database(storage.database_path, create=False)
