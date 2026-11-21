"""Модель слушателя и проверки, независимые от интерфейса и SQLite."""

from dataclasses import asdict, dataclass, replace
from datetime import date
import re


@dataclass(repr=False)
class Student:
    id: int | None = None
    surname: str = ""
    first_name: str = ""
    patronymic: str = ""
    birth_date: str = ""
    snils: str = ""
    citizenship: str = ""
    sex: str = ""
    citizenship_code: str = ""
    phone: str = ""
    email: str = ""
    registration_address: str = ""
    residential_address: str = ""
    passport_series: str = ""
    passport_number: str = ""
    passport_issued_by: str = ""
    passport_issue_date: str = ""
    passport_department_code: str = ""
    education_level: str = ""
    education_document: str = ""
    education_details: str = ""
    workplace: str = ""
    job_title: str = ""
    note: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.surname, self.first_name, self.patronymic) if part)


def iso_date(value: str, caption: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise ValueError(f"{caption}: укажите существующую дату в формате ГГГГ-ММ-ДД.") from None


def is_adult(birth_date: date, on_date: date) -> bool:
    return on_date.year - birth_date.year - ((on_date.month, on_date.day) < (birth_date.month, birth_date.day)) >= 18


def normalize_snils(value: str) -> str:
    if not value:
        return ""
    if not re.fullmatch(r"[0-9\s-]+", value):
        raise ValueError("СНИЛС: допустимы только 11 цифр, пробелы и дефисы.")
    digits = re.sub(r"[\s-]", "", value)
    if len(digits) != 11 or int(digits[:9]) == 0:
        raise ValueError("СНИЛС: укажите 11 цифр корректного номера.")
    # Для старых номеров до 001-001-998 включительно проверяется только формат.
    if int(digits[:9]) > 1001998:
        total = sum(int(digit) * weight for digit, weight in zip(digits[:9], range(9, 0, -1)))
        control = total % 101
        if control == 100:
            control = 0
        if int(digits[-2:]) != control:
            raise ValueError("СНИЛС: неверная контрольная сумма.")
    return digits


def validate_student(student: Student, *, today: date | None = None) -> Student:
    today = today or date.today()
    values = asdict(student)
    for field, value in values.items():
        if field != "id":
            values[field] = " ".join(value.split()) if field != "note" else "\n".join(
                " ".join(line.split()) for line in value.strip().splitlines()
            )
    student = Student(**values)
    if not student.surname or not student.first_name or not student.birth_date:
        raise ValueError("Заполните фамилию, имя и дату рождения.")
    born = iso_date(student.birth_date, "Дата рождения")
    if born > today:
        raise ValueError("Дата рождения не может быть в будущем.")
    if not is_adult(born, today):
        raise ValueError("Создать карточку можно только для слушателя 18 лет и старше.")
    student = replace(student, snils=normalize_snils(student.snils))
    if student.email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", student.email):
        raise ValueError("Проверьте формат электронной почты.")
    series = student.passport_series.replace(" ", "")
    if series and not re.fullmatch(r"[0-9]{4}", series):
        raise ValueError("Серия российского паспорта должна содержать 4 цифры.")
    if student.passport_number and not re.fullmatch(r"[0-9]{6}", student.passport_number):
        raise ValueError("Номер российского паспорта должен содержать 6 цифр.")
    code = student.passport_department_code
    if code and not re.fullmatch(r"[0-9]{3}-?[0-9]{3}", code):
        raise ValueError("Код подразделения укажите в формате 000-000.")
    if student.passport_issue_date:
        issued = iso_date(student.passport_issue_date, "Дата выдачи паспорта")
        if not born <= issued <= today:
            raise ValueError("Дата выдачи паспорта должна быть между датой рождения и текущей датой.")
    return replace(student, passport_series=series, passport_department_code=(code[:3] + "-" + code[-3:]) if code else "")
