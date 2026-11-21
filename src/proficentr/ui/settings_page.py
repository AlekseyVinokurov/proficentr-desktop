"""Настройки локального хранилища; файловые операции выполняет storage."""

import os
import sqlite3

from PySide6.QtWidgets import (
    QApplication, QFileDialog, QLabel, QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)
from PySide6.QtCore import Qt, QSignalBlocker

from proficentr.storage import relocate_storage
from proficentr.organization import TrainingCenterRepository
from proficentr.ui.training_center_page import TrainingCenterPage
from proficentr.ui.about_page import AboutPage
from proficentr.document_templates import TemplateRepository
from proficentr.ui.templates_page import TemplatesPage
from proficentr.frdo_templates import FrdoTemplateRepository
from proficentr.ui.frdo_page import FrdoPage


class SettingsPage(QWidget):
    def __init__(self, database_path, storage, database):
        super().__init__()
        self.storage = storage
        self.database = database
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        title = QLabel("Настройки")
        font = title.font()
        font.setPointSize(20)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        storage_page = QWidget()
        self.tabs.addTab(storage_page, "Хранилище")
        layout = QVBoxLayout(storage_page)
        self.path_label = QLabel(f"Хранилище: {storage.root}\nSQLite: {database_path}")
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path_label)
        self.state_label = QLabel()
        self.refresh_status()
        layout.addWidget(self.state_label)
        self.move_button = QPushButton("Выбрать папку и безопасно перенести данные…")
        self.move_button.setEnabled(database is not None and "PROFICENTR_DATA_DIR" not in os.environ)
        self.move_button.clicked.connect(self.choose_directory)
        layout.addWidget(self.move_button)
        if "PROFICENTR_DATA_DIR" in os.environ:
            layout.addWidget(QLabel("Путь задан PROFICENTR_DATA_DIR. Для выбора через интерфейс уберите переменную и перезапустите приложение."))
        note = QLabel("Выберите пустой каталог на локальном диске. После переноса потребуется перезапуск. Старая копия сохраняется. Перед переносом закройте другие экземпляры приложения.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        repository = TrainingCenterRepository(database.connection, storage) if database else None
        self.center_page = TrainingCenterPage(repository)
        self.tabs.addTab(self.center_page, "Учебный центр")
        self.about_page = AboutPage(storage)
        self.tabs.addTab(self.about_page, "О программе")
        self.templates_page = TemplatesPage(TemplateRepository(database.connection, storage) if database else None)
        self.tabs.addTab(self.templates_page, "Шаблоны документов")
        self.frdo_page = FrdoPage(FrdoTemplateRepository(database.connection, storage) if database else None)
        self.tabs.addTab(self.frdo_page, "ФИС ФРДО")
        self._tab_index = 0
        self.tabs.currentChanged.connect(self._change_tab)

    def confirm_leave(self):
        return self.center_page.confirm_leave()

    def _change_tab(self, index):
        if self._tab_index == 1 and index != 1 and not self.confirm_leave():
            with QSignalBlocker(self.tabs):
                self.tabs.setCurrentIndex(1)
            return
        self._tab_index = index

    def refresh_status(self):
        try:
            self.storage.require_available()
            text = "Состояние: хранилище доступно для записи."
        except (OSError, ValueError) as error:
            text = f"Состояние: хранилище недоступно. {error}"
        self.state_label.setText(text)
        self.state_label.setWordWrap(True)

    def choose_directory(self):
        from pathlib import Path

        if not self.confirm_leave():
            return
        selected = QFileDialog.getExistingDirectory(self, "Новая папка хранилища")
        if not selected:
            return
        answer = QMessageBox.question(
            self, "Перенос хранилища",
            "Перенести текущую базу и электронный архив в выбранную папку? "
            "Исходные данные сохранятся. После успешного переноса приложение закроется; запустите его снова.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            relocate_storage(self.storage, self.database.connection, Path(selected))
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            QMessageBox.critical(self, "Перенос не выполнен", f"Прежнее хранилище сохранено.\n{error}")
            self.refresh_status()
            return
        QMessageBox.information(self, "Хранилище перенесено", "Копия проверена, новый путь сохранён. Запустите приложение снова.")
        QApplication.instance().quit()
