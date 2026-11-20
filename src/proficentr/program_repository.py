"""Транзакционный справочник программ без физического удаления."""

from dataclasses import asdict, replace
import sqlite3

from proficentr.programs import DIRECTIONS, Program, validate_program

FIELDS = tuple(asdict(Program()))
DATA_FIELDS = FIELDS[1:]


class DuplicateProgramError(Exception):
    def __init__(self, matches: list[Program]):
        super().__init__("Найдены программы с совпадающим наименованием или кодом. Проверьте вид и редакцию.")
        self.matches = matches


class ProgramRepositoryError(Exception):
    """Диагностика без исходных сообщений SQLite и содержимого карточек."""


class ProgramRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.create_function("program_casefold", 1, lambda text: " ".join(text.split()).casefold(), deterministic=True)

    def _select(self, condition="1", parameters=()) -> list[Program]:
        try:
            rows = self.connection.execute(
                f"SELECT {', '.join(FIELDS)} FROM programs WHERE {condition} "  # nosec B608 # Fixed fields and internal conditions; values are bound parameters.
                "ORDER BY program_casefold(full_name), id", parameters,
            ).fetchall()
            return [Program(**(dict(zip(FIELDS, row)) | {"active": bool(row[-1])})) for row in rows]
        except sqlite3.Error:
            raise ProgramRepositoryError("Не удалось прочитать реестр программ. Проверьте доступность базы.") from None

    def load(self, program_id: int) -> Program:
        rows = self._select("id = ?", (program_id,))
        if not rows:
            raise ProgramRepositoryError("Карточка программы не найдена.")
        return rows[0]

    def search(self, query: str = "", *, direction: str | None = None, active: bool | None = True) -> list[Program]:
        if direction is not None and direction not in DIRECTIONS:
            raise ValueError("Неизвестное направление программ.")
        if active is not None and type(active) is not bool:
            raise ValueError("Неизвестный фильтр активности.")
        query = " ".join(query.split()).casefold()
        return self._select(
            "(? = '' OR instr(program_casefold(full_name), ?) > 0 OR instr(program_casefold(short_name), ?) > 0 "
            "OR instr(program_casefold(code), ?) > 0) AND (? IS NULL OR direction = ?) AND (? IS NULL OR active = ?)",
            (query, query, query, query, direction, direction, active, active),
        )

    def find_duplicates(self, program: Program) -> list[Program]:
        # Archived entries also participate. Different editions may be saved explicitly.
        return self._select(
            "id != ? AND (program_casefold(full_name) = ? OR (? != '' AND program_casefold(code) = ?))",
            (program.id or 0, program.full_name.casefold(), program.code, program.code.casefold()),
        )

    def save(self, program: Program, *, allow_duplicates=False) -> Program:
        program = validate_program(program)
        if self.connection.in_transaction:
            raise ProgramRepositoryError("Завершите текущую операцию с базой перед сохранением.")
        try:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                matches = self.find_duplicates(program)
                if matches and not allow_duplicates:
                    raise DuplicateProgramError(matches)
                values = asdict(program)
                parameters = tuple(values[field] for field in DATA_FIELDS)
                if program.id is None:
                    cursor = self.connection.execute(
                        f"INSERT INTO programs ({', '.join(DATA_FIELDS)}) VALUES ({', '.join('?' for _ in DATA_FIELDS)})",  # nosec B608 # Fixed fields; bound values.
                        parameters,
                    )
                    program.id = cursor.lastrowid
                else:
                    cursor = self.connection.execute(
                        f"UPDATE programs SET {', '.join(field + ' = ?' for field in DATA_FIELDS)} WHERE id = ?",  # nosec B608 # Fixed fields; bound values.
                        (*parameters, program.id),
                    )
                    if cursor.rowcount != 1:
                        raise ProgramRepositoryError("Карточка программы не найдена; изменения не сохранены.")
            return program
        except OverflowError:
            raise ProgramRepositoryError("Указанное число часов слишком велико для сохранения.") from None
        except sqlite3.Error:
            raise ProgramRepositoryError("Не удалось сохранить программу. Изменения не применены. Проверьте доступность базы.") from None

    def set_active(self, program_id: int, active: bool) -> Program:
        if type(active) is not bool:
            raise ValueError("Выберите статус программы: активная или архивная.")
        if self.connection.in_transaction:
            raise ProgramRepositoryError("Завершите текущую операцию с базой перед изменением статуса.")
        try:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                program = self.load(program_id)
                self.connection.execute("UPDATE programs SET active = ? WHERE id = ?", (active, program_id))
            return replace(program, active=active)
        except sqlite3.Error:
            raise ProgramRepositoryError("Не удалось изменить статус программы. Изменения не применены.") from None
