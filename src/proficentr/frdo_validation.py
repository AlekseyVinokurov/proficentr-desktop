"""Подготовка копий значений и проверка строк ФРДО без записи в SQLite."""

from dataclasses import dataclass, field
from datetime import date, datetime
from difflib import get_close_matches
import re
import unicodedata

from openpyxl.utils import get_column_letter

from proficentr.frdo_templates import FrdoError, TemplateStructure
from proficentr.students import normalize_snils

# Соответствия букв установлены по XLSX, правила — по приложенным PDF.
FIELDS = {
    'dpo': dict(start='S', end='T', hours='U', surname='V', first_name='W', patronymic='X',
                birth='Y', sex='Z', snils='AA', citizenship='AE', form='AB', funding='AC',
                relations='AD', change='AN'),
    'po': dict(start='N', end='O', hours='P', surname='Q', first_name='R', patronymic='S',
               birth='T', sex='U', snils='V', citizenship='W', form='X', funding='Y',
               relations='Z', change='AI'),
}


@dataclass(frozen=True, repr=False)
class ValidationIssue:
    record: str
    column: str
    field: str
    value: object
    reason: str
    suggestions: tuple[str, ...] = ()

    def message(self):
        choices = f' Допустимое сопоставление: {", ".join(self.suggestions)}.' if self.suggestions else ''
        return f'Запись {self.record}, {self.column} ({self.field}), значение {self.value!r}: {self.reason}.{choices}'


@dataclass(repr=False)
class ValidationResult:
    values: dict[str, str] = field(default_factory=dict)
    issues: list[ValidationIssue] = field(default_factory=list)
    changes: dict[str, tuple[object, str]] = field(default_factory=dict)

    @property
    def valid(self):
        return not self.issues


def normalize_text(value: str, *, preserve_internal_spaces=False) -> str:
    """Только технические пробелы. Невидимые/управляющие символы остаются для ошибки."""
    value = value.replace('\u00a0', ' ').replace('\u202f', ' ')
    value = re.sub(r'[\t\r\n]+', ' ', value).strip(' ')
    return value if preserve_internal_spaces else re.sub(' +', ' ', value)


def export_filename(ogrn: str, kpp: str, issue_year: int, number: int) -> str:
    if not isinstance(ogrn, str) or not re.fullmatch('[0-9]{13}', ogrn):
        raise FrdoError('ОГРН должен содержать 13 цифр.')
    if not isinstance(kpp, str) or not re.fullmatch('[0-9]{9}', kpp):
        raise FrdoError('КПП должен содержать 9 цифр.')
    if type(issue_year) is not int or not 1000 <= issue_year <= 9999:
        raise FrdoError('Передайте четырёхзначный год выдачи документов.')
    if type(number) is not int or number < 1:
        raise FrdoError('Номер выгрузки должен быть положительным целым числом.')
    return f'{ogrn}_{kpp}_{issue_year % 100:02d}_{number}.xlsx'


class PreExportValidator:
    def __init__(self, structure: TemplateStructure, *, mappings=None):
        self.structure = structure
        # Явные соответствия отдельно по виду: {dpo: {Z: {male: Муж}}, po: {...}}.
        self.mappings = (mappings or {}).get(structure.kind, {})

    def validate(self, record, values, *, today=None):
        today = today or date.today()
        kind = self.structure.kind
        fields = FIELDS[kind]
        result = ValidationResult()
        columns = {get_column_letter(i + 1): name for i, name in enumerate(self.structure.headers)}
        protected = {'F', 'G', 'I'} | ({'Q', 'R'} if kind == 'dpo' else set())

        def issue(column, reason, suggestions=()):
            result.issues.append(ValidationIssue(str(record), column, columns.get(column, 'Неизвестное поле'),
                                                values.get(column, ''), reason, tuple(suggestions)))

        for column, raw in values.items():
            if column not in columns:
                issue(column, 'неизвестный столбец')
                continue
            if raw is None:
                value = ''
            elif isinstance(raw, datetime):
                value = raw.strftime('%d.%m.%Y')
            elif isinstance(raw, date):
                value = raw.strftime('%d.%m.%Y')
            elif isinstance(raw, str):
                value = raw if column in protected or raw in self.structure.dictionaries.get(column, ()) else normalize_text(raw)
            else:
                # Не преобразуем числовые коды/номера: ведущие нули уже могли быть потеряны.
                issue(column, 'передайте строку; числовое значение может потерять ведущие нули')
                continue
            result.values[column] = value
            if raw != value:
                result.changes[column] = (raw, value)
            if any(unicodedata.category(c).startswith('C') or (c.isspace() and c != ' ') for c in value):
                issue(column, 'управляющий, невидимый или недопустимый пробельный символ Unicode')
            if column in protected and (value != normalize_text(value) or '  ' in value):
                issue(column, 'неоднозначные пробелы в реквизитах; подтвердите исправление')
            allowed = self.structure.dictionaries.get(column)
            if value and allowed:
                mapped = self.mappings.get(column, {}).get(value, value)
                if mapped in allowed:
                    result.values[column] = mapped
                    if mapped != raw:
                        result.changes[column] = (raw, mapped)
                else:
                    suggestions = [v for v in allowed if normalize_text(v).casefold() == value.casefold()]
                    issue(column, 'значение отсутствует в справочнике XLSX; задайте точное сопоставление',
                          suggestions or get_close_matches(value, allowed, n=3))

        def val(column):
            return result.values.get(column, '')

        def required(column):
            if not val(column):
                issue(column, 'обязательное поле не заполнено')

        def pattern(column, regex, limit=None):
            value = val(column)
            if value and (not re.fullmatch(regex, value) or (limit and len(value) > limit)):
                issue(column, f'недопустимые символы или длина{f" (до {limit})" if limit else ""}')

        def parsed_date(column):
            value = val(column)
            if not value:
                return None
            try:
                if not re.fullmatch(r'[0-9]{2}\.[0-9]{2}\.[0-9]{4}', value):
                    raise ValueError
                return datetime.strptime(value, '%d.%m.%Y').date()
            except ValueError:
                issue(column, 'нужна существующая дата в формате ДД.ММ.ГГГГ')
                return None

        required_columns = ['A', 'B', 'C', 'D', 'E', 'H', 'I', 'K'] + [fields[n] for n in
                            ('start', 'end', 'hours', 'surname', 'first_name', 'patronymic', 'sex')]
        if val('A') != 'Справка об обучении':
            required_columns += ['F', 'G']
        if kind == 'dpo':
            required_columns += ['N']
        arts = kind == 'po' and val('A') == 'Свидетельство об освоении дополнительных предпрофессиональных программ в области искусств'
        if kind == 'po':
            if arts:
                for column in ('J', 'L'):
                    if val(column):
                        issue(column, 'для предпрофессиональных программ в области искусств поле должно быть пустым')
            else:
                required_columns += ['L']
        for column in required_columns:
            required(column)
        if val('B') and val('B') != 'Оригинал':
            issue('B', 'на этом этапе поддерживается только Оригинал; дубликаты не реализованы')
        for column in ('C', 'D', 'E'):
            if val(column) and val(column) != 'Нет':
                issue(column, 'обычный экспорт оригинала требует Нет; сложные исправления не реализованы')
        duplicate_start = 32 if kind == 'dpo' else 27
        for i in range(duplicate_start, len(columns) + 1):
            column = get_column_letter(i)
            if val(column):
                issue(column, 'при обычном экспорте поле должно быть пустым')

        pattern('F', r'[А-Яа-яЁёA-Za-z0-9. /-]+', 20)
        pattern('G', r'[А-Яа-яЁёA-Za-z0-9. /-]+', 40)
        pattern('I', r'[А-Яа-яЁёA-Za-z0-9(). /-]+' if kind == 'dpo' else r'[А-Яа-яЁёA-Za-z0-9№_(). /-]+', 30)
        pattern('K', r'[А-Яа-яЁёA-Za-z0-9().,/ :«»"?№&+#_;-]+', 600 if kind == 'dpo' else 255)
        if kind == 'dpo':
            pattern('N', r'[А-Яа-яЁёA-Za-z0-9(). :;,/«»-]+', 500)
            pattern('Q', r'[А-Яа-яЁёA-Za-z0-9. /-]+', 20)
            pattern('R', r'[А-Яа-яЁёA-Za-z0-9. /-]+', 40)
        name_columns = [fields[n] for n in ('surname', 'first_name', 'patronymic')]
        if kind == 'dpo':
            name_columns.append('P')
        for column in name_columns:
            pattern(column, r"[А-Яа-яЁё. '`()\-]+", 50)
            if val(column).startswith('-') or val(column).endswith('-') or re.search(r'--| -|- ', val(column)):
                issue(column, 'недопустимое расположение пробела или дефиса в ФИО')

        issued = parsed_date('H')
        birth = parsed_date(fields['birth'])
        if issued and issued > today:
            issue('H', 'дата выдачи находится в будущем')
        if birth and (birth > today or (issued and birth > issued)):
            issue(fields['birth'], 'дата рождения позже даты выдачи или текущей даты')
        if issued and issued.year >= 2021:
            for name in ('birth', 'citizenship', 'form', 'funding', 'relations'):
                required(fields[name])
            if val(fields['citizenship']) == '643':
                required(fields['snils'])
        snils = val(fields['snils'])
        if snils:
            if not re.fullmatch(r'[0-9]{3}-[0-9]{3}-[0-9]{3} [0-9]{2}', snils):
                issue(fields['snils'], 'формат СНИЛС: ХХХ-ХХХ-ХХХ ХХ')
            else:
                try:
                    normalize_snils(snils)
                except ValueError as error:
                    issue(fields['snils'], str(error))
        years = {}
        for name in ('start', 'end'):
            column = fields[name]
            value = val(column)
            if value:
                if not re.fullmatch('[0-9]{4}', value) or not 1978 <= int(value) <= today.year:
                    issue(column, 'год оригинала должен быть от 1978 до текущего года')
                else:
                    years[name] = int(value)
        if len(years) == 2 and years['end'] < years['start']:
            issue(fields['end'], 'окончание обучения раньше начала')
        if issued and 'end' in years and years['end'] > issued.year:
            issue(fields['end'], 'обучение завершено позже выдачи документа')
        hours = val(fields['hours'])
        if hours and (not re.fullmatch('[0-9]+', hours) or int(hours) < (6 if kind == 'po' else 1)):
            issue(fields['hours'], 'срок обучения должен быть целым числом часов, не менее ' + ('6' if kind == 'po' else '1'))
        return result
