"""Ручные события передачи и локальные подтверждения; без почтовых интеграций."""
from dataclasses import asdict, replace
from datetime import date
import hashlib
from pathlib import Path
import re
from uuid import uuid4
import os

from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtPdf import QPdfDocument
from proficentr.issued_documents import TransferEvent, IssuanceError, METHODS
from proficentr.organization import read_logo
from proficentr.settings import _require_local
from proficentr.students import iso_date

EVENT_FIELDS=tuple(TransferEvent.__dataclass_fields__)
MAX_EVIDENCE_BYTES=10*1024*1024


def read_evidence(source):
    path=Path(source).absolute();_require_local(path)
    if any(p.is_symlink() for p in (path,*path.parents)) or not path.is_file():raise IssuanceError('Выберите обычный локальный файл без символических ссылок.')
    if path.suffix.lower() in ('.png','.jpg','.jpeg'):
        data,extension=read_logo(path);return data,extension[1:]
    if path.suffix.lower()!='.pdf':raise IssuanceError('Подтверждения: только PDF, JPG или PNG.')
    with path.open('rb') as stream:data=stream.read(MAX_EVIDENCE_BYTES+1)
    if len(data)>MAX_EVIDENCE_BYTES:raise IssuanceError('PDF не должен превышать 10 МиБ.')
    if not data.startswith(b'%PDF-') or not data.rstrip().endswith(b'%%EOF'):raise IssuanceError('Содержимое файла не соответствует PDF.')
    buffer=QBuffer();buffer.setData(data);buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    document=QPdfDocument();document.load(buffer)
    valid=document.status()==QPdfDocument.Status.Ready and document.error()==QPdfDocument.Error.None_ and document.pageCount()>0
    document.close()
    if not valid:raise IssuanceError('Не удалось прочитать PDF; повреждённые и защищённые паролем подтверждения не поддерживаются.')
    return data,'pdf'


class DeliveryRepository:
    def __init__(self, issued):self.issued=issued;self.connection=issued.connection;self.storage=issued.storage

    def load(self, event_id):
        row=self.connection.execute(
            f"SELECT {', '.join(EVENT_FIELDS)} FROM document_transfers WHERE id=?",(event_id,),  # nosec B608 # Fixed fields.
        ).fetchone()
        if row is None:raise IssuanceError('Событие передачи не найдено.')
        return TransferEvent(**dict(zip(EVENT_FIELDS,row)))

    def history(self, document_id):
        return [self.load(row[0]) for row in self.connection.execute('SELECT id FROM document_transfers WHERE document_id=? ORDER BY event_date,id',(document_id,))]

    def _validate(self, event):
        d=self.issued.load(event.document_id)
        if d.state!='registered':raise IssuanceError('Передача доступна только для зарегистрированного выпускного документа.')
        day=iso_date(event.event_date,'Дата передачи/события')
        if day<iso_date(d.document_date,'Дата документа') or day>date.today():raise IssuanceError('Дата события должна быть между датой документа и сегодняшней датой.')
        if event.method not in METHODS:raise IssuanceError('Выберите способ передачи.')
        for field in ('recipient','confirmation','address','email','tracking_number','note'):
            value=getattr(event,field)
            if type(value) is not str or len(value)>10000:raise IssuanceError('Проверьте длину реквизитов передачи.')
            setattr(event,field,value.strip())
        event=replace(event,medium='electronic_copy' if event.method=='email' else 'original')
        if event.method=='post_confirmation':
            parent=self.load(event.parent_event_id)
            if parent.document_id!=event.document_id or parent.method!='post':raise IssuanceError('Подтверждение должно ссылаться на почтовую отправку этого документа.')
            received=iso_date(event.received_date,'Дата вручения')
            if not iso_date(parent.event_date,'Дата отправки')<=received<=day or not event.confirmation:raise IssuanceError('Укажите подтверждение и дату вручения между отправкой и событием подтверждения.')
            event=replace(event,recipient=parent.recipient,address=parent.address,tracking_number=parent.tracking_number,postal_status='confirmed')
        elif event.parent_event_id is not None or event.received_date:raise IssuanceError('Связь с отправкой и дата вручения заполняются только для подтверждения Почты России.')
        if not event.recipient:raise IssuanceError('Укажите получателя.')
        if event.method=='personal':
            if not event.confirmation:raise IssuanceError('Укажите способ подтверждения личной передачи.')
            event.postal_status='confirmed'
        if event.method=='post':
            if not event.address:raise IssuanceError('Укажите адрес, фактически использованный при отправке.')
            if not event.postal_status:event.postal_status='sent'
            if event.postal_status not in ('sent','unconfirmed'):raise IssuanceError('Вручение подтверждается отдельным событием, а не трек-номером.')
        if event.method=='email':
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',event.email):raise IssuanceError('Укажите email получателя.')
            if not event.postal_status:event.postal_status='sent'
            if event.postal_status not in ('sent','confirmed'):raise IssuanceError('Выберите состояние отправки электронной копии.')
            if event.postal_status=='confirmed' and not event.confirmation:raise IssuanceError('Укажите подтверждение получения электронной копии.')
        return event

    def _copy(self, data, kind):
        self.storage.require_available();folder=self.storage.root/'attachments'/'issued-evidence'
        self.issued._check_path(folder);folder.mkdir(parents=True,exist_ok=True)
        destination=folder/('evidence-'+uuid4().hex+'.'+kind)
        owned=False
        try:
            with destination.open('xb') as stream:
                owned=True;stream.write(data);stream.flush();os.fsync(stream.fileno())
        except Exception:
            if owned:destination.unlink(missing_ok=True)
            raise
        return destination

    def _attach(self, event_id, data, kind, evidence_type):
        path=self._copy(data,kind)
        try:
            self.connection.execute('INSERT INTO transfer_evidence(event_id,relative_path,sha256,evidence_type,format) VALUES (?,?,?,?,?)',
                (event_id,path.relative_to(self.storage.root).as_posix(),hashlib.sha256(data).hexdigest(),evidence_type,kind))
        except Exception:path.unlink(missing_ok=True);raise
        return path

    def add(self, event, *, evidence=None, evidence_type='Подтверждение передачи'):
        if event.id is not None:raise IssuanceError('История не редактируется: добавьте новое событие.')
        evidence_type=evidence_type.strip()
        if not evidence_type or len(evidence_type)>500:raise IssuanceError('Укажите вид подтверждающего файла (до 500 символов).')
        prepared=read_evidence(evidence) if evidence is not None else None
        path=None
        try:
            with self.issued.transaction():
                event=self._validate(event)
                fields=EVENT_FIELDS[1:-1];values=asdict(event)
                key=self.connection.execute(
                    f"INSERT INTO document_transfers ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",tuple(values[k] for k in fields),  # nosec B608 # Fixed fields.
                ).lastrowid
                if prepared:path=self._attach(key,*prepared,evidence_type)
        except Exception:
            if path is not None:path.unlink(missing_ok=True)
            raise
        return self.load(key)

    def attach(self, event_id, source, evidence_type):
        evidence_type=evidence_type.strip()
        if not evidence_type or len(evidence_type)>500:raise IssuanceError('Укажите вид подтверждающего файла (до 500 символов).')
        data,kind=read_evidence(source);path=None
        try:
            with self.issued.transaction():
                self.load(event_id);path=self._attach(event_id,data,kind,evidence_type)
        except Exception:
            if path is not None:path.unlink(missing_ok=True)
            raise
        return self.evidence(event_id)[-1]

    def evidence(self, event_id):
        return self.connection.execute('SELECT id,relative_path,sha256,evidence_type,format FROM transfer_evidence WHERE event_id=? ORDER BY id',(event_id,)).fetchall()

    def verify_evidence(self, evidence_id):
        row=self.connection.execute('SELECT relative_path,sha256,format FROM transfer_evidence WHERE id=?',(evidence_id,)).fetchone()
        if row is None or not re.fullmatch(r'attachments/issued-evidence/evidence-[a-f0-9]{32}\.(pdf|png|jpg)',row[0]):raise IssuanceError('Неверный путь подтверждения.')
        path=self.storage.root/row[0];self.issued._check_path(path)
        data,kind=read_evidence(path)
        if kind!=row[2] or hashlib.sha256(data).hexdigest()!=row[1]:raise IssuanceError('SHA-256 подтверждения не совпадает с реестром.')
        return path

    def evidence_copy(self, evidence_id):
        original=self.verify_evidence(evidence_id);data,kind=read_evidence(original)
        expected=self.connection.execute('SELECT sha256 FROM transfer_evidence WHERE id=?',(evidence_id,)).fetchone()[0]
        if hashlib.sha256(data).hexdigest()!=expected:raise IssuanceError('Подтверждение изменилось.')
        destination=self.issued._exports('evidence-copies')/('copy-'+uuid4().hex+'.'+kind)
        with destination.open('xb') as stream:stream.write(data)
        return destination

    def recover_unregistered(self):
        count=0
        with self.issued.transaction():
            folder=self.storage.root/'attachments'/'issued-evidence';self.issued._check_path(folder)
            referenced={row[0] for row in self.connection.execute('SELECT relative_path FROM transfer_evidence')}
            for path in folder.glob('evidence-*'):
                if not re.fullmatch(r'evidence-[a-f0-9]{32}\.(pdf|png|jpg)',path.name):continue
                self.issued._check_path(path)
                if path.relative_to(self.storage.root).as_posix() not in referenced:
                    target=self.issued._exports('unregistered-evidence')/uuid4().hex;target.mkdir();path.rename(target/path.name);count+=1
        return count
