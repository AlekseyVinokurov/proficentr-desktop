"""Приказы: снимки, архив, ручное подтверждение и локальный пакет документов."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from io import BytesIO
import hashlib
import json
import re
import shutil
import sqlite3
from uuid import uuid4

from docx import Document
from proficentr.contract_repository import ContractRepository, encoded
from proficentr.contracts import number_key
from proficentr.docx_context import SCHEMA, validate_context
from proficentr.docx_engine import generate_document, publish_docx, read_docx, render_docx, validate_package
from proficentr.document_templates import TemplateRepository
from proficentr.enrollment_orders import EnrollmentOrder, KINDS, OrderError
from proficentr.students import is_adult, iso_date
from proficentr.trainings import validate_dates

FIELDS = tuple(asdict(EnrollmentOrder()))
DRAFT_FIELDS = ('order_date', 'kind', 'group_id', 'historical_number', 'note', 'template_id')

@dataclass(repr=False)
class OrderReview:
    order_id: int
    signature: str
    template_id: int
    context: dict
    trainings: list
    roster: list
    contracts: list


class EnrollmentOrderRepository:
    def __init__(self, connection, storage):
        self.connection = connection
        self.storage = storage
        self.templates = TemplateRepository(connection, storage)
        self.contracts = ContractRepository(connection, storage)

    @contextmanager
    def transaction(self):
        if self.connection.in_transaction:
            raise OrderError('Завершите текущую операцию с базой.')
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                yield
        except (sqlite3.Error, OSError):
            raise OrderError('Операция с приказом не выполнена. Изменения не сохранены; проверьте базу и хранилище.') from None

    def _entity(self, table, fields, record_id):
        row = self.connection.execute(
            f"SELECT {', '.join(fields)} FROM {table} WHERE id = ?", (record_id,),  # nosec B608 # Only internal table/field constants, bound ID.
        ).fetchone()
        if row is None:
            raise OrderError('Связанная запись не найдена. Проверьте выбранные сведения.')
        return dict(zip(fields, row))

    def _student(self, student_id):
        fields = tuple(key for key in SCHEMA['student'] if key != 'full_name')
        values = self._entity('students', fields, student_id)
        values['full_name'] = ' '.join(values[key] for key in ('surname', 'first_name', 'patronymic') if values[key])
        return values

    def _advance(self, year, value):
        if value > 0:
            self.connection.execute('INSERT INTO enrollment_order_sequences(year,last_value) VALUES (?,?) '
                                    'ON CONFLICT(year) DO UPDATE SET last_value = max(last_value, excluded.last_value)', (year, value))

    def _check_archive_path(self, path):
        root = self.storage.root
        if not path.resolve().is_relative_to(root.resolve()) or any(parent.is_symlink() for parent in [path, *path.parents] if parent.is_relative_to(root)):
            raise OrderError('В архиве обнаружена символическая ссылка или неверный путь.')

    def verify_document(self, order_id):
        order = self.load(order_id)
        if order.state == 'draft' or not re.fullmatch(r'documents/[0-9]{4}/[0-9]{2}/orders/order-[a-f0-9]{32}\.docx', order.document_path):
            raise OrderError('У приказа нет корректного архивного документа.')
        path = self.storage.root / order.document_path
        self._check_archive_path(path)
        data = read_docx(path)
        if hashlib.sha256(data).hexdigest() != order.document_sha256:
            raise OrderError('SHA-256 приказа не совпадает с реестром. Оригинал повреждён или изменён; автоматическое исправление запрещено.')
        validate_package(data)
        return path

    def document_copy(self, order_id):
        original = self.verify_document(order_id)
        data = read_docx(original)
        if hashlib.sha256(data).hexdigest() != self.load(order_id).document_sha256:
            raise OrderError('Архивный документ изменился.')
        return publish_docx(data, self._exports('order-copies') / ('copy-' + uuid4().hex + '.docx'))

    def _exports(self, category):
        self.storage.require_available()
        folder = self.storage.root / 'exports' / category
        self._check_archive_path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _quarantine(self, path):
        folder = self._exports('unregistered-orders') / uuid4().hex
        folder.mkdir()
        path.rename(folder / path.name)

    def recover_unregistered(self):
        """Under the same writer lock as registration; never adopt/delete an orphan."""
        count = 0
        with self.transaction():
            references = {row[0] for row in self.connection.execute("SELECT document_path FROM enrollment_orders WHERE document_path != ''")}
            documents = self.storage.root / 'documents'
            self._check_archive_path(documents)
            for path in documents.glob('[0-9][0-9][0-9][0-9]/[0-9][0-9]/orders/*'):
                if not (re.fullmatch(r'order-[a-f0-9]{32}\.docx', path.name) or re.fullmatch(r'\.docx-.*\.tmp', path.name)):
                    continue
                self._check_archive_path(path)
                if path.relative_to(self.storage.root).as_posix() not in references:
                    self._quarantine(path)
                    count += 1
        return count

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
            raise OrderError('Не удалось прочитать выбранные обучения.') from None

    def groups(self):
        try:
            return self.connection.execute('SELECT id, name FROM training_groups ORDER BY contract_fold(name), id').fetchall()
        except sqlite3.Error:
            raise OrderError('Не удалось прочитать группы.') from None

    def load(self, order_id):
        return EnrollmentOrder(**self._entity('enrollment_orders', FIELDS, order_id))

    def training_ids(self, order_id):
        return [row[0] for row in self.connection.execute(
            'SELECT training_id FROM enrollment_order_trainings WHERE order_id = ? ORDER BY position', (order_id,))]

    def _trainings(self, order, training_ids, *, required=True):
        if len(training_ids) > 500 or len(set(training_ids)) != len(training_ids):
            raise OrderError('Выберите не более 500 разных обучений.')
        if required and not training_ids:
            raise OrderError('Добавьте существующие подготовленные обучения.')
        if order.kind == 'individual' and len(training_ids) > 1:
            raise OrderError('Индивидуальный приказ включает одно обучение.')
        result = []
        for training_id in training_ids:
            row = self._entity('trainings', ('id','student_id','program_id','group_id','start_date','end_date','state','program_snapshot'), training_id)
            if row['state'] != 'preparation' or self.connection.execute('SELECT 1 FROM training_enrollments WHERE training_id = ?', (training_id,)).fetchone():
                raise OrderError('Выберите обучение в состоянии «Подготовка»: исключённые и зачисленные записи недоступны.')
            if order.kind == 'group' and row['group_id'] != order.group_id:
                raise OrderError('Групповой приказ включает только выбранные обучения одной группы.')
            self._entity('programs', ('id',), row['program_id'])
            validate_dates(row['start_date'], row['end_date'])
            snapshot = json.loads(row['program_snapshot'])
            if set(snapshot) != {'full_name','direction','kind','hours','code','edition','qualification','grade'}:
                raise OrderError('Повреждён снимок программы.')
            if result and (row['program_id'], snapshot) != (result[0]['program_id'], json.loads(result[0]['program_snapshot'])):
                raise OrderError('Нужны одна программа и одинаковый снимок её редакции.')
            student = self._student(row['student_id'])
            if not is_adult(iso_date(student['birth_date'], 'Дата рождения'), iso_date(row['start_date'], 'Дата начала')):
                raise OrderError('На дату начала обучения слушателю должно исполниться 18 лет.')
            result.append(row)
        return result

    def save(self, order, training_ids):
        iso_date(order.order_date, 'Дата приказа')
        if order.kind not in KINDS or (order.kind == 'group' and not order.group_id):
            raise OrderError('Выберите вид приказа и группу для группового приказа.')
        if len(order.historical_number) > 100 or len(order.note) > 10000:
            raise OrderError('Номер или примечание слишком длинные.')
        with self.transaction():
            old = self.load(order.id) if order.id else None
            if order.state != 'draft' or old and (old.state != 'draft' or old.revision != order.revision):
                raise OrderError('Изменять можно только актуальный черновик. Откройте запись заново.')
            if order.group_id:
                self._entity('training_groups', ('id',), order.group_id)
            self._trainings(order, training_ids, required=False)
            values = asdict(order)
            values.update(historical_number=order.historical_number.strip(), note=order.note.strip())
            parameters = tuple(values[key] for key in DRAFT_FIELDS)
            if old:
                order_id = old.id
                self.connection.execute(
                    f"UPDATE enrollment_orders SET {', '.join(key + ' = ?' for key in DRAFT_FIELDS)}, revision = revision + 1 WHERE id = ?",  # nosec B608 # Fixed fields, bound values.
                    (*parameters, order_id))
                self.connection.execute('DELETE FROM enrollment_order_trainings WHERE order_id = ?', (order_id,))
            else:
                order_id = self.connection.execute(
                    f"INSERT INTO enrollment_orders ({', '.join(DRAFT_FIELDS)}) VALUES ({', '.join('?' for _ in DRAFT_FIELDS)})",  # nosec B608 # Fixed fields, bound values.
                    parameters).lastrowid
            self.connection.executemany('INSERT INTO enrollment_order_trainings(order_id,training_id,position) VALUES (?,?,?)',
                                        [(order_id, training_id, position) for position, training_id in enumerate(training_ids)])
        return self.load(order_id)

    def related_contracts(self, training_ids):
        result = []
        for training_id in training_ids:
            rows = self.connection.execute('SELECT c.id,c.number,c.contract_date,c.state FROM contracts c JOIN contract_trainings ct ON ct.contract_id = c.id WHERE ct.training_id = ? ORDER BY c.id', (training_id,)).fetchall()
            result.append({'training_id': training_id, 'contracts': [dict(zip(('id','number','date','state'), row)) for row in rows]})
        return result

    def warnings(self, training_ids):
        result = ['Подписание договоров в программе не учитывается. Ответственный вручную проверяет основания, договоры и применимые требования к образованию.']
        for item in self.related_contracts(training_ids):
            if not any(c['state'] == 'registered' for c in item['contracts']):
                result.append(f"Обучение ID {item['training_id']}: нет зарегистрированного действующего договора.")
        return result

    def _context(self, order, trainings):
        context = {root: dict.fromkeys(fields, '') for root, fields in SCHEMA.items() if root != 'students'}
        context['center'] = self._entity('training_center', tuple(SCHEMA['center']), 1)
        if not context['center']['full_name']:
            raise OrderError('Заполните карточку учебного центра в настройках.')
        students = [dict(self._student(t['student_id']), start_date=t['start_date'], end_date=t['end_date']) for t in trainings]
        context['students'] = students
        if len(students) == 1:
            context['student'] = {key: students[0][key] for key in SCHEMA['student']}
        context['program'] = {('name' if key == 'full_name' else key): value for key, value in json.loads(trainings[0]['program_snapshot']).items()}
        mixed = any(len({t[key] for t in trainings}) > 1 for key in ('start_date','end_date'))
        for key in ('start_date','end_date'):
            context['training'][key] = '' if mixed else trainings[0][key]
        context['training']['state'] = 'Подготовка'
        if order.group_id:
            context['group'] = self._entity('training_groups', tuple(SCHEMA['group']), order.group_id)
            if mixed:
                context['group'].update(start_date='', end_date='')
        context['order'].update(kind=order.kind, student_count=len(students))
        context['document'].update(number='ПРОЕКТ — НЕ ЗАРЕГИСТРИРОВАН', date=order.order_date, is_test=False, is_draft=True)
        return validate_context(context)

    def suitable_templates(self, order, training_ids):
        trainings = self._trainings(order, training_ids)
        prefix = json.loads(trainings[0]['program_snapshot'])['direction'] + '-' + order.kind
        return [t for t in self.templates.list_versions('enrollment_order') if t.active and (t.variant == prefix or t.variant.startswith(prefix + '-'))]

    def _review(self, order_id):
        order = self.load(order_id)
        if order.state != 'draft':
            raise OrderError('Формировать и регистрировать можно только черновик.')
        training_ids = self.training_ids(order_id)
        trainings = self._trainings(order, training_ids)
        context = self._context(order, trainings)
        candidates = self.suitable_templates(order, training_ids)
        template = next((t for t in candidates if t.id == order.template_id), None)
        if template is None and order.template_id is None and len(candidates) == 1:
            template = candidates[0]
        if template is None:
            raise OrderError('Добавьте и активируйте шаблон enrollment_order с вариантом dpo-individual, dpo-group, po-individual или po-group в «Настройки → Шаблоны документов». При нескольких вариантах выберите один.')
        source = self.templates.source(template.id)
        # Mixed periods must be visible per student, never substituted by group dates.
        if any(len({t[key] for t in trainings}) > 1 for key in ('start_date','end_date')):
            from jinja2 import nodes
            from docxtpl import DocxTemplate
            from proficentr.docx_engine import RestrictedEnvironment
            dates = set()
            for name, data in validate_package(read_docx(source.path)).items():
                if not re.fullmatch(r'word/(document|header[0-9]*|footer[0-9]*)\.xml', name):
                    continue
                tree = RestrictedEnvironment(context=context).parse(DocxTemplate.patch_xml(None, data.decode('utf-8')))
                for loop in tree.find_all(nodes.For):
                    if isinstance(loop.iter, nodes.Name) and loop.iter.name == 'students':
                        for output in loop.find_all(nodes.Output):
                            dates.update(n.attr for n in output.find_all(nodes.Getattr) if isinstance(n.node, nodes.Name) and n.node.name == loop.target.name)
            if not {'start_date','end_date'} <= dates:
                raise OrderError('Периоды различаются: шаблон должен выводить start_date и end_date в строках цикла students.')
        roster = [] if not order.group_id else [list(row) for row in self.connection.execute('SELECT id,student_id,state,program_snapshot,start_date,end_date FROM trainings WHERE group_id = ? ORDER BY id', (order.group_id,))]
        contracts = self.related_contracts(training_ids)
        signature = hashlib.sha256(encoded({'draft': asdict(order), 'context': context, 'trainings': trainings,
                                           'roster': roster, 'contracts': contracts, 'template': asdict(template)}).encode()).hexdigest()
        return OrderReview(order_id, signature, template.id, context, trainings, roster, contracts)

    def prepare(self, order_id):
        with self.transaction():
            return self._review(order_id)

    def preview(self, order_id):
        with self.transaction():
            review = self._review(order_id)
            source = self.templates.source(review.template_id)
            data = read_docx(source.path)
            if hashlib.sha256(data).hexdigest() != source.sha256:
                raise OrderError('Шаблон изменился.')
            result = render_docx(data, review.context)
            document = Document(BytesIO(result))
            stamp = document.add_paragraph('ПРОЕКТ ПРИКАЗА — НЕ ЗАРЕГИСТРИРОВАН. Не подтверждает подписание и зачисление.')
            stamp.runs[0].bold = True
            document.element.body.insert(0, stamp._p)
            stream = BytesIO(); document.save(stream)
            path = publish_docx(stream.getvalue(), self._exports('order-drafts') / ('DRAFT-unregistered-' + uuid4().hex + '.docx'))
        return path, review

    def _assign_number(self, order):
        year = iso_date(order.order_date, 'Дата приказа').year
        if order.historical_number:
            number = order.historical_number
            if self.connection.execute('SELECT 1 FROM enrollment_orders WHERE number_key = ?', (number_key(number),)).fetchone():
                raise OrderError('Номер приказа уже зарегистрирован.')
            match = re.fullmatch(r'З-([0-9]{4})-([0-9]{4,9})', number)
            if match:
                self._advance(int(match[1]), int(match[2]))
            return number
        row = self.connection.execute('SELECT last_value FROM enrollment_order_sequences WHERE year = ?', (year,)).fetchone()
        counter = row[0] if row else 0
        while True:
            counter += 1
            number = f'З-{year:04d}-{counter:04d}'
            if not self.connection.execute('SELECT 1 FROM enrollment_orders WHERE number_key = ?', (number_key(number),)).fetchone():
                self._advance(year, counter)
                return number

    def register(self, order_id, signature):
        path = None
        try:
            with self.transaction():
                review = self._review(order_id)
                if signature != review.signature:
                    raise OrderError('Сведения, состав, договоры или шаблон изменились. Сформируйте и проверьте новый проект.')
                order = self.load(order_id)
                number = self._assign_number(order)
                review.context['document'].update(number=number, is_draft=False)
                day = iso_date(order.order_date, 'Дата приказа')
                self._check_archive_path(self.storage.root / 'documents' / f'{day.year:04d}' / f'{day.month:02d}' / 'orders')
                destination = self.storage.document_directory(day, 'orders') / ('order-' + uuid4().hex + '.docx')
                path = generate_document(self.templates.source(review.template_id), review.context, destination)
                snapshot = encoded({'context': review.context, 'trainings': review.trainings, 'group_roster': review.roster,
                                    'contracts': review.contracts, 'template': asdict(self.templates.load(review.template_id))})
                self.connection.execute("UPDATE enrollment_orders SET state = 'registered', number = ?, number_key = ?, registered_at = CURRENT_TIMESTAMP, "
                                        "template_id = ?, document_path = ?, document_sha256 = ?, snapshot = ?, revision = revision + 1 WHERE id = ?",
                                        (number, number_key(number), review.template_id, path.relative_to(self.storage.root).as_posix(), hashlib.sha256(read_docx(path)).hexdigest(), snapshot, order_id))
        except Exception:
            if path is not None:
                self._quarantine(path)
            raise
        return self.load(order_id)

    def confirm_signing(self, order_id, *, signed_date, confirmed_by, grounds_checked, education_checked):
        day = iso_date(signed_date, 'Дата подписания')
        if type(grounds_checked) is not bool or type(education_checked) is not bool or not grounds_checked or not education_checked:
            raise OrderError('Вручную подтвердите проверку оснований/договоров и применимых требований к образованию.')
        confirmed_by = confirmed_by.strip()
        if not confirmed_by or len(confirmed_by) > 500:
            raise OrderError('Укажите ответственного за подтверждение.')
        with self.transaction():
            order = self.load(order_id)
            if order.state != 'registered':
                raise OrderError('Подписание подтверждают только для зарегистрированного приказа. Повторное зачисление запрещено.')
            if day < iso_date(order.order_date, 'Дата приказа') or day > date.today():
                raise OrderError('Дата подписания должна быть между датой приказа и сегодняшней датой.')
            self.verify_document(order_id)
            current = self._trainings(order, self.training_ids(order_id))
            if current != json.loads(order.snapshot)['trainings']:
                raise OrderError('Обучения изменились после регистрации. Требуется новый приказ с актуальными сведениями.')
            self.connection.executemany('INSERT INTO training_enrollments(training_id,order_id) VALUES (?,?)', [(t['id'], order_id) for t in current])
            self.connection.execute("UPDATE enrollment_orders SET state = 'signed', signed_date = ?, confirmed_at = CURRENT_TIMESTAMP, confirmed_by = ?, grounds_checked = 1, education_checked = 1, revision = revision + 1 WHERE id = ?", (signed_date, confirmed_by, order_id))
        return self.load(order_id)

    def available_trainings(self, query='', group_id=None):
        enrolled = {row[0] for row in self.connection.execute('SELECT training_id FROM training_enrollments')}
        return [row for row in self.contracts.available_trainings(query, group_id) if row[0] not in enrolled]

    def search(self, query=''):
        query = ' '.join(query.split()).casefold()
        result = []
        for order_id, in self.connection.execute('SELECT id FROM enrollment_orders ORDER BY order_date DESC,id DESC'):
            order = self.load(order_id)
            if order.state == 'draft':
                names = [row[1] for row in self.training_labels(self.training_ids(order_id))]
                group = self._entity('training_groups', ('name',), order.group_id)['name'] if order.group_id else ''
            else:
                context = json.loads(order.snapshot)['context']
                names = [s['full_name'] for s in context['students']]
                group = context['group']['name']
            if query and query not in ' '.join([order.number or '', order.order_date, group, *names]).casefold():
                continue
            result.append((order.id, order.number or 'Без номера', order.order_date, KINDS[order.kind], group, len(names), order.state))
        return result

    def prepare_package(self, order_id):
        with self.transaction():
            order = self.load(order_id)
            files = [('Приказ.docx', read_docx(self.verify_document(order_id)), order.document_sha256)]
            ids = self.training_ids(order_id)
            contract_ids = {c['id'] for row in self.related_contracts(ids) for c in row['contracts'] if c['state'] == 'registered'}
            for contract_id in sorted(contract_ids):
                contract = self.contracts.load(contract_id)
                files.append((f'Договор-{contract_id}.docx', read_docx(self.contracts.verify_document(contract_id)), contract.document_sha256))
            warnings = self.warnings(ids)
            folder = self._exports('enrollment-packages') / uuid4().hex
            folder.mkdir()
            try:
                for name, data, sha in files:
                    if hashlib.sha256(data).hexdigest() != sha:
                        raise OrderError('Архивный документ изменился во время подготовки пакета.')
                    publish_docx(data, folder / name)
                (folder / 'Прочитайте.txt').write_text('Локальные копии документов. Пакет не является универсальным обязательным комплектом. Согласия автоматически не создаются.\n' + '\n'.join(warnings), encoding='utf-8')
            except Exception:
                shutil.rmtree(folder)
                raise
        return folder, warnings
