"""Поставляемые рабочие DOCX: каталог, настройки, дополнительные факты и неподписанные проекты."""
from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
from uuid import uuid4
from typing import Protocol

from proficentr.docx_context import (DocxError, SCHEMA, ROW_WORK_FIELDS, empty_context, validate_context)
from proficentr.docx_engine import read_docx, validate_template, generate_document
from proficentr.document_templates import TemplateRepository
from proficentr.organization import read_logo
from proficentr.programs import KINDS

RESOURCE_ROOT = Path(__file__).parent / 'resources' / 'working_templates'
CATEGORIES = {'admission':'Документы приёма','contracts':'Договоры','orders':'Приказы','protocols':'Протоколы','issued':'Выпускные документы и реестры'}
MODES = {'none':'Без фона','background':'С фоном','preprinted':'Преднапечатанный бланк'}


class PdfExporter(Protocol):
    """Точка расширения для отдельного проверяемого конвертера. В T06 реализации нет."""
    def export(self, docx_bytes: bytes) -> bytes: ...


def catalog():
    data = json.loads((RESOURCE_ROOT / 'manifest.json').read_text(encoding='utf-8'))
    items = data['templates']
    if data['version'] != 1 or len(items) != 24 or len({i['code'] for i in items}) != len(items):
        raise DocxError('Повреждён реестр стандартных шаблонов.')
    for item in items:
        if (item['category'] not in CATEGORIES or not re.fullmatch(r'[a-z]+_[0-9]{2}',item['code'])
            or item['path'] != item['category']+'/'+item['code']+'.docx'
            or not re.fullmatch(r'[a-f0-9]{64}',item['sha256']) or item['version']!=1
            or type(item['group']) is not bool or type(item['pdf']) is not bool):
            raise DocxError('Недопустимые свойства стандартного шаблона.')
    return items


def binding(context):
    base = deepcopy(context)
    for root,fields in SCHEMA.items():
        if type(fields) is dict:base[root]={k:base.get(root,{}).get(k,'') for k in fields}
    if base.get('students') and not base.get('group',{}).get('name'):base.setdefault('group',{})['name']='Индивидуальное обучение' if len(base['students'])==1 else 'Индивидуальные обучения'
    for root in ('work','commission','modules','documents','document','admission'):base.pop(root,None)
    for root in ('issued','protocol'):base.get(root,{}).pop('number',None)
    for row in base.get('students',[]):
        for field in ROW_WORK_FIELDS:row.pop(field,None)
    return sha256(json.dumps(base,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def complete_context(connection, context):
    c = empty_context(); c.update(deepcopy(context))
    base_binding = binding(c)
    work = c['work']; center=c.get('center',{}); student=c.get('student',{}); program=c.get('program',{})
    def contact(values):return ' / '.join(str(values.get(k,'')) for k in ('phone','email') if values.get(k))
    def bank(values):return '; '.join(str(values.get(k,'')) for k in ('bank_name','bik','settlement_account','correspondent_account') if values.get(k))
    derived={'training_price':c.get('contract',{}).get('total_price',''),'organization_contact':contact(center),'student_contact':contact(student),'customer_contact':contact(c.get('customer',{})),
        'organization_bank_details':bank(center),'customer_bank_details':bank(c.get('customer',{})),
        'identity_document':' '.join(str(student.get(k,'')) for k in ('passport_series','passport_number') if student.get(k)),
        'program_type':KINDS.get(program.get('direction'),{}).get(program.get('kind'),''),
        'education_type': 'ДПО' if program.get('direction')=='dpo' else 'ПО' if program.get('direction')=='po' else '',
        'education_basis':student.get('education_details',''),
        'effective_date':c.get('completion',{}).get('date') or c.get('training',{}).get('start_date','')}
    for key,value in derived.items():
        if not work.get(key):work[key]=value
    for index,row in enumerate(c.get('students',[]),1):
        for field in ROW_WORK_FIELDS:row.setdefault(field,'')
        row['row_number']=str(index)
        for field in ('program_name','program_hours','program_type'):
            row[field] = row.get(field) or (program.get('name' if field=='program_name' else 'hours','') if field!='program_type' else work['program_type'])
    row = connection.execute('SELECT fields FROM template_supplements WHERE binding=? ORDER BY id DESC LIMIT 1',(base_binding,)).fetchone()
    if row:
        extra=json.loads(row[0]); work.update(extra.get('work',{}))
        for root in ('commission','modules','documents'):
            if root in extra:c[root]=extra[root]
        for original,extra_row in zip(c['students'],extra.get('rows',[])):original.update(extra_row)
    if not c['group'].get('name') and c['students']:c['group']['name']='Индивидуальное обучение' if len(c['students'])==1 else 'Индивидуальные обучения'
    from proficentr.issued_documents import KINDS as ISSUED_KINDS
    type_map={'dpo_upskilling':'dpo_upskilling_certificate','dpo_retraining':'dpo_retraining_diploma'}
    work['qualification_document_type']=work.get('qualification_document_type') or ISSUED_KINDS.get(type_map.get(program.get('kind'),'po_profession_certificate') if program.get('direction') in ('dpo','po') else '', '')
    for row in c['students']:
        for field in ('study_form','qualification_document_type'):
            if not row.get(field):row[field]=work.get(field,'')
    return validate_context(c)


def add_background(data, image):
    """Изображение на странице позади текста; исходная геометрия текста сохраняется."""
    from docx import Document
    from docx.oxml import OxmlElement
    doc=Document(BytesIO(data))
    for section in doc.sections:
        section.header.is_linked_to_previous=False
        inline=section.header.paragraphs[0].add_run().add_picture(BytesIO(image),width=section.page_width,height=section.page_height)._inline
        anchor=OxmlElement('wp:anchor')
        for key,value in {'distT':'0','distB':'0','distL':'0','distR':'0','simplePos':'0','relativeHeight':'0','behindDoc':'1','locked':'0','layoutInCell':'1','allowOverlap':'1'}.items():anchor.set(key,value)
        simple=OxmlElement('wp:simplePos');simple.set('x','0');simple.set('y','0');anchor.append(simple)
        for axis in ('H','V'):
            position=OxmlElement('wp:position'+axis);position.set('relativeFrom','page')
            offset=OxmlElement('wp:posOffset');offset.text='0';position.append(offset);anchor.append(position)
        anchor.append(deepcopy(inline.extent));anchor.append(OxmlElement('wp:wrapNone'))
        for child in inline:
            if child.tag!=inline.extent.tag:anchor.append(deepcopy(child))
        inline.getparent().replace(inline,anchor)
    stream=BytesIO();doc.save(stream);return stream.getvalue()


class WorkingLibrary:
    def __init__(self, connection, storage, *, access_check=None):
        self.connection=connection; self.storage=storage; self.templates=TemplateRepository(connection,storage,access_check=access_check)
        self.access_check=access_check

    def access(self, permission):
        if self.access_check:self.access_check('templates.'+permission)

    def can_manage(self):
        try:self.access('write');return True
        except PermissionError:return False

    def item(self, code):
        self.access('read')
        return next(i for i in catalog() if i['code']==code)

    def current(self, code):
        self.access('read')
        cursor=self.connection.execute('SELECT * FROM working_template_events WHERE code=? ORDER BY id DESC LIMIT 1',(code,))
        row=cursor.fetchone();event=dict(zip((d[0] for d in cursor.description),row)) if row else None
        item=self.item(code)
        active=next((v for v in self.templates.list_versions(item['document_type'],item['variant']) if v.active),None)
        if active and (not event or event['effective_version_id']!=active.id):
            previous=self.connection.execute('SELECT * FROM working_template_events WHERE code=? AND effective_version_id=? ORDER BY id DESC LIMIT 1',(code,active.id))
            selected=previous.fetchone()
            if selected:return dict(zip((d[0] for d in previous.description),selected))
            return dict(code=code,base_version_id=active.id,effective_version_id=active.id,origin='custom',mode='none',background_path='',background_sha256='')
        return event

    def history(self, code):
        self.access('read')
        return self.connection.execute('SELECT origin,mode,base_version_id,effective_version_id,created_at FROM working_template_events WHERE code=? ORDER BY id DESC',(code,)).fetchall()

    def _asset_path(self, relative):
        if not re.fullmatch(r'templates/backgrounds/[a-f0-9]{32}\.(png|jpg)',relative):raise DocxError('Недопустимый путь фона.')
        path=self.storage.root/relative
        if any(p.is_symlink() for p in (path,*path.parents)) or not path.resolve().is_relative_to(self.storage.root):raise DocxError('Недопустимый каталог фона.')
        return path

    def configure(self, code, *, source=None, restore=False, background=None, mode=None):
        self.access('write');item=self.item(code);old=self.current(code)
        self.templates._require_idle();self.storage.require_available()
        base=None;origin='standard';image=None;bg_path='';bg_hash=''
        if source:
            path=Path(source).absolute()
            if any(p.is_symlink() for p in (path,*path.parents)):raise DocxError('Символические ссылки запрещены.')
            base=read_docx(path);origin='custom'
        elif old and not restore:origin=old['origin']
        else:
            base=(RESOURCE_ROOT/item['path']).read_bytes()
            if sha256(base).hexdigest()!=item['sha256']:raise DocxError('Стандартный DOCX повреждён.')
        mode=mode if mode is not None else ('none' if restore or not old else old['mode'])
        if mode not in MODES or (mode!='none' and item['document_type'] not in ('dpo_upskilling_certificate','dpo_retraining_diploma','po_profession_certificate')):
            raise DocxError('Фон доступен только для трёх выпускных бланков.')
        if old and not restore:bg_path=old['background_path'];bg_hash=old['background_sha256']
        if background:
            path=Path(background).absolute()
            if any(p.is_symlink() for p in (path,*path.parents)):raise DocxError('Символические ссылки запрещены.')
            image,extension=read_logo(path)
            bg_path='templates/backgrounds/'+uuid4().hex+extension;bg_hash=sha256(image).hexdigest()
        elif mode=='background' and bg_path:
            image=self._asset_path(bg_path).read_bytes()
            if sha256(image).hexdigest()!=bg_hash:raise DocxError('SHA-256 фона не совпадает.')
        if mode=='background' and image is None:raise DocxError('Подключите PNG/JPG для режима «С фоном».')
        owned=[]
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                if self.current(code)!=old:raise DocxError('Настройки изменились. Обновите карточку.')
                if base is None:base=read_docx(self.templates.source(old['base_version_id']).path)
                validate_template(base)
                if background:
                    path=self._asset_path(bg_path);path.parent.mkdir(parents=True,exist_ok=True)
                    with path.open('xb') as stream:
                        owned.append(path);stream.write(image);stream.flush();os.fsync(stream.fileno())
                    if sha256(path.read_bytes()).hexdigest()!=bg_hash:raise DocxError('Не удалось сохранить фон.')
                def register(data,active):
                    relative='templates/'+uuid4().hex+'.docx';path=self.storage.root/relative
                    if path.parent.is_symlink():raise DocxError('Недопустимый каталог шаблонов.')
                    return self.templates._insert(data,item['document_type'],item['variant'],item['name'],active,path,relative,owned)
                base_id=register(base,mode!='background')
                effective=base_id
                if mode=='background':
                    materialized=add_background(base,image);validate_template(materialized);effective=register(materialized,True)
                self.connection.execute('INSERT INTO working_template_events(code,base_version_id,effective_version_id,origin,mode,background_path,background_sha256,catalog_snapshot) VALUES (?,?,?,?,?,?,?,?)',(code,base_id,effective,origin,mode,bg_path,bg_hash,json.dumps(item,ensure_ascii=False)))
        except BaseException:
            for path in reversed(owned):path.unlink(missing_ok=True)
            raise
        return self.current(code)

    def save_supplement(self, context, extra):
        self.access('use');self.templates._require_idle()
        allowed={'work','rows','commission','modules','documents'}
        if type(extra) is not dict or set(extra)-allowed:raise DocxError('Недопустимые дополнительные сведения.')
        probe=deepcopy(context)
        probe['work']=extra.get('work',{})
        for root in ('commission','modules','documents'):
            if root in extra:probe[root]=extra[root]
        if type(extra.get('rows',[])) is not list:raise DocxError('Строки дополнительных сведений должны быть списком.')
        for row in extra.get('rows',[]):
            if type(row) is not dict or set(row)-set(ROW_WORK_FIELDS):raise DocxError('Дополнять можно только новые поля строки.')
        if len(extra.get('rows',[]))>len(probe.get('students',[])):raise DocxError('Лишние строки дополнительных сведений.')
        for row,value in zip(probe.get('students',[]),extra.get('rows',[])):row.update(value)
        validate_context(probe)
        with self.connection:
            self.connection.execute('INSERT INTO template_supplements(binding,fields) VALUES (?,?)',(binding(context),json.dumps(extra,ensure_ascii=False)))

    def generate(self, code, context):
        """Дополнительные сценарии создают только неподписанный проект, без регистрации приказа/выдачи."""
        self.access('use');current=self.current(code)
        if not current:raise DocxError('Сначала подключите стандартный или пользовательский шаблон.')
        self.templates._require_idle();path=None;owned=False
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                context=self.templates.complete_context(context)
                context['document'].update(is_draft=True,is_test=False)
                folder=self.templates._export_directory('working-documents');path=folder/('DRAFT-'+uuid4().hex+'.docx')
                generate_document(self.templates.source(current['effective_version_id']),context,path)
                owned=True
                self.connection.execute('INSERT INTO working_template_generations(template_version_id,relative_path,sha256,context_snapshot) VALUES (?,?,?,?)',
                    (current['effective_version_id'],path.relative_to(self.storage.root).as_posix(),sha256(path.read_bytes()).hexdigest(),json.dumps(context,ensure_ascii=False)))
        except BaseException:
            if owned:path.unlink(missing_ok=True)
            raise
        return path

    def context(self, kind, key):
        self.access('use')
        if kind=='training':
            from proficentr.admission import AdmissionRepository
            from proficentr.organization import TrainingCenterRepository
            repo=AdmissionRepository(self.connection,self.storage);t=repo.trainings.load(key);student=repo.students.load(t.student_id)
            c=empty_context();center=asdict(TrainingCenterRepository(self.connection,self.storage).load())
            c['center']={k:center.get(k,'') for k in SCHEMA['center']}
            values=asdict(student)|{'full_name':student.full_name};c['student']={k:values.get(k,'') for k in SCHEMA['student']}
            p=json.loads(t.program_snapshot);c['program']={k:p.get('full_name' if k=='name' else k,'') for k in SCHEMA['program']}
            c['training'].update(start_date=t.start_date,end_date=t.end_date,state='Подготовка')
            c['students']=[dict(c['student'],start_date=t.start_date,end_date=t.end_date)]
            return complete_context(self.connection,c)
        from proficentr.contract_repository import ContractRepository
        from proficentr.enrollment_order_repository import EnrollmentOrderRepository
        from proficentr.attestation_repository import AttestationRepository
        from proficentr.completion_repository import CompletionRepository
        from proficentr.issued_repository import IssuedDocumentRepository
        repositories={'contract':ContractRepository,'enrollment':EnrollmentOrderRepository,'protocol':AttestationRepository,'completion':CompletionRepository,'issued':IssuedDocumentRepository}
        repo=repositories[kind](self.connection,self.storage);document=repo.load(key)
        if document.state=='draft':return repo.prepare(key).context
        repo.verify_document(key)
        c=json.loads(document.snapshot)['context']
        if kind=='issued':
            c=complete_context(self.connection,c)
            c['documents']=[dict(row_number='1',student_full_name=c['student']['full_name'],program_name=c['program']['name'],
                document_type=c['issued']['kind'],document_series=c['issued']['blank_series'],document_number=c['issued']['blank_number'],
                registration_number=c['issued']['number'],issue_date=c['issued']['date'],delivery_summary='',delivery_evidence='')]
        return complete_context(self.connection,c)
