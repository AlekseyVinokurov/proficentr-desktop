from dataclasses import replace
from datetime import date

import pytest
from PySide6.QtWidgets import QApplication, QComboBox, QMessageBox

from test_contracts import records
from test_frdo_preparation import setup
from proficentr.ui.frdo_preparation_page import FrdoPreparationPage, FrdoRecordDialog
from proficentr.ui.student_dialog import StudentDialog
from proficentr.ui.program_dialog import ProgramDialog
from proficentr.ui.main_window import MainWindow


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def context(records, app):
    r = setup(records)
    page = FrdoPreparationPage(r.frdo)
    page.start.setDate(date(2026, 11, 1))
    page.end.setDate(date(2026, 11, 30))
    yield r, page
    page.close()


def choose(edit, value):
    index = edit.findData(value)
    assert index >= 0
    edit.setCurrentIndex(index)


def test_screen_check_edit_and_recheck_without_xlsx(context, monkeypatch):
    r, page = context
    page.check_button.click()
    assert page.table.rowCount() == 1
    assert 'Требует исправления: 1' in page.state.text()
    page.table.selectRow(0)
    assert page.edit_button.isEnabled() and 'Пол получателя' in page.errors.toPlainText()
    import proficentr.ui.frdo_preparation_page as module
    def edit(dialog):
        choose(dialog.personal['sex'], 'Муж')
        choose(dialog.personal['citizenship_code'], '000')
        assert dialog.personal['citizenship_code'].currentText() == '000 — Без гражданства'
        for name, value in [('form','Очная'),('funding','Платное обучение'),('relations','в образовательной организации')]:
            choose(dialog.training[name], value)
        dialog.training['patronymic'].setText('Нет')
        dialog.training['qualification'].setText('Нет')
        assert dialog.save()
        return dialog.result()
    monkeypatch.setattr(module.FrdoRecordDialog, 'exec', edit)
    page.edit_button.click()
    assert 'Готово: 1' in page.state.text()
    assert not list((r.storage.root / 'exports').rglob('*.xlsx'))


def test_cancel_bad_input_and_changed_filter(context, monkeypatch):
    r, page = context
    dialog = FrdoRecordDialog(r.frdo, r.document.id)
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a[-1]))
    dialog.personal['sex'].setEditText('female')
    assert not dialog.save() and warnings
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    dialog.reject()
    assert r.frdo.students.load(r.first.id).sex == ''
    page.refresh()
    assert page.table.rowCount() == 1
    page.kind.setCurrentIndex(1)
    assert page.table.rowCount() == 0 and not page.edit_button.isEnabled()
    page.check_button.click()
    assert 'подключите шаблон ПО' in page.state.text()


def test_original_cards_preserve_new_fields(context):
    r, _ = context
    student = replace(r.students.load(r.first.id), sex='Муж', citizenship_code='000')
    student_dialog = StudentDialog(r.students, student)
    assert student_dialog.values().sex == 'Муж' and student_dialog.values().citizenship_code == '000'
    assert not student_dialog.is_dirty()
    program = replace(r.frdo.programs.load(r.program.id), dpo_area='Тестовая область', dpo_group='Тестовая группа')
    program_dialog = ProgramDialog(r.frdo.programs, program)
    assert program_dialog.values().dpo_area == 'Тестовая область'
    assert program_dialog.values().dpo_group == 'Тестовая группа'
    assert not program_dialog.is_dirty()
    student_dialog.close()
    program_dialog.close()


def test_documents_tab_is_available(context):
    r, _ = context
    window = MainWindow(r.db.path, r.storage, r.db)
    assert window.documents_page.tabText(window.documents_page.indexOf(window.frdo_preparation_page)) == 'Подготовка ФРДО'
    window.close()
