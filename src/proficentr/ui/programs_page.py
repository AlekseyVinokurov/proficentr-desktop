"""Реестр программ с фильтрами и архивированием без удаления."""

from PySide6.QtCore import Qt, QSignalBlocker, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHeaderView, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from proficentr.programs import DIRECTIONS, KINDS
from proficentr.program_repository import ProgramRepositoryError
from proficentr.ui.program_dialog import ProgramDialog


class ProgramsPage(QWidget):
    def __init__(self, repository):
        super().__init__()
        self.repository = repository
        layout = QVBoxLayout(self)
        title = QLabel("Программы")
        font = title.font()
        font.setPointSize(20)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        self.search = QLineEdit()
        self.search.setAccessibleName("Поиск программ")
        self.search.setPlaceholderText("Поиск по наименованию или внутреннему коду")
        layout.addWidget(self.search)
        filters = QHBoxLayout()
        self.direction_filter = QComboBox()
        self.direction_filter.setAccessibleName("Фильтр направления программ")
        self.direction_filter.addItem("Все", None)
        for key, text in DIRECTIONS.items():
            self.direction_filter.addItem(text, key)
        self.active_filter = QComboBox()
        self.active_filter.setAccessibleName("Фильтр активности программ")
        for text, value in (("Активные", True), ("Архивные", False), ("Все", None)):
            self.active_filter.addItem(text, value)
        filters.addWidget(QLabel("Направление:"))
        filters.addWidget(self.direction_filter)
        filters.addWidget(QLabel("Статус:"))
        filters.addWidget(self.active_filter)
        filters.addStretch()
        layout.addLayout(filters)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("Добавить программу")
        self.add_button.clicked.connect(lambda: self.open_card())
        self.open_button = QPushButton("Открыть / изменить")
        self.open_button.clicked.connect(self.open_selected)
        self.archive_button = QPushButton("В архив")
        self.archive_button.clicked.connect(self.toggle_active)
        for button in (self.add_button, self.open_button, self.archive_button):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(("Наименование", "Направление", "Вид программы", "Часы", "Статус"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.cellDoubleClicked.connect(lambda row, column: self.open_selected())
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table)
        self.state = QLabel()
        layout.addWidget(self.state)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.timer.start())
        self.direction_filter.currentIndexChanged.connect(lambda: self.refresh())
        self.active_filter.currentIndexChanged.connect(lambda: self.refresh())
        self.setEnabled(repository is not None)
        self.refresh()

    def _selection_changed(self):
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        self.open_button.setEnabled(item is not None)
        self.archive_button.setEnabled(item is not None)
        self.archive_button.setText("Вернуть в активные" if item and not item.data(Qt.ItemDataRole.UserRole + 1) else "В архив")

    def refresh(self, select_id=None):
        try:
            rows = self.repository.search(self.search.text(), direction=self.direction_filter.currentData(),
                                          active=self.active_filter.currentData()) if self.repository else []
        except ProgramRepositoryError as error:
            self.table.setRowCount(0)
            self._selection_changed()
            self.state.setText(str(error))
            return
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        for row, program in enumerate(rows):
            texts = (program.full_name, DIRECTIONS[program.direction], KINDS[program.direction][program.kind],
                     str(program.hours), "Активная" if program.active else "Архивная")
            for column, text in enumerate(texts):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                item.setData(Qt.ItemDataRole.UserRole, program.id)
                item.setData(Qt.ItemDataRole.UserRole + 1, program.active)
                self.table.setItem(row, column, item)
            if program.id == select_id:
                self.table.selectRow(row)
        self._selection_changed()
        self.state.setText(f"Найдено программ: {len(rows)}")

    def open_selected(self):
        row = self.table.currentRow()
        if row >= 0:
            self.open_card(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))

    def open_card(self, program_id=None):
        try:
            while True:
                program = self.repository.load(program_id) if program_id is not None else None
                dialog = ProgramDialog(self.repository, program, self)
                dialog.exec()
                if dialog.saved_program is not None:
                    saved = dialog.saved_program
                    with QSignalBlocker(self.direction_filter), QSignalBlocker(self.active_filter):
                        self.direction_filter.setCurrentIndex(0)
                        self.active_filter.setCurrentIndex(self.active_filter.findData(saved.active))
                    self.search.clear()
                    self.timer.stop()
                    self.refresh(saved.id)
                if dialog.open_program_id is None:
                    break
                program_id = dialog.open_program_id
        except ProgramRepositoryError as error:
            QMessageBox.warning(self, "Программа недоступна", str(error))

    def toggle_active(self):
        row = self.table.currentRow()
        if row < 0:
            return
        try:
            program = self.repository.load(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))
            question = "Переместить программу в архив? Карточка и её ID сохранятся." if program.active else "Вернуть программу в активный список?"
            if QMessageBox.question(self, "Статус программы", question,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            self.repository.set_active(program.id, not program.active)
            self.refresh(program.id)
        except ProgramRepositoryError as error:
            QMessageBox.warning(self, "Статус не изменён", str(error))
