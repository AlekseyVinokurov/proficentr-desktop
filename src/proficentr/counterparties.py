"""Контрагенты учебного центра: модель и локальная проверка формата реквизитов."""

from dataclasses import asdict, dataclass
import re

KINDS = {"legal_entity": "Юридическое лицо", "sole_proprietor": "Индивидуальный предприниматель"}


@dataclass(repr=False)
class Counterparty:
    id: int | None = None
    kind: str = "legal_entity"
    full_name: str = ""
    short_name: str = ""
    inn: str = ""
    kpp: str = ""
    ogrn: str = ""
    legal_address: str = ""
    actual_address: str = ""
    postal_address: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    contact_name: str = ""
    contact_position: str = ""
    bank_name: str = ""
    bik: str = ""
    settlement_account: str = ""
    correspondent_account: str = ""
    representative_name: str = ""
    representative_position: str = ""
    authority_basis: str = ""
    power_of_attorney: str = ""
    note: str = ""


def validate_counterparty(counterparty: Counterparty) -> Counterparty:
    values = asdict(counterparty)
    for field, value in values.items():
        if field != "id":
            values[field] = " ".join(value.split()) if field != "note" else "\n".join(
                " ".join(line.split()) for line in value.strip().splitlines()
            )
    counterparty = Counterparty(**values)
    if counterparty.kind not in KINDS:
        raise ValueError("Выберите тип: юридическое лицо или индивидуальный предприниматель.")
    if not counterparty.full_name:
        raise ValueError("Заполните полное наименование контрагента.")
    is_ip = counterparty.kind == "sole_proprietor"
    if is_ip and counterparty.kpp:
        raise ValueError("Для ИП КПП не применяется. Очистите поле КПП.")
    rules = {
        "inn": (r"[0-9]{12}" if is_ip else r"[0-9]{10}",
                "ИНН ИП: укажите 12 цифр." if is_ip else "ИНН юридического лица: укажите 10 цифр."),
        "kpp": (r"[0-9]{4}[0-9A-Za-z]{2}[0-9]{3}", "КПП: укажите 9 знаков (в позициях 5–6 допустимы латинские буквы)."),
        "ogrn": (r"[0-9]{15}" if is_ip else r"[0-9]{13}",
                 "ОГРНИП: укажите 15 цифр." if is_ip else "ОГРН: укажите 13 цифр."),
        "bik": (r"[0-9]{9}", "БИК: укажите 9 цифр."),
        "settlement_account": (r"[0-9]{20}", "Расчётный счёт: укажите 20 цифр."),
        "correspondent_account": (r"[0-9]{20}", "Корреспондентский счёт: укажите 20 цифр."),
        "email": (r"[^\s@]+@[^\s@]+\.[^\s@]+", "Проверьте формат электронной почты."),
    }
    for field, (pattern, message) in rules.items():
        value = getattr(counterparty, field)
        if value and not re.fullmatch(pattern, value):
            raise ValueError(message)
    return counterparty
