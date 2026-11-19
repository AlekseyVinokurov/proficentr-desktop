"""Сценарий запуска; место будущей бизнес-логики без зависимости от интерфейса."""

from proficentr.database import Database
from proficentr.settings import data_directory


def open_database() -> Database:
    return Database(data_directory() / "proficentr.sqlite3")
