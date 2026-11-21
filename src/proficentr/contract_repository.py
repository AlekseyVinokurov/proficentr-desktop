"""Договоры: черновики, подтверждённая регистрация, снимки и файловый архив."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from io import BytesIO
import hashlib
import json
import re
import sqlite3
from uuid import uuid4

from docx import Document

from proficentr.contracts import Contract, ContractError, format_price, number_key, validate_contract
from proficentr.docx_context import SCHEMA, validate_context
from proficentr.docx_engine import generate_document, publish_docx, read_docx, render_docx, validate_package
from proficentr.document_templates import TemplateRepository
from proficentr.students import is_adult, iso_date
from proficentr.trainings import validate_dates

FIELDS = tuple(asdict(Contract()))
DRAFT_FIELDS = ('customer_type', 'counterparty_id', 'student_id', 'person_data', 'contract_date',
                'historical_number', 'total_cents', 'payment_terms', 'note', 'template_id')
TEMPLATE_TYPES = {'student': 'contract_person', 'person': 'contract_person',
                  'legal_entity': 'contract_legal', 'sole_proprietor': 'contract_entrepreneur'}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


@dataclass(repr=False)
class ContractReview:
    contract_id: int
    signature: str
    template_id: int
    context: dict
    training_ids: list[int]
    program_id: int


class ContractRepository:
    def __init__(self, connection, storage):
        self.connection = connection
        self.storage = storage
        self.templates = TemplateRepository(connection, storage)
        connection.create_function('contract_fold', 1, lambda text: ' '.join(text.split()).casefold(), deterministic=True)

    @contextmanager
    def transaction(self):
        if self.connection.in_transaction:
            raise ContractError('Завершите текущую операцию с базой.')
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                yield
        except (sqlite3.Error, OSError):
            raise ContractError('Операция с договором не выполнена. Изменения не сохранены; проверьте базу и хранилище.') from None

    def _entity(self, table, fields, record_id):
        row = self.connection.execute(
            f"SELECT {', '.join(fields)} FROM {table} WHERE id = ?", (record_id,),  # nosec B608 # Only internal table/field constants, bound ID.
        ).fetchone()
        if row is None:
            raise ContractError('Связанная запись не найдена. Проверьте выбранные сведения.')
        return dict(zip(fields, row))

    def load(self, contract_id):
        try:
            values = self._entity('contracts', FIELDS, contract_id)
            values['person_data'] = json.loads(values['person_data'])
            return Contract(**values)
        except (sqlite3.Error, json.JSONDecodeError):
            raise ContractError('Не удалось прочитать договор.') from None

    def training_ids(self, contract_id):
        try:
            return [row[0] for row in self.connection.execute(
                'SELECT training_id FROM contract_trainings WHERE contract_id = ? ORDER BY position', (contract_id,),
            )]
        except sqlite3.Error:
            raise ContractError('Не удалось прочитать состав договора.') from None

    def _trainings(self, training_ids, *, required):
        if len(training_ids) > 500 or len(set(training_ids)) != len(training_ids):
            raise ContractError('Выберите не более 500 разных записей обучения.')
        if required and not training_ids:
            raise ContractError('Добавьте хотя бы одно существующее обучение.')
        rows = []
        program = None
        for training_id in training_ids:
            item = self._entity('trainings', ('id', 'student_id', 'program_id', 'group_id', 'start_date', 'end_date', 'state', 'program_snapshot'), training_id)
            if item['state'] != 'preparation':
                raise ContractError('Исключённая запись обучения не может быть включена в договор.')
            validate_dates(item['start_date'], item['end_date'])
            try:
                snapshot = json.loads(item['program_snapshot'])
                if set(snapshot) - {'dpo_area', 'dpo_group', 'po_profession'} != {'full_name', 'direction', 'kind', 'hours', 'code', 'edition', 'qualification', 'grade'}:
                    raise ValueError
            except (ValueError, TypeError):
                raise ContractError('Повреждён снимок программы обучения.') from None
            basis = (item['program_id'], snapshot)
            if program is not None and basis != program:
                raise ContractError('В договор можно включить одну программу с одинаковым снимком редакции. Оформите отдельные договоры или явно исправьте подготовительные записи.')
            program = basis
            rows.append(item)
        return rows

    def save(self, contract, training_ids):
        contract = validate_contract(contract)
        with self.transaction():
            old = self.load(contract.id) if contract.id else None
            if old and (old.state != 'draft' or old.revision != contract.revision):
                raise ContractError('Договор зарегистрирован или изменён в другом окне. Откройте актуальную запись.')
            if contract.state != 'draft':
                raise ContractError('Редактировать можно только черновик.')
            self._trainings(training_ids, required=False)
            if contract.counterparty_id:
                customer = self._entity('counterparties', ('kind',), contract.counterparty_id)
                if customer['kind'] != contract.customer_type:
                    raise ContractError('Тип организации не совпадает с типом заказчика.')
            if contract.student_id:
                self._entity('students', ('id',), contract.student_id)
            values = asdict(contract)
            values['person_data'] = encoded(contract.person_data)
            parameters = tuple(values[key] for key in DRAFT_FIELDS)
            if old is None:
                cursor = self.connection.execute(
                    f"INSERT INTO contracts ({', '.join(DRAFT_FIELDS)}) VALUES ({', '.join('?' for _ in DRAFT_FIELDS)})",  # nosec B608 # Fixed field names and bound values.
                    parameters,
                )
                contract_id = cursor.lastrowid
            else:
                contract_id = old.id
                self.connection.execute(
                    f"UPDATE contracts SET {', '.join(key + ' = ?' for key in DRAFT_FIELDS)}, revision = revision + 1 WHERE id = ?",  # nosec B608 # Fixed field names and bound values.
                    (*parameters, contract_id),
                )
                self.connection.execute('DELETE FROM contract_trainings WHERE contract_id = ?', (contract_id,))
            self.connection.executemany('INSERT INTO contract_trainings(contract_id, training_id, position) VALUES (?,?,?)',
                                        [(contract_id, training_id, position) for position, training_id in enumerate(training_ids)])
        return self.load(contract_id)

    def _student(self, student_id):
        fields = tuple(key for key in SCHEMA['student'] if key != 'full_name')
        values = self._entity('students', fields, student_id)
        values['full_name'] = ' '.join(values[key] for key in ('surname', 'first_name', 'patronymic') if values[key])
        return values

    def _context(self, contract, trainings):
        context = {root: dict.fromkeys(fields, '') for root, fields in SCHEMA.items() if root != 'students'}
        try:
            center = self._entity('training_center', tuple(SCHEMA['center']), 1)
        except ContractError:
            raise ContractError('Заполните карточку учебного центра в настройках.') from None
        if not center['full_name']:
            raise ContractError('Заполните наименование учебного центра в настройках.')
        context['center'] = center
        students = []
        for training in trainings:
            student = self._student(training['student_id'])
            if not is_adult(iso_date(student['birth_date'], 'Дата рождения'), iso_date(training['start_date'], 'Дата начала')):
                raise ContractError('На дату начала обучения слушателю должно быть 18 лет.')
            students.append(dict(student, start_date=training['start_date'], end_date=training['end_date']))
        context['students'] = students
        if len(students) == 1:
            context['student'] = {key: students[0][key] for key in SCHEMA['student']}
        if contract.customer_type == 'student':
            if contract.student_id not in {row['student_id'] for row in trainings}:
                raise ContractError('Заказчик-слушатель должен входить в выбранные обучения.')
            customer = self._student(contract.student_id)
        elif contract.customer_type == 'person':
            customer = contract.person_data
        else:
            fields = tuple(key for key in SCHEMA['customer'] if key not in {
                'birth_date', 'registration_address', 'passport_series', 'passport_number', 'passport_issued_by', 'passport_issue_date', 'passport_department_code'})
            customer = self._entity('counterparties', fields, contract.counterparty_id)
            if customer['kind'] != contract.customer_type:
                raise ContractError('Тип организации-заказчика изменён. Исправьте черновик.')
        context['customer'].update({key: value for key, value in customer.items() if key in SCHEMA['customer']})
        context['customer']['kind'] = contract.customer_type
        if not context['customer']['full_name']:
            raise ContractError('Выберите заказчика или заполните ФИО стороннего заказчика.')
        if not contract.payment_terms:
            raise ContractError('Укажите условия оплаты.')
        snapshot = json.loads(trainings[0]['program_snapshot'])
        context['program'] = {('name' if key == 'full_name' else key): value for key, value in snapshot.items() if key not in ('dpo_area', 'dpo_group', 'po_profession')}
        for key in ('start_date', 'end_date'):
            values = {row[key] for row in trainings}
            context['training'][key] = values.pop() if len(values) == 1 else ''
        context['training']['state'] = 'Подготовка'
        group_ids = {row['group_id'] for row in trainings}
        if len(group_ids) == 1 and None not in group_ids:
            context['group'] = self._entity('training_groups', tuple(SCHEMA['group']), group_ids.pop())
        context['contract'].update(total_price=format_price(contract.total_cents), payment_terms=contract.payment_terms,
                                   currency='RUB', student_count=len({row['student_id'] for row in trainings}))
        context['document'].update(number='ПРОЕКТ — НЕ ЗАРЕГИСТРИРОВАН', date=contract.contract_date, is_test=False, is_draft=True)
        return validate_context(context)

    def _candidates(self, contract, trainings):
        direction = json.loads(trainings[0]['program_snapshot'])['direction']
        return [version for version in self.templates.list_versions(TEMPLATE_TYPES[contract.customer_type])
                if version.active and (version.variant == direction or version.variant.startswith(direction + '-'))]

    def suitable_templates(self, contract, training_ids):
        try:
            trainings = self._trainings(training_ids, required=True)
            return self._candidates(contract, trainings)
        except sqlite3.Error:
            raise ContractError('Не удалось подобрать шаблоны.') from None

    def _review(self, contract_id):
        contract = self.load(contract_id)
        if contract.state != 'draft':
            raise ContractError('Формировать и регистрировать можно только черновик.')
        training_ids = self.training_ids(contract_id)
        trainings = self._trainings(training_ids, required=True)
        context = self._context(contract, trainings)
        candidates = self._candidates(contract, trainings)
        selected = next((version for version in candidates if version.id == contract.template_id), None)
        if contract.template_id is None and len(candidates) == 1:
            selected = candidates[0]
        if selected is None:
            raise ContractError('Выберите подходящий активный шаблон типа договора с вариантом dpo/po или dpo-…/po-… в «Настройки → Шаблоны документов». При нескольких вариантах нужен явный выбор.')
        self.templates.source(selected.id)
        signature = hashlib.sha256(encoded({'draft': asdict(contract), 'context': context,
                                           'trainings': trainings, 'template': asdict(selected)}).encode()).hexdigest()
        return ContractReview(contract.id, signature, selected.id, context, training_ids, trainings[0]['program_id'])

    def prepare(self, contract_id):
        with self.transaction():
            return self._review(contract_id)

    def preview(self, contract_id):
        with self.transaction():
            review = self._review(contract_id)
            source = self.templates.source(review.template_id)
            data = read_docx(source.path)
            if hashlib.sha256(data).hexdigest() != source.sha256:
                raise ContractError('Шаблон изменился. Повторите выбор версии.')
            result = render_docx(data, review.context)
            document = Document(BytesIO(result))
            stamp = document.add_paragraph('ПРОЕКТ ДОГОВОРА — НЕ ЗАРЕГИСТРИРОВАН. Не подтверждает подписание.')
            stamp.runs[0].bold = True
            document.element.body.insert(0, stamp._p)
            stream = BytesIO(); document.save(stream)
            result = stream.getvalue()
            validate_package(result)
            folder = self._exports('contract-drafts')
            path = publish_docx(result, folder / ('DRAFT-unregistered-' + uuid4().hex + '.docx'))
        return path, review

    def _assign_number(self, contract):
        year = iso_date(contract.contract_date, 'Дата договора').year
        if contract.historical_number:
            number = contract.historical_number
            if self.connection.execute('SELECT 1 FROM contracts WHERE number_key = ?', (number_key(number),)).fetchone():
                raise ContractError('Номер договора уже зарегистрирован или аннулирован.')
            match = re.fullmatch(r'([0-9]{4})-([0-9]{4,9})', number)
            if match:
                self._advance(int(match[1]), int(match[2]))
            return number
        row = self.connection.execute('SELECT last_value FROM contract_sequences WHERE year = ?', (year,)).fetchone()
        counter = row[0] if row else 0
        while True:
            counter += 1
            number = f'{year:04d}-{counter:04d}'
            if not self.connection.execute('SELECT 1 FROM contracts WHERE number_key = ?', (number_key(number),)).fetchone():
                self._advance(year, counter)
                return number

    def _advance(self, year, value):
        if value > 0:
            self.connection.execute('INSERT INTO contract_sequences(year,last_value) VALUES (?,?) '
                                    'ON CONFLICT(year) DO UPDATE SET last_value = max(last_value, excluded.last_value)', (year, value))

    def register(self, contract_id, signature):
        path = None
        try:
            with self.transaction():
                review = self._review(contract_id)
                if review.signature != signature:
                    raise ContractError('После просмотра изменились сведения, обучения или шаблон. Сформируйте и проверьте новый проект.')
                contract = self.load(contract_id)
                number = self._assign_number(contract)
                review.context['document'].update(number=number, is_draft=False)
                day = iso_date(contract.contract_date, 'Дата договора')
                self._check_archive_path(self.storage.root / 'documents' / f'{day.year:04d}' / f'{day.month:02d}' / 'contracts')
                folder = self.storage.document_directory(day, 'contracts')
                destination = folder / ('contract-' + uuid4().hex + '.docx')
                # Path is assigned only after successful exclusive publication.
                path = generate_document(self.templates.source(review.template_id), review.context, destination)
                sha256 = hashlib.sha256(read_docx(path)).hexdigest()
                template = self.templates.load(review.template_id)
                snapshot = encoded({'context': review.context, 'training_ids': review.training_ids,
                                    'program_id': review.program_id, 'customer_type': contract.customer_type,
                                    'template': asdict(template)})
                self.connection.execute(
                    "UPDATE contracts SET state = 'registered', number = ?, number_key = ?, registered_at = CURRENT_TIMESTAMP, "
                    "template_id = ?, document_path = ?, document_sha256 = ?, snapshot = ?, revision = revision + 1 WHERE id = ?",
                    (number, number_key(number), review.template_id, path.relative_to(self.storage.root).as_posix(), sha256, snapshot, contract_id),
                )
        except Exception:
            if path is not None:
                # Preserve an uncommitted file for recovery instead of reusing or replacing it.
                self._quarantine(path)
            raise
        return self.load(contract_id)

    def annul(self, contract_id, reason):
        reason = reason.strip()
        if not reason:
            raise ContractError('Укажите основание аннулирования.')
        with self.transaction():
            contract = self.load(contract_id)
            if contract.state != 'registered':
                raise ContractError('Аннулировать можно только зарегистрированный договор.')
            self.verify_document(contract_id)
            self.connection.execute("UPDATE contracts SET state = 'annulled', annul_reason = ?, annulled_at = CURRENT_TIMESTAMP, "
                                    'revision = revision + 1 WHERE id = ?', (reason, contract_id))
        return self.load(contract_id)

    def _check_archive_path(self, path):
        root = self.storage.root
        if not path.resolve().is_relative_to(root.resolve()) or any(parent.is_symlink() for parent in [path, *path.parents] if parent.is_relative_to(root)):
            raise ContractError('В архиве обнаружена символическая ссылка или неверный путь.')

    def verify_document(self, contract_id):
        contract = self.load(contract_id)
        if contract.state == 'draft' or not re.fullmatch(r'documents/[0-9]{4}/[0-9]{2}/contracts/contract-[a-f0-9]{32}\.docx', contract.document_path):
            raise ContractError('У договора нет корректного архивного документа.')
        path = self.storage.root / contract.document_path
        self._check_archive_path(path)
        data = read_docx(path)
        if hashlib.sha256(data).hexdigest() != contract.document_sha256:
            raise ContractError('SHA-256 договора не совпадает с реестром. Оригинал повреждён или изменён; автоматическое исправление запрещено.')
        validate_package(data)
        return path

    def document_copy(self, contract_id):
        original = self.verify_document(contract_id)
        data = read_docx(original)
        if hashlib.sha256(data).hexdigest() != self.load(contract_id).document_sha256:
            raise ContractError('Архивный документ изменился.')
        return publish_docx(data, self._exports('contract-copies') / ('copy-' + uuid4().hex + '.docx'))

    def _exports(self, category):
        self.storage.require_available()
        folder = self.storage.root / 'exports' / category
        self._check_archive_path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _quarantine(self, path):
        folder = self._exports('unregistered-contracts') / uuid4().hex
        folder.mkdir()
        path.rename(folder / path.name)

    def recover_unregistered(self):
        """Under the same writer lock as registration; never adopt/delete an orphan."""
        count = 0
        with self.transaction():
            references = {row[0] for row in self.connection.execute("SELECT document_path FROM contracts WHERE document_path != ''")}
            documents = self.storage.root / 'documents'
            self._check_archive_path(documents)
            for path in documents.glob('[0-9][0-9][0-9][0-9]/[0-9][0-9]/contracts/*'):
                if not (re.fullmatch(r'contract-[a-f0-9]{32}\.docx', path.name) or re.fullmatch(r'\.docx-.*\.tmp', path.name)):
                    continue
                self._check_archive_path(path)
                if path.relative_to(self.storage.root).as_posix() not in references:
                    self._quarantine(path)
                    count += 1
        return count

    def search(self, query=''):
        try:
            query = ' '.join(query.split()).casefold()
            rows = self.connection.execute(
                "SELECT c.id, c.number, c.contract_date, c.total_cents, c.state, c.snapshot, c.customer_type, c.student_id, c.counterparty_id, c.person_data "
                "FROM contracts c WHERE instr(contract_fold(coalesce(c.number,'')), ?) > 0 "
                "OR instr(contract_fold(c.contract_date), ?) > 0 OR instr(contract_fold(c.snapshot), ?) > 0 "
                "OR c.id IN (SELECT ct.contract_id FROM contract_trainings ct JOIN students s ON s.id = (SELECT student_id FROM trainings WHERE id = ct.training_id) "
                "WHERE instr(contract_fold(s.surname || ' ' || s.first_name || ' ' || s.patronymic), ?) > 0) "
                "OR c.counterparty_id IN (SELECT id FROM counterparties WHERE instr(contract_fold(full_name), ?) > 0) "
                "OR instr(contract_fold(c.person_data), ?) > 0 ORDER BY c.contract_date DESC, c.id DESC",
                (query,) * 6,
            ).fetchall()
            result = []
            for record_id, number, day, cents, state, snapshot, kind, student_id, customer_id, person_data in rows:
                if state != 'draft':
                    context = json.loads(snapshot)['context']
                    customer, program, count = context['customer']['full_name'], context['program']['name'], context['contract']['student_count']
                else:
                    if kind == 'student' and student_id:
                        customer = self._student(student_id)['full_name']
                    elif customer_id:
                        customer = self._entity('counterparties', ('full_name',), customer_id)['full_name']
                    else:
                        customer = json.loads(person_data).get('full_name', '')
                    items = self.connection.execute('SELECT t.student_id, t.program_snapshot FROM trainings t JOIN contract_trainings ct ON ct.training_id = t.id WHERE ct.contract_id = ? ORDER BY ct.position', (record_id,)).fetchall()
                    program = json.loads(items[0][1])['full_name'] if items else ''
                    count = len({item[0] for item in items})
                result.append((record_id, number or 'Без номера', day, customer, program, count, format_price(cents), state))
            return result
        except (sqlite3.Error, json.JSONDecodeError):
            raise ContractError('Не удалось прочитать реестр договоров.') from None

    def available_trainings(self, query='', group_id=None):
        try:
            return self.connection.execute(
                "SELECT t.id, s.surname || ' ' || s.first_name || ' ' || s.patronymic, t.program_snapshot, t.start_date, t.end_date, g.name "
                "FROM trainings t JOIN students s ON s.id = t.student_id LEFT JOIN training_groups g ON g.id = t.group_id "
                "WHERE t.state = 'preparation' AND (? IS NULL OR t.group_id = ?) "
                "AND instr(contract_fold(s.surname || ' ' || s.first_name || ' ' || s.patronymic), ?) > 0 "
                "ORDER BY s.surname, s.id, t.id", (group_id, group_id, ' '.join(query.split()).casefold()),
            ).fetchall()
        except sqlite3.Error:
            raise ContractError('Не удалось прочитать доступные обучения.') from None

    def customers(self, kind, query=''):
        try:
            return self.connection.execute('SELECT id, full_name FROM counterparties WHERE kind = ? AND instr(contract_fold(full_name), ?) > 0 '
                                           'ORDER BY contract_fold(full_name) LIMIT 100', (kind, query.casefold())).fetchall()
        except sqlite3.Error:
            raise ContractError('Не удалось найти заказчиков.') from None

    def training_labels(self, training_ids):
        try:
            result = []
            for training_id in training_ids:
                row = self.connection.execute(
                    "SELECT t.id, s.surname || ' ' || s.first_name || ' ' || s.patronymic, t.program_snapshot, t.start_date, t.end_date, t.state, t.student_id "
                    'FROM trainings t JOIN students s ON s.id = t.student_id WHERE t.id = ?', (training_id,),
                ).fetchone()
                if row:
                    result.append(row)
            return result
        except sqlite3.Error:
            raise ContractError('Не удалось прочитать выбранные обучения.') from None

    def groups(self):
        try:
            return self.connection.execute('SELECT id, name FROM training_groups ORDER BY contract_fold(name), id').fetchall()
        except sqlite3.Error:
            raise ContractError('Не удалось прочитать группы.') from None

    def customer_name(self, customer_id):
        try:
            return self._entity('counterparties', ('full_name',), customer_id)['full_name']
        except sqlite3.Error:
            raise ContractError('Не удалось прочитать заказчика.') from None
