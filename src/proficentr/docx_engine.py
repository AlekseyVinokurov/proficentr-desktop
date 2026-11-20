"""Проверка DOCX и ограниченная шаблонизация без интерфейса и сетевых функций."""

from io import BytesIO
import hashlib
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import sys
import tempfile
from zipfile import ZipFile, BadZipFile, ZIP_STORED, ZIP_DEFLATED

from defusedxml import ElementTree as SafeXML
from docxtpl import DocxTemplate
from docx.opc.constants import NAMESPACE as NS, RELATIONSHIP_TYPE as RT
from jinja2 import StrictUndefined, nodes
from jinja2.sandbox import ImmutableSandboxedEnvironment

from proficentr.docx_context import DocxError, SCHEMA, fictional_context, validate_context
from proficentr.settings import _require_local

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UNPACKED_BYTES = 32 * 1024 * 1024
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_RENDER_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 1000
REL_NS = NS.OPC_RELATIONSHIPS
CONTENT_NS = NS.OPC_CONTENT_TYPES
WORD_NS = NS.WML_MAIN
# Namespace identifiers come from python-docx; none of these relationships are fetched.
REL_TYPES = {getattr(RT, name) for name in (
    'OFFICE_DOCUMENT', 'CORE_PROPERTIES', 'EXTENDED_PROPERTIES', 'CUSTOM_PROPERTIES',
    'STYLES', 'SETTINGS', 'WEB_SETTINGS', 'FONT_TABLE', 'NUMBERING', 'COMMENTS',
    'FOOTNOTES', 'ENDNOTES', 'HEADER', 'FOOTER', 'THEME', 'IMAGE', 'THUMBNAIL',
    'CUSTOM_XML', 'CUSTOM_XML_PROPS',
)}
REL_TYPES.add(RT.STYLES.replace('schemas.openxmlformats.org/officeDocument/2006',
                               'schemas.microsoft.com/office/2007') + 'WithEffects')
CONTENT_TYPES = {
    'application/vnd.openxmlformats-package.relationships+xml',
    'application/vnd.openxmlformats-package.core-properties+xml',
    'application/vnd.openxmlformats-officedocument.extended-properties+xml',
    'application/vnd.openxmlformats-officedocument.custom-properties+xml',
    'application/vnd.openxmlformats-officedocument.customXmlProperties+xml',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml',
    'application/vnd.ms-word.stylesWithEffects+xml',
    *{'application/vnd.openxmlformats-officedocument.wordprocessingml.' + part + '+xml'
      for part in ('settings', 'webSettings', 'fontTable', 'numbering', 'comments', 'footnotes', 'endnotes', 'header', 'footer')},
    'application/vnd.openxmlformats-officedocument.theme+xml',
    'application/xml', 'image/png', 'image/jpeg', 'image/gif', 'image/bmp', 'image/tiff',
}
PART_PATTERN = re.compile(
    r'(?:\[Content_Types\]\.xml|_rels/\.rels|docProps/(?:core|app|custom)\.xml|'
    r'customXml/(?:item[0-9]+\.xml|itemProps[0-9]+\.xml|_rels/item[0-9]+\.xml\.rels)|'
    r'docProps/thumbnail\.(?:jpeg|jpg|png)|word/(?:document|styles|stylesWithEffects|settings|webSettings|fontTable|numbering|comments|footnotes|endnotes|header[0-9]+|footer[0-9]+)\.xml|'
    r'word/_rels/(?:document|styles|stylesWithEffects|settings|webSettings|fontTable|numbering|comments|footnotes|endnotes|header[0-9]+|footer[0-9]+)\.xml\.rels|'
    r'word/theme/theme[0-9]+\.xml|word/media/[A-Za-z0-9_-]+\.(?:png|jpg|jpeg|gif|bmp|tif|tiff))\Z'
)
RENDER_PART = re.compile(r'word/(?:document|header[0-9]+|footer[0-9]+|footnotes)\.xml\Z')
DANGEROUS_ELEMENTS = {'altChunk', 'object', 'OLEObject', 'control', 'ocx', 'subDoc', 'attachedTemplate',
                      'externalData', 'dataBinding', 'mailMerge', 'movie', 'hlinkClick', 'hlinkMouseOver'}
SAFE_WORD_FIELD = re.compile(r'\s*(?:PAGE|NUMPAGES|SECTIONPAGES)(?:\s+\\\*\s+MERGEFORMAT)?\s*', re.I)


def _xml(data):
    try:
        root = SafeXML.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        if sum(1 for _ in root.iter()) > 100_000:
            raise DocxError("Слишком сложная XML-структура DOCX.")
        return root
    except DocxError:
        raise
    except Exception:
        raise DocxError("DOCX содержит некорректный XML или запрещённые DTD/сущности.") from None


def validate_package(data):
    """Закрытый набор частей DOCX, без распаковки на диск и обращения к связям."""
    if len(data) > MAX_FILE_BYTES:
        raise DocxError("Размер DOCX превышает 10 МиБ.")
    try:
        with ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ENTRIES or sum(item.file_size for item in infos) > MAX_UNPACKED_BYTES:
                raise DocxError("DOCX превышает ограничения размера или числа частей архива.")
            names = {item.filename for item in infos}
            if len(names) != len(infos) or not {'[Content_Types].xml', '_rels/.rels', 'word/document.xml'} <= names:
                raise DocxError("Нарушена структура DOCX: отсутствуют обязательные части или есть дубли.")
            parts = {}
            roots = {}
            for item in infos:
                mode = item.external_attr >> 16
                if (not PART_PATTERN.fullmatch(item.filename) or item.flag_bits & 1
                        or item.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
                        or stat.S_ISLNK(mode) or item.file_size > MAX_PART_BYTES
                        or item.file_size > max(1, item.compress_size) * 200):
                    raise DocxError("DOCX содержит недопустимую часть, вложение или чрезмерное сжатие.")
                part = archive.read(item)
                parts[item.filename] = part
                if item.filename.endswith(('.xml', '.rels')):
                    root = _xml(part)
                    roots[item.filename] = root
                    for element in root.iter():
                        local = element.tag.rsplit('}', 1)[-1]
                        if local in DANGEROUS_ELEMENTS:
                            raise DocxError("DOCX содержит опасные вложения, привязки или внешние объекты.")
                        for attribute, value in element.attrib.items():
                            if (attribute.rsplit('}', 1)[-1] in {'src', 'href', 'link', 'source', 'codebase', 'action', 'url', 'base', 'docLocation', 'schemaLocation'}
                                    or re.search(r'url\s*\(|@import', value, re.I)):
                                raise DocxError("Прямые внешние ресурсы и активные ссылки в XML DOCX запрещены.")
                        if local == 'instrText' and not SAFE_WORD_FIELD.fullmatch(element.text or ''):
                            raise DocxError("Поля Word разрешены только для нумерации страниц; DDE и другие инструкции запрещены.")
                        if local == 'fldSimple' and not SAFE_WORD_FIELD.fullmatch(element.get('{' + WORD_NS + '}instr', '')):
                            raise DocxError("Недопустимая инструкция поля Word.")
            types = roots['[Content_Types].xml']
            if types.tag != '{' + CONTENT_NS + '}Types':
                raise DocxError("Некорректный реестр типов DOCX.")
            defaults, overrides = {}, {}
            for element in types:
                kind = element.get('ContentType')
                if kind not in CONTENT_TYPES:
                    raise DocxError("DOCX содержит запрещённый тип содержимого (макросы или вложения).")
                if element.tag == '{' + CONTENT_NS + '}Default':
                    defaults[element.get('Extension')] = kind
                elif element.tag == '{' + CONTENT_NS + '}Override':
                    path = element.get('PartName', '')
                    if not path.startswith('/') or path[1:] not in names:
                        raise DocxError("Некорректная ссылка в типах содержимого DOCX.")
                    overrides[path[1:]] = kind
                else:
                    raise DocxError("Некорректный реестр типов DOCX.")
            main_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
            if overrides.get('word/document.xml') != main_type or roots['word/document.xml'].tag != '{' + WORD_NS + '}document':
                raise DocxError("Файл не является обычным документом DOCX.")
            for name in names - {'[Content_Types].xml'}:
                extension = 'rels' if name.endswith('.rels') else PurePosixPath(name).suffix[1:]
                if name not in overrides and extension not in defaults:
                    raise DocxError("В DOCX есть части без типа содержимого.")
            office_documents = []
            for name, root in roots.items():
                if not name.endswith('.rels'):
                    continue
                if root.tag != '{' + REL_NS + '}Relationships':
                    raise DocxError("Некорректные связи DOCX.")
                ids = set()
                parent = '' if name == '_rels/.rels' else str(PurePosixPath(name).parent.parent)
                for relation in root:
                    kind = relation.get('Type', '')
                    target = relation.get('Target', '')
                    if (relation.tag != '{' + REL_NS + '}Relationship' or kind not in REL_TYPES
                            or relation.get('TargetMode', 'Internal') != 'Internal'
                            or not relation.get('Id') or relation.get('Id') in ids
                            or re.search(r'[:\\%?#\x00-\x20]', target) or not target):
                        raise DocxError("Внешние ссылки и неподдерживаемые связи в DOCX запрещены.")
                    ids.add(relation.get('Id'))
                    resolved = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join(parent, target))
                    if resolved not in names:
                        raise DocxError("Связь DOCX указывает на отсутствующую часть.")
                    if name == '_rels/.rels' and kind == RT.OFFICE_DOCUMENT:
                        office_documents.append(resolved)
            if office_documents != ['word/document.xml']:
                raise DocxError("Не найдена однозначная связь с основным документом DOCX.")
            return parts
    except DocxError:
        raise
    except (BadZipFile, OSError, ValueError, RuntimeError, NotImplementedError):
        raise DocxError("Не удалось прочитать DOCX: архив повреждён или формат не поддерживается.") from None


class RestrictedEnvironment(ImmutableSandboxedEnvironment):
    """Разрешения задаёт AST; sandbox дополнительно исключает Python-атрибуты."""

    def __init__(self, *, context=None):
        super().__init__(undefined=StrictUndefined, autoescape=True)
        self.globals.clear()
        self.filters.clear()
        self.tests.clear()
        self.render_budget = MAX_RENDER_BYTES
        self.max_value_size = 10_000 if context is None else max(
            [64] + [len(str(value).encode('utf-8')) for value in _leaves(context)])
        self.list_size = 500 if context is None else len(context.get('students', []))

    def is_safe_attribute(self, obj, attr, value):
        return False

    def is_safe_callable(self, obj):
        return False

    def getattr(self, obj, attribute):
        return self.getitem(obj, attribute)

    def getitem(self, obj, argument):
        if type(obj) is dict and type(argument) is str and argument in obj:
            return obj[argument]
        return self.undefined(name=str(argument))

    def _parse(self, source, name, filename):
        tree = super()._parse(source, name, filename)
        if sum(1 for _ in tree.find_all(nodes.Node)) > 5000:
            raise DocxError("Шаблон слишком сложный: не более 5000 элементов синтаксиса.")
        cost = self._check(tree, dict(SCHEMA), depth=0, in_loop=False)
        self.render_budget -= cost
        if self.render_budget < 0:
            raise DocxError("Превышен допустимый объём результата шаблона (16 МиБ XML).")
        return tree

    def _field(self, node, scope):
        if type(node) is nodes.Name and node.name in scope:
            return scope[node.name]
        if type(node) in (nodes.Getattr, nodes.Getitem):
            parent = self._field(node.node, scope)
            key = node.attr if type(node) is nodes.Getattr else (node.arg.value if type(node.arg) is nodes.Const else None)
            if type(parent) is dict and type(key) is str and key in parent:
                return parent[key]
        raise DocxError("Неизвестное поле или недопустимое выражение шаблона. Используйте инструкцию по полям.")

    def _condition(self, node, scope):
        if type(node) in (nodes.And, nodes.Or):
            self._condition(node.left, scope)
            self._condition(node.right, scope)
        elif type(node) is nodes.Not:
            self._condition(node.node, scope)
        elif type(node) is nodes.Compare:
            if self._field(node.expr, scope) is not None or len(node.ops) != 1:
                raise DocxError("В условии сравнивайте простое поле с постоянным значением.")
            operand = node.ops[0]
            if operand.op not in ('eq', 'ne') or type(operand.expr) is not nodes.Const or type(operand.expr.value) not in (str, int, bool, type(None)):
                raise DocxError("Разрешены только сравнения == и != с простым значением.")
        elif self._field(node, scope) is not None:
            raise DocxError("В условии укажите простое поле.")

    def _check(self, node, scope, *, depth, in_loop):
        if depth > 20:
            raise DocxError("Превышена глубина условий шаблона.")
        kind = type(node)
        if kind is nodes.Template:
            return sum(self._check(item, scope, depth=depth + 1, in_loop=in_loop) for item in node.body)
        if kind is nodes.Output:
            total = 0
            for item in node.nodes:
                if type(item) is nodes.TemplateData:
                    total += len(item.data.encode('utf-8'))
                elif self._field(item, scope) is None:
                    total += self.max_value_size * 6 + 128
                else:
                    raise DocxError("Для подстановки укажите поле, а не весь словарь или список.")
            return total
        if kind is nodes.If:
            self._condition(node.test, scope)
            return sum(self._check(item, scope, depth=depth + 1, in_loop=in_loop)
                       for item in node.body + node.elif_ + node.else_)
        if kind is nodes.For:
            if (in_loop or type(node.iter) is not nodes.Name or node.iter.name != 'students'
                    or type(node.target) is not nodes.Name or node.target.name.startswith('_')
                    or node.target.name in SCHEMA or node.target.name == 'loop'
                    or node.recursive or node.test is not None or node.else_):
                raise DocxError("Разрешён один уровень цикла «for row in students» без фильтров и else.")
            inner = dict(scope, **{node.target.name: SCHEMA['students'][0]})
            return self.list_size * sum(self._check(item, inner, depth=depth + 1, in_loop=True) for item in node.body)
        raise DocxError("Разрешены только подстановка полей, if/else и цикл по students.")


def _leaves(value):
    if type(value) is dict:
        for item in value.values():
            yield from _leaves(item)
    elif type(value) is list:
        for item in value:
            yield from _leaves(item)
    else:
        yield value


def _require_docx_local(path):
    try:
        _require_local(path)
    except ValueError:
        raise DocxError("Выберите локальный файл вне сетевых дисков и папок облачной синхронизации.") from None


def read_docx(path):
    path = Path(path)
    _require_docx_local(path)
    if path.suffix.lower() != '.docx':
        raise DocxError("Выберите файл с расширением .docx.")
    try:
        with path.open('rb') as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except OSError:
        raise DocxError("Не удалось прочитать файл DOCX.") from None
    if len(data) > MAX_FILE_BYTES:
        raise DocxError("Размер DOCX превышает 10 МиБ.")
    return data


def render_docx(data, context):
    """Все данные, синтаксис и части проверяются до выполнения шаблонизации."""
    context = validate_context(context)
    parts = validate_package(data)
    try:
        template = DocxTemplate(BytesIO(data))
        environment = RestrictedEnvironment(context=context)
        # Check even unused/unrendered parts: unsupported placeholders must not survive silently.
        checker = RestrictedEnvironment(context=context)
        for name, xml in parts.items():
            if not name.endswith('.xml'):
                continue
            if RENDER_PART.fullmatch(name):
                visible = ''.join(_xml(xml).itertext())
                if re.search(r'{{\s*[rp]\s', visible):
                    raise DocxError("RichText и вставка поддокументов в этом синтаксисе запрещены.")
                checker.parse(template.patch_xml(xml.decode('utf-8')))
            elif name == 'docProps/core.xml':
                # docxtpl renders core properties individually; XML text is decoded first.
                for element in _xml(xml):
                    text = element.text or ''
                    if any(token in text for token in ('{{', '{%', '{#')) and element.tag.rsplit('}', 1)[-1] not in {
                        'creator', 'description', 'identifier', 'language', 'lastModifiedBy', 'subject', 'title',
                    }:
                        raise DocxError("Это свойство DOCX не поддерживает поля шаблона.")
                    checker.parse(text)
            elif b'{{' in xml or b'{%' in xml or b'{#' in xml:
                raise DocxError("Поля разрешены в основном тексте, таблицах, колонтитулах, сносках и основных свойствах DOCX.")
        template.render(context, jinja_env=environment, autoescape=True)
        output = BytesIO()
        template.save(output)
        result = output.getvalue()
        validate_package(result)
        return result
    except DocxError:
        raise
    except Exception:
        raise DocxError("Тестовая генерация не удалась: проверьте синтаксис, обязательные данные и расположение тегов в Word.") from None


def validate_template(data):
    render_docx(data, fictional_context())


def publish_docx(data, destination):
    """Атомарная публикация нового файла; существующий путь не заменяется."""
    destination = Path(destination)
    _require_docx_local(destination)
    if not destination.is_absolute() or destination.suffix.lower() != '.docx':
        raise DocxError("Укажите абсолютный локальный путь нового файла .docx.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.docx-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if sys.platform == 'win32':
            # Windows rename fails if destination exists (including dangling links).
            os.rename(temporary, destination)
        else:
            os.link(temporary, destination)
    except FileExistsError:
        raise DocxError("Файл назначения уже существует; выберите новый путь.") from None
    except OSError:
        raise DocxError("Не удалось сохранить DOCX. Существующие файлы не изменены.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def generate_document(version, data, destination):
    """Независимый сервис: версия (path, sha256), простые данные и путь результата."""
    source = read_docx(version.path)
    if hashlib.sha256(source).hexdigest() != version.sha256:
        raise DocxError("SHA-256 шаблона не совпадает с реестром. Используйте сохранённую исходную версию.")
    return publish_docx(render_docx(source, data), destination)
