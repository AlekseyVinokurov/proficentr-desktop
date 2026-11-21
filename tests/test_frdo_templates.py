from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import pytest

from proficentr.database import Database, MIGRATIONS
import proficentr.database as database_module
from proficentr.frdo_templates import FrdoError, FrdoTemplateRepository, inspect_template
from proficentr.services import prepare_storage, open_database
from frdo_fixtures import synthetic_template


@pytest.fixture
def context(tmp_path):
    storage = prepare_storage(tmp_path / 'storage')
    with open_database(storage) as db:
        yield FrdoTemplateRepository(db.connection, storage), tmp_path


def write_source(folder, kind, data=None):
    source = folder / f'{kind}.xlsx'
    source.write_bytes(data if data is not None else synthetic_template(kind))
    return source


def rewrite(data, name, transform):
    output = BytesIO()
    with ZipFile(BytesIO(data)) as source, ZipFile(output, 'w', ZIP_DEFLATED) as target:
        for entry in source.infolist():
            content = source.read(entry)
            target.writestr(entry, transform(content) if entry.filename == name else content)
    return output.getvalue()


@pytest.mark.parametrize('kind', ['dpo', 'po'])
def test_copy_preserves_every_ooxml_part_hash_and_restart(context, kind):
    repo, folder = context
    source = write_source(folder, kind)
    original = source.read_bytes()
    structure = inspect_template(original, kind)
    saved = repo.register(source, kind)
    assert source.read_bytes() == repo.source(saved) == original
    assert saved.sha256 == sha256(original).hexdigest()
    assert saved.version == 1 and saved.original_name == source.name and saved.added_at
    copied = repo.structure(kind)
    assert copied.ooxml == structure.ooxml
    for required in ('xl/workbook.xml', 'xl/styles.xml', 'xl/worksheets/sheet1.xml', 'xl/worksheets/sheet2.xml', 'xl/tables/table1.xml'):
        assert copied.ooxml[required] == structure.ooxml[required]
    assert copied.dictionaries and '000' in copied.dictionaries['AE' if kind == 'dpo' else 'W']
    with open_database(repo.storage) as restarted:
        other = FrdoTemplateRepository(restarted.connection, repo.storage)
        assert other.active(kind) == saved
        assert other.structure(kind).ooxml == structure.ooxml


def test_independent_registration_and_replacement(context):
    repo, folder = context
    first = repo.register(write_source(folder, 'dpo'), 'dpo')
    po = repo.register(write_source(folder, 'po'), 'po')
    second = repo.register(folder / 'dpo.xlsx', 'dpo')
    assert second.version == 2 and repo.active('po') == po
    assert len(repo.versions('dpo')) == 2
    assert repo.source(first) == repo.source(second)
    assert repo.versions('dpo')[1].active == 0


@pytest.mark.parametrize('failure', ['invalid', 'wrong_kind', 'write', 'sql'])
def test_failed_replacement_preserves_previous_file_selection_and_cleans_copy(context, monkeypatch, failure):
    repo, folder = context
    source = write_source(folder, 'dpo')
    old = repo.register(source, 'dpo')
    data = repo.source(old)
    if failure == 'invalid':
        source.write_bytes(b'not xlsx')
    elif failure == 'wrong_kind':
        source.write_bytes(synthetic_template('po'))
    elif failure == 'write':
        monkeypatch.setattr('proficentr.frdo_templates.os.fsync', lambda *a: (_ for _ in ()).throw(OSError('disk')))
    else:
        repo.connection.execute("CREATE TRIGGER fail_frdo BEFORE INSERT ON frdo_templates BEGIN SELECT RAISE(ABORT,'test'); END")
        repo.connection.commit()
    with pytest.raises(FrdoError):
        repo.register(source, 'dpo')
    assert repo.active('dpo') == old and repo.source(old) == data
    assert len(list((repo.storage.root / 'templates').glob('*.xlsx'))) == 1


@pytest.mark.parametrize('kind', ['dpo', 'po'])
@pytest.mark.parametrize('part,before,after', [
    ('xl/workbook.xml', b'state="hidden"', b'state="visible"'),
    ('xl/workbook.xml', 'Проверки!'.encode(), 'Несуществующий!'.encode()),
    ('xl/worksheets/sheet1.xml', b'<dataValidations', b'<removedValidations'),
    ('xl/tables/table1.xml', 'Таблица1'.encode(), 'Таблица2'.encode()),
])
def test_ooxml_damage_is_rejected(kind, part, before, after):
    from xml.sax.saxutils import escape
    # openpyxl кодирует кириллицу в атрибутах XML как numeric entities.
    data = synthetic_template(kind)
    def damage(content):
        text = content.decode()
        if before.decode() in text:
            return text.replace(before.decode(), after.decode()).encode()
        encoded = lambda b: ''.join(f'&#{ord(c)};' if ord(c) > 127 else escape(c) for c in b.decode())
        assert encoded(before) in text
        return text.replace(encoded(before), encoded(after)).encode()
    with pytest.raises(FrdoError):
        inspect_template(rewrite(data, part, damage), kind)


def test_tampered_archive_is_reported(context):
    repo, folder = context
    saved = repo.register(write_source(folder, 'po'), 'po')
    (repo.storage.root / saved.relative_path).write_bytes(b'changed')
    with pytest.raises(FrdoError, match='SHA-256'):
        repo.structure('po')


def test_migration_preserves_previous_schema_and_records(tmp_path, monkeypatch):
    path = tmp_path / 'old.sqlite3'
    with monkeypatch.context() as patch:
        patch.setattr(database_module, 'MIGRATIONS', MIGRATIONS[:-1])
        with Database(path) as db:
            db.connection.execute("INSERT INTO training_center(id,full_name) VALUES(1,'Тестовый центр')")
            db.connection.commit()
    with Database(path) as db:
        assert db.connection.execute('SELECT full_name FROM training_center').fetchone() == ('Тестовый центр',)
        assert db.connection.execute('SELECT count(*) FROM frdo_templates').fetchone() == (0,)
    assert list((tmp_path / 'migration-backups').glob('*.sqlite3'))


@pytest.mark.parametrize('kind,expected_hash', [
    ('dpo', '3824efc21a11a3d57ea946684d4cd97d55f7b3c10b7d66dae7041248f3135fcd'),
    ('po', '13f0c83e1c568fb0f728c5ebe51351e902e8a93e84b97aaaa1e4767b19a86407'),
])
def test_local_reference_compatibility_and_exact_dictionaries(context, kind, expected_hash):
    source = Path(__file__).resolve().parents[1] / 'references' / 'frdo' / f'{kind}_template.xlsx'
    if not source.exists():
        pytest.skip('Пользовательский эталон доступен только локально; в Git не публикуется')
    repo, folder = context
    original = source.read_bytes()
    assert sha256(original).hexdigest() == expected_hash
    saved = repo.register(source, kind)
    structure = repo.structure(kind)
    assert repo.source(saved) == source.read_bytes() == original
    assert structure.ooxml == inspect_template(original, kind).ooxml
    assert structure.dictionaries['Z' if kind == 'dpo' else 'U'] == ('Муж', 'Жен')
    assert '000' in structure.dictionaries['AE' if kind == 'dpo' else 'W']
    assert 'Оригинал' in structure.dictionaries['B']
    if kind == 'dpo':
        assert 'Образование и наука' in structure.dictionaries['L']  # structured table reference


def test_xml_entity_is_reported_as_validation_error():
    data = rewrite(synthetic_template('dpo'), 'xl/workbook.xml',
                   lambda _: b'<!DOCTYPE a [<!ENTITY test "unsafe">]><a>&test;</a>')
    with pytest.raises(FrdoError):
        inspect_template(data, 'dpo')


def test_relocation_preserves_template_versions_and_files(context, monkeypatch):
    from proficentr.storage import relocate_storage
    repo, folder = context
    saved = repo.register(write_source(folder, 'dpo'), 'dpo')
    original = repo.source(saved)
    target = relocate_storage(repo.storage, repo.connection, folder / 'relocated')
    with open_database(target) as db:
        moved = FrdoTemplateRepository(db.connection, target)
        assert moved.active('dpo') == saved
        assert moved.source(saved) == original
        assert moved.structure('dpo').dictionaries
