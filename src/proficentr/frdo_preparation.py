"""Адаптеры исторических выпускных документов и подготовка без создания XLSX."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
import sqlite3

from openpyxl.utils import get_column_letter, column_index_from_string

from proficentr.frdo_profiles import PROFILES
from proficentr.frdo_templates import FrdoError, FrdoTemplateRepository, inspect_template
from proficentr.frdo_validation import FIELDS, PreExportValidator, ValidationIssue
from proficentr.issued_documents import KINDS
from proficentr.issued_repository import IssuedDocumentRepository
from proficentr.program_repository import ProgramRepository
from proficentr.programs import DIRECTIONS
from proficentr.student_repository import StudentRepository
from proficentr.students import iso_date

PROGRAM_TYPES = {
    'dpo_upskilling': 'Повышение квалификации',
    'dpo_retraining': 'Профессиональная переподготовка',
    'po_initial_training': 'Программа профессиональной подготовки по профессии рабочего, должности служащего',
    'po_retraining': 'Программа переподготовки рабочих, служащих',
    'po_upskilling': 'Программа повышения квалификации рабочих, служащих',
}


@dataclass(repr=False)
class TrainingDetails:
    training_id: int | None = None
    form: str = ''
    funding: str = ''
    relations: str = ''
    education_level: str = ''
    education_surname: str = ''
    education_series: str = ''
    education_number: str = ''
    dpo_area: str = ''
    dpo_group: str = ''
    po_profession: str = ''
    po_grade: str = ''
    qualification: str = ''
    patronymic: str = ''


DETAIL_FIELDS = tuple(TrainingDetails.__dataclass_fields__)[1:]
PERSONAL_FIELDS = ('sex', 'citizenship_code')
PROGRAM_FIELDS = ('dpo_area', 'dpo_group', 'po_profession')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def display_date(value):
    if not value:
        return ''
    try:
        return iso_date(value, 'Дата').strftime('%d.%m.%Y')
    except ValueError:
        return value  # Ошибка будет показана штатным PreExportValidator.


def _base(document, snapshot, student, details):
    kind = document.direction
    fields = FIELDS[kind]
    context = snapshot['context']
    historical = context['student']
    training = snapshot['basis']['training']
    if training['id'] != document.training_id or training['student_id'] != student.id or details.training_id != document.training_id:
        raise FrdoError('Нарушена связь исторического документа со слушателем и обучением.')
    values = {get_column_letter(i + 1): '' for i in range(len(PROFILES[kind]['headers']))}
    values.update(A=KINDS[document.kind], B='Оригинал', C='Нет', D='Нет', E='Нет',
                  F=document.blank_series, G=document.blank_number, H=display_date(document.document_date),
                  I=document.number or '', J=PROGRAM_TYPES.get(document.program_kind, ''), K=document.program_name)
    snils = student.snils
    if re.fullmatch('[0-9]{11}', snils):
        snils = f'{snils[:3]}-{snils[3:6]}-{snils[6:9]} {snils[9:]}'
    for name, value in dict(start=training['start_date'][:4], end=document.completion_date[:4], hours=str(document.hours),
            surname=historical['surname'], first_name=historical['first_name'], patronymic=historical['patronymic'] or details.patronymic,
            birth=display_date(student.birth_date), sex=student.sex, snils=snils, citizenship=student.citizenship_code,
            form=details.form, funding=details.funding, relations=details.relations).items():
        values[fields[name]] = value
    return values, json.loads(training['program_snapshot'])


def adapt_dpo(document, snapshot, student, details):
    values, program = _base(document, snapshot, student, details)
    values.update(L=details.dpo_area or program.get('dpo_area', ''),
                  M=details.dpo_group or program.get('dpo_group', ''),
                  N=document.qualification or details.qualification,
                  O=details.education_level or snapshot['context']['student'].get('education_level', ''),
                  P=details.education_surname, Q=details.education_series, R=details.education_number)
    return values


def adapt_po(document, snapshot, student, details):
    values, program = _base(document, snapshot, student, details)
    values.update(L=details.po_profession or program.get('po_profession', ''),
                  M=details.po_grade or document.qualification_grade)
    return values


@dataclass(frozen=True, repr=False)
class PreparedRecord:
    document_id: int
    training_id: int
    student_id: int
    kind: str
    full_name: str
    program_name: str
    number: str
    document_date: str
    template_id: int
    template_sha256: str
    signature: str
    cells: tuple[tuple[str, str], ...]
    issues: tuple[ValidationIssue, ...]

    @property
    def ready(self):
        return not self.issues

    @property
    def values(self):
        return dict(self.cells)

    @property
    def status(self):
        return 'Готово' if self.ready else 'Требует исправления'

    def messages(self):
        # Имена полей и причины, без технических букв Excel.
        return [f'{self.full_name} — {issue.field}: {issue.reason}' +
                (f'. Варианты: {", ".join(issue.suggestions)}' if issue.suggestions else '') for issue in self.issues]


class FrdoPreparationRepository:
    def __init__(self, connection, storage):
        self.connection = connection
        self.storage = storage
        self.templates = FrdoTemplateRepository(connection, storage)
        self.issued = IssuedDocumentRepository(connection, storage)
        self.students = StudentRepository(connection)
        self.programs = ProgramRepository(connection)

    def details(self, training_id):
        row = self.connection.execute('SELECT * FROM frdo_training_details WHERE training_id=?', (training_id,)).fetchone()
        return TrainingDetails(*row) if row else TrainingDetails(training_id)

    def _source(self, document_id):
        document = self.issued.load(document_id)
        if document.state != 'registered' or document.kind not in KINDS or document.direction not in FIELDS:
            raise FrdoError('Для ФРДО нужен зарегистрированный основной выпускной документ ДПО или ПО.')
        try:
            snapshot = json.loads(document.snapshot)
            training = snapshot['basis']['training']
            if training['id'] != document.training_id:
                raise FrdoError('Историческое обучение не соответствует документу.')
            student = self.students.load(training['student_id'])
            # Читаем именно снимок внутри зарегистрированного документа, не текущую программу.
            json.loads(training['program_snapshot'])
            return document, snapshot, student, self.details(document.training_id)
        except (KeyError, TypeError, ValueError) as error:
            raise FrdoError('Не удалось прочитать исторические сведения выпускного документа.') from error

    def review(self, document_id, *, today=None, template=None, structure=None):
        document, snapshot, student, details = self._source(document_id)
        template = template or self.templates.active(document.direction)
        if template is None:
            raise FrdoError(f'Сначала подключите шаблон {DIRECTIONS[document.direction]} в настройках.')
        structure = structure or inspect_template(self.templates.source(template), document.direction)
        try:
            values = (adapt_dpo if document.direction == 'dpo' else adapt_po)(document, snapshot, student, details)
        except (KeyError, TypeError, ValueError) as error:
            raise FrdoError('Неполные исторические сведения документа; требуется проверка основания выдачи.') from error
        result = PreExportValidator(structure).validate(document.full_name, values, today=today)
        signature = sha256(encoded({'document': asdict(document), 'student': asdict(student),
                                    'details': asdict(details), 'template': asdict(template),
                                    'program_card': asdict(self.programs.load(snapshot['basis']['training']['program_id']))}).encode()).hexdigest()
        return PreparedRecord(document.id, document.training_id, student.id, document.direction, document.full_name,
                              document.program_name, document.number, document.document_date, template.id, template.sha256,
                              signature, tuple(result.values.items()), tuple(result.issues))

    def search(self, kind, start, end, *, today=None):
        if kind not in FIELDS:
            raise FrdoError('Выберите ДПО или ПО.')
        if iso_date(start, 'Начало периода') > iso_date(end, 'Конец периода'):
            raise FrdoError('Начало периода позже окончания.')
        template = self.templates.active(kind)
        if template is None:
            raise FrdoError(f'Сначала подключите шаблон {DIRECTIONS[kind]} в настройках.')
        structure = inspect_template(self.templates.source(template), kind)
        ids = self.connection.execute("SELECT id FROM issued_documents WHERE state='registered' AND direction=? "
                                      'AND document_date BETWEEN ? AND ? ORDER BY document_date,number,id', (kind, start, end))
        return [self.review(r[0], today=today, template=template, structure=structure) for r in ids.fetchall()]

    def edit_context(self, document_id):
        document, snapshot, student, details = self._source(document_id)
        training = snapshot['basis']['training']
        return document, student, details, self.programs.load(training['program_id'])

    def _write_changes(self, scope, table, key_name, key, old, changes):
        diff = {k: {'old': old[k], 'new': value} for k, value in changes.items() if old[k] != value}
        if diff:
            self.connection.execute(
                f'UPDATE {table} SET ' + ','.join(k + '=?' for k in changes) + f' WHERE {key_name}=?',  # nosec B608 # Internal fixed scopes and allowlisted field names.
                (*changes.values(), key))
        return {scope: {'id': key, 'fields': diff}} if diff else {}

    def save_details(self, document_id, *, personal=None, training=None, program=None, expected_signature):
        """Единая атомарная правка дополнений. Архив и защищённый реестр не затрагиваются."""
        personal, training, program = personal or {}, training or {}, program or {}
        if set(personal) - set(PERSONAL_FIELDS) or set(training) - set(DETAIL_FIELDS) or set(program) - set(PROGRAM_FIELDS):
            raise FrdoError('Переданы неподдерживаемые поля подготовки ФРДО.')
        if any(not isinstance(v, str) or len(v) > 1000 for data in (personal, training, program) for v in data.values()):
            raise FrdoError('Сведения должны быть строками длиной до 1000 символов.')
        if self.connection.in_transaction:
            raise FrdoError('Завершите текущую операцию с базой.')
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                review = self.review(document_id)
                if review.signature != expected_signature:
                    raise FrdoError('Исходные сведения или шаблон изменились. Откройте запись повторно.')
                document, snapshot, student, old = self._source(document_id)
                structure = self.templates.structure(document.direction)
                f = FIELDS[document.direction]
                target_columns = {'sex': f['sex'], 'citizenship_code': f['citizenship'], 'form': f['form'],
                    'funding': f['funding'], 'relations': f['relations'], 'education_level': 'O',
                    'education_surname': 'P', 'education_series': 'Q', 'education_number': 'R',
                    'dpo_area': 'L', 'dpo_group': 'M', 'po_profession': 'L', 'po_grade': 'M', 'qualification': 'N', 'patronymic': f['patronymic']}
                permitted_training = {'form', 'funding', 'relations', 'patronymic'} | (
                    {'education_level', 'education_surname', 'education_series', 'education_number', 'dpo_area', 'dpo_group', 'qualification'}
                    if document.direction == 'dpo' else {'po_profession', 'po_grade'})
                permitted_program = {'dpo_area', 'dpo_group'} if document.direction == 'dpo' else {'po_profession'}
                if set(training) - permitted_training or set(program) - permitted_program:
                    raise FrdoError('Поля не соответствуют выбранной категории ДПО/ПО.')
                if training.get('patronymic') and snapshot['context']['student']['patronymic']:
                    raise FrdoError('Отчество уже зафиксировано в выданном документе; его подмена запрещена.')
                if training.get('qualification') and document.qualification:
                    raise FrdoError('Квалификация уже зафиксирована в выданном документе; её подмена запрещена.')
                for data in (personal, training, program):
                    for name, value in data.items():
                        column = target_columns[name]
                        allowed = structure.dictionaries.get(column)
                        if value and allowed and value not in allowed:
                            raise FrdoError(f'{structure.headers[column_index_from_string(column)-1]}: выберите точное значение справочника.')
                        # Переиспользуем правила P05.1; отсутствие других полей не блокирует сохранение неполной карточки.
                        errors = PreExportValidator(structure).validate(document.full_name, {column: value}).issues
                        errors = [i for i in errors if i.column == column and i.value == value and
                                  i.reason != 'обязательное поле не заполнено']
                        if errors:
                            raise FrdoError(f'{errors[0].field}: {errors[0].reason}')
                changes = self._write_changes('student', 'students', 'id', student.id, asdict(student), personal)
                if training:
                    self.connection.execute('INSERT OR IGNORE INTO frdo_training_details(training_id) VALUES (?)', (document.training_id,))
                changes.update(self._write_changes('training', 'frdo_training_details', 'training_id', document.training_id, asdict(old), training))
                program_id = snapshot['basis']['training']['program_id']
                current_program = self.programs.load(program_id)
                if program and current_program.direction != document.direction:
                    raise FrdoError('Текущая программа относится к другой категории; уточняйте историческую запись обучения.')
                changes.update(self._write_changes('program', 'programs', 'id', program_id, asdict(current_program), program))
                if changes:
                    self.connection.execute('INSERT INTO frdo_change_history(document_id,changes) VALUES (?,?)', (document_id, encoded(changes)))
        except sqlite3.Error:
            raise FrdoError('Сведения ФРДО не сохранены. Проверьте доступность базы.') from None
        return self.review(document_id)
