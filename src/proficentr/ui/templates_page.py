"""Настройки DOCX-шаблонов: версии, копии и генерация на вымышленных данных."""

import os
from pathlib import Path
import shutil
import subprocess  # nosec B404 # Launch only the installed local DOCX handler after explicit user action.
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from proficentr.docx_context import DocxError, field_guide
from proficentr.docx_engine import read_docx, validate_package
from proficentr.document_templates import DOCUMENT_TYPES
from proficentr.ui.groups_page import make_table, fill_table, selected_id


def open_local_docx(path):
    """Открываем только проверенную локальную копию DOCX, без URL и shell."""
    path = Path(path).resolve()
    validate_package(read_docx(path))
    if sys.platform == 'win32':
        os.startfile(str(path))  # nosec B606 # Explicit user action, validated local DOCX copy only.
    else:
        editor = shutil.which('open' if sys.platform == 'darwin' else 'xdg-open')
        if not editor:
            raise DocxError("Не найден обработчик DOCX. Откройте сохранённую копию в установленном редакторе.")
        subprocess.Popen([editor, str(path)], shell=False)  # nosec B603 # Fixed executable from PATH and validated absolute local DOCX, no shell.


class TemplateImportDialog(QDialog):
    def __init__(self, repository, source, previous=None, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.source = source
        self.saved_version = None
        self.setWindowTitle('Новая версия шаблона' if previous else 'Добавить DOCX-шаблон')
        self.resize(600, 350)
        layout = QVBoxLayout(self)
        file_label = QLabel(str(source))
        file_label.setTextFormat(Qt.TextFormat.PlainText)
        file_label.setWordWrap(True)
        layout.addWidget(file_label)
        form = QFormLayout()
        self.document_type = QComboBox()
        for key, text in DOCUMENT_TYPES.items():
            self.document_type.addItem(text, key)
        self.variant = QLineEdit(previous.variant if previous else 'default')
        self.variant.setMaxLength(100)
        self.name = QLineEdit(previous.name if previous else Path(source).stem)
        self.name.setMaxLength(200)
        self.activate_version = QCheckBox('Активировать после успешной проверки')
        self.activate_version.setChecked(True)
        if previous:
            self.document_type.setCurrentIndex(self.document_type.findData(previous.document_type))
            self.document_type.setEnabled(False)
            self.variant.setReadOnly(True)
        for caption, widget in (('Тип документа', self.document_type), ('Вариант применения', self.variant), ('Название', self.name)):
            widget.setAccessibleName(caption)
            form.addRow(caption, widget)
        form.addRow(self.activate_version)
        layout.addLayout(form)
        note = QLabel('Перед регистрацией проверяются DOCX, разрешённые поля и тестовая генерация. Прежние файлы не заменяются. Для одной пары «тип + вариант» активна одна версия.')
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        self.save_button = QPushButton('Проверить и зарегистрировать')
        self.save_button.clicked.connect(self.save)
        cancel = QPushButton('Отмена')
        cancel.clicked.connect(self.reject)
        buttons.addWidget(self.save_button)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def save(self):
        try:
            self.saved_version = self.repository.register(self.source, self.document_type.currentData(),
                                                         self.variant.text(), self.name.text(),
                                                         activate=self.activate_version.isChecked())
        except (DocxError, OSError, ValueError) as error:
            # DocxError is safe; filesystem messages may contain paths.
            QMessageBox.warning(self, 'Шаблон не зарегистрирован', str(error) if isinstance(error, DocxError) else 'Не удалось прочитать файл или сохранить версию. Прежние шаблоны сохранены.')
            return False
        self.accept()
        return True


class TemplatesPage(QWidget):
    def __init__(self, repository):
        super().__init__()
        self.repository = repository
        layout = QVBoxLayout(self)
        hint = QLabel('Шаблоны хранятся отдельно от EXE. Для изменения формы откройте копию в Word и загрузите новую версию. Тестовый документ использует вымышленные данные и не регистрируется.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.type_filter = QComboBox()
        self.type_filter.setAccessibleName('Фильтр типа шаблонов')
        self.type_filter.addItem('Все типы', None)
        for key, text in DOCUMENT_TYPES.items():
            self.type_filter.addItem(text, key)
        self.type_filter.currentIndexChanged.connect(lambda: self.refresh())
        layout.addWidget(self.type_filter)
        buttons = QHBoxLayout()
        self.add_button = QPushButton('Добавить DOCX')
        self.add_button.clicked.connect(lambda: self.import_file())
        self.version_button = QPushButton('Новая версия')
        self.version_button.clicked.connect(lambda: self.import_file(new_version=True))
        self.activate_button = QPushButton('Активировать версию')
        self.activate_button.clicked.connect(self.activate)
        self.help_button = QPushButton('Доступные поля')
        self.help_button.clicked.connect(self.show_guide)
        for button in (self.add_button, self.version_button, self.activate_button, self.help_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        copies = QHBoxLayout()
        self.export_button = QPushButton('Выгрузить копию…')
        self.export_button.clicked.connect(self.export_copy)
        self.edit_button = QPushButton('Открыть копию в Word')
        self.edit_button.clicked.connect(self.edit_copy)
        self.test_button = QPushButton('Сформировать тестовый документ')
        self.test_button.clicked.connect(self.test_document)
        for button in (self.export_button, self.edit_button, self.test_button):
            copies.addWidget(button)
        layout.addLayout(copies)
        self.table = make_table(('Тип', 'Вариант', 'Название', 'Версия', 'Добавлено (UTC)', 'Состояние', 'SHA-256'))
        self.table.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.table)
        self.state = QLabel()
        self.state.setTextFormat(Qt.TextFormat.PlainText)
        self.state.setWordWrap(True)
        layout.addWidget(self.state)
        self.setEnabled(repository is not None)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def selection_changed(self):
        has_selection = selected_id(self.table) is not None
        for button in (self.version_button, self.activate_button, self.export_button, self.edit_button, self.test_button):
            button.setEnabled(has_selection)

    def refresh(self, select_id=None):
        try:
            versions = self.repository.list_versions(self.type_filter.currentData()) if self.repository else []
            fill_table(self.table, [(version.id, DOCUMENT_TYPES.get(version.document_type, version.document_type), version.variant,
                                    version.name, version.version, version.added_at,
                                    'Активная' if version.active else 'Неактивная', version.sha256) for version in versions])
            for row, version in enumerate(versions):
                self.table.item(row, 6).setToolTip(version.sha256 + '\n' + version.relative_path)
                if version.id == select_id:
                    self.table.selectRow(row)
            self.state.setText(f'Версий: {len(versions)}. Предыдущие версии доступны в этой таблице.')
        except DocxError as error:
            fill_table(self.table, [])
            self.state.setText(str(error))
        self.selection_changed()

    def import_file(self, new_version=False):
        try:
            previous_id = selected_id(self.table)
            if new_version and previous_id is None:
                return
            previous = self.repository.load(previous_id) if new_version else None
            path, _ = QFileDialog.getOpenFileName(self, 'Загрузить DOCX-шаблон', '', 'Документы Word (*.docx)')
            if not path:
                return
            dialog = TemplateImportDialog(self.repository, Path(path), previous, self)
            dialog.exec()
            if dialog.saved_version:
                self.type_filter.setCurrentIndex(0)
                self.refresh(dialog.saved_version.id)
        except DocxError as error:
            self.report_error(error)

    def activate(self):
        version_id = selected_id(self.table)
        if version_id is None:
            return
        if QMessageBox.question(self, 'Активация версии', 'Использовать выбранную версию для этого типа и варианта? Старые файлы и сформированные документы сохранятся.',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.activate(version_id)
            self.refresh(version_id)
        except DocxError as error:
            self.report_error(error)

    def export_copy(self):
        version_id = selected_id(self.table)
        if version_id is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Новый файл копии DOCX', 'template-copy.docx', 'Документы Word (*.docx)')
        if not path:
            return
        try:
            self.repository.export_copy(version_id, Path(path))
            self.state.setText('Копия сохранена: ' + path)
        except (DocxError, OSError) as error:
            self.report_error(error)

    def edit_copy(self):
        version_id = selected_id(self.table)
        if version_id is None:
            return
        try:
            path = self.repository.editable_copy(version_id)
            self.state.setText('Копия для редактирования: ' + str(path) + '\nПосле правок загрузите её как новую версию.')
            open_local_docx(path)
        except (DocxError, OSError) as error:
            self.report_error(error)

    def test_document(self):
        version_id = selected_id(self.table)
        if version_id is None:
            return
        try:
            path = self.repository.test_document(version_id)
            self.state.setText('ТЕСТОВЫЙ ДОКУМЕНТ, НЕ ЗАРЕГИСТРИРОВАН. Только вымышленные данные.\n' + str(path))
            if QMessageBox.question(self, 'Тестовый документ сформирован', 'Открыть тестовый DOCX в установленном редакторе? Документ не зарегистрирован и не имеет юридического номера.',
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
                open_local_docx(path)
        except (DocxError, OSError) as error:
            self.report_error(error)

    def report_error(self, error):
        QMessageBox.warning(self, 'Операция с шаблоном не выполнена', str(error) if isinstance(error, DocxError) else 'Файл или редактор недоступен. Прежние версии и сохранённые копии не изменены.')

    def show_guide(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('Инструкция по DOCX-шаблонам')
        dialog.resize(800, 600)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit(field_guide())
        text.setReadOnly(True)
        layout.addWidget(text)
        close = QPushButton('Закрыть')
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()
