"""Реестр неизменяемых версий шаблонов и разрешение путей хранилища."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from proficentr.docx_context import DocxError, fictional_context
from proficentr.docx_engine import generate_document, publish_docx, read_docx, validate_template

DOCUMENT_TYPES = {
    'contract_person': 'Договор с физическим лицом',
    'contract_legal': 'Договор с юридическим лицом',
    'contract_entrepreneur': 'Договор с ИП',
    'application': 'Заявление',
    'consent': 'Согласие',
    'enrollment_order': 'Приказ о зачислении',
    'completion_order': 'Приказ о завершении',
    'protocol_dpo': 'Протокол ДПО',
    'protocol_po': 'Протокол ПО',
    'dpo_upskilling_certificate': 'Удостоверение о повышении квалификации',
    'dpo_retraining_diploma': 'Диплом о профессиональной переподготовке',
    'po_profession_certificate': 'Свидетельство о профессии рабочего, должности служащего',
    'admission_checklist': 'Перечень документов для приёма',
    'expulsion_order': 'Приказ о досрочном отчислении',
    'study_certificate': 'Справка об обучении',
    'issuance_registry': 'Реестр выдачи',
    'delivery_receipt': 'Подтверждение получения',
    'other': 'Другой документ',
}


@dataclass(frozen=True)
class TemplateVersion:
    id: int
    document_type: str
    variant: str
    name: str
    version: int
    relative_path: str
    sha256: str
    added_at: str
    active: bool


@dataclass(frozen=True)
class TemplateSource:
    path: Path
    sha256: str
    required_variables: tuple[str, ...] = ()


class TemplateRepository:
    def __init__(self, connection, storage, *, access_check=None):
        self.access_check = access_check
        self.connection = connection
        self.storage = storage

    def access(self, permission):
        if self.access_check:self.access_check("templates."+permission)

    def _require_idle(self):
        if self.connection.in_transaction:
            raise DocxError("Завершите текущую операцию с базой перед изменением шаблонов.")

    def list_versions(self, document_type=None, variant=None):
        self.access("read")
        try:
            rows = self.connection.execute(
                "SELECT id, document_type, variant, name, version, relative_path, sha256, added_at, active "
                "FROM document_templates WHERE (? IS NULL OR document_type = ?) AND (? IS NULL OR variant = ?) "
                "ORDER BY document_type, variant, version DESC", (document_type, document_type, variant, variant),
            ).fetchall()
            return [TemplateVersion(*row[:-1], bool(row[-1])) for row in rows]
        except sqlite3.Error:
            raise DocxError("Не удалось прочитать реестр шаблонов.") from None

    def load(self, version_id):
        self.access("read")
        try:
            row = self.connection.execute(
                "SELECT id, document_type, variant, name, version, relative_path, sha256, added_at, active "
                "FROM document_templates WHERE id = ?", (version_id,),
            ).fetchone()
        except sqlite3.Error:
            raise DocxError("Не удалось прочитать версию шаблона.") from None
        if row is None:
            raise DocxError("Версия шаблона не найдена.")
        return TemplateVersion(*row[:-1], bool(row[-1]))

    def source(self, version_id):
        version = self.load(version_id)
        if not re.fullmatch(r'templates/[a-f0-9]{32}\.docx', version.relative_path):
            raise DocxError("Некорректный путь шаблона в реестре.")
        root = self.storage.root
        path = root / version.relative_path
        if (root / 'templates').is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise DocxError("Путь шаблона выходит за пределы локального хранилища.")
        data = read_docx(path)
        if hashlib.sha256(data).hexdigest() != version.sha256:
            raise DocxError("SHA-256 шаблона не совпадает с реестром. Исходный файл изменён или повреждён.")
        row = self.connection.execute('SELECT code,origin,catalog_snapshot FROM working_template_events WHERE effective_version_id=? ORDER BY id DESC LIMIT 1', (version_id,)).fetchone()
        required = ()
        if row and row[1]=='standard':
            required = tuple(json.loads(row[2])['required_variables'])
        return TemplateSource(path, version.sha256, required)

    def register(self, source, document_type, variant, name, *, activate=True):
        self.access("write")
        variant = ' '.join(variant.split())
        name = ' '.join(name.split())
        if document_type not in DOCUMENT_TYPES or not variant or not name or len(variant) > 100 or len(name) > 200:
            raise DocxError("Выберите тип, название (до 200 знаков) и вариант применения (до 100 знаков).")
        if type(activate) is not bool:
            raise DocxError("Укажите состояние активности версии.")
        self._require_idle()
        self.storage.require_available()
        data = read_docx(source)
        validate_template(data)
        relative = 'templates/' + uuid4().hex + '.docx'
        path = self.storage.root / relative
        if path.parent.is_symlink():
            raise DocxError("Каталог шаблонов не должен быть символической ссылкой.")
        owned = []
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                version_id = self._insert(data, document_type, variant, name, activate, path, relative, owned)
                if activate:self._record_library_selection(version_id, custom=True)
        except Exception as error:
            if path in owned:
                path.unlink(missing_ok=True)
            if isinstance(error, (sqlite3.Error, OSError)):
                raise DocxError("Не удалось зарегистрировать шаблон. Прежние версии сохранены.") from None
            raise
        return self.load(version_id)

    def _insert(self, data, document_type, variant, name, activate, path, relative, owned):
        version = self.connection.execute("SELECT coalesce(max(version), 0) + 1 FROM document_templates WHERE document_type=? AND variant=?", (document_type,variant)).fetchone()[0]
        publish_docx(data, path)
        owned.append(path)
        if activate:
            self.connection.execute("UPDATE document_templates SET active=0 WHERE document_type=? AND variant=?", (document_type,variant))
        return self.connection.execute("INSERT INTO document_templates(document_type,variant,name,version,relative_path,sha256,active) VALUES (?,?,?,?,?,?,?)", (document_type,variant,name,version,relative,hashlib.sha256(data).hexdigest(),activate)).lastrowid

    def _record_library_selection(self, version_id, *, custom=False):
        selected=self.load(version_id)
        previous=self.connection.execute('SELECT code,origin,mode,base_version_id,background_path,background_sha256,catalog_snapshot FROM working_template_events WHERE effective_version_id=? ORDER BY id DESC LIMIT 1',(version_id,)).fetchone()
        if previous and not custom:
            code,origin,mode,base,bg,digest,snapshot=previous
        else:
            from proficentr.working_library import catalog
            item=next((item for item in catalog() if (item['document_type'],item['variant'])==(selected.document_type,selected.variant)),None)
            if not item:return
            code=item['code'];origin='custom';mode='none';base=version_id;bg='';digest='';snapshot=json.dumps(item,ensure_ascii=False)
        self.connection.execute('INSERT INTO working_template_events(code,base_version_id,effective_version_id,origin,mode,background_path,background_sha256,catalog_snapshot) VALUES (?,?,?,?,?,?,?,?)',(code,base,version_id,origin,mode,bg,digest,snapshot))

    def supports_contract(self, version_id, contract, trainings):
        row=self.connection.execute('SELECT origin FROM working_template_events WHERE effective_version_id=? ORDER BY id DESC LIMIT 1',(version_id,)).fetchone()
        if row and row[0]=='standard' and self.load(version_id).document_type=='contract_person':
            return contract.customer_type=='student' and len(trainings)==1 and all(t['student_id']==contract.student_id for t in trainings)
        return True

    def complete_context(self, context):
        from proficentr.working_library import complete_context
        return complete_context(self.connection, context)

    def activate(self, version_id):
        self.access("write")
        self._require_idle()
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                version = self.load(version_id)
                # Verify both hash and current syntax before changing the active selection.
                validate_template(read_docx(self.source(version_id).path))
                self.connection.execute("UPDATE document_templates SET active = 0 WHERE document_type = ? AND variant = ?", (version.document_type, version.variant))
                self.connection.execute("UPDATE document_templates SET active = 1 WHERE id = ?", (version_id,))
                self._record_library_selection(version_id)
        except sqlite3.Error:
            raise DocxError("Не удалось активировать версию. Прежняя активная версия сохранена.") from None
        return self.load(version_id)

    def active_version(self, document_type, variant):
        versions = [version for version in self.list_versions(document_type, variant) if version.active]
        if not versions:
            raise DocxError("Нет активной версии для выбранного типа и варианта.")
        return versions[0]

    def export_copy(self, version_id, destination):
        source = self.source(version_id)
        data = read_docx(source.path)
        if hashlib.sha256(data).hexdigest() != source.sha256:
            raise DocxError("SHA-256 шаблона не совпадает с реестром.")
        validate_template(data)
        return publish_docx(data, destination)

    def test_document(self, version_id):
        folder = self._export_directory('template-tests')
        destination = folder / ('TEST-unregistered-' + uuid4().hex + '.docx')
        source=self.source(version_id);context=fictional_context()
        if source.required_variables:
            def fill(value):
                if type(value) is dict:return {k:fill(v) for k,v in value.items()}
                if type(value) is list:return [fill(v) for v in value]
                return 'Вымышленные данные' if value=='' else value
            context=fill(context)
        return generate_document(source, context, destination)

    def editable_copy(self, version_id):
        folder = self._export_directory('template-edits')
        return self.export_copy(version_id, folder / ('edit-' + uuid4().hex + '.docx'))

    def _export_directory(self, category):
        self.storage.require_available()
        folder = self.storage.root / 'exports' / category
        if (self.storage.root / 'exports').is_symlink() or folder.is_symlink():
            raise DocxError("Каталог копий не должен быть символической ссылкой.")
        folder.mkdir(parents=True, exist_ok=True)
        return folder
