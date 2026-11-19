"""SQLite и последовательные транзакционные миграции."""

from pathlib import Path
import sqlite3

from proficentr.settings import _require_local


# Каждая новая миграция добавляется в конец; уже применённые не изменяются.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    ("""CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",),
    ("""CREATE TABLE training_center (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        full_name TEXT NOT NULL DEFAULT '', short_name TEXT NOT NULL DEFAULT '',
        inn TEXT NOT NULL DEFAULT '', kpp TEXT NOT NULL DEFAULT '', ogrn TEXT NOT NULL DEFAULT '',
        legal_address TEXT NOT NULL DEFAULT '', actual_address TEXT NOT NULL DEFAULT '',
        postal_address TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '', website TEXT NOT NULL DEFAULT '',
        bank_name TEXT NOT NULL DEFAULT '', bik TEXT NOT NULL DEFAULT '',
        settlement_account TEXT NOT NULL DEFAULT '', correspondent_account TEXT NOT NULL DEFAULT '',
        director_name TEXT NOT NULL DEFAULT '', director_position TEXT NOT NULL DEFAULT '',
        director_genitive TEXT NOT NULL DEFAULT '', license_number TEXT NOT NULL DEFAULT '',
        license_date TEXT NOT NULL DEFAULT '', license_authority TEXT NOT NULL DEFAULT '',
        logo_path TEXT NOT NULL DEFAULT ''
    )""",),
)


class Database:
    def __init__(self, path: Path, *, create: bool = True):
        self.path = path.resolve()
        _require_local(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            f"{self.path.as_uri()}?mode={'rwc' if create else 'rw'}", uri=True, timeout=10
        )
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self._migrate()
        except Exception:
            self.close()
            raise

    def _migrate(self) -> None:
        # Блокировка до чтения версии исключает гонку двух первых запусков.
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            exists = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            versions = [row[0] for row in self.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )] if exists else []
            if versions != list(range(1, len(versions) + 1)):
                raise RuntimeError("Нарушена последовательность миграций базы.")
            if len(versions) > len(MIGRATIONS):
                raise RuntimeError("База создана более новой версией приложения.")
            for version, statements in enumerate(MIGRATIONS, start=1):
                if version <= len(versions):
                    continue
                for statement in statements:
                    self.connection.execute(statement)
                self.connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)", (version,)
                )

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
