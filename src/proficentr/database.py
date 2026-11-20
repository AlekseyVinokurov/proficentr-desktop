"""SQLite и последовательные транзакционные миграции."""

from contextlib import closing
from pathlib import Path
import uuid
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
    ("""CREATE TABLE students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        surname TEXT NOT NULL, first_name TEXT NOT NULL,
        patronymic TEXT NOT NULL DEFAULT '', birth_date TEXT NOT NULL,
        snils TEXT NOT NULL DEFAULT '', citizenship TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
        registration_address TEXT NOT NULL DEFAULT '', residential_address TEXT NOT NULL DEFAULT '',
        passport_series TEXT NOT NULL DEFAULT '', passport_number TEXT NOT NULL DEFAULT '',
        passport_issued_by TEXT NOT NULL DEFAULT '', passport_issue_date TEXT NOT NULL DEFAULT '',
        passport_department_code TEXT NOT NULL DEFAULT '', education_level TEXT NOT NULL DEFAULT '',
        education_document TEXT NOT NULL DEFAULT '', education_details TEXT NOT NULL DEFAULT '',
        workplace TEXT NOT NULL DEFAULT '', job_title TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT ''
    )""",
     "CREATE INDEX students_snils_idx ON students(snils)",
     "CREATE INDEX students_birth_date_idx ON students(birth_date)"),
    ("""CREATE TABLE counterparties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('legal_entity', 'sole_proprietor')),
        full_name TEXT NOT NULL CHECK (length(trim(full_name)) > 0),
        short_name TEXT NOT NULL DEFAULT '', inn TEXT NOT NULL DEFAULT '',
        kpp TEXT NOT NULL DEFAULT '' CHECK (kind != 'sole_proprietor' OR kpp = ''),
        ogrn TEXT NOT NULL DEFAULT '', legal_address TEXT NOT NULL DEFAULT '',
        actual_address TEXT NOT NULL DEFAULT '', postal_address TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', website TEXT NOT NULL DEFAULT '',
        contact_name TEXT NOT NULL DEFAULT '', contact_position TEXT NOT NULL DEFAULT '',
        bank_name TEXT NOT NULL DEFAULT '', bik TEXT NOT NULL DEFAULT '',
        settlement_account TEXT NOT NULL DEFAULT '', correspondent_account TEXT NOT NULL DEFAULT '',
        representative_name TEXT NOT NULL DEFAULT '', representative_position TEXT NOT NULL DEFAULT '',
        authority_basis TEXT NOT NULL DEFAULT '', power_of_attorney TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT ''
    )""",
     "CREATE INDEX counterparties_inn_idx ON counterparties(inn)"),
    ("""CREATE TABLE programs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT NOT NULL CHECK (length(trim(full_name)) > 0),
        short_name TEXT NOT NULL DEFAULT '', code TEXT NOT NULL DEFAULT '',
        direction TEXT NOT NULL, kind TEXT NOT NULL,
        hours INTEGER NOT NULL CHECK (typeof(hours) = 'integer' AND hours > 0),
        qualification TEXT NOT NULL DEFAULT '', grade TEXT NOT NULL DEFAULT '',
        approval_date TEXT NOT NULL DEFAULT '', approval_number TEXT NOT NULL DEFAULT '',
        approval_details TEXT NOT NULL DEFAULT '', edition TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        CHECK ((direction = 'dpo' AND kind IN ('dpo_upskilling', 'dpo_retraining')) OR
               (direction = 'po' AND kind IN ('po_initial_training', 'po_retraining', 'po_upskilling')))
    )""",
     "CREATE INDEX programs_active_direction_idx ON programs(active, direction)"),
    ("""CREATE TABLE training_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL CHECK (length(trim(name)) > 0),
        program_id INTEGER NOT NULL REFERENCES programs(id),
        start_date TEXT NOT NULL, end_date TEXT NOT NULL CHECK (end_date >= start_date),
        note TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        UNIQUE (id, program_id)
    )""",
     """CREATE TABLE trainings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL REFERENCES students(id),
        program_id INTEGER NOT NULL REFERENCES programs(id),
        group_id INTEGER,
        start_date TEXT NOT NULL, end_date TEXT NOT NULL CHECK (end_date >= start_date),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        state TEXT NOT NULL DEFAULT 'preparation' CHECK (state IN ('preparation', 'excluded')),
        note TEXT NOT NULL DEFAULT '', program_snapshot TEXT NOT NULL,
        CHECK (state != 'excluded' OR group_id IS NOT NULL),
        FOREIGN KEY (group_id, program_id) REFERENCES training_groups(id, program_id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
     "CREATE INDEX training_groups_program_idx ON training_groups(program_id)",
     "CREATE INDEX trainings_group_idx ON trainings(group_id)",
     "CREATE INDEX trainings_program_idx ON trainings(program_id)",
     "CREATE INDEX trainings_student_dates_idx ON trainings(student_id, start_date, end_date)",
     "CREATE UNIQUE INDEX trainings_group_student_idx ON trainings(group_id, student_id) WHERE state = 'preparation'"),
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
            if versions and len(versions) < len(MIGRATIONS):
                # Reserved write lock blocks concurrent writers. Read via a separate
                # connection: backing up the connection holding a write lock would hang.
                from proficentr.storage import backup_database

                folder = self.path.parent / "migration-backups"
                folder.mkdir(exist_ok=True)
                backup = folder / (uuid.uuid4().hex + ".sqlite3")
                trial = folder / (uuid.uuid4().hex + ".trial.sqlite3")
                with closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)) as source:
                    backup_database(source, backup)
                try:
                    with closing(sqlite3.connect(backup)) as source:
                        backup_database(source, trial)
                    with closing(sqlite3.connect(trial)) as copy:
                        copy.execute("PRAGMA foreign_keys = ON")
                        with copy:
                            self._apply(copy, len(versions))
                        if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                            raise RuntimeError("Проверка пробной миграции не пройдена.")
                finally:
                    trial.unlink(missing_ok=True)
            self._apply(self.connection, len(versions))

    @staticmethod
    def _apply(connection, applied):
        for version, statements in enumerate(MIGRATIONS, start=1):
            if version <= applied:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
