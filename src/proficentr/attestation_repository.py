"""Протоколы и история результатов; регистрация не подтверждает подписание."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date
from io import BytesIO
import hashlib
import json
import re
import sqlite3
from uuid import uuid4

from docx import Document
from proficentr.attestations import AttestationError, AttestationProtocol, AttestationResult, KINDS, RESULTS
from proficentr.contract_repository import ContractRepository, encoded
from proficentr.contracts import number_key
from proficentr.docx_context import SCHEMA, validate_context
from proficentr.docx_engine import generate_document, publish_docx, read_docx, render_docx, validate_package
from proficentr.document_templates import TemplateRepository
from proficentr.students import iso_date

FIELDS = tuple(asdict(AttestationProtocol()))
DRAFT_FIELDS = ('protocol_date', 'attestation_date', 'direction', 'kind', 'group_id', 'historical_number', 'chair', 'members', 'note', 'template_id')
RESULT_FIELDS = tuple(AttestationResult.__dataclass_fields__)


@dataclass(repr=False)
class ProtocolReview:
    signature: str
    template_id: int
    context: dict
    trainings: list
    results: list


class AttestationRepository:
    def __init__(self, connection, storage):
        self.connection = connection
        self.storage = storage
        self.templates = TemplateRepository(connection, storage)
        self._catalog = ContractRepository(connection, storage)

    @contextmanager
    def transaction(self):
        if self.connection.in_transaction:
            raise AttestationError('Завершите текущую операцию с базой.')
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                yield
        except (sqlite3.Error, OSError):
            raise AttestationError('Операция с протоколом не выполнена. Изменения не сохранены; проверьте базу и хранилище.') from None

    def _entity(self, table, fields, key):
        row = self.connection.execute(
            f"SELECT {', '.join(fields)} FROM {table} WHERE id = ?", (key,),  # nosec B608 # Internal table and field constants only.
        ).fetchone()
        if row is None:
            raise AttestationError('Связанная запись не найдена.')
        return dict(zip(fields, row))

    def load(self, protocol_id):
        return AttestationProtocol(**self._entity('attestation_protocols', FIELDS, protocol_id))

    def results(self, protocol_id):
        rows = self.connection.execute(
            f"SELECT {', '.join(RESULT_FIELDS)} FROM attestation_results WHERE protocol_id = ? ORDER BY position",  # nosec B608 # Fixed fields.
            (protocol_id,))
        return [AttestationResult(**dict(zip(RESULT_FIELDS, row))) for row in rows]

    def training_ids(self, protocol_id):
        return [r.training_id for r in self.results(protocol_id)]

    def groups(self):
        return self._catalog.groups()

    def training_labels(self, ids):
        return self._catalog.training_labels(ids)

    def available_trainings(self, query='', group_id=None):
        enrolled = {row[0] for row in self.connection.execute('SELECT training_id FROM training_enrollments')}
        return [row for row in self._catalog.available_trainings(query, group_id) if row[0] in enrolled]

    def _trainings(self, protocol, results, *, required=True):
        ids = [r.training_id for r in results]
        if len(ids) > 500 or len(set(ids)) != len(ids) or required and not ids:
            raise AttestationError('Выберите от 1 до 500 разных зачисленных обучений.')
        if protocol.group_id is None and len(ids) > 1:
            raise AttestationError('Индивидуальный протокол включает одно обучение; для группового выберите группу.')
        trainings = []
        for key in ids:
            training = self._entity('trainings', ('id', 'student_id', 'program_id', 'group_id', 'start_date', 'end_date', 'state', 'program_snapshot'), key)
            if training['state'] == 'excluded' or not self.connection.execute('SELECT 1 FROM training_enrollments WHERE training_id = ?', (key,)).fetchone():
                raise AttestationError('Аттестация доступна только для зачисленных обучений; исключённые записи недоступны.')
            if protocol.group_id is not None and training['group_id'] != protocol.group_id:
                raise AttestationError('Выберите обучения одной указанной группы.')
            snapshot = json.loads(training['program_snapshot'])
            if snapshot['direction'] != protocol.direction:
                raise AttestationError('Направление протокола не совпадает с направлением программы.')
            if trainings and (training['program_id'], snapshot) != (trainings[0]['program_id'], json.loads(trainings[0]['program_snapshot'])):
                raise AttestationError('Нужны одна программа и совместимая редакция её снимка.')
            trainings.append(training)
        return trainings

    def save(self, protocol, results):
        iso_date(protocol.protocol_date, 'Дата протокола')
        iso_date(protocol.attestation_date, 'Дата аттестации')
        if protocol.direction not in KINDS:
            raise AttestationError('Выберите направление ДПО или ПО.')
        protocol = replace(protocol, kind=KINDS[protocol.direction])
        for key in ('historical_number', 'chair', 'members', 'note'):
            value = getattr(protocol, key)
            if type(value) is not str or len(value) > (100 if key == 'historical_number' else 10000):
                raise AttestationError('Неверная длина реквизитов протокола.')
            setattr(protocol, key, value.strip())
        for result in results:
            if result.result not in RESULTS or any(type(getattr(result, key)) is not str or len(getattr(result, key)) > 10000 for key in RESULT_FIELDS if key != 'training_id'):
                raise AttestationError('Проверьте индивидуальные результаты и длину полей.')
        with self.transaction():
            old = self.load(protocol.id) if protocol.id else None
            if protocol.state != 'draft' or old and (old.state != 'draft' or old.revision != protocol.revision):
                raise AttestationError('Изменять можно только актуальный черновик. Откройте запись заново.')
            self._trainings(protocol, results, required=False)
            values = tuple(getattr(protocol, key) for key in DRAFT_FIELDS)
            if old:
                key = old.id
                self.connection.execute(
                    f"UPDATE attestation_protocols SET {', '.join(k + ' = ?' for k in DRAFT_FIELDS)}, revision = revision + 1 WHERE id = ?",  # nosec B608 # Fixed fields.
                    (*values, key))
                self.connection.execute('DELETE FROM attestation_results WHERE protocol_id = ?', (key,))
            else:
                key = self.connection.execute(
                    f"INSERT INTO attestation_protocols ({', '.join(DRAFT_FIELDS)}) VALUES ({', '.join('?' for _ in DRAFT_FIELDS)})",  # nosec B608 # Fixed fields.
                    values).lastrowid
            self.connection.executemany('INSERT INTO attestation_results(protocol_id,position,training_id,result,grade,qualification,qualification_grade,note) VALUES (?,?,?,?,?,?,?,?)',
                [(key, i, *[getattr(r, field) for field in RESULT_FIELDS]) for i, r in enumerate(results)])
        return self.load(key)

    def suitable_templates(self, protocol, ids=None):
        return [t for t in self.templates.list_versions('protocol_' + protocol.direction) if t.active]

    def _context(self, protocol, trainings, results):
        context = {root: dict.fromkeys(fields, '') for root, fields in SCHEMA.items() if root != 'students'}
        context['center'] = self._entity('training_center', tuple(SCHEMA['center']), 1)
        if not context['center']['full_name']:
            raise AttestationError('Заполните карточку учебного центра в настройках.')
        students = []
        for t, result in zip(trainings, results):
            fields = tuple(k for k in SCHEMA['student'] if k != 'full_name')
            student = self._entity('students', fields, t['student_id'])
            student['full_name'] = ' '.join(student[k] for k in ('surname', 'first_name', 'patronymic') if student[k])
            student.update(start_date=t['start_date'], end_date=t['end_date'], attestation_result=RESULTS[result.result] if result.result else '',
                           grade=result.grade, qualification=result.qualification, qualification_grade=result.qualification_grade, attestation_note=result.note)
            students.append(student)
        context['students'] = students
        if len(students) == 1:
            context['student'] = {k: students[0][k] for k in SCHEMA['student']}
        context['program'] = {('name' if k == 'full_name' else k): v for k, v in json.loads(trainings[0]['program_snapshot']).items() if k not in ('dpo_area', 'dpo_group', 'po_profession')}
        for field in ('start_date', 'end_date'):
            values = {t[field] for t in trainings}
            context['training'][field] = next(iter(values)) if len(values) == 1 else ''
        context['training']['state'] = 'Зачислен'
        if protocol.group_id:
            context['group'] = self._entity('training_groups', tuple(SCHEMA['group']), protocol.group_id)
            context['group'].update({k: context['training'][k] for k in ('start_date', 'end_date')})
        context['protocol'].update(number='ПРОЕКТ — НЕ ЗАРЕГИСТРИРОВАН', date=protocol.protocol_date, attestation_date=protocol.attestation_date,
                                   kind=protocol.kind, chair=protocol.chair, members=protocol.members, note=protocol.note)
        context['document'].update(number=context['protocol']['number'], date=protocol.protocol_date, is_test=False, is_draft=True)
        return validate_context(context)

    def _review(self, protocol_id):
        protocol = self.load(protocol_id)
        if protocol.state != 'draft':
            raise AttestationError('Формировать и регистрировать можно только черновик.')
        results = self.results(protocol_id)
        trainings = self._trainings(protocol, results)
        context = self._context(protocol, trainings, results)
        candidates = self.suitable_templates(protocol)
        template = next((t for t in candidates if t.id == protocol.template_id), None)
        if template is None and protocol.template_id is None and len(candidates) == 1:
            template = candidates[0]
        if template is None:
            raise AttestationError('Загрузите и активируйте соответствующий шаблон protocol_' + protocol.direction + ' через «Настройки → Шаблоны документов». При нескольких вариантах выберите один.')
        self.templates.source(template.id)
        results = [asdict(r) for r in results]
        signature = hashlib.sha256(encoded({'draft': asdict(protocol), 'context': context, 'trainings': trainings, 'results': results, 'template': asdict(template)}).encode()).hexdigest()
        return ProtocolReview(signature, template.id, context, trainings, results)

    def prepare(self, protocol_id):
        with self.transaction():
            return self._review(protocol_id)

    def preview(self, protocol_id):
        with self.transaction():
            review = self._review(protocol_id)
            data = render_docx(read_docx(self.templates.source(review.template_id).path), review.context)
            # A draft is visibly marked even if the uploaded form omits document.is_draft.
            document = Document(BytesIO(data))
            stamp = document.add_paragraph('ПРОЕКТ ПРОТОКОЛА — НЕ ЗАРЕГИСТРИРОВАН. Результаты не подтверждены.')
            document.element.body.insert(0, stamp._p)
            stream = BytesIO(); document.save(stream)
            path = publish_docx(stream.getvalue(), self._exports('protocol-drafts') / ('DRAFT-marked-' + uuid4().hex + '.docx'))
        return path, review

    def _advance(self, year, value):
        if value > 0:
            self.connection.execute('INSERT INTO attestation_sequences(year,last_value) VALUES (?,max(?,coalesce((SELECT last_value FROM attestation_sequences WHERE year=?),0))) ON CONFLICT(year) DO UPDATE SET last_value = excluded.last_value', (year, value, year))

    def _assign_number(self, protocol):
        year = iso_date(protocol.protocol_date, 'Дата протокола').year
        if protocol.historical_number:
            number = protocol.historical_number
            if self.connection.execute('SELECT 1 FROM attestation_protocols WHERE number_key = ?', (number_key(number),)).fetchone():
                raise AttestationError('Номер протокола уже зарегистрирован.')
            match = re.fullmatch(r'П-([0-9]{4})-([0-9]{4,9})', number)
            if match:
                self._advance(int(match[1]), int(match[2]))
            return number
        row = self.connection.execute('SELECT last_value FROM attestation_sequences WHERE year = ?', (year,)).fetchone()
        counter = row[0] if row else 0
        while True:
            counter += 1
            number = f'П-{year:04d}-{counter:04d}'
            if not self.connection.execute('SELECT 1 FROM attestation_protocols WHERE number_key = ?', (number_key(number),)).fetchone():
                self._advance(year, counter)
                return number

    def register(self, protocol_id, signature):
        path = None
        try:
            with self.transaction():
                review = self._review(protocol_id)
                if review.signature != signature:
                    raise AttestationError('Сведения или шаблон изменились. Проверьте новый проект.')
                protocol = self.load(protocol_id)
                number = self._assign_number(protocol)
                review.context['protocol']['number'] = number
                review.context['document'].update(number=number, is_draft=False)
                day = iso_date(protocol.protocol_date, 'Дата протокола')
                self._check_path(self.storage.root / 'documents' / f'{day.year:04d}' / f'{day.month:02d}' / 'protocols')
                destination = self.storage.document_directory(day, 'protocols') / ('protocol-' + uuid4().hex + '.docx')
                path = generate_document(self.templates.source(review.template_id), review.context, destination)
                snapshot = encoded({'context': review.context, 'trainings': review.trainings, 'results': review.results,
                                    'template': asdict(self.templates.load(review.template_id))})
                self.connection.execute("UPDATE attestation_protocols SET state='registered', number=?, number_key=?, registered_at=CURRENT_TIMESTAMP, template_id=?, document_path=?, document_sha256=?, snapshot=?, revision=revision+1 WHERE id=?",
                    (number, number_key(number), review.template_id, path.relative_to(self.storage.root).as_posix(), hashlib.sha256(read_docx(path)).hexdigest(), snapshot, protocol_id))
        except Exception:
            if path is not None:
                self._quarantine(path)
            raise
        return self.load(protocol_id)

    def confirm_signing(self, protocol_id, *, signed_date, confirmed_by):
        day = iso_date(signed_date, 'Дата подписания')
        confirmed_by = confirmed_by.strip()
        if not confirmed_by or len(confirmed_by) > 500:
            raise AttestationError('Укажите ответственного за подтверждение.')
        with self.transaction():
            protocol = self.load(protocol_id)
            if protocol.state != 'registered':
                raise AttestationError('Подписание подтверждают только для зарегистрированного протокола.')
            if day < max(iso_date(protocol.protocol_date, 'Дата протокола'), iso_date(protocol.attestation_date, 'Дата аттестации')) or day > date.today():
                raise AttestationError('Дата подписания должна быть не раньше протокола и аттестации и не позже сегодняшней даты.')
            results = self.results(protocol_id)
            if not results or any(not r.result for r in results):
                raise AttestationError('Для каждого включённого слушателя должен быть указан результат. Оформите новый полный протокол.')
            self.verify_document(protocol_id)
            self.connection.execute("UPDATE attestation_protocols SET state='signed', signed_date=?, confirmed_at=CURRENT_TIMESTAMP, confirmed_by=?, revision=revision+1 WHERE id=?", (signed_date, confirmed_by, protocol_id))
        return self.load(protocol_id)

    def history(self, training_id):
        return self.connection.execute('SELECT p.id,p.attestation_date,p.number,p.state,r.result,r.grade,r.qualification,r.qualification_grade,r.note FROM attestation_protocols p JOIN attestation_results r ON r.protocol_id=p.id WHERE r.training_id=? ORDER BY p.attestation_date DESC,p.id DESC', (training_id,)).fetchall()

    def current_confirmed_result(self, training_id):
        """Последняя дата аттестации, затем наибольший постоянный ID; не время подтверждения."""
        return next((row for row in self.history(training_id) if row[3] == 'signed'), None)

    def search(self, query=''):
        query = ' '.join(query.split()).casefold()
        found = []
        for key, in self.connection.execute('SELECT id FROM attestation_protocols ORDER BY protocol_date DESC,id DESC'):
            p = self.load(key)
            if p.state == 'draft':
                labels = self.training_labels(self.training_ids(key))
                names = ', '.join(row[1] for row in labels)
                program = json.loads(labels[0][2])['full_name'] if labels else ''
                outcomes = ', '.join(RESULTS[r.result] for r in self.results(key))
            else:
                context = json.loads(p.snapshot)['context']
                names = ', '.join(s['full_name'] for s in context['students'])
                program = context['program']['name']
                outcomes = ', '.join(s['attestation_result'] or RESULTS[''] for s in context['students'])
            row = (p.id, p.number or 'Без номера', p.protocol_date, p.attestation_date, p.kind, program, names, outcomes, p.state)
            if not query or query in ' '.join(str(v) for v in row[1:]).casefold():
                found.append(row)
        return found

    def _check_path(self, path):
        root = self.storage.root
        if not path.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(root)):
            raise AttestationError('Неверный путь архива или символическая ссылка.')

    def _exports(self, category):
        self.storage.require_available()
        folder = self.storage.root / 'exports' / category
        self._check_path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _quarantine(self, path):
        folder = self._exports('unregistered-protocols') / uuid4().hex
        folder.mkdir()
        path.rename(folder / path.name)

    def recover_unregistered(self):
        count = 0
        with self.transaction():
            references = {row[0] for row in self.connection.execute("SELECT document_path FROM attestation_protocols WHERE document_path != ''")}
            self._check_path(self.storage.root / 'documents')
            for path in (self.storage.root / 'documents').glob('[0-9][0-9][0-9][0-9]/[0-9][0-9]/protocols/*'):
                if not (re.fullmatch(r'protocol-[a-f0-9]{32}\.docx', path.name) or re.fullmatch(r'\.docx-.*\.tmp', path.name)):
                    continue
                self._check_path(path)
                if path.relative_to(self.storage.root).as_posix() not in references:
                    self._quarantine(path); count += 1
        return count

    def verify_document(self, protocol_id):
        p = self.load(protocol_id)
        if p.state == 'draft' or not re.fullmatch(r'documents/[0-9]{4}/[0-9]{2}/protocols/protocol-[a-f0-9]{32}\.docx', p.document_path):
            raise AttestationError('У протокола нет корректного архивного документа.')
        path = self.storage.root / p.document_path
        self._check_path(path)
        data = read_docx(path)
        if hashlib.sha256(data).hexdigest() != p.document_sha256:
            raise AttestationError('SHA-256 протокола не совпадает с реестром. Оригинал повреждён или изменён.')
        validate_package(data)
        return path

    def document_copy(self, protocol_id):
        data = read_docx(self.verify_document(protocol_id))
        if hashlib.sha256(data).hexdigest() != self.load(protocol_id).document_sha256:
            raise AttestationError('Архивный документ изменился.')
        return publish_docx(data, self._exports('protocol-copies') / ('copy-' + uuid4().hex + '.docx'))
