"""Карточка слушателя: ввод, предупреждение о дублях и отложенное сохранение."""

from dataclasses import asdict

from PySide6.QtWidgets import (
    QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from proficentr.students import Student
from proficentr.student_repository import DuplicateStudentError, StudentRepositoryError

GROUPS = (
    ("Личные сведения", (
        ("surname", "Фамилия *"), ("first_name", "Имя *"), ("patronymic", "Отчество"),
        ("birth_date", "Дата рождения *"), ("snils", "СНИЛС"), ("citizenship", "Гражданство"),
    )),
    ("Контакты", (
        ("phone", "Мобильный телефон"), ("email", "Email"),
        ("registration_address", "Адрес регистрации"), ("residential_address", "Адрес проживания, если отличается"),
    )),
    ("Российский паспорт", (
        ("passport_series", "Серия"), ("passport_number", "Номер"), ("passport_issued_by", "Кем выдан"),
        ("passport_issue_date", "Дата выдачи"), ("passport_department_code", "Код подразделения"),
    )),
    ("Образование и работа", (
        ("education_level", "Уровень образования"), ("education_document", "Документ об образовании"),
        ("education_details", "Реквизиты документа об образовании"),
        ("workplace", "Место работы"), ("job_title", "Должность"), ("note", "Примечание"),
    )),
)


class DuplicateDialog(QDialog):
    def __init__(self, matches, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Проверка совпадающих карточек")
        self.resize(680, 340)
        self.matches = matches
        self.open_student_id = None
        self.create_separate = False
        layout = QVBoxLayout(self)
        note = QLabel("Проверьте существующие карточки. Совпадение СНИЛС — точное совпадение номера; ФИО и дата рождения указывают на возможный дубль. Автоматического объединения нет.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.list = QListWidget()
        for match in matches:
            reason = "Совпадение СНИЛС" if match.reason == "snils" else "Возможный дубль: ФИО и дата рождения"
            self.list.addItem(f"№ {match.student.id} · {match.student.full_name} · {match.student.birth_date} · {reason}")
        self.list.setCurrentRow(0)
        layout.addWidget(self.list)
        hint = QLabel("Открытие существующей карточки отменит текущие несохранённые изменения.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        open_button = QPushButton("Открыть выбранную")
        open_button.clicked.connect(self.open_selected)
        separate = QPushButton("Сохранить отдельную запись")
        separate.clicked.connect(self.confirm_separate)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(open_button)
        buttons.addWidget(separate)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def open_selected(self):
        index = self.list.currentRow()
        if index >= 0:
            self.open_student_id = self.matches[index].student.id
            self.accept()

    def confirm_separate(self):
        self.create_separate = True
        self.accept()


class StudentDialog(QDialog):
    def __init__(self, repository, student=None, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.original = student or Student()
        self.saved_student = None
        self.open_student_id = None
        self.setWindowTitle("Карточка слушателя" if self.original.id else "Добавить слушателя")
        self.resize(680, 640)
        self.setMinimumSize(420, 350)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Внутренний ID: {self.original.id}" if self.original.id else "Новая карточка. * — обязательные поля; возраст — 18+."))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        content = QVBoxLayout(body)
        self.fields = {}
        for title, fields in GROUPS:
            group = QGroupBox(title)
            form = QFormLayout(group)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            for field, caption in fields:
                edit = QPlainTextEdit() if field == "note" else QLineEdit()
                edit.setAccessibleName(caption)
                if field == "note":
                    edit.setMaximumHeight(90)
                    edit.setPlainText(self.original.note)
                else:
                    edit.setText(getattr(self.original, field))
                    if field.endswith("date"):
                        edit.setPlaceholderText("ГГГГ-ММ-ДД")
                self.fields[field] = edit
                form.addRow(caption, edit)
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

    def values(self):
        values = {field: (edit.toPlainText() if field == "note" else edit.text()) for field, edit in self.fields.items()}
        return Student(id=self.original.id, **values)

    def is_dirty(self):
        return asdict(self.values()) != asdict(self.original)

    def save(self):
        student = self.values()
        try:
            try:
                saved = self.repository.save(student)
            except DuplicateStudentError as error:
                dialog = DuplicateDialog(error.matches, self)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return False
                if dialog.open_student_id is not None:
                    self.open_student_id = dialog.open_student_id
                    super().reject()
                    return False
                if not dialog.create_separate:
                    return False
                saved = self.repository.save(student, allow_duplicates=True)
        except (ValueError, StudentRepositoryError) as error:
            QMessageBox.warning(self, "Карточка не сохранена", str(error))
            return False
        self.saved_student = saved
        self.accept()
        return True

    def reject(self):
        if self.is_dirty():
            answer = QMessageBox.question(
                self, "Несохранённые изменения", "Сохранить изменения карточки слушателя?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer == QMessageBox.StandardButton.Save:
                self.save()
                return
            if answer != QMessageBox.StandardButton.Discard:
                return
        super().reject()
