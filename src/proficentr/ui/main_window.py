"""Главное окно и навигация P01."""

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QMainWindow, QStackedWidget,
    QVBoxLayout, QWidget,
)
from proficentr.storage import Storage
from proficentr.ui.settings_page import SettingsPage


SECTIONS = (
    "Главная", "Слушатели", "Организации", "Программы", "Группы",
    "Документы", "ФИС ФРДО", "Настройки",
)


class MainWindow(QMainWindow):
    def __init__(self, database_path: Path, storage: Storage | None = None, database=None):
        super().__init__()
        self.setWindowTitle("Proficentr Desktop")
        self.resize(1080, 700)
        self.setMinimumSize(760, 480)
        container = QWidget()
        layout = QHBoxLayout(container)
        self.navigation = QListWidget()
        self.navigation.setAccessibleName("Разделы приложения")
        self.navigation.addItems(SECTIONS)
        self.navigation.setFixedWidth(190)
        self.pages = QStackedWidget()
        for section in SECTIONS:
            if section == "Настройки":
                root = database_path.parent.parent if database_path.parent.name == "database" else database_path.parent
                self.settings_page = SettingsPage(database_path, storage or Storage(root), database)
                self.pages.addWidget(self.settings_page)
                continue
            page = QWidget()
            content = QVBoxLayout(page)
            content.setContentsMargins(24, 20, 24, 20)
            title = QLabel(section)
            font = title.font()
            font.setPointSize(20)
            font.setBold(True)
            title.setFont(font)
            content.addWidget(title)
            if section == "Главная":
                description = "Добро пожаловать в Proficentr Desktop.\nВыберите раздел в меню слева."
            else:
                description = "Раздел подготовлен. Функции будут добавлены на следующих этапах."
            label = QLabel(description)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            content.addWidget(label)
            content.addStretch()
            self.pages.addWidget(page)
        layout.addWidget(self.navigation)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(container)
        self.navigation.currentRowChanged.connect(self._navigate)
        self.navigation.setCurrentRow(0)

    def _navigate(self, index: int) -> None:
        if 0 <= index < len(SECTIONS):
            self.pages.setCurrentIndex(index)
            self.statusBar().showMessage(f"Готово • {SECTIONS[index]}")
