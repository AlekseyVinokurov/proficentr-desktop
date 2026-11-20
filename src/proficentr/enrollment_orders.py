"""Приказ и отдельное ручное подтверждение подписания."""
from dataclasses import dataclass

STATES = {'draft': 'Черновик', 'registered': 'Зарегистрирован', 'signed': 'Подтверждено подписание'}
KINDS = {'individual': 'Индивидуальный', 'group': 'Групповой'}


class OrderError(ValueError):
    pass


@dataclass(repr=False)
class EnrollmentOrder:
    id: int | None = None
    order_date: str = ''
    kind: str = 'individual'
    group_id: int | None = None
    historical_number: str = ''
    note: str = ''
    template_id: int | None = None
    state: str = 'draft'
    number: str | None = None
    number_key: str | None = None
    created_at: str = ''
    registered_at: str = ''
    document_path: str = ''
    document_sha256: str = ''
    snapshot: str = '{}'
    signed_date: str = ''
    confirmed_at: str = ''
    confirmed_by: str = ''
    grounds_checked: int = 0
    education_checked: int = 0
    revision: int = 1
