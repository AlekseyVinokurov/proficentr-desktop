"""Локальный архив и безопасное копирование хранилища."""

from datetime import date
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile

from proficentr import settings

DIRECTORIES = ("database", "documents", "attachments", "templates", "organization", "exports")
CATEGORIES = {"contracts", "orders", "protocols", "issued", "other"}


class Storage:
    def __init__(self, root: Path):
        self.root = settings.validate_directory(root)

    @property
    def database_path(self) -> Path:
        return self.root / "database" / "proficentr.sqlite3"

    def initialize(self) -> None:
        for name in DIRECTORIES:
            folder = self.root / name
            if folder.is_symlink():
                raise ValueError(f"Каталог хранилища не должен быть символической ссылкой: {folder}")
            settings.validate_directory(folder)
            folder.mkdir(parents=True, exist_ok=True)

    def require_available(self) -> None:
        if not self.root.is_dir() or not self.database_path.is_file():
            raise OSError(f"Хранилище недоступно или отсутствует база: {self.database_path}. "
                          "Подключите локальный диск. Новая база не создавалась.")
        with tempfile.TemporaryFile(dir=self.database_path.parent) as probe:
            probe.write(b"check")
            probe.flush()

    def document_directory(self, document_date: date, category: str) -> Path:
        if category not in CATEGORIES:
            raise ValueError("Неизвестная категория документа.")
        self.require_available()
        path = self.root / "documents" / f"{document_date.year:04d}" / f"{document_date.month:02d}" / category
        settings.validate_directory(path)
        path.mkdir(parents=True, exist_ok=True)
        return path


def backup_database(source: sqlite3.Connection, destination: Path) -> None:
    """Копия учитывает WAL. Файл назначения создаётся исключительно новым."""
    if source.in_transaction:
        raise RuntimeError("Завершите текущую операцию с базой перед переносом.")
    size = source.execute("PRAGMA page_count").fetchone()[0] * source.execute("PRAGMA page_size").fetchone()[0]
    if shutil.disk_usage(destination.parent).free < size + 16 * 1024 * 1024:
        raise OSError("Недостаточно свободного места для копии SQLite.")
    with destination.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(destination)) as copy:
            source.backup(copy)
            if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise OSError("Проверка целостности копии SQLite не пройдена.")
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def relocate_storage(source: Storage, connection: sqlite3.Connection, destination: Path) -> Storage:
    """Копия архива, затем атомарная смена настройки. Исходник сохраняется."""
    source.require_available()
    target = Storage(destination)
    if target.root.is_relative_to(source.root) or source.root.is_relative_to(target.root):
        raise ValueError("Выберите отдельный каталог вне текущего хранилища.")
    if target.root.exists() and (not target.root.is_dir() or any(target.root.iterdir())):
        raise FileExistsError("Целевой каталог должен быть пустым; существующие данные не перезаписываются.")
    # Символические ссылки не переносятся: они могут указывать за пределы архива.
    size = connection.execute("PRAGMA page_count").fetchone()[0] * connection.execute("PRAGMA page_size").fetchone()[0]
    for name in DIRECTORIES[1:]:
        folder = source.root / name
        if folder.is_symlink():
            raise ValueError("В архиве обнаружена символическая ссылка.")
        if folder.exists():
            for file in folder.rglob("*"):
                if file.is_symlink():
                    raise ValueError("В архиве обнаружена символическая ссылка.")
                if file.is_file():
                    size += file.stat().st_size
    created = not target.root.exists()
    target.root.mkdir(parents=True, exist_ok=True)
    published = []
    try:
        with tempfile.TemporaryDirectory(prefix=".proficentr-transfer-", dir=target.root) as temporary:
            if shutil.disk_usage(target.root).free < size + 16 * 1024 * 1024:
                raise OSError("Недостаточно свободного места для переноса хранилища.")
            staged = Storage(Path(temporary))
            staged.initialize()
            for name in DIRECTORIES[1:]:
                folder = source.root / name
                if folder.exists():
                    shutil.copytree(folder, staged.root / name, dirs_exist_ok=True)
            backup_database(connection, staged.database_path)
            for name in DIRECTORIES:
                output = target.root / name
                if output.exists():
                    raise FileExistsError(f"Конфликт в каталоге назначения: {output}")
                (staged.root / name).rename(output)
                published.append(output)
            target.require_available()
        settings.save_directory(target.root)
    except Exception:
        for folder in reversed(published):
            shutil.rmtree(folder)
        if created:
            target.root.rmdir()
        raise
    return target
