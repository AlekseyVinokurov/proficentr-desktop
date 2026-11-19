"""Реестр контрагентов; не связан с единственной карточкой учебного центра."""

from dataclasses import asdict, dataclass
import re
import sqlite3

from proficentr.counterparties import Counterparty, validate_counterparty

FIELDS = tuple(asdict(Counterparty()))
DATA_FIELDS = FIELDS[1:]


@dataclass(repr=False)
class DuplicateMatch:
    counterparty: Counterparty
    reason: str  # inn — совпадение номера; name — возможный дубль по наименованию.


class DuplicateCounterpartyError(Exception):
    def __init__(self, matches: list[DuplicateMatch]):
        super().__init__("Найдены совпадающие карточки контрагентов. Требуется проверка сотрудником.")
        self.matches = matches


class CounterpartyRepositoryError(Exception):
    """Диагностика без содержимого карточек и исходных сообщений SQLite."""


class CounterpartyRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.create_function("counterparty_casefold", 1, lambda text: " ".join(text.split()).casefold(), deterministic=True)
        connection.create_function("counterparty_digits", 1, lambda text: re.sub(r"[^0-9]", "", text), deterministic=True)

    def _select(self, condition="1", parameters=()) -> list[Counterparty]:
        try:
            rows = self.connection.execute(
                f"SELECT {', '.join(FIELDS)} FROM counterparties WHERE {condition} "  # nosec B608 # Fixed fields and internal conditions; values are bound parameters.
                "ORDER BY counterparty_casefold(full_name), id", parameters,
            ).fetchall()
            return [Counterparty(**dict(zip(FIELDS, row))) for row in rows]
        except sqlite3.Error:
            raise CounterpartyRepositoryError("Не удалось прочитать реестр организаций. Проверьте доступность базы.") from None

    def load(self, counterparty_id: int) -> Counterparty:
        rows = self._select("id = ?", (counterparty_id,))
        if not rows:
            raise CounterpartyRepositoryError("Карточка организации не найдена.")
        return rows[0]

    def search(self, query: str = "") -> list[Counterparty]:
        query = " ".join(query.split()).casefold()
        if not query:
            return self._select()
        digits = re.sub(r"[^0-9]", "", query) if re.fullmatch(r"[+0-9()\s-]+", query) else ""
        return self._select(
            "instr(counterparty_casefold(full_name), ?) > 0 OR instr(counterparty_casefold(short_name), ?) > 0 "
            "OR instr(inn, ?) > 0 OR instr(counterparty_casefold(phone), ?) > 0 "
            "OR (? != '' AND (instr(inn, ?) > 0 OR instr(counterparty_digits(phone), ?) > 0))",
            (query, query, query, query, digits, digits, digits),
        )

    def find_duplicates(self, counterparty: Counterparty) -> list[DuplicateMatch]:
        # Called with normalized values. Name checks include both full and short names.
        rows = self._select(
            "id != ? AND ((? != '' AND inn = ?) OR counterparty_casefold(full_name) = ? "
            "OR (? != '' AND counterparty_casefold(short_name) = ?) "
            "OR (? != '' AND counterparty_casefold(full_name) = ?) "
            "OR (short_name != '' AND counterparty_casefold(short_name) = ?))",
            (counterparty.id or 0, counterparty.inn, counterparty.inn, counterparty.full_name.casefold(),
             counterparty.short_name, counterparty.short_name.casefold(),
             counterparty.short_name, counterparty.short_name.casefold(), counterparty.full_name.casefold()),
        )
        return [DuplicateMatch(row, "inn" if counterparty.inn and row.inn == counterparty.inn else "name") for row in rows]

    def save(self, counterparty: Counterparty, *, allow_duplicates=False) -> Counterparty:
        counterparty = validate_counterparty(counterparty)
        if self.connection.in_transaction:
            raise CounterpartyRepositoryError("Завершите текущую операцию с базой перед сохранением.")
        try:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                matches = self.find_duplicates(counterparty)
                if matches and not allow_duplicates:
                    raise DuplicateCounterpartyError(matches)
                values = asdict(counterparty)
                parameters = tuple(values[field] for field in DATA_FIELDS)
                if counterparty.id is None:
                    cursor = self.connection.execute(
                        f"INSERT INTO counterparties ({', '.join(DATA_FIELDS)}) VALUES ({', '.join('?' for _ in DATA_FIELDS)})",  # nosec B608 # Fixed field names; bound values.
                        parameters,
                    )
                    counterparty.id = cursor.lastrowid
                else:
                    cursor = self.connection.execute(
                        f"UPDATE counterparties SET {', '.join(field + ' = ?' for field in DATA_FIELDS)} WHERE id = ?",  # nosec B608 # Fixed field names; bound values.
                        (*parameters, counterparty.id),
                    )
                    if cursor.rowcount != 1:
                        raise CounterpartyRepositoryError("Карточка организации не найдена; изменения не сохранены.")
            return counterparty
        except sqlite3.Error:
            raise CounterpartyRepositoryError("Не удалось сохранить карточку организации. Изменения не применены. Проверьте доступность базы.") from None
