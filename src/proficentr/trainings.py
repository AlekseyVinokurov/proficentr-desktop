"""Подготовительные обучения и группы; без юридического зачисления."""

from dataclasses import dataclass, replace

from proficentr.students import iso_date

PREPARATION = "preparation"
EXCLUDED = "excluded"
STATES = {PREPARATION: "Подготовка", EXCLUDED: "Исключён из состава (подготовка)"}


@dataclass(repr=False)
class TrainingGroup:
    id: int | None = None
    name: str = ""
    program_id: int | None = None
    start_date: str = ""
    end_date: str = ""
    note: str = ""
    active: bool = True


@dataclass(repr=False)
class Training:
    id: int | None = None
    student_id: int | None = None
    program_id: int | None = None
    group_id: int | None = None
    start_date: str = ""
    end_date: str = ""
    created_at: str = ""
    state: str = PREPARATION
    note: str = ""
    program_snapshot: str = ""


def validate_dates(start: str, end: str) -> None:
    if iso_date(end, "Дата окончания") < iso_date(start, "Дата начала"):
        raise ValueError("Дата окончания не может предшествовать дате начала.")


def validate_group(group: TrainingGroup) -> TrainingGroup:
    group = replace(group, name=" ".join(group.name.split()), note=group.note.strip())
    if not group.name:
        raise ValueError("Укажите обозначение группы.")
    if not group.program_id:
        raise ValueError("Выберите существующую программу.")
    if type(group.active) is not bool:
        raise ValueError("Выберите состояние группы.")
    validate_dates(group.start_date, group.end_date)
    return group


def validate_training(training: Training) -> Training:
    if not training.student_id or not training.program_id:
        raise ValueError("Выберите существующих слушателя и программу.")
    if training.state != PREPARATION:
        raise ValueError("Редактирование допустимо только в состоянии «Подготовка».")
    validate_dates(training.start_date, training.end_date)
    return replace(training, note=training.note.strip())
