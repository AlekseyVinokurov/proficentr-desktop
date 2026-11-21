"""Подключение неизменяемых XLSX и чтение справочников конкретной версии."""

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from zipfile import BadZipFile, ZipFile, ZIP_STORED, ZIP_DEFLATED
from zlib import error as ZlibError

from defusedxml.ElementTree import fromstring
from defusedxml.common import DefusedXmlException
from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries
from openpyxl.xml.constants import SHEET_MAIN_NS

from proficentr.frdo_profiles import PROFILES


class FrdoError(ValueError):
    pass


@dataclass(frozen=True)
class TemplateStructure:
    kind: str
    headers: tuple[str, ...]
    dictionaries: dict[str, tuple[str, ...]]  # Ключ — буква столбца листа Шаблон.
    ooxml: dict[str, bytes]


def read_xlsx(path):
    path = Path(path)
    if path.suffix.lower() != '.xlsx' or path.stat().st_size > 20 * 1024 * 1024:
        raise FrdoError('Выберите XLSX размером до 20 МиБ.')
    return path.read_bytes()


def inspect_template(data: bytes, kind: str) -> TemplateStructure:
    if kind not in PROFILES:
        raise FrdoError('Выберите ДПО или ПО.')
    profile = PROFILES[kind]
    if len(data) > 20 * 1024 * 1024:
        raise FrdoError('Выберите XLSX размером до 20 МиБ.')
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(x.file_size for x in entries) > 100 * 1024 * 1024:
                raise FrdoError('Превышен допустимый размер распакованного XLSX.')
            if len({x.filename for x in entries}) != len(entries):
                raise FrdoError('Повторяющиеся части OOXML.')
            if any(x.flag_bits & 1 or x.compress_type not in (ZIP_STORED, ZIP_DEFLATED) for x in entries):
                raise FrdoError('Зашифрованный или неподдерживаемый ZIP внутри XLSX.')
            parts = {x.filename: archive.read(x) for x in entries}
            for name, content in parts.items():
                if name.endswith(('.xml', '.rels')):
                    xml = fromstring(content)
                    if name.endswith('.rels') and any(x.get('TargetMode') == 'External' for x in xml):
                        raise FrdoError('Внешние связи в шаблоне не поддерживаются.')
                if 'vbaProject' in name or name.startswith('xl/externalLinks/'):
                    raise FrdoError('Макросы и внешние связи не поддерживаются.')
        workbook = load_workbook(BytesIO(data), data_only=False, keep_links=False)
        try:
            if [(s.title, s.sheet_state) for s in workbook] != [('Шаблон', 'visible'), ('Проверки', 'hidden')]:
                raise FrdoError('Ожидаются видимый Шаблон и скрытый Проверки.')
            sheet = workbook['Шаблон']
            headers = tuple(c.value for c in sheet[1])
            if headers != profile['headers'] or sheet.max_row != profile['last_row']:
                raise FrdoError('Заголовки, порядок столбцов или диапазон строк не соответствуют выбранному виду.')
            if {n: v.attr_text for n, v in workbook.defined_names.items()} != profile['names']:
                raise FrdoError('Именованные диапазоны не соответствуют профилю шаблона.')
            validations = tuple((v.type, str(v.sqref), v.formula1, v.formula2) for v in sheet.data_validations.dataValidation)
            if validations != profile['validations']:
                raise FrdoError('Проверки данных и связи выпадающих списков не соответствуют профилю.')
            table = workbook['Проверки'].tables.get('Таблица1')
            if table is None or table.ref != profile['table_ref']:
                raise FrdoError('Отсутствует ожидаемая таблица Таблица1 на листе Проверки.')
            # Проверяем таблицу также непосредственно в OOXML, а не только объект openpyxl.
            ns = '{' + SHEET_MAIN_NS + '}'
            tables = [fromstring(v) for n, v in parts.items() if n.startswith('xl/tables/') and n.endswith('.xml')]
            if not any(t.tag == ns + 'table' and t.get('name') == 'Таблица1' and t.get('ref') == table.ref for t in tables):
                raise FrdoError('Таблица не подтверждена в OOXML.')
            if 'xl/styles.xml' not in parts:
                raise FrdoError('Отсутствуют форматы ячеек.')
            dictionaries = {}
            for validation in sheet.data_validations.dataValidation:
                if validation.type != 'list':
                    continue
                definition = workbook.defined_names[validation.formula1.lstrip('=')]
                expression = definition.attr_text
                if expression.startswith('Таблица1[[') and expression.endswith(']]'):
                    caption = expression[len('Таблица1[['):-2]
                    offset = [c.name for c in table.tableColumns].index(caption)
                    left, top, _, bottom = range_boundaries(table.ref)
                    cells = workbook['Проверки'].iter_rows(min_row=top + 1, max_row=bottom,
                                                          min_col=left + offset, max_col=left + offset)
                    values = [c.value for row in cells for c in row]
                else:
                    values = []
                    for title, coordinate in definition.destinations:
                        if title != 'Проверки':
                            raise FrdoError('Справочник должен находиться на листе Проверки.')
                        values.extend(c.value for row in workbook[title][coordinate] for c in row)
                if not values or any(isinstance(v, str) and v.startswith('=') for v in values):
                    raise FrdoError('Пустой или вычисляемый справочник не поддерживается.')
                exact = tuple(str(v) for v in values if v is not None)
                if not exact:
                    raise FrdoError('Пустой справочник.')
                for span in validation.sqref.ranges:
                    dictionaries[sheet.cell(1, span.min_col).column_letter] = exact
            return TemplateStructure(kind, headers, dictionaries, parts)
        finally:
            workbook.close()
    except FrdoError:
        raise
    except (BadZipFile, KeyError, ValueError, TypeError, OSError, SyntaxError, AttributeError, IndexError, DefusedXmlException, ZlibError) as error:
        raise FrdoError('Не удалось проверить структуру XLSX.') from error


@dataclass(frozen=True)
class FrdoTemplateVersion:
    id: int
    kind: str
    version: int
    original_name: str
    relative_path: str
    sha256: str
    added_at: str
    active: int


class FrdoTemplateRepository:
    def __init__(self, connection, storage):
        self.connection = connection
        self.storage = storage

    def versions(self, kind):
        if kind not in PROFILES:
            raise FrdoError('Выберите ДПО или ПО.')
        return [FrdoTemplateVersion(*r) for r in self.connection.execute(
            'SELECT id,kind,version,original_name,relative_path,sha256,added_at,active '
            'FROM frdo_templates WHERE kind=? ORDER BY version DESC', (kind,))]

    def active(self, kind):
        return next((v for v in self.versions(kind) if v.active), None)

    def source(self, version):
        self.storage.require_available()
        if not re.fullmatch(r'templates/[a-f0-9]{32}\.xlsx', version.relative_path):
            raise FrdoError('Некорректный путь шаблона.')
        path = self.storage.root / version.relative_path
        if path.parent.is_symlink() or path.is_symlink() or not path.resolve().is_relative_to(self.storage.root):
            raise FrdoError('Путь шаблона выходит за пределы хранилища.')
        data = read_xlsx(path)
        if sha256(data).hexdigest() != version.sha256:
            raise FrdoError('SHA-256 шаблона не совпадает. Файл изменён или повреждён.')
        return data

    def structure(self, kind):
        version = self.active(kind)
        if version is None:
            raise FrdoError('Шаблон не подключён.')
        return inspect_template(self.source(version), kind)

    def register(self, source, kind):
        if self.connection.in_transaction:
            raise FrdoError('Завершите текущую операцию с базой.')
        self.storage.require_available()
        data = read_xlsx(source)
        inspect_template(data, kind)
        relative = 'templates/' + uuid4().hex + '.xlsx'
        destination = self.storage.root / relative
        if destination.parent.is_symlink():
            raise FrdoError('Каталог шаблонов не должен быть символической ссылкой.')
        created = False
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                version = self.connection.execute('SELECT coalesce(max(version),0)+1 FROM frdo_templates WHERE kind=?', (kind,)).fetchone()[0]
                with destination.open('xb') as stream:
                    created = True
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                if destination.read_bytes() != data:
                    raise OSError('Копия не совпадает с исходными байтами.')
                self.connection.execute('UPDATE frdo_templates SET active=0 WHERE kind=?', (kind,))
                self.connection.execute('INSERT INTO frdo_templates(kind,version,original_name,relative_path,sha256) VALUES (?,?,?,?,?)',
                                        (kind, version, Path(source).name, relative, sha256(data).hexdigest()))
        except Exception as error:
            if created:
                destination.unlink(missing_ok=True)
            if isinstance(error, (sqlite3.Error, OSError)):
                raise FrdoError('Не удалось подключить шаблон. Прежняя версия сохранена.') from error
            raise
        return self.active(kind)
