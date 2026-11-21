"""Только вымышленные завершения, выпускные документы и подтверждения."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from docx import Document
from openpyxl import load_workbook
import pytest

from legacy_schema import legacy_columns, remove_preparation_schema
from PySide6.QtCore import QBuffer,QIODevice
from PySide6.QtGui import QImage,QColor

from test_contracts import records
from proficentr.enrollment_orders import EnrollmentOrder
from proficentr.enrollment_order_repository import EnrollmentOrderRepository
from proficentr.attestations import AttestationProtocol,AttestationResult
from proficentr.attestation_repository import AttestationRepository
from proficentr.completion_orders import CompletionOrder,CompletionEntry
from proficentr.completion_repository import CompletionRepository
from proficentr.issued_documents import IssuedDocument,IssuanceError,TransferEvent,document_kind
from proficentr.issued_repository import IssuedDocumentRepository
from proficentr.document_delivery import DeliveryRepository,read_evidence
from proficentr.database import Database
from proficentr.program_repository import ProgramRepository
from proficentr.trainings import Training
from proficentr.storage import relocate_storage
import proficentr.database as db_module
import proficentr.issued_repository as issue_module


def complete(r, direction='dpo', kind='dpo_upskilling'):
    if kind!=r.program.kind:
        program=ProgramRepository(r.db.connection).save(replace(r.program,id=None,full_name='Вымышленная программа '+kind,direction=direction,kind=kind))
        r.group=r.trainings.save_group(replace(r.group,program_id=program.id),change_program=True)
    enrollment=EnrollmentOrderRepository(r.db.connection,r.storage)
    enrollment.templates.register(r.source,'enrollment_order',direction+'-group','Вымышленный приказ')
    o=enrollment.save(EnrollmentOrder(order_date='2020-02-01',kind='group',group_id=r.group.id),[t.id for t in r.members]);o=enrollment.register(o.id,enrollment.prepare(o.id).signature)
    enrollment.confirm_signing(o.id,signed_date='2020-02-02',confirmed_by='Тест',grounds_checked=True,education_checked=True)
    protocols=AttestationRepository(r.db.connection,r.storage);protocols.templates.register(r.source,'protocol_'+direction,'standard','Вымышленный протокол')
    p=protocols.save(AttestationProtocol(protocol_date='2020-04-01',attestation_date='2020-04-01',group_id=r.group.id,direction=direction),
        [AttestationResult(t.id,'passed',qualification='Профессия '+str(i) if direction=='po' else '',qualification_grade=str(i+2) if direction=='po' else '') for i,t in enumerate(r.members)])
    p=protocols.register(p.id,protocols.prepare(p.id).signature);protocols.confirm_signing(p.id,signed_date='2020-04-01',confirmed_by='Тест')
    source=r.source.parent/'completion-for-issued.docx';d=Document();d.add_paragraph('{{ document.number }}')
    d.add_paragraph('{% for row in students %}{{ row.full_name }} {{ row.completion_date }}{% endfor %}');d.save(source)
    completion=CompletionRepository(r.db.connection,r.storage);completion.templates.register(source,'completion_order',direction+'-group','Вымышленный приказ завершения')
    o=completion.save(CompletionOrder(order_date='2020-04-03',kind='group',group_id=r.group.id,direction=direction),[CompletionEntry(t.id,'2020-04-0'+str(i+1)) for i,t in enumerate(r.members)])
    o=completion.register(o.id,completion.prepare(o.id).signature);r.completion_order=completion.confirm_signing(o.id,signed_date='2020-04-03',confirmed_by='Тест',conditions_checked=True)
    r.completions=completion;r.protocols=protocols
    r.repo=IssuedDocumentRepository(r.db.connection,r.storage);r.delivery=DeliveryRepository(r.repo)
    d=Document();d.add_paragraph('{{ issued.kind }} {{ issued.number }} {{ issued.date }}')
    d.add_paragraph('{{ student.full_name }} {{ program.name }} {{ program.hours }} {{ center.full_name }}')
    d.add_paragraph('{{ issued.blank_series }} {{ issued.blank_number }} {{ issued.qualification }} {{ issued.qualification_grade }}')
    d.add_paragraph('{{ issued.completion_order_number }} {{ issued.completion_order_date }} {{ issued.completion_date }}');d.save(r.source)
    r.repo.templates.register(r.source,document_kind(direction,kind),'standard','Вымышленная выпускная форма')
    return r


@pytest.fixture
def issued(records):return complete(records)


def draft(r, index=0, **changes):return r.repo.save(replace(IssuedDocument(training_id=r.members[index].id,document_date='2020-04-04'),**changes))
def register(r,d):return r.repo.register(d.id,r.repo.prepare(d.id).signature)
def event(d,method='personal',**changes):return replace(TransferEvent(document_id=d.id,method=method,event_date='2020-04-05',recipient='Получатель Примеров',confirmation='Подписанная расписка'),**changes)


def png(path,fmt='PNG'):
    img=QImage(2,2,QImage.Format.Format_RGB32);img.fill(QColor('red'));assert img.save(str(path),fmt);return path


def pdf(path):
    data=bytearray(b'%PDF-1.4\n');offsets=[0]
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 10] >>']
    for i,obj in enumerate(objects,1):offsets.append(len(data));data.extend(f'{i} 0 obj\n'.encode()+obj+b'\nendobj\n')
    start=len(data);data.extend(b'xref\n0 4\n0000000000 65535 f \n')
    for offset in offsets[1:]:data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode());path.write_bytes(data);return path


@pytest.mark.parametrize('direction,kind,prefix',[('dpo','dpo_upskilling','У'),('dpo','dpo_retraining','Д'),('po','po_initial_training','С'),('po','po_retraining','С'),('po','po_upskilling','С')])
def test_document_kinds_auto_historical_fields_and_docx(records,direction,kind,prefix):
    r=complete(records,direction,kind);d=draft(r)
    preview,review=r.repo.preview(d.id)
    assert 'ПРОЕКТ ВЫПУСКНОГО ДОКУМЕНТА' in Document(preview).paragraphs[0].text
    assert not r.db.connection.execute('SELECT * FROM issued_sequences').fetchall()
    d=r.repo.register(d.id,review.signature)
    assert d.number==prefix+'-2020-0001' and d.state=='registered' and d.kind==document_kind(direction,kind)
    assert d.created_at and d.registered_at and d.blank_series==d.blank_number==''
    assert d.completion_date=='2020-04-01' and d.document_date=='2020-04-04'
    assert not r.delivery.history(d.id) and 'не подтверждена' in r.repo.delivery_summary(d.id)[0]
    assert d.qualification==('Профессия 0' if direction=='po' else '')
    text='\n'.join(p.text for p in Document(r.repo.verify_document(d.id)).paragraphs)
    assert d.number in text and d.full_name in text and d.completion_order_number in text
    assert not r.db.connection.execute('PRAGMA foreign_key_check').fetchall()


def test_no_completion_rejected(records):
    r=records;repo=IssuedDocumentRepository(r.db.connection,r.storage)
    with pytest.raises(IssuanceError):repo.save(IssuedDocument(training_id=r.members[0].id,document_date='2020-04-04'))


def test_historical_snapshot_used_before_and_after_registration(issued):
    r=issued;old_name=r.first.surname
    r.students.save(replace(r.first,surname='Новое имя'))
    ProgramRepository(r.db.connection).save(replace(r.program,full_name='Новая программа',direction='po',kind='po_retraining'))
    with r.db.connection:r.db.connection.execute("UPDATE training_center SET full_name='Новый центр',director_name='Новый директор'")
    d=register(r,draft(r));assert old_name in d.full_name and d.program_kind=='dpo_upskilling' and d.program_name=='Тестовая ДПО'
    c=json.loads(d.snapshot)['context'];assert c['center']['full_name']=='Вымышленный центр'
    before=r.repo.verify_document(d.id).read_bytes();r.students.save(replace(r.first,surname='Ещё новое имя'))
    assert r.repo.load(d.id)==d and r.repo.verify_document(d.id).read_bytes()==before
    assert r.repo.search(old_name)[0][0]==d.id


def test_later_attestation_does_not_change_completion_basis(issued):
    r=issued;p=r.protocols.save(AttestationProtocol(protocol_date='2021-01-01',attestation_date='2021-01-01'),[AttestationResult(r.members[0].id,'failed')])
    p=r.protocols.register(p.id,r.protocols.prepare(p.id).signature);r.protocols.confirm_signing(p.id,signed_date='2021-01-02',confirmed_by='Тест')
    d=register(r,draft(r));assert json.loads(d.snapshot)['basis']['entry']['protocol_id']!=p.id


def test_batch_partial_failure_retry_no_duplicates(issued):
    r=issued;docs=[draft(r,i) for i in (0,1)];reviews=[(d.id,r.repo.prepare(d.id).signature) for d in docs]
    with r.db.connection:r.db.connection.execute(f"CREATE TRIGGER fail_second_issue BEFORE UPDATE ON issued_documents WHEN NEW.state='registered' AND NEW.id={docs[1].id} BEGIN SELECT RAISE(ABORT,'test'); END")
    results=r.repo.register_batch(reviews);assert [r[1] for r in results]==['registered','error']
    with r.db.connection:r.db.connection.execute('DROP TRIGGER fail_second_issue')
    results=r.repo.register_batch(reviews);assert [r[1] for r in results]==['already_registered','registered']
    assert len(r.repo.search(state='registered'))==2
    assert len(list((r.storage.root/'documents').glob('*/*/issued/issued-*.docx')))==2
    assert r.repo.draft_for(docs[0].training_id).id==docs[0].id


def test_numbers_historical_blank_controls(issued):
    r=issued;d=register(r,draft(r,historical_number='У-2020-0010',blank_series='',blank_number='А-1'))
    second=draft(r,1,blank_number='а-1')
    with pytest.raises(IssuanceError,match='бланка'):register(r,second)
    second=r.repo.save(replace(second,blank_number='А-2'))
    assert register(r,second).number=='У-2020-0011'
    with pytest.raises(IssuanceError):draft(r)
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:r.db.connection.execute('DELETE FROM issued_sequences')
    assert d.number!=d.blank_number


def test_historical_number_uniqueness_normalized(issued):
    r=issued;register(r,draft(r,historical_number='Архив / 1'))
    with pytest.raises(IssuanceError):register(r,draft(r,1,historical_number='архив   / 1'))
    assert register(r,draft(r,1,document_date='2021-01-01')).number=='У-2021-0001'


@pytest.mark.parametrize('sql',["UPDATE issued_documents SET full_name='other' WHERE id=?",'DELETE FROM issued_documents WHERE id=?',"UPDATE issued_documents SET snapshot='{}' WHERE id=?","UPDATE issued_documents SET blank_number='other' WHERE id=?"])
def test_registered_immutable_sql_and_repository(issued,sql):
    r=issued;d=register(r,draft(r))
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:r.db.connection.execute(sql,(d.id,))
    with pytest.raises(IssuanceError):r.repo.save(replace(d,blank_number='other'))


def test_copy_hash_and_orphan_recovery(issued):
    r=issued;d=register(r,draft(r,blank_number='TEST-BLANK'));path=r.repo.verify_document(d.id);copy=r.repo.document_copy(d.id);copy.write_bytes(b'edit')
    other=draft(r,1)
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute('UPDATE OR REPLACE issued_documents SET id=? WHERE id=?',(d.id,other.id))
    values=asdict(d);values.pop('id')
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute(f"UPDATE OR REPLACE issued_documents SET {','.join(k+'=?' for k in values)} WHERE id=?",(*values.values(),other.id))
    independent=dict(values,training_id=other.training_id,full_name=other.full_name,completion_date=other.completion_date,
        number='У-2020-0099',number_key='у-2020-0099',blank_number='',blank_key=None)
    for collision in (dict(independent,number=d.number,number_key=d.number_key),
                      dict(independent,blank_number=d.blank_number,blank_key=d.blank_key)):
        with pytest.raises(sqlite3.IntegrityError),r.db.connection:
            r.db.connection.execute(f"UPDATE OR REPLACE issued_documents SET {','.join(k+'=?' for k in collision)} WHERE id=?",(*collision.values(),other.id))
    assert r.repo.load(d.id)==d and r.repo.load(other.id)==other
    assert hashlib.sha256(path.read_bytes()).hexdigest()==d.document_sha256
    orphan=path.parent/('issued-'+uuid4().hex+'.docx');orphan.write_bytes(path.read_bytes())
    assert r.repo.recover_unregistered()==1 and not orphan.exists()
    path.write_bytes(b'bad')
    with pytest.raises(IssuanceError,match='SHA-256'):r.repo.document_copy(d.id)


@pytest.mark.parametrize('failure',['file','db','commit'])
def test_registration_rollback(issued,monkeypatch,failure):
    r=issued;d=draft(r);review=r.repo.prepare(d.id)
    if failure=='file':
        def fail(*a):raise OSError('synthetic')
        monkeypatch.setattr(issue_module,'generate_document',fail)
    elif failure=='db':
        with r.db.connection:r.db.connection.execute("CREATE TRIGGER refuse_issue BEFORE UPDATE ON issued_documents WHEN NEW.state='registered' BEGIN SELECT RAISE(ABORT,'test'); END")
    else:
        with r.db.connection:
            r.db.connection.execute('CREATE TABLE deferred_issue_test(id INTEGER REFERENCES students(id) DEFERRABLE INITIALLY DEFERRED)')
            r.db.connection.execute("CREATE TRIGGER refuse_issue_commit AFTER UPDATE ON issued_documents WHEN NEW.state='registered' BEGIN INSERT INTO deferred_issue_test VALUES (999999); END")
    with pytest.raises(IssuanceError):r.repo.register(d.id,review.signature)
    assert r.repo.load(d.id).state=='draft' and not r.db.connection.execute('SELECT * FROM issued_sequences').fetchall()
    assert not list((r.storage.root/'documents').glob('*/*/issued/*.docx'))


def test_personal_post_confirmation_email_separate_events(issued):
    r=issued;d=register(r,draft(r))
    email=r.delivery.add(event(d,'email',email='sample@example.test',confirmation=''))
    assert email.medium=='electronic_copy' and 'не подтверждена' in r.repo.delivery_summary(d.id)[0]
    post=r.delivery.add(event(d,'post',address='Вымышленный адрес',tracking_number='TRACK-TEST',confirmation=''))
    assert post.postal_status=='sent' and 'не подтверждена' in r.repo.delivery_summary(d.id)[0]
    confirmed=r.delivery.add(event(d,'post_confirmation',parent_event_id=post.id,event_date='2020-04-08',received_date='2020-04-07',confirmation='Уведомление о вручении'))
    assert confirmed.tracking_number==post.tracking_number and '2020-04-07' in r.repo.delivery_summary(d.id)[0]
    personal=r.delivery.add(event(d,'personal',event_date='2020-04-09'))
    assert personal.medium=='original' and len(r.delivery.history(d.id))==4
    assert r.delivery.load(post.id).postal_status=='sent'
    replacement=asdict(confirmed);replacement.pop('id');replacement.pop('created_at')
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute(f"INSERT OR REPLACE INTO document_transfers ({','.join(replacement)}) VALUES ({','.join('?' for _ in replacement)})",tuple(replacement.values()))
    assert r.delivery.load(confirmed.id)==confirmed and len(r.delivery.history(d.id))==4


@pytest.mark.parametrize('changes',[{'method':'email','email':'bad'},{'recipient':''},{'confirmation':''},{'event_date':'2019-01-01'},
    {'event_date':'9999-01-01'},{'method':'post','address':''},{'method':'post','address':'Адрес','postal_status':'confirmed'},
    {'method':'post_confirmation','parent_event_id':999,'received_date':'2020-04-05'}])
def test_invalid_transfers(issued,changes):
    r=issued;d=register(r,draft(r))
    with pytest.raises(ValueError):r.delivery.add(event(d,**changes))
    assert not r.delivery.history(d.id)


def test_draft_cannot_transfer_and_history_cannot_change(issued):
    r=issued;d=draft(r)
    with pytest.raises(IssuanceError):r.delivery.add(event(d))
    d=register(r,d);e=r.delivery.add(event(d))
    with pytest.raises(IssuanceError):r.delivery.add(e)
    for sql in ('DELETE FROM document_transfers WHERE id=?',"UPDATE document_transfers SET recipient='other' WHERE id=?"):
        with pytest.raises(sqlite3.IntegrityError),r.db.connection:r.db.connection.execute(sql,(e.id,))


@pytest.mark.parametrize('extension',['png','jpg','pdf'])
def test_evidence_valid_content_copy_hash_and_immutability(issued,tmp_path,extension):
    r=issued;d=register(r,draft(r));source=tmp_path/('source.'+extension)
    (pdf(source) if extension=='pdf' else png(source,'JPEG' if extension=='jpg' else 'PNG'))
    original=source.read_bytes();e=r.delivery.add(event(d),evidence=source,evidence_type='Вымышленная расписка')
    evidence=r.delivery.evidence(e.id)[0];path=r.delivery.verify_evidence(evidence[0])
    assert path.read_bytes()==original and source.read_bytes()==original
    copy=r.delivery.evidence_copy(evidence[0]);copy.write_bytes(b'edited copy');assert path.read_bytes()==original
    for sql in ('DELETE FROM transfer_evidence WHERE id=?',"UPDATE transfer_evidence SET sha256='other' WHERE id=?"):
        with pytest.raises(sqlite3.IntegrityError),r.db.connection:r.db.connection.execute(sql,(evidence[0],))
    with pytest.raises(sqlite3.IntegrityError),r.db.connection:
        r.db.connection.execute('INSERT OR REPLACE INTO transfer_evidence(event_id,relative_path,sha256,evidence_type,format) VALUES (?,?,?,?,?)',
            (e.id,evidence[1],evidence[2],'Замена подтверждения',extension))
    assert r.delivery.evidence(e.id)==[evidence]
    path.write_bytes(b'bad')
    with pytest.raises(ValueError):r.delivery.verify_evidence(evidence[0])


@pytest.mark.parametrize('mode',['exe','fake_pdf','fake_png','oversize_pdf','symlink_file','symlink_directory','cloud'])
def test_evidence_bad_formats_paths_and_size(issued,tmp_path,mode):
    source=tmp_path/'evidence.pdf'
    if mode=='exe':source=tmp_path/'bad.exe';source.write_bytes(b'MZtest')
    elif mode=='fake_pdf':source.write_bytes(b'%PDF-1.4\nnot a PDF\n%%EOF')
    elif mode=='fake_png':source=tmp_path/'bad.png';source.write_bytes(b'not image')
    elif mode=='oversize_pdf':source.write_bytes(b'%PDF-'+b' '*10*1024*1024+b'%%EOF')
    elif mode=='cloud':
        folder=tmp_path/'OneDrive';folder.mkdir();source=pdf(folder/'evidence.pdf')
    else:
        target=pdf(source)
        if mode=='symlink_file':source=tmp_path/'linked.pdf';source.symlink_to(target)
        else:
            folder=tmp_path/'linked';folder.symlink_to(tmp_path,target_is_directory=True);source=folder/'evidence.pdf'
    with pytest.raises((ValueError,OSError)):read_evidence(source)


def test_evidence_rollback_preserves_existing(issued,tmp_path):
    r=issued;d=register(r,draft(r));source=png(tmp_path/'evidence.png')
    e=r.delivery.add(event(d),evidence=source);original=r.delivery.evidence(e.id)[0];data=r.delivery.verify_evidence(original[0]).read_bytes()
    with r.db.connection:r.db.connection.execute("CREATE TRIGGER refuse_evidence BEFORE INSERT ON transfer_evidence BEGIN SELECT RAISE(ABORT,'test'); END")
    with pytest.raises(IssuanceError):r.delivery.add(event(d,'email',email='sample@example.test'),evidence=source)
    assert len(r.delivery.history(d.id))==1 and r.delivery.verify_evidence(original[0]).read_bytes()==data
    assert len(list((r.storage.root/'attachments'/'issued-evidence').glob('*')))==1


def test_evidence_commit_failure_cleans_owned_file(issued,tmp_path):
    r=issued;d=register(r,draft(r));source=png(tmp_path/'evidence.png')
    with r.db.connection:
        r.db.connection.execute('CREATE TABLE deferred_delivery_test(id INTEGER REFERENCES students(id) DEFERRABLE INITIALLY DEFERRED)')
        r.db.connection.execute("CREATE TRIGGER refuse_delivery_commit AFTER INSERT ON transfer_evidence BEGIN INSERT INTO deferred_delivery_test VALUES (999999); END")
    with pytest.raises(IssuanceError):r.delivery.add(event(d),evidence=source)
    assert not r.delivery.history(d.id) and not list((r.storage.root/'attachments'/'issued-evidence').glob('*'))


def test_register_search_filters_xlsx_text_formulas(issued):
    r=issued
    d=register(r,draft(r,historical_number='=SUM(1,2)',blank_series='+SERIES',blank_number='@BLANK'))
    assert r.repo.search('Тестов',kind=d.kind,year=2020,state='registered')[0][0]==d.id
    assert not r.repo.search(year=2021) and r.repo.candidates(r.group.id,pending_only=True)[0][0]==r.members[1].id
    r.delivery.add(event(d,'email',email='sample@example.test'))
    path=r.repo.export_xlsx('2020-01-01','2020-12-31');book=load_workbook(path);sheet=book.active
    assert sheet.max_row==2 and sheet.cell(2,2).value=='=SUM(1,2)' and sheet.cell(2,2).data_type=='s'
    assert sheet.cell(2,4).value=='+SERIES' and sheet.cell(2,5).value=='@BLANK'
    assert 'не подтверждена' in sheet.cell(2,9).value and 'отправлено' in sheet.cell(2,10).value
    assert all('паспорт' not in str(c.value).lower() and 'снилс' not in str(c.value).lower() for row in sheet for c in row)
    assert r.repo.export_xlsx('2021-01-01','2021-12-31').is_file()


def test_template_missing_and_stale_review(issued):
    r=issued;d=draft(r);review=r.repo.prepare(d.id)
    d=r.repo.save(replace(d,blank_number='TEST-1'))
    with pytest.raises(IssuanceError):r.repo.register(d.id,review.signature)
    with r.db.connection:r.db.connection.execute("UPDATE document_templates SET active=0 WHERE document_type='dpo_upskilling_certificate'")
    with pytest.raises(IssuanceError,match='Настройки'):r.repo.prepare(d.id)


def test_migration12_preserves_v11_and_storage_move(issued,tmp_path,monkeypatch):
    r=issued;path=tmp_path/'v11.sqlite3';migrations=db_module.MIGRATIONS
    monkeypatch.setattr(db_module,'MIGRATIONS',migrations[:11])
    with Database(path) as old:
        r.db.connection.backup(old.connection)
        with old.connection:
            remove_preparation_schema(old.connection)
            old.connection.execute('DROP TABLE frdo_templates')
            for name, in old.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE 'issued_%' OR name LIKE 'transfer_%' OR name LIKE 'transfers_%' OR name LIKE 'evidence_%')").fetchall():old.connection.execute(f'DROP TRIGGER {name}')
            for table in ('transfer_evidence','document_transfers','issued_documents','issued_sequences'):old.connection.execute(f'DROP TABLE {table}')
            old.connection.execute('DELETE FROM schema_migrations WHERE version>=12')
        tables=[r[0] for r in old.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        columns = {table: legacy_columns(old.connection, table) for table in tables}
        before={t:old.connection.execute(f'SELECT * FROM {t}').fetchall() for t in tables}
    monkeypatch.setattr(db_module,'MIGRATIONS',migrations)
    with Database(path) as db:
        assert db.connection.execute('SELECT max(version) FROM schema_migrations').fetchone()==(len(migrations),)
        for t,rows in before.items():
            if t!='schema_migrations':assert db.connection.execute(f'SELECT {columns[t]} FROM {t}').fetchall()==rows
        assert db.connection.execute('PRAGMA integrity_check').fetchall()==[('ok',)] and not db.connection.execute('PRAGMA foreign_key_check').fetchall()
    with sqlite3.connect(next((tmp_path/'migration-backups').glob('*.sqlite3'))) as db:assert db.execute('SELECT max(version) FROM schema_migrations').fetchone()==(11,)
    d=register(r,draft(r));e=r.delivery.add(event(d),evidence=png(tmp_path/'source.png'));file=r.delivery.evidence(e.id)[0]
    moved=relocate_storage(r.storage,r.db.connection,tmp_path/'moved')
    with Database(moved.database_path) as db:
        repo=IssuedDocumentRepository(db.connection,moved);delivery=DeliveryRepository(repo)
        assert repo.load(d.id)==d and repo.verify_document(d.id).read_bytes()==r.repo.verify_document(d.id).read_bytes()
        assert delivery.history(d.id)==r.delivery.history(d.id) and delivery.verify_evidence(file[0]).read_bytes()==r.delivery.verify_evidence(file[0]).read_bytes()


def test_three_document_numbering_areas_in_one_database(records):
    r=complete(records);assert register(r,draft(r)).number=='У-2020-0001'
    from proficentr.trainings import TrainingGroup
    for kind,direction,prefix in (('dpo_retraining','dpo','Д'),('po_initial_training','po','С')):
        r.group=r.trainings.save_group(TrainingGroup(name='Вымышленная группа '+kind,program_id=r.program.id,start_date='2020-03-01',end_date='2020-04-01'))
        r.members=r.trainings.add_students(r.group.id,[r.first.id,r.second.id],allow_overlap=True)
        complete(r,direction,kind);assert register(r,draft(r)).number==prefix+'-2020-0001'
    assert len(r.repo.search(state='registered'))==3


def test_xlsx_existing_file_is_never_deleted(issued,monkeypatch):
    from types import SimpleNamespace
    r=issued;register(r,draft(r));monkeypatch.setattr(issue_module,'uuid4',lambda:SimpleNamespace(hex='0'*32))
    path=r.repo.export_xlsx('2020-01-01','2020-12-31');before=path.read_bytes()
    with pytest.raises(FileExistsError):r.repo.export_xlsx('2020-01-01','2020-12-31')
    assert path.read_bytes()==before
    def unavailable(*args):raise OSError('synthetic storage failure')
    monkeypatch.setattr(r.repo,'_exports',unavailable)
    with pytest.raises(OSError):r.repo.export_xlsx('2020-01-01','2020-12-31')
