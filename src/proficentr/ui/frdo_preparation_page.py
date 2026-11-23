"""Подготовка сведений и неизменяемых XLSX ФРДО."""

from datetime import date
import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDateEdit, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from proficentr.frdo_export import FrdoExportRepository, PreviousExportError
from proficentr.frdo_validation import FIELDS
from proficentr.ui.groups_page import make_table, fill_table, selected_id
from proficentr.ui.student_dialog import StudentDialog
from proficentr.student_repository import StudentRepositoryError

ERRORS = (ValueError, OSError, sqlite3.Error, StudentRepositoryError)


def combo(values, selected, *, labels=None):
    edit = QComboBox()
    edit.addItem('Не указано', '')
    for value in dict.fromkeys(values):
        edit.addItem((labels or {}).get(value, value), value)
    if selected and edit.findData(selected) < 0:
        edit.addItem(f'{selected} (нет в справочнике)', selected)
    edit.setCurrentIndex(max(0, edit.findData(selected)))
    edit.setEditable(True)
    edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    edit.completer().setFilterMode(Qt.MatchFlag.MatchContains)
    return edit


def combo_value(edit):
    # Свободный ввод не должен молча сохранять прежний currentData.
    index = edit.findText(edit.currentText())
    return edit.itemData(index) if index >= 0 else edit.currentText()


class FrdoRecordDialog(QDialog):
    def __init__(self, repository, document_id, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.document_id = document_id
        self.review = repository.review(document_id)
        document, student, details, program = repository.edit_context(document_id)
        self.document = document
        structure = repository.templates.structure(document.direction)
        self.setWindowTitle('Сведения для ФРДО')
        self.resize(850, 680)
        layout = QVBoxLayout(self)
        title = QLabel(f'{document.full_name}\n{document.program_name}\nДокумент {document.number} от {document.document_date}')
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        note = QLabel('Реквизиты и ФИО выданного документа берутся из защищённого реестра. '
                      'Дополнения для ФРДО сохраняются отдельно, архивный документ не меняется. '
                      'Пустое значение означает отсутствие сведений, а не подтверждённое «Нет».')
        note.setWordWrap(True)
        layout.addWidget(note)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        self.personal = {}
        self.training = {}
        self.program = {}
        f = FIELDS[document.direction]

        def tab(caption):
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            body = QWidget()
            form = QFormLayout(body)
            form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            scroll.setWidget(body)
            tabs.addTab(scroll, caption)
            return form

        def add(form, controls, name, caption, value, column=None):
            labels = {'000': '000 — Без гражданства', '643': '643 — Российская Федерация'} if column == f['citizenship'] else {}
            edit = combo(structure.dictionaries.get(column, ()), value, labels=labels) if column in structure.dictionaries else QLineEdit(value)
            controls[name] = edit
            edit.setAccessibleName(caption)
            form.addRow(caption, edit)

        form = tab('Слушатель')
        add(form, self.personal, 'sex', 'Пол', student.sex, f['sex'])
        add(form, self.personal, 'citizenship_code', 'Гражданство — код ОКСМ (000 — без гражданства)', student.citizenship_code, f['citizenship'])
        label = QLabel('Эти два значения сохраняются в карточке слушателя и используются для всех его обучений. '
                       'Дата рождения и СНИЛС берутся из карточки; ФИО — из выданного документа. '
                       f'Текущее описание гражданства: {student.citizenship or "не указано"}.')
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        form.addRow(label)
        self.student_button = QPushButton('Открыть карточку слушателя (дата рождения, СНИЛС…)')
        self.student_button.clicked.connect(self.open_student)
        form.addRow(self.student_button)
        form = tab('Это обучение')
        for name, caption in [('form', 'Форма обучения'), ('funding', 'Источник финансирования'), ('relations', 'Форма получения образования')]:
            add(form, self.training, name, caption, getattr(details, name), f[name])
        if not self.review.values[f['patronymic']] or details.patronymic:
            add(form, self.training, 'patronymic', 'Отчество на дату выдачи (при отсутствии укажите Нет)', details.patronymic)
        if document.direction == 'dpo':
            for name, caption, column in [('dpo_area', 'Область профессиональной деятельности', 'L'),
                    ('dpo_group', 'Укрупнённая группа специальностей', 'M'),
                    ('education_level', 'Уровень предыдущего образования', 'O'),
                    ('education_surname', 'Фамилия в документе ВО/СПО', 'P'),
                    ('education_series', 'Серия документа ВО/СПО', 'Q'), ('education_number', 'Номер документа ВО/СПО', 'R')]:
                add(form, self.training, name, caption, self.review.values[column], column)
            if not document.qualification:
                add(form, self.training, 'qualification', 'Фактически присвоенная квалификация (не присвоена — укажите Нет)', details.qualification)
        else:
            for name, caption, column in [('po_profession', 'Профессия рабочего / должность служащего', 'L'),
                                          ('po_grade', 'Присвоенный разряд, класс, категория', 'M')]:
                add(form, self.training, name, caption, self.review.values[column], column)
        form.addRow(QLabel('Сведения относятся только к этому обучению и его исторической редакции программы.'))
        form = tab('Программа: новые обучения')
        note = QLabel('Здесь можно сохранить классификаторы текущей карточки программы для будущих обучений. '
                      'Эти изменения не подменяют снимок программы уже зарегистрированного документа. '
                      f'Текущая карточка: {program.full_name}, редакция {program.edition or "не указана"}.')
        note.setTextFormat(Qt.TextFormat.PlainText)
        note.setWordWrap(True)
        form.addRow(note)
        if program.direction == document.direction:
            for name, caption, column in ([('dpo_area', 'Область профессиональной деятельности', 'L'), ('dpo_group', 'Укрупнённая группа', 'M')]
                                          if document.direction == 'dpo' else [('po_profession', 'Профессия рабочего / должность служащего', 'L')]):
                add(form, self.program, name, caption, getattr(program, name), column)
        self.errors = QPlainTextEdit('\n'.join(self.review.messages()) or 'Все необходимые сведения проходят локальную проверку.')
        self.errors.setReadOnly(True)
        self.errors.setMaximumHeight(150)
        layout.addWidget(self.errors)
        buttons = QHBoxLayout()
        self.save_button = QPushButton('Сохранить и проверить')
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(self.save_button)
        close = QPushButton('Отмена')
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._initial = self._values()

    def _values(self):
        return [{name: combo_value(edit) if isinstance(edit, QComboBox) else edit.text() for name, edit in controls.items()}
                for controls in (self.personal, self.training, self.program)]

    def open_student(self):
        if self._values() != self._initial:
            QMessageBox.warning(self, 'Несохранённые сведения', 'Сначала сохраните дополнения ФРДО или отмените изменения.')
            return
        student = self.repository.students.load(self.review.student_id)
        dialog = StudentDialog(self.repository.students, student, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.review = self.repository.review(self.document_id)
            self.errors.setPlainText('\n'.join(self.review.messages()) or 'Готово')

    def save(self):
        personal, training, program = [
            {name: value for name, value in current.items() if value != original[name]}
            for current, original in zip(self._values(), self._initial)
        ]
        try:
            self.review = self.repository.save_details(self.document_id, personal=personal, training=training,
                                                       program=program, expected_signature=self.review.signature)
        except ERRORS as error:
            QMessageBox.warning(self, 'Сведения не сохранены', str(error))
            return False
        self._initial = self._values()
        self.accept()
        return True

    def reject(self):
        if self._values() != self._initial:
            answer = QMessageBox.question(self, 'Несохранённые сведения', 'Закрыть без сохранения дополнений ФРДО?',
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                         QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        super().reject()


class FrdoPreparationPage(QWidget):
    def __init__(self, repository):
        super().__init__()
        self.repository = repository
        self.records = {}
        self.exports = FrdoExportRepository(repository.connection, repository.storage) if repository else None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Подготовка ФРДО'))
        filters = QHBoxLayout()
        self.kind = QComboBox()
        self.kind.addItem('ДПО', 'dpo')
        self.kind.addItem('ПО', 'po')
        filters.addWidget(self.kind)
        self.start = QDateEdit()
        self.end = QDateEdit()
        for edit in (self.start, self.end):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat('dd.MM.yyyy')
        today = date.today()
        self.start.setDate(date(today.year, 1, 1))
        self.end.setDate(today)
        filters.addWidget(QLabel('Выдача с'))
        filters.addWidget(self.start)
        filters.addWidget(QLabel('по'))
        filters.addWidget(self.end)
        self.check_button = QPushButton('Проверить готовность')
        self.check_button.setEnabled(repository is not None)
        self.check_button.clicked.connect(self.refresh)
        filters.addWidget(self.check_button)
        layout.addLayout(filters)
        self.state = QLabel('Выберите категорию и период выдачи, проверьте и выделите документы для XLSX.')
        self.state.setWordWrap(True)
        self.state.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.state)
        self.table = make_table(['ID', 'Слушатель', 'Документ', 'Дата выдачи', 'Программа', 'Готовность'])
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.table)
        self.errors = QPlainTextEdit()
        self.errors.setReadOnly(True)
        self.errors.setMaximumHeight(180)
        layout.addWidget(self.errors)
        self.edit_button = QPushButton('Открыть сведения и исправить')
        self.edit_button.setEnabled(False)
        self.edit_button.clicked.connect(self.open_record)
        layout.addWidget(self.edit_button)
        actions = QHBoxLayout()
        self.select_ready_button = QPushButton('Выбрать готовые')
        self.select_ready_button.clicked.connect(self.select_ready)
        actions.addWidget(self.select_ready_button)
        self.export_button = QPushButton('Сформировать XLSX')
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_selected)
        actions.addWidget(self.export_button)
        layout.addLayout(actions)
        layout.addWidget(QLabel('Сформированные XLSX (подготовлены, приём реестром не подтверждён)'))
        self.packages_table = make_table(['Пакет', 'Категория', 'Год', 'Документов', 'Создан UTC', 'Файл / состояние'])
        self.packages_table.setMaximumHeight(180)
        layout.addWidget(self.packages_table)
        self.history_state = QLabel()
        self.history_state.setWordWrap(True)
        self.history_state.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.history_state)
        package_actions = QHBoxLayout()
        self.copy_button = QPushButton('Получить файл пакета')
        self.copy_button.clicked.connect(self.copy_package)
        self.restore_button = QPushButton('Восстановить из копии')
        self.restore_button.clicked.connect(self.restore_package)
        package_actions.addWidget(self.copy_button)
        package_actions.addWidget(self.restore_button)
        layout.addLayout(package_actions)
        self.table.itemSelectionChanged.connect(self.show_errors)
        self.table.itemDoubleClicked.connect(lambda _: self.open_record())
        self.kind.currentIndexChanged.connect(self.invalidate)
        self.start.dateChanged.connect(self.invalidate)
        self.end.dateChanged.connect(self.invalidate)

    def invalidate(self):
        self.records.clear()
        self.table.setRowCount(0)
        self.errors.clear()
        self.edit_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.state.setText('Параметры изменены. Нажмите «Проверить готовность».')

    def refresh(self):
        self.invalidate()
        self.refresh_packages()
        if self.repository is None:
            return
        try:
            rows = self.repository.search(self.kind.currentData(), self.start.date().toString('yyyy-MM-dd'), self.end.date().toString('yyyy-MM-dd'))
        except ERRORS as error:
            self.state.setText(str(error))
            return
        self.records = {row.document_id: row for row in rows}
        fill_table(self.table, [(r.document_id, r.full_name, r.number, r.document_date, r.program_name, r.status) for r in rows])
        ready = sum(r.ready for r in rows)
        self.state.setText(f'Документов: {len(rows)}. Готово: {ready}. Требует исправления: {len(rows)-ready}. '
                           'Это результат локальной проверки; приём ФИС ФРДО не подтверждён.')
        self.show_errors()

    def show_errors(self):
        record = self.records.get(selected_id(self.table))
        self.edit_button.setEnabled(record is not None)
        selected = self.selected_records()
        self.errors.setPlainText('\n'.join(message for row in selected for message in row.messages()))
        self.export_button.setEnabled(bool(selected) and all(r.ready for r in selected))
        self.export_button.setToolTip('Выделите готовые документы одной категории и года. Ошибки блокируют весь набор.')

    def selected_records(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return tuple(self.records[self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)] for row in rows)

    def select_ready(self):
        self.table.clearSelection()
        for row in range(self.table.rowCount()):
            key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if self.records[key].ready:
                for col in range(self.table.columnCount()):
                    item = self.table.item(row, col)
                    if item:
                        item.setSelected(True)

    def refresh_packages(self):
        if self.exports is None:
            return
        rows = []
        try:
            for p in self.exports.packages():
                try:
                    location = str(self.exports.file(p.id))
                except ERRORS as error:
                    location = str(error) + '\n' + p.relative_path
                rows.append((p.id, p.number, {'dpo':'ДПО','po':'ПО'}[p.kind], p.issue_year, p.row_count, p.created_at, location))
            fill_table(self.packages_table, rows)
            orphaned = self.exports.unregistered_files()
            self.history_state.setText(f'Обнаружено незарегистрированных файлов после сбоя: {len(orphaned)}. Они не считаются пакетами; сохраните их отдельно перед повторной подготовкой.' if orphaned else '')
        except ERRORS as error:
            self.history_state.setText(str(error))

    def export_selected(self):
        try:
            selection = self.exports.select(self.selected_records())
            try:
                packages = self.exports.create(selection)
            except PreviousExportError as error:
                if QMessageBox.question(self, 'Повторная подготовка ФРДО', str(error),
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                    return
                packages = self.exports.create(selection, allow_reexport=True)
            self.refresh_packages()
            text = '\n'.join(f'{p.filename} — пакет № {p.number}, документов: {p.row_count}, создан UTC {p.created_at}\n'
                             f'{self.exports.file(p.id)}' for p in packages)
            self.state.setText('XLSX подготовлен. Приём ФИС ФРДО не подтверждён.\n' + text)
        except ERRORS as error:
            QMessageBox.warning(self, 'Подготовка ФРДО', str(error))
            self.refresh_packages()

    def copy_package(self):
        key = selected_id(self.packages_table)
        if key is None:
            return
        try:
            package = self.exports.load(key)
            destination, _ = QFileDialog.getSaveFileName(self, 'Получить тот же XLSX', package.filename, 'Excel (*.xlsx)')
            if destination:
                self.exports.copy(key, destination)
                self.state.setText('Сохранена копия исходного пакета: ' + destination)
        except ERRORS as error:
            QMessageBox.warning(self, 'Пакет ФРДО', str(error))

    def restore_package(self):
        key = selected_id(self.packages_table)
        if key is None:
            return
        source, _ = QFileDialog.getOpenFileName(self, 'Исходный XLSX из резервной копии', '', 'Excel (*.xlsx)')
        if source:
            try:
                self.exports.restore(key, source)
                self.refresh_packages()
            except ERRORS as error:
                QMessageBox.warning(self, 'Восстановление ФРДО', str(error))

    def open_record(self):
        key = selected_id(self.table)
        if key is None:
            return
        try:
            dialog = FrdoRecordDialog(self.repository, key, self)
            dialog.exec()
        except ERRORS as error:
            QMessageBox.warning(self, 'Подготовка ФРДО', str(error))
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()  # Повторный вход пересчитывает данные и проверяет текущий шаблон.
