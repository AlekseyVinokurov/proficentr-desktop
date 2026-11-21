"""Все сведения и классификаторы вымышленные."""
from dataclasses import asdict, replace
from datetime import date
import json
import sqlite3

import pytest

from test_contracts import records
from test_issued_documents import complete, draft, register
from frdo_fixtures import preparation_template
from proficentr.database import Database, MIGRATIONS
import proficentr.database as database_module
from proficentr.frdo_preparation import FrdoPreparationRepository
from proficentr.frdo_templates import FrdoError
from proficentr.program_repository import ProgramRepository
from proficentr.services import open_database
from proficentr.trainings import Training


@pytest.fixture
def prepared(records):
    return setup(records)


def setup(records, kind='dpo', program_kind='dpo_upskilling'):
    r = complete(records, kind, program_kind)
    r.frdo = FrdoPreparationRepository(r.db.connection, r.storage)
    source = r.source.parent / f'{kind}.xlsx'
    source.write_bytes(preparation_template(kind))
    r.frdo.templates.register(source, kind)
    r.document = register(r, draft(r, blank_series='ТЕСТ', blank_number='000001', document_date='2026-11-01'))
    return r


def changes(r, *, citizenship='000'):
    return dict(personal={'sex': 'Муж', 'citizenship_code': citizenship},
                training={'form': 'Очная', 'funding': 'Платное обучение', 'relations': 'в образовательной организации',
                          'patronymic': 'Нет', **({'qualification': 'Нет'} if r.document.direction == 'dpo' else {'po_profession': 'Тестовая профессия'})})


def save(r, **kwargs):
    return r.frdo.save_details(r.document.id, expected_signature=r.frdo.review(r.document.id).signature, **kwargs)


@pytest.mark.parametrize('kind,program_kind', [('dpo', 'dpo_upskilling'), ('dpo', 'dpo_retraining'), ('po', 'po_initial_training'), ('po', 'po_retraining'), ('po', 'po_upskilling')])
def test_adapters_use_actual_document_fields_and_exact_dictionaries(records, kind, program_kind):
    r = setup(records, kind, program_kind)
    before = r.frdo.review(r.document.id)
    assert not before.ready
    after = save(r, **changes(r))
    assert after.ready, after.messages()
    values = after.values
    assert len(values) == (40 if kind == 'dpo' else 35)
    assert values['B'] == 'Оригинал' and values['C'] == values['D'] == values['E'] == 'Нет'
    assert values['F'] == 'ТЕСТ' and values['G'] == '000001' and values['I'] == r.document.number
    assert values['H'] == '01.11.2026' and values['K'] == r.document.program_name
    assert values['S' if kind == 'dpo' else 'N'] == '2020'
    assert values['T' if kind == 'dpo' else 'O'] == '2020'
    assert values['U' if kind == 'dpo' else 'P'] == '72'
    assert values['AE' if kind == 'dpo' else 'W'] == '000'
    assert all(not values[col] for col in (['AF','AG','AH','AI','AJ','AK','AL','AM','AN'] if kind == 'dpo' else ['AA','AB','AC','AD','AE','AF','AG','AH','AI']))
    values['G'] = 'tamper'
    assert after.values['G'] == '000001'  # Стабильное представление не меняется извне.


def test_missing_fields_never_guessed_and_problems_have_human_field_names(prepared):
    r = prepared
    review = r.frdo.review(r.document.id)
    assert not review.ready and review.status == 'Требует исправления'
    assert review.values['Z'] == review.values['AE'] == review.values['AB'] == review.values['AC'] == ''
    assert review.values['X'] == review.values['N'] == ''
    assert any('Пол получателя' in text and r.document.full_name in text for text in review.messages())
    assert not r.db.connection.execute('SELECT * FROM frdo_training_details').fetchall()
    assert not r.db.connection.execute('SELECT * FROM frdo_change_history').fetchall()


def test_edits_persist_restart_and_never_modify_registered_document(prepared):
    r = prepared
    original = asdict(r.document)
    source = r.repo.verify_document(r.document.id).read_bytes()
    after = save(r, **changes(r))
    assert r.frdo.students.load(r.first.id).sex == 'Муж'
    assert asdict(r.repo.load(r.document.id)) == original
    assert r.repo.verify_document(r.document.id).read_bytes() == source
    with open_database(r.storage) as db:
        restarted = FrdoPreparationRepository(db.connection, r.storage)
        assert restarted.review(r.document.id).cells == after.cells
        assert restarted.review(r.document.id).signature == after.signature
        assert restarted.review(r.document.id).ready
        assert db.connection.execute('SELECT count(*) FROM frdo_change_history').fetchone() == (1,)


def test_program_current_edition_and_other_student_changes_do_not_replace_history(prepared):
    r = prepared
    initial = r.frdo.review(r.document.id)
    current = r.frdo.programs.load(r.program.id)
    r.frdo.programs.save(replace(current, full_name='Совершенно новая редакция', hours=999, qualification='Новая', dpo_area='Тестовая область'))
    r.students.save(replace(r.students.load(r.first.id), surname='Новаяфамилия'))
    review = r.frdo.review(r.document.id)
    assert review.values['K'] == initial.values['K'] and review.values['U'] == '72'
    assert review.values['V'] == 'Тестов' and review.values['L'] == ''
    assert review.signature != initial.signature
    with pytest.raises(FrdoError, match='изменились'):
        r.frdo.save_details(r.document.id, expected_signature=initial.signature, **changes(r))


def test_two_documents_in_same_group_share_personal_but_not_training_conditions(prepared):
    r = prepared
    second = register(r, draft(r, index=1, blank_series='ТЕСТ', blank_number='000002', document_date='2026-11-02'))
    save(r, **changes(r))
    other = r.frdo.review(second.id)
    assert other.training_id != r.document.training_id and other.student_id != r.first.id
    assert other.values['AB'] == other.values['AC'] == other.values['Z'] == ''
    rows = r.frdo.search('dpo', '2026-11-01', '2026-11-01')
    assert [row.document_id for row in rows] == [r.document.id]
    assert len(r.frdo.search('dpo', '2026-11-01', '2026-11-30')) == 2


@pytest.mark.parametrize('bad', ['муж', 'male', 'Муж '])
def test_dictionary_errors_rollback_all_changes(prepared, bad):
    r = prepared
    arguments = changes(r)
    arguments['personal']['sex'] = bad
    with pytest.raises(FrdoError, match='справочника'):
        save(r, **arguments)
    assert r.frdo.students.load(r.first.id).sex == ''
    assert not r.db.connection.execute('SELECT * FROM frdo_change_history').fetchall()
    assert not r.db.connection.execute('SELECT * FROM frdo_training_details').fetchall()


def test_unknown_personal_and_document_fields_cannot_be_overwritten(prepared):
    with pytest.raises(FrdoError, match='неподдерживаемые'):
        save(prepared, personal={'blank_number': 'invented'})
    with pytest.raises(FrdoError, match='категории'):
        save(prepared, training={'po_profession': 'Тестовая профессия'})


def test_template_replacement_invalidates_old_review(prepared):
    r = prepared
    review = r.frdo.review(r.document.id)
    source = r.source.parent / 'replacement.xlsx'
    source.write_bytes(preparation_template('dpo'))
    r.frdo.templates.register(source, 'dpo')
    with pytest.raises(FrdoError, match='изменились'):
        r.frdo.save_details(r.document.id, expected_signature=review.signature, **changes(r))


def test_pre_export_validator_checks_citizenship_snils_without_guessing(prepared):
    r = prepared
    checked = save(r, **changes(r, citizenship='643'))
    assert not checked.ready and any(issue.column == 'AA' for issue in checked.issues)
    r.students.save(replace(r.students.load(r.first.id), snils='11223344595'))
    assert r.frdo.review(r.document.id).ready
    assert r.frdo.review(r.document.id).values['AA'] == '112-233-445 95'


def test_current_program_classifiers_saved_for_future_snapshots_only(prepared):
    r = prepared
    result = save(r, program={'dpo_area': 'Тестовая область', 'dpo_group': 'Тестовая группа'})
    assert result.values['L'] == result.values['M'] == ''
    future = r.trainings.save(Training(student_id=r.first.id, program_id=r.program.id, start_date='2027-01-01', end_date='2027-02-01'))
    snapshot = json.loads(future.program_snapshot)
    assert snapshot['dpo_area'] == 'Тестовая область' and snapshot['dpo_group'] == 'Тестовая группа'
    assert json.loads(r.members[0].program_snapshot).get('dpo_area', '') == ''
    result = save(r, training={'dpo_area': 'Тестовая область'})
    assert result.values['L'] == 'Тестовая область'


@pytest.mark.parametrize('sql', ['UPDATE frdo_change_history SET changes=\'{}\'', 'DELETE FROM frdo_change_history',
    'INSERT OR REPLACE INTO frdo_change_history(id,document_id,changes) SELECT id,document_id,changes FROM frdo_change_history'])
def test_change_history_is_immutable(prepared, sql):
    r = prepared
    save(r, **changes(r))
    before = r.db.connection.execute('SELECT * FROM frdo_change_history').fetchall()
    with pytest.raises(sqlite3.IntegrityError), r.db.connection:
        r.db.connection.execute(sql)
    assert r.db.connection.execute('SELECT * FROM frdo_change_history').fetchall() == before


def test_classifiers_in_historical_snapshot_survive_program_revisions(records):
    r = records
    r.program = r.trainings.programs.save(replace(r.program, dpo_area='Тестовая область', dpo_group='Тестовая группа'))
    r.members = [r.trainings.save(member, refresh_snapshot=True) for member in r.members]
    r = setup(r)
    assert r.frdo.review(r.document.id).values['L'] == 'Тестовая область'
    r.frdo.programs.save(replace(r.frdo.programs.load(r.program.id), dpo_area='', dpo_group='', full_name='Другая редакция'))
    review = r.frdo.review(r.document.id)
    assert review.values['L'] == 'Тестовая область' and review.values['M'] == 'Тестовая группа'


def test_individual_and_group_training_of_same_student_remain_independent(prepared):
    from proficentr.enrollment_orders import EnrollmentOrder
    from proficentr.enrollment_order_repository import EnrollmentOrderRepository
    from proficentr.attestations import AttestationProtocol, AttestationResult
    from proficentr.attestation_repository import AttestationRepository
    from proficentr.completion_orders import CompletionOrder, CompletionEntry
    from proficentr.completion_repository import CompletionRepository
    from proficentr.issued_documents import IssuedDocument
    from proficentr.issued_repository import IssuedDocumentRepository
    r = prepared
    save(r, **changes(r))
    program = r.frdo.programs.save(replace(r.program, id=None, full_name='Отдельная историческая программа', hours=144, kind='dpo_retraining'))
    individual = r.trainings.save(Training(student_id=r.first.id, program_id=program.id, start_date='2021-01-01', end_date='2021-02-01'))
    orders = EnrollmentOrderRepository(r.db.connection, r.storage)
    orders.templates.register(r.source, 'enrollment_order', 'dpo-individual', 'Тест')
    order = orders.save(EnrollmentOrder(order_date='2020-12-01'), [individual.id])
    order = orders.register(order.id, orders.prepare(order.id).signature)
    orders.confirm_signing(order.id, signed_date='2020-12-02', confirmed_by='Тест', grounds_checked=True, education_checked=True)
    protocols = AttestationRepository(r.db.connection, r.storage)
    protocol = protocols.save(AttestationProtocol(protocol_date='2021-02-01', attestation_date='2021-02-01', direction='dpo'), [AttestationResult(individual.id, 'passed')])
    protocol = protocols.register(protocol.id, protocols.prepare(protocol.id).signature)
    protocols.confirm_signing(protocol.id, signed_date='2021-02-01', confirmed_by='Тест')
    completions = CompletionRepository(r.db.connection, r.storage)
    completions.templates.register(r.source, 'completion_order', 'dpo-individual', 'Тест')
    order = completions.save(CompletionOrder(order_date='2021-02-02', direction='dpo'), [CompletionEntry(individual.id, '2021-02-01')])
    order = completions.register(order.id, completions.prepare(order.id).signature)
    completions.confirm_signing(order.id, signed_date='2021-02-02', confirmed_by='Тест', conditions_checked=True)
    issued = IssuedDocumentRepository(r.db.connection, r.storage)
    issued.templates.register(r.source, 'dpo_retraining_diploma', 'standard', 'Тест')
    document = issued.save(IssuedDocument(training_id=individual.id, document_date='2026-11-02', blank_series='ТЕСТ', blank_number='000002'))
    document = issued.register(document.id, issued.prepare(document.id).signature)
    row = r.frdo.review(document.id)
    assert row.values['K'] == 'Отдельная историческая программа' and row.values['U'] == '144'
    assert row.values['S'] == row.values['T'] == '2021'
    assert row.values['Z'] == 'Муж' and row.values['AE'] == '000'
    assert row.values['AB'] == row.values['AC'] == row.values['N'] == ''
    assert row.training_id != r.document.training_id and row.student_id == r.first.id
    assert r.frdo.review(r.document.id).ready


def test_migration14_preserves_data_and_unknown_defaults(tmp_path, monkeypatch):
    path = tmp_path / 'v13.sqlite3'
    with monkeypatch.context() as patch:
        patch.setattr(database_module, 'MIGRATIONS', MIGRATIONS[:13])
        with Database(path) as db:
            db.connection.execute("INSERT INTO students(surname,first_name,birth_date,citizenship) VALUES('Тестов','Иван','1990-01-01','Россия')")
            db.connection.execute("INSERT INTO programs(full_name,direction,kind,hours) VALUES('Тест','dpo','dpo_upskilling',72)")
            db.connection.commit()
    with Database(path) as db:
        assert db.connection.execute('SELECT surname,citizenship,sex,citizenship_code FROM students').fetchone() == ('Тестов','Россия','','')
        assert db.connection.execute('SELECT dpo_area,dpo_group,po_profession FROM programs').fetchone() == ('','','')
        assert not db.connection.execute('SELECT * FROM frdo_training_details').fetchall()
    with sqlite3.connect(next((tmp_path / 'migration-backups').glob('*.sqlite3'))) as old:
        assert old.execute('SELECT max(version) FROM schema_migrations').fetchone() == (13,)


def test_database_failure_rolls_back_personal_training_program_and_history(prepared):
    r = prepared
    before = r.frdo.review(r.document.id)
    r.db.connection.execute("CREATE TRIGGER fail_frdo_history BEFORE INSERT ON frdo_change_history BEGIN SELECT RAISE(ABORT,'test failure'); END")
    r.db.connection.commit()
    with pytest.raises(FrdoError, match='не сохранены'):
        save(r, **changes(r), program={'dpo_area': 'Тестовая область'})
    assert r.frdo.review(r.document.id).signature == before.signature
    assert r.frdo.students.load(r.first.id).sex == ''
    assert r.frdo.programs.load(r.program.id).dpo_area == ''
    assert not r.db.connection.execute('SELECT * FROM frdo_training_details').fetchall()
    assert not r.db.connection.execute('SELECT * FROM frdo_change_history').fetchall()
