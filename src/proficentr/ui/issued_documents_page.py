"""Выпускные документы: по одному файлу на обучение и отдельная передача."""
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import sqlite3

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (QAbstractItemView,QCheckBox,QComboBox,QDialog,QFileDialog,QFormLayout,
    QHBoxLayout,QLabel,QLineEdit,QMessageBox,QPlainTextEdit,QPushButton,QTableWidgetItem,QVBoxLayout,QWidget)
from proficentr.document_delivery import DeliveryRepository
from proficentr.issued_documents import KINDS, STATES, METHODS, TransferEvent
from proficentr.ui.groups_page import make_table,fill_table,selected_id
from proficentr.ui.templates_page import open_local_docx
from proficentr.ui.training_dialogs import confirm

ERRORS=(ValueError,OSError,sqlite3.Error)


def warn(parent,error):QMessageBox.warning(parent,'Выпускные документы',str(error) if isinstance(error,ValueError) else 'Проверьте доступность базы и хранилища.')


class CompletedPicker(QDialog):
    def __init__(self, repository, *, group_id=None, parent=None):
        super().__init__(parent);self.repository=repository;self.group_id=group_id;self.training_ids=[]
        self.setWindowTitle('Завершённые обучения');self.resize(1050,620)
        layout=QVBoxLayout(self)
        self.pending=QCheckBox('Только без зарегистрированного выпускного документа');self.pending.setChecked(True);layout.addWidget(self.pending)
        self.query=QLineEdit();self.query.setPlaceholderText('Поиск по ФИО или программе');layout.addWidget(self.query)
        self.table=make_table(('ФИО','Вид документа','Программа','Завершение','Состояние оформления'))
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection);layout.addWidget(self.table)
        hint=QLabel('Выберите одну или несколько записей (Ctrl / Shift). Для каждого слушателя будет отдельный DOCX и свои реквизиты.');hint.setWordWrap(True);layout.addWidget(hint)
        choose=QPushButton('Подготовить выбранные');choose.clicked.connect(self.choose);layout.addWidget(choose)
        close=QPushButton('Закрыть');close.clicked.connect(self.reject);layout.addWidget(close)
        self.pending.toggled.connect(self.refresh);self.query.textChanged.connect(self.refresh);self.refresh()

    def refresh(self):
        try:
            query=self.query.text().strip().casefold()
            rows=self.repository.candidates(self.group_id,pending_only=self.pending.isChecked())
            fill_table(self.table,[r[:6] for r in rows if not query or query in (r[1]+' '+r[3]).casefold()])
        except ERRORS as error:warn(self,error)

    def choose(self):
        self.training_ids=[self.table.item(i.row(),0).data(Qt.ItemDataRole.UserRole) for i in self.table.selectionModel().selectedRows()]
        if self.training_ids:self.accept()


class IssueBatchDialog(QDialog):
    def __init__(self, repository, training_ids, parent=None, *, documents=None):
        super().__init__(parent);self.repository=repository;self.documents=documents if documents is not None else [repository.draft_for(key) for key in dict.fromkeys(training_ids)]
        self.reviews={};self.paths={};self.go_to_templates=False
        self.setWindowTitle('Оформление выпускных документов');self.resize(1180,650)
        layout=QVBoxLayout(self)
        hint=QLabel('Проверьте дату и бланк каждого документа. Тип, ФИО и программа взяты из исторического завершения. Снимите флажок для исключения строки. Регистрация не означает передачи оригинала.');hint.setWordWrap(True);layout.addWidget(hint)
        self.table=make_table(('Включить','ФИО / программа / вид','Завершение','Дата документа','Серия бланка','Номер бланка','Исторический номер','Активная форма','Результат операции'))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked|QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setRowCount(len(self.documents));layout.addWidget(self.table)
        for i,d in enumerate(self.documents):
            check=QCheckBox();check.setChecked(d.state=='draft');check.setEnabled(d.state=='draft');self.table.setCellWidget(i,0,check)
            for j,value in ((1,f'{d.full_name} · {d.program_name} · {KINDS[d.kind]}'),(2,d.completion_date),(3,d.document_date),(4,d.blank_series),(5,d.blank_number),(6,d.historical_number),(8,('Зарегистрирован: '+d.number) if d.number else 'Черновик')):
                item=QTableWidgetItem(value);item.setToolTip(value)
                if j in (1,2,8) or d.state!='draft':item.setFlags(item.flags()&~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(i,j,item)
            combo=QComboBox();combo.addItem('Выберите форму',None)
            for t in repository.suitable_templates(d):combo.addItem(f'{t.name} · {t.variant} · v{t.version}',t.id)
            combo.setCurrentIndex(max(0,combo.findData(d.template_id)))
            if d.template_id is None and combo.count()==2:combo.setCurrentIndex(1)
            combo.setEnabled(d.state=='draft');self.table.setCellWidget(i,7,combo);combo.currentIndexChanged.connect(self.changed)
        self.table.itemChanged.connect(self.changed)
        actions=QHBoxLayout()
        for title,method in (('Сохранить черновики',self.save),('Подготовить проекты DOCX',self.preview),('Открыть проект выбранной строки',self.open_preview),('Зарегистрировать подготовленные',self.register)):
            button=QPushButton(title);button.clicked.connect(method);actions.addWidget(button)
        layout.addLayout(actions)
        more=QHBoxLayout();templates=QPushButton('Настройки шаблонов');templates.clicked.connect(self.open_templates);more.addWidget(templates)
        close=QPushButton('Закрыть');close.clicked.connect(self.reject);more.addWidget(close);layout.addLayout(more)
        self._saved=self.fingerprint()

    def fingerprint(self):
        return tuple((self.table.cellWidget(i,0).isChecked(),*[self.table.item(i,j).text() for j in (3,4,5,6)],self.table.cellWidget(i,7).currentData()) for i in range(len(self.documents)))

    def changed(self,*args):self.reviews.clear()

    def selected_rows(self):return [i for i,d in enumerate(self.documents) if self.table.cellWidget(i,0).isChecked() and d.state=='draft']

    def status(self,i,text):
        self.table.blockSignals(True);self.table.item(i,8).setText(text);self.table.item(i,8).setToolTip(text);self.table.blockSignals(False)

    def _save_row(self,i):
        d=self.documents[i]
        values=replace(d,document_date=self.table.item(i,3).text(),blank_series=self.table.item(i,4).text(),blank_number=self.table.item(i,5).text(),historical_number=self.table.item(i,6).text(),template_id=self.table.cellWidget(i,7).currentData())
        self.documents[i]=self.repository.save(values);self.status(i,'Черновик сохранён')

    def save(self):
        ok=True;self.reviews.clear()
        for i in self.selected_rows():
            try:self._save_row(i)
            except ERRORS as error:self.status(i,str(error));ok=False
        if ok:self._saved=self.fingerprint()
        return ok

    def preview(self):
        self.reviews.clear();ok=True
        for i in self.selected_rows():
            try:
                self._save_row(i);path,review=self.repository.preview(self.documents[i].id)
                self.paths[i]=path;self.reviews[i]=review.signature;self.status(i,'Проект: '+str(path))
            except ERRORS as error:self.status(i,str(error));ok=False
        self._reviewed=self.fingerprint()
        if ok:self._saved=self.fingerprint()
        return ok

    def open_preview(self):
        i=self.table.currentRow()
        if i in self.paths:
            try:open_local_docx(self.paths[i])
            except ERRORS as error:warn(self,error)

    def register(self):
        if not self.reviews or self.fingerprint()!=getattr(self,'_reviewed',None):return False
        if not confirm(self,'Регистрация выпускных документов','Проекты проверены? Зарегистрировать отдельные документы? При частичной ошибке результаты будут показаны по каждой строке.'):return False
        indexes=[i for i in self.selected_rows() if i in self.reviews]
        results=self.repository.register_batch([(self.documents[i].id,self.reviews[i]) for i in indexes])
        for i,(_,status,key,error) in zip(indexes,results):
            if status=='error':self.status(i,'Не оформлен: '+error);continue
            self.documents[i]=self.repository.load(key);self.status(i,'Зарегистрирован: '+self.documents[i].number)
            self.table.cellWidget(i,0).setChecked(False);self.table.cellWidget(i,0).setEnabled(False)
            for j in (3,4,5,6):self.table.item(i,j).setFlags(self.table.item(i,j).flags()&~Qt.ItemFlag.ItemIsEditable)
            self.table.cellWidget(i,7).setEnabled(False)
        self.reviews.clear()
        if not self.selected_rows():self._saved=self.fingerprint()
        return all(r[1]!='error' for r in results)

    def reject(self):
        if self.fingerprint()!=self._saved:
            answer=QMessageBox.question(self,'Несохранённые реквизиты','Сохранить черновики?',QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Save)
            if answer==QMessageBox.StandardButton.Save:
                if not self.save():return False
            elif answer!=QMessageBox.StandardButton.Discard:return False
        super().reject();return True

    def open_templates(self):
        if self.reject():self.go_to_templates=True


class TransferDialog(QDialog):
    def __init__(self, delivery, document, parent=None):
        super().__init__(parent);self.delivery=delivery;self.document=document;self.saved_event=None;self.evidence=None
        self.setWindowTitle('Запись фактически выполненной передачи');self.resize(740,700)
        form=QFormLayout(self);student=json.loads(document.snapshot)['context']['student']
        self.method=QComboBox()
        for key,label in METHODS.items():self.method.addItem(label,key)
        self.day=QLineEdit(date.today().isoformat());self.recipient=QLineEdit(document.full_name)
        self.confirmation=QLineEdit();self.address=QLineEdit(student['residential_address'] or student['registration_address']);self.email=QLineEdit(student['email'])
        self.tracking=QLineEdit();self.received=QLineEdit(date.today().isoformat())
        self.postal=QComboBox();self.postal.addItem('Отправлено','sent');self.postal.addItem('Вручение не подтверждено','unconfirmed');self.postal.addItem('Получение копии подтверждено','confirmed')
        self.parent_event=QComboBox();self.parent_event.addItem('Выберите исходную отправку',None)
        for e in delivery.history(document.id):
            if e.method=='post':self.parent_event.addItem(f'{e.event_date} · {e.recipient} · {e.tracking_number}',e.id)
        self.note=QPlainTextEdit();self.note.setMaximumHeight(70)
        for caption,widget in (('Способ',self.method),('Дата события (ГГГГ-ММ-ДД)',self.day),('Получатель',self.recipient),
            ('Способ/сведения подтверждения',self.confirmation),('Использованный адрес',self.address),('Email получателя',self.email),
            ('Почтовый идентификатор',self.tracking),('Состояние отправки',self.postal),('Исходная почтовая отправка',self.parent_event),('Дата вручения',self.received),('Примечание',self.note)):
            widget.setAccessibleName(caption);form.addRow(caption,widget)
        hint=QLabel('Фиксируется выполненное вручную действие. Email передаёт электронную копию. Наличие трек-номера не подтверждает вручение оригинала.');hint.setWordWrap(True);form.addRow(hint)
        self.file_label=QLabel('Подтверждение не выбрано');self.file_label.setTextFormat(Qt.TextFormat.PlainText);form.addRow(self.file_label)
        self.evidence_type=QLineEdit('Подтверждение передачи');form.addRow('Вид подтверждения',self.evidence_type)
        attach=QPushButton('Выбрать PDF / JPG / PNG');attach.clicked.connect(self.pick);form.addRow(attach)
        save=QPushButton('Сохранить событие');save.clicked.connect(self.save);form.addRow(save)
        close=QPushButton('Отмена');close.clicked.connect(self.reject);form.addRow(close)
        self.method.currentIndexChanged.connect(self.method_changed);self.method_changed()

    def method_changed(self):
        method=self.method.currentData()
        for widget,enabled in ((self.address,method=='post'),(self.email,method=='email'),(self.tracking,method=='post'),
            (self.parent_event,method=='post_confirmation'),(self.received,method=='post_confirmation'),(self.postal,method in ('post','email'))):widget.setEnabled(enabled)

    def pick(self):
        path,_=QFileDialog.getOpenFileName(self,'Подтверждение','','Подтверждения (*.pdf *.png *.jpg *.jpeg)')
        if path:self.evidence=Path(path);self.file_label.setText(path)

    def save(self):
        method=self.method.currentData()
        event=TransferEvent(document_id=self.document.id,method=method,event_date=self.day.text(),recipient=self.recipient.text(),confirmation=self.confirmation.text(),
            address=self.address.text() if method=='post' else '',email=self.email.text() if method=='email' else '',tracking_number=self.tracking.text() if method=='post' else '',
            postal_status=self.postal.currentData() if method in ('post','email') else '',received_date=self.received.text() if method=='post_confirmation' else '',
            parent_event_id=self.parent_event.currentData() if method=='post_confirmation' else None,note=self.note.toPlainText())
        try:
            self.saved_event=self.delivery.add(event,evidence=self.evidence,evidence_type=self.evidence_type.text());self.accept();return True
        except ERRORS as error:warn(self,error);return False


class DeliveryHistoryDialog(QDialog):
    def __init__(self, delivery, document, parent=None):
        super().__init__(parent);self.delivery=delivery;self.document=document
        self.setWindowTitle('История передачи: '+document.number);self.resize(1100,700)
        layout=QVBoxLayout(self)
        self.table=make_table(('Способ','Дата','Получатель','Адрес / email','Трек-номер','Состояние','Вручение','Подтверждение / примечание'));layout.addWidget(self.table)
        self.table.itemSelectionChanged.connect(self.refresh_evidence)
        self.files=make_table(('Вид подтверждения','Формат','SHA-256'));layout.addWidget(self.files)
        actions=QHBoxLayout()
        for title,method in (('Добавить событие',self.add),('Прикрепить подтверждение',self.attach),('Сохранить копию подтверждения…',self.copy)):
            b=QPushButton(title);b.clicked.connect(method);actions.addWidget(b)
        layout.addLayout(actions)
        close=QPushButton('Закрыть');close.clicked.connect(self.accept);layout.addWidget(close);self.refresh()

    def refresh(self):
        fill_table(self.table,[(e.id,METHODS[e.method],e.event_date,e.recipient,e.address or e.email,e.tracking_number,{'sent':'Отправлено','unconfirmed':'Вручение не подтверждено','confirmed':'Подтверждено'}[e.postal_status],e.received_date,e.confirmation+' '+e.note) for e in self.delivery.history(self.document.id)])
        self.refresh_evidence()

    def refresh_evidence(self):
        key=selected_id(self.table)
        fill_table(self.files,[(key,kind,fmt,sha) for key,path,sha,kind,fmt in self.delivery.evidence(key)] if key else [])

    def add(self):
        TransferDialog(self.delivery,self.document,self).exec();self.refresh()

    def attach(self):
        key=selected_id(self.table)
        if not key:return
        path,_=QFileDialog.getOpenFileName(self,'Подтверждение','','Подтверждения (*.pdf *.png *.jpg *.jpeg)')
        if not path:return
        from PySide6.QtWidgets import QInputDialog
        kind,ok=QInputDialog.getText(self,'Вид подтверждения','Например: расписка, почтовая квитанция, подтверждение email')
        if ok:
            try:self.delivery.attach(key,Path(path),kind);self.refresh_evidence()
            except ERRORS as error:warn(self,error)

    def copy(self):
        key=selected_id(self.files)
        if not key:return
        try:
            copy=self.delivery.evidence_copy(key)
            destination,_=QFileDialog.getSaveFileName(self,'Сохранить проверенную копию',copy.name,'Подтверждение (*'+copy.suffix+')')
            if destination:
                from proficentr.settings import _require_local
                target=Path(destination).absolute();_require_local(target)
                if target.suffix.lower()!=copy.suffix:raise ValueError('Сохраните копию с исходным расширением.')
                with target.open('xb') as stream:stream.write(copy.read_bytes())
        except ERRORS as error:warn(self,error)


class IssuedDocumentsPage(QWidget):
    templates_requested=Signal()

    def __init__(self, repository=None):
        super().__init__();self.repository=repository;self.delivery=DeliveryRepository(repository) if repository else None
        layout=QVBoxLayout(self);self.search=QLineEdit();self.search.setPlaceholderText('Поиск по ФИО, номеру и программе');layout.addWidget(self.search)
        filters=QHBoxLayout();self.kind=QComboBox();self.kind.addItem('Все виды',None)
        for key,label in KINDS.items():self.kind.addItem(label,key)
        self.year=QLineEdit();self.year.setPlaceholderText('Год (все — пусто)');self.state=QComboBox();self.state.addItem('Все состояния',None)
        for key,label in STATES.items():self.state.addItem(label,key)
        for widget in (self.kind,self.year,self.state):filters.addWidget(widget)
        layout.addLayout(filters)
        actions=QHBoxLayout()
        self.add_button=QPushButton('Завершённые без документов / оформление');self.add_button.clicked.connect(lambda:self.open_training())
        self.open_button=QPushButton('Реквизиты / черновик');self.open_button.clicked.connect(self.open_selected)
        self.copy_button=QPushButton('Копия DOCX');self.copy_button.clicked.connect(self.open_copy)
        self.delivery_button=QPushButton('Передача и подтверждения');self.delivery_button.clicked.connect(self.open_delivery)
        for b in (self.add_button,self.open_button,self.copy_button,self.delivery_button):actions.addWidget(b)
        layout.addLayout(actions)
        self.table=make_table(('ФИО','Вид','Номер','Дата','Программа','Оформление','Передача оригинала','Электронная копия'));layout.addWidget(self.table)
        self.table.itemSelectionChanged.connect(self.selection_changed);self.table.cellDoubleClicked.connect(lambda:self.open_selected())
        export=QHBoxLayout();self.start=QLineEdit(f'{date.today().year}-01-01');self.end=QLineEdit(date.today().isoformat())
        export.addWidget(QLabel('Период внутреннего XLSX:'));export.addWidget(self.start);export.addWidget(self.end)
        button=QPushButton('Экспорт XLSX');button.clicked.connect(self.export);export.addWidget(button);layout.addLayout(export)
        self.status=QLabel();self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status)
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(180);self.timer.timeout.connect(self.refresh)
        for field in (self.search,self.year):field.textChanged.connect(lambda:self.timer.start())
        for combo in (self.kind,self.state):combo.currentIndexChanged.connect(self.refresh)
        self.setEnabled(repository is not None)
        if repository:
            try:repository.recover_unregistered();self.delivery.recover_unregistered()
            except ERRORS:self.status.setText('Не удалось проверить непривязанные файлы. Повторите после восстановления доступа к хранилищу.')
        self.refresh()

    def showEvent(self,event):super().showEvent(event);self.refresh()

    def refresh(self,*args,select_id=None):
        try:
            year=self.year.text().strip()
            if year and (not year.isdigit() or len(year)!=4):raise ValueError('Год: введите четыре цифры или оставьте поле пустым.')
            rows=self.repository.search(self.search.text(),kind=self.kind.currentData(),year=int(year) if year else None,state=self.state.currentData()) if self.repository else []
            fill_table(self.table,[(*r[:6],STATES[r[6]],*r[7:]) for r in rows])
            for i in range(self.table.rowCount()):
                if self.table.item(i,0).data(Qt.ItemDataRole.UserRole)==select_id:self.table.selectRow(i)
            self.status.setText(f'Выпускных документов: {len(rows)}')
        except ERRORS as error:fill_table(self.table,[]);self.status.setText(str(error))
        self.selection_changed()

    def selection_changed(self):
        key=selected_id(self.table);item=self.table.item(self.table.currentRow(),5) if key else None
        self.open_button.setEnabled(key is not None)
        for b in (self.copy_button,self.delivery_button):b.setEnabled(bool(item and item.text()==STATES['registered']))

    def open_training(self,*,training_ids=None,group_id=None):
        try:
            if not training_ids:
                picker=CompletedPicker(self.repository,group_id=group_id,parent=self)
                if picker.exec()!=QDialog.DialogCode.Accepted:return
                training_ids=picker.training_ids
            dialog=IssueBatchDialog(self.repository,training_ids,self);dialog.exec();self.refresh()
            if dialog.go_to_templates:self.templates_requested.emit()
        except ERRORS as error:warn(self,error)

    def open_selected(self):
        key=selected_id(self.table)
        if not key:return
        try:
            d=self.repository.load(key)
            if d.state=='draft':
                dialog=IssueBatchDialog(self.repository,[d.training_id],self,documents=[d]);dialog.exec();self.refresh()
                if dialog.go_to_templates:self.templates_requested.emit()
                return
            text=QPlainTextEdit('\n'.join([d.full_name,KINDS[d.kind],f'{d.number} от {d.document_date}',d.program_name,f'{d.hours} академических часов',
                f'Завершение: {d.completion_date}',f'Приказ: {d.completion_order_number} от {d.completion_order_date}',
                'Квалификация: '+d.qualification+' '+d.qualification_grade,'Бланк: '+d.blank_series+' / '+d.blank_number,'Регистрация (UTC): '+d.registered_at,*self.repository.delivery_summary(d.id)]));text.setReadOnly(True)
            dialog=QDialog(self);dialog.setWindowTitle('Зарегистрированный выпускной документ');dialog.resize(800,500)
            layout=QVBoxLayout(dialog);layout.addWidget(text);close=QPushButton('Закрыть');close.clicked.connect(dialog.accept);layout.addWidget(close);dialog.exec()
        except ERRORS as error:warn(self,error)

    def open_copy(self):
        key=selected_id(self.table)
        if key:
            try:open_local_docx(self.repository.document_copy(key))
            except ERRORS as error:warn(self,error)

    def open_delivery(self):
        key=selected_id(self.table)
        if key:
            try:DeliveryHistoryDialog(self.delivery,self.repository.load(key),self).exec();self.refresh(select_id=key)
            except ERRORS as error:warn(self,error)

    def export(self):
        try:
            path=self.repository.export_xlsx(self.start.text(),self.end.text(),kind=self.kind.currentData())
            QMessageBox.information(self,'Внутренний XLSX-реестр',str(path)+'\nЭто внутренний реестр, а не выгрузка ФИС ФРДО.')
        except ERRORS as error:warn(self,error)
