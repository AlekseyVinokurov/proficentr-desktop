"""Запуск: python -m proficentr."""

import argparse
import json
import os
from contextlib import ExitStack
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from proficentr.services import open_database, prepare_storage
from proficentr.ui.main_window import MainWindow, SECTIONS


def main() -> int:
    parser = argparse.ArgumentParser(description="Proficentr Desktop")
    parser.add_argument("--smoke-test", action="store_true", help="Проверка запуска и навигации с выходом")
    parser.add_argument("--apply-update", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--update-probe", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--update-ready", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.apply_update:
        from proficentr.updater import apply
        return apply(args.apply_update)
    if args.update_probe:
        from contextlib import closing
        import sqlite3
        import tempfile
        from proficentr.database import Database
        from proficentr.storage import backup_database
        try:
            with tempfile.TemporaryDirectory() as folder:
                copy = Path(folder) / "probe.sqlite3"
                with closing(sqlite3.connect(f"{args.update_probe.resolve().as_uri()}?mode=ro", uri=True)) as source:
                    backup_database(source, copy)
                with Database(copy, create=False):
                    pass
            return 0
        except Exception:
            return 1
    app = QApplication([sys.argv[0]])
    app.setApplicationName("Proficentr Desktop")
    locks = ExitStack()
    try:
        from proficentr.instance import exclusive
        if not args.update_ready:
            # Only briefly test the update lock; the app lock lives until shutdown.
            with exclusive("update"):
                locks.enter_context(exclusive("app"))
        else:
            locks.enter_context(exclusive("app"))
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
            if args.update_ready:
                # This acknowledgement follows migrations and construction of all pages.
                QTimer.singleShot(0, lambda: args.update_ready.write_text(json.dumps({"pid": os.getpid()}), encoding="ascii"))
            if not args.smoke_test:
                window.settings_page.about_page.schedule_check()
            if args.smoke_test:
                for index in range(len(SECTIONS)):
                    window.navigation.setCurrentRow(index)
                    if window.pages.currentIndex() != index:
                        raise RuntimeError("Не работает навигация.")
                window.navigation.setCurrentRow(0)
                # CI fixtures may contain templates: exercise the renderer in the frozen EXE.
                templates = window.settings_page.templates_page.repository
                for version in templates.list_versions():
                    templates.test_document(version.id)
                contracts = window.contracts_page.repository
                for row in contracts.search():
                    if row[-1] != 'draft':
                        contracts.verify_document(row[0])
                orders = window.orders_page.repository
                for row in orders.search():
                    if row[-1] != "draft":
                        orders.verify_document(row[0])
                issued = window.issued_documents_page.repository
                for row in issued.search(state='registered'):
                    issued.verify_document(row[0])
                    for event in window.issued_documents_page.delivery.history(row[0]):
                        for evidence in window.issued_documents_page.delivery.evidence(event.id):
                            window.issued_documents_page.delivery.verify_evidence(evidence[0])
                if issued.search(state='registered'):
                    issued.export_xlsx('1900-01-01', '9999-12-31')
                QTimer.singleShot(200, app.quit)
            return app.exec()
    except Exception as error:
        if args.smoke_test:
            return 1
        QMessageBox.critical(None, "Ошибка запуска", f"Не удалось запустить приложение.\n{error}")
        return 1
    finally:
        locks.close()


if __name__ == "__main__":
    raise SystemExit(main())
