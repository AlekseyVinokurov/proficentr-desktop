from dataclasses import replace
from io import BytesIO
from pathlib import Path
import hashlib
import sqlite3
from types import SimpleNamespace
from zipfile import ZipFile, ZIP_DEFLATED

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
import pytest

import proficentr.database as database_module
import proficentr.docx_engine as engine
from proficentr.database import Database
from proficentr.docx_context import DocxError, fictional_context, validate_context
from proficentr.docx_engine import generate_document, render_docx, validate_package, validate_template
from proficentr.document_templates import TemplateRepository, DOCUMENT_TYPES
from proficentr.services import prepare_storage, open_database
from proficentr.storage import relocate_storage


def template_bytes(text='{{ student.full_name }}'):
    document = Document()
    document.add_paragraph(text)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def modify_package(data, replacements=None, additions=None):
    result = BytesIO()
    with ZipFile(BytesIO(data)) as source, ZipFile(result, 'w', ZIP_DEFLATED) as output:
        for name in source.namelist():
            value = (replacements or {}).get(name, source.read(name))
            output.writestr(name, value)
        for name, value in (additions or {}).items():
            output.writestr(name, value)
    return result.getvalue()


@pytest.fixture
def registry(tmp_path):
    storage = prepare_storage(tmp_path / 'storage')
    source = tmp_path / 'source.docx'
    source.write_bytes(template_bytes())
    with open_database(storage) as db:
        yield SimpleNamespace(repo=TemplateRepository(db.connection, storage), db=db, storage=storage, source=source)


def register(r, **changes):
    return r.repo.register(r.source, **(dict(document_type='application', variant='dpo', name='Тестовая форма') | changes))


@pytest.mark.parametrize('document_type', DOCUMENT_TYPES)
def test_register_each_type_and_sha(registry, document_type):
    r = registry
    version = register(r, document_type=document_type)
    assert version.id and version.version == 1 and version.active and version.added_at
    assert version.sha256 == hashlib.sha256(r.source.read_bytes()).hexdigest()
    path = r.storage.root / version.relative_path
    assert path.parent == r.storage.root / 'templates'
    assert path.read_bytes() == r.source.read_bytes()
    assert r.repo.load(version.id) == version


def test_version_history_activation_rollback_and_unique_active(registry):
    r = registry
    first = register(r)
    r.source.write_bytes(template_bytes('Новая форма {{ program.name }}'))
    second = register(r, activate=False)
    assert first.id != second.id and second.version == 2 and not second.active
    assert r.repo.active_version('application', 'dpo').id == first.id
    assert r.repo.activate(second.id).active
    assert not r.repo.load(first.id).active
    assert r.repo.activate(first.id) == first
    assert not r.repo.load(second.id).active
    assert [v.version for v in r.repo.list_versions('application', 'dpo')] == [2, 1]
    with pytest.raises(sqlite3.IntegrityError):
        with r.db.connection:
            r.db.connection.execute('UPDATE document_templates SET active = 1')
    assert r.repo.active_version('application', 'dpo') == first
    assert (r.storage.root / first.relative_path).read_bytes() != (r.storage.root / second.relative_path).read_bytes()
    third = register(r)
    assert third.version == 3 and r.repo.active_version('application', 'dpo') == third
    other = register(r, variant='po')
    assert other.active and other.version == 1
    assert r.repo.active_version('application', 'dpo') == third


def test_failed_registration_and_activation_keep_previous_versions(registry):
    r = registry
    first = register(r)
    files = set((r.storage.root / 'templates').iterdir())
    r.source.write_bytes(template_bytes('{{ student.unknown }}'))
    with pytest.raises(DocxError):
        register(r)
    assert r.repo.list_versions() == [first]
    assert set((r.storage.root / 'templates').iterdir()) == files
    r.source.write_bytes(template_bytes('Вторая форма'))
    second = register(r, activate=False)
    (r.storage.root / second.relative_path).write_bytes(b'changed')
    for action in (lambda: r.repo.activate(second.id), lambda: r.repo.test_document(second.id),
                   lambda: r.repo.export_copy(second.id, r.source.parent / 'copy.docx')):
        with pytest.raises(DocxError, match='SHA-256'):
            action()
    assert r.repo.active_version('application', 'dpo') == first


def test_failed_sql_registration_cleans_file_and_rolls_back_activation(registry):
    r = registry
    first = register(r)
    files = set((r.storage.root / 'templates').iterdir())
    r.db.connection.execute("CREATE TRIGGER fail_template BEFORE INSERT ON document_templates BEGIN SELECT RAISE(ABORT, 'private error'); END")
    with pytest.raises(DocxError) as error:
        register(r)
    assert 'private error' not in str(error.value)
    assert r.repo.list_versions() == [first]
    assert set((r.storage.root / 'templates').iterdir()) == files


def test_export_editable_copy_and_generated_document_are_independent(registry):
    r = registry
    first = register(r)
    editable = r.repo.editable_copy(first.id)
    editable.write_bytes(template_bytes('Изменённая копия'))
    assert (r.storage.root / first.relative_path).read_bytes() == r.source.read_bytes()
    exported = r.repo.export_copy(first.id, r.source.parent / 'new-copy.docx')
    assert exported.read_bytes() == r.source.read_bytes()
    output = r.repo.test_document(first.id)
    before = output.read_bytes()
    assert 'TEST-unregistered-' in output.name
    assert Document(output).paragraphs[0].text == fictional_context()['student']['full_name']
    r.source.write_bytes(template_bytes('Новый текст'))
    register(r)
    assert output.read_bytes() == before and r.repo.source(first.id).path.is_file()
    with pytest.raises(DocxError, match='существует'):
        r.repo.export_copy(first.id, exported)
    assert exported.read_bytes() != editable.read_bytes()


def test_simple_fields_tables_header_footer_formatting_and_special_characters():
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run('Начало ').italic = True
    paragraph.add_run('{{ center.full_name }}').bold = True
    paragraph.add_run(' Конец')
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = '{{ program.name }}'
    table.cell(0, 1).text = '{{ program.hours }}'
    document.sections[0].header.paragraphs[0].text = '{{ document.number }}'
    document.sections[0].footer.paragraphs[0].text = '{{ training.start_date }}'
    output = BytesIO(); document.save(output)
    context = fictional_context()
    context['center']['full_name'] = 'УЦ <Пример> & «Образец»'
    result = Document(BytesIO(render_docx(output.getvalue(), context)))
    assert result.paragraphs[0].text == 'Начало УЦ <Пример> & «Образец» Конец'
    assert result.paragraphs[0].runs[0].italic
    assert result.paragraphs[0].runs[1].bold
    assert result.tables[0].cell(0, 0).text == context['program']['name']
    assert result.tables[0].cell(0, 1).text == '72'
    assert result.sections[0].header.paragraphs[0].text == context['document']['number']
    assert result.sections[0].footer.paragraphs[0].text == '2020-01-10'


def test_group_rows_preserve_style_and_support_empty_list():
    document = Document()
    table = document.add_table(rows=4, cols=2)
    table.cell(0, 0).text = 'ФИО'
    table.cell(1, 0).text = '{%tr for row in students %}'
    table.cell(2, 0).paragraphs[0].add_run('{{ row.full_name }}').bold = True
    table.cell(2, 1).text = '{{ row.start_date }}'
    table.cell(3, 0).text = '{%tr endfor %}'
    data = BytesIO(); document.save(data)
    context = fictional_context()
    result = Document(BytesIO(render_docx(data.getvalue(), context)))
    assert len(result.tables[0].rows) == 3
    assert result.tables[0].cell(1, 0).text == context['students'][0]['full_name']
    assert result.tables[0].cell(2, 0).text == context['students'][1]['full_name']
    assert any(run.bold for run in result.tables[0].cell(1, 0).paragraphs[0].runs)
    context['students'] = []
    result = Document(BytesIO(render_docx(data.getvalue(), context)))
    assert len(result.tables[0].rows) == 1


@pytest.mark.parametrize('flag', [True, False])
def test_condition_blocks_and_paragraphs(flag):
    document = Document()
    document.add_paragraph('{%p if document.is_test %}')
    document.add_paragraph('Вымышленный образец')
    document.add_paragraph('{%p else %}')
    document.add_paragraph('Другой вариант')
    document.add_paragraph('{%p endif %}')
    document.add_paragraph('{% if program.direction == "dpo" and not customer.full_name %}ДПО{% elif program.direction != "po" %}Иначе{% else %}ПО{% endif %}')
    data = BytesIO(); document.save(data)
    context = fictional_context(); context['document']['is_test'] = flag
    result = Document(BytesIO(render_docx(data.getvalue(), context)))
    assert result.paragraphs[0].text == ('Вымышленный образец' if flag else 'Другой вариант')
    assert result.paragraphs[1].text == 'Иначе'


@pytest.mark.parametrize('text', [
    '{{ student.unknown }}', '{{ unknown.name }}', '{{ student.__class__ }}',
    '{{ student.items }}', '{{ student.full_name.upper() }}', '{{ range(100) }}',
    '{{ cycler.__init__.__globals__ }}', '{{ student.full_name | safe }}',
    '{{ 2 ** 100000000 }}', '{{ "a" * 99999999 }}', '{{ student[document.number] }}',
    '{% set value = 1 %}', '{% include "file" %}', '{% import "file" as x %}',
    '{% macro x() %}a{% endmacro %}', '{{ student }}', '{{ students }}',
    '{% for x in students %}{% for y in students %}x{% endfor %}{% endfor %}',
    '{% for x in students if x.full_name %}x{% endfor %}',
    '{% for student in students %}x{% endfor %}',
    '{% if student.full_name is defined %}x{% endif %}',
    '{{r student.full_name }}', '{{p student.full_name }}', '{{ student.full_name ',
])
def test_unknown_fields_and_executable_syntax_rejected(text):
    with pytest.raises(DocxError):
        validate_template(template_bytes(text))


def test_dictionary_field_syntax_and_split_runs():
    document = Document()
    p = document.add_paragraph()
    p.add_run('{{ stu'); p.add_run('dent.full_name }}')
    document.add_paragraph('{{ program["hours"] }}')
    output = BytesIO(); document.save(output)
    result = Document(BytesIO(render_docx(output.getvalue(), fictional_context())))
    assert result.paragraphs[0].text == fictional_context()['student']['full_name']
    assert result.paragraphs[1].text == '72'


@pytest.mark.parametrize('value', [object(), Path('/tmp'), lambda: None, {'unknown': 'x'}, ['invalid']])
def test_python_objects_and_wrong_shapes_rejected(value):
    context = fictional_context()
    context['student'] = value
    with pytest.raises(DocxError):
        render_docx(template_bytes(), context)


def test_context_subclasses_and_limits_cannot_execute_code():
    class Evil(str):
        def __str__(self):
            pytest.fail('Custom object must not be evaluated')
    context = fictional_context()
    context['student']['full_name'] = Evil('x')
    with pytest.raises(DocxError):
        validate_context(context)
    for value in ('x' * 10001, '\x00', 1 << 1000, float('inf')):
        context['student']['full_name'] = value
        with pytest.raises(DocxError):
            validate_context(context)
    context = fictional_context(); context['students'] *= 251
    with pytest.raises(DocxError):
        validate_context(context)


def test_missing_data_generation_error_and_destination_protection(registry, monkeypatch):
    r = registry
    version = register(r)
    source = r.repo.source(version.id)
    destination = r.source.parent / 'result.docx'
    with pytest.raises(DocxError):
        generate_document(source, {}, destination)
    assert not destination.exists()
    destination.write_bytes(b'existing original')
    with pytest.raises(DocxError):
        generate_document(source, fictional_context(), destination)
    assert destination.read_bytes() == b'existing original'
    destination.unlink()
    def fail(*args):
        raise OSError('private failure')
    monkeypatch.setattr(engine.os, 'fsync', fail)
    with pytest.raises(DocxError):
        generate_document(source, fictional_context(), destination)
    assert not destination.exists()
    assert not list(destination.parent.glob('.docx-*.tmp'))


def test_generator_sha_and_size_budgets(registry):
    r = registry
    version = register(r)
    source = r.repo.source(version.id)
    with pytest.raises(DocxError, match='SHA-256'):
        generate_document(replace(source, sha256='0' * 64), fictional_context(), r.source.parent / 'fail.docx')
    context = fictional_context()
    context['students'] = [dict(context['student'], full_name='x' * 10000) for _ in range(200)]
    with pytest.raises(DocxError):
        render_docx(template_bytes('{% for row in students %}{{ row.full_name }}{% endfor %}'), context)
    # XML loop expansion is bounded before rendering, even with a compact context.
    context['students'] = [{} for _ in range(500)]
    with pytest.raises(DocxError, match='объём'):
        render_docx(template_bytes('{% for row in students %}' + 'word ' * 10000 + '{% endfor %}'), context)


@pytest.mark.parametrize('part', ['word/vbaProject.bin', 'word/embeddings/object.bin', '../outside.xml',
                                  '/absolute.xml', 'word/media/picture.svg', 'word/activeX/activeX1.xml'])
def test_dangerous_archive_parts(part):
    with pytest.raises(DocxError):
        validate_template(modify_package(template_bytes(), additions={part: b'danger'}))


@pytest.mark.parametrize('payload', [
    '<w:altChunk/>', '<w:object/>', '<w:attachedTemplate/>', '<w:dataBinding/>',
    '<w:fldSimple w:instr="DDE cmd.exe"/>', '<w:r><w:instrText>INCLUDEPICTURE file</w:instrText></w:r>',
])
def test_dangerous_word_instructions(payload):
    data = template_bytes()
    with ZipFile(BytesIO(data)) as package:
        document = package.read('word/document.xml').replace(b'<w:body>', b'<w:body>' + payload.encode())
    with pytest.raises(DocxError):
        validate_template(modify_package(data, {'word/document.xml': document}))


def test_external_relationships_dtd_and_bad_zip():
    data = template_bytes()
    with ZipFile(BytesIO(data)) as package:
        relations = package.read('word/_rels/document.xml.rels')
    external = f'<Relationship Id="bad" Type="{RT.IMAGE}" Target="https://example.invalid/image.png" TargetMode="External"/>'.encode()
    with pytest.raises(DocxError, match='Внешние'):
        validate_template(modify_package(data, {'word/_rels/document.xml.rels': relations.replace(b'</Relationships>', external + b'</Relationships>')}))
    for invalid in (b'not zip', modify_package(data, {'word/document.xml': b'<!DOCTYPE x [<!ENTITY a "payload">]><x>&a;</x>'})):
        with pytest.raises(DocxError):
            validate_template(invalid)
    with pytest.raises(DocxError, match='10 МиБ'):
        validate_package(b'x' * (engine.MAX_FILE_BYTES + 1))


def test_archive_duplicates_compression_bomb_and_symlink():
    data = template_bytes()
    output = BytesIO(data)
    with ZipFile(output, 'a') as archive:
        with pytest.warns(UserWarning):
            archive.writestr('word/document.xml', b'duplicate')
    with pytest.raises(DocxError):
        validate_template(output.getvalue())
    with pytest.raises(DocxError):
        validate_template(modify_package(data, additions={'word/media/image1.png': b'x' * 200000}))
    from zipfile import ZipInfo
    output = BytesIO(data)
    with ZipFile(output, 'a') as archive:
        info = ZipInfo('word/media/image2.png')
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, b'outside')
    with pytest.raises(DocxError):
        validate_template(output.getvalue())


def test_template_paths_and_symlinks_cannot_escape_storage(registry, tmp_path):
    r = registry
    version = register(r)
    path = r.storage.root / version.relative_path
    path.unlink()
    try:
        path.symlink_to(r.source)
    except OSError:
        pytest.skip('Symlinks unavailable on this platform')
    with pytest.raises(DocxError, match='пределы'):
        r.repo.source(version.id)
    with r.db.connection:
        r.db.connection.execute('UPDATE document_templates SET relative_path = ? WHERE id = ?', ('../source.docx', version.id))
    with pytest.raises(DocxError, match='путь'):
        r.repo.source(version.id)


def test_storage_relocation_preserves_versions_templates_and_test_outputs(registry, tmp_path):
    r = registry
    first = register(r)
    second = register(r)
    copy = r.repo.editable_copy(first.id)
    output = r.repo.test_document(first.id)
    moved = relocate_storage(r.storage, r.db.connection, tmp_path / 'moved')
    with Database(moved.database_path) as db:
        repo = TemplateRepository(db.connection, moved)
        assert repo.list_versions() == r.repo.list_versions()
        assert repo.source(first.id).path.read_bytes() == r.source.read_bytes()
        assert repo.source(second.id).path.read_bytes() == r.source.read_bytes()
        assert (moved.root / copy.relative_to(r.storage.root)).read_bytes() == copy.read_bytes()
        assert (moved.root / output.relative_to(r.storage.root)).read_bytes() == output.read_bytes()
        assert repo.test_document(first.id).is_file()


def test_migration_seven_preserves_six_and_existing_records(tmp_path, monkeypatch):
    from proficentr.programs import Program
    from proficentr.program_repository import ProgramRepository
    from proficentr.trainings import TrainingGroup, Training
    from proficentr.training_repository import TrainingRepository
    migrations = database_module.MIGRATIONS
    with monkeypatch.context() as old:
        old.setattr(database_module, 'MIGRATIONS', migrations[:6])
        storage = prepare_storage(tmp_path / 'archive')
        with open_database(storage) as db:
            db.connection.execute("INSERT INTO students(surname,first_name,birth_date) VALUES ('Тестов','Пример','1990-01-01')")
            db.connection.execute("INSERT INTO training_center(id,full_name) VALUES (1,'Вымышленный центр')")
            db.connection.commit()
            program = ProgramRepository(db.connection).save(Program(full_name='Тест', direction='po', kind='po_retraining', hours=100))
            repo = TrainingRepository(db.connection)
            group = repo.save_group(TrainingGroup(name='ТЕСТ', program_id=program.id, start_date='2020-01-01', end_date='2020-02-01'))
            repo.add_students(group.id, [1])
            repo.save(Training(student_id=1, program_id=program.id, start_date='2021-01-01', end_date='2021-02-01'))
            tables = ['training_center', 'students', 'counterparties', 'programs', 'training_groups', 'trainings', 'schema_migrations']
            before = {table: db.connection.execute(f'SELECT * FROM {table}').fetchall() for table in tables}
    with open_database(storage) as db:
        for table, rows in before.items():
            after = db.connection.execute(f'SELECT * FROM {table}').fetchall()
            assert (after[:6] if table == 'schema_migrations' else after) == rows
        assert db.connection.execute('SELECT max(version) FROM schema_migrations').fetchone() == (len(migrations),)
        assert db.connection.execute('PRAGMA foreign_key_check').fetchall() == []
    backups = list((storage.database_path.parent / 'migration-backups').glob('*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('SELECT max(version) FROM schema_migrations').fetchone() == (6,)
        assert backup.execute('SELECT * FROM trainings').fetchall() == before['trainings']


def test_u01_preserves_registry_files_and_generated_documents(registry, tmp_path, monkeypatch):
    from proficentr import updater
    r = registry
    first = register(r)
    second = register(r)
    r.repo.activate(first.id)
    output = r.repo.test_document(second.id)
    files = {path: path.read_bytes() for path in (r.storage.root / first.relative_path, r.storage.root / second.relative_path, output)}
    db_before = r.db.path.read_bytes()
    install = tmp_path / 'app'; install.mkdir(); (install / 'old').write_text('old')
    staged = tmp_path / 'stage'; staged.mkdir(); (staged / 'new').write_text('new')
    monkeypatch.setattr(updater.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    def launch(args, **kwargs):
        Path(args[-1]).write_text('ready')
        return SimpleNamespace(poll=lambda: None)
    updater.switch(install, staged, r.db.path, tmp_path, launch=launch)
    assert r.db.path.read_bytes() == db_before
    assert all(path.read_bytes() == original for path, original in files.items())
    assert r.repo.active_version('application', 'dpo').id == first.id


def test_command_line_smoke_renders_registered_template(registry):
    import os
    import subprocess
    import sys
    r = registry
    version = register(r)
    result = subprocess.run([sys.executable, '-m', 'proficentr', '--smoke-test'],
                            env={**os.environ, 'PROFICENTR_DATA_DIR': str(r.storage.root)},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert len(list((r.storage.root / 'exports' / 'template-tests').glob('*.docx'))) == 1
    assert r.repo.load(version.id) == version


def test_renderer_import_has_no_qt_dependency():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-c',
                             "import sys; import proficentr.docx_engine; assert not any(n.startswith('PySide6') for n in sys.modules)"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('payload', [
    '<w:p><w:r><w:pict><v:imagedata xmlns:v="urn:schemas-microsoft-com:vml" src="file:///private"/></w:pict></w:r></w:p>',
    '<w:p><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml" style="background:url(file:///private)"/></w:pict></w:r></w:p>',
])
def test_direct_xml_resource_links_rejected_without_relationship(payload):
    data = template_bytes()
    with ZipFile(BytesIO(data)) as package:
        document = package.read('word/document.xml').replace(b'<w:body>', b'<w:body>' + payload.encode())
    with pytest.raises(DocxError, match='внешние'):
        validate_template(modify_package(data, {'word/document.xml': document}))


def test_network_destination_is_domain_error_before_writing(registry):
    r = registry
    version = register(r)
    with pytest.raises(DocxError, match='локальный'):
        r.repo.export_copy(version.id, Path('//server/share/copy.docx'))
