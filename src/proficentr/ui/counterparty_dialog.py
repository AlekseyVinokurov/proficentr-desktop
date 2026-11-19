"""Карточка контрагента: отложенное сохранение и явное решение о дублях."""

from dataclasses import asdict

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from proficentr.counterparties import Counterparty, KINDS
from proficentr.counterparty_repository import DuplicateCounterpartyError, CounterpartyRepositoryError

GROUPS = (
    ("Основные сведения", (
        ("full_name", "Полное наименование *"), ("short_name", "Сокращённое наименование"),
        ("inn", "ИНН"), ("kpp", "КПП (для юридического лица)"), ("ogrn", "ОГРН / ОГРНИП"),
        ("legal_address", "Юридический адрес"), ("actual_address", "Фактический адрес"),
        ("postal_address", "Почтовый адрес"),
    )),
    ("Контакты", (
        ("phone", "Телефон"), ("email", "Email"), ("website", "Сайт"),
        ("contact_name", "Контактное лицо"), ("contact_position", "Должность контактного лица"),
    )),
    ("Банковские реквизиты", (
        ("bank_name", "Банк"), ("bik", "БИК"), ("settlement_account", "Расчётный счёт"),
        ("correspondent_account", "Корреспондентский счёт"),
    )),
    ("Подписание договоров", (
        ("representative_name", "ФИО руководителя / представителя"),
        ("representative_position", "Должность"), ("authority_basis", "Основание полномочий"),
        ("power_of_attorney", "Реквизиты доверенности (если применимо)"),
    )),
    ("Дополнительно", (("note", "Примечание"),)),
)


class DuplicateDialog(QDialog):
    def __init__(self, matches, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Проверка совпадающих организаций")
        self.resize(680, 340)
        self.matches = matches
        self.open_counterparty_id = None
        self.create_separate = False
        layout = QVBoxLayout(self)
        note = QLabel("Найдены совпадения по ИНН или наименованию. Проверьте существующие карточки. Автоматического объединения нет.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.list = QListWidget()
        for match in matches:
            reason = "Совпадение ИНН" if match.reason == "inn" else "Возможный дубль: наименование"
            item = match.counterparty
            self.list.addItem(f"№ {item.id} · {item.full_name} · ИНН {item.inn or 'не указан'} · {reason}")
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
        for button in (open_button, separate, cancel):
            buttons.addWidget(button)
        layout.addLayout(buttons)

    def open_selected(self):
        index = self.list.currentRow()
        if index >= 0:
            self.open_counterparty_id = self.matches[index].counterparty.id
            self.accept()

    def confirm_separate(self):
        self.create_separate = True
        self.accept()


class CounterpartyDialog(QDialog):
    def __init__(self, repository, counterparty=None, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.original = counterparty or Counterparty()
        self.saved_counterparty = None
        self.open_counterparty_id = None
        self.setWindowTitle("Карточка организации" if self.original.id else "Добавить организацию")
        self.resize(720, 660)
        self.setMinimumSize(420, 350)
        layout = QVBoxLayout(self)
        heading = QLabel(f"Внутренний ID: {self.original.id}" if self.original.id else "Новый контрагент. * — обязательные поля. Остальные сведения можно заполнить позже.")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        content = QVBoxLayout(body)
        self.fields = {}
        self.kind = QComboBox()
        self.kind.setAccessibleName("Тип контрагента")
        for value, label in KINDS.items():
            self.kind.addItem(label, value)
        self.kind.setCurrentIndex(self.kind.findData(self.original.kind))
        for index, (title, fields) in enumerate(GROUPS):
            group = QGroupBox(title)
            form = QFormLayout(group)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            if index == 0:
                form.addRow("Тип контрагента *", self.kind)
            for field, caption in fields:
                edit = QPlainTextEdit() if field == "note" else QLineEdit()
                edit.setAccessibleName(caption)
                if field == "note":
                    edit.setMaximumHeight(90)
                    edit.setPlainText(self.original.note)
                else:
                    edit.setText(getattr(self.original, field))
                self.fields[field] = edit
                form.addRow(caption, edit)
            content.addWidget(group)
        self._update_kind_fields()
        self.kind.currentIndexChanged.connect(self._change_kind)
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

    def _update_kind_fields(self):
        is_ip = self.kind.currentData() == "sole_proprietor"
        self.fields["kpp"].setEnabled(not is_ip)
        self.fields["kpp"].setPlaceholderText("Для ИП не применяется" if is_ip else "Необязательно")
        self.fields["inn"].setPlaceholderText("12 цифр" if is_ip else "10 цифр")
        self.fields["ogrn"].setPlaceholderText("ОГРНИП: 15 цифр" if is_ip else "ОГРН: 13 цифр")

    def _change_kind(self):
        if self.kind.currentData() == "sole_proprietor" and self.fields["kpp"].text().strip():
            answer = QMessageBox.question(self, "КПП не применяется к ИП", "Очистить КПП и выбрать тип «Индивидуальный предприниматель»? Изменения применятся после сохранения карточки.",
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                with QSignalBlocker(self.kind):
                    self.kind.setCurrentIndex(self.kind.findData("legal_entity"))
                return
            self.fields["kpp"].clear()
        self._update_kind_fields()

    def values(self):
        values = {field: (edit.toPlainText() if field == "note" else edit.text()) for field, edit in self.fields.items()}
        return Counterparty(id=self.original.id, kind=self.kind.currentData(), **values)

    def is_dirty(self):
        return asdict(self.values()) != asdict(self.original)

    def save(self):
        counterparty = self.values()
        try:
            try:
                saved = self.repository.save(counterparty)
            except DuplicateCounterpartyError as error:
                dialog = DuplicateDialog(error.matches, self)
                if dialog.exec() != QDialog.DialogCode.Accepted:
                    return False
                if dialog.open_counterparty_id is not None:
                    self.open_counterparty_id = dialog.open_counterparty_id
                    super().reject()
                    return False
                if not dialog.create_separate:
                    return False
                saved = self.repository.save(counterparty, allow_duplicates=True)
        except (ValueError, CounterpartyRepositoryError) as error:
            QMessageBox.warning(self, "Карточка не сохранена", str(error))
            return False
        self.saved_counterparty = saved
        self.accept()
        return True

    def reject(self):
        if self.is_dirty():
            answer = QMessageBox.question(
                self, "Несохранённые изменения", "Сохранить изменения карточки организации?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer == QMessageBox.StandardButton.Save:
                self.save()
                return
            if answer != QMessageBox.StandardButton.Discard:
                return
        super().reject()
