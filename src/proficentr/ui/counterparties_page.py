"""Реестр организаций-заказчиков и открытие карточек."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from proficentr.counterparties import KINDS
from proficentr.counterparty_repository import CounterpartyRepositoryError
from proficentr.ui.counterparty_dialog import CounterpartyDialog


class CounterpartiesPage(QWidget):
    def __init__(self, repository):
        super().__init__()
        self.repository = repository
        layout = QVBoxLayout(self)
        title = QLabel("Организации")
        font = title.font()
        font.setPointSize(20)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        self.search = QLineEdit()
        self.search.setAccessibleName("Поиск организаций")
        self.search.setPlaceholderText("Поиск по наименованию, ИНН или телефону")
        layout.addWidget(self.search)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("Добавить организацию")
        self.add_button.clicked.connect(lambda: self.open_card())
        self.open_button = QPushButton("Открыть / изменить")
        self.open_button.clicked.connect(self.open_selected)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.open_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(("Наименование", "ИНН", "Тип контрагента", "Телефон", "Контактное лицо"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.cellDoubleClicked.connect(lambda row, column: self.open_selected())
        self.table.itemSelectionChanged.connect(lambda: self.open_button.setEnabled(self.table.currentRow() >= 0))
        layout.addWidget(self.table)
        self.state = QLabel()
        layout.addWidget(self.state)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.timer.start())
        self.setEnabled(repository is not None)
        self.refresh()

    def refresh(self, select_id=None):
        try:
            rows = self.repository.search(self.search.text()) if self.repository else []
        except CounterpartyRepositoryError as error:
            self.state.setText(str(error))
            self.table.setRowCount(0)
            return
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        self.open_button.setEnabled(False)
        for row, counterparty in enumerate(rows):
            for column, text in enumerate((counterparty.full_name, counterparty.inn, KINDS[counterparty.kind], counterparty.phone, counterparty.contact_name)):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, counterparty.id)
                self.table.setItem(row, column, item)
            if counterparty.id == select_id:
                self.table.selectRow(row)
        self.state.setText(f"Найдено организаций: {len(rows)}")

    def open_selected(self):
        row = self.table.currentRow()
        if row >= 0:
            self.open_card(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))

    def open_card(self, counterparty_id=None):
        try:
            while True:
                counterparty = self.repository.load(counterparty_id) if counterparty_id is not None else None
                dialog = CounterpartyDialog(self.repository, counterparty, self)
                dialog.exec()
                if dialog.saved_counterparty is not None:
                    # Новая карточка видна даже если не подходит под прежний фильтр.
                    self.search.clear()
                    self.timer.stop()
                    self.refresh(dialog.saved_counterparty.id)
                if dialog.open_counterparty_id is None:
                    break
                counterparty_id = dialog.open_counterparty_id
        except CounterpartyRepositoryError as error:
            QMessageBox.warning(self, "Карточка недоступна", str(error))
