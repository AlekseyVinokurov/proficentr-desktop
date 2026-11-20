"""Завершение обучения по конкретному подтверждённому результату аттестации."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from io import BytesIO
import hashlib
import json
import re
import sqlite3
from uuid import uuid4

from docx import Document
from docxtpl import DocxTemplate
from jinja2 import nodes
from proficentr.attestation_repository import AttestationRepository
from proficentr.attestations import RESULTS
from proficentr.completion_orders import CompletionOrder, CompletionEntry, CompletionError, KINDS
from proficentr.contract_repository import encoded
from proficentr.contracts import number_key
from proficentr.docx_context import SCHEMA, validate_context
from proficentr.docx_engine import (RestrictedEnvironment, generate_document, publish_docx,
                                    read_docx, render_docx, validate_package)
from proficentr.document_templates import TemplateRepository
from proficentr.students import iso_date

FIELDS = tuple(CompletionOrder.__dataclass_fields__)
ENTRY_FIELDS = tuple(CompletionEntry.__dataclass_fields__)
DRAFT_FIELDS = ('order_date','kind','group_id','direction','historical_number','note','template_id')


@dataclass(repr=False)
class CompletionReview:
    signature: str
    template_id: int
    context: dict
    trainings: list
    entries: list


class CompletionRepository:
    def __init__(self, connection, storage):
        self.connection = connection
        self.storage = storage
        self.attestations = AttestationRepository(connection, storage)
        self.templates = TemplateRepository(connection, storage)

    @contextmanager
    def transaction(self):
        if self.connection.in_transaction:
            raise CompletionError('Завершите текущую операцию с базой.')
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                yield
        except (sqlite3.Error, OSError):
            raise CompletionError('Операция с приказом о завершении не выполнена. Изменения не сохранены; проверьте базу и хранилище.') from None

    def _entity(self, table, fields, key):
        row = self.connection.execute(
            f"SELECT {', '.join(fields)} FROM {table} WHERE id = ?", (key,),  # nosec B608 # Internal constants only.
        ).fetchone()
        if row is None:
            raise CompletionError('Связанная запись не найдена.')
        return dict(zip(fields, row))

    def load(self, order_id):
        return CompletionOrder(**self._entity('completion_orders', FIELDS, order_id))

    def entries(self, order_id):
        rows = self.connection.execute(
            f"SELECT {', '.join(ENTRY_FIELDS)} FROM completion_order_trainings WHERE order_id = ? ORDER BY position",  # nosec B608 # Fixed fields.
            (order_id,))
        return [CompletionEntry(**dict(zip(ENTRY_FIELDS,row))) for row in rows]

    def training_ids(self, order_id):
        return [entry.training_id for entry in self.entries(order_id)]

    def groups(self):
        return self.attestations.groups()

    def training_labels(self, ids):
        return self.attestations.training_labels(ids)

    def completion(self, training_id):
        return self.connection.execute('SELECT order_id,protocol_id,completion_date,created_at FROM training_completions WHERE training_id=?', (training_id,)).fetchone()

    def available_trainings(self, query='', group_id=None):
        completed = {row[0] for row in self.connection.execute('SELECT training_id FROM training_completions')}
        rows = self.attestations.available_trainings(query, group_id)
        return [(*row, self.attestations.current_confirmed_result(row[0])) for row in rows if row[0] not in completed]

    def candidate_entries(self, ids):
        result = []
        for row in self.training_labels(ids):
            current = self.attestations.current_confirmed_result(row[0])
            if current is None or current[4] != 'passed':
                raise CompletionError('Выберите обучение с актуальным подтверждённым результатом «Успешно прошёл».')
            result.append(CompletionEntry(row[0],row[4],current[0]))
        if len(result) != len(ids):
            raise CompletionError('Выбранное обучение не найдено.')
        return result

    def _eligible(self, order, entries, *, required=True):
        ids = [entry.training_id for entry in entries]
        if len(ids) > 500 or len(set(ids)) != len(ids) or required and not ids:
            raise CompletionError('Выберите от 1 до 500 разных обучений.')
        if order.kind == 'individual' and len(ids) > 1:
            raise CompletionError('Индивидуальный приказ включает одно обучение.')
        trainings, normalized = [], []
        for entry in entries:
            t = self._entity('trainings', ('id','student_id','program_id','group_id','start_date','end_date','state','program_snapshot'), entry.training_id)
            if t['state'] == 'excluded' or not self.connection.execute('SELECT 1 FROM training_enrollments WHERE training_id=?',(t['id'],)).fetchone():
                raise CompletionError('Для завершения требуется подтверждённое зачисление.')
            if self.completion(t['id']):
                raise CompletionError('Обучение уже завершено. Повторное завершение запрещено.')
            if order.kind == 'group' and t['group_id'] != order.group_id:
                raise CompletionError('Групповой приказ включает только выбранные обучения одной группы.')
            program = json.loads(t['program_snapshot'])
            if program['direction'] != order.direction:
                raise CompletionError('Направление приказа не совпадает с программой обучения.')
            if trainings and (t['program_id'],program) != (trainings[0]['program_id'],json.loads(trainings[0]['program_snapshot'])):
                raise CompletionError('Нужны одна программа и одинаковая редакция её снимка.')
            current = self.attestations.current_confirmed_result(t['id'])
            if current is None or current[4] != 'passed':
                raise CompletionError('Нет актуальной подписанной успешной аттестации. Требуется новое оформление или урегулирование результатов.')
            if entry.protocol_id is not None and entry.protocol_id != current[0]:
                raise CompletionError('Актуальное основание аттестации изменилось. Обновите основания черновика; для зарегистрированного приказа требуется новое оформление.')
            protocol = self.attestations.load(current[0])
            if protocol.direction != order.direction:
                raise CompletionError('Направление протокола не соответствует приказу.')
            day = iso_date(entry.completion_date,'Фактическая дата завершения')
            if day < max(iso_date(t['start_date'],'Начало обучения'),iso_date(protocol.attestation_date,'Дата аттестации')):
                raise CompletionError('Фактическая дата завершения не может предшествовать началу обучения и аттестации-основанию.')
            outcome = next(r for r in self.attestations.results(protocol.id) if r.training_id == t['id'])
            basis = encoded({'protocol_id':protocol.id, 'number':protocol.number, 'date':protocol.protocol_date,
                             'attestation_date':protocol.attestation_date, 'kind':protocol.kind, 'direction':protocol.direction,
                             'signed_date':protocol.signed_date, 'confirmed_at':protocol.confirmed_at, 'confirmed_by':protocol.confirmed_by,
                             'result':asdict(outcome)})
            if entry.basis_snapshot != '{}' and entry.basis_snapshot != basis:
                raise CompletionError('Снимок основания не совпадает с подтверждённым результатом. Откройте приказ заново.')
            normalized.append(CompletionEntry(t['id'],entry.completion_date,protocol.id,basis))
            trainings.append(t)
        return trainings,normalized

    def save(self, order, entries):
        iso_date(order.order_date,'Дата приказа')
        if order.direction not in ('dpo','po') or order.kind not in KINDS or (order.kind == 'group') != (order.group_id is not None):
            raise CompletionError('Выберите направление, вид приказа и группу для группового приказа.')
        if len(order.historical_number) > 100 or len(order.note) > 10000:
            raise CompletionError('Номер или примечание слишком длинные.')
        with self.transaction():
            old = self.load(order.id) if order.id else None
            if order.state != 'draft' or old and (old.state != 'draft' or old.revision != order.revision):
                raise CompletionError('Редактировать можно только актуальный черновик. Откройте запись заново.')
            _,entries = self._eligible(order,entries,required=False)
            values = asdict(order)
            values.update(historical_number=order.historical_number.strip(),note=order.note.strip())
            parameters = tuple(values[k] for k in DRAFT_FIELDS)
            if old:
                key = old.id
                self.connection.execute(
                    f"UPDATE completion_orders SET {', '.join(k + '=?' for k in DRAFT_FIELDS)}, revision=revision+1 WHERE id=?",  # nosec B608 # Fixed fields.
                    (*parameters,key))
                self.connection.execute('DELETE FROM completion_order_trainings WHERE order_id=?',(key,))
            else:
                key = self.connection.execute(
                    f"INSERT INTO completion_orders ({', '.join(DRAFT_FIELDS)}) VALUES ({', '.join('?' for _ in DRAFT_FIELDS)})",  # nosec B608 # Fixed fields.
                    parameters).lastrowid
            self.connection.executemany('INSERT INTO completion_order_trainings(order_id,position,training_id,completion_date,protocol_id,basis_snapshot) VALUES (?,?,?,?,?,?)',
                [(key,i,*[getattr(entry,k) for k in ENTRY_FIELDS]) for i,entry in enumerate(entries)])
        return self.load(key)

    def _context(self, order, trainings, entries):
        context = {root:dict.fromkeys(fields,'') for root,fields in SCHEMA.items() if root != 'students'}
        context['center'] = self._entity('training_center',tuple(SCHEMA['center']),1)
        if not context['center']['full_name']:
            raise CompletionError('Заполните карточку учебного центра в настройках.')
        students = []
        for t,entry in zip(trainings,entries):
            fields = tuple(k for k in SCHEMA['student'] if k != 'full_name')
            student = self._entity('students',fields,t['student_id'])
            student['full_name'] = ' '.join(student[k] for k in ('surname','first_name','patronymic') if student[k])
            basis = json.loads(entry.basis_snapshot); outcome = basis['result']
            student.update(start_date=t['start_date'],end_date=t['end_date'],completion_date=entry.completion_date,
                completion_basis=f"{basis['kind']}: протокол {basis['number']} от {basis['date']}",
                protocol_number=basis['number'],protocol_date=basis['date'],attestation_date=basis['attestation_date'],
                attestation_result=RESULTS[outcome['result']],grade=outcome['grade'],qualification=outcome['qualification'],
                qualification_grade=outcome['qualification_grade'],attestation_note=outcome['note'])
            students.append(student)
        context['students'] = students
        if len(students) == 1:
            context['student'] = {k:students[0][k] for k in SCHEMA['student']}
        context['program'] = {('name' if k == 'full_name' else k):v for k,v in json.loads(trainings[0]['program_snapshot']).items()}
        for field in ('start_date','end_date'):
            values = {t[field] for t in trainings}
            context['training'][field] = next(iter(values)) if len(values) == 1 else ''
        context['training']['state'] = 'Зачислен'
        if order.group_id:
            context['group'] = self._entity('training_groups',tuple(SCHEMA['group']),order.group_id)
            context['group'].update({k:context['training'][k] for k in ('start_date','end_date')})
        for field,key in (('date','completion_date'),('basis','completion_basis')):
            values = {s[key] for s in students}
            context['completion'][field] = next(iter(values)) if len(values) == 1 else ''
        if len({e.protocol_id for e in entries}) == 1:
            basis = json.loads(entries[0].basis_snapshot)
            context['protocol'].update(number=basis['number'],date=basis['date'],attestation_date=basis['attestation_date'],kind=basis['kind'])
        context['order'].update(kind=order.kind,student_count=len(students))
        context['document'].update(number='ПРОЕКТ — НЕ ЗАРЕГИСТРИРОВАН',date=order.order_date,is_draft=True,is_test=False)
        return validate_context(context)

    def suitable_templates(self, order):
        variant = order.direction + '-' + order.kind
        return [t for t in self.templates.list_versions('completion_order') if t.active and t.variant == variant]

    def _review(self, order_id):
        order = self.load(order_id)
        if order.state != 'draft':
            raise CompletionError('Формировать и регистрировать можно только черновик.')
        trainings,entries = self._eligible(order,self.entries(order_id))
        context = self._context(order,trainings,entries)
        candidates = self.suitable_templates(order)
        template = next((t for t in candidates if t.id == order.template_id),None)
        if template is None and order.template_id is None and len(candidates) == 1:
            template = candidates[0]
        if template is None:
            raise CompletionError('Добавьте и активируйте шаблон completion_order с вариантом ' + order.direction + '-' + order.kind + ' в «Настройки → Шаблоны документов».')
        source = self.templates.source(template.id)
        if len({e.completion_date for e in entries}) > 1:
            dates = set()
            for name,data in validate_package(read_docx(source.path)).items():
                if not re.fullmatch(r'word/(document|header[0-9]*|footer[0-9]*)\.xml',name): continue
                tree = RestrictedEnvironment(context=context).parse(DocxTemplate.patch_xml(None,data.decode('utf-8')))
                for loop in tree.find_all(nodes.For):
                    if isinstance(loop.iter,nodes.Name) and loop.iter.name == 'students':
                        for output in loop.find_all(nodes.Output):
                            dates.update(n.attr for n in output.find_all(nodes.Getattr) if isinstance(n.node,nodes.Name) and n.node.name == loop.target.name)
            if 'completion_date' not in dates:
                raise CompletionError('Даты завершения различаются: шаблон должен выводить row.completion_date в цикле students.')
        for protocol_id in {e.protocol_id for e in entries}: self.attestations.verify_document(protocol_id)
        entries = [asdict(e) for e in entries]
        signature = hashlib.sha256(encoded({'draft':asdict(order),'context':context,'trainings':trainings,'entries':entries,'template':asdict(template)}).encode()).hexdigest()
        return CompletionReview(signature,template.id,context,trainings,entries)

    def prepare(self, order_id):
        with self.transaction(): return self._review(order_id)

    def preview(self, order_id):
        with self.transaction():
            review = self._review(order_id)
            source = self.templates.source(review.template_id); data = read_docx(source.path)
            if hashlib.sha256(data).hexdigest() != source.sha256: raise CompletionError('Шаблон изменился.')
            document = Document(BytesIO(render_docx(data,review.context)))
            stamp = document.add_paragraph('ПРОЕКТ ПРИКАЗА О ЗАВЕРШЕНИИ — НЕ ЗАРЕГИСТРИРОВАН. Не завершает обучение.')
            document.element.body.insert(0,stamp._p)
            stream = BytesIO(); document.save(stream)
            path = publish_docx(stream.getvalue(),self._exports('completion-drafts') / ('DRAFT-' + uuid4().hex + '.docx'))
        return path,review

    def _advance(self, year, value):
        if value > 0:
            self.connection.execute('INSERT INTO completion_sequences(year,last_value) VALUES (?,max(?,coalesce((SELECT last_value FROM completion_sequences WHERE year=?),0))) ON CONFLICT(year) DO UPDATE SET last_value=excluded.last_value',(year,value,year))

    def _assign_number(self, order):
        year = iso_date(order.order_date,'Дата приказа').year
        if order.historical_number:
            number = order.historical_number
            if self.connection.execute('SELECT 1 FROM completion_orders WHERE number_key=?',(number_key(number),)).fetchone():
                raise CompletionError('Номер приказа о завершении уже зарегистрирован.')
            match = re.fullmatch(r'О-([0-9]{4})-([0-9]{4,9})',number)
            if match: self._advance(int(match[1]),int(match[2]))
            return number
        row = self.connection.execute('SELECT last_value FROM completion_sequences WHERE year=?',(year,)).fetchone()
        counter = row[0] if row else 0
        while True:
            counter += 1; number = f'О-{year:04d}-{counter:04d}'
            if not self.connection.execute('SELECT 1 FROM completion_orders WHERE number_key=?',(number_key(number),)).fetchone():
                self._advance(year,counter); return number

    def register(self, order_id, signature):
        path = None
        try:
            with self.transaction():
                review = self._review(order_id)
                if signature != review.signature:
                    raise CompletionError('Сведения или основание изменились. Проверьте новый проект.')
                order = self.load(order_id); number = self._assign_number(order)
                review.context['document'].update(number=number,is_draft=False)
                day = iso_date(order.order_date,'Дата приказа')
                self._check_path(self.storage.root / 'documents' / f'{day.year:04d}' / f'{day.month:02d}' / 'orders')
                destination = self.storage.document_directory(day,'orders') / ('completion-order-' + uuid4().hex + '.docx')
                path = generate_document(self.templates.source(review.template_id),review.context,destination)
                snapshot = encoded({'context':review.context,'trainings':review.trainings,'entries':review.entries,'template':asdict(self.templates.load(review.template_id))})
                self.connection.execute("UPDATE completion_orders SET state='registered',number=?,number_key=?,registered_at=CURRENT_TIMESTAMP,template_id=?,document_path=?,document_sha256=?,snapshot=?,revision=revision+1 WHERE id=?",
                    (number,number_key(number),review.template_id,path.relative_to(self.storage.root).as_posix(),hashlib.sha256(read_docx(path)).hexdigest(),snapshot,order_id))
        except Exception:
            if path is not None: self._quarantine(path)
            raise
        return self.load(order_id)

    def confirm_signing(self, order_id, *, signed_date, confirmed_by, conditions_checked):
        day = iso_date(signed_date,'Дата подписания'); confirmed_by = confirmed_by.strip()
        if conditions_checked is not True or not confirmed_by or len(confirmed_by) > 500:
            raise CompletionError('Укажите ответственного и явно подтвердите проверку остальных необходимых условий завершения.')
        with self.transaction():
            order = self.load(order_id)
            if order.state != 'registered': raise CompletionError('Подписание подтверждают только для зарегистрированного приказа.')
            if day < iso_date(order.order_date,'Дата приказа') or day > date.today():
                raise CompletionError('Дата подписания должна быть между датой приказа и сегодняшней датой.')
            trainings,entries = self._eligible(order,self.entries(order_id))
            if any(day < iso_date(e.completion_date,'Фактическая дата завершения') for e in entries):
                raise CompletionError('Дата подписания не может предшествовать фактическому завершению.')
            self.verify_document(order_id)
            for protocol_id in {e.protocol_id for e in entries}: self.attestations.verify_document(protocol_id)
            self.connection.executemany('INSERT INTO training_completions(training_id,order_id,protocol_id,completion_date) VALUES (?,?,?,?)',
                                       [(e.training_id,order_id,e.protocol_id,e.completion_date) for e in entries])
            self.connection.execute("UPDATE completion_orders SET state='signed',signed_date=?,confirmed_at=CURRENT_TIMESTAMP,confirmed_by=?,conditions_checked=1,revision=revision+1 WHERE id=?",(signed_date,confirmed_by,order_id))
        return self.load(order_id)

    def search(self, query=''):
        query = ' '.join(query.split()).casefold(); found = []
        for key, in self.connection.execute('SELECT id FROM completion_orders ORDER BY order_date DESC,id DESC'):
            order = self.load(key)
            if order.state == 'draft':
                labels = self.training_labels(self.training_ids(key)); names = ', '.join(row[1] for row in labels)
                group = self._entity('training_groups',('name',),order.group_id)['name'] if order.group_id else ''
                program = json.loads(labels[0][2])['full_name'] if labels else ''
            else:
                c = json.loads(order.snapshot)['context']; names = ', '.join(s['full_name'] for s in c['students'])
                group = c['group']['name']; program = c['program']['name']
            row = (order.id,order.number or 'Без номера',order.order_date,KINDS[order.kind],group,program,names,order.state)
            if not query or query in ' '.join(str(v) for v in row[1:]).casefold(): found.append(row)
        return found

    def _check_path(self, path):
        root = self.storage.root
        if not path.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in [path,*path.parents] if p.is_relative_to(root)):
            raise CompletionError('Неверный путь архива или символическая ссылка.')

    def _exports(self, category):
        self.storage.require_available(); folder = self.storage.root / 'exports' / category
        self._check_path(folder); folder.mkdir(parents=True,exist_ok=True); return folder

    def _quarantine(self, path):
        folder = self._exports('unregistered-completion-orders') / uuid4().hex
        folder.mkdir(); path.rename(folder / path.name)

    def recover_unregistered(self):
        count = 0
        with self.transaction():
            references = {row[0] for row in self.connection.execute("SELECT document_path FROM completion_orders WHERE document_path != ''")}
            self._check_path(self.storage.root / 'documents')
            for path in (self.storage.root / 'documents').glob('[0-9][0-9][0-9][0-9]/[0-9][0-9]/orders/*'):
                if not (re.fullmatch(r'completion-order-[a-f0-9]{32}\.docx',path.name) or re.fullmatch(r'\.docx-.*\.tmp',path.name)): continue
                self._check_path(path)
                if path.relative_to(self.storage.root).as_posix() not in references: self._quarantine(path); count += 1
        return count

    def verify_document(self, order_id):
        order = self.load(order_id)
        if order.state == 'draft' or not re.fullmatch(r'documents/[0-9]{4}/[0-9]{2}/orders/completion-order-[a-f0-9]{32}\.docx',order.document_path):
            raise CompletionError('У приказа нет корректного архивного документа.')
        path = self.storage.root / order.document_path; self._check_path(path); data = read_docx(path)
        if hashlib.sha256(data).hexdigest() != order.document_sha256:
            raise CompletionError('SHA-256 приказа не совпадает с реестром. Оригинал повреждён или изменён.')
        validate_package(data); return path

    def document_copy(self, order_id):
        data = read_docx(self.verify_document(order_id))
        if hashlib.sha256(data).hexdigest() != self.load(order_id).document_sha256: raise CompletionError('Архивный документ изменился.')
        return publish_docx(data,self._exports('completion-copies') / ('copy-' + uuid4().hex + '.docx'))
