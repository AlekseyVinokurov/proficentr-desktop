"""Формы подготовки обучения; запись в БД только после сохранения."""

from dataclasses import replace
import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)

from proficentr.program_repository import ProgramRepositoryError
from proficentr.programs import DIRECTIONS, KINDS
from proficentr.training_repository import (
    OverlappingTrainingError, ProgramChangeRequired, TrainingRepositoryError,
)
from proficentr.trainings import Training, TrainingGroup

ERRORS = (ValueError, TrainingRepositoryError, ProgramRepositoryError)


def confirm(parent, title, message):
    return QMessageBox.question(parent, title, message,
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                               QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes


def program_combo(repository, current_id=None):
    combo = QComboBox()
    combo.setAccessibleName("Образовательная программа")
    combo.setMinimumContentsLength(20)
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.addItem("Выберите активную программу", None)
    programs = repository.programs.search()
    if current_id is not None and not any(program.id == current_id for program in programs):
        programs.append(repository.programs.load(current_id))
    for program in programs:
        combo.addItem(f"{program.full_name} · {DIRECTIONS[program.direction]} · {KINDS[program.direction][program.kind]} · {program.hours} ч · {program.code} · ред. {program.edition}" + (" · архивная" if not program.active else ""), program.id)
    combo.setCurrentIndex(max(0, combo.findData(current_id)))
    return combo


class DraftDialog(QDialog):
    def is_dirty(self):
        return self._state() != self._initial_state

    def reject(self):
        if self.is_dirty():
            answer = QMessageBox.question(self, "Несохранённые изменения", "Сохранить изменения?",
                                          QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                          QMessageBox.StandardButton.Save)
            if answer == QMessageBox.StandardButton.Save:
                self.save()
                return
            if answer != QMessageBox.StandardButton.Discard:
                return
        super().reject()

    def add_buttons(self, layout):
        buttons = QHBoxLayout()
        self.save_button = QPushButton("Сохранить")
        self.save_button.clicked.connect(self.save)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(self.save_button)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)


class GroupDialog(DraftDialog):
    def __init__(self, repository, group=None, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.original = group or TrainingGroup()
        self.saved_group = None
        self.setWindowTitle("Учебная группа")
        self.resize(680, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Внутренний ID: {self.original.id or 'новая группа'}"))
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.program = program_combo(repository, self.original.program_id)
        form.addRow("Программа *", self.program)
        self.fields = {}
        for field, caption in (("name", "Обозначение *"), ("start_date", "Плановое начало *"), ("end_date", "Плановое окончание *"), ("note", "Примечание")):
            edit = QPlainTextEdit() if field == "note" else QLineEdit()
            edit.setAccessibleName(caption)
            if field == "note":
                edit.setPlainText(getattr(self.original, field))
                edit.setMaximumHeight(90)
            else:
                edit.setText(getattr(self.original, field))
                if field.endswith("date"):
                    edit.setPlaceholderText("ГГГГ-ММ-ДД")
            self.fields[field] = edit
            form.addRow(caption, edit)
        layout.addLayout(form)
        note = QLabel("Даты группы используются по умолчанию при добавлении слушателей. Изменение дат группы сохраняет даты уже созданных обучений.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.add_buttons(layout)
        self._initial_state = self._state()

    def _state(self):
        return {field: edit.toPlainText() if field == "note" else edit.text() for field, edit in self.fields.items()} | {"program_id": self.program.currentData()}

    def save(self):
        try:
            group = replace(self.original, **self._state())
            change_program = allow_overlap = False
            while True:
                try:
                    saved = self.repository.save_group(group, change_program=change_program, allow_overlap=allow_overlap)
                    break
                except ProgramChangeRequired as error:
                    if not confirm(self, "Изменение программы группы", str(error)):
                        return False
                    change_program = True
                except OverlappingTrainingError as error:
                    if not confirm(self, "Пересечение дат", str(error)):
                        return False
                    allow_overlap = True
        except ERRORS as error:
            QMessageBox.warning(self, "Группа не сохранена", str(error))
            return False
        self.saved_group = saved
        self.accept()
        return True


class StudentPicker(QDialog):
    def __init__(self, repository, *, multiple=False, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.student_ids = []
        self.setWindowTitle("Выбор существующих слушателей")
        self.resize(650, 460)
        layout = QVBoxLayout(self)
        hint = QLabel("Введите часть ФИО. Показаны первые 100 совпадений; уточните поиск. Если слушателя нет, создайте карточку в разделе «Слушатели».")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Поиск по ФИО")
        self.search.setAccessibleName("Поиск слушателей")
        layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection if multiple else QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.list)
        if multiple:
            layout.addWidget(QLabel("Для выбора нескольких строк используйте Ctrl или Shift."))
        self.state = QLabel()
        layout.addWidget(self.state)
        buttons = QHBoxLayout()
        self.select_button = QPushButton("Выбрать")
        self.select_button.clicked.connect(self.select)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(self.select_button)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.timer.start())
        self.list.itemDoubleClicked.connect(lambda: self.select())

    def refresh(self):
        self.list.clear()
        try:
            rows = self.repository.search_students(self.search.text())
            for student_id, name, born in rows:
                item = QListWidgetItem(f"{name} · {born} · ID {student_id}")
                item.setData(Qt.ItemDataRole.UserRole, student_id)
                self.list.addItem(item)
            self.state.setText(f"Найдено: {len(rows)}" if rows else "Нет результатов. Уточните ФИО или создайте карточку в разделе «Слушатели».")
        except TrainingRepositoryError as error:
            self.state.setText(str(error))

    def select(self):
        self.student_ids = [item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems()]
        if self.student_ids:
            self.accept()


class TrainingDialog(DraftDialog):
    def __init__(self, repository, training=None, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.original = training or Training()
        self.saved_training = None
        self.student_id = self.original.student_id
        self.setWindowTitle("Обучение — Подготовка")
        self.resize(680, 480)
        layout = QVBoxLayout(self)
        hint = QLabel(f"ID: {self.original.id or 'новая запись'} · Создано: {self.original.created_at or 'при сохранении'}\nПодготовка записи не означает юридического зачисления.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.student_label = QLabel()
        self.choose_student = QPushButton("Выбрать слушателя")
        self.choose_student.clicked.connect(self.pick_student)
        self.choose_student.setEnabled(self.original.id is None)
        form.addRow("Слушатель *", self.student_label)
        form.addRow(self.choose_student)
        self.program = program_combo(repository, self.original.program_id)
        self.program.setEnabled(self.original.group_id is None)
        form.addRow("Программа *", self.program)
        self.fields = {}
        for field, caption in (("start_date", "Плановое начало *"), ("end_date", "Плановое окончание *"), ("note", "Примечание")):
            edit = QLineEdit(getattr(self.original, field))
            edit.setAccessibleName(caption)
            if field.endswith("date"):
                edit.setPlaceholderText("ГГГГ-ММ-ДД")
            self.fields[field] = edit
            form.addRow(caption, edit)
        self.refresh_snapshot = QCheckBox("Явно обновить снимок программы из справочника")
        form.addRow(self.refresh_snapshot)
        self.snapshot_label = QLabel()
        self.snapshot_label.setWordWrap(True)
        if self.original.program_snapshot:
            snapshot = json.loads(self.original.program_snapshot)
            self.snapshot_label.setText(f"Сохранённый снимок: {snapshot['full_name']} · {DIRECTIONS[snapshot['direction']]} · {KINDS[snapshot['direction']][snapshot['kind']]} · {snapshot['hours']} ч · код {snapshot['code']} · ред. {snapshot['edition']} · {snapshot['qualification']} · {snapshot['grade']}")
        form.addRow(self.snapshot_label)
        layout.addLayout(form)
        self.update_student_label()
        self.add_buttons(layout)
        self._initial_state = self._state()

    def update_student_label(self):
        if self.student_id:
            # Resolve one compact row by ID without loading identity documents.
            self.student_label.setText(self.repository.student_label(self.student_id))
        else:
            self.student_label.setText("Не выбран")

    def pick_student(self):
        picker = StudentPicker(self.repository, parent=self)
        if picker.exec() == QDialog.DialogCode.Accepted:
            self.student_id = picker.student_ids[0]
            self.update_student_label()

    def _state(self):
        return {key: edit.text() for key, edit in self.fields.items()} | {
            "student_id": self.student_id, "program_id": self.program.currentData(),
            "refresh_snapshot": self.refresh_snapshot.isChecked(),
        }

    def save(self):
        state = self._state()
        refresh = state.pop("refresh_snapshot")
        training = replace(self.original, **state)
        if self.original.id and (refresh or training.program_id != self.original.program_id):
            if not confirm(self, "Снимок программы", "Обновить сохранённый снимок программы для этой подготовительной записи?"):
                return False
            refresh = True
        try:
            try:
                saved = self.repository.save(training, refresh_snapshot=refresh)
            except OverlappingTrainingError as error:
                if not confirm(self, "Пересечение дат", str(error)):
                    return False
                saved = self.repository.save(training, refresh_snapshot=refresh, allow_overlap=True)
        except ERRORS as error:
            QMessageBox.warning(self, "Обучение не сохранено", str(error))
            return False
        self.saved_training = saved
        self.accept()
        return True
