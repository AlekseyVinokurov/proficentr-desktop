"""Запуск: python -m proficentr."""

import argparse
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from proficentr.services import open_database
from proficentr.ui.main_window import MainWindow, SECTIONS


def main() -> int:
    parser = argparse.ArgumentParser(description="Proficentr Desktop")
    parser.add_argument("--smoke-test", action="store_true", help="Проверка запуска и навигации с выходом")
    args = parser.parse_args()
    app = QApplication([sys.argv[0]])
    app.setApplicationName("Proficentr Desktop")
    try:
        with open_database() as database:
            window = MainWindow(database.path)
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
