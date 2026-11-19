"""Форма реквизитов учебного центра с отложенным сохранением."""

from pathlib import Path
import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from proficentr.organization import TrainingCenter, read_logo


GROUPS = (
    ("Общие сведения", (
        ("full_name", "Полное наименование"), ("short_name", "Сокращённое наименование"),
        ("inn", "ИНН"), ("kpp", "КПП"), ("ogrn", "ОГРН / ОГРНИП"),
        ("legal_address", "Юридический адрес"), ("actual_address", "Фактический адрес"),
        ("postal_address", "Почтовый адрес"), ("phone", "Телефон"),
        ("email", "Email"), ("website", "Сайт"),
    )),
    ("Банковские реквизиты", (
        ("bank_name", "Наименование банка"), ("bik", "БИК"),
        ("settlement_account", "Расчётный счёт"), ("correspondent_account", "Корреспондентский счёт"),
    )),
    ("Руководитель", (
        ("director_name", "ФИО руководителя"), ("director_position", "Должность"),
        ("director_genitive", "ФИО в родительном падеже"),
    )),
    ("Образовательная лицензия", (
        ("license_number", "Регистрационный номер"), ("license_date", "Дата предоставления"),
        ("license_authority", "Лицензирующий орган"),
    )),
)


class TrainingCenterPage(QWidget):
    def __init__(self, repository):
        super().__init__()
        self.repository = repository
        self.original = repository.load() if repository else TrainingCenter()
        self.logo_source = None
        self.remove_logo = False
        self.fields = {}
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        content = QVBoxLayout(body)
        content.addWidget(QLabel("Реквизиты вашего учебного центра. Поля можно заполнять постепенно."))
        for title, fields in GROUPS:
            group = QGroupBox(title)
            form = QFormLayout(group)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            for name, caption in fields:
                edit = QLineEdit()
                edit.setAccessibleName(caption)
                if name == "license_date":
                    edit.setPlaceholderText("ГГГГ-ММ-ДД, например 2020-01-31")
                self.fields[name] = edit
                form.addRow(caption, edit)
            content.addWidget(group)
        logo_group = QGroupBox("Логотип")
        logo_layout = QVBoxLayout(logo_group)
        self.preview = QLabel("Логотип не выбран")
        self.preview.setMinimumHeight(100)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_layout.addWidget(self.preview)
        buttons = QHBoxLayout()
        self.upload_button = QPushButton("Загрузить / заменить…")
        self.upload_button.clicked.connect(self.choose_logo)
        self.remove_button = QPushButton("Удалить логотип")
        self.remove_button.clicked.connect(self.clear_logo)
        buttons.addWidget(self.upload_button)
        buttons.addWidget(self.remove_button)
        logo_layout.addLayout(buttons)
        logo_layout.addWidget(QLabel("PNG / JPG, до 5 МиБ и 25 млн пикселей. Применяется после сохранения."))
        content.addWidget(logo_group)
        content.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.save_button = QPushButton("Сохранить")
        self.save_button.clicked.connect(self.save)
        layout.addWidget(self.save_button)
        self.setEnabled(repository is not None)
        self.populate()

    def values(self):
        return TrainingCenter(**{name: edit.text() for name, edit in self.fields.items()}, logo_path=self.original.logo_path)

    def is_dirty(self):
        return self.values() != self.original or self.logo_source is not None or self.remove_logo

    def populate(self):
        for name, edit in self.fields.items():
            edit.setText(getattr(self.original, name))
        self.logo_source = None
        self.remove_logo = False
        self.preview.clear()
        self.preview.setText("Логотип не выбран")
        if self.repository and self.original.logo_path:
            try:
                self.show_logo(self.repository.logo_file(self.original.logo_path))
            except (OSError, ValueError) as error:
                self.preview.setText(f"Логотип недоступен: {error}")

    def show_logo(self, path):
        data, _ = read_logo(path)
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        self.preview.setPixmap(pixmap.scaled(
            280, 140, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        ))

    def choose_logo(self):
        file, _ = QFileDialog.getOpenFileName(self, "Логотип учебного центра", "", "Изображения (*.png *.jpg *.jpeg)")
        if not file:
            return
        try:
            self.show_logo(Path(file))
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Логотип не загружен", str(error))
            return
        self.logo_source = Path(file)
        self.remove_logo = False
        self.message.setText("Есть несохранённые изменения.")

    def clear_logo(self):
        self.logo_source = None
        self.remove_logo = bool(self.original.logo_path)
        self.preview.clear()
        self.preview.setText("Логотип не выбран")

    def save(self):
        try:
            self.original = self.repository.save(
                self.values(), logo_source=self.logo_source, remove_logo=self.remove_logo
            )
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            QMessageBox.critical(self, "Реквизиты не сохранены", str(error))
            return False
        self.populate()
        self.message.setText(self.repository.cleanup_warning or "Реквизиты учебного центра сохранены.")
        return True

    def confirm_leave(self):
        if not self.is_dirty():
            return True
        choice = QMessageBox.question(
            self, "Несохранённые изменения", "Сохранить изменения карточки учебного центра?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Save:
            return self.save()
        if choice == QMessageBox.StandardButton.Discard:
            self.populate()
            self.message.clear()
            return True
        return False
