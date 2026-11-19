import importlib
import os
import subprocess
import sys

from PySide6.QtWidgets import QApplication

from proficentr.ui.main_window import MainWindow, SECTIONS


def test_main_modules_import():
    for name in ("__main__", "settings", "database", "services", "ui.main_window"):
        assert importlib.import_module(f"proficentr.{name}")


def test_window_navigation(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path / "synthetic.sqlite3")
    window.show()
    app.processEvents()
    assert window.windowTitle() == "Proficentr Desktop"
    assert window.navigation.count() == window.pages.count() == 8
    for index, title in enumerate(SECTIONS):
        window.navigation.setCurrentRow(index)
        app.processEvents()
        assert window.pages.currentIndex() == index
        assert title in window.statusBar().currentMessage()
    window.close()


def test_entrypoint_twice(tmp_path):
    env = {**os.environ, "PROFICENTR_DATA_DIR": str(tmp_path)}
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-m", "proficentr", "--smoke-test"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
    assert (tmp_path / "database" / "proficentr.sqlite3").is_file()


def test_settings_transfer_and_restart(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    from proficentr import settings
    from proficentr.services import open_database, prepare_storage

    app = QApplication.instance() or QApplication([])
    storage = prepare_storage(tmp_path / "source")
    target = tmp_path / "Новое хранилище"
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(target))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    notices = []
    monkeypatch.setattr(QMessageBox, "information", lambda *args: notices.append(args[1]))
    quit_calls = []
    monkeypatch.setattr(app, "quit", lambda: quit_calls.append(True))
    with open_database(storage) as db:
        window = MainWindow(db.path, storage, db)
        page = window.settings_page
        assert str(storage.root) in page.path_label.text()
        assert str(db.path) in page.path_label.text()
        assert "доступно" in page.state_label.text()
        assert page.move_button.isEnabled()
        page.move_button.click()
        assert settings.configured_directory() == target
        assert notices == ["Хранилище перенесено"]
        assert quit_calls == [True]
        window.close()
