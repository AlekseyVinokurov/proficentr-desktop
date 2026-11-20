"""Реестр неизменяемых версий шаблонов и разрешение путей хранилища."""

from dataclasses import dataclass
import hashlib
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


class TemplateRepository:
    def __init__(self, connection, storage):
        self.connection = connection
        self.storage = storage

    def _require_idle(self):
        if self.connection.in_transaction:
            raise DocxError("Завершите текущую операцию с базой перед изменением шаблонов.")

    def list_versions(self, document_type=None, variant=None):
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
        return TemplateSource(path, version.sha256)

    def register(self, source, document_type, variant, name, *, activate=True):
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
        created = False
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                version = self.connection.execute(
                    "SELECT coalesce(max(version), 0) + 1 FROM document_templates WHERE document_type = ? AND variant = ?",
                    (document_type, variant),
                ).fetchone()[0]
                publish_docx(data, path)
                created = True
                if activate:
                    self.connection.execute("UPDATE document_templates SET active = 0 WHERE document_type = ? AND variant = ?", (document_type, variant))
                cursor = self.connection.execute(
                    "INSERT INTO document_templates(document_type,variant,name,version,relative_path,sha256,active) VALUES (?,?,?,?,?,?,?)",
                    (document_type, variant, name, version, relative, hashlib.sha256(data).hexdigest(), activate),
                )
                version_id = cursor.lastrowid
        except Exception as error:
            if created:
                path.unlink(missing_ok=True)
            if isinstance(error, (sqlite3.Error, OSError)):
                raise DocxError("Не удалось зарегистрировать шаблон. Прежние версии сохранены.") from None
            raise
        return self.load(version_id)

    def activate(self, version_id):
        self._require_idle()
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                version = self.load(version_id)
                # Verify both hash and current syntax before changing the active selection.
                validate_template(read_docx(self.source(version_id).path))
                self.connection.execute("UPDATE document_templates SET active = 0 WHERE document_type = ? AND variant = ?", (version.document_type, version.variant))
                self.connection.execute("UPDATE document_templates SET active = 1 WHERE id = ?", (version_id,))
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
        return generate_document(self.source(version_id), fictional_context(), destination)

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
