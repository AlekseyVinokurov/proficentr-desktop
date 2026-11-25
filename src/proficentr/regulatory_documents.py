"""Нормативные документы организации: неизменяемые редакции и журнал статусов."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date
from hashlib import sha256
import os
from pathlib import Path
import re
from uuid import uuid4

from proficentr.document_delivery import read_evidence
from proficentr.docx_engine import read_docx, validate_package
from proficentr.settings import _require_local
from proficentr.students import iso_date

CATEGORIES = {
    'charter': 'Устав', 'license': 'Образовательная лицензия',
    'local': 'Локальные нормативные акты', 'pedagogical': 'Протоколы педагогического совета',
    'staff': 'Протоколы общего собрания работников', 'founder': 'Решения учредителя',
    'orders': 'Приказы по организационной деятельности', 'privacy': 'Документы по персональным данным',
    'other': 'Прочие документы',
}
STATUSES = {'draft': 'Проект', 'active': 'Действующий', 'archived': 'Архивный'}


class RegulatoryError(ValueError):
    pass


@dataclass(frozen=True)
class RegulatoryVersion:
    id: int | None = None
    document_id: int | None = None
    revision: int = 0
    title: str = ''
    category: str = 'other'
    number: str = ''
    document_date: str = ''
    effective_date: str = ''
    approval_basis: str = ''
    edition: str = ''
    note: str = ''
    pdf_path: str = ''
    pdf_sha256: str = ''
    docx_path: str = ''
    docx_sha256: str = ''
    created_at: str = ''
    status: str = 'draft'


FIELDS = tuple(RegulatoryVersion.__dataclass_fields__)[:-1]
TEXT_FIELDS = ('title', 'number', 'document_date', 'effective_date', 'approval_basis', 'edition', 'note')


class RegulatoryRepository:
    def __init__(self, connection, storage, *, access_check=None):
        self.connection = connection
        self.storage = storage
        # P06 injects the application-wide permission check. No user/role store is created here.
        self.access_check = access_check
        connection.create_function('regulatory_fold', 1, str.casefold, deterministic=True)

    def require_access(self, permission):
        if self.access_check is not None:
            self.access_check('regulatory.' + permission)

    def can_manage(self):
        try:
            self.require_access('write')
            return True
        except PermissionError:
            return False

    @contextmanager
    def transaction(self):
        if self.connection.in_transaction:
            raise RegulatoryError('Завершите текущую операцию с базой.')
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            yield

    def load(self, version_id):
        self.require_access('read')
        cursor = self.connection.execute('SELECT * FROM regulatory_versions WHERE id=?', (version_id,))
        row = cursor.fetchone()
        if row is None:
            raise RegulatoryError('Редакция документа не найдена.')
        values = dict(zip((d[0] for d in cursor.description), row))
        values['status'] = self.connection.execute(
            'SELECT status FROM regulatory_status_events WHERE version_id=? ORDER BY id DESC LIMIT 1', (version_id,)).fetchone()[0]
        return RegulatoryVersion(**values)

    def history(self, document_id):
        self.require_access('read')
        return [self.load(row[0]) for row in self.connection.execute(
            'SELECT id FROM regulatory_versions WHERE document_id=? ORDER BY revision DESC', (document_id,))]

    def events(self, version_id):
        self.require_access('read')
        return self.connection.execute('SELECT status,created_at,note FROM regulatory_status_events WHERE version_id=? ORDER BY id', (version_id,)).fetchall()

    def links(self, version_id):
        self.require_access('read')
        return [self.load(row[0]) for row in self.connection.execute(
            'SELECT target_version_id FROM regulatory_links WHERE version_id=? ORDER BY target_version_id', (version_id,))]

    def search(self, query='', category=None, status=None):
        self.require_access('read')
        # A status filter can show an active edition even when a newer draft exists.
        rows = self.connection.execute('''SELECT v.id FROM regulatory_versions v
            JOIN regulatory_status_events e ON e.id=(SELECT max(id) FROM regulatory_status_events WHERE version_id=v.id)
            WHERE (? IS NULL OR v.category=?) AND (? IS NULL OR e.status=?)
            AND instr(regulatory_fold(v.title || ' ' || v.number || ' ' || v.approval_basis || ' ' || v.note),?)>0
            ORDER BY v.document_id,v.revision DESC''', (category, category, status, status, query.strip().casefold())).fetchall()
        result = []; seen = set()
        for row in rows:
            version = self.load(row[0])
            if version.document_id not in seen:
                result.append(version); seen.add(version.document_id)
        return result

    def _validate(self, version, status, has_pdf, links):
        if version.category not in CATEGORIES or status not in STATUSES:
            raise RegulatoryError('Выберите категорию и статус документа.')
        if any(not isinstance(getattr(version, k), str) or len(getattr(version, k)) > 10000 for k in TEXT_FIELDS):
            raise RegulatoryError('Реквизиты документа должны быть текстом длиной до 10000 символов.')
        version = replace(version, **{k: getattr(version, k).strip() for k in TEXT_FIELDS})
        if not version.title:
            raise RegulatoryError('Укажите наименование документа.')
        for field in ('document_date', 'effective_date'):
            if getattr(version, field): iso_date(getattr(version, field), 'Дата документа / вступления в силу')
        if status == 'active' and not has_pdf:
            raise RegulatoryError('Для действующего документа нужен основной файл PDF.')
        if status == 'active':
            if not version.document_date:
                raise RegulatoryError('Для действующего документа укажите дату документа.')
            if version.category == 'license' and not version.number:
                raise RegulatoryError('Для действующей образовательной лицензии укажите номер.')
            if version.category in ('charter', 'local', 'privacy'):
                if not version.effective_date or not (version.approval_basis or links):
                    raise RegulatoryError('Для действующего устава или локального акта укажите дату вступления в силу и основание утверждения (текст или связанный документ).')
        return version

    def _path(self, relative):
        if not re.fullmatch(r'organization/regulatory/[a-f0-9]{32}\.(pdf|docx)', relative):
            raise RegulatoryError('Недопустимый путь файла реестра.')
        path = self.storage.root / relative
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.resolve().is_relative_to(self.storage.root):
            raise RegulatoryError('Символические ссылки и выход за пределы хранилища запрещены.')
        _require_local(path)
        return path

    def _import_bytes(self, source, kind):
        path = Path(source).absolute(); _require_local(path)
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file() or path.suffix.lower() != '.' + kind:
            raise RegulatoryError('Выберите обычный локальный файл PDF или DOCX соответствующего типа.')
        if kind == 'pdf': return read_evidence(path)[0]
        data = read_docx(path); validate_package(data)
        return data

    def _write(self, data, kind, owned):
        path = self._path('organization/regulatory/' + uuid4().hex + '.' + kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            owned.append(path)
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        if sha256(path.read_bytes()).digest() != sha256(data).digest():
            raise RegulatoryError('Не удалось проверить сохранённые байты документа.')
        return path.relative_to(self.storage.root).as_posix(), sha256(data).hexdigest()

    def add(self, version, *, pdf=None, docx=None, previous_id=None, links=()):
        self.require_access('write')
        self.require_access('read')
        if version.id is not None:
            raise RegulatoryError('Сохранённая редакция неизменяема. Добавьте новую редакцию.')
        prepared = {k: self._import_bytes(s, k) for k, s in (('pdf', pdf), ('docx', docx)) if s is not None}
        version = self._validate(version, version.status, 'pdf' in prepared, links)
        owned = []
        try:
            self.storage.require_available()
            with self.transaction():
                if previous_id is not None:
                    old = self.load(previous_id); history = self.history(old.document_id)
                    if history[0].id != previous_id:
                        raise RegulatoryError('Появилась новая редакция. Обновите карточку.')
                    document_id = old.document_id; revision = old.revision + 1
                else:
                    document_id = self.connection.execute('INSERT INTO regulatory_documents DEFAULT VALUES').lastrowid
                    revision = 1
                targets = [self.load(key) for key in set(links)]
                if any(t.document_id == document_id for t in targets):
                    raise RegulatoryError('Связь с самим документом не допускается.')
                files = {}
                for kind, data in prepared.items():
                    relative, digest = self._write(data, kind, owned)
                    files[kind + '_path'] = relative; files[kind + '_sha256'] = digest
                version = replace(version, document_id=document_id, revision=revision,
                    edition=version.edition or str(revision), pdf_path='', pdf_sha256='', docx_path='', docx_sha256='')
                values = asdict(version) | files
                fields = FIELDS[1:-1]
                key = self.connection.execute(
                    f"INSERT INTO regulatory_versions ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",  # nosec B608 # Fixed dataclass fields; values are bound.
                    tuple(values[k] for k in fields)).lastrowid
                self.connection.executemany('INSERT INTO regulatory_links(version_id,target_version_id) VALUES (?,?)', [(key, t.id) for t in targets])
                self._status(key, version.status, 'Импорт новой редакции')
        except BaseException:
            cleanup_failed = False
            for path in reversed(owned):
                try: path.unlink(missing_ok=True)
                except OSError: cleanup_failed = True
            if cleanup_failed:
                raise RegulatoryError('Импорт отменён. Часть незарегистрированных файлов не удалось убрать; при следующем открытии реестра они будут перенесены в карантин. Проверьте доступ к хранилищу.')
            raise
        return self.load(key)

    def _status(self, version_id, status, note):
        version = self.connection.execute('SELECT document_id FROM regulatory_versions WHERE id=?', (version_id,)).fetchone()
        if status == 'active':
            active_ids = self.connection.execute('''SELECT v.id FROM regulatory_versions v
                JOIN regulatory_status_events e ON e.id=(SELECT max(id) FROM regulatory_status_events WHERE version_id=v.id)
                WHERE v.document_id=? AND v.id<>? AND e.status='active' ''', (version[0], version_id)).fetchall()
            for old_id, in active_ids:
                self.connection.execute("INSERT INTO regulatory_status_events(version_id,status,note) VALUES (?,'archived',?)",
                                        (old_id, 'Заменён действующей редакцией'))
        self.connection.execute('INSERT INTO regulatory_status_events(version_id,status,note) VALUES (?,?,?)', (version_id, status, note))

    def set_status(self, version_id, status, *, expected_status, note):
        self.require_access('write')
        if not isinstance(note, str) or not note.strip() or len(note) > 10000:
            raise RegulatoryError('Укажите основание изменения статуса.')
        with self.transaction():
            version = self.load(version_id)
            if version.status != expected_status:
                raise RegulatoryError('Статус изменился. Обновите карточку.')
            if version.status == 'archived' or status not in ('active', 'archived') or status == version.status:
                raise RegulatoryError('Архивная редакция не изменяется; добавьте новую редакцию.')
            self._validate(version, status, bool(version.pdf_path), self.links(version_id))
            if status == 'active':
                if self.history(version.document_id)[0].id != version_id:
                    raise RegulatoryError('Ввести в действие можно только последнюю редакцию документа.')
                self.read_file(version_id)
            self._status(version_id, status, note.strip())
        return self.load(version_id)

    def read_file(self, version_id, kind='pdf'):
        self.require_access('read')
        if kind not in ('pdf', 'docx'): raise RegulatoryError('Неизвестный тип файла.')
        version = self.load(version_id); relative = getattr(version, kind + '_path')
        if not relative: raise RegulatoryError('Файл ' + kind.upper() + ' не загружен для этой редакции.')
        self.storage.require_available(); path = self._path(relative)
        if not path.is_file():
            raise RegulatoryError('Архивный файл недоступен. Восстановите исходные байты из резервной копии.')
        data = self._import_bytes(path, kind)
        if sha256(data).hexdigest() != getattr(version, kind + '_sha256'):
            raise RegulatoryError('SHA-256 файла не совпадает с реестром. Восстановите исходные байты из резервной копии.')
        return data

    def copy(self, version_id, destination, kind='pdf'):
        data = self.read_file(version_id, kind)
        path = Path(destination).absolute(); _require_local(path)
        if path.resolve().is_relative_to(self.storage.root) and path.parent.resolve() != (self.storage.root / 'exports' / 'regulatory').resolve():
            raise RegulatoryError('Копию нельзя сохранять поверх управляемого архива.')
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise RegulatoryError('Копия не должна использовать символические ссылки.')
        owned = False
        try:
            with path.open('xb') as stream:
                owned = True; stream.write(data); stream.flush(); os.fsync(stream.fileno())
            if sha256(path.read_bytes()).digest() != sha256(data).digest():
                raise RegulatoryError('Не удалось проверить байты сохранённой копии.')
        except BaseException:
            if owned: path.unlink(missing_ok=True)
            raise
        return path

    def editable_copy(self, version_id):
        self.require_access('read'); self.storage.require_available()
        folder = self.storage.root / 'exports' / 'regulatory'
        if any(p.is_symlink() for p in (folder, *folder.parents)):
            raise RegulatoryError('Недопустимый каталог копий.')
        folder.mkdir(parents=True, exist_ok=True)
        return self.copy(version_id, folder / (uuid4().hex + '.docx'), 'docx')

    def recover_unregistered(self):
        """Под блокировкой убирает аварийные файлы только в отдельный карантин, не удаляя их."""
        self.require_access('write'); self.storage.require_available()
        count = 0
        with self.transaction():
            referenced = {p for row in self.connection.execute('SELECT pdf_path,docx_path FROM regulatory_versions') for p in row if p}
            folder = self.storage.root / 'organization' / 'regulatory'
            self._path('organization/regulatory/' + '0' * 32 + '.pdf')
            for path in folder.glob('*'):
                relative = path.relative_to(self.storage.root).as_posix()
                if not re.fullmatch(r'organization/regulatory/[a-f0-9]{32}\.(pdf|docx)', relative): continue
                self._path(relative)
                if relative not in referenced:
                    target = self.storage.root / 'exports' / 'regulatory-recovery' / uuid4().hex
                    if any(p.is_symlink() for p in (target, *target.parents)):
                        raise RegulatoryError('Недопустимый каталог восстановления.')
                    target.mkdir(parents=True); path.rename(target / path.name); count += 1
        return count
