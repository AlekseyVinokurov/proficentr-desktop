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
    assert (tmp_path / "proficentr.sqlite3").is_file()
