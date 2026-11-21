"""Транзакции подготовки обучения, снимки и компактные представления реестров."""

from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import sqlite3

from proficentr.program_repository import ProgramRepository, ProgramRepositoryError
from proficentr.students import is_adult, iso_date
from proficentr.trainings import (
    COMPLETED, ENROLLED, EXCLUDED, PREPARATION, Training, TrainingGroup, validate_group, validate_training,
)

GROUP_FIELDS = tuple(asdict(TrainingGroup()))
TRAINING_FIELDS = tuple(asdict(Training()))
SNAPSHOT_FIELDS = ("full_name", "direction", "kind", "hours", "code", "edition", "qualification", "grade", "dpo_area", "dpo_group", "po_profession")


class TrainingRepositoryError(Exception):
    """Сообщение без персональных данных и внутренних ошибок SQLite."""


class DuplicateTrainingError(ValueError):
    pass


class OverlappingTrainingError(ValueError):
    pass


class ProgramChangeRequired(ValueError):
    pass


class TrainingRepository:
    def __init__(self, connection):
        self.connection = connection
        self.programs = ProgramRepository(connection)
        self.has_enrollments = bool(connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'training_enrollments'").fetchone())
        self.has_completions = bool(connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'training_completions'").fetchone())
        connection.create_function("training_fold", 1, lambda value: " ".join(value.split()).casefold(), deterministic=True)

    @contextmanager
    def transaction(self):
        if self.connection.in_transaction:
            raise TrainingRepositoryError("Завершите текущую операцию с базой.")
        try:
            with self.connection:
                self.connection.execute("BEGIN IMMEDIATE")
                yield
        except (sqlite3.Error, ProgramRepositoryError):
            raise TrainingRepositoryError("Операция с обучением не выполнена. Изменения не сохранены; проверьте доступность базы и связанных записей.") from None

    def _load(self, table, fields, model, record_id):
        try:
            row = self.connection.execute(
                f"SELECT {', '.join(fields)} FROM {table} WHERE id = ?", (record_id,),  # nosec B608 # Internal fixed table/fields, bound ID.
            ).fetchone()
        except sqlite3.Error:
            raise TrainingRepositoryError("Не удалось прочитать запись обучения.") from None
        if row is None:
            raise TrainingRepositoryError("Запись не найдена.")
        values = dict(zip(fields, row))
        if "active" in values:
            values["active"] = bool(values["active"])
        return model(**values)

    def load_group(self, group_id):
        return self._load("training_groups", GROUP_FIELDS, TrainingGroup, group_id)

    def load(self, training_id):
        record = self._load("trainings", TRAINING_FIELDS, Training, training_id)
        if self.has_enrollments and self.connection.execute("SELECT 1 FROM training_enrollments WHERE training_id = ?", (training_id,)).fetchone():
            record = replace(record, state=ENROLLED)
        if self.has_completions and self.connection.execute("SELECT 1 FROM training_completions WHERE training_id = ?", (training_id,)).fetchone():
            record = replace(record, state=COMPLETED)
        return record

    def _write(self, table, fields, record):
        values = asdict(record)
        data_fields = fields[1:]
        if record.id is None:
            cursor = self.connection.execute(
                f"INSERT INTO {table} ({', '.join(data_fields)}) VALUES ({', '.join('?' for _ in data_fields)})",  # nosec B608 # Internal constants, bound values.
                tuple(values[key] for key in data_fields),
            )
            return cursor.lastrowid
        cursor = self.connection.execute(
            f"UPDATE {table} SET {', '.join(key + ' = ?' for key in data_fields)} WHERE id = ?",  # nosec B608 # Internal constants, bound values.
            (*[values[key] for key in data_fields], record.id),
        )
        if cursor.rowcount != 1:
            raise TrainingRepositoryError("Запись не найдена; изменения не сохранены.")
        return record.id

    def _snapshot(self, program_id, *, require_active):
        program = self.programs.load(program_id)
        if require_active and not program.active:
            raise ValueError("Для нового выбора нужна активная программа.")
        return json.dumps({key: getattr(program, key) for key in SNAPSHOT_FIELDS
                           if key not in {"dpo_area", "dpo_group", "po_profession"} or getattr(program, key)}, ensure_ascii=False)

    def save_group(self, group, *, change_program=False, allow_overlap=False):
        group = validate_group(group)
        with self.transaction():
            old = self.load_group(group.id) if group.id else None
            changed = old is not None and old.program_id != group.program_id
            snapshot = self._snapshot(group.program_id, require_active=old is None or changed)
            if changed:
                if self.has_enrollments and self.connection.execute("SELECT 1 FROM training_enrollments e JOIN trainings t ON t.id = e.training_id WHERE t.group_id = ?", (group.id,)).fetchone():
                    raise ValueError("Есть зачисленные записи: изменение программы группы запрещено.")
                members = self.connection.execute("SELECT id, state FROM trainings WHERE group_id = ?", (group.id,)).fetchall()
                if members and not change_program:
                    raise ProgramChangeRequired("Программа группы изменена. Явно подтвердите обновление программы и снимков всех подготовительных записей состава.")
                if any(state not in (PREPARATION, EXCLUDED) for _, state in members):
                    raise ValueError("Есть оформленные записи: изменение программы группы запрещено.")
                self.connection.execute("UPDATE trainings SET program_id = ?, program_snapshot = ? WHERE group_id = ?",
                                        (group.program_id, snapshot, group.id))
                if not allow_overlap and self.connection.execute(
                    "SELECT 1 FROM trainings a JOIN trainings b ON a.student_id = b.student_id "
                    "AND a.program_id = b.program_id AND a.id != b.id "
                    "AND a.start_date <= b.end_date AND a.end_date >= b.start_date "
                    "WHERE a.group_id = ? AND a.state = 'preparation' AND b.state = 'preparation' LIMIT 1",
                    (group.id,),
                ).fetchone():
                    raise OverlappingTrainingError("После изменения программы есть пересекающиеся обучения по той же программе. Сохранить изменение группы?")
            record_id = self._write("training_groups", GROUP_FIELDS, group)
        return self.load_group(record_id)

    def set_active(self, group_id, active):
        if type(active) is not bool:
            raise ValueError("Выберите состояние группы.")
        with self.transaction():
            self.load_group(group_id)
            self.connection.execute("UPDATE training_groups SET active = ? WHERE id = ?", (active, group_id))
        return self.load_group(group_id)

    def save(self, training, *, refresh_snapshot=False, allow_overlap=False):
        with self.transaction():
            record_id = self._save(training, refresh_snapshot=refresh_snapshot, allow_overlap=allow_overlap)
        return self.load(record_id)

    def add_students(self, group_id, student_ids, *, allow_overlap=False):
        if not student_ids or len(set(student_ids)) != len(student_ids):
            raise ValueError("Выберите разных слушателей.")
        with self.transaction():
            record_ids = [self._save(Training(student_id=student_id, group_id=group_id),
                                     allow_overlap=allow_overlap) for student_id in student_ids]
        return [self.load(record_id) for record_id in record_ids]

    def _save(self, training, *, refresh_snapshot=False, allow_overlap=False):
        old = self.load(training.id) if training.id else None
        if old and old.state != PREPARATION:
            raise ValueError("Изменять можно только подготовительные записи.")
        if old and (training.student_id != old.student_id or training.group_id != old.group_id):
            raise ValueError("Связи существующего обучения со слушателем и группой менять нельзя.")
        if training.group_id is not None:
            group = self.load_group(training.group_id)
            if not group.active:
                raise ValueError("Сначала восстановите группу из архива.")
            training = replace(training, program_id=training.program_id or group.program_id,
                               start_date=training.start_date or group.start_date, end_date=training.end_date or group.end_date)
            if training.program_id != group.program_id:
                raise ValueError("Программа обучения должна совпадать с программой группы.")
        training = validate_training(training)
        born = self.connection.execute("SELECT birth_date FROM students WHERE id = ?", (training.student_id,)).fetchone()
        if born is None:
            raise ValueError("Выберите существующего слушателя.")
        if not is_adult(iso_date(born[0], "Дата рождения"), iso_date(training.start_date, "Дата начала")):
            raise ValueError("На дату начала обучения слушателю должно исполниться 18 лет.")
        changed = old is not None and training.program_id != old.program_id
        if changed and not refresh_snapshot:
            raise ProgramChangeRequired("Подтвердите изменение программы и обновление её снимка.")
        snapshot = self._snapshot(training.program_id, require_active=old is None or changed) if old is None or changed or refresh_snapshot else old.program_snapshot
        if training.group_id is not None and self.connection.execute(
            "SELECT 1 FROM trainings WHERE group_id = ? AND student_id = ? AND state = 'preparation' AND id != ?",
            (training.group_id, training.student_id, training.id or 0),
        ).fetchone():
            raise DuplicateTrainingError("Слушатель уже добавлен в эту группу.")
        if not allow_overlap and self.connection.execute(
            "SELECT 1 FROM trainings WHERE student_id = ? AND program_id = ? AND state = 'preparation' "
            "AND start_date <= ? AND end_date >= ? AND id != ?",
            (training.student_id, training.program_id, training.end_date, training.start_date, training.id or 0),
        ).fetchone():
            raise OverlappingTrainingError("Есть обучение этого слушателя по той же программе с пересекающимися датами. Сохранить отдельную запись?")
        if old:
            created_at = old.created_at
        else:
            created_at = self.connection.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        training = replace(training, created_at=created_at, program_snapshot=snapshot)
        record_id = self._write("trainings", TRAINING_FIELDS, training)
        return record_id

    def exclude(self, training_id):
        with self.transaction():
            training = self.load(training_id)
            if training.state != PREPARATION or training.group_id is None:
                raise ValueError("Исключать можно только подготовительную запись состава группы.")
            if not self.load_group(training.group_id).active:
                raise ValueError("Сначала восстановите группу из архива.")
            self.connection.execute("UPDATE trainings SET state = ? WHERE id = ?", (EXCLUDED, training_id))
        return self.load(training_id)

    def search_groups(self, query="", active=True):
        try:
            rows = self.connection.execute(
                "SELECT g.id, g.name, p.full_name, g.start_date, g.end_date, g.active "
                "FROM training_groups g JOIN programs p ON p.id = g.program_id "
                "WHERE (? IS NULL OR g.active = ?) AND (instr(training_fold(g.name), ?) > 0 "
                "OR instr(training_fold(p.full_name), ?) > 0) ORDER BY training_fold(g.name), g.id",
                (active, active, " ".join(query.split()).casefold(), " ".join(query.split()).casefold()),
            ).fetchall()
            return rows
        except sqlite3.Error:
            raise TrainingRepositoryError("Не удалось прочитать реестр групп.") from None

    def list_trainings(self, *, group_id=None, query="", include_excluded=False):
        try:
            # Only display fields: passports, SNILS and full student cards are not loaded.
            return self.connection.execute(
                "SELECT t.id, s.surname || ' ' || s.first_name || ' ' || s.patronymic, "
                "t.program_snapshot, t.start_date, t.end_date, "  # nosec B608 # Fixed state projections; bound parameters.
                + ("CASE WHEN EXISTS (SELECT 1 FROM training_completions c WHERE c.training_id = t.id) THEN 'completed' WHEN EXISTS (SELECT 1 FROM training_enrollments e WHERE e.training_id = t.id) THEN 'enrolled' ELSE t.state END " if self.has_completions else
                   "CASE WHEN EXISTS (SELECT 1 FROM training_enrollments e WHERE e.training_id = t.id) THEN 'enrolled' ELSE t.state END " if self.has_enrollments else "t.state ")
                +
                "FROM trainings t JOIN students s ON s.id = t.student_id "
                "WHERE ((? IS NULL AND t.group_id IS NULL) OR t.group_id = ?) "
                "AND (? OR t.state = 'preparation') "
                "AND instr(training_fold(s.surname || ' ' || s.first_name || ' ' || s.patronymic), ?) > 0 "
                "ORDER BY training_fold(s.surname), s.id, t.id",
                (group_id, group_id, include_excluded, " ".join(query.split()).casefold()),
            ).fetchall()
        except sqlite3.Error:
            raise TrainingRepositoryError("Не удалось прочитать список обучений.") from None

    def search_students(self, query, limit=100):
        query = " ".join(query.split()).casefold()
        if not query:
            return []
        try:
            return self.connection.execute(
                "SELECT id, surname || ' ' || first_name || ' ' || patronymic, birth_date FROM students "
                "WHERE instr(training_fold(surname || ' ' || first_name || ' ' || patronymic), ?) > 0 "
                "ORDER BY training_fold(surname), id LIMIT ?", (query, limit),
            ).fetchall()
        except sqlite3.Error:
            raise TrainingRepositoryError("Не удалось найти слушателей.") from None

    def student_label(self, student_id):
        try:
            row = self.connection.execute(
                "SELECT surname || ' ' || first_name || ' ' || patronymic FROM students WHERE id = ?", (student_id,),
            ).fetchone()
            if not row:
                raise TrainingRepositoryError("Слушатель не найден.")
            return f"{row[0]} · ID {student_id}"
        except sqlite3.Error:
            raise TrainingRepositoryError("Не удалось прочитать ФИО слушателя.") from None
