"""Выпускной документ и отдельные события передачи."""
from dataclasses import dataclass

KINDS = {
    'dpo_upskilling_certificate': 'Удостоверение о повышении квалификации',
    'dpo_retraining_diploma': 'Диплом о профессиональной переподготовке',
    'po_profession_certificate': 'Свидетельство о профессии рабочего, должности служащего',
}
PREFIXES = dict(zip(KINDS, ('У','Д','С')))
STATES = {'draft':'Черновик','registered':'Зарегистрирован'}
METHODS = {'personal':'Лично (оригинал)','post':'Почта России (отправка оригинала)',
           'post_confirmation':'Почта России (подтверждение вручения)','email':'Email (электронная копия)'}


class IssuanceError(ValueError):
    pass


def document_kind(direction, program_kind):
    if direction == 'dpo' and program_kind in ('dpo_upskilling','dpo_retraining'):
        return 'dpo_upskilling_certificate' if program_kind == 'dpo_upskilling' else 'dpo_retraining_diploma'
    if direction == 'po' and program_kind in ('po_initial_training','po_retraining','po_upskilling'):
        return 'po_profession_certificate'
    raise IssuanceError('Не поддерживается структурированный вид программы из снимка завершения.')


@dataclass(repr=False)
class IssuedDocument:
    id: int | None = None
    training_id: int | None = None
    kind: str = ''
    full_name: str = ''
    program_name: str = ''
    hours: int = 0
    direction: str = ''
    program_kind: str = ''
    qualification: str = ''
    qualification_grade: str = ''
    completion_date: str = ''
    completion_order_id: int | None = None
    completion_order_number: str = ''
    completion_order_date: str = ''
    document_date: str = ''
    historical_number: str = ''
    blank_series: str = ''
    blank_number: str = ''
    blank_key: str | None = None
    template_id: int | None = None
    state: str = 'draft'
    number: str | None = None
    number_key: str | None = None
    document_path: str = ''
    document_sha256: str = ''
    snapshot: str = '{}'
    created_at: str = ''
    registered_at: str = ''
    revision: int = 1


@dataclass(repr=False)
class TransferEvent:
    id: int | None = None
    document_id: int | None = None
    method: str = 'personal'
    event_date: str = ''
    recipient: str = ''
    confirmation: str = ''
    address: str = ''
    email: str = ''
    tracking_number: str = ''
    postal_status: str = ''
    received_date: str = ''
    parent_event_id: int | None = None
    medium: str = ''
    note: str = ''
    created_at: str = ''
