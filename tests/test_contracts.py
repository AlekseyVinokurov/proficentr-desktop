from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from io import BytesIO
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

from docx import Document
import pytest

import proficentr.database as db_module
import proficentr.contract_repository as module
from proficentr.contracts import Contract, ContractError, format_price, price_from_text
from proficentr.contract_repository import ContractRepository
from proficentr.counterparties import Counterparty
from proficentr.counterparty_repository import CounterpartyRepository
from proficentr.database import Database
from proficentr.docx_context import DocxError
from proficentr.programs import Program
from proficentr.program_repository import ProgramRepository
from proficentr.services import prepare_storage, open_database
from proficentr.storage import relocate_storage
from proficentr.students import Student
from proficentr.student_repository import StudentRepository
from proficentr.trainings import Training, TrainingGroup
from proficentr.training_repository import TrainingRepository


@pytest.fixture
def records(tmp_path):
    storage = prepare_storage(tmp_path / 'data')
    with open_database(storage) as db:
        students = StudentRepository(db.connection)
        first = students.save(Student(surname='Тестов', first_name='Иван', birth_date='1990-01-01', registration_address='Тестовый адрес', passport_series='1234', passport_number='123456'))
        second = students.save(Student(surname='Образцова', first_name='Анна', birth_date='1991-01-01'))
        program = ProgramRepository(db.connection).save(Program(full_name='Тестовая ДПО', direction='dpo', kind='dpo_upskilling', hours=72))
        trainings = TrainingRepository(db.connection)
        group = trainings.save_group(TrainingGroup(name='ТЕСТ-01', program_id=program.id, start_date='2020-03-01', end_date='2020-04-01'))
        members = trainings.add_students(group.id, [first.id, second.id])
        db.connection.execute("INSERT INTO training_center(id,full_name,inn) VALUES (1,'Вымышленный центр','1234567890')")
        db.connection.commit()
        repo = ContractRepository(db.connection, storage)
        source = tmp_path / 'template.docx'
        document = Document()
        document.add_paragraph('{{ document.number }} от {{ document.date }}')
        document.add_paragraph('{{ center.full_name }} · {{ customer.full_name }} · {{ program.name }} · {{ program.hours }} ч')
        document.add_paragraph('{{ contract.total_price }} {{ contract.currency }} · {{ contract.payment_terms }}')
        table = document.add_table(rows=3, cols=2)
        table.cell(0, 0).text = '{%tr for row in students %}'
        table.cell(1, 0).text = '{{ row.full_name }}'
        table.cell(1, 1).text = '{{ row.start_date }} — {{ row.end_date }}'
        table.cell(2, 0).text = '{%tr endfor %}'
        document.save(source)
        def template(kind='contract_person', variant='dpo'):
            return repo.templates.register(source, kind, variant, 'Вымышленная форма')
        yield SimpleNamespace(repo=repo, db=db, storage=storage, first=first, second=second, program=program,
                              students=students, trainings=trainings, group=group, members=members, source=source, template=template)


def draft(r, *, ids=None, **changes):
    return r.repo.save(replace(Contract(student_id=r.first.id, contract_date='2026-02-10', total_cents=12345,
                                       payment_terms='Оплата по счёту'), **changes), ids if ids is not None else [r.members[0].id])


def register(r, contract):
    review = r.repo.prepare(contract.id)
    return r.repo.register(contract.id, review.signature)


@pytest.mark.parametrize('kind', ['student', 'person', 'legal_entity', 'sole_proprietor'])
def test_each_customer_type_reuses_records_and_registers(records, kind):
    r = records
    changes = {'customer_type': kind}
    if kind == 'person':
        changes['person_data'] = {'full_name': 'Плательщик Пётр Примерович', 'registration_address': 'Адрес образца',
                                  'passport_series': '4321', 'passport_number': '654321'}
    elif kind in ('legal_entity', 'sole_proprietor'):
        customer = CounterpartyRepository(r.db.connection).save(Counterparty(kind=kind, full_name='Вымышленный заказчик'))
        changes['counterparty_id'] = customer.id
    r.template({'student': 'contract_person', 'person': 'contract_person', 'legal_entity': 'contract_legal', 'sole_proprietor': 'contract_entrepreneur'}[kind])
    before = r.db.connection.execute('SELECT count(*) FROM students').fetchone()
    saved = register(r, draft(r, **changes))
    context = json.loads(saved.snapshot)['context']
    assert saved.state == 'registered' and saved.number == '2026-0001'
    assert context['customer']['full_name']
    assert context['customer']['kind'] == kind
    assert context['contract']['total_price'] == '123,45'
    if kind == 'student':
        assert context['customer']['passport_number'] == '123456'
    assert r.db.connection.execute('SELECT count(*) FROM students').fetchone() == before
    assert r.students.load(r.first.id).workplace == ''
    assert r.trainings.load(r.members[0].id).state == 'preparation'


def test_group_subset_different_customers_and_multiple_trainings(records):
    r = records; r.template()
    first = register(r, draft(r, ids=[r.members[0].id]))
    second = register(r, draft(r, ids=[r.members[1].id], customer_type='person', person_data={'full_name': 'Другой вымышленный заказчик'}))
    assert first.number != second.number
    assert r.repo.training_ids(first.id) == [r.members[0].id]
    assert r.repo.training_ids(second.id) == [r.members[1].id]
    later = r.trainings.save(Training(student_id=r.first.id, program_id=r.program.id, start_date='2027-01-01', end_date='2027-02-01'))
    third = register(r, draft(r, ids=[later.id], contract_date='2027-01-01'))
    assert third.number == '2027-0001'
    assert r.repo.training_ids(third.id) == [later.id]


def test_one_contract_for_multiple_students_preserves_table_and_total(records):
    r = records; r.template()
    saved = register(r, draft(r, ids=[member.id for member in r.members]))
    context = json.loads(saved.snapshot)['context']
    assert context['student']['full_name'] == '' and context['contract']['student_count'] == 2
    assert context['contract']['total_price'] == '123,45'
    document = Document(r.repo.verify_document(saved.id))
    assert len(document.tables[0].rows) == 2
    assert document.tables[0].cell(1, 0).text == r.second.full_name


@pytest.mark.parametrize('text,cents', [('0', 0), ('0,10', 10), ('123.45', 12345), ('999999999999,99', 99999999999999), ('10,1', 1010)])
def test_exact_money(text, cents):
    assert price_from_text(text) == cents
    assert price_from_text(format_price(cents)) == cents


@pytest.mark.parametrize('bad', ['-1', '0.001', 'NaN', '1e3', '1 000', '9999999999999999999999999', 0.1])
def test_invalid_money(bad):
    with pytest.raises(ContractError):
        price_from_text(bad)


def test_float_and_negative_cents_rejected_in_model(records):
    for amount in (0.1, -1, True, 2**63):
        with pytest.raises(ContractError):
            draft(records, total_cents=amount)


def test_draft_edit_remove_links_change_customer_and_stale_revision(records):
    r = records
    saved = draft(r, ids=[member.id for member in r.members])
    changed = r.repo.save(replace(saved, customer_type='person', person_data={'full_name': 'Тестовый заказчик'}, total_cents=10,
                                  payment_terms='Взаимозачёт'), [r.members[1].id])
    assert changed.id == saved.id and changed.created_at == saved.created_at and changed.revision > saved.revision
    assert changed.student_id is None and changed.counterparty_id is None
    assert r.repo.training_ids(saved.id) == [r.members[1].id]
    assert changed.number is None and changed.registered_at == ''
    with pytest.raises(ContractError, match='другом окне'):
        r.repo.save(saved, [])
    assert r.repo.load(saved.id) == changed


def test_preview_uses_real_data_has_visible_stamp_and_takes_no_number(records):
    r = records; r.template()
    saved = draft(r)
    path, review = r.repo.preview(saved.id)
    text = '\n'.join(p.text for p in Document(path).paragraphs)
    assert text.startswith('ПРОЕКТ ДОГОВОРА — НЕ ЗАРЕГИСТРИРОВАН')
    assert r.first.full_name in Document(path).tables[0].cell(0, 0).text
    assert 'Вымышленный центр' in text
    assert r.repo.load(saved.id) == saved
    assert r.db.connection.execute('SELECT * FROM contract_sequences').fetchall() == []
    assert r.repo.preview(saved.id)[0] != path
    registered = r.repo.register(saved.id, review.signature)
    assert registered.number == '2026-0001'
    assert not Document(r.repo.verify_document(saved.id)).paragraphs[0].text.startswith('ПРОЕКТ')


def test_historical_number_conflict_casefold_and_automatic_sequence(records):
    r = records; r.template()
    first = register(r, draft(r, contract_date='2012-05-01', historical_number='Исх-01/ДПО'))
    assert first.number == 'Исх-01/ДПО' and first.contract_date == '2012-05-01'
    assert first.document_path.startswith('documents/2012/05/contracts/')
    assert first.created_at[:4] != '2012'
    duplicate = draft(r, historical_number='исх-01/дпо')
    with pytest.raises(ContractError, match='Номер'):
        register(r, duplicate)
    assert r.repo.load(duplicate.id).number is None
    r.repo.annul(first.id, 'Вымышленное основание')
    with pytest.raises(ContractError):
        register(r, duplicate)
    historical = register(r, draft(r, historical_number='2026-0042'))
    assert historical.number == '2026-0042'
    assert register(r, draft(r)).number == '2026-0043'
    assert register(r, draft(r, contract_date='2027-01-01')).number == '2027-0001'


def test_registration_file_hash_snapshot_and_master_data_independence(records):
    r = records; template = r.template()
    saved = register(r, draft(r))
    before = r.repo.verify_document(saved.id).read_bytes()
    snapshot = json.loads(saved.snapshot)
    assert snapshot['template']['id'] == template.id
    assert saved.document_sha256 == hashlib.sha256(before).hexdigest()
    assert snapshot['context']['program']['hours'] == 72
    r.students.save(replace(r.first, surname='Исправлен'))
    ProgramRepository(r.db.connection).save(replace(r.program, full_name='Новая редакция справочника', hours=100))
    r.trainings.save(replace(r.members[0], start_date='2021-01-01', end_date='2021-02-01'), refresh_snapshot=True)
    with r.db.connection:
        r.db.connection.execute("UPDATE training_center SET full_name = 'Новые реквизиты'")
    assert r.repo.load(saved.id) == saved and r.repo.verify_document(saved.id).read_bytes() == before
    assert r.repo.search(saved.number)[0][3] == r.first.full_name
    copy = r.repo.document_copy(saved.id)
    copy.write_bytes(b'edited external copy')
    assert r.repo.verify_document(saved.id).read_bytes() == before


def test_program_uses_training_snapshot_not_catalog_and_rejects_mixed_editions(records):
    r = records; r.template()
    ProgramRepository(r.db.connection).save(replace(r.program, full_name='Каталог изменён', hours=100))
    saved = register(r, draft(r))
    assert json.loads(saved.snapshot)['context']['program']['name'] == r.program.full_name
    r.trainings.save(r.members[1], refresh_snapshot=True)
    with pytest.raises(ContractError, match='снимком'):
        draft(r, ids=[member.id for member in r.members])


def test_different_programs_and_excluded_training_rejected(records):
    r = records
    other = ProgramRepository(r.db.connection).save(Program(full_name='Тестовая ПО', direction='po', kind='po_retraining', hours=144))
    item = r.trainings.save(Training(student_id=r.first.id, program_id=other.id, start_date='2020-03-01', end_date='2020-04-01'))
    with pytest.raises(ContractError, match='одну программу'):
        draft(r, ids=[r.members[0].id, item.id])
    r.trainings.exclude(r.members[0].id)
    with pytest.raises(ContractError, match='Исключённая'):
        draft(r)


def test_template_missing_wrong_direction_multiple_variants_and_tamper(records):
    r = records; saved = draft(r)
    with pytest.raises(ContractError, match='Шаблоны'):
        r.repo.prepare(saved.id)
    r.template(variant='po')
    with pytest.raises(ContractError):
        r.repo.prepare(saved.id)
    first = r.template(); second = r.template(variant='dpo-group')
    with pytest.raises(ContractError, match='явный выбор'):
        r.repo.prepare(saved.id)
    saved = r.repo.save(replace(saved, template_id=second.id), r.repo.training_ids(saved.id))
    review = r.repo.prepare(saved.id)
    assert review.template_id == second.id
    (r.storage.root / second.relative_path).write_bytes(b'tampered')
    with pytest.raises(DocxError, match='SHA-256'):
        r.repo.register(saved.id, review.signature)
    assert r.repo.load(saved.id).number is None


@pytest.mark.parametrize('change', ['draft', 'student', 'center', 'training', 'template'])
def test_registration_rejects_changes_after_preview(records, change):
    r = records; r.template(); saved = draft(r)
    review = r.repo.prepare(saved.id)
    if change == 'draft':
        r.repo.save(replace(saved, total_cents=1), r.repo.training_ids(saved.id))
    elif change == 'student':
        r.students.save(replace(r.first, surname='Исправление'))
    elif change == 'center':
        with r.db.connection:
            r.db.connection.execute("UPDATE training_center SET full_name = 'Изменено'")
    elif change == 'training':
        r.trainings.save(replace(r.members[0], start_date='2021-01-01', end_date='2021-02-01'))
    else:
        r.template()
    with pytest.raises(ContractError):
        r.repo.register(saved.id, review.signature)
    assert r.repo.load(saved.id).number is None
    assert r.db.connection.execute('SELECT * FROM contract_sequences').fetchall() == []


def test_required_customer_terms_links_center_and_type(records):
    r = records; r.template()
    for contract in (draft(r, ids=[]), draft(r, student_id=r.second.id), draft(r, payment_terms=''),
                     draft(r, customer_type='person'), draft(r, student_id=None)):
        with pytest.raises(ContractError):
            r.repo.prepare(contract.id)
    customer = CounterpartyRepository(r.db.connection).save(Counterparty(full_name='Вымышленная организация'))
    with pytest.raises(ContractError, match='Тип'):
        draft(r, customer_type='sole_proprietor', counterparty_id=customer.id)
    saved = draft(r)
    with r.db.connection:
        r.db.connection.execute("UPDATE training_center SET full_name = ''")
    with pytest.raises(ContractError, match='центра'):
        r.repo.prepare(saved.id)


def test_registered_and_annulled_contracts_and_links_are_immutable(records):
    r = records; r.template(); saved = register(r, draft(r))
    with pytest.raises(ContractError):
        r.repo.save(saved, [])
    for statement, params in [
        ("UPDATE contracts SET note = 'overwrite' WHERE id = ?", (saved.id,)),
        ('DELETE FROM contracts WHERE id = ?', (saved.id,)),
        ('DELETE FROM contract_trainings WHERE contract_id = ?', (saved.id,)),
        ('INSERT INTO contract_trainings(contract_id,training_id,position) VALUES (?,?,?)', (saved.id, r.members[1].id, 1)),
        ('UPDATE contract_trainings SET training_id = ? WHERE contract_id = ?', (r.members[1].id, saved.id)),
    ]:
        with pytest.raises(sqlite3.IntegrityError):
            with r.db.connection:
                r.db.connection.execute(statement, params)
    with pytest.raises(ContractError):
        r.repo.annul(saved.id, '')
    annulled = r.repo.annul(saved.id, 'Ошибка оформления — пример')
    assert annulled.number == saved.number and annulled.document_sha256 == saved.document_sha256
    assert annulled.snapshot == saved.snapshot and annulled.annulled_at
    assert register(r, draft(r)).number == '2026-0002'
    with pytest.raises(ContractError):
        r.repo.annul(saved.id, 'Ещё раз')


def test_tampered_archive_is_not_repaired_or_opened(records):
    r = records; r.template(); saved = register(r, draft(r))
    path = r.repo.verify_document(saved.id)
    path.write_bytes(b'damaged')
    with pytest.raises(ContractError, match='SHA-256'):
        r.repo.document_copy(saved.id)
    assert path.read_bytes() == b'damaged' and r.repo.load(saved.id) == saved


def test_generation_failure_no_number_and_sql_failure_quarantines_file(records, monkeypatch):
    r = records; r.template(); saved = draft(r)
    review = r.repo.prepare(saved.id)
    def fail(*args, **kwargs):
        raise DocxError('Тестовая ошибка генерации')
    with monkeypatch.context() as patch:
        patch.setattr(module, 'generate_document', fail)
        with pytest.raises(DocxError):
            r.repo.register(saved.id, review.signature)
    assert r.repo.load(saved.id) == saved
    r.db.connection.execute("CREATE TRIGGER fail_contract BEFORE UPDATE ON contracts WHEN NEW.state = 'registered' BEGIN SELECT RAISE(ABORT, 'private diagnostic'); END")
    with pytest.raises(ContractError) as error:
        r.repo.register(saved.id, review.signature)
    assert 'private diagnostic' not in str(error.value)
    assert r.repo.load(saved.id) == saved
    assert not list((r.storage.root / 'documents').rglob('contract-*.docx'))
    assert len(list((r.storage.root / 'exports' / 'unregistered-contracts').rglob('*.docx'))) == 1
    assert r.db.connection.execute('SELECT * FROM contract_sequences').fetchall() == []
    r.db.connection.execute('DROP TRIGGER fail_contract')
    assert register(r, saved).number == '2026-0001'


def test_crash_recovery_quarantines_only_unregistered_files(records):
    r = records; r.template(); saved = register(r, draft(r))
    original = r.repo.verify_document(saved.id)
    orphan = original.parent / ('contract-' + uuid4().hex + '.docx')
    orphan.write_bytes(original.read_bytes())
    temporary = original.parent / '.docx-synthetic.tmp'; temporary.write_bytes(b'partial')
    unrelated = original.parent / 'user-original.docx'; unrelated.write_bytes(b'preserve')
    assert r.repo.recover_unregistered() == 2
    assert original.is_file() and unrelated.read_bytes() == b'preserve'
    assert not orphan.exists() and not temporary.exists()
    assert r.repo.recover_unregistered() == 0
    assert any(path.read_bytes() == b'partial' for path in (r.storage.root / 'exports' / 'unregistered-contracts').rglob('*.tmp'))


def test_concurrent_registration_has_unique_sequential_numbers(records):
    r = records; r.template()
    contracts = [draft(r), draft(r)]
    barrier = Barrier(2)
    def work(contract):
        with Database(r.db.path) as db:
            repo = ContractRepository(db.connection, r.storage)
            review = repo.prepare(contract.id)
            barrier.wait(timeout=10)
            return repo.register(contract.id, review.signature).number
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert set(pool.map(work, contracts)) == {'2026-0001', '2026-0002'}


def test_relocation_and_u01_preserve_registered_and_annulled_contracts(records, tmp_path, monkeypatch):
    from proficentr import updater
    r = records; r.template()
    saved = register(r, draft(r)); annulled = r.repo.annul(saved.id, 'Вымышленное основание')
    original = r.repo.verify_document(saved.id).read_bytes()
    moved = relocate_storage(r.storage, r.db.connection, tmp_path / 'moved')
    with Database(moved.database_path) as db:
        repo = ContractRepository(db.connection, moved)
        assert repo.load(saved.id) == annulled and repo.verify_document(saved.id).read_bytes() == original
        before = moved.database_path.read_bytes()
        install = tmp_path / 'app'; install.mkdir(); (install / 'old').write_text('old')
        stage = tmp_path / 'stage'; stage.mkdir(); (stage / 'new').write_text('new')
        monkeypatch.setattr(updater.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
        def launch(args, **kwargs):
            Path(args[-1]).write_text('ready'); return SimpleNamespace(poll=lambda: None)
        updater.switch(install, stage, moved.database_path, tmp_path, launch=launch)
        assert moved.database_path.read_bytes() == before
        assert repo.load(saved.id) == annulled and repo.verify_document(saved.id).read_bytes() == original


def test_migration_eight_preserves_all_seven_tables_and_files(tmp_path, monkeypatch):
    migrations = db_module.MIGRATIONS
    with monkeypatch.context() as old:
        old.setattr(db_module, 'MIGRATIONS', migrations[:7])
        storage = prepare_storage(tmp_path / 'old')
        with open_database(storage) as db:
            db.connection.execute("INSERT INTO training_center(id,full_name) VALUES (1,'Тестовый центр')")
            db.connection.execute("INSERT INTO students(surname,first_name,birth_date) VALUES ('Тест','Пример','1990-01-01')")
            db.connection.execute("INSERT INTO counterparties(kind,full_name) VALUES ('legal_entity','Тестовая организация')")
            db.connection.commit()
            program = ProgramRepository(db.connection).save(Program(full_name='Тестовая программа', direction='dpo', kind='dpo_upskilling', hours=72))
            trainings = TrainingRepository(db.connection)
            group = trainings.save_group(TrainingGroup(name='ТЕСТ', program_id=program.id, start_date='2020-01-01', end_date='2020-02-01'))
            trainings.add_students(group.id, [1])
            source = tmp_path / 'fixture.docx'
            document = Document(); document.add_paragraph('{{ document.number }}'); document.save(source)
            from proficentr.document_templates import TemplateRepository
            templates = TemplateRepository(db.connection, storage)
            version = templates.register(source, 'contract_person', 'dpo', 'Тестовая форма')
            tables = ('schema_migrations', 'training_center', 'students', 'counterparties', 'programs', 'training_groups', 'trainings', 'document_templates')
            before = {table: db.connection.execute(f'SELECT * FROM {table}').fetchall() for table in tables}
            original = (storage.root / version.relative_path).read_bytes()
    for _ in range(2):
        with open_database(storage) as db:
            assert db.connection.execute('SELECT max(version) FROM schema_migrations').fetchone() == (8,)
            for table, rows in before.items():
                actual = db.connection.execute(f'SELECT * FROM {table}').fetchall()
                assert (actual[:7] if table == 'schema_migrations' else actual) == rows
            assert (storage.root / version.relative_path).read_bytes() == original
            assert db.connection.execute('PRAGMA foreign_key_check').fetchall() == []
    backups = list((storage.database_path.parent / 'migration-backups').glob('*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute('SELECT max(version) FROM schema_migrations').fetchone() == (7,)
        assert backup.execute('SELECT * FROM document_templates').fetchall() == before['document_templates']


def test_company_representative_basis_is_snapshotted_and_remains_historical(records):
    r = records; r.template('contract_legal')
    companies = CounterpartyRepository(r.db.connection)
    company = companies.save(Counterparty(full_name='Вымышленный заказчик', representative_name='Примеров Пётр Тестович',
                                         authority_basis='Доверенность', power_of_attorney='Тестовая доверенность № 01'))
    saved = register(r, draft(r, customer_type='legal_entity', counterparty_id=company.id))
    context = json.loads(saved.snapshot)['context']
    assert context['customer']['power_of_attorney'] == company.power_of_attorney
    companies.save(replace(company, full_name='Изменённое имя', power_of_attorney='Новая доверенность'))
    assert r.repo.load(saved.id).snapshot == saved.snapshot
    assert r.repo.search(saved.number)[0][3] == company.full_name


def test_archive_name_collision_never_replaces_registered_original(records, monkeypatch):
    r = records; r.template(); saved = register(r, draft(r))
    original = r.repo.verify_document(saved.id)
    data = original.read_bytes()
    collision_id = original.stem.removeprefix('contract-')
    monkeypatch.setattr(module, 'uuid4', lambda: SimpleNamespace(hex=collision_id))
    new = draft(r)
    with pytest.raises(DocxError, match='существует'):
        register(r, new)
    assert original.read_bytes() == data
    assert r.repo.load(new.id).number is None
    assert r.repo.load(saved.id) == saved
