from dataclasses import replace
from pathlib import Path

from docx import Document
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QInputDialog

from proficentr.contracts import Contract
from proficentr.contract_repository import ContractRepository
from proficentr.counterparties import Counterparty
from proficentr.counterparty_repository import CounterpartyRepository
from proficentr.programs import Program
from proficentr.program_repository import ProgramRepository
from proficentr.services import prepare_storage, open_database
from proficentr.student_repository import StudentRepository
from proficentr.students import Student
from proficentr.trainings import TrainingGroup
from proficentr.training_repository import TrainingRepository
from proficentr.ui.main_window import MainWindow, SECTIONS
from proficentr.ui.contracts_page import ContractDialog
import proficentr.ui.contracts_page as ui_module


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def context(app, tmp_path, monkeypatch):
    storage = prepare_storage(tmp_path / 'data')
    with open_database(storage) as db:
        db.connection.execute("INSERT INTO training_center(id,full_name) VALUES (1,'Тестовый центр')")
        db.connection.commit()
        students = StudentRepository(db.connection)
        one = students.save(Student(surname='Вымышленный', first_name='Иван', birth_date='1990-01-01'))
        two = students.save(Student(surname='Вымышленная', first_name='Анна', birth_date='1990-01-01'))
        program = ProgramRepository(db.connection).save(Program(full_name='Тестовая ДПО', direction='dpo', kind='dpo_upskilling', hours=72))
        trainings = TrainingRepository(db.connection)
        group = trainings.save_group(TrainingGroup(name='ТЕСТ', program_id=program.id, start_date='2020-01-01', end_date='2020-02-01'))
        members = trainings.add_students(group.id, [one.id, two.id])
        repo = ContractRepository(db.connection, storage)
        source = tmp_path / 'template.docx'
        document = Document(); document.add_paragraph('{{ customer.full_name }} {{ document.number }} {{ contract.total_price }}'); document.save(source)
        repo.templates.register(source, 'contract_person', 'dpo', 'Вымышленный договор')
        monkeypatch.setattr(ui_module, 'open_local_docx', lambda path: None)
        window = MainWindow(db.path, storage, db)
        yield window, repo, group, members, one
        window.close()


def fill(dialog):
    dialog.price.setText('123,45')
    dialog.terms.setPlainText('Оплата по счёту или взаимозачёт — пример')


def test_single_card_save_preview_confirm_registration_and_registry(context, monkeypatch):
    window, repo, group, members, one = context
    window.navigation.setCurrentRow(SECTIONS.index('Документы'))
    page = window.contracts_page
    assert window.pages.currentWidget() is page and page.table.rowCount() == 0
    def execute(dialog):
        assert len(dialog.training_ids) == 1
        assert dialog.customer_student.currentData() == one.id
        fill(dialog)
        assert dialog.save()
        saved = dialog.saved_contract
        assert saved.number is None
        assert dialog.preview() and dialog.register_button.isEnabled()
        monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
        assert not dialog.register() and repo.load(saved.id).number is None
        monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
        assert dialog.register()
        return dialog.result()
    monkeypatch.setattr(ContractDialog, 'exec', execute)
    page.open_card(training_ids=[members[0].id])
    assert page.table.rowCount() == 1
    assert page.table.item(0, 2).text() == one.full_name
    assert page.table.item(0, 5).text() == '123,45'
    assert page.table.item(0, 6).text() == 'Зарегистрирован'
    number = page.table.item(0, 0).text()
    page.search.setText(number); page.refresh()
    assert page.table.rowCount() == 1
    page.search.setText('Отсутствует'); page.refresh()
    assert page.table.rowCount() == 0


def test_group_subset_select_remove_and_customer_fields(context, monkeypatch):
    window, repo, group, members, one = context
    card = ContractDialog(repo, group_id=group.id)
    assert card.group_filter.currentData() == group.id
    assert card.available.rowCount() == 2 and card.selected.count() == 0
    card.available.selectAll(); card.add_trainings()
    assert set(card.training_ids) == {member.id for member in members}
    card.selected.item(0).setSelected(True); card.remove_trainings()
    assert len(card.training_ids) == 1
    card.customer_type.setCurrentIndex(card.customer_type.findData('person'))
    card.person_fields['full_name'].setText('Сторонний вымышленный заказчик')
    fill(card)
    assert card.save()
    assert card.saved_contract.student_id is None
    assert len(repo.training_ids(card.saved_contract.id)) == 1
    assert card.saved_contract.person_data['full_name'] == 'Сторонний вымышленный заказчик'
    assert repo.connection.execute('SELECT count(*) FROM students').fetchone() == (2,)
    card.reject()


@pytest.mark.parametrize('kind', ['legal_entity', 'sole_proprietor'])
def test_existing_company_selected_without_new_cards(context, kind):
    window, repo, group, members, one = context
    company = CounterpartyRepository(repo.connection).save(Counterparty(kind=kind, full_name='Заказчик — пример'))
    card = ContractDialog(repo, training_ids=[members[0].id])
    card.customer_type.setCurrentIndex(card.customer_type.findData(kind))
    assert card.customer_company.findData(company.id) > 0
    card.customer_company.setCurrentIndex(card.customer_company.findData(company.id))
    fill(card); assert card.save()
    assert card.saved_contract.counterparty_id == company.id and card.saved_contract.student_id is None
    assert not card.person_fields['full_name'].isEnabled()
    card.reject()


@pytest.mark.parametrize('answer', [QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Discard, QMessageBox.StandardButton.Save])
def test_unsaved_draft_cancel_discard_save(context, app, monkeypatch, answer):
    window, repo, group, members, one = context
    saved = repo.save(Contract(student_id=one.id, contract_date='2020-01-01', payment_terms='Пример'), [members[0].id])
    card = ContractDialog(repo, saved)
    card.show(); app.processEvents()
    assert not card.is_dirty()
    card.price.setText('987,65')
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: answer)
    card.close(); app.processEvents()
    assert card.isVisible() == (answer == QMessageBox.StandardButton.Cancel)
    assert (repo.load(saved.id).total_cents == 98765) == (answer == QMessageBox.StandardButton.Save)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Discard)
    card.reject()


def test_changes_invalidate_preview_and_source_changes_block_registration(context, monkeypatch):
    window, repo, group, members, one = context
    card = ContractDialog(repo, training_ids=[members[0].id]); fill(card)
    assert card.preview() and card.review
    card.price.setText('1,01')
    assert card.review is None and not card.register_button.isEnabled()
    assert card.preview()
    StudentRepository(repo.connection).save(replace(one, surname='Исправлено'))
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a[-1]))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    assert not card.register() and warnings
    assert repo.load(card.original.id).number is None
    card.reject()


def test_missing_template_preserves_draft_and_points_to_settings(context, monkeypatch):
    window, repo, group, members, one = context
    card = ContractDialog(repo, training_ids=[members[0].id])
    card.customer_type.setCurrentIndex(card.customer_type.findData('person'))
    card.person_fields['full_name'].setText('Вымышленный заказчик')
    fill(card)
    # Active version is still known, but its file is deliberately missing.
    version = repo.templates.list_versions()[0]
    (repo.storage.root / version.relative_path).unlink()
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a[-1]))
    assert not card.preview() and warnings
    assert card.original.id and repo.load(card.original.id).state == 'draft'
    card.reject()


def test_annul_confirmation_and_open_copy_use_preserved_archive(context, monkeypatch):
    window, repo, group, members, one = context
    saved = repo.save(Contract(student_id=one.id, contract_date='2020-01-01', payment_terms='Пример'), [members[0].id])
    saved = repo.register(saved.id, repo.prepare(saved.id).signature)
    original = repo.verify_document(saved.id).read_bytes()
    page = window.contracts_page; page.refresh(saved.id)
    assert page.copy_button.isEnabled() and page.annul_button.isEnabled()
    opened = []
    monkeypatch.setattr(ui_module, 'open_local_docx', lambda path: opened.append(path))
    page.copy_button.click()
    assert len(opened) == 1 and opened[0] != repo.verify_document(saved.id)
    assert opened[0].read_bytes() == original
    monkeypatch.setattr(QInputDialog, 'getMultiLineText', lambda *a: ('Вымышленная ошибка оформления', True))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    page.annul_button.click(); assert repo.load(saved.id).state == 'registered'
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    page.annul_button.click(); assert repo.load(saved.id).state == 'annulled'
    assert not page.annul_button.isEnabled() and page.copy_button.isEnabled()
    assert repo.verify_document(saved.id).read_bytes() == original


def test_quick_entry_from_group_and_individual_sections(context, monkeypatch):
    window, repo, group, members, one = context
    calls = []
    monkeypatch.setattr(window.contracts_page, 'open_card', lambda **kwargs: calls.append(kwargs))
    page = window.groups_page
    page.refresh(group.id)
    page.contract_button.click()
    assert calls[-1] == {'training_ids': [], 'group_id': group.id}
    assert window.pages.currentWidget() is window.contracts_page
    page.individual.contract_requested.emit([members[0].id], None)
    assert calls[-1] == {'training_ids': [members[0].id], 'group_id': None}


def test_stale_open_card_requires_reload_before_preview(context, monkeypatch):
    window, repo, group, members, one = context
    saved = repo.save(Contract(student_id=one.id, contract_date='2020-01-01', payment_terms='Пример'), [members[0].id])
    card = ContractDialog(repo, saved)
    repo.save(replace(saved, total_cents=100), [members[0].id])
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: warnings.append(args[-1]))
    assert not card.preview() and warnings and 'другом окне' in warnings[0]
    assert card.review is None
    card.reject()
