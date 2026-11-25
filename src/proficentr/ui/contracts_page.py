"""Договоры в разделе «Документы»: одна карточка для сторон, обучений и оформления."""

from dataclasses import replace
from datetime import date
import json

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFormLayout, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from proficentr.contracts import Contract, ContractError, CUSTOMER_TYPES, PERSON_FIELDS, STATES, format_price, price_from_text
from proficentr.docx_context import DocxError
from proficentr.document_templates import DOCUMENT_TYPES
from proficentr.contract_repository import TEMPLATE_TYPES
from proficentr.ui.groups_page import make_table, fill_table, selected_id
from proficentr.ui.templates_page import open_local_docx

ERRORS = (ContractError, DocxError, ValueError, OSError)
PERSON_CAPTIONS = dict(zip(PERSON_FIELDS, ('ФИО', 'Дата рождения', 'Адрес регистрации', 'Серия паспорта', 'Номер паспорта',
                                          'Кем выдан', 'Дата выдачи', 'Код подразделения', 'Телефон', 'Email')))


def warn(parent, error):
    QMessageBox.warning(parent, 'Договор', str(error) if isinstance(error, ValueError) else 'Хранилище или редактор недоступны. Сохранённые документы не заменены.')


class ContractDialog(QDialog):
    def __init__(self, repository, contract=None, *, training_ids=None, group_id=None, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.original = contract or Contract(contract_date=date.today().isoformat())
        self.saved_contract = None
        self.review = None
        self.training_ids = list(training_ids if training_ids is not None else repository.training_ids(contract.id) if contract else [])
        self.setWindowTitle('Черновик договора')
        self.resize(940, 700)
        layout = QVBoxLayout(self)
        hint = QLabel('Регистрация в программе не означает подписание сторонами. Проверьте проект и подтвердите окончательное оформление. Состояние обучений не меняется.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        details = QWidget(); form = QFormLayout(details)
        self.day = QLineEdit(self.original.contract_date)
        self.day.setPlaceholderText('ГГГГ-ММ-ДД')
        self.historical = QLineEdit(self.original.historical_number)
        self.historical.setPlaceholderText('Только для исторического договора; иначе номер выдаётся при регистрации')
        self.price = QLineEdit(format_price(self.original.total_cents))
        self.terms = QPlainTextEdit(self.original.payment_terms)
        self.terms.setMaximumHeight(100)
        self.note = QPlainTextEdit(self.original.note)
        self.note.setMaximumHeight(80)
        self.template = QComboBox()
        self.template.setMinimumContentsLength(20)
        self.template.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for caption, widget in (('Дата договора *', self.day), ('Исходный номер', self.historical), ('Общая стоимость, руб. *', self.price),
                                ('Условия оплаты *', self.terms), ('Примечание', self.note), ('Активный шаблон', self.template)):
            widget.setAccessibleName(caption)
            form.addRow(caption, widget)
        note = QLabel('Выберите утверждённую форму для вида обучения и заказчика. Стоимость общая для всех выбранных обучений; валюта — рубли.')
        note.setWordWrap(True)
        form.addRow(note)
        self.template_hint = QLabel(); self.template_hint.setWordWrap(True)
        form.addRow(self.template_hint)
        self.tabs.addTab(details, 'Договор')
        trainings = QWidget(); training_layout = QVBoxLayout(trainings)
        filters = QHBoxLayout()
        self.group_filter = QComboBox()
        self.group_filter.addItem('Все группы и индивидуальные обучения', None)
        for record_id, name in repository.groups():
            self.group_filter.addItem(name, record_id)
        self.group_filter.setCurrentIndex(max(0, self.group_filter.findData(group_id)))
        self.training_search = QLineEdit()
        self.training_search.setPlaceholderText('Поиск обучений по ФИО')
        filters.addWidget(self.group_filter); filters.addWidget(self.training_search)
        training_layout.addLayout(filters)
        self.available = make_table(('ФИО', 'Программа', 'Начало', 'Окончание', 'Группа'))
        self.available.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        training_layout.addWidget(self.available)
        add = QPushButton('Добавить выбранные обучения (Ctrl / Shift)')
        add.clicked.connect(self.add_trainings)
        training_layout.addWidget(add)
        self.selected = QListWidget()
        self.selected.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.selected.setMaximumHeight(120)
        training_layout.addWidget(self.selected)
        remove = QPushButton('Исключить выделенные из черновика договора')
        remove.clicked.connect(self.remove_trainings)
        training_layout.addWidget(remove)
        self.tabs.addTab(trainings, 'Обучения')
        customer_page = QWidget(); customer_form = QFormLayout(customer_page)
        self.customer_type = QComboBox()
        for key, text in CUSTOMER_TYPES.items():
            self.customer_type.addItem(text, key)
        self.customer_type.setCurrentIndex(self.customer_type.findData(self.original.customer_type))
        self.customer_student = QComboBox()
        self.customer_query = QLineEdit()
        self.customer_query.setPlaceholderText('Поиск организации / ИП по названию (первые 100)')
        self.customer_company = QComboBox()
        self.customer_company.setMinimumContentsLength(20)
        self.customer_company.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        customer_form.addRow('Тип заказчика', self.customer_type)
        customer_form.addRow('Слушатель-заказчик', self.customer_student)
        customer_form.addRow('Поиск заказчика', self.customer_query)
        customer_form.addRow('Организация / ИП', self.customer_company)
        self.person_fields = {}
        for key in PERSON_FIELDS:
            edit = QLineEdit(self.original.person_data.get(key, ''))
            edit.setAccessibleName(PERSON_CAPTIONS[key])
            if key in ('birth_date', 'passport_issue_date'):
                edit.setPlaceholderText('ГГГГ-ММ-ДД')
            self.person_fields[key] = edit
            customer_form.addRow(PERSON_CAPTIONS[key], edit)
        customer_note = QLabel('Другой заказчик-физлицо заполняется только здесь. Его карточка слушателя не создаётся. Для организации / ИП используется существующий справочник «Организации»; работодатель слушателя не меняется.')
        customer_note.setWordWrap(True)
        customer_form.addRow(customer_note)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(customer_page)
        self.tabs.addTab(scroll, 'Заказчик')
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.save_button = QPushButton('Сохранить черновик')
        self.save_button.clicked.connect(self.save)
        self.preview_button = QPushButton('Сформировать проект DOCX')
        self.preview_button.clicked.connect(self.preview)
        self.register_button = QPushButton('Зарегистрировать')
        self.register_button.clicked.connect(self.register)
        close = QPushButton('Закрыть'); close.clicked.connect(self.reject)
        for button in (self.save_button, self.preview_button, self.register_button, close):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(180)
        self.timer.timeout.connect(self.refresh_available)
        self.training_search.textChanged.connect(lambda: self.timer.start())
        self.group_filter.currentIndexChanged.connect(lambda: self.refresh_available())
        self.customer_timer = QTimer(self); self.customer_timer.setSingleShot(True); self.customer_timer.setInterval(180)
        self.customer_timer.timeout.connect(self.refresh_customers)
        self.customer_query.textChanged.connect(lambda: self.customer_timer.start())
        self.refresh_selected(initial=True)
        self.refresh_customers(initial=True)
        self.refresh_available()
        self.refresh_templates(initial=True)
        self.customer_type.currentIndexChanged.connect(self.customer_changed)
        for edit in (self.day, self.historical, self.price, *self.person_fields.values()):
            edit.textChanged.connect(self.changed)
        for edit in (self.terms, self.note):
            edit.textChanged.connect(self.changed)
        for combo in (self.customer_student, self.customer_company, self.template):
            combo.currentIndexChanged.connect(self.changed)
        self._initial_state = self._state()
        self.changed()

    def _state(self):
        return (self.day.text(), self.historical.text(), self.price.text(), self.terms.toPlainText(), self.note.toPlainText(),
                self.customer_type.currentData(), self.customer_student.currentData(), self.customer_company.currentData(),
                tuple(edit.text() for edit in self.person_fields.values()), tuple(self.training_ids), self.template.currentData())

    def is_dirty(self):
        return self._state() != self._initial_state

    def changed(self):
        self.review = None
        self.register_button.setEnabled(False)

    def customer_changed(self):
        self.refresh_customers()
        self.refresh_templates()
        self.changed()

    def refresh_customers(self, initial=False):
        kind = self.customer_type.currentData()
        company = kind in ('legal_entity', 'sole_proprietor')
        self.customer_student.setEnabled(kind == 'student')
        self.customer_query.setEnabled(company)
        self.customer_company.setEnabled(company)
        for edit in self.person_fields.values():
            edit.setEnabled(kind == 'person')
        previous = self.original.counterparty_id if initial else self.customer_company.currentData()
        self.customer_company.clear(); self.customer_company.addItem('Выберите существующего заказчика', None)
        if company:
            rows = self.repository.customers(kind, self.customer_query.text())
            if initial and previous and not any(record_id == previous for record_id, _ in rows):
                rows.append((previous, self.repository.customer_name(previous)))
            for record_id, name in rows:
                self.customer_company.addItem(name, record_id)
            self.customer_company.setCurrentIndex(max(0, self.customer_company.findData(previous)))

    def refresh_available(self):
        try:
            rows = self.repository.available_trainings(self.training_search.text(), self.group_filter.currentData())
            fill_table(self.available, [(record_id, name, json.loads(snapshot)['full_name'], start, end, group or 'Индивидуальное')
                                        for record_id, name, snapshot, start, end, group in rows])
        except ERRORS as error:
            warn(self, error)

    def refresh_selected(self, initial=False):
        previous = self.original.student_id if initial else self.customer_student.currentData()
        self.selected.clear(); self.customer_student.clear()
        self.customer_student.addItem('Выберите слушателя из состава договора', None)
        students = set()
        for record_id, name, snapshot, start, end, state, student_id in self.repository.training_labels(self.training_ids):
            item = QListWidgetItem(f'{name} · {json.loads(snapshot)["full_name"]} · {start} — {end}' + (' · исключён из обучения' if state != 'preparation' else ''))
            item.setData(Qt.ItemDataRole.UserRole, record_id); self.selected.addItem(item)
            if student_id not in students:
                self.customer_student.addItem(name, student_id); students.add(student_id)
        if previous in students:
            self.customer_student.setCurrentIndex(self.customer_student.findData(previous))
        elif initial and self.original.id is None and len(students) == 1:
            self.customer_student.setCurrentIndex(1)

    def refresh_templates(self, initial=False):
        previous = self.original.template_id if initial else self.template.currentData()
        self.template.clear(); self.template.addItem('Выберите подходящий активный шаблон', None)
        if not self.training_ids:
            return
        try:
            versions = self.repository.suitable_templates(replace(self.original, customer_type=self.customer_type.currentData()), self.training_ids)
            for version in versions:
                self.template.addItem(version.name, version.id)
            if self.template.findData(previous) > 0:
                self.template.setCurrentIndex(self.template.findData(previous))
            elif len(versions) == 1:
                self.template.setCurrentIndex(1)
            self.template_hint.setText('' if versions else
                'Нет утверждённой формы. Подключите шаблон «' + DOCUMENT_TYPES[TEMPLATE_TYPES[self.customer_type.currentData()]] +
                '» для выбранного направления обучения в «Настройки → Шаблоны документов».')
        except ERRORS as error:
            self.status.setText(str(error))

    def add_trainings(self):
        ids = [self.available.item(index.row(), 0).data(Qt.ItemDataRole.UserRole) for index in self.available.selectionModel().selectedRows()]
        self.training_ids += [record_id for record_id in ids if record_id not in self.training_ids]
        self.refresh_selected(); self.refresh_templates(); self.changed()

    def remove_trainings(self):
        ids = {item.data(Qt.ItemDataRole.UserRole) for item in self.selected.selectedItems()}
        self.training_ids = [record_id for record_id in self.training_ids if record_id not in ids]
        self.refresh_selected(); self.refresh_templates(); self.changed()

    def save(self):
        try:
            values = replace(self.original, customer_type=self.customer_type.currentData(), student_id=self.customer_student.currentData(),
                             counterparty_id=self.customer_company.currentData(), person_data={key: edit.text() for key, edit in self.person_fields.items()},
                             contract_date=self.day.text(), historical_number=self.historical.text(), total_cents=price_from_text(self.price.text()),
                             payment_terms=self.terms.toPlainText(), note=self.note.toPlainText(), template_id=self.template.currentData())
            self.original = self.repository.save(values, self.training_ids)
            self.saved_contract = self.original
            self._initial_state = self._state()
            self.review = None; self.register_button.setEnabled(False)
            self.status.setText(f'Черновик № ID {self.original.id} сохранён. Регистрационный номер не присвоен.')
            return True
        except ERRORS as error:
            warn(self, error)
            return False

    def preview(self):
        if (self.original.id is None or self.is_dirty()) and not self.save():
            return False
        try:
            if self.repository.load(self.original.id).revision != self.original.revision:
                raise ContractError('Черновик изменён в другом окне. Откройте актуальную запись перед просмотром.')
            path, self.review = self.repository.preview(self.original.id)
            self.register_button.setEnabled(True)
            self.status.setText('Проект не зарегистрирован. Проверьте сведения перед регистрацией.\n' + str(path))
            open_local_docx(path)
            return True
        except ERRORS as error:
            warn(self, error)
            return False

    def register(self):
        if self.review is None or self.is_dirty():
            QMessageBox.information(self, 'Проверка проекта', 'Сначала сформируйте и проверьте актуальный проект DOCX.')
            return False
        if QMessageBox.question(self, 'Регистрация договора', 'Сведения проекта проверены? Присвоить номер и сохранить окончательный DOCX? Регистрация не подтверждает подписание сторонами; обычное редактирование будет запрещено.',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return False
        try:
            self.saved_contract = self.repository.register(self.original.id, self.review.signature)
            self.original = self.saved_contract
            self.accept()
            return True
        except ERRORS as error:
            self.changed()
            warn(self, error)
            return False

    def reject(self):
        if self.is_dirty():
            answer = QMessageBox.question(self, 'Несохранённые изменения', 'Сохранить изменения черновика?',
                                          QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                          QMessageBox.StandardButton.Save)
            if answer == QMessageBox.StandardButton.Save:
                if not self.save():
                    return
            elif answer != QMessageBox.StandardButton.Discard:
                return
        super().reject()


class ContractsPage(QWidget):
    def __init__(self, repository):
        super().__init__()
        self.repository = repository
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('Договоры'))
        hint = QLabel('Черновик → регистрация → при необходимости аннулирование. Регистрация не означает подписание или зачисление слушателя. Используйте утверждённые вашей организацией формы.')
        hint.setWordWrap(True); layout.addWidget(hint)
        self.search = QLineEdit(); self.search.setPlaceholderText('Поиск по номеру, дате, заказчику, ФИО или программе')
        layout.addWidget(self.search)
        actions = QHBoxLayout()
        self.add_button = QPushButton('Создать договор'); self.add_button.clicked.connect(lambda: self.open_card())
        self.open_button = QPushButton('Открыть'); self.open_button.clicked.connect(self.open_selected)
        self.copy_button = QPushButton('Открыть копию DOCX'); self.copy_button.clicked.connect(self.open_copy)
        self.annul_button = QPushButton('Аннулировать'); self.annul_button.clicked.connect(self.annul)
        for button in (self.add_button, self.open_button, self.copy_button, self.annul_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.table = make_table(('Номер', 'Дата', 'Заказчик', 'Программа', 'Слушателей', 'Стоимость, руб.', 'Состояние'))
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.cellDoubleClicked.connect(lambda: self.open_selected())
        layout.addWidget(self.table)
        self.state = QLabel(); self.state.setWordWrap(True); self.state.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.state)
        self.recovery = QLabel(); self.recovery.setWordWrap(True); layout.addWidget(self.recovery)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(180); self.timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.timer.start())
        self.setEnabled(repository is not None)
        if repository:
            try:
                count = repository.recover_unregistered()
                if count:
                    self.recovery.setText(f'Файлов без регистрации: {count}. Сохранены отдельно в exports/unregistered-contracts. Они не включены в реестр договоров.')
            except ERRORS as error:
                self.recovery.setText(str(error))
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event); self.refresh()

    def selection_changed(self):
        record_id = selected_id(self.table)
        item = self.table.item(self.table.currentRow(), 6) if record_id else None
        self.open_button.setEnabled(record_id is not None)
        self.copy_button.setEnabled(bool(item and item.text() != STATES['draft']))
        self.annul_button.setEnabled(bool(item and item.text() == STATES['registered']))

    def refresh(self, select_id=None):
        try:
            rows = self.repository.search(self.search.text()) if self.repository else []
            fill_table(self.table, [(*row[:-1], STATES[row[-1]]) for row in rows])
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == select_id:
                    self.table.selectRow(row)
            self.state.setText(f'Договоров: {len(rows)}')
        except ERRORS as error:
            fill_table(self.table, []); self.state.setText(str(error))
        self.selection_changed()

    def open_selected(self):
        record_id = selected_id(self.table)
        if record_id is not None:
            self.open_card(record_id)

    def open_card(self, contract_id=None, *, training_ids=None, group_id=None):
        try:
            contract = self.repository.load(contract_id) if contract_id else None
            if contract and contract.state != 'draft':
                self.show_registered(contract)
                return
            dialog = ContractDialog(self.repository, contract, training_ids=training_ids, group_id=group_id, parent=self)
            dialog.exec()
            if dialog.saved_contract:
                self.search.clear(); self.timer.stop(); self.refresh(dialog.saved_contract.id)
        except ERRORS as error:
            warn(self, error)

    def show_registered(self, contract):
        context = json.loads(contract.snapshot)['context']
        lines = [f'{STATES[contract.state]} · {contract.number} от {contract.contract_date}',
                 'Регистрация не подтверждает подписание сторонами.',
                 'Учебный центр: ' + context['center']['full_name'], 'Заказчик: ' + context['customer']['full_name'],
                 'Программа: ' + context['program']['name'] + f" · {context['program']['hours']} ч", 'Обучения:']
        lines += [f"{row['full_name']} · {row['start_date']} — {row['end_date']}" for row in context['students']]
        lines += ['Стоимость, руб.: ' + context['contract']['total_price'], 'Оплата: ' + context['contract']['payment_terms'],
                  'Внесено: ' + contract.created_at, 'Зарегистрировано (UTC): ' + contract.registered_at]
        if contract.state == 'annulled':
            lines += ['Основание аннулирования: ' + contract.annul_reason, 'Аннулировано (UTC): ' + contract.annulled_at]
        dialog = QDialog(self); dialog.setWindowTitle('Зарегистрированный договор'); dialog.resize(780, 540)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit('\n'.join(lines)); text.setReadOnly(True); layout.addWidget(text)
        copy = QPushButton('Открыть проверенную копию DOCX'); copy.clicked.connect(lambda: self.open_copy(contract.id)); layout.addWidget(copy)
        close = QPushButton('Закрыть'); close.clicked.connect(dialog.accept); layout.addWidget(close); dialog.exec()

    def open_copy(self, contract_id=None):
        # clicked(bool) must not be interpreted as a persistent ID.
        contract_id = contract_id if type(contract_id) is int else selected_id(self.table)
        if contract_id is None:
            return
        try:
            open_local_docx(self.repository.document_copy(contract_id))
        except ERRORS as error:
            warn(self, error)

    def annul(self):
        record_id = selected_id(self.table)
        if record_id is None:
            return
        reason, accepted = QInputDialog.getMultiLineText(self, 'Основание аннулирования', 'Укажите основание. Номер и оригинал сохранятся:')
        if not accepted:
            return
        if QMessageBox.question(self, 'Аннулирование договора', 'Подтвердить аннулирование? Номер не будет использован повторно, архивный DOCX сохранится.',
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.annul(record_id, reason); self.refresh(record_id)
        except ERRORS as error:
            warn(self, error)
