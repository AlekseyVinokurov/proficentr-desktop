"""Образовательные программы: структурированные виды, без правил зачисления и выдачи документов."""

from dataclasses import asdict, dataclass
from datetime import date
import re

DIRECTIONS = {"dpo": "ДПО", "po": "ПО"}
KINDS = {
    "dpo": {
        "dpo_upskilling": "Повышение квалификации",
        "dpo_retraining": "Профессиональная переподготовка",
    },
    "po": {
        "po_initial_training": "Профессиональная подготовка по профессиям рабочих и должностям служащих",
        "po_retraining": "Переподготовка рабочих и служащих",
        "po_upskilling": "Повышение квалификации рабочих и служащих",
    },
}


@dataclass(repr=False)
class Program:
    id: int | None = None
    full_name: str = ""
    short_name: str = ""
    code: str = ""
    direction: str = ""
    kind: str = ""
    hours: int = 0
    qualification: str = ""
    grade: str = ""
    approval_date: str = ""
    approval_number: str = ""
    approval_details: str = ""
    edition: str = ""
    note: str = ""
    active: bool = True


def hours_from_text(value: str) -> int:
    value = value.strip()
    try:
        if not re.fullmatch(r"[0-9]+", value):
            raise ValueError
        hours = int(value)
        if hours <= 0:
            raise ValueError
        return hours
    except ValueError:
        raise ValueError("Продолжительность: укажите положительное целое число академических часов.") from None


def validate_program(program: Program) -> Program:
    values = asdict(program)
    for field, value in values.items():
        if field not in {"id", "hours", "active"}:
            values[field] = " ".join(value.split()) if field != "note" else "\n".join(
                " ".join(line.split()) for line in value.strip().splitlines()
            )
    program = Program(**values)
    if not program.full_name:
        raise ValueError("Заполните полное наименование программы.")
    if program.direction not in DIRECTIONS:
        raise ValueError("Выберите направление: ДПО или ПО.")
    if program.kind not in KINDS[program.direction]:
        raise ValueError("Выберите вид программы, соответствующий направлению ДПО или ПО.")
    if type(program.hours) is not int or program.hours <= 0:
        raise ValueError("Продолжительность: укажите положительное целое число академических часов.")
    if type(program.active) is not bool:
        raise ValueError("Выберите статус программы: активная или архивная.")
    if program.approval_date:
        try:
            if date.fromisoformat(program.approval_date).isoformat() != program.approval_date:
                raise ValueError
        except ValueError:
            raise ValueError("Дата утверждения: укажите существующую дату в формате ГГГГ-ММ-ДД.") from None
    return program
