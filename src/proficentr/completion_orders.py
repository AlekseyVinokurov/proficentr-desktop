"""Приказ о завершении и отдельные факты успешного завершения обучений."""
from dataclasses import dataclass

STATES = {'draft': 'Черновик', 'registered': 'Зарегистрирован', 'signed': 'Подписание подтверждено'}
KINDS = {'individual': 'Индивидуальный', 'group': 'Групповой'}


class CompletionError(ValueError):
    pass


@dataclass(repr=False)
class CompletionOrder:
    id: int | None = None
    order_date: str = ''
    kind: str = 'individual'
    group_id: int | None = None
    direction: str = 'dpo'
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
    conditions_checked: int = 0
    revision: int = 1


@dataclass(repr=False)
class CompletionEntry:
    training_id: int
    completion_date: str
    protocol_id: int | None = None
    basis_snapshot: str = '{}'
