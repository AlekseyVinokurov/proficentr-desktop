"""Оформление завершения: состав, фактические даты и подписанный приказ."""
from dataclasses import asdict, replace
from datetime import date
import json
import sqlite3

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QTabWidget, QTableWidgetItem, QVBoxLayout, QWidget)
from proficentr.attestations import RESULTS
from proficentr.completion_orders import CompletionOrder, CompletionEntry, KINDS, STATES
from proficentr.ui.groups_page import make_table, fill_table, selected_id
from proficentr.ui.templates_page import open_local_docx
from proficentr.ui.training_dialogs import confirm

ERRORS = (ValueError, OSError, sqlite3.Error)


def warn(parent, error):
    QMessageBox.warning(parent,'Приказ о завершении',str(error) if isinstance(error,ValueError) else 'Проверьте доступность базы и хранилища.')


class CompletionDialog(QDialog):
    def __init__(self, repository, order=None, *, training_ids=None, group_id=None, parent=None):
        super().__init__(parent); self.repository = repository
        ids = list(training_ids or [])
        labels = repository.training_labels(ids) if ids else repository.available_trainings(group_id=group_id)
        direction = json.loads(labels[0][2])['direction'] if labels else 'dpo'
        self.original = order or CompletionOrder(order_date=date.today().isoformat(),kind='group' if group_id else 'individual',group_id=group_id,direction=direction)
        initial = repository.entries(order.id) if order else repository.candidate_entries(ids)
        self.saved_order = None; self.review = None; self.go_to_templates = False
        self.setWindowTitle('Приказ о завершении обучения'); self.resize(1080,740)
        layout = QVBoxLayout(self)
        hint = QLabel('Завершение доступно по актуальной подписанной успешной аттестации. Регистрация не завершает обучение. Подписание и проверка остальных условий подтверждаются отдельно.')
        hint.setWordWrap(True); layout.addWidget(hint)
        tabs = QTabWidget(); layout.addWidget(tabs)
        details = QWidget(); form = QFormLayout(details)
        self.day = QLineEdit(self.original.order_date)
        self.kind = QComboBox()
        for key,label in KINDS.items(): self.kind.addItem(label,key)
        self.kind.setCurrentIndex(self.kind.findData(self.original.kind))
        self.direction = QComboBox(); self.direction.addItem('ДПО','dpo'); self.direction.addItem('ПО','po')
        self.direction.setCurrentIndex(self.direction.findData(self.original.direction))
        self.group = QComboBox(); self.group.addItem('Без группы',None)
        for key,name in repository.groups(): self.group.addItem(name,key)
        self.group.setCurrentIndex(max(0,self.group.findData(self.original.group_id)))
        self.historical = QLineEdit(self.original.historical_number)
        self.note = QPlainTextEdit(self.original.note); self.note.setMaximumHeight(80)
        self.template = QComboBox()
        for caption,widget in (('Дата приказа (ГГГГ-ММ-ДД) *',self.day),('Вид приказа',self.kind),('Направление',self.direction),
                ('Группа',self.group),('Исторический номер',self.historical),('Примечание',self.note),('Активный шаблон',self.template)):
            widget.setAccessibleName(caption); form.addRow(caption,widget)
        self.templates_button = QPushButton('Открыть настройки шаблонов'); self.templates_button.clicked.connect(self.open_templates); form.addRow(self.templates_button)
        tabs.addTab(details,'Приказ')
        roster = QWidget(); content = QVBoxLayout(roster)
        self.query = QLineEdit(); self.query.setPlaceholderText('Поиск зачисленных обучений по ФИО'); content.addWidget(self.query)
        self.available = make_table(('ФИО','Программа','Начало','Плановое окончание','Группа','Подтверждённый результат','Основание'))
        self.available.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); content.addWidget(self.available)
        add = QPushButton('Добавить выбранные (Ctrl / Shift)'); add.clicked.connect(self.add_trainings); content.addWidget(add)
        hint = QLabel('Предлагается плановое окончание. Проверьте и при необходимости исправьте фактическую дату для каждого обучения (ГГГГ-ММ-ДД).')
        hint.setWordWrap(True); content.addWidget(hint)
        self.selected = make_table(('ФИО / программа / период','Фактическая дата завершения','Подтверждённый результат и протокол'))
        self.selected.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.selected.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        content.addWidget(self.selected)
        actions = QHBoxLayout()
        remove = QPushButton('Убрать выделенные'); remove.clicked.connect(self.remove_trainings); actions.addWidget(remove)
        refresh = QPushButton('Обновить основания черновика'); refresh.clicked.connect(self.refresh_basis); actions.addWidget(refresh)
        content.addLayout(actions); tabs.addTab(roster,'Обучения и основания')
        self.status = QLabel(); self.status.setWordWrap(True); self.status.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.save_button = QPushButton('Сохранить черновик'); self.save_button.clicked.connect(self.save)
        self.preview_button = QPushButton('Проект DOCX'); self.preview_button.clicked.connect(self.preview)
        self.register_button = QPushButton('Зарегистрировать'); self.register_button.clicked.connect(self.register)
        close = QPushButton('Закрыть'); close.clicked.connect(self.reject)
        for button in (self.save_button,self.preview_button,self.register_button,close): actions.addWidget(button)
        layout.addLayout(actions)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(180); self.timer.timeout.connect(self.refresh_available)
        self.query.textChanged.connect(lambda:self.timer.start())
        self.day.textChanged.connect(self.changed); self.historical.textChanged.connect(self.changed); self.note.textChanged.connect(self.changed)
        self.selected.itemChanged.connect(self.changed); self.template.currentIndexChanged.connect(self.changed)
        for combo in (self.direction,self.kind,self.group): combo.currentIndexChanged.connect(self.selection_changed)
        self.group.setEnabled(self.original.kind == 'group')
        self.set_entries(initial); self.refresh_available(); self.refresh_templates(self.original.template_id); self.changed()
        self._initial_state = self._state()

    def entries(self):
        return [replace(self.selected.item(i,0).data(Qt.ItemDataRole.UserRole),completion_date=self.selected.item(i,1).text()) for i in range(self.selected.rowCount())]

    def set_entries(self, entries):
        labels = {r[0]:r for r in self.repository.training_labels([e.training_id for e in entries])}
        self.selected.setRowCount(0); self.selected.setRowCount(len(entries))
        for i,entry in enumerate(entries):
            row = labels[entry.training_id]
            p = self.repository.attestations.load(entry.protocol_id) if entry.protocol_id else None
            text = f'Успешно прошёл · {p.number} от {p.protocol_date} · аттестация {p.attestation_date}' if p else ''
            label = f'{row[1]} · {json.loads(row[2])["full_name"]} · {row[3]} — {row[4]}'
            for j,value in enumerate((label,entry.completion_date,text)):
                item = QTableWidgetItem(value)
                if j != 1: item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if j == 0: item.setData(Qt.ItemDataRole.UserRole,entry)
                self.selected.setItem(i,j,item)

    def values(self):
        return replace(self.original,order_date=self.day.text(),kind=self.kind.currentData(),direction=self.direction.currentData(),
            group_id=self.group.currentData() if self.kind.currentData() == 'group' else None,
            historical_number=self.historical.text(),note=self.note.toPlainText(),template_id=self.template.currentData())

    def _state(self):
        return (self.day.text(),self.kind.currentData(),self.direction.currentData(),self.group.currentData(),self.historical.text(),
                self.note.toPlainText(),self.template.currentData(),[asdict(e) for e in self.entries()])

    def is_dirty(self): return self._state() != self._initial_state

    def changed(self,*args): self.review = None; self.register_button.setEnabled(False)

    def selection_changed(self):
        self.group.setEnabled(self.kind.currentData() == 'group')
        self.refresh_available(); self.refresh_templates(); self.changed()

    def refresh_available(self):
        try:
            group = self.group.currentData() if self.kind.currentData() == 'group' else None
            rows = self.repository.available_trainings(self.query.text(),group)
            rows = [r for r in rows if json.loads(r[2])['direction'] == self.direction.currentData() and (self.kind.currentData() == 'group' or r[5] is None)]
            fill_table(self.available,[(key,name,json.loads(snapshot)['full_name'],start,end,group or 'Индивидуально',
                RESULTS[current[4]] if current else 'Нет подписанного протокола',f'{current[2]} · аттестация {current[1]}' if current else '')
                for key,name,snapshot,start,end,group,current in rows])
        except ERRORS as error: warn(self,error)

    def refresh_templates(self, previous=None):
        if previous is None: previous = self.template.currentData()
        self.template.clear(); self.template.addItem('Выберите подходящую активную форму',None)
        versions = self.repository.suitable_templates(self.values())
        for t in versions: self.template.addItem(f'{t.name} · {t.variant} · версия {t.version}',t.id)
        self.template.setCurrentIndex(max(0,self.template.findData(previous)))
        if previous is None and len(versions) == 1: self.template.setCurrentIndex(1)

    def add_trainings(self):
        ids = [self.available.item(i.row(),0).data(Qt.ItemDataRole.UserRole) for i in self.available.selectionModel().selectedRows()]
        try:
            entries = self.entries(); existing = {e.training_id for e in entries}
            entries += self.repository.candidate_entries([key for key in ids if key not in existing])
            self.set_entries(entries); self.changed()
        except ERRORS as error: warn(self,error)

    def remove_trainings(self):
        selected = {i.row() for i in self.selected.selectionModel().selectedRows()}
        self.set_entries([e for i,e in enumerate(self.entries()) if i not in selected]); self.changed()

    def refresh_basis(self):
        try:
            dates = {e.training_id:e.completion_date for e in self.entries()}
            entries = self.repository.candidate_entries(list(dates))
            self.set_entries([replace(e,completion_date=dates[e.training_id]) for e in entries]); self.changed()
            self.status.setText('Основания обновлены. Проверьте даты и новый проект.')
        except ERRORS as error: warn(self,error)

    def save(self):
        try:
            self.original = self.repository.save(self.values(),self.entries()); self.saved_order = self.original
            self.set_entries(self.repository.entries(self.original.id)); self._initial_state = self._state(); self.changed()
            self.status.setText('Черновик сохранён без номера. Обучения не завершены.'); return True
        except ERRORS as error: warn(self,error); return False

    def preview(self):
        if (self.original.id is None or self.is_dirty()) and not self.save(): return False
        try:
            if self.repository.load(self.original.id).revision != self.original.revision: raise ValueError('Черновик изменён в другом окне. Откройте его заново.')
            path,self.review = self.repository.preview(self.original.id); open_local_docx(path)
            self.register_button.setEnabled(True); self.status.setText('Проверьте проект перед регистрацией.\n' + str(path)); return True
        except ERRORS as error: warn(self,error); return False

    def register(self):
        if self.review is None or self.is_dirty(): return False
        if not confirm(self,'Регистрация приказа','Проект проверен? Присвоить номер и сохранить неизменяемый приказ? Завершение произойдёт после отдельного подтверждения подписания и условий.'): return False
        try:
            self.saved_order = self.repository.register(self.original.id,self.review.signature); self.original = self.saved_order; self.accept(); return True
        except ERRORS as error: self.changed(); warn(self,error); return False

    def open_templates(self):
        if self.reject(): self.go_to_templates = True

    def reject(self):
        if self.is_dirty():
            answer = QMessageBox.question(self,'Несохранённые изменения','Сохранить черновик?',QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Save)
            if answer == QMessageBox.StandardButton.Save:
                if not self.save(): return False
            elif answer != QMessageBox.StandardButton.Discard: return False
        super().reject()
        return True


class CompletionSigningDialog(QDialog):
    def __init__(self, repository, order, parent=None):
        super().__init__(parent); self.repository = repository; self.order = order
        self.setWindowTitle('Подтверждение подписания и завершения'); self.resize(780,500)
        form = QFormLayout(self)
        c = json.loads(order.snapshot)['context']
        text = QPlainTextEdit('\n'.join([f'{order.number} от {order.order_date}',
            *[f"{s['full_name']} · завершение {s['completion_date']} · {s['attestation_result']} · {s['completion_basis']}" for s in c['students']]]))
        text.setReadOnly(True); form.addRow(text)
        self.day = QLineEdit(date.today().isoformat()); form.addRow('Фактическая дата подписания *',self.day)
        self.responsible = QLineEdit(); form.addRow('Ответственный (ФИО) *',self.responsible)
        self.signed = QCheckBox('Подтверждаю наличие фактически подписанного приказа'); form.addRow(self.signed)
        self.conditions = QCheckBox('Остальные необходимые условия завершения проверены и выполнены'); form.addRow(self.conditions)
        hint = QLabel('Проверка применимых требований и локальных актов выполняется сотрудником. Программа фиксирует подтверждение, но не проверяет подлинность подписи. Выдача выпускного документа не выполняется.')
        hint.setWordWrap(True); form.addRow(hint)
        button = QPushButton('Подтвердить подписание и завершить обучения'); button.clicked.connect(self.confirm); form.addRow(button)
        close = QPushButton('Отмена'); close.clicked.connect(self.reject); form.addRow(close)

    def confirm(self):
        if not self.signed.isChecked(): warn(self,ValueError('Явно подтвердите наличие подписанного приказа.')); return False
        try:
            self.repository.confirm_signing(self.order.id,signed_date=self.day.text(),confirmed_by=self.responsible.text(),conditions_checked=self.conditions.isChecked())
            self.accept(); return True
        except ERRORS as error: warn(self,error); return False


class CompletionOrdersPage(QWidget):
    templates_requested = Signal()

    def __init__(self, repository=None):
        super().__init__(); self.repository = repository
        layout = QVBoxLayout(self)
        self.search = QLineEdit(); self.search.setPlaceholderText('Поиск по номеру, дате, группе, программе и ФИО'); layout.addWidget(self.search)
        actions = QHBoxLayout()
        self.add_button = QPushButton('Создать приказ'); self.add_button.clicked.connect(lambda:self.open_card())
        self.open_button = QPushButton('Открыть'); self.open_button.clicked.connect(self.open_selected)
        self.copy_button = QPushButton('Открыть копию DOCX'); self.copy_button.clicked.connect(self.open_copy)
        self.sign_button = QPushButton('Подтвердить подписание'); self.sign_button.clicked.connect(self.sign)
        for button in (self.add_button,self.open_button,self.copy_button,self.sign_button): actions.addWidget(button)
        layout.addLayout(actions)
        self.table = make_table(('Номер','Дата','Вид','Группа','Программа','ФИО','Состояние')); layout.addWidget(self.table)
        self.table.itemSelectionChanged.connect(self.selection_changed); self.table.cellDoubleClicked.connect(lambda:self.open_selected())
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.recovery = QLabel(); layout.addWidget(self.recovery)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(180); self.timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda:self.timer.start()); self.setEnabled(repository is not None)
        if repository:
            try:
                count = repository.recover_unregistered()
                if count: self.recovery.setText(f'Файлов без регистрации: {count}; сохранены в exports/unregistered-completion-orders.')
            except ERRORS: self.recovery.setText('Не удалось проверить файлы без регистрации.')
        self.refresh()

    def showEvent(self,event): super().showEvent(event); self.refresh()

    def refresh(self, select_id=None):
        try:
            rows = self.repository.search(self.search.text()) if self.repository else []
            fill_table(self.table,[(*r[:-1],STATES[r[-1]]) for r in rows])
            for i in range(self.table.rowCount()):
                if self.table.item(i,0).data(Qt.ItemDataRole.UserRole) == select_id: self.table.selectRow(i)
            self.status.setText(f'Приказов о завершении: {len(rows)}')
        except ERRORS: fill_table(self.table,[]); self.status.setText('Не удалось прочитать приказы о завершении.')
        self.selection_changed()

    def selection_changed(self):
        key = selected_id(self.table); item = self.table.item(self.table.currentRow(),6) if key else None
        self.open_button.setEnabled(key is not None)
        self.sign_button.setEnabled(bool(item and item.text() == STATES['registered']))
        self.copy_button.setEnabled(bool(item and item.text() != STATES['draft']))

    def open_selected(self):
        key = selected_id(self.table)
        if key: self.open_card(key)

    def open_card(self, order_id=None, *, training_ids=None, group_id=None):
        try:
            order = self.repository.load(order_id) if order_id else None
            if order and order.state != 'draft':
                c = json.loads(order.snapshot)['context']
                lines = [f'{order.number} от {order.order_date} · {STATES[order.state]}',c['program']['name'],c['group']['name'],
                    *[f"{s['full_name']} · план {s['start_date']} — {s['end_date']} · завершение {s['completion_date']} · {s['completion_basis']} · {s['attestation_result']} · {s['qualification']} · {s['qualification_grade']}" for s in c['students']],
                    'Примечание: ' + order.note,'Регистрация (UTC): ' + order.registered_at,'Подписание: ' + order.signed_date,
                    'Подтверждение (UTC): ' + order.confirmed_at,'Ответственный: ' + order.confirmed_by,
                    'Проверка условий: ' + ('подтверждена' if order.conditions_checked else 'не подтверждена')]
                dialog = QDialog(self); dialog.setWindowTitle('Зарегистрированный приказ о завершении'); dialog.resize(850,500)
                layout = QVBoxLayout(dialog); text = QPlainTextEdit('\n'.join(lines)); text.setReadOnly(True); layout.addWidget(text)
                close = QPushButton('Закрыть'); close.clicked.connect(dialog.accept); layout.addWidget(close); dialog.exec(); return
            dialog = CompletionDialog(self.repository,order,training_ids=training_ids,group_id=group_id,parent=self); dialog.exec()
            self.refresh(dialog.saved_order.id if dialog.saved_order else order_id)
            if dialog.go_to_templates: self.templates_requested.emit()
        except ERRORS as error: warn(self,error)

    def open_copy(self):
        key = selected_id(self.table)
        if key:
            try: open_local_docx(self.repository.document_copy(key))
            except ERRORS as error: warn(self,error)

    def sign(self):
        key = selected_id(self.table)
        if key:
            try: CompletionSigningDialog(self.repository,self.repository.load(key),self).exec(); self.refresh(key)
            except ERRORS as error: warn(self,error)
