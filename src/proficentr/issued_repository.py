"""Оформление по историческому завершению; передача учитывается отдельно."""
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
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from proficentr.completion_repository import CompletionRepository
from proficentr.contract_repository import encoded
from proficentr.contracts import number_key
from proficentr.docx_context import SCHEMA, validate_context
from proficentr.docx_engine import generate_document, publish_docx, read_docx, render_docx, validate_package
from proficentr.document_templates import TemplateRepository
from proficentr.issued_documents import IssuedDocument, IssuanceError, KINDS, PREFIXES, document_kind
from proficentr.students import iso_date

FIELDS = tuple(IssuedDocument.__dataclass_fields__)
DATA_FIELDS = tuple(k for k in FIELDS if k not in ('id','state','number','number_key','blank_key','document_path','document_sha256','snapshot','created_at','registered_at','revision'))


@dataclass(repr=False)
class IssueReview:
    signature: str
    template_id: int
    context: dict
    basis: dict


class IssuedDocumentRepository:
    def __init__(self, connection, storage):
        self.connection = connection; self.storage = storage
        self.completions = CompletionRepository(connection,storage)
        self.templates = TemplateRepository(connection,storage)

    @contextmanager
    def transaction(self):
        if self.connection.in_transaction: raise IssuanceError('Завершите текущую операцию с базой.')
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE'); yield
        except (sqlite3.Error,OSError):
            raise IssuanceError('Операция с выпускным документом не выполнена. Изменения не сохранены; проверьте базу и хранилище.') from None

    def load(self, key):
        row = self.connection.execute(
            f"SELECT {', '.join(FIELDS)} FROM issued_documents WHERE id=?",(key,),  # nosec B608 # Fixed fields.
        ).fetchone()
        if row is None: raise IssuanceError('Выпускной документ не найден.')
        return IssuedDocument(**dict(zip(FIELDS,row)))

    def registered_for(self, training_id):
        row = self.connection.execute("SELECT id FROM issued_documents WHERE training_id=? AND state='registered'",(training_id,)).fetchone()
        return self.load(row[0]) if row else None

    def _source(self, training_id, *, verify=False):
        fact = self.completions.completion(training_id)
        row = self.connection.execute('SELECT t.student_id FROM trainings t JOIN students s ON s.id=t.student_id JOIN training_enrollments e ON e.training_id=t.id WHERE t.id=?',(training_id,)).fetchone()
        if not fact or not row: raise IssuanceError('Требуется существующий слушатель, подтверждённое зачисление и завершение обучения.')
        order = self.completions.load(fact[0])
        if order.state != 'signed': raise IssuanceError('Приказ о завершении должен быть подписан.')
        snapshot = json.loads(order.snapshot)
        index = next((i for i,t in enumerate(snapshot['trainings']) if t['id']==training_id),None)
        if index is None: raise IssuanceError('Обучение отсутствует в историческом составе приказа.')
        student = snapshot['context']['students'][index]
        entry = next(e for e in snapshot['entries'] if e['training_id']==training_id)
        basis = json.loads(entry['basis_snapshot'])
        protocol = self.completions.attestations.load(fact[1])
        if protocol.state!='signed' or entry['protocol_id']!=fact[1] or basis['result']['result']!='passed' or entry['completion_date']!=fact[2]:
            raise IssuanceError('Повреждено историческое основание завершения.')
        if verify:
            self.completions.verify_document(order.id); self.completions.attestations.verify_document(protocol.id)
        c = snapshot['context']; program = c['program']; kind = document_kind(program['direction'],program['kind'])
        values = dict(training_id=training_id,kind=kind,full_name=student['full_name'],program_name=program['name'],hours=program['hours'],
            direction=program['direction'],program_kind=program['kind'],qualification=student['qualification'],qualification_grade=student['qualification_grade'],
            completion_date=fact[2],completion_order_id=order.id,completion_order_number=order.number,completion_order_date=order.order_date)
        context = {root:dict.fromkeys(fields,'') for root,fields in SCHEMA.items() if root!='students'}
        for root in ('center','program','group'): context[root]=dict(c[root])
        context['student']={k:student[k] for k in SCHEMA['student']}
        context['students']=[dict(student)]
        context['training'].update(start_date=student['start_date'],end_date=student['end_date'],state='Завершён')
        context['completion'].update(date=fact[2],basis=student['completion_basis'])
        context['protocol'].update(number=basis['number'],date=basis['date'],attestation_date=basis['attestation_date'],kind=basis['kind'])
        return values,context,{'completion':list(fact),'completion_order':asdict(order),'entry':entry,'training':snapshot['trainings'][index]}

    def save(self, document):
        day = iso_date(document.document_date,'Дата выпускного документа')
        if any(type(getattr(document,k)) is not str or len(getattr(document,k))>100 for k in ('historical_number','blank_series','blank_number')):
            raise IssuanceError('Номер и реквизиты бланка должны содержать не более 100 символов.')
        with self.transaction():
            old=self.load(document.id) if document.id else None
            if document.state!='draft' or old and (old.state!='draft' or old.revision!=document.revision or old.training_id!=document.training_id):
                raise IssuanceError('Редактировать можно только актуальный черновик без изменения обучения.')
            if self.registered_for(document.training_id): raise IssuanceError('Основной документ для этого обучения уже зарегистрирован.')
            values,_,_=self._source(document.training_id)
            if day < iso_date(values['completion_date'],'Дата завершения'): raise IssuanceError('Дата документа не может предшествовать фактическому завершению.')
            document=replace(document,**values)
            for k in ('historical_number','blank_series','blank_number'): setattr(document,k,getattr(document,k).strip())
            args=tuple(getattr(document,k) for k in DATA_FIELDS)
            if old:
                key=old.id
                self.connection.execute(
                    f"UPDATE issued_documents SET {', '.join(k+'=?' for k in DATA_FIELDS)},revision=revision+1 WHERE id=?",(*args,key),  # nosec B608 # Fixed fields.
                )
            else:
                key=self.connection.execute(
                    f"INSERT INTO issued_documents ({', '.join(DATA_FIELDS)}) VALUES ({', '.join('?' for _ in DATA_FIELDS)})",args,  # nosec B608 # Fixed fields.
                ).lastrowid
        return self.load(key)

    def draft_for(self, training_id):
        registered=self.registered_for(training_id)
        if registered: return registered
        row=self.connection.execute("SELECT id FROM issued_documents WHERE training_id=? AND state='draft' ORDER BY id DESC LIMIT 1",(training_id,)).fetchone()
        if row: return self.load(row[0])
        values,_,_=self._source(training_id)
        return self.save(IssuedDocument(training_id=training_id,document_date=values['completion_date']))

    def candidates(self, group_id=None, *, pending_only=False):
        found=[]
        for key,group in self.connection.execute('SELECT t.id,t.group_id FROM trainings t JOIN training_completions c ON c.training_id=t.id ORDER BY t.id'):
            if group_id is not None and group!=group_id: continue
            values,_,_=self._source(key); registered=self.registered_for(key)
            if pending_only and registered: continue
            drafts=self.connection.execute("SELECT id FROM issued_documents WHERE training_id=? AND state='draft' ORDER BY id DESC LIMIT 1",(key,)).fetchone()
            found.append((key,values['full_name'],KINDS[values['kind']],values['program_name'],values['completion_date'],
                          registered.number if registered else 'Черновик' if drafts else 'Не оформлен',registered.id if registered else None))
        return found

    def suitable_templates(self, document): return [t for t in self.templates.list_versions(document.kind) if t.active]

    def _review(self, key):
        d=self.load(key)
        if d.state!='draft' or self.registered_for(d.training_id): raise IssuanceError('Для завершённого обучения уже есть основной документ либо выбран не черновик.')
        values,context,basis=self._source(d.training_id,verify=True)
        if any(getattr(d,k)!=v for k,v in values.items()): raise IssuanceError('Реквизиты черновика не соответствуют историческому завершению.')
        versions=self.suitable_templates(d); t=next((t for t in versions if t.id==d.template_id),None)
        if t is None and d.template_id is None and len(versions)==1: t=versions[0]
        if t is None: raise IssuanceError('Загрузите и активируйте форму '+d.kind+' через «Настройки → Шаблоны документов». При нескольких вариантах выберите один.')
        self.templates.source(t.id)
        context['issued'].update(kind=KINDS[d.kind],number='ПРОЕКТ — НЕ ЗАРЕГИСТРИРОВАН',date=d.document_date,blank_series=d.blank_series,blank_number=d.blank_number,
            completion_date=d.completion_date,completion_order_number=d.completion_order_number,completion_order_date=d.completion_order_date,
            qualification=d.qualification,qualification_grade=d.qualification_grade)
        context['document'].update(number=context['issued']['number'],date=d.document_date,is_draft=True,is_test=False)
        context=validate_context(context)
        signature=hashlib.sha256(encoded({'draft':asdict(d),'context':context,'basis':basis,'template':asdict(t)}).encode()).hexdigest()
        return IssueReview(signature,t.id,context,basis)

    def prepare(self, key):
        with self.transaction(): return self._review(key)

    def preview(self, key):
        with self.transaction():
            review=self._review(key); source=self.templates.source(review.template_id); data=read_docx(source.path)
            if hashlib.sha256(data).hexdigest()!=source.sha256: raise IssuanceError('Шаблон изменился.')
            doc=Document(BytesIO(render_docx(data,review.context)))
            stamp=doc.add_paragraph('ПРОЕКТ ВЫПУСКНОГО ДОКУМЕНТА — НЕ ЗАРЕГИСТРИРОВАН.');doc.element.body.insert(0,stamp._p)
            stream=BytesIO();doc.save(stream)
            path=publish_docx(stream.getvalue(),self._exports('issued-drafts')/('DRAFT-'+uuid4().hex+'.docx'))
        return path,review

    def _advance(self, kind, year, value):
        if value>0:
            self.connection.execute('INSERT INTO issued_sequences(kind,year,last_value) VALUES (?,?,max(?,coalesce((SELECT last_value FROM issued_sequences WHERE kind=? AND year=?),0))) ON CONFLICT(kind,year) DO UPDATE SET last_value=excluded.last_value',(kind,year,value,kind,year))

    def _assign_number(self, d):
        year=iso_date(d.document_date,'Дата документа').year
        if d.historical_number:
            number=d.historical_number
            if self.connection.execute("SELECT 1 FROM issued_documents WHERE kind=? AND number_key=? AND state='registered'",(d.kind,number_key(number))).fetchone(): raise IssuanceError('Этот регистрационный номер уже занят в области данного вида документа.')
            match=re.fullmatch(re.escape(PREFIXES[d.kind])+r'-([0-9]{4})-([0-9]{4,9})',number)
            if match: self._advance(d.kind,int(match[1]),int(match[2]))
            return number
        row=self.connection.execute('SELECT last_value FROM issued_sequences WHERE kind=? AND year=?',(d.kind,year)).fetchone();counter=row[0] if row else 0
        while True:
            counter+=1;number=f'{PREFIXES[d.kind]}-{year:04d}-{counter:04d}'
            if not self.connection.execute("SELECT 1 FROM issued_documents WHERE kind=? AND number_key=? AND state='registered'",(d.kind,number_key(number))).fetchone():
                self._advance(d.kind,year,counter);return number

    def register(self, key, signature):
        path=None
        try:
            with self.transaction():
                review=self._review(key)
                if signature!=review.signature: raise IssuanceError('Черновик или шаблон изменился. Проверьте новый проект.')
                d=self.load(key);number=self._assign_number(d)
                blank_key=encoded([number_key(d.blank_series),number_key(d.blank_number)]) if d.blank_number else None
                if blank_key and self.connection.execute("SELECT 1 FROM issued_documents WHERE kind=? AND blank_key=? AND state='registered'",(d.kind,blank_key)).fetchone(): raise IssuanceError('Реквизиты физического бланка уже использованы для этого вида документа.')
                review.context['issued']['number']=number;review.context['document'].update(number=number,is_draft=False)
                day=iso_date(d.document_date,'Дата документа');self._check_path(self.storage.root/'documents'/f'{day.year:04d}'/f'{day.month:02d}'/'issued')
                path=generate_document(self.templates.source(review.template_id),review.context,self.storage.document_directory(day,'issued')/('issued-'+uuid4().hex+'.docx'))
                snapshot=encoded({'context':review.context,'basis':review.basis,'template':asdict(self.templates.load(review.template_id))})
                self.connection.execute("UPDATE issued_documents SET state='registered',number=?,number_key=?,blank_key=?,template_id=?,document_path=?,document_sha256=?,snapshot=?,registered_at=CURRENT_TIMESTAMP,revision=revision+1 WHERE id=?",
                    (number,number_key(number),blank_key,review.template_id,path.relative_to(self.storage.root).as_posix(),hashlib.sha256(read_docx(path)).hexdigest(),snapshot,key))
        except Exception:
            if path is not None:self._quarantine(path)
            raise
        return self.load(key)

    def register_batch(self, reviews):
        """Per-document transactions; retry returns existing primary, never reissues it."""
        result=[]
        for key,signature in reviews:
            try:
                d=self.load(key);existing=self.registered_for(d.training_id)
                if existing: result.append((key,'already_registered',existing.id,''));continue
                registered=self.register(key,signature);result.append((key,'registered',registered.id,''))
            except (ValueError,OSError,sqlite3.Error) as error:
                result.append((key,'error',None,str(error) if isinstance(error,ValueError) else 'Проверьте базу и хранилище.'))
        return result

    def search(self, query='', *, kind=None, year=None, state=None, start='', end=''):
        query=' '.join(query.split()).casefold();found=[]
        for key, in self.connection.execute('SELECT id FROM issued_documents ORDER BY document_date DESC,id DESC'):
            d=self.load(key)
            if kind and d.kind!=kind or state and d.state!=state or year and int(d.document_date[:4])!=int(year) or start and d.document_date<start or end and d.document_date>end: continue
            if query and query not in ' '.join((d.full_name,d.number or '',d.program_name)).casefold():continue
            original,email=self.delivery_summary(key)
            found.append((d.id,d.full_name,KINDS[d.kind],d.number or 'Без номера',d.document_date,d.program_name,d.state,original,email))
        return found

    def delivery_summary(self, key):
        originals=[];emails=[]
        for method,day,received,status in self.connection.execute('SELECT method,event_date,received_date,postal_status FROM document_transfers WHERE document_id=? ORDER BY event_date,id',(key,)):
            if method=='personal': originals.append('Лично '+day)
            elif method=='post_confirmation': originals.append('Почта: вручено '+received)
            elif method=='email': emails.append(day+(' (получение подтверждено)' if status=='confirmed' else ' (отправлено)'))
        return '; '.join(originals) or 'Передача оригинала не подтверждена','; '.join(emails)

    def export_xlsx(self, start, end, *, kind=None):
        if iso_date(start,'Начало периода')>iso_date(end,'Конец периода'):raise IssuanceError('Начало периода позже окончания.')
        book=Workbook(write_only=True);sheet=book.create_sheet('Внутренний реестр')
        headers=('Вид документа','Регистрационный номер','Дата документа','Серия бланка','Номер бланка','ФИО','Программа','Дата завершения','Подтверждённая передача оригинала','Электронная копия (email)')
        def append(values):
            cells=[]
            for value in values:
                cell=WriteOnlyCell(sheet,value=str(value));cell.data_type='s';cells.append(cell)
            sheet.append(cells)
        append(headers)
        for row in self.search(kind=kind,state='registered',start=start,end=end):
            d=self.load(row[0]);append((KINDS[d.kind],d.number,d.document_date,d.blank_series,d.blank_number,d.full_name,d.program_name,d.completion_date,row[7],row[8]))
        buffer=BytesIO();book.save(buffer)
        path=self._exports('issued-registers')/('internal-register-'+uuid4().hex+'.xlsx')
        owned=False
        try:
            with path.open('xb') as stream:
                owned=True;stream.write(buffer.getvalue())
        except Exception:
            if owned:path.unlink(missing_ok=True)
            raise
        return path

    def _check_path(self, path):
        root = self.storage.root
        if not path.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in [path,*path.parents] if p.is_relative_to(root)):
            raise IssuanceError('Неверный путь архива или символическая ссылка.')

    def _exports(self, category):
        self.storage.require_available(); folder = self.storage.root / 'exports' / category
        self._check_path(folder); folder.mkdir(parents=True,exist_ok=True); return folder

    def _quarantine(self, path):
        folder = self._exports('unregistered-issued') / uuid4().hex
        folder.mkdir(); path.rename(folder / path.name)

    def recover_unregistered(self):
        count = 0
        with self.transaction():
            references = {row[0] for row in self.connection.execute("SELECT document_path FROM issued_documents WHERE document_path != ''")}
            self._check_path(self.storage.root / 'documents')
            for path in (self.storage.root / 'documents').glob('[0-9][0-9][0-9][0-9]/[0-9][0-9]/issued/*'):
                if not (re.fullmatch(r'issued-[a-f0-9]{32}\.docx',path.name) or re.fullmatch(r'\.docx-.*\.tmp',path.name)): continue
                self._check_path(path)
                if path.relative_to(self.storage.root).as_posix() not in references: self._quarantine(path); count += 1
        return count

    def verify_document(self, order_id):
        order = self.load(order_id)
        if order.state == 'draft' or not re.fullmatch(r'documents/[0-9]{4}/[0-9]{2}/issued/issued-[a-f0-9]{32}\.docx',order.document_path):
            raise IssuanceError('У выпускного документа нет корректного архивного документа.')
        path = self.storage.root / order.document_path; self._check_path(path); data = read_docx(path)
        if hashlib.sha256(data).hexdigest() != order.document_sha256:
            raise IssuanceError('SHA-256 выпускного документа не совпадает с реестром. Оригинал повреждён или изменён.')
        validate_package(data); return path

    def document_copy(self, order_id):
        data = read_docx(self.verify_document(order_id))
        if hashlib.sha256(data).hexdigest() != self.load(order_id).document_sha256: raise IssuanceError('Архивный документ изменился.')
        return publish_docx(data,self._exports('issued-copies') / ('copy-' + uuid4().hex + '.docx'))
