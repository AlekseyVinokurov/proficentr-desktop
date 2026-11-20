"""Приказы о зачислении в разделе документов."""
from dataclasses import replace
from datetime import date
import json
import sqlite3

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget)

from proficentr.enrollment_orders import EnrollmentOrder, KINDS, STATES
from proficentr.ui.groups_page import make_table, fill_table, selected_id
from proficentr.ui.templates_page import open_local_docx

ERRORS = (ValueError, OSError, sqlite3.Error)


def warn(parent, error):
    QMessageBox.warning(parent, 'Приказ о зачислении', str(error) if isinstance(error, ValueError) else 'Операция не выполнена. Проверьте доступность базы и хранилища.')


def confirm(parent, title, text):
    return QMessageBox.question(parent, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes


class OrderDialog(QDialog):
    def __init__(self, repository, order=None, *, training_ids=None, group_id=None, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.original = order or EnrollmentOrder(order_date=date.today().isoformat(), kind='group' if group_id else 'individual', group_id=group_id)
        self.training_ids = list(training_ids if training_ids is not None else repository.training_ids(order.id) if order else [])
        self.saved_order = None
        self.review = None
        self.setWindowTitle('Черновик приказа о зачислении'); self.resize(920, 700)
        layout = QVBoxLayout(self)
        hint = QLabel('Сохранение и регистрация приказа не зачисляют слушателей. После фактического подписания отдельно подтвердите его и проверки ответственного.')
        hint.setWordWrap(True); layout.addWidget(hint)
        self.tabs = QTabWidget(); layout.addWidget(self.tabs)
        details = QWidget(); form = QFormLayout(details)
        self.day = QLineEdit(self.original.order_date); self.day.setPlaceholderText('ГГГГ-ММ-ДД')
        self.kind = QComboBox()
        for key, label in KINDS.items(): self.kind.addItem(label, key)
        self.kind.setCurrentIndex(self.kind.findData(self.original.kind))
        self.group = QComboBox(); self.group.addItem('Без группы', None)
        for key, label in repository.groups(): self.group.addItem(label, key)
        self.group.setCurrentIndex(max(0, self.group.findData(self.original.group_id)))
        self.historical = QLineEdit(self.original.historical_number); self.historical.setPlaceholderText('Исходный номер исторического приказа; иначе оставьте пустым')
        self.note = QPlainTextEdit(self.original.note); self.note.setMaximumHeight(80)
        self.template = QComboBox(); self.template.setMinimumContentsLength(20)
        self.template.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for caption, widget in (('Дата приказа *',self.day), ('Вид *',self.kind), ('Группа',self.group),
                                ('Исторический номер',self.historical), ('Примечание',self.note), ('Активный шаблон',self.template)):
            widget.setAccessibleName(caption); form.addRow(caption,widget)
        help_text = QLabel('Свои формы: Настройки → Шаблоны документов → enrollment_order. Варианты: dpo-individual, dpo-group, po-individual, po-group (допустим суффикс). Различающиеся периоды выводятся в строках students.')
        help_text.setWordWrap(True); form.addRow(help_text); self.tabs.addTab(details,'Приказ')
        page = QWidget(); content = QVBoxLayout(page)
        self.query = QLineEdit(); self.query.setPlaceholderText('Поиск подготовленных обучений по ФИО'); content.addWidget(self.query)
        self.available = make_table(('ФИО','Программа','Начало','Окончание','Группа'))
        self.available.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); content.addWidget(self.available)
        add = QPushButton('Добавить выбранные (Ctrl / Shift)'); add.clicked.connect(self.add_trainings); content.addWidget(add)
        self.selected = QListWidget(); self.selected.setMaximumHeight(90)
        self.selected.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); content.addWidget(self.selected)
        remove = QPushButton('Убрать выделенные из черновика'); remove.clicked.connect(self.remove_trainings); content.addWidget(remove)
        self.contracts = QPlainTextEdit(); self.contracts.setReadOnly(True); self.contracts.setMaximumHeight(140); content.addWidget(self.contracts)
        self.tabs.addTab(page,'Обучения и основания')
        self.status = QLabel(); self.status.setWordWrap(True); self.status.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.save_button = QPushButton('Сохранить черновик'); self.save_button.clicked.connect(self.save)
        self.preview_button = QPushButton('Проект DOCX'); self.preview_button.clicked.connect(self.preview)
        self.register_button = QPushButton('Зарегистрировать'); self.register_button.clicked.connect(self.register)
        close = QPushButton('Закрыть'); close.clicked.connect(self.reject)
        for button in (self.save_button,self.preview_button,self.register_button,close): buttons.addWidget(button)
        layout.addLayout(buttons)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(180); self.timer.timeout.connect(self.refresh_available)
        self.query.textChanged.connect(lambda: self.timer.start())
        self.kind.currentIndexChanged.connect(self.selection_changed); self.group.currentIndexChanged.connect(self.selection_changed)
        self.day.textChanged.connect(self.changed); self.historical.textChanged.connect(self.changed)
        self.note.textChanged.connect(self.changed); self.template.currentIndexChanged.connect(self.changed)
        self.selection_changed(initial=True)
        self._initial_state = self._state()

    def _state(self):
        return (self.day.text(),self.kind.currentData(),self.group.currentData(),self.historical.text(),self.note.toPlainText(),self.template.currentData(),tuple(self.training_ids))

    def is_dirty(self):
        return self._state() != self._initial_state

    def changed(self):
        self.review = None; self.register_button.setEnabled(False)

    def selection_changed(self, *args, initial=False):
        self.group.setEnabled(self.kind.currentData() == 'group')
        self.refresh_available(); self.refresh_selected(); self.refresh_templates(initial=initial); self.changed()

    def values(self):
        return replace(self.original, order_date=self.day.text(),kind=self.kind.currentData(),
                       group_id=self.group.currentData() if self.kind.currentData() == 'group' else None,
                       historical_number=self.historical.text(),note=self.note.toPlainText(),template_id=self.template.currentData())

    def refresh_available(self):
        try:
            rows = self.repository.available_trainings(self.query.text(), self.group.currentData() if self.kind.currentData() == 'group' else None)
            fill_table(self.available, [(key,name,json.loads(snapshot)['full_name'],start,end,group or 'Индивидуальное') for key,name,snapshot,start,end,group in rows])
        except ERRORS as error: warn(self,error)

    def refresh_selected(self):
        self.selected.clear()
        for key,name,snapshot,start,end,state,student_id in self.repository.training_labels(self.training_ids):
            item = QListWidgetItem(f'{name} · {start} — {end} · ID {key}'); item.setData(Qt.ItemDataRole.UserRole,key); self.selected.addItem(item)
        lines = self.repository.warnings(self.training_ids)
        for row in self.repository.related_contracts(self.training_ids):
            lines += [f"Обучение ID {row['training_id']}: договор {c['number'] or 'без номера'} от {c['date']} ({c['state']})" for c in row['contracts']]
        self.contracts.setPlainText('\n'.join(lines))

    def refresh_templates(self, initial=False):
        previous = self.original.template_id if initial else self.template.currentData()
        self.template.clear(); self.template.addItem('Выберите подходящую активную форму',None)
        if not self.training_ids: return
        try:
            versions = self.repository.suitable_templates(self.values(), self.training_ids)
            for t in versions: self.template.addItem(f'{t.name} · {t.variant} · версия {t.version}',t.id)
            self.template.setCurrentIndex(max(0,self.template.findData(previous)))
            if previous is None and len(versions) == 1: self.template.setCurrentIndex(1)
        except ERRORS as error: self.status.setText(str(error))

    def add_trainings(self):
        keys = [self.available.item(i.row(),0).data(Qt.ItemDataRole.UserRole) for i in self.available.selectionModel().selectedRows()]
        self.training_ids += [key for key in keys if key not in self.training_ids]
        self.refresh_selected(); self.refresh_templates(); self.changed()

    def remove_trainings(self):
        keys = {item.data(Qt.ItemDataRole.UserRole) for item in self.selected.selectedItems()}
        self.training_ids = [key for key in self.training_ids if key not in keys]
        self.refresh_selected(); self.refresh_templates(); self.changed()

    def save(self):
        try:
            self.original = self.repository.save(self.values(),self.training_ids); self.saved_order = self.original
            self._initial_state = self._state(); self.changed(); self.status.setText('Черновик сохранён без регистрационного номера.')
            return True
        except ERRORS as error: warn(self,error); return False

    def preview(self):
        if (self.original.id is None or self.is_dirty()) and not self.save(): return False
        try:
            if self.repository.load(self.original.id).revision != self.original.revision:
                raise ValueError('Черновик изменён в другом окне. Откройте актуальную запись.')
            path,self.review = self.repository.preview(self.original.id)
            self.register_button.setEnabled(True); self.status.setText('Проверьте проект перед регистрацией.\n' + str(path)); open_local_docx(path)
            return True
        except ERRORS as error: warn(self,error); return False

    def register(self):
        if self.review is None or self.is_dirty(): return False
        if not confirm(self,'Регистрация приказа','Проект проверен? Присвоить номер и сохранить неизменяемый DOCX? Подписание и зачисление подтверждаются отдельно.'): return False
        try:
            self.saved_order = self.repository.register(self.original.id,self.review.signature)
            self.original = self.saved_order; self.accept(); return True
        except ERRORS as error: self.changed(); warn(self,error); return False

    def reject(self):
        if self.is_dirty():
            answer = QMessageBox.question(self,'Несохранённые изменения','Сохранить черновик?',QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Save)
            if answer == QMessageBox.StandardButton.Save:
                if not self.save(): return
            elif answer != QMessageBox.StandardButton.Discard: return
        super().reject()


class SigningDialog(QDialog):
    def __init__(self, repository, order, parent=None):
        super().__init__(parent); self.repository = repository; self.order = order
        self.setWindowTitle('Подтверждение фактического подписания'); self.resize(700,480)
        form = QFormLayout(self)
        text = QPlainTextEdit('\n'.join([f'{order.number} от {order.order_date}',
            *[f"{s['full_name']} · {s['start_date']} — {s['end_date']}" for s in json.loads(order.snapshot)['context']['students']],
            *repository.warnings(repository.training_ids(order.id))])); text.setReadOnly(True); form.addRow(text)
        self.day = QLineEdit(date.today().isoformat()); form.addRow('Фактическая дата подписания *',self.day)
        self.responsible = QLineEdit(); form.addRow('Ответственный (ФИО) *',self.responsible)
        self.grounds = QCheckBox('Приказ подписан уполномоченным лицом; основания и договоры проверены вручную')
        self.education = QCheckBox('Применимые условия допуска/образования проверены вручную')
        form.addRow(self.grounds); form.addRow(self.education)
        hint = QLabel('Программа не проверяет подлинность подписи и юридическую достаточность оснований. После подтверждения обучения будут зачислены и защищены от обычного редактирования. Время подтверждения сохранится отдельно (UTC).')
        hint.setWordWrap(True); form.addRow(hint)
        self.button = QPushButton('Подтвердить подписание и зачислить'); self.button.clicked.connect(self.confirm); form.addRow(self.button)
        close = QPushButton('Отмена'); close.clicked.connect(self.reject); form.addRow(close)

    def confirm(self):
        if not self.grounds.isChecked() or not self.education.isChecked() or not self.responsible.text().strip():
            warn(self,ValueError('Заполните ответственного и явно подтвердите обе проверки.')); return False
        if not confirm(self,'Зачисление','Подтвердить фактическое подписание и зачислить выбранные обучения? Обычное редактирование их оснований будет запрещено.'): return False
        try:
            self.repository.confirm_signing(self.order.id,signed_date=self.day.text(),confirmed_by=self.responsible.text(),
                                            grounds_checked=self.grounds.isChecked(),education_checked=self.education.isChecked())
            self.accept(); return True
        except ERRORS as error: warn(self,error); return False


class EnrollmentOrdersPage(QWidget):
    def __init__(self, repository):
        super().__init__(); self.repository = repository
        layout = QVBoxLayout(self)
        hint = QLabel('Черновик → регистрация → отдельное подтверждение подписания и зачисление. Используйте утверждённые формы своей организации.'); hint.setWordWrap(True); layout.addWidget(hint)
        self.search = QLineEdit(); self.search.setPlaceholderText('Поиск по номеру, дате, группе или ФИО'); layout.addWidget(self.search)
        actions = QHBoxLayout(); secondary = QHBoxLayout()
        self.add_button = QPushButton('Создать приказ'); self.add_button.clicked.connect(lambda: self.open_card())
        self.open_button = QPushButton('Открыть'); self.open_button.clicked.connect(self.open_selected)
        self.copy_button = QPushButton('Открыть копию DOCX'); self.copy_button.clicked.connect(self.open_copy)
        self.sign_button = QPushButton('Подтвердить подписание'); self.sign_button.clicked.connect(self.sign)
        self.package_button = QPushButton('Подготовить пакет документов'); self.package_button.clicked.connect(self.package)
        for button in (self.add_button,self.open_button,self.copy_button): actions.addWidget(button)
        for button in (self.sign_button,self.package_button): secondary.addWidget(button)
        layout.addLayout(actions); layout.addLayout(secondary)
        self.table = make_table(('Номер','Дата','Вид','Группа','Слушателей','Состояние')); layout.addWidget(self.table)
        self.table.itemSelectionChanged.connect(self.selection_changed); self.table.cellDoubleClicked.connect(lambda: self.open_selected())
        self.state = QLabel(); self.state.setWordWrap(True); self.state.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.state)
        self.recovery = QLabel(); self.recovery.setWordWrap(True); layout.addWidget(self.recovery)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(180); self.timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.timer.start()); self.setEnabled(repository is not None)
        if repository:
            try:
                count = repository.recover_unregistered()
                if count: self.recovery.setText(f'Файлов без регистрации: {count}; сохранены в exports/unregistered-orders.')
            except ERRORS as error: self.recovery.setText('Не удалось проверить файлы без регистрации.')
        self.refresh()

    def showEvent(self,event):
        super().showEvent(event); self.refresh()

    def refresh(self,select_id=None):
        try:
            rows = self.repository.search(self.search.text()) if self.repository else []
            fill_table(self.table,[(*row[:-1],STATES[row[-1]]) for row in rows])
            for i in range(self.table.rowCount()):
                if self.table.item(i,0).data(Qt.ItemDataRole.UserRole) == select_id: self.table.selectRow(i)
            self.state.setText(f'Приказов: {len(rows)}')
        except ERRORS as error: fill_table(self.table,[]); self.state.setText('Не удалось прочитать реестр приказов.')
        self.selection_changed()

    def selection_changed(self):
        key = selected_id(self.table); item = self.table.item(self.table.currentRow(),5) if key else None
        self.open_button.setEnabled(key is not None)
        self.sign_button.setEnabled(bool(item and item.text() == STATES['registered']))
        for button in (self.copy_button,self.package_button): button.setEnabled(bool(item and item.text() != STATES['draft']))

    def open_selected(self):
        key = selected_id(self.table)
        if key: self.open_card(key)

    def open_card(self,order_id=None,*,training_ids=None,group_id=None):
        try:
            order = self.repository.load(order_id) if order_id else None
            if order and order.state != 'draft':
                context = json.loads(order.snapshot)['context']
                lines = [f'{order.number} от {order.order_date} · {STATES[order.state]}', 'Группа: ' + context['group']['name'],
                    'Программа: ' + context['program']['name'],
                    *[f"{s['full_name']} · {s['start_date']} — {s['end_date']}" for s in context['students']],
                    'Зарегистрировано (UTC): ' + order.registered_at, 'Дата подписания: ' + (order.signed_date or 'не подтверждена'),
                    'Подтверждение (UTC): ' + order.confirmed_at, 'Ответственный: ' + order.confirmed_by,
                    'Проверка оснований/договоров: ' + ('подтверждена' if order.grounds_checked else 'не подтверждена'),
                    'Проверка условий образования: ' + ('подтверждена' if order.education_checked else 'не подтверждена'),
                    *self.repository.warnings(self.repository.training_ids(order.id))]
                dialog = QDialog(self); dialog.setWindowTitle('Зарегистрированный приказ'); dialog.resize(760,500)
                layout = QVBoxLayout(dialog); text = QPlainTextEdit('\n'.join(lines)); text.setReadOnly(True); layout.addWidget(text)
                close = QPushButton('Закрыть'); close.clicked.connect(dialog.accept); layout.addWidget(close); dialog.exec(); return
            dialog = OrderDialog(self.repository,order,training_ids=training_ids,group_id=group_id,parent=self); dialog.exec()
            self.refresh(dialog.saved_order.id if dialog.saved_order else order_id)
        except ERRORS as error: warn(self,error)

    def open_copy(self):
        key = selected_id(self.table)
        if key:
            try: open_local_docx(self.repository.document_copy(key))
            except ERRORS as error: warn(self,error)

    def sign(self):
        key = selected_id(self.table)
        if key:
            try: SigningDialog(self.repository,self.repository.load(key),self).exec(); self.refresh(key)
            except ERRORS as error: warn(self,error)

    def package(self):
        key = selected_id(self.table)
        if key:
            try:
                folder,warnings = self.repository.prepare_package(key)
                QMessageBox.information(self,'Пакет подготовлен',str(folder) + '\n\n' + '\n'.join(warnings))
            except ERRORS as error: warn(self,error)
