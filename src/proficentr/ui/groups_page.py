"""Раздел «Группы»: реестр групп, состав и индивидуальная подготовка."""

import json

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QHeaderView, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from proficentr.training_repository import OverlappingTrainingError, TrainingRepositoryError
from proficentr.trainings import STATES, PREPARATION
from proficentr.attestations import RESULTS
from proficentr.ui.training_dialogs import ERRORS, GroupDialog, StudentPicker, TrainingDialog, confirm


def make_table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.verticalHeader().hide()
    return table


def fill_table(table, rows):
    table.setRowCount(0)
    table.setRowCount(len(rows))
    for row, values in enumerate(rows):
        for column, value in enumerate(values[1:]):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            item.setData(Qt.ItemDataRole.UserRole, values[0])
            table.setItem(row, column, item)


def selected_id(table):
    row = table.currentRow()
    item = table.item(row, 0) if row >= 0 else None
    return item.data(Qt.ItemDataRole.UserRole) if item else None


class TrainingList(QWidget):
    contract_requested = Signal(object, object)
    order_requested = Signal(object, object)
    attestation_requested = Signal(object, object)
    completion_requested = Signal(object, object)
    issuance_requested = Signal(object, object)

    def __init__(self, repository, *, group_id=None, attestations=None, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.attestations = attestations
        self.group_id = group_id
        layout = QVBoxLayout(self)
        hint = QLabel("Состояние «Подготовка» не означает юридического зачисления. Для отсутствующего слушателя сначала создайте карточку в разделе «Слушатели».")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.search = QLineEdit()
        self.search.setAccessibleName("Поиск обучений по ФИО")
        self.search.setPlaceholderText("Поиск по ФИО")
        layout.addWidget(self.search)
        self.show_excluded = QCheckBox("Показать исключённые подготовительные записи")
        self.show_excluded.setVisible(group_id is not None)
        layout.addWidget(self.show_excluded)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("Добавить слушателей" if group_id is not None else "Добавить индивидуальное обучение")
        self.add_button.clicked.connect(self.add)
        self.open_button = QPushButton("Открыть / изменить")
        self.open_button.clicked.connect(self.open_selected)
        self.exclude_button = QPushButton("Исключить из состава")
        self.exclude_button.setVisible(group_id is not None)
        self.exclude_button.clicked.connect(self.exclude)
        self.contract_button = QPushButton("Создать договор")
        self.contract_button.clicked.connect(lambda: self.contract_requested.emit(
            [selected_id(self.table)] if selected_id(self.table) else [], self.group_id))
        for button in (self.add_button, self.open_button, self.exclude_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        documents = QHBoxLayout()
        self.order_button = QPushButton("Создать приказ")
        self.order_button.clicked.connect(lambda: self.order_requested.emit([selected_id(self.table)] if selected_id(self.table) else [], self.group_id))
        self.attestation_button = QPushButton("Аттестация")
        self.attestation_button.clicked.connect(lambda: self.attestation_requested.emit([selected_id(self.table)] if selected_id(self.table) else [], self.group_id))
        self.completion_button = QPushButton("Завершение обучения")
        self.completion_button.clicked.connect(lambda: self.completion_requested.emit([selected_id(self.table)] if selected_id(self.table) else [], self.group_id))
        self.issued_button = QPushButton("Выпускные документы")
        self.issued_button.clicked.connect(lambda: self.issuance_requested.emit([selected_id(self.table)] if selected_id(self.table) else [], self.group_id))
        documents.addWidget(self.contract_button); documents.addWidget(self.order_button); documents.addWidget(self.attestation_button); documents.addWidget(self.completion_button); documents.addWidget(self.issued_button)
        layout.addLayout(documents)
        self.table = make_table(("ФИО", "Программа (снимок)", "Начало", "Окончание", "Состояние", "Подтверждённая аттестация", "Основание"))
        self.table.cellDoubleClicked.connect(lambda: self.open_selected())
        self.table.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.table)
        self.state = QLabel()
        layout.addWidget(self.state)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.timer.start())
        self.show_excluded.toggled.connect(lambda: self.refresh())
        self.refresh()

    def selection_changed(self):
        training_id = selected_id(self.table)
        self.open_button.setEnabled(training_id is not None)
        self.attestation_button.setEnabled(bool(training_id and self.table.item(self.table.currentRow(),4).text() == STATES["enrolled"]))
        self.issued_button.setEnabled(bool(training_id and self.table.item(self.table.currentRow(),4).text() == STATES["completed"]))
        self.completion_button.setEnabled(bool(training_id and self.table.item(self.table.currentRow(),4).text() == STATES["enrolled"]))
        item = self.table.item(self.table.currentRow(), 4) if training_id else None
        self.exclude_button.setEnabled(bool(item and item.text() == STATES[PREPARATION]))

    def refresh(self, select_id=None):
        try:
            rows = self.repository.list_trainings(group_id=self.group_id, query=self.search.text(),
                                                  include_excluded=self.show_excluded.isChecked()) if self.repository else []
            display = []
            for record_id,name,snapshot,start,end,state in rows:
                current = self.attestations.current_confirmed_result(record_id) if self.attestations else None
                display.append((record_id,name,json.loads(snapshot)["full_name"],start,end,STATES[state],
                    RESULTS[current[4]] if current else 'Нет подтверждённого результата',
                    f'{current[2]} · {current[1]}' if current else ''))
            fill_table(self.table, display)
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == select_id:
                    self.table.selectRow(row)
            active = self.group_id is None or self.repository.load_group(self.group_id).active
            self.add_button.setEnabled(active)
            self.selection_changed()
            if not active:
                self.exclude_button.setEnabled(False)
            self.state.setText(f"Найдено обучений: {len(rows)}" + (" · Группа архивная" if not active else ""))
        except TrainingRepositoryError as error:
            fill_table(self.table, [])
            self.selection_changed()
            self.state.setText(str(error))

    def add(self):
        try:
            if self.group_id is None:
                dialog = TrainingDialog(self.repository, parent=self)
                dialog.exec()
                if dialog.saved_training:
                    self.search.clear()
                    self.timer.stop()
                    self.refresh(dialog.saved_training.id)
                return
            picker = StudentPicker(self.repository, multiple=True, parent=self)
            if picker.exec() != QDialog.DialogCode.Accepted:
                return
            try:
                saved = self.repository.add_students(self.group_id, picker.student_ids)
            except OverlappingTrainingError as error:
                if not confirm(self, "Пересечение дат", str(error) + " Подтверждение относится ко всему выбранному составу."):
                    return
                saved = self.repository.add_students(self.group_id, picker.student_ids, allow_overlap=True)
            self.search.clear()
            self.timer.stop()
            self.refresh(saved[-1].id)
        except ERRORS as error:
            QMessageBox.warning(self, "Обучения не сохранены", str(error))

    def open_selected(self):
        training_id = selected_id(self.table)
        if training_id is None:
            return
        try:
            training = self.repository.load(training_id)
            if training.state == "completed":
                QMessageBox.information(self, "Завершён", "Обучение завершено по подтверждённому подписанному приказу. Историческое основание и фактическая дата доступны в разделе «Документы → Приказы о завершении».")
                return
            if training.state == "enrolled":
                QMessageBox.information(self, "Зачислен", "Обучение зачислено на основании подтверждённого подписанного приказа. Программа, снимок, период и группа защищены от обычного изменения. Приказ доступен в разделе «Документы».")
                return
            if training.state != PREPARATION:
                QMessageBox.information(self, "Исключённая запись", "Запись сохранена в истории состава. Для повторного добавления выберите слушателя через «Добавить слушателей».")
                return
            dialog = TrainingDialog(self.repository, training, self)
            dialog.exec()
            self.refresh(training_id)
        except ERRORS as error:
            QMessageBox.warning(self, "Обучение недоступно", str(error))

    def exclude(self):
        training_id = selected_id(self.table)
        if training_id is None:
            return
        if not confirm(self, "Исключение из состава", "Исключить ошибочно добавленного слушателя? Подготовительная запись останется в истории состава; карточка слушателя и другие обучения сохранятся."):
            return
        try:
            self.repository.exclude(training_id)
            self.refresh()
        except ERRORS as error:
            QMessageBox.warning(self, "Состав не изменён", str(error))


class GroupsPage(QWidget):
    contract_requested = Signal(object, object)
    order_requested = Signal(object, object)
    attestation_requested = Signal(object, object)
    completion_requested = Signal(object, object)
    issuance_requested = Signal(object, object)

    def __init__(self, repository, *, attestations=None):
        super().__init__()
        self.repository = repository
        self.attestations = attestations
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        groups = QWidget()
        content = QVBoxLayout(groups)
        self.search = QLineEdit()
        self.search.setAccessibleName("Поиск групп")
        self.search.setPlaceholderText("Поиск по обозначению группы или программе")
        content.addWidget(self.search)
        self.active_filter = QComboBox()
        self.active_filter.setAccessibleName("Состояние групп")
        for text, value in (("Активные", True), ("Архивные", False), ("Все", None)):
            self.active_filter.addItem(text, value)
        content.addWidget(self.active_filter)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("Создать группу")
        self.add_button.clicked.connect(lambda: self.open_card())
        self.open_button = QPushButton("Открыть / изменить")
        self.open_button.clicked.connect(self.open_selected)
        self.members_button = QPushButton("Состав группы")
        self.members_button.clicked.connect(self.open_members)
        self.archive_button = QPushButton("В архив")
        self.archive_button.clicked.connect(self.toggle_active)
        self.contract_button = QPushButton("Создать договор")
        self.contract_button.clicked.connect(lambda: self.contract_requested.emit([], selected_id(self.table)))
        for button in (self.add_button, self.open_button, self.members_button, self.archive_button):
            buttons.addWidget(button)
        content.addLayout(buttons)
        documents = QHBoxLayout()
        self.order_button = QPushButton("Создать приказ")
        self.order_button.clicked.connect(lambda: self.order_requested.emit([], selected_id(self.table)))
        self.attestation_button = QPushButton("Аттестация")
        self.attestation_button.clicked.connect(lambda: self.attestation_requested.emit([], selected_id(self.table)))
        self.completion_button = QPushButton("Завершение обучения")
        self.completion_button.clicked.connect(lambda: self.completion_requested.emit([], selected_id(self.table)))
        self.issued_button = QPushButton("Выпускные документы")
        self.issued_button.clicked.connect(lambda: self.issuance_requested.emit([], selected_id(self.table)))
        documents.addWidget(self.contract_button); documents.addWidget(self.order_button); documents.addWidget(self.attestation_button); documents.addWidget(self.completion_button); documents.addWidget(self.issued_button)
        content.addLayout(documents)
        self.table = make_table(("Группа", "Программа", "Начало", "Окончание", "Состояние"))
        self.table.cellDoubleClicked.connect(lambda: self.open_selected())
        self.table.itemSelectionChanged.connect(self.selection_changed)
        content.addWidget(self.table)
        self.state = QLabel()
        content.addWidget(self.state)
        self.tabs.addTab(groups, "Учебные группы")
        self.individual = TrainingList(repository, attestations=attestations)
        self.individual.contract_requested.connect(self.contract_requested.emit)
        self.individual.order_requested.connect(self.order_requested.emit)
        self.individual.attestation_requested.connect(self.attestation_requested.emit)
        self.individual.completion_requested.connect(self.completion_requested.emit)
        self.individual.issuance_requested.connect(self.issuance_requested.emit)
        self.tabs.addTab(self.individual, "Индивидуальное обучение")
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.timer.start())
        self.active_filter.currentIndexChanged.connect(lambda: self.refresh())
        self.tabs.currentChanged.connect(lambda: self.individual.refresh())
        self.setEnabled(repository is not None)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.individual.refresh()

    def selection_changed(self):
        record_id = selected_id(self.table)
        for button in (self.open_button, self.members_button, self.archive_button, self.contract_button, self.order_button, self.attestation_button, self.completion_button, self.issued_button):
            button.setEnabled(record_id is not None)
        item = self.table.item(self.table.currentRow(), 4) if record_id else None
        self.archive_button.setText("Вернуть в активные" if item and item.text() == "Архивная" else "В архив")

    def refresh(self, select_id=None):
        try:
            rows = self.repository.search_groups(self.search.text(), active=self.active_filter.currentData()) if self.repository else []
            fill_table(self.table, [(*row[:-1], "Активная" if row[-1] else "Архивная") for row in rows])
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == select_id:
                    self.table.selectRow(row)
            self.state.setText(f"Найдено групп: {len(rows)}")
        except TrainingRepositoryError as error:
            fill_table(self.table, [])
            self.state.setText(str(error))
        self.selection_changed()

    def open_selected(self):
        group_id = selected_id(self.table)
        if group_id is not None:
            self.open_card(group_id)

    def open_card(self, group_id=None):
        try:
            group = self.repository.load_group(group_id) if group_id is not None else None
            dialog = GroupDialog(self.repository, group, self)
            dialog.exec()
            if dialog.saved_group:
                self.search.clear()
                self.timer.stop()
                self.active_filter.setCurrentIndex(self.active_filter.findData(dialog.saved_group.active))
                self.refresh(dialog.saved_group.id)
        except ERRORS as error:
            QMessageBox.warning(self, "Группа недоступна", str(error))

    def open_members(self):
        group_id = selected_id(self.table)
        if group_id is None:
            return
        try:
            group = self.repository.load_group(group_id)
            dialog = QDialog(self)
            dialog.setWindowTitle(f"Состав группы: {group.name} · ID {group.id}")
            dialog.resize(960, 580)
            layout = QVBoxLayout(dialog)
            members = TrainingList(self.repository, group_id=group_id, attestations=self.attestations, parent=dialog)
            def create_contract(training_ids, selected_group_id):
                dialog.accept()
                self.contract_requested.emit(training_ids, selected_group_id)
            members.contract_requested.connect(create_contract)
            def create_order(training_ids, selected_group_id):
                dialog.accept()
                self.order_requested.emit(training_ids, selected_group_id)
            members.order_requested.connect(create_order)
            def create_attestation(training_ids, selected_group_id):
                dialog.accept()
                self.attestation_requested.emit(training_ids, selected_group_id)
            members.attestation_requested.connect(create_attestation)
            def create_completion(training_ids, selected_group_id):
                dialog.accept()
                self.completion_requested.emit(training_ids, selected_group_id)
            members.completion_requested.connect(create_completion)
            def create_issued(training_ids, selected_group_id):
                dialog.accept()
                self.issuance_requested.emit(training_ids, selected_group_id)
            members.issuance_requested.connect(create_issued)
            layout.addWidget(members)
            close = QPushButton("Закрыть")
            close.clicked.connect(dialog.accept)
            layout.addWidget(close)
            dialog.exec()
        except ERRORS as error:
            QMessageBox.warning(self, "Группа недоступна", str(error))

    def toggle_active(self):
        group_id = selected_id(self.table)
        if group_id is None:
            return
        try:
            group = self.repository.load_group(group_id)
            if not confirm(self, "Состояние группы", "Переместить группу в архив? Состав и обучения сохранятся." if group.active else "Восстановить группу из архива?"):
                return
            self.repository.set_active(group.id, not group.active)
            self.refresh()
        except ERRORS as error:
            QMessageBox.warning(self, "Состояние не изменено", str(error))
