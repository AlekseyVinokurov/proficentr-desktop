"""Запуск: python -m proficentr."""

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from proficentr.services import open_database, prepare_storage
from proficentr.ui.main_window import MainWindow, SECTIONS


def main() -> int:
    parser = argparse.ArgumentParser(description="Proficentr Desktop")
    parser.add_argument("--smoke-test", action="store_true", help="Проверка запуска и навигации с выходом")
    args = parser.parse_args()
    app = QApplication([sys.argv[0]])
    app.setApplicationName("Proficentr Desktop")
    try:
        storage = prepare_storage()
        if storage is None:
            if args.smoke_test:
                raise ValueError("Для smoke-test задайте PROFICENTR_DATA_DIR.")
            selected = QFileDialog.getExistingDirectory(None, "Выберите пустую локальную папку для данных Proficentr")
            if not selected:
                return 0
            storage = prepare_storage(Path(selected))
        with open_database(storage) as database:
            window = MainWindow(database.path, storage, database)
            window.show()
            if args.smoke_test:
                for index in range(len(SECTIONS)):
                    window.navigation.setCurrentRow(index)
                    if window.pages.currentIndex() != index:
                        raise RuntimeError("Не работает навигация.")
                window.navigation.setCurrentRow(0)
                QTimer.singleShot(200, app.quit)
            return app.exec()
    except Exception as error:
        if args.smoke_test:
            return 1
        QMessageBox.critical(None, "Ошибка запуска", f"Не удалось запустить приложение.\n{error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
