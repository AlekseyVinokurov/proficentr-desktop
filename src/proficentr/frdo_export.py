"""Неизменяемые пакеты ФРДО: исходный OOXML, журнал и общая нумерация."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from xml.etree.ElementTree import Element, SubElement, tostring  # nosec B405 # Construction/serialization only; all parsing uses defusedxml.
from zipfile import ZipFile

from defusedxml.ElementTree import fromstring
from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import to_excel, MAC_EPOCH, WINDOWS_EPOCH
from openpyxl.xml.constants import SHEET_MAIN_NS, REL_NS, PKG_REL_NS

from proficentr.frdo_preparation import FrdoPreparationRepository, PreparedRecord, encoded
from proficentr.frdo_profiles import PROFILES
from proficentr.frdo_templates import FrdoError, inspect_template, read_xlsx
from proficentr.frdo_validation import FIELDS, export_filename

NS = '{' + SHEET_MAIN_NS + '}'


def capacity(kind):
    return PROFILES[kind]['last_row'] - 1


def _sheet_path(parts):
    workbook = fromstring(parts['xl/workbook.xml'])
    sheet = next(s for s in workbook.find(NS + 'sheets') if s.get('name') == 'Шаблон')
    key = sheet.get('{' + REL_NS + '}id')
    links = fromstring(parts['xl/_rels/workbook.xml.rels'])
    target = next(r.get('Target') for r in links if r.tag == '{' + PKG_REL_NS + '}Relationship' and r.get('Id') == key)
    path = target.lstrip('/') if target.startswith('/') else 'xl/' + target
    if path not in parts or '..' in Path(path).parts:
        raise FrdoError('Не найден безопасный путь листа «Шаблон».')
    return path


def fill_xlsx(source, kind, records, *, structure=None):
    """Меняется только sheetData. Прочие XML и части ZIP сохраняются побайтно.

    Реквизиты — inlineStr; даты и годы/часы — числовые значения Excel.
    Не используем save openpyxl: он теряет неизвестные расширения OOXML.
    """
    records = tuple(records)
    if not records or len(records) > capacity(kind):
        raise FrdoError('Количество строк превышает вместимость шаблона или набор пуст.')
    structure = structure or inspect_template(source, kind)
    parts = structure.ooxml
    path = _sheet_path(parts)
    original = parts[path]
    # Профиль уже проверен безопасным XML-парсером. Выделяем только элемент
    # sheetData, оставляя атрибуты корня, расширения и служебные узлы нетронутыми.
    match = re.search(rb'<(?P<prefix>[A-Za-z_][\w.-]*:)?sheetData(?:\s[^>]*)?>.*?</(?P=prefix)sheetData>', original, re.DOTALL)
    if match is None:  # В стандартных XLSX используется пространство имён по умолчанию.
        match = re.search(rb'<sheetData(?:\s[^>]*)?>.*?</sheetData>', original, re.DOTALL)
    if match is None:
        raise FrdoError('Не найден раздел строк исходного XLSX.')
    root = fromstring(original)
    data = root.find(NS + 'sheetData')
    rows = {int(r.get('r')): r for r in data}
    fields = FIELDS[kind]
    date_columns = {'H', fields['birth']}
    number_columns = {fields['start'], fields['end'], fields['hours']}
    properties = fromstring(parts['xl/workbook.xml']).find(NS + 'workbookPr')
    epoch = MAC_EPOCH if properties is not None and properties.get('date1904') in ('1', 'true') else WINDOWS_EPOCH
    columns = [get_column_letter(i + 1) for i in range(len(structure.headers))]
    for row_number in range(2, PROFILES[kind]['last_row'] + 1):
        row = rows.get(row_number)
        values = records[row_number - 2].values if row_number - 2 < len(records) else {}
        if row is None:
            if not values:
                continue
            row = Element(NS + 'row', {'r': str(row_number)})
            rows[row_number] = row
        cells = {c.get('r'): c for c in row}
        for column in columns:
            address = column + str(row_number)
            cell = cells.get(address)
            # Служебные формулы не подменяем данными. Для такого шаблона нужна
            # явная адаптация профиля, а не тихая потеря вычислений.
            if cell is not None and cell.find(NS + 'f') is not None:
                raise FrdoError(f'В области данных шаблона обнаружена формула {address}.')
            value = values.get(column, '')
            if not isinstance(value, str):
                raise FrdoError(f'Значение {address} должно быть строкой.')
            if cell is None:
                if not value:
                    continue
                cell = SubElement(row, NS + 'c', {'r': address})
                cells[address] = cell
            for child in list(cell):
                if child.tag in (NS + 'v', NS + 'is'):
                    cell.remove(child)
            cell.attrib.pop('t', None)
            if value:
                if column in date_columns:
                    SubElement(cell, NS + 'v').text = str(to_excel(datetime.strptime(value, '%d.%m.%Y'), epoch))
                elif column in number_columns:
                    SubElement(cell, NS + 'v').text = str(int(value))
                else:
                    cell.set('t', 'inlineStr')
                    SubElement(SubElement(cell, NS + 'is'), NS + 't').text = value
        # Новые ячейки должны идти в порядке столбцов, исходные атрибуты остаются.
        row[:] = sorted(row, key=lambda c: (len(re.sub(r'\d', '', c.get('r', ''))), c.get('r', '')))
    data[:] = [rows[n] for n in sorted(rows)]
    updated = original[:match.start()] + tostring(data, encoding='utf-8') + original[match.end():]
    output = BytesIO()
    with ZipFile(BytesIO(source)) as old, ZipFile(output, 'w') as new:
        new.comment = old.comment
        for info in old.infolist():
            new.writestr(info, updated if info.filename == path else parts[info.filename])
    result = output.getvalue()
    verified = inspect_template(result, kind)
    if any(verified.ooxml[name] != content for name, content in parts.items() if name != path):
        raise FrdoError('Нарушена сохранность служебных частей XLSX.')
    return result


@dataclass(frozen=True, repr=False)
class ExportSelection:
    records: tuple[PreparedRecord, ...]
    ogrn: str
    kpp: str
    year: int
    kind: str


@dataclass(frozen=True)
class FrdoPackage:
    id: str
    request_key: str
    part_index: int
    kind: str
    ogrn: str
    kpp: str
    issue_year: int
    number: int
    created_at: str
    template_id: int
    template_version: int
    template_sha256: str
    relative_path: str
    sha256: str
    row_count: int

    @property
    def filename(self):
        return Path(self.relative_path).name


PACKAGE_SELECT = ('SELECT id,request_key,part_index,kind,ogrn,kpp,issue_year,number,created_at,'
                  'template_id,template_version,template_sha256,relative_path,sha256,row_count FROM frdo_packages')


class PreviousExportError(FrdoError):
    pass


def _sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_file(path, data):
    created = False
    try:
        with path.open('xb') as stream:
            created = True
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(path.parent)
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise


class FrdoExportRepository:
    def __init__(self, connection, storage):
        self.connection = connection
        self.storage = storage
        self.preparation = FrdoPreparationRepository(connection, storage)

    def select(self, records):
        records = tuple(sorted(records, key=lambda r: r.document_id))
        if not records or len({r.document_id for r in records}) != len(records):
            raise FrdoError('Выберите непустой набор разных выпускных документов.')
        errors = [f'Документ {r.number}: ' + '; '.join(r.messages()) for r in records if not r.ready]
        if errors:
            raise FrdoError('XLSX не сформирован. Исправьте все выбранные записи:\n' + '\n'.join(errors))
        kinds = {r.kind for r in records}
        years = {int(r.document_date[:4]) for r in records}
        if len(kinds) != 1 or len(years) != 1:
            raise FrdoError('Выберите одну категорию и один год выдачи документов.')
        organizations = set()
        for record in records:
            document, snapshot, _, _ = self.preparation._source(record.document_id)
            center = snapshot['context']['center']
            ogrn, kpp = center.get('ogrn', ''), center.get('kpp', '')
            try:
                export_filename(ogrn, kpp, next(iter(years)), 1)
            except ValueError:
                raise FrdoError(f'Документ {document.number}: в исторических реквизитах организации нужны ОГРН (13 цифр) и КПП (9 цифр).') from None
            organizations.add((ogrn, kpp))
        if len(organizations) != 1:
            raise FrdoError('Выбранные документы выданы разными организациями. Сформируйте отдельные наборы.')
        ogrn, kpp = organizations.pop()
        return ExportSelection(records, ogrn, kpp, years.pop(), kinds.pop())

    def packages(self):
        return [FrdoPackage(*row) for row in self.connection.execute(
            PACKAGE_SELECT + ' ORDER BY created_at DESC,number DESC')]

    def load(self, package_id):
        row = self.connection.execute(PACKAGE_SELECT + ' WHERE id=?', (package_id,)).fetchone()
        if row is None:
            raise FrdoError('Пакет ФРДО не найден.')
        return FrdoPackage(*row)

    def _path(self, relative):
        if not re.fullmatch(r'exports/frdo/[a-f0-9]{32}/[0-9]{13}_[0-9]{9}_[0-9]{2}_[0-9]+\.xlsx', relative):
            raise FrdoError('Некорректный путь пакета ФРДО.')
        path = self.storage.root / relative
        if any(p.is_symlink() for p in (path, *path.parents) if p != self.storage.root.parent) or not path.resolve().is_relative_to(self.storage.root):
            raise FrdoError('Путь пакета ФРДО выходит за пределы хранилища.')
        return path

    def file(self, package_id):
        self.storage.require_available()
        package = self.load(package_id)
        path = self._path(package.relative_path)
        try:
            if sha256(read_xlsx(path)).hexdigest() != package.sha256:
                raise FrdoError(f'Пакет № {package.number} повреждён: SHA-256 не совпадает. Восстановите исходный файл из резервной копии.')
        except OSError:
            raise FrdoError(f'Файл пакета № {package.number} недоступен. Восстановите его из резервной копии.') from None
        return path

    def copy(self, package_id, destination):
        package = self.load(package_id)
        data = self.file(package_id).read_bytes()
        if sha256(data).hexdigest() != package.sha256:
            raise FrdoError('Сохранённый пакет изменился при чтении.')
        destination = Path(destination)
        if destination.suffix.lower() != '.xlsx':
            raise FrdoError('Укажите имя файла с расширением .xlsx.')
        try:
            _write_file(destination, data)
        except FileExistsError:
            raise FrdoError('Файл назначения уже существует. Выберите другое имя.') from None
        except OSError:
            raise FrdoError('Копия XLSX не сохранена. Проверьте место и доступ к выбранной папке.') from None
        return destination

    def restore(self, package_id, source):
        """Восстановление только отсутствующего файла и только идентичных байтов."""
        self.storage.require_available()
        package = self.load(package_id)
        try:
            data = read_xlsx(source)
        except OSError:
            raise FrdoError('Исходная копия XLSX недоступна для чтения.') from None
        if sha256(data).hexdigest() != package.sha256:
            raise FrdoError('Для восстановления нужен исходный XLSX с SHA-256 из журнала.')
        inspect_template(data, package.kind)
        path = self._path(package.relative_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_file(path, data)
        except FileExistsError:
            raise FrdoError('Файл уже существует. Сохраните повреждённый экземпляр отдельно перед восстановлением.') from None
        except OSError:
            raise FrdoError('Файл не восстановлен. Проверьте место и доступность хранилища.') from None
        return self.file(package_id)

    def unregistered_files(self):
        """Остатки аварии не получают номера и не считаются пакетами; не удаляем."""
        folder = self.storage.root / 'exports' / 'frdo'
        if folder.is_symlink():
            raise FrdoError('Каталог ФРДО не должен быть символической ссылкой.')
        registered = {p.relative_path for p in self.packages()}
        return tuple(p for p in folder.glob('*/*.xlsx') if p.relative_to(self.storage.root).as_posix() not in registered)

    def create(self, selection, *, allow_reexport=False):
        if self.connection.in_transaction:
            raise FrdoError('Завершите текущую операцию с базой.')
        self.storage.require_available()
        created = []
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                current = self.select(selection.records)
                if current != selection:
                    raise FrdoError('Состав набора изменился. Повторите проверку.')
                template = self.preparation.templates.active(selection.kind)
                if template is None:
                    raise FrdoError('Активный шаблон не подключён.')
                source = self.preparation.templates.source(template)
                structure = inspect_template(source, selection.kind)
                problems = []
                for old in selection.records:
                    try:
                        fresh = self.preparation.review(old.document_id, template=template, structure=structure)
                        if fresh != old:
                            problems.append(f'Документ {old.number}: сведения или шаблон изменились. Повторите проверку готовности.')
                        if not fresh.ready:
                            problems.extend(f'Документ {old.number}: {message}' for message in fresh.messages())
                    except FrdoError as error:
                        problems.append(f'Документ {old.number}: {error}')
                if problems:
                    raise FrdoError('XLSX не сформирован.\n' + '\n'.join(problems))
                key = sha256(encoded({'kind': selection.kind, 'year': selection.year, 'ogrn': selection.ogrn,
                    'kpp': selection.kpp, 'records': [(r.document_id, r.signature) for r in selection.records]}).encode()).hexdigest()
                existing = [p for p in self.packages() if p.request_key == key]
                if existing:
                    for p in existing:
                        self.file(p.id)
                    return sorted(existing, key=lambda p: p.part_index)
                previous = []
                for record in selection.records:
                    numbers = self.connection.execute('SELECT p.number FROM frdo_package_documents d JOIN frdo_packages p ON p.id=d.package_id WHERE d.document_id=? ORDER BY p.number', (record.document_id,)).fetchall()
                    if numbers:
                        previous.append(f'Документ {record.number}: пакеты ' + ', '.join(str(n[0]) for n in numbers))
                if previous and not allow_reexport:
                    raise PreviousExportError('Эти документы уже включались в XLSX. Прежние файлы останутся неизменными. Создать новый пакет?\n' + '\n'.join(previous))
                number = self.connection.execute('SELECT coalesce(max(number),0) FROM frdo_packages WHERE ogrn=? AND kpp=? AND issue_year=?', (selection.ogrn, selection.kpp, selection.year)).fetchone()[0]
                result = []
                for offset in range(0, len(selection.records), capacity(selection.kind)):
                    chunk = selection.records[offset:offset + capacity(selection.kind)]
                    data = fill_xlsx(source, selection.kind, chunk, structure=structure)
                    number += 1
                    package_id = uuid4().hex
                    relative = f'exports/frdo/{package_id}/' + export_filename(selection.ogrn, selection.kpp, selection.year, number)
                    path = self._path(relative)
                    path.parent.mkdir(parents=True, exist_ok=False)
                    _sync_directory(path.parent.parent)
                    _sync_directory(self.storage.root / 'exports')
                    created.append(path)
                    _write_file(path, data)
                    digest = sha256(data).hexdigest()
                    if sha256(read_xlsx(path)).hexdigest() != digest:
                        raise FrdoError('Не удалось проверить записанный XLSX.')
                    self.connection.execute('INSERT INTO frdo_packages(id,request_key,part_index,kind,ogrn,kpp,issue_year,number,template_id,template_version,template_sha256,relative_path,sha256,row_count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (package_id, key, offset // capacity(selection.kind), selection.kind, selection.ogrn, selection.kpp, selection.year, number, template.id, template.version, template.sha256, relative, digest, len(chunk)))
                    self.connection.executemany('INSERT INTO frdo_package_documents(package_id,position,document_id,source_signature) VALUES (?,?,?,?)',
                        [(package_id, i, r.document_id, r.signature) for i, r in enumerate(chunk)])
                    result.append(self.load(package_id))
                # Замена/повреждение исходного файла во время записи не должны пройти незаметно.
                self.preparation.templates.source(template)
            return result
        except BaseException as error:
            # При неопределённом результате COMMIT не удаляем потенциально
            # зарегистрированные файлы. Повторный запуск сверит журнал и SHA.
            for path in created:
                try:
                    registered = self.connection.execute('SELECT 1 FROM frdo_packages WHERE relative_path=?', (path.relative_to(self.storage.root).as_posix(),)).fetchone()
                    if not registered:
                        path.unlink(missing_ok=True)
                        path.parent.rmdir()
                except (OSError, sqlite3.Error):
                    pass  # Остаток доступен через unregistered_files, не считается пакетом.
            if isinstance(error, (OSError, sqlite3.Error)):
                raise FrdoError('Не удалось завершить формирование XLSX. Проверьте свободное место и доступность хранилища; проверьте журнал перед новой попыткой.') from error
            raise
