"""Модель договора и точные суммы в копейках, без юридического зачисления."""

from dataclasses import dataclass, field, replace
import re

from proficentr.students import iso_date

CUSTOMER_TYPES = {'student': 'Сам слушатель', 'person': 'Другое физическое лицо',
                  'legal_entity': 'Юридическое лицо', 'sole_proprietor': 'ИП'}
STATES = {'draft': 'Черновик', 'registered': 'Зарегистрирован', 'annulled': 'Аннулирован'}
PERSON_FIELDS = ('full_name', 'birth_date', 'registration_address', 'passport_series', 'passport_number',
                 'passport_issued_by', 'passport_issue_date', 'passport_department_code', 'phone', 'email')
MAX_CENTS = 2**63 - 1


class ContractError(ValueError):
    """Безопасная диагностика без реквизитов сторон и внутренних ошибок SQLite."""


@dataclass(repr=False)
class Contract:
    id: int | None = None
    customer_type: str = 'student'
    counterparty_id: int | None = None
    student_id: int | None = None
    person_data: dict = field(default_factory=dict)
    contract_date: str = ''
    historical_number: str = ''
    total_cents: int = 0
    payment_terms: str = ''
    note: str = ''
    state: str = 'draft'
    number: str | None = None
    number_key: str | None = None
    created_at: str = ''
    registered_at: str = ''
    template_id: int | None = None
    document_path: str = ''
    document_sha256: str = ''
    snapshot: str = '{}'
    annul_reason: str = ''
    annulled_at: str = ''
    revision: int = 0


def price_from_text(value: str) -> int:
    if type(value) is not str or not re.fullmatch(r'[0-9]+(?:[.,][0-9]{1,2})?', value.strip()):
        raise ContractError('Укажите сумму в рублях, не более двух знаков после запятой.')
    parts = re.split('[.,]', value.strip())
    if len(parts[0]) > 17:
        raise ContractError('Сумма слишком велика.')
    cents = int(parts[0]) * 100 + int(parts[1].ljust(2, '0') if len(parts) == 2 else '0')
    if cents > MAX_CENTS:
        raise ContractError('Сумма слишком велика.')
    return cents


def format_price(cents: int) -> str:
    if type(cents) is not int or not 0 <= cents <= MAX_CENTS:
        raise ContractError('Стоимость должна быть целым неотрицательным числом копеек.')
    return f'{cents // 100},{cents % 100:02d}'


def number_key(number: str) -> str:
    return ' '.join(number.split()).casefold()


def validate_contract(contract: Contract) -> Contract:
    if contract.customer_type not in CUSTOMER_TYPES:
        raise ContractError('Выберите тип заказчика.')
    iso_date(contract.contract_date, 'Дата договора')
    format_price(contract.total_cents)
    if type(contract.person_data) is not dict or any(key not in PERSON_FIELDS or type(value) is not str for key, value in contract.person_data.items()):
        raise ContractError('Некорректные реквизиты заказчика-физлица.')
    person = {key: value.strip() for key, value in contract.person_data.items()} if contract.customer_type == 'person' else {}
    for key in ('birth_date', 'passport_issue_date'):
        if person.get(key):
            iso_date(person[key], 'Дата заказчика-физлица')
    for key, pattern in (('passport_series', r'[0-9]{4}'), ('passport_number', r'[0-9]{6}'),
                         ('passport_department_code', r'[0-9]{3}-?[0-9]{3}'), ('email', r'[^\s@]+@[^\s@]+\.[^\s@]+')):
        if person.get(key) and not re.fullmatch(pattern, person[key]):
            raise ContractError('Проверьте формат паспорта, кода подразделения и email заказчика-физлица.')
    historical = contract.historical_number.strip()
    if len(historical) > 100 or any(ord(char) < 32 for char in historical):
        raise ContractError('Исходный номер: до 100 знаков, без управляющих символов.')
    # Remove irrelevant references when the customer type changes in a draft.
    return replace(contract, person_data=person if contract.customer_type == 'person' else {},
                   counterparty_id=contract.counterparty_id if contract.customer_type in ('legal_entity', 'sole_proprietor') else None,
                   student_id=contract.student_id if contract.customer_type == 'student' else None,
                   historical_number=historical, payment_terms=contract.payment_terms.strip(), note=contract.note.strip())
