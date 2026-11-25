"""Представление действующих правил приёма для сотрудника и печатной памятки."""
from dataclasses import dataclass
from html import escape

from proficentr.admission import SOURCES, MIN_HOURS, program_problems
from proficentr.programs import KINDS

GROUPS = ('Получить от слушателя', 'Подготовить и распечатать', 'Проверить перед зачислением')
STATUS_LABELS = {'required': 'Нужно получить', 'absent': 'Не хватает документа',
                 'prepared': 'Подготовлен — проверьте подписание', 'received': 'Документ получен',
                 'verified': 'Проверено', 'not_required': 'Не требуется'}


@dataclass(frozen=True)
class ChecklistRow:
    title: str
    detail: str = ''
    key: str = ''
    status: str = ''


@dataclass(frozen=True)
class AdmissionChecklist:
    title: str
    subtitle: str
    notice: str
    groups: tuple[tuple[ChecklistRow, ...], ...]


def checklist(repository, program, program_id=None, assessment=None):
    """Ничего не сохраняет; обязательность берётся только из механизма admission."""
    direction = program['direction']
    title = ('ДПО' if direction == 'dpo' else 'ПО') + ' — ' + KINDS[direction][program['kind']]
    subtitle = f"{program['full_name']} · {program['hours']} ч." if program_id is not None else ''
    notice = ('Предварительный перечень. Программа не выбрана: часы и специальные условия конкретной программы ещё не учтены.'
              if program_id is None else 'Перечень по настроенным требованиям программы. Применимость отдельных условий нужно проверить.')
    rules = repository.program_rules(program, program_id)
    if assessment:
        rules = repository.applicable(assessment.training_id) if not assessment.historical else repository.rules(include_history=True)
        notice = ('Историческая комплектность: изменения запрещены.' if assessment.historical else
                  'Можно зачислять' if assessment.ready else 'Не хватает документов или не выполнены условия допуска.')
    by_key = {r.rule_key: r for r in rules}
    by_signature = {repository.rule_signature(r): r for r in rules}
    groups = [[], [], []]
    items = assessment.items if assessment else rules
    for item in items:
        key = item.key if assessment else item.rule_key
        rule = by_signature.get(item.rule_signature) if assessment and assessment.historical else by_key.get(key)
        typ = rule.document_type if rule else 'consent' if key == 'pd_consent' else 'evidence'
        group = 1 if typ in ('application', 'consent') else 2 if typ == 'condition' else 0
        detail = SOURCES[item.source] + (' · Обязательно' if item.required else ' · Не блокирует зачисление')
        if rule and rule.condition_tag:
            detail += ' · При применимости условия'
        if rule and rule.sign_required:
            detail += ' · Проверить подпись'
        if assessment:
            status = STATUS_LABELS[item.status]
            if item.status == 'required':
                status = ('Нужно получить', 'Распечатать', 'Нужно проверить')[group]
            detail += ' · ' + status
            if item.reason:
                detail += ' · ' + item.reason
        groups[group].append(ChecklistRow(item.title, detail, key, item.status if assessment else ''))
    if not assessment or not assessment.historical:
        groups[1].append(ChecklistRow('Договор на обучение', 'По выбранному виду обучения и заказчику; используйте утверждённый шаблон.'))
    if not assessment or (not assessment.historical and not any(i.key == 'pd_consent' for i in assessment.items)):
        groups[1].append(ChecklistRow('Отдельное согласие на обработку персональных данных — если применимо',
                                     'Сначала определите правовое основание. При самостоятельном законном основании основное согласие не требуется.'))
    if assessment and assessment.historical:
        groups[2].extend(ChecklistRow(p) for p in assessment.problems)
        return AdmissionChecklist(title, subtitle, notice, tuple(tuple(g) for g in groups))
    groups[2].extend(ChecklistRow(t) for t in (
        'Проверить комплектность документов и соответствие выбранной программе',
        'Проверить подписание заявления, договора и применимых согласий',
        'Проверить правовое основание обработки персональных данных и условия допуска'))
    if program_id is not None:
        groups[2].extend(ChecklistRow(p) for p in program_problems(program))
    elif program['kind'] in MIN_HOURS:
        groups[2].append(ChecklistRow(f"Выбрать программу продолжительностью не менее {MIN_HOURS[program['kind']]} академических часов"))
    return AdmissionChecklist(title, subtitle, notice, tuple(tuple(g) for g in groups))


def memo_html(value):
    """Экранирует все данные: названия и локальные требования не являются HTML."""
    parts = ['<html><head><meta charset="utf-8"></head><body>',
             '<h1>Памятка по приёму на обучение</h1>', '<h2>' + escape(value.title) + '</h2>',
             '<p>' + escape(value.subtitle) + '</p>', '<p>' + escape(value.notice) + '</p>']
    for heading, rows in zip(GROUPS, value.groups):
        parts.append('<h2>' + escape(heading) + '</h2>')
        for row in rows:
            parts.append('<p>☐ ' + escape(row.title) + ('<br><small>' + escape(row.detail) + '</small>' if row.detail else '') + '</p>')
    parts.append('<p>Дата проверки: ______________ Ответственный: __________________________</p></body></html>')
    return ''.join(parts)
