"""Приём: версионируемые требования, комплектность и подтверждения конкретного обучения."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from proficentr.document_templates import TemplateRepository
from proficentr.docx_context import SCHEMA, validate_context
from proficentr.docx_engine import generate_document, read_docx
from proficentr.document_delivery import read_evidence
from proficentr.contract_repository import encoded
from proficentr.student_repository import StudentRepository
from proficentr.training_repository import TrainingRepository
from proficentr.organization import TrainingCenterRepository
from proficentr.students import iso_date
from proficentr.programs import KINDS

STATES = {'required':'Требуется', 'not_required':'Не требуется с основанием', 'prepared':'Подготовлен',
          'received':'Получен', 'verified':'Проверен', 'absent':'Отсутствует'}
SOURCES = {'law':'Законодательство', 'local':'Локальный акт', 'special':'Требование программы'}
TYPES = {'application':'Заявление', 'consent':'Согласие', 'evidence':'Подтверждающий документ', 'condition':'Условие допуска'}
BASES = {'unknown':'Не указано', 'contract':'Договор / заключение по инициативе субъекта',
         'law':'Обязанность по закону', 'consent':'Согласие'}
EDUCATION_BASES = {'':'Не проверено', 'spo':'Имеется СПО', 'vo':'Имеется высшее образование',
    'studying_spo':'Получает СПО (подтверждено справкой)', 'studying_vo':'Получает высшее образование (подтверждено справкой)',
    'ineligible':'Нет подходящего основания допуска к ДПО'}
MIN_HOURS = {'dpo_upskilling':16, 'dpo_retraining':250}


def program_problems(program):
    minimum = MIN_HOURS.get(program['kind'])
    if minimum and program['hours'] < minimum:
        return (f"Программа «{program['full_name']}»: минимум {minimum} академических часов (приказ Минобрнауки № 266).",)
    return ()


class AdmissionError(ValueError):
    pass


@dataclass(frozen=True)
class AdmissionRule:
    rule_key: str
    title: str
    program_kind: str = ''
    program_id: int | None = None
    condition_tag: str = ''
    source: str = 'local'
    reference: str = ''
    required: bool = True
    document_type: str = 'evidence'
    sign_required: bool = False
    reusable: bool = False
    scope: str = ''
    template_variant: str = 'admission'
    waiver_allowed: bool = False
    active: bool = True
    version: int = 0
    id: int | None = None


RULE_FIELDS = tuple(AdmissionRule.__dataclass_fields__)
BOOL_FIELDS = ('required','sign_required','reusable','waiver_allowed','active')


@dataclass(frozen=True)
class AdmissionItem:
    key: str
    title: str
    source: str
    reference: str
    required: bool
    status: str
    reason: str
    document_id: int | None
    rule_signature: str


@dataclass(frozen=True)
class AdmissionReview:
    training_id: int
    signature: str
    items: tuple[AdmissionItem, ...]
    problems: tuple[str, ...]
    historical: bool = False

    @property
    def ready(self):
        return not self.problems


class AdmissionRepository:
    def __init__(self, connection, storage):
        self.connection = connection
        self.storage = storage
        self.templates = TemplateRepository(connection,storage)
        self.trainings = TrainingRepository(connection)
        self.students = StudentRepository(connection)

    @contextmanager
    def transaction(self):
        if self.connection.in_transaction:
            raise AdmissionError('Завершите текущую операцию с базой.')
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                yield
        except (sqlite3.Error,OSError):
            raise AdmissionError('Сведения приёма не сохранены. Проверьте базу, доступ к хранилищу и свободное место.') from None

    def rules(self, *, include_history=False):
        query = ('SELECT * FROM admission_rules ORDER BY id' if include_history else
                 'SELECT * FROM admission_rules WHERE id IN (SELECT max(id) FROM admission_rules GROUP BY rule_key) ORDER BY id')
        rows = self.connection.execute(query).fetchall()
        result=[]
        for row in rows:
            values=dict(zip(RULE_FIELDS,row))
            for key in BOOL_FIELDS:values[key]=bool(values[key])
            result.append(AdmissionRule(**values))
        return result

    def save_rule(self, rule, *, responsible, day, expected_version=0):
        self._who(responsible,day)
        if not re.fullmatch('[a-z][a-z0-9_]{0,59}',rule.rule_key):
            raise AdmissionError('Ключ требования: латинские буквы, цифры и подчёркивания, до 60 знаков.')
        kinds = {'','dpo','po',*(k for values in KINDS.values() for k in values)}
        if rule.program_kind not in kinds or rule.source not in SOURCES or rule.document_type not in TYPES:
            raise AdmissionError('Проверьте вид обучения, источник и тип документа.')
        if not rule.title.strip() or not rule.reference.strip():
            raise AdmissionError('Укажите название требования и точное основание.')
        if any(type(getattr(rule,k)) is not bool for k in BOOL_FIELDS):
            raise AdmissionError('Проверьте признаки требования.')
        if any(not isinstance(getattr(rule,k),str) or len(getattr(rule,k))>2000 for k in
               ('title','reference','condition_tag','scope','template_variant')):
            raise AdmissionError('Поля требования должны быть строками до 2000 знаков.')
        if rule.rule_key == 'pd_consent':
            raise AdmissionError('Основное согласие определяется правовым основанием обработки в конкретном приёме.')
        if rule.document_type == 'consent' and (not rule.scope.strip() or rule.required or not rule.sign_required):
            raise AdmissionError('Дополнительное согласие требует области действия и подтверждения подписания; оно не является условием зачисления.')
        if rule.document_type=='application' and rule.reusable:
            raise AdmissionError('Заявление привязано к конкретному обучению и не используется для другого приёма.')
        if rule.reusable and not rule.scope.strip():
            raise AdmissionError('Для повторного использования укажите конкретную область действия подтверждения.')
        if rule.waiver_allowed and rule.source == 'law':
            raise AdmissionError('Обязательное требование закона нельзя отменить отметкой сотрудника.')
        if rule.program_id is not None:
            if not self.connection.execute('SELECT 1 FROM programs WHERE id=?',(rule.program_id,)).fetchone():
                raise AdmissionError('Программа не найдена.')
        with self.transaction():
            previous = next((r for r in self.rules() if r.rule_key==rule.rule_key),None)
            if (previous.version if previous else 0) != expected_version:
                raise AdmissionError('Перечень изменился. Откройте требование заново.')
            values = asdict(rule); values.update(id=None,version=expected_version+1)
            self.connection.execute('INSERT INTO admission_rules VALUES ('+','.join('?' for _ in RULE_FIELDS)+')',tuple(values[k] for k in RULE_FIELDS))  # nosec B608 # Fixed dataclass fields; values bound.
            self.connection.execute('INSERT INTO admission_rule_history(rule_key,version,responsible,day) VALUES (?,?,?,?)',
                                    (rule.rule_key,expected_version+1,responsible.strip(),day))
        return next(r for r in self.rules() if r.rule_key==rule.rule_key)

    def program_options(self):
        return self.connection.execute('SELECT id,full_name FROM programs ORDER BY full_name').fetchall()

    def training_options(self,query=''):
        rows=self.connection.execute('SELECT t.id,s.surname,s.first_name,s.patronymic,t.program_snapshot,t.start_date FROM trainings t JOIN students s ON s.id=t.student_id ORDER BY t.id DESC').fetchall()
        result=[]
        for key,surname,first,patronymic,snapshot,start in rows:
            name=' '.join(v for v in (surname,first,patronymic) if v)
            if query.strip().casefold() in name.casefold():result.append((key,name,snapshot,start))
        return result

    def _who(self,responsible,day):
        if not isinstance(responsible,str) or not responsible.strip() or len(responsible)>500:
            raise AdmissionError('Укажите ответственное лицо (до 500 знаков).')
        if iso_date(day,'Дата подтверждения')>date.today():
            raise AdmissionError('Дата подтверждения не может быть в будущем.')

    def _editable(self,training_id,signature):
        training = self.trainings.load(training_id)
        if training.state!='preparation' or self.connection.execute('SELECT 1 FROM training_enrollments WHERE training_id=?',(training_id,)).fetchone():
            raise AdmissionError('Приём уже завершён либо обучение исключено. Исторические подтверждения не изменяются.')
        if self.review(training_id).signature != signature:
            raise AdmissionError('Сведения или требования приёма изменились. Повторите проверку.')
        return training

    def context(self,training_id):
        row = self.connection.execute('SELECT id,pd_basis,pd_reference,pd_scope,conditions,note FROM admission_context_events WHERE training_id=? ORDER BY id DESC LIMIT 1',(training_id,)).fetchone()
        return dict(zip(('id','pd_basis','pd_reference','pd_scope','conditions','note'),row)) if row else dict(id=None,pd_basis='unknown',pd_reference='',pd_scope='',conditions='{}',note='')

    def save_context(self,training_id,*,pd_basis,pd_reference,pd_scope,conditions,note,responsible,day,expected_signature):
        self._who(responsible,day)
        if pd_basis not in BASES or any(not isinstance(s,str) or len(s)>4000 for s in (pd_reference,pd_scope,note)):
            raise AdmissionError('Проверьте основание и область обработки персональных данных.')
        if type(conditions) is not dict or any(not isinstance(k,str) or not k or len(k)>100 or type(v) is not bool for k,v in conditions.items()):
            raise AdmissionError('Для каждого условия явно выберите «да» или «нет».')
        if any(not v for v in conditions.values()) and not note.strip():
            raise AdmissionError('Укажите основание неприменимости условий.')
        with self.transaction():
            self._editable(training_id,expected_signature)
            self.connection.execute('INSERT INTO admission_context_events(training_id,pd_basis,pd_reference,pd_scope,conditions,note,responsible,day) VALUES (?,?,?,?,?,?,?,?)',
                (training_id,pd_basis,pd_reference.strip(),pd_scope.strip(),encoded(conditions),note.strip(),responsible.strip(),day))
        return self.review(training_id)

    def applicable(self,training_id):
        t = self.trainings.load(training_id); program=json.loads(t.program_snapshot)
        context=self.context(training_id)
        return self.program_rules(program,t.program_id,pd_basis=context['pd_basis'],pd_scope=context['pd_scope'])

    def program_rules(self,program,program_id=None,*,pd_basis='unknown',pd_scope=''):
        """Единый подбор правил для предварительного перечня и конкретного приёма."""
        rules=[r for r in self.rules() if r.active and r.program_kind in ('',program['direction'],program['kind'])
               and (r.program_id is None or r.program_id==program_id)]
        if pd_basis!='unknown':rules.append(AdmissionRule('pd_consent','Отдельное согласие на обработку ПД',source='law',reference='152-ФЗ, статьи 6 и 9; основание указано для этого обучения',
                    required=pd_basis=='consent',document_type='consent',sign_required=True,reusable=True,scope=pd_scope))
        return rules

    @staticmethod
    def rule_signature(rule):
        return sha256(encoded(asdict(rule)).encode()).hexdigest()

    @staticmethod
    def training_signature(training):
        return sha256(encoded({k:getattr(training,k) for k in ('student_id','program_id','program_snapshot','start_date','end_date')}).encode()).hexdigest()

    def document(self,document_id):
        cursor=self.connection.execute('SELECT * FROM admission_documents WHERE id=?',(document_id,))
        row=cursor.fetchone()
        if row is None: raise AdmissionError('Документ приёма не найден.')
        return dict(zip((d[0] for d in cursor.description),row))

    def document_event(self,document_id):
        row=self.connection.execute('SELECT id,state,signed,day,responsible,note,education_basis FROM admission_document_events WHERE document_id=? ORDER BY id DESC LIMIT 1',(document_id,)).fetchone()
        return dict(zip(('id','state','signed','day','responsible','note','education_basis'),row)) if row else None

    def _path(self,relative):
        if not re.fullmatch(r'(documents|attachments)/admission/[a-f0-9]{32}\.(docx|pdf|png|jpg)',relative):
            raise AdmissionError('Некорректный путь документа приёма.')
        path=self.storage.root/relative
        if not path.resolve().is_relative_to(self.storage.root) or any(p.is_symlink() for p in (path,*path.parents) if p.is_relative_to(self.storage.root)):
            raise AdmissionError('Путь документа приёма выходит за пределы хранилища.')
        return path

    def file(self,document_id):
        doc=self.document(document_id)
        if not doc['file_path']: raise AdmissionError('Для этого документа вложение не сохранялось. Получение и проверка отражены в истории.')
        self.storage.require_available();path=self._path(doc['file_path'])
        try:
            if path.stat().st_size>20*1024*1024:raise AdmissionError('Документ приёма повреждён: превышен допустимый размер.')
            data=path.read_bytes()
        except OSError:
            raise AdmissionError('Архивный документ приёма недоступен. Восстановите исходную копию из резервной копии хранилища.') from None
        if sha256(data).hexdigest()!=doc['sha256']:
            raise AdmissionError('Документ приёма повреждён: SHA-256 не совпадает. Замена новым документом запрещена.')
        return path

    def copy(self,document_id,destination):
        original=self.file(document_id);doc=self.document(document_id)
        data=original.read_bytes()
        if sha256(data).hexdigest()!=doc['sha256']:raise AdmissionError('Документ изменился при чтении.')
        destination=Path(destination)
        if destination.suffix.lower()!=original.suffix:raise AdmissionError('Сохраните исходное расширение файла.')
        with destination.open('xb') as stream:
            import os
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        return destination

    def _valid_document(self,doc,rule,student_id,day,training_id=None):
        event=self.document_event(doc['id'])
        if doc['student_id']!=student_id or doc['document_type']!=rule.document_type or doc['scope']!=rule.scope:
            return 'Подтверждение относится к другому слушателю или области действия.'
        if training_id is not None and not doc['reusable']:
            binding=json.loads(doc['context_snapshot']).get('training_binding')
            if doc['training_id']!=training_id or binding!=self.training_signature(self.trainings.load(training_id)):
                return 'Документ относится к прежним условиям обучения. Требуется новое подтверждение.'
        if doc['valid_until'] and doc['valid_until']<day: return 'Срок действия подтверждения истёк.'
        if not event or event['state']!='verified':return 'Подтверждение не проверено либо отозвано.'
        if rule.rule_key=='dpo_education' and event['education_basis'] not in ('spo','vo','studying_spo','studying_vo'):
            return 'Не подтверждено наличие СПО/ВО или обучение в СПО/ВО.'
        if rule.sign_required and not event['signed']:return 'Нет подтверждения подписания.'
        if doc['file_path']:
            try:self.file(doc['id'])
            except (AdmissionError,OSError) as error:return str(error)
        return ''

    def review(self,training_id,*,day=None):
        day=day or date.today().isoformat()
        training=self.trainings.load(training_id)
        frozen=self.connection.execute('SELECT assessment FROM admission_enrollment_checks a JOIN training_enrollments e ON e.order_id=a.order_id WHERE e.training_id=?',(training_id,)).fetchone()
        if frozen:
            assessment=next(r for r in json.loads(frozen[0]) if r['training_id']==training_id)
            return AdmissionReview(training_id,assessment['signature'],tuple(AdmissionItem(**i) for i in assessment['items']),tuple(assessment['problems']),True)
        if training.state in ('enrolled','completed'):
            return AdmissionReview(training_id,sha256(encoded(asdict(training)).encode()).hexdigest(),(),
                ('Для прежнего зачисления комплектность в P03.5 не фиксировалась. Исторические документы не изменены.',),True)
        student=self.students.load(training.student_id);program=json.loads(training.program_snapshot)
        context=self.context(training_id);flags=json.loads(context['conditions']);items=[];problems=[];evidence=[]
        if training.state=='excluded':problems.append('Обучение исключено из подготовительного состава; зачисление недоступно.')
        problems.extend(program_problems(program))
        if context['pd_basis']=='unknown' or not context['pd_reference'] or not context['pd_scope']:
            problems.append(f'{student.full_name} — не указаны правовое основание, реквизиты основания и область обработки персональных данных.')
        for rule in self.applicable(training_id):
            signature=self.rule_signature(rule)
            row=self.connection.execute('SELECT id,state,document_id,reason,rule_signature,training_signature FROM admission_checks WHERE training_id=? AND rule_key=? ORDER BY id DESC LIMIT 1',(training_id,rule.rule_key)).fetchone()
            state,doc_id,reason='required',None,''
            applicable=True
            if rule.condition_tag:
                flag=flags.get(rule.condition_tag)
                if flag is None and rule.required:problems.append(f'{rule.title}: укажите применимость условия для этого приёма.')
                elif not flag:applicable=False;state='not_required';reason=context['note']
            if rule.rule_key=='pd_consent' and context['pd_basis'] in ('contract','law'):
                applicable=False;state='not_required';reason=context['pd_reference']
            if not applicable and row and rule.rule_key=='pd_consent':doc_id=row[2]
            if applicable and row:
                _,state,doc_id,reason,previous_signature,training_signature=row
                if previous_signature!=signature or training_signature!=self.training_signature(training):
                    state='required';reason='Требование или условия обучения изменились: нужна повторная проверка.'
                if state=='not_required' and (not rule.waiver_allowed or not reason):state='required'
                if state=='verified' and doc_id is not None:
                    doc=self.document(doc_id);error=self._valid_document(doc,rule,training.student_id,day,training_id)
                    evidence.append((doc,self.document_event(doc_id)))
                    if error:state='absent';reason=error
            required=bool(rule.required and applicable)
            item=AdmissionItem(rule.rule_key,rule.title,rule.source,rule.reference,required,state,reason,doc_id,signature)
            items.append(item)
            if required and state not in ('verified','not_required'):
                problems.append(f'{student.full_name} — {rule.title}: {reason or STATES[state]}.')
        signature=sha256(encoded({'training':asdict(training),'student':asdict(student),'context':context,
            'items':[asdict(i) for i in items],'evidence':evidence,'day':day}).encode()).hexdigest()
        return AdmissionReview(training_id,signature,tuple(items),tuple(problems))

    def _event(self,document_id,state,signed,day,responsible,note,education_basis=''):
        doc=self.document(document_id)
        latest=self.document_event(document_id)
        if latest and latest['state']=='revoked' and state!='revoked':
            raise AdmissionError('Отозванное согласие не восстанавливается отметкой. Зарегистрируйте новое согласие.')
        if day<doc['document_date'] or (latest and day<latest['day']):
            raise AdmissionError('Дата события не может предшествовать документу или предыдущему событию.')
        if state=='verified' and doc['rule_key']=='dpo_education' and education_basis not in ('spo','vo','studying_spo','studying_vo'):
            raise AdmissionError('Для допуска к ДПО подтвердите СПО/ВО или обучение в СПО/ВО; другое образование не подходит.')
        self.connection.execute('INSERT INTO admission_document_events(document_id,state,signed,day,responsible,note,education_basis) VALUES (?,?,?,?,?,?,?)',
                                (document_id,state,signed,day,responsible.strip(),note.strip(),education_basis))

    def _check(self,training_id,rule,state,document_id,reason,day,responsible):
        self.connection.execute('INSERT INTO admission_checks(training_id,rule_key,state,document_id,reason,day,responsible,rule_signature,rule_snapshot,training_signature) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (training_id,rule.rule_key,state,document_id,reason.strip(),day,responsible.strip(),self.rule_signature(rule),encoded(asdict(rule)),self.training_signature(self.trainings.load(training_id))))

    def record(self,training_id,key,*,state,reason,day,responsible,expected_signature,signed=False,source=None,valid_until='',reusable=False,document_id=None,education_basis=''):
        self._who(responsible,day)
        if state not in STATES or state=='prepared' or type(signed) is not bool or type(reusable) is not bool:
            raise AdmissionError('Выберите состояние документа; «Подготовлен» устанавливается при формировании DOCX.')
        if education_basis not in EDUCATION_BASES:raise AdmissionError('Выберите известное основание допуска к ДПО.')
        if not isinstance(reason,str) or not reason.strip() or len(reason)>4000:raise AdmissionError('Укажите подтверждение проверки или основание изменения (до 4000 знаков).')
        if valid_until and iso_date(valid_until,'Срок действия')<iso_date(day,'Дата подтверждения'):raise AdmissionError('Срок действия раньше даты подтверждения.')
        content=None
        if source is not None:content=read_evidence(source)
        path=None
        try:
            with self.transaction():
                t=self._editable(training_id,expected_signature)
                rule=next((r for r in self.applicable(training_id) if r.rule_key==key),None)
                if rule is None:raise AdmissionError('Требование больше не применяется к обучению.')
                if state=='not_required' and not rule.waiver_allowed:raise AdmissionError('Это требование не отменяется вручную: укажите применимые условия или правовое основание.')
                if rule.document_type=='condition' and (source is not None or document_id is not None):raise AdmissionError('Условие допуска подтверждается отметкой; вложения учитываются отдельным документом.')
                if state=='verified' and rule.sign_required and not signed and document_id is None:raise AdmissionError('Отдельно подтвердите подписание документа.')
                if state in ('received','verified') and rule.document_type!='condition':
                    if document_id is not None:
                        if content is not None:raise AdmissionError('Существующее вложение не заменяется. Зарегистрируйте новый документ.')
                        doc=self.document(document_id)
                        if doc['file_path']:self.file(document_id)
                        if doc['training_id']!=training_id:
                            if not doc['reusable'] or not rule.reusable:raise AdmissionError('Это подтверждение не разрешено использовать повторно.')
                            error=self._valid_document(doc,rule,t.student_id,day,training_id)
                            if error:raise AdmissionError(error)
                        else:
                            if not doc['reusable'] and json.loads(doc['context_snapshot']).get('training_binding')!=self.training_signature(t):
                                raise AdmissionError('Документ относится к прежним условиям обучения. Зарегистрируйте новое подтверждение.')
                            if rule.sign_required and state=='verified' and not signed:raise AdmissionError('Подтвердите подписание документа отдельно от формирования DOCX.')
                            if doc['student_id']!=t.student_id or doc['document_type']!=rule.document_type or doc['scope']!=rule.scope:raise AdmissionError('Документ относится к другому требованию или области действия.')
                            previous=self.document_event(document_id)
                            self._event(document_id,state,signed,day,responsible,reason,education_basis or (previous['education_basis'] if previous else ''))
                    else:
                        relative='';digest=''
                        if content:
                            self.storage.require_available();data,extension=content
                            relative='attachments/admission/'+uuid4().hex+'.'+extension
                            candidate=self._path(relative);candidate.parent.mkdir(parents=True,exist_ok=True)
                            self._publish_bytes(candidate,data);path=candidate;digest=sha256(data).hexdigest()
                        document_id=self.connection.execute('INSERT INTO admission_documents(training_id,student_id,rule_key,title,document_type,scope,document_date,valid_until,reusable,file_path,sha256,context_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                            (training_id,t.student_id,key,rule.title,rule.document_type,rule.scope,day,valid_until,bool(rule.reusable and reusable),relative,digest,encoded({'training_binding':self.training_signature(t)}))).lastrowid
                        self._event(document_id,state,signed,day,responsible,reason,education_basis)
                elif document_id is not None or source is not None:raise AdmissionError('Вложение привязывается только к полученному или проверенному документу.')
                self._check(training_id,rule,state,document_id,reason,day,responsible)
        except BaseException:
            if path is not None:self._cleanup_unregistered(path)
            raise
        return self.review(training_id)

    @staticmethod
    def _publish_bytes(path,data):
        import os
        created=False
        try:
            with path.open('xb') as stream:
                created=True;stream.write(data);stream.flush();os.fsync(stream.fileno())
            if path.read_bytes()!=data:raise AdmissionError('Не удалось проверить записанное вложение.')
        except BaseException:
            if created:path.unlink(missing_ok=True)
            raise

    def _cleanup_unregistered(self,path):
        try:
            if not self.connection.execute('SELECT 1 FROM admission_documents WHERE file_path=?',(path.relative_to(self.storage.root).as_posix(),)).fetchone():path.unlink(missing_ok=True)
        except (OSError,sqlite3.Error):
            pass  # Непривязанный файл не считается зарегистрированным; архив не перезаписывается.

    def reusable_documents(self,training_id,key):
        t=self.trainings.load(training_id);rule=next((r for r in self.applicable(training_id) if r.rule_key==key),None)
        if rule is None:raise AdmissionError('Требование больше не применяется к этому обучению.')
        if not rule.reusable:return []
        result=[]
        for document_id, in self.connection.execute('SELECT id FROM admission_documents WHERE student_id=? AND reusable=1 ORDER BY id DESC',(t.student_id,)):
            doc=self.document(document_id)
            if not self._valid_document(doc,rule,t.student_id,date.today().isoformat(),training_id):result.append(doc)
        return result

    def revoke(self,document_id,*,day,responsible,reason):
        self._who(responsible,day)
        if not reason.strip():raise AdmissionError('Укажите основание отзыва согласия.')
        with self.transaction():
            doc=self.document(document_id)
            if doc['document_type']!='consent':raise AdmissionError('Отзыв применяется только к согласию.')
            if day<doc['document_date']:raise AdmissionError('Отзыв не может предшествовать согласию.')
            self._event(document_id,'revoked',False,day,responsible,reason)

    def prepare_form(self,training_id,key,*,template_id,day,responsible,expected_signature,valid_until=''):
        self._who(responsible,day);path=None
        if valid_until and iso_date(valid_until,'Срок действия')<iso_date(day,'Дата документа'):raise AdmissionError('Срок действия раньше даты документа.')
        try:
            with self.transaction():
                t=self._editable(training_id,expected_signature)
                rule=next((r for r in self.applicable(training_id) if r.rule_key==key),None)
                if rule is None:raise AdmissionError('Требование больше не применяется к этому обучению.')
                if key=='pd_consent':
                    c=self.context(training_id)
                    if c['pd_basis']!='consent' or not c['pd_reference'] or not c['pd_scope']:
                        raise AdmissionError('Сначала укажите согласие как правовое основание, его реквизиты и область обработки.')
                if rule.document_type not in ('application','consent'):raise AdmissionError('DOCX формируется только для заявления или отдельного согласия.')
                template=self.templates.load(template_id)
                if not template.active or template.document_type!=rule.document_type or template.variant!=rule.template_variant:
                    raise AdmissionError('Выберите активный утверждённый шаблон указанного типа и варианта.')
                context={root:dict.fromkeys(fields,'') for root,fields in SCHEMA.items() if root!='students'}
                context['students']=[]
                student=asdict(self.students.load(t.student_id));student['full_name']=self.students.load(t.student_id).full_name
                context['student']={k:student.get(k,'') for k in SCHEMA['student']}
                center=asdict(TrainingCenterRepository(self.connection,self.storage).load())
                context['center']={k:center.get(k,'') for k in SCHEMA['center']}
                p=json.loads(t.program_snapshot)
                context['program']={('name' if k=='full_name' else k):v for k,v in p.items() if k in ('full_name','direction','kind','hours','code','edition','qualification','grade')}
                context['training'].update(start_date=t.start_date,end_date=t.end_date,state='Подготовка')
                context['document'].update(date=day,number='НЕ ПОДПИСАН',is_test=False,is_draft=True)
                c=self.context(training_id)
                context['admission']={'title':rule.title,'scope':rule.scope,'pd_basis':BASES[c['pd_basis']],
                                      'pd_reference':c['pd_reference'],'pd_scope':c['pd_scope'],'valid_until':valid_until}
                context=validate_context(context)
                relative='documents/admission/'+uuid4().hex+'.docx'
                self.storage.require_available();candidate=self._path(relative);candidate.parent.mkdir(parents=True,exist_ok=True)
                generate_document(self.templates.source(template_id),context,candidate);path=candidate
                digest=sha256(read_docx(path)).hexdigest()
                document_id=self.connection.execute('INSERT INTO admission_documents(training_id,student_id,rule_key,title,document_type,scope,document_date,valid_until,reusable,file_path,sha256,template_id,template_sha256,context_snapshot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (training_id,t.student_id,key,rule.title,rule.document_type,rule.scope,day,valid_until,rule.reusable,relative,digest,template_id,template.sha256,encoded({'context':context,'training_binding':self.training_signature(t)}))).lastrowid
                self._event(document_id,'prepared',False,day,responsible,'Сформирован DOCX; подписание не подтверждено.')
                self._check(training_id,rule,'prepared',document_id,'DOCX не подтверждает подписание.',day,responsible)
        except BaseException:
            if path is not None:self._cleanup_unregistered(path)
            raise
        return document_id

    def history(self,training_id):
        return self.connection.execute('SELECT day,responsible,rule_key,state,reason,document_id FROM admission_checks WHERE training_id=? ORDER BY id',(training_id,)).fetchall()

    def history_lines(self,training_id):
        lines=[]
        for day,who,state,reason,doc_id,snapshot in self.connection.execute(
                'SELECT day,responsible,state,reason,document_id,rule_snapshot FROM admission_checks WHERE training_id=? ORDER BY id',(training_id,)):
            rule=json.loads(snapshot)
            lines.append(f"{day} · {who} · {rule['title']} · {STATES[state]} · {reason}")
            lines.append(f"Основание: {SOURCES[rule['source']]} · {rule['reference']}")
        for row in self.connection.execute('SELECT day,responsible,pd_basis,pd_reference,pd_scope,note FROM admission_context_events WHERE training_id=? ORDER BY id',(training_id,)):
            day,who,basis,reference,scope,note=row
            lines.append(f'Основание ПД: {day} · {who} · {BASES[basis]} · {reference} · {scope} · {note}')
        for day,who,state,note,document_id,signed in self.connection.execute(
                'SELECT e.day,e.responsible,e.state,e.note,e.document_id,e.signed FROM admission_document_events e WHERE e.document_id IN (SELECT document_id FROM admission_checks WHERE training_id=?) ORDER BY e.id',(training_id,)):
            title = self.document(document_id)['title']
            lines.append(f"{title}: {day} · {who} · {STATES.get(state,'Отозвано')} · подписание: {'подтверждено' if signed else 'не подтверждено'} · {note}")
        return lines

    def enrollment_problems(self,training_ids):
        return [problem for key in training_ids for problem in self.review(key).problems]

    def confirm_enrollment(self,order_id,training_ids):
        """Вызывается только внутри транзакции подтверждения приказа."""
        if not self.connection.in_transaction:raise AdmissionError('Требуется транзакция подтверждения зачисления.')
        reviews=[self.review(key) for key in training_ids]
        problems=[p for r in reviews for p in r.problems]
        if problems:raise AdmissionError('Зачисление невозможно: комплект приёма не готов.\n'+'\n'.join(problems))
        assessments=[]
        for review in reviews:
            value=asdict(review)
            value['context_event_id']=self.context(review.training_id)['id']
            value['check_ids']=[r[0] for r in self.connection.execute(
                'SELECT max(id) FROM admission_checks WHERE training_id=? GROUP BY rule_key',(review.training_id,))]
            value['document_event_ids']=[self.document_event(i.document_id)['id'] for i in review.items if i.document_id]
            assessments.append(value)
        self.connection.execute('INSERT INTO admission_enrollment_checks(order_id,assessment) VALUES (?,?)',
                                (order_id,encoded(assessments)))
