from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from proficentr.database import Database
from proficentr.programs import Program
from proficentr.student_repository import StudentRepository
from proficentr.students import Student
from proficentr.training_repository import TrainingRepository
from proficentr.trainings import Training, TrainingGroup, EXCLUDED
from proficentr.ui.main_window import MainWindow, SECTIONS
from proficentr.ui.groups_page import TrainingList
from proficentr.ui.training_dialogs import GroupDialog, StudentPicker, TrainingDialog


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def context(tmp_path):
    with Database(tmp_path / 'ui.sqlite3') as db:
        repo = TrainingRepository(db.connection)
        program = repo.programs.save(Program(full_name='Пример ДПО', direction='dpo', kind='dpo_upskilling', hours=72))
        student = StudentRepository(db.connection).save(Student(surname='Вымышленный', first_name='Пример', birth_date='1990-01-01',
                                                               passport_series='1234', passport_number='123456'))
        yield db, repo, program, student


def make_group(repo, program):
    return repo.save_group(TrainingGroup(name='Группа-01', program_id=program.id, start_date='2020-03-01', end_date='2020-04-01'))


def make_training(program, student):
    return Training(program_id=program.id, student_id=student.id, start_date='2020-03-01', end_date='2020-04-01')


def test_navigation_registry_create_search_open_and_members(app, context, monkeypatch):
    db, repo, program, student = context
    window = MainWindow(db.path, database=db)
    window.navigation.setCurrentRow(SECTIONS.index('Группы'))
    page = window.groups_page
    assert window.pages.currentWidget() is page
    assert page.tabs.tabText(0) == 'Учебные группы'
    assert page.tabs.tabText(1) == 'Индивидуальное обучение'
    assert not page.open_button.isEnabled()
    def create(dialog):
        dialog.program.setCurrentIndex(dialog.program.findData(program.id))
        for key, value in {'name': 'Группа-01', 'start_date': '2020-03-01', 'end_date': '2020-04-01'}.items():
            dialog.fields[key].setText(value)
        assert dialog.save()
        return dialog.result()
    monkeypatch.setattr(GroupDialog, 'exec', create)
    page.add_button.click()
    assert page.table.rowCount() == 1 and page.open_button.isEnabled()
    group_id = page.table.item(0, 0).data(Qt.ItemDataRole.UserRole)
    def edit(dialog):
        assert dialog.original.id == group_id
        dialog.fields['name'].setText('Исправленная группа')
        assert dialog.save()
        return dialog.result()
    monkeypatch.setattr(GroupDialog, 'exec', edit)
    page.open_button.click()
    assert page.table.item(0, 0).text() == 'Исправленная группа'
    page.search.setText('ИСПРАВЛЕННАЯ'); page.refresh()
    assert page.table.rowCount() == 1
    page.table.selectRow(0)
    opened = []
    def inspect(dialog):
        widget = dialog.findChild(TrainingList)
        opened.append(widget.group_id)
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(QDialog, 'exec', inspect)
    page.members_button.click()
    assert opened == [group_id]
    page.search.setText('Нет такой группы'); page.refresh()
    assert page.table.rowCount() == 0
    window.close()


@pytest.mark.parametrize('answer', [QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Discard, QMessageBox.StandardButton.Save])
@pytest.mark.parametrize('kind', ['group', 'training'])
def test_unsaved_changes_close_cancel_discard_save(app, context, monkeypatch, kind, answer):
    db, repo, program, student = context
    if kind == 'group':
        original = make_group(repo, program)
        dialog = GroupDialog(repo, original)
    else:
        original = repo.save(make_training(program, student))
        dialog = TrainingDialog(repo, original)
    dialog.show(); app.processEvents()
    assert not dialog.is_dirty()
    if kind == 'group':
        dialog.fields['name'].setText('Изменено')
    else:
        dialog.fields['note'].setText('Изменено')
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: answer)
    dialog.close(); app.processEvents()
    stored = repo.load_group(original.id) if kind == 'group' else repo.load(original.id)
    assert dialog.isVisible() == (answer == QMessageBox.StandardButton.Cancel)
    assert (stored != original) == (answer == QMessageBox.StandardButton.Save)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Discard)
    dialog.reject()


def test_group_program_change_decline_and_confirm(app, context, monkeypatch):
    db, repo, program, student = context
    group = make_group(repo, program)
    member = repo.add_students(group.id, [student.id])[0]
    other = repo.programs.save(replace(program, id=None, full_name='Другая программа'))
    dialog = GroupDialog(repo, group)
    dialog.program.setCurrentIndex(dialog.program.findData(other.id))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    assert not dialog.save()
    assert repo.load(member.id) == member
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    assert dialog.save()
    assert repo.load(member.id).program_id == other.id


def test_picker_only_existing_compact_rows_and_multi_add_duplicate_exclude(app, context, monkeypatch):
    db, repo, program, student = context
    second = StudentRepository(db.connection).save(Student(surname='Вымышленный', first_name='Второй', birth_date='1991-01-01'))
    picker = StudentPicker(repo, multiple=True)
    picker.search.setText('вымышленный'); picker.refresh()
    assert picker.list.count() == 2
    assert all('123456' not in picker.list.item(i).text() for i in range(2))
    for index in range(2):
        picker.list.item(index).setSelected(True)
    picker.select()
    assert set(picker.student_ids) == {student.id, second.id}
    group = make_group(repo, program)
    page = TrainingList(repo, group_id=group.id)
    def select(dialog):
        dialog.student_ids = [student.id, second.id]
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(StudentPicker, 'exec', select)
    page.add_button.click()
    assert page.table.rowCount() == 2
    assert page.table.item(0, 4).text() == 'Подготовка'
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a[-1]))
    page.add_button.click()
    assert warnings and 'уже добавлен' in warnings[0] and page.table.rowCount() == 2
    page.table.selectRow(0)
    training_id = page.table.item(0, 0).data(Qt.ItemDataRole.UserRole)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    page.exclude_button.click()
    assert page.table.rowCount() == 2
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    page.exclude_button.click()
    assert page.table.rowCount() == 1 and repo.load(training_id).state == EXCLUDED
    page.show_excluded.setChecked(True)
    assert page.table.rowCount() == 2
    assert StudentRepository(db.connection).load(student.id) == student


def test_individual_picker_save_edit_overlap_and_snapshot(app, context, monkeypatch):
    db, repo, program, student = context
    dialog = TrainingDialog(repo)
    def select(picker):
        picker.student_ids = [student.id]
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(StudentPicker, 'exec', select)
    dialog.choose_student.click()
    dialog.program.setCurrentIndex(dialog.program.findData(program.id))
    dialog.fields['start_date'].setText('2020-03-01')
    dialog.fields['end_date'].setText('2020-04-01')
    assert dialog.save()
    saved = dialog.saved_training
    card = TrainingDialog(repo, saved)
    assert not card.choose_student.isEnabled() and not card.is_dirty()
    repo.programs.save(replace(program, full_name='Изменено в справочнике'))
    assert 'Пример ДПО' in card.snapshot_label.text()
    card.fields['note'].setText('Примечание')
    assert card.save() and repo.load(saved.id).program_snapshot == saved.program_snapshot
    card = TrainingDialog(repo, repo.load(saved.id))
    card.refresh_snapshot.setChecked(True)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    assert not card.save()
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    assert card.save() and 'Изменено в справочнике' in repo.load(saved.id).program_snapshot
    other = TrainingDialog(repo, make_training(program, student))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    assert not other.save()
    assert len(repo.list_trainings()) == 1
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    assert other.save() and len(repo.list_trainings()) == 2


def test_archive_restore_ui_preserves_composition(app, context, monkeypatch):
    db, repo, program, student = context
    group = make_group(repo, program)
    member = repo.add_students(group.id, [student.id])[0]
    window = MainWindow(db.path, database=db)
    page = window.groups_page
    page.refresh(group.id)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    page.archive_button.click()
    assert repo.load_group(group.id).active
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    page.archive_button.click()
    assert page.table.rowCount() == 0
    page.active_filter.setCurrentIndex(page.active_filter.findData(False))
    assert page.table.rowCount() == 1
    page.table.selectRow(0)
    assert page.archive_button.text() == 'Вернуть в активные'
    composition = TrainingList(repo, group_id=group.id)
    assert not composition.add_button.isEnabled()
    assert composition.table.rowCount() == 1
    page.archive_button.click()
    assert repo.load_group(group.id).active and repo.load(member.id) == member
    window.close()


def test_invalid_form_does_not_save_when_closing(app, context, monkeypatch):
    db, repo, program, student = context
    card = TrainingDialog(repo)
    card.show(); app.processEvents()
    card.fields['start_date'].setText('не дата')
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a[-1]))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Save)
    card.close(); app.processEvents()
    assert card.isVisible() and warnings and not repo.list_trainings()
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Discard)
    card.reject()
