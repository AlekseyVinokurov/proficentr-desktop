"""Реестр слушателей и открытие карточек."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from proficentr.student_repository import StudentRepositoryError
from proficentr.ui.student_dialog import StudentDialog


class StudentsPage(QWidget):
    def __init__(self, repository):
        super().__init__()
        self.repository = repository
        layout = QVBoxLayout(self)
        title = QLabel("Слушатели")
        font = title.font()
        font.setPointSize(20)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        self.search = QLineEdit()
        self.search.setAccessibleName("Поиск слушателей")
        self.search.setPlaceholderText("Поиск по ФИО, телефону или СНИЛС")
        layout.addWidget(self.search)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("Добавить слушателя")
        self.add_button.clicked.connect(lambda: self.open_card())
        self.open_button = QPushButton("Открыть / изменить")
        self.open_button.clicked.connect(self.open_selected)
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.open_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("ФИО", "Дата рождения", "Телефон", "Email"))
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
        except StudentRepositoryError as error:
            self.state.setText(str(error))
            self.table.setRowCount(0)
            return
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        self.open_button.setEnabled(False)
        for row, student in enumerate(rows):
            for column, text in enumerate((student.full_name, student.birth_date, student.phone, student.email)):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, student.id)
                self.table.setItem(row, column, item)
            if student.id == select_id:
                self.table.selectRow(row)
        self.state.setText(f"Найдено слушателей: {len(rows)}")

    def open_selected(self):
        row = self.table.currentRow()
        if row >= 0:
            self.open_card(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole))

    def open_card(self, student_id=None):
        try:
            while True:
                student = self.repository.load(student_id) if student_id is not None else None
                dialog = StudentDialog(self.repository, student, self)
                dialog.exec()
                if dialog.saved_student is not None:
                    # Новая карточка видна даже если не подходит под прежний фильтр.
                    self.search.clear()
                    self.timer.stop()
                    self.refresh(dialog.saved_student.id)
                if dialog.open_student_id is None:
                    break
                student_id = dialog.open_student_id
        except StudentRepositoryError as error:
            QMessageBox.warning(self, "Карточка недоступна", str(error))
