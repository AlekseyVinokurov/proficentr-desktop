from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from proficentr.services import open_database, prepare_storage
from proficentr.ui.main_window import MainWindow, SECTIONS
from test_organization import make_logo


def test_form_load_save_and_deferred_logo(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    storage = prepare_storage(tmp_path / "archive")
    logo = make_logo(tmp_path / "logo.png")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(logo), ""))
    with open_database(storage) as db:
        window = MainWindow(db.path, storage, db)
        page = window.settings_page.center_page
        page.fields["full_name"].setText("Учебный центр Пример")
        page.upload_button.click()
        assert page.is_dirty()
        assert page.repository.load().full_name == ""
        assert page.repository.load().logo_path == ""
        assert not page.preview.pixmap().isNull()
        assert list((storage.root / "organization").iterdir()) == []
        page.save_button.click()
        assert not page.is_dirty()
        assert page.repository.load().full_name == "Учебный центр Пример"
        window.close()
    with open_database(storage) as db:
        window = MainWindow(db.path, storage, db)
        page = window.settings_page.center_page
        assert page.fields["full_name"].text() == "Учебный центр Пример"
        assert not page.preview.pixmap().isNull()
        page.remove_button.click()
        assert page.repository.load().logo_path
        page.save_button.click()
        assert page.repository.load().logo_path == ""
        window.close()


def test_dirty_navigation_close_and_save_error(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    storage = prepare_storage(tmp_path / "archive")
    answer = [QMessageBox.StandardButton.Cancel]
    monkeypatch.setattr(QMessageBox, "question", lambda *args: answer[0])
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: errors.append(args[-1]))
    with open_database(storage) as db:
        window = MainWindow(db.path, storage, db)
        window.show()
        window.navigation.setCurrentRow(SECTIONS.index("Настройки"))
        settings = window.settings_page
        settings.tabs.setCurrentIndex(1)
        page = settings.center_page
        page.fields["short_name"].setText("Несохранённый пример")
        settings.tabs.setCurrentIndex(0)
        assert settings.tabs.currentIndex() == 1
        window.navigation.setCurrentRow(0)
        assert window.pages.currentWidget() is settings
        assert window.navigation.currentRow() == SECTIONS.index("Настройки")
        window.close()
        assert window.isVisible()
        answer[0] = QMessageBox.StandardButton.Save
        page.fields["inn"].setText("bad")
        window.navigation.setCurrentRow(0)
        assert window.pages.currentWidget() is settings
        assert errors
        assert page.repository.load().short_name == ""
        assert page.is_dirty()
        page.fields["inn"].clear()
        window.navigation.setCurrentRow(0)
        assert window.pages.currentIndex() == 0
        assert page.repository.load().short_name == "Несохранённый пример"
        assert not page.is_dirty()
        window.navigation.setCurrentRow(SECTIONS.index("Настройки"))
        page.fields["short_name"].setText("Отменённое изменение")
        answer[0] = QMessageBox.StandardButton.Discard
        window.navigation.setCurrentRow(0)
        assert page.fields["short_name"].text() == "Несохранённый пример"
        window.close()
