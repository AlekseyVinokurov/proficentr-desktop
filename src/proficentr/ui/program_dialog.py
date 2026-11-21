"""Карточка программы: совместимые виды ДПО/ПО и отложенное сохранение."""

from dataclasses import asdict

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from proficentr.programs import DIRECTIONS, KINDS, Program, hours_from_text
from proficentr.program_repository import DuplicateProgramError, ProgramRepositoryError

GROUPS = (
    ("Основные сведения", (("full_name", "Полное наименование *"), ("short_name", "Сокращённое наименование"),
                          ("code", "Внутренний код"), ("hours", "Академические часы *"))),
    ("Квалификация и редакция", (("qualification", "Квалификация / профессия / должность (если применимо)"),
                                ("grade", "Разряд / категория (если применимо)"), ("edition", "Редакция"))),
    ("Утверждение программы", (("approval_date", "Дата утверждения"), ("approval_number", "Номер утверждения"),
                              ("approval_details", "Другие сведения об утверждении"))),
    ("Примечание и статус", (("note", "Примечание"),)),
)


class DuplicateDialog(QDialog):
    def __init__(self, matches, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Проверка совпадающих программ")
        self.resize(700, 360)
        self.matches = matches
        self.open_program_id = None
        self.create_separate = False
        layout = QVBoxLayout(self)
        note = QLabel("Совпало наименование или внутренний код. Проверьте вид, часы и редакцию: разные программы с одинаковым названием допустимы. Автоматического объединения нет.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.list = QListWidget()
        for item in matches:
            status = "Активная" if item.active else "Архивная"
            self.list.addItem(f"№ {item.id} · {item.full_name} · {DIRECTIONS[item.direction]} · {KINDS[item.direction][item.kind]} · {item.hours} ч · редакция {item.edition or 'не указана'} · код {item.code or 'не указан'} · {status}")
        self.list.setCurrentRow(0)
        layout.addWidget(self.list)
        hint = QLabel("Открытие существующей карточки отменит текущие несохранённые изменения.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        for label, action in (("Открыть выбранную", self.open_selected), ("Сохранить отдельную запись", self.confirm_separate), ("Отмена", self.reject)):
            button = QPushButton(label)
            button.clicked.connect(action)
            buttons.addWidget(button)
        layout.addLayout(buttons)

    def open_selected(self):
        index = self.list.currentRow()
        if index >= 0:
            self.open_program_id = self.matches[index].id
            self.accept()

    def confirm_separate(self):
        self.create_separate = True
        self.accept()


class ProgramDialog(QDialog):
    def __init__(self, repository, program=None, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.original = program or Program()
        self.saved_program = None
        self.open_program_id = None
        self.setWindowTitle("Карточка программы" if self.original.id else "Добавить программу")
        self.resize(720, 660)
        self.setMinimumSize(420, 350)
        layout = QVBoxLayout(self)
        heading = QLabel(f"Внутренний ID: {self.original.id}" if self.original.id else "Новая программа. * — обязательные поля. Квалификация и разряд заполняются только при применимости.")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.direction = QComboBox()
        self.direction.setAccessibleName("Направление программы")
        self.direction.addItem("Выберите направление", "")
        for key, text in DIRECTIONS.items():
            self.direction.addItem(text, key)
        self.direction.setCurrentIndex(self.direction.findData(self.original.direction))
        self.kind = QComboBox()
        self.kind.setAccessibleName("Вид программы")
        self.kind.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.kind.setMinimumContentsLength(18)
        self._populate_kinds(self.original.kind)
        self.direction.currentIndexChanged.connect(lambda: self._populate_kinds())
        self.active = QComboBox()
        self.active.setAccessibleName("Статус программы")
        self.active.addItem("Активная", True)
        self.active.addItem("Архивная", False)
        self.active.setCurrentIndex(self.active.findData(self.original.active))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        content = QVBoxLayout(body)
        self.fields = {}
        for index, (title, fields) in enumerate(GROUPS):
            group = QGroupBox(title)
            form = QFormLayout(group)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            if index == 0:
                form.addRow("Направление *", self.direction)
                form.addRow("Вид программы *", self.kind)
            for field, caption in fields:
                edit = QPlainTextEdit() if field == "note" else QLineEdit()
                edit.setAccessibleName(caption)
                value = getattr(self.original, field)
                if field == "note":
                    edit.setMaximumHeight(90)
                    edit.setPlainText(value)
                else:
                    edit.setText(str(value) if field != "hours" or value else "")
                    if field == "approval_date":
                        edit.setPlaceholderText("ГГГГ-ММ-ДД")
                    elif field == "hours":
                        edit.setPlaceholderText("Положительное целое число")
                self.fields[field] = edit
                form.addRow(caption, edit)
            if index == len(GROUPS) - 1:
                form.addRow("Статус", self.active)
            content.addWidget(group)
        scroll.setWidget(body)
        layout.addWidget(scroll)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("Сохранить")
        self.save_button.clicked.connect(self.save)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(self.save_button)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self._initial_state = self._state()

    def _populate_kinds(self, selected=""):
        with QSignalBlocker(self.kind):
            self.kind.clear()
            self.kind.addItem("Выберите вид программы", "")
            for key, text in KINDS.get(self.direction.currentData(), {}).items():
                self.kind.addItem(text, key)
            self.kind.setCurrentIndex(max(0, self.kind.findData(selected)))
        self.kind.setEnabled(bool(self.direction.currentData()))

    def _state(self):
        return {field: (edit.toPlainText() if field == "note" else edit.text()) for field, edit in self.fields.items()} | {
            "direction": self.direction.currentData(), "kind": self.kind.currentData(), "active": self.active.currentData(),
        }

    def values(self):
        values = self._state()
        values["hours"] = hours_from_text(values["hours"])
        return Program(**(asdict(self.original) | values))

    def is_dirty(self):
        return self._state() != self._initial_state

    def save(self):
        try:
            program = self.values()
            try:
                saved = self.repository.save(program)
            except DuplicateProgramError as error:
                dialog = DuplicateDialog(error.matches, self)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return False
                if dialog.open_program_id is not None:
                    self.open_program_id = dialog.open_program_id
                    super().reject()
                    return False
                if not dialog.create_separate:
                    return False
                saved = self.repository.save(program, allow_duplicates=True)
        except (ValueError, ProgramRepositoryError) as error:
            QMessageBox.warning(self, "Программа не сохранена", str(error))
            return False
        self.saved_program = saved
        self.accept()
        return True

    def reject(self):
        if self.is_dirty():
            answer = QMessageBox.question(self, "Несохранённые изменения", "Сохранить изменения программы?",
                                          QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                          QMessageBox.StandardButton.Save)
            if answer == QMessageBox.StandardButton.Save:
                self.save()
                return
            if answer != QMessageBox.StandardButton.Discard:
                return
        super().reject()
