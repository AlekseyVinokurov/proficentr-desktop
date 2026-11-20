"""Оформление протоколов и явное подтверждение фактического подписания."""
from dataclasses import asdict, replace
from datetime import date
import json
import sqlite3

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QTabWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from proficentr.attestations import AttestationProtocol, AttestationResult, KINDS, RESULTS, STATES
from proficentr.ui.groups_page import make_table, fill_table, selected_id
from proficentr.ui.templates_page import open_local_docx
from proficentr.ui.training_dialogs import confirm

ERRORS = (ValueError, OSError, sqlite3.Error)


def warn(parent, error):
    QMessageBox.warning(parent, 'Протокол аттестации', str(error) if isinstance(error, ValueError) else 'Проверьте доступность базы и хранилища.')


class ProtocolDialog(QDialog):
    def __init__(self, repository, protocol=None, *, training_ids=None, group_id=None, parent=None):
        super().__init__(parent)
        self.repository = repository
        ids = list(training_ids or [])
        direction = 'dpo'
        if ids:
            labels = repository.training_labels(ids)
            if labels: direction = json.loads(labels[0][2])['direction']
        elif group_id:
            available = repository.available_trainings(group_id=group_id)
            if available: direction = json.loads(available[0][2])['direction']
        self.original = protocol or AttestationProtocol(protocol_date=date.today().isoformat(), attestation_date=date.today().isoformat(), direction=direction, group_id=group_id)
        self.saved_protocol = None
        self.review = None
        self.setWindowTitle('Протокол аттестации'); self.resize(1080, 760)
        layout = QVBoxLayout(self)
        hint = QLabel('Регистрация фиксирует документ. Результаты становятся подтверждёнными после отдельного подтверждения фактического подписания. Форму и правила аттестации определяют применимые требования и локальные акты организации.')
        hint.setWordWrap(True); layout.addWidget(hint)
        tabs = QTabWidget(); layout.addWidget(tabs)
        details = QWidget(); form = QFormLayout(details)
        self.day = QLineEdit(self.original.protocol_date)
        self.attestation_day = QLineEdit(self.original.attestation_date)
        self.direction = QComboBox()
        for key, label in KINDS.items(): self.direction.addItem(('ДПО — ' if key == 'dpo' else 'ПО — ') + label, key)
        self.direction.setCurrentIndex(self.direction.findData(self.original.direction))
        self.group = QComboBox(); self.group.addItem('Индивидуальный протокол', None)
        for key, name in repository.groups(): self.group.addItem(name, key)
        self.group.setCurrentIndex(max(0, self.group.findData(self.original.group_id)))
        self.historical = QLineEdit(self.original.historical_number)
        self.chair = QLineEdit(self.original.chair)
        self.members = QPlainTextEdit(self.original.members); self.members.setMaximumHeight(80)
        self.note = QPlainTextEdit(self.original.note); self.note.setMaximumHeight(80)
        self.template = QComboBox()
        for label, widget in (('Дата протокола (ГГГГ-ММ-ДД) *',self.day), ('Дата аттестации *',self.attestation_day),
                ('Направление и вид',self.direction), ('Группа / индивидуально',self.group), ('Исторический номер',self.historical),
                ('Председатель комиссии',self.chair), ('Состав комиссии',self.members), ('Примечание',self.note), ('Активный шаблон',self.template)):
            widget.setAccessibleName(label); form.addRow(label,widget)
        help_text = QLabel('Формы protocol_dpo / protocol_po загружаются через «Настройки → Шаблоны документов». Оценка, квалификация и разряд заполняются при необходимости.')
        help_text.setWordWrap(True); form.addRow(help_text); tabs.addTab(details,'Протокол')
        roster = QWidget(); content = QVBoxLayout(roster)
        self.query = QLineEdit(); self.query.setPlaceholderText('Поиск зачисленных обучений по ФИО'); content.addWidget(self.query)
        self.available = make_table(('ФИО','Программа','Начало','Окончание','Группа'))
        self.available.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); content.addWidget(self.available)
        add = QPushButton('Добавить выбранные обучения (Ctrl / Shift)'); add.clicked.connect(self.add_trainings); content.addWidget(add)
        self.results_table = make_table(('ФИО / программа / даты','Результат','Оценка','Квалификация / профессия / должность','Разряд / категория','Примечание'))
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.results_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection); content.addWidget(self.results_table)
        remove = QPushButton('Убрать выделенные из протокола'); remove.clicked.connect(self.remove_trainings); content.addWidget(remove)
        tabs.addTab(roster,'Слушатели и результаты')
        self.status = QLabel(); self.status.setWordWrap(True); self.status.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.save_button = QPushButton('Сохранить черновик'); self.save_button.clicked.connect(self.save)
        self.preview_button = QPushButton('Проект DOCX'); self.preview_button.clicked.connect(self.preview)
        self.register_button = QPushButton('Зарегистрировать'); self.register_button.clicked.connect(self.register)
        close = QPushButton('Закрыть'); close.clicked.connect(self.reject)
        for button in (self.save_button,self.preview_button,self.register_button,close): actions.addWidget(button)
        layout.addLayout(actions)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(180); self.timer.timeout.connect(self.refresh_available)
        self.query.textChanged.connect(lambda: self.timer.start())
        for widget in (self.day,self.attestation_day,self.historical,self.chair): widget.textChanged.connect(self.changed)
        self.members.textChanged.connect(self.changed); self.note.textChanged.connect(self.changed)
        self.template.currentIndexChanged.connect(self.changed); self.results_table.itemChanged.connect(self.changed)
        self.direction.currentIndexChanged.connect(self.selection_changed); self.group.currentIndexChanged.connect(self.selection_changed)
        initial = repository.results(protocol.id) if protocol else [AttestationResult(key) for key in ids]
        self.set_results(initial)
        self.refresh_available(); self.refresh_templates(self.original.template_id); self.changed()
        self._initial_state = self._state()

    def results(self):
        rows = []
        for i in range(self.results_table.rowCount()):
            key = self.results_table.item(i,0).data(Qt.ItemDataRole.UserRole)
            rows.append(AttestationResult(key, self.results_table.cellWidget(i,1).currentData(),
                *[self.results_table.item(i,j).text() for j in range(2,6)]))
        return rows

    def set_results(self, results):
        labels = {row[0]: row for row in self.repository.training_labels([r.training_id for r in results])}
        self.results_table.setRowCount(0); self.results_table.setRowCount(len(results))
        for i, result in enumerate(results):
            row = labels[result.training_id]
            text = f'{row[1]} · {json.loads(row[2])["full_name"]} · {row[3]} — {row[4]}'
            item = QTableWidgetItem(text); item.setData(Qt.ItemDataRole.UserRole,result.training_id)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable); self.results_table.setItem(i,0,item)
            combo = QComboBox()
            for key, label in RESULTS.items(): combo.addItem(label,key)
            combo.setCurrentIndex(combo.findData(result.result)); combo.currentIndexChanged.connect(self.changed)
            self.results_table.setCellWidget(i,1,combo)
            for j, field in enumerate(('grade','qualification','qualification_grade','note'),start=2):
                self.results_table.setItem(i,j,QTableWidgetItem(getattr(result,field)))

    def _state(self):
        return (self.day.text(), self.attestation_day.text(), self.direction.currentData(), self.group.currentData(),
                self.historical.text(), self.chair.text(), self.members.toPlainText(), self.note.toPlainText(),
                self.template.currentData(), [asdict(r) for r in self.results()])

    def is_dirty(self):
        return self._state() != self._initial_state

    def changed(self, *args):
        self.review = None; self.register_button.setEnabled(False)

    def values(self):
        return replace(self.original, protocol_date=self.day.text(), attestation_date=self.attestation_day.text(),
            direction=self.direction.currentData(), group_id=self.group.currentData(), historical_number=self.historical.text(),
            chair=self.chair.text(), members=self.members.toPlainText(), note=self.note.toPlainText(), template_id=self.template.currentData())

    def selection_changed(self):
        self.refresh_available(); self.refresh_templates(); self.changed()

    def refresh_available(self):
        try:
            rows = self.repository.available_trainings(self.query.text(),self.group.currentData())
            rows = [r for r in rows if json.loads(r[2])['direction'] == self.direction.currentData() and (self.group.currentData() is not None or r[5] is None)]
            fill_table(self.available,[(key,name,json.loads(snapshot)['full_name'],start,end,group or 'Индивидуально') for key,name,snapshot,start,end,group in rows])
        except ERRORS as error: warn(self,error)

    def refresh_templates(self, previous=None):
        if previous is None: previous = self.template.currentData()
        self.template.clear(); self.template.addItem('Выберите активную форму',None)
        versions = self.repository.suitable_templates(self.values())
        for t in versions: self.template.addItem(f'{t.name} · {t.variant} · версия {t.version}',t.id)
        self.template.setCurrentIndex(max(0,self.template.findData(previous)))
        if previous is None and len(versions) == 1: self.template.setCurrentIndex(1)

    def add_trainings(self):
        results = self.results(); ids = {r.training_id for r in results}
        for index in self.available.selectionModel().selectedRows():
            key = self.available.item(index.row(),0).data(Qt.ItemDataRole.UserRole)
            if key not in ids: results.append(AttestationResult(key)); ids.add(key)
        self.set_results(results); self.changed()

    def remove_trainings(self):
        selected = {i.row() for i in self.results_table.selectionModel().selectedRows()}
        self.set_results([r for i,r in enumerate(self.results()) if i not in selected]); self.changed()

    def save(self):
        try:
            self.original = self.repository.save(self.values(),self.results()); self.saved_protocol = self.original
            self._initial_state = self._state(); self.changed(); self.status.setText('Черновик сохранён без номера.'); return True
        except ERRORS as error: warn(self,error); return False

    def preview(self):
        if (self.original.id is None or self.is_dirty()) and not self.save(): return False
        try:
            if self.repository.load(self.original.id).revision != self.original.revision:
                raise ValueError('Черновик изменён в другом окне. Откройте его заново.')
            path,self.review = self.repository.preview(self.original.id)
            open_local_docx(path); self.register_button.setEnabled(True); self.status.setText('Проверьте проект перед регистрацией.\n' + str(path)); return True
        except ERRORS as error: warn(self,error); return False

    def register(self):
        if self.review is None or self.is_dirty(): return False
        message = 'Проект проверен? Присвоить номер и сохранить неизменяемые документ и результаты? Подписание подтверждается отдельно.'
        if any(not r.result for r in self.results()):
            message += '\nЕсть незаполненные результаты: такой протокол нельзя подтвердить как подписанный. После регистрации исправление потребует нового протокола.'
        if not confirm(self,'Регистрация протокола',message): return False
        try:
            self.saved_protocol = self.repository.register(self.original.id,self.review.signature)
            self.original = self.saved_protocol; self.accept(); return True
        except ERRORS as error: self.changed(); warn(self,error); return False

    def reject(self):
        if self.is_dirty():
            answer = QMessageBox.question(self,'Несохранённые изменения','Сохранить черновик?',QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Save)
            if answer == QMessageBox.StandardButton.Save:
                if not self.save(): return
            elif answer != QMessageBox.StandardButton.Discard: return
        super().reject()


class ProtocolSigningDialog(QDialog):
    def __init__(self, repository, protocol, parent=None):
        super().__init__(parent); self.repository = repository; self.protocol = protocol
        self.setWindowTitle('Подтверждение подписания протокола')
        form = QFormLayout(self)
        label = QLabel(f'{protocol.number} от {protocol.protocol_date}\nАттестация: {protocol.attestation_date}')
        label.setTextFormat(Qt.TextFormat.PlainText); form.addRow(label)
        self.day = QLineEdit(date.today().isoformat()); form.addRow('Фактическая дата подписания *',self.day)
        self.responsible = QLineEdit(); form.addRow('Ответственный (ФИО) *',self.responsible)
        self.signed = QCheckBox('Подтверждаю фактическое подписание протокола'); form.addRow(self.signed)
        button = QPushButton('Подтвердить подписание'); button.clicked.connect(self.confirm); form.addRow(button)
        close = QPushButton('Отмена'); close.clicked.connect(self.reject); form.addRow(close)

    def confirm(self):
        if not self.signed.isChecked(): warn(self,ValueError('Явно подтвердите фактическое подписание.')); return False
        try:
            self.repository.confirm_signing(self.protocol.id,signed_date=self.day.text(),confirmed_by=self.responsible.text())
            self.accept(); return True
        except ERRORS as error: warn(self,error); return False


class AttestationsPage(QWidget):
    def __init__(self, repository=None):
        super().__init__(); self.repository = repository
        layout = QVBoxLayout(self)
        self.search = QLineEdit(); self.search.setPlaceholderText('Поиск по номеру, ФИО, программе, датам и результату'); layout.addWidget(self.search)
        actions = QHBoxLayout()
        self.add_button = QPushButton('Создать протокол'); self.add_button.clicked.connect(lambda: self.open_card())
        self.open_button = QPushButton('Открыть'); self.open_button.clicked.connect(self.open_selected)
        self.copy_button = QPushButton('Открыть копию DOCX'); self.copy_button.clicked.connect(self.open_copy)
        self.sign_button = QPushButton('Подтвердить подписание'); self.sign_button.clicked.connect(self.sign)
        for button in (self.add_button,self.open_button,self.copy_button,self.sign_button): actions.addWidget(button)
        layout.addLayout(actions)
        self.table = make_table(('Номер','Дата протокола','Дата аттестации','Вид','Программа','ФИО','Результаты','Состояние')); layout.addWidget(self.table)
        self.table.itemSelectionChanged.connect(self.selection_changed); self.table.cellDoubleClicked.connect(lambda: self.open_selected())
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.recovery = QLabel(); layout.addWidget(self.recovery)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(180); self.timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.timer.start()); self.setEnabled(repository is not None)
        if repository:
            try:
                count = repository.recover_unregistered()
                if count: self.recovery.setText(f'Файлов без регистрации: {count}; сохранены в exports/unregistered-protocols.')
            except ERRORS: self.recovery.setText('Не удалось проверить файлы без регистрации.')
        self.refresh()

    def showEvent(self,event):
        super().showEvent(event); self.refresh()

    def refresh(self,select_id=None):
        try:
            rows = self.repository.search(self.search.text()) if self.repository else []
            fill_table(self.table,[(*r[:-1],STATES[r[-1]]) for r in rows])
            for i in range(self.table.rowCount()):
                if self.table.item(i,0).data(Qt.ItemDataRole.UserRole) == select_id: self.table.selectRow(i)
            self.status.setText(f'Протоколов: {len(rows)}')
        except ERRORS: fill_table(self.table,[]); self.status.setText('Не удалось прочитать протоколы.')
        self.selection_changed()

    def selection_changed(self):
        key = selected_id(self.table); item = self.table.item(self.table.currentRow(),7) if key else None
        self.open_button.setEnabled(key is not None)
        self.sign_button.setEnabled(bool(item and item.text() == STATES['registered']))
        self.copy_button.setEnabled(bool(item and item.text() != STATES['draft']))

    def open_selected(self):
        key = selected_id(self.table)
        if key: self.open_card(key)

    def open_card(self,protocol_id=None,*,training_ids=None,group_id=None):
        try:
            protocol = self.repository.load(protocol_id) if protocol_id else None
            if protocol and protocol.state != 'draft':
                c = json.loads(protocol.snapshot)['context']
                lines = [f'{protocol.number} от {protocol.protocol_date} · {STATES[protocol.state]}',
                    f'{protocol.kind} · {protocol.attestation_date}', c['program']['name'], c['group']['name'],
                    'Председатель: ' + protocol.chair, 'Комиссия: ' + protocol.members, 'Примечание: ' + protocol.note,
                    *[f"{s['full_name']} · {s['start_date']} — {s['end_date']} · {s['attestation_result'] or RESULTS['']} · {s['grade']} · {s['qualification']} · {s['qualification_grade']} · {s['attestation_note']}" for s in c['students']],
                    'Регистрация (UTC): ' + protocol.registered_at, 'Дата подписания: ' + protocol.signed_date,
                    'Подтверждение (UTC): ' + protocol.confirmed_at, 'Ответственный: ' + protocol.confirmed_by]
                dialog = QDialog(self); dialog.setWindowTitle('Зарегистрированный протокол'); dialog.resize(850,500)
                layout = QVBoxLayout(dialog); text = QPlainTextEdit('\n'.join(lines)); text.setReadOnly(True); layout.addWidget(text)
                close = QPushButton('Закрыть'); close.clicked.connect(dialog.accept); layout.addWidget(close); dialog.exec(); return
            dialog = ProtocolDialog(self.repository,protocol,training_ids=training_ids,group_id=group_id,parent=self); dialog.exec()
            self.refresh(dialog.saved_protocol.id if dialog.saved_protocol else protocol_id)
        except ERRORS as error: warn(self,error)

    def open_copy(self):
        key = selected_id(self.table)
        if key:
            try: open_local_docx(self.repository.document_copy(key))
            except ERRORS as error: warn(self,error)

    def sign(self):
        key = selected_id(self.table)
        if key:
            try: ProtocolSigningDialog(self.repository,self.repository.load(key),self).exec(); self.refresh(key)
            except ERRORS as error: warn(self,error)
