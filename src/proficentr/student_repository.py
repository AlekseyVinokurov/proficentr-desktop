"""Транзакционный доступ к реестру слушателей без SQL в интерфейсе."""

from dataclasses import asdict, dataclass
from datetime import date
import re
import sqlite3

from proficentr.students import Student, validate_student

FIELDS = tuple(asdict(Student()))
DATA_FIELDS = FIELDS[1:]


@dataclass(repr=False)
class DuplicateMatch:
    student: Student
    reason: str  # snils — точное совпадение номера; name_birth_date — возможный дубль.


class DuplicateStudentError(Exception):
    def __init__(self, matches: list[DuplicateMatch]):
        super().__init__("Найдены совпадающие карточки. Требуется проверка сотрудником.")
        self.matches = matches


class StudentRepositoryError(Exception):
    """Безопасное сообщение: тексты SQLite и содержимое записи не раскрываются."""


class StudentRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.create_function("student_casefold", 1, lambda text: text.casefold(), deterministic=True)
        connection.create_function("student_digits", 1, lambda text: re.sub(r"[^0-9]", "", text), deterministic=True)

    def _select(self, condition="1", parameters=()) -> list[Student]:
        try:
            rows = self.connection.execute(
                f"SELECT {', '.join(FIELDS)} FROM students WHERE {condition} "  # nosec B608 # field names are fixed constants; values are bound parameters.
                "ORDER BY student_casefold(surname), student_casefold(first_name), id", parameters,
            ).fetchall()
            return [Student(**dict(zip(FIELDS, row))) for row in rows]
        except sqlite3.Error:
            raise StudentRepositoryError("Не удалось прочитать реестр слушателей. Проверьте доступность базы.") from None

    def load(self, student_id: int) -> Student:
        rows = self._select("id = ?", (student_id,))
        if not rows:
            raise StudentRepositoryError("Карточка слушателя не найдена.")
        return rows[0]

    def search(self, query: str = "") -> list[Student]:
        query = " ".join(query.split()).casefold()
        if not query:
            return self._select()
        digits = re.sub(r"[^0-9]", "", query) if re.fullmatch(r"[+0-9()\s-]+", query) else ""
        return self._select(
            "instr(student_casefold(surname || ' ' || first_name || ' ' || patronymic), ?) > 0 "
            "OR instr(student_casefold(phone), ?) > 0 "
            "OR (? != '' AND (instr(student_digits(phone), ?) > 0 OR instr(snils, ?) > 0))",
            (query, query, digits, digits, digits),
        )

    def find_duplicates(self, student: Student) -> list[DuplicateMatch]:
        # Вызывается после нормализации; текущая карточка исключается при исправлении.
        rows = self._select(
            "id != ? AND ((? != '' AND snils = ?) OR "
            "(student_casefold(surname) = ? AND student_casefold(first_name) = ? "
            "AND student_casefold(patronymic) = ? AND birth_date = ?))",
            (student.id or 0, student.snils, student.snils, student.surname.casefold(),
             student.first_name.casefold(), student.patronymic.casefold(), student.birth_date),
        )
        return [DuplicateMatch(row, "snils" if student.snils and row.snils == student.snils else "name_birth_date") for row in rows]

    def save(self, student: Student, *, allow_duplicates=False, today: date | None = None) -> Student:
        student = validate_student(student, today=today)
        if self.connection.in_transaction:
            raise StudentRepositoryError("Завершите текущую операцию с базой перед сохранением.")
        try:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                matches = self.find_duplicates(student)
                if matches and not allow_duplicates:
                    raise DuplicateStudentError(matches)
                values = asdict(student)
                parameters = tuple(values[field] for field in DATA_FIELDS)
                if student.id is None:
                    cursor = self.connection.execute(
                        f"INSERT INTO students ({', '.join(DATA_FIELDS)}) VALUES ({', '.join('?' for _ in DATA_FIELDS)})",  # nosec B608 # field names are fixed constants; values are bound parameters.
                        parameters,
                    )
                    student.id = cursor.lastrowid
                else:
                    cursor = self.connection.execute(
                        f"UPDATE students SET {', '.join(field + ' = ?' for field in DATA_FIELDS)} WHERE id = ?",  # nosec B608 # field names are fixed constants; values are bound parameters.
                        (*parameters, student.id),
                    )
                    if cursor.rowcount != 1:
                        raise StudentRepositoryError("Карточка слушателя не найдена; изменения не сохранены.")
            return student
        except sqlite3.Error:
            raise StudentRepositoryError("Не удалось сохранить карточку. Изменения не применены. Проверьте доступность базы.") from None
