from dataclasses import replace

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from proficentr.services import open_database, prepare_storage
from proficentr.student_repository import StudentRepository
from proficentr.students import Student
from proficentr.ui.main_window import MainWindow
from proficentr.ui.student_dialog import DuplicateDialog, StudentDialog


def sample():
    return Student(surname="Тестовый", first_name="Пример", birth_date="1990-02-15")


def test_create_and_edit_refresh_table(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    storage = prepare_storage(tmp_path / "archive")
    with open_database(storage) as db:
        window = MainWindow(db.path, storage, db)
        page = window.students_page
        assert page.table.rowCount() == 0

        def create(dialog):
            for field in ("surname", "first_name", "birth_date"):
                dialog.fields[field].setText(getattr(sample(), field))
            assert dialog.save()
            return dialog.result()

        page.search.setText("Не подходит к новому слушателю")
        monkeypatch.setattr(StudentDialog, "exec", create)
        page.add_button.click()
        assert page.table.rowCount() == 1
        assert page.search.text() == ""
        assert page.table.item(0, 0).text() == "Тестовый Пример"
        assert page.open_button.isEnabled()

        def edit(dialog):
            assert dialog.fields["surname"].text() == "Тестовый"
            dialog.fields["surname"].setText("Исправленный")
            assert dialog.save()
            return dialog.result()

        monkeypatch.setattr(StudentDialog, "exec", edit)
        page.open_button.click()
        assert page.table.rowCount() == 1
        assert page.table.item(0, 0).text() == "Исправленный Пример"
        page.search.setText("исправл")
        page.refresh()
        assert page.table.rowCount() == 1
        page.search.setText("НетТакого")
        page.refresh()
        assert page.table.rowCount() == 0
        window.close()


def test_cancel_and_failed_save_do_not_change_database(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    storage = prepare_storage(tmp_path / "archive")
    answer = [QMessageBox.StandardButton.Cancel]
    monkeypatch.setattr(QMessageBox, "question", lambda *args: answer[0])
    messages = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[-1]))
    with open_database(storage) as db:
        repo = StudentRepository(db.connection)
        original = repo.save(sample())
        dialog = StudentDialog(repo, original)
        dialog.show()
        dialog.fields["first_name"].setText("Несохранённое")
        dialog.reject()
        assert dialog.isVisible()
        assert repo.load(original.id) == original
        dialog.fields["birth_date"].setText("2099-01-01")
        assert not dialog.save()
        assert messages
        assert repo.load(original.id) == original
        answer[0] = QMessageBox.StandardButton.Discard
        dialog.reject()
        assert not dialog.isVisible()
        assert repo.load(original.id) == original


def test_duplicates_open_existing_or_explicitly_save_separate(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    storage = prepare_storage(tmp_path / "archive")
    with open_database(storage) as db:
        repo = StudentRepository(db.connection)
        existing = repo.save(sample())

        def open_existing(dialog):
            assert dialog.matches[0].reason == "name_birth_date"
            dialog.open_selected()
            return dialog.result()

        monkeypatch.setattr(DuplicateDialog, "exec", open_existing)
        card = StudentDialog(repo)
        for field in ("surname", "first_name", "birth_date"):
            card.fields[field].setText(getattr(sample(), field))
        assert not card.save()
        assert card.open_student_id == existing.id
        assert len(repo.search()) == 1

        def separate(dialog):
            dialog.confirm_separate()
            return dialog.result()

        monkeypatch.setattr(DuplicateDialog, "exec", separate)
        card = StudentDialog(repo)
        for field in ("surname", "first_name", "birth_date"):
            card.fields[field].setText(getattr(sample(), field))
        assert card.save()
        assert card.saved_student.id != existing.id
        assert len(repo.search()) == 2


def test_open_duplicate_routes_to_existing_card(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    storage = prepare_storage(tmp_path / "archive")
    with open_database(storage) as db:
        window = MainWindow(db.path, storage, db)
        repo = window.students_page.repository
        existing = repo.save(sample())
        opened = []

        def execute(dialog):
            opened.append(dialog.original.id)
            if dialog.original.id is None:
                dialog.open_student_id = existing.id
            return QDialog.DialogCode.Rejected

        monkeypatch.setattr(StudentDialog, "exec", execute)
        window.students_page.open_card()
        assert opened == [None, existing.id]
        assert len(repo.search()) == 1
        window.close()
