"""Комплект конкретного приёма и редактирование перечня без изменения кода."""
from dataclasses import replace
from datetime import date
import json
from uuid import uuid4
import sqlite3
from proficentr.training_repository import TrainingRepositoryError
from proficentr.student_repository import StudentRepositoryError

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox,QComboBox,QDialog,QFileDialog,QFormLayout,QHBoxLayout,QLabel,
    QLineEdit,QMessageBox,QPlainTextEdit,QPushButton,QScrollArea,QTabWidget,QVBoxLayout,QWidget)

from proficentr.admission import AdmissionRule, STATES, SOURCES, TYPES, BASES, EDUCATION_BASES
from proficentr.programs import KINDS
from proficentr.ui.groups_page import make_table,fill_table,selected_id
from proficentr.ui.training_dialogs import TrainingDialog
from proficentr.ui.templates_page import open_local_docx

ERRORS=(ValueError,OSError,sqlite3.Error,TrainingRepositoryError,StudentRepositoryError)


def choices(values,current=''):
    edit=QComboBox()
    for key,label in values.items():edit.addItem(label,key)
    edit.setCurrentIndex(max(0,edit.findData(current)))
    return edit


def line(form,label,value=''):
    edit=QLineEdit(str(value));edit.setAccessibleName(label);form.addRow(label,edit);return edit


def buttons(dialog,layout,callback):
    row=QHBoxLayout();save=QPushButton('Сохранить');save.clicked.connect(callback)
    cancel=QPushButton('Отмена');cancel.clicked.connect(dialog.reject)
    row.addWidget(save);row.addWidget(cancel);layout.addLayout(row)
    return save


def warn(parent,error):
    message='Операция не выполнена. Проверьте базу, доступ к файлам и свободное место.' if isinstance(error,(OSError,sqlite3.Error)) else str(error)
    QMessageBox.warning(parent,'Приём на обучение',message)


class AdmissionContextDialog(QDialog):
    def __init__(self,repository,training_id,parent=None):
        super().__init__(parent);self.repository=repository;self.training_id=training_id
        self.review=repository.review(training_id);c=repository.context(training_id)
        self.setWindowTitle('Основание обработки и условия приёма');layout=QVBoxLayout(self);form=QFormLayout()
        self.basis=choices(BASES,c['pd_basis']);form.addRow('Правовое основание обработки ПД',self.basis)
        self.reference=line(form,'Реквизиты основания (статья, договор, инициатива)',c['pd_reference'])
        self.scope=line(form,'Цели, данные и действия в рамках этого приёма',c['pd_scope'])
        self.flags={};flags=json.loads(c['conditions'])
        for tag in dict.fromkeys(r.condition_tag for r in repository.applicable(training_id) if r.condition_tag):
            edit=choices({'unknown':'Не определено','yes':'Применяется','no':'Не применяется'},'yes' if flags.get(tag) is True else 'no' if flags.get(tag) is False else 'unknown')
            self.flags[tag]=edit;form.addRow(tag,edit)
        self.note=line(form,'Основание неприменимости условий / примечание',c['note'])
        self.day=line(form,'Дата подтверждения',date.today().isoformat());self.responsible=line(form,'Ответственное лицо *')
        hint=QLabel('Наличие договора не выбирает основание автоматически. Дополнительные согласия имеют отдельные цели; отзыв согласия не прекращает обработку по самостоятельному законному основанию.')
        hint.setWordWrap(True);layout.addWidget(hint);layout.addLayout(form)
        self.save_button=buttons(self,layout,self.save)

    def save(self):
        try:
            flags={k:e.currentData()=='yes' for k,e in self.flags.items() if e.currentData()!='unknown'}
            self.repository.save_context(self.training_id,pd_basis=self.basis.currentData(),pd_reference=self.reference.text(),
                pd_scope=self.scope.text(),conditions=flags,note=self.note.text(),day=self.day.text(),responsible=self.responsible.text(),expected_signature=self.review.signature)
            self.accept();return True
        except ERRORS as error:warn(self,error);return False


class AdmissionRecordDialog(QDialog):
    def __init__(self,repository,training_id,item,parent=None):
        super().__init__(parent);self.repository=repository;self.training_id=training_id;self.item=item
        self.review=repository.review(training_id);self.rule=next(r for r in repository.applicable(training_id) if r.rule_key==item.key)
        self.setWindowTitle(item.title);layout=QVBoxLayout(self);form=QFormLayout()
        self.state=choices({k:v for k,v in STATES.items() if k!='prepared'},item.status if item.status!='prepared' else 'received')
        form.addRow('Состояние',self.state)
        self.education=choices(EDUCATION_BASES)
        if item.key=='dpo_education':form.addRow('Подтверждённое основание допуска к ДПО',self.education)
        self.reason=line(form,'Подтверждение / реквизиты / основание *',item.reason)
        self.day=line(form,'Дата документа / подтверждения',date.today().isoformat())
        self.responsible=line(form,'Ответственное лицо *')
        self.signed=QCheckBox('Подписание проверено отдельно от формирования DOCX')
        form.addRow(self.signed)
        self.until=line(form,'Действует до (необязательно, ГГГГ-ММ-ДД)')
        self.reusable=QCheckBox('Разрешено повторное использование в той же области действия')
        self.reusable.setEnabled(self.rule.reusable);form.addRow(self.reusable)
        self.source=QLineEdit();self.source.setReadOnly(True);form.addRow('Новое вложение (PDF/JPG/PNG)',self.source)
        pick=QPushButton('Выбрать подтверждение');pick.clicked.connect(self.pick_source);form.addRow(pick)
        self.new_document=QCheckBox('Зарегистрировать новое подтверждение; прежнее сохранить')
        self.new_document.setEnabled(item.document_id is not None);form.addRow(self.new_document)
        if item.document_id:
            event=repository.document_event(item.document_id)
            doc=repository.document(item.document_id)
            self.signed.setChecked(bool(event['signed']))
            self.education.setCurrentIndex(max(0,self.education.findData(event['education_basis'])))
            self.until.setText(doc['valid_until']);self.until.setEnabled(False)
            self.reusable.setChecked(bool(doc['reusable']));self.reusable.setEnabled(False)
            self.new_document.toggled.connect(self.until.setEnabled)
            self.new_document.toggled.connect(lambda active:self.reusable.setEnabled(active and self.rule.reusable))
            form.addRow(QLabel('Последнее событие документа: '+STATES.get(event['state'],'Отозвано')+' · '+event['day']))
        layout.addLayout(form);self.save_button=buttons(self,layout,self.save)

    def pick_source(self):
        path,_=QFileDialog.getOpenFileName(self,'Подтверждающий документ','','Подтверждения (*.pdf *.jpg *.jpeg *.png)')
        if path:self.source.setText(path);self.new_document.setChecked(True)

    def save(self):
        try:
            state=self.state.currentData()
            linked=self.item.document_id if not self.new_document.isChecked() and state in ('received','verified') else None
            self.repository.record(self.training_id,self.item.key,state=state,reason=self.reason.text(),day=self.day.text(),
                responsible=self.responsible.text(),signed=self.signed.isChecked(),valid_until=self.until.text(),education_basis=self.education.currentData(),
                reusable=self.reusable.isChecked(),source=self.source.text() or None,document_id=linked,expected_signature=self.review.signature)
            self.accept();return True
        except ERRORS as error:warn(self,error);return False


class AdmissionFormDialog(QDialog):
    def __init__(self,repository,training_id,item,parent=None):
        super().__init__(parent);self.repository=repository;self.training_id=training_id;self.item=item
        self.review=repository.review(training_id);self.document_id=None
        rule=next(r for r in repository.applicable(training_id) if r.rule_key==item.key)
        self.setWindowTitle('Сформировать и зарегистрировать неподписанный DOCX');layout=QVBoxLayout(self);form=QFormLayout()
        self.template=QComboBox()
        for t in repository.templates.list_versions(rule.document_type):
            if t.active and t.variant==rule.template_variant:self.template.addItem(f'{t.name} · версия {t.version}',t.id)
        form.addRow('Утверждённый шаблон',self.template)
        hint=QLabel(f'Шаблоны подключаются в «Настройки → Шаблоны документов»: тип {TYPES[rule.document_type]}, вариант {rule.template_variant}. Подключайте утверждённый отдельный документ; готовых юридических текстов приложение не содержит.')
        hint.setWordWrap(True);layout.addWidget(hint)
        self.until=line(form,'Действует до (необязательно, ГГГГ-ММ-ДД)')
        self.day=line(form,'Дата документа',date.today().isoformat());self.responsible=line(form,'Ответственное лицо *')
        layout.addLayout(form);self.save_button=buttons(self,layout,self.save)
        self.save_button.setEnabled(self.template.count()>0)

    def save(self):
        try:
            self.document_id=self.repository.prepare_form(self.training_id,self.item.key,template_id=self.template.currentData(),
                day=self.day.text(),responsible=self.responsible.text(),expected_signature=self.review.signature,valid_until=self.until.text())
            self.accept();return True
        except ERRORS as error:warn(self,error);return False


class AdmissionRuleDialog(QDialog):
    def __init__(self,repository,rule=None,parent=None):
        super().__init__(parent);self.repository=repository
        self.original=rule or AdmissionRule('r_'+uuid4().hex,'')
        self.setWindowTitle('Требование приёма — новая версия');layout=QVBoxLayout(self);form=QFormLayout()
        self.title=line(form,'Название *',self.original.title)
        kinds={'':'Все виды','dpo':'Все ДПО','po':'Все ПО'}
        for values in KINDS.values():kinds.update(values)
        self.kind=choices(kinds,self.original.program_kind);form.addRow('Вид обучения',self.kind)
        programs={'':'Все программы'}
        for key,name in repository.program_options():programs[key]=name
        self.program=choices(programs,self.original.program_id or '');form.addRow('Конкретная программа',self.program)
        self.tag=line(form,'Условие применимости (например, вредные факторы)',self.original.condition_tag)
        self.origin=choices(SOURCES,self.original.source);form.addRow('Источник требования',self.origin)
        self.reference=line(form,'Точный акт, статья, пункт / реквизиты программы *',self.original.reference)
        self.required=QCheckBox('Обязательно для зачисления');self.required.setChecked(self.original.required);form.addRow(self.required)
        self.typ=choices(TYPES,self.original.document_type);form.addRow('Тип',self.typ)
        self.signed=QCheckBox('Требуется отдельное подтверждение подписания');self.signed.setChecked(self.original.sign_required);form.addRow(self.signed)
        self.reusable=QCheckBox('Допустимо повторное использование действующего подтверждения');self.reusable.setChecked(self.original.reusable);form.addRow(self.reusable)
        self.scope=line(form,'Область действия / цель подтверждения',self.original.scope)
        self.variant=line(form,'Вариант утверждённого DOCX-шаблона',self.original.template_variant)
        self.waiver=QCheckBox('Допускается «не требуется» с основанием (кроме закона)');self.waiver.setChecked(self.original.waiver_allowed);form.addRow(self.waiver)
        self.active=QCheckBox('Применять новую версию');self.active.setChecked(self.original.active);form.addRow(self.active)
        self.day=line(form,'Дата изменения',date.today().isoformat());self.responsible=line(form,'Ответственное лицо *')
        content=QWidget();content.setLayout(form);scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(content);layout.addWidget(scroll)
        self.typ.currentIndexChanged.connect(self.type_changed)
        self.resize(680,650);self.save_button=buttons(self,layout,self.save)

    def type_changed(self):
        if self.typ.currentData()=='consent':
            self.required.setChecked(False);self.signed.setChecked(True)
        if self.typ.currentData()=='application':self.reusable.setChecked(False)

    def save(self):
        try:
            rule=replace(self.original,title=self.title.text(),program_kind=self.kind.currentData(),program_id=self.program.currentData() or None,
                condition_tag=self.tag.text(),source=self.origin.currentData(),reference=self.reference.text(),required=self.required.isChecked(),
                document_type=self.typ.currentData(),sign_required=self.signed.isChecked(),reusable=self.reusable.isChecked(),scope=self.scope.text(),
                template_variant=self.variant.text(),waiver_allowed=self.waiver.isChecked(),active=self.active.isChecked())
            self.repository.save_rule(rule,responsible=self.responsible.text(),day=self.day.text(),expected_version=self.original.version)
            self.accept();return True
        except ERRORS as error:warn(self,error);return False


class AdmissionPage(QWidget):
    templates_requested=Signal()
    enrollment_requested=Signal(object,object)

    def __init__(self,repository):
        super().__init__();self.repository=repository;self.assessment=None;self.items={}
        layout=QVBoxLayout(self);tabs=QTabWidget();layout.addWidget(tabs)
        page=QWidget();body=QVBoxLayout(page);tabs.addTab(page,'Приём на обучение')
        filters=QHBoxLayout();self.training=QComboBox();self.training.setMinimumContentsLength(20)
        self.training.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.search=QLineEdit();self.search.setPlaceholderText('Поиск слушателя')
        filters.addWidget(self.search);filters.addWidget(self.training,1)
        new=QPushButton('Новое обучение');new.clicked.connect(self.new_training);filters.addWidget(new)
        self.check_button=QPushButton('Проверить');self.check_button.clicked.connect(self.refresh);filters.addWidget(self.check_button)
        body.addLayout(filters)
        self.state=QLabel();self.state.setWordWrap(True);self.state.setTextFormat(Qt.TextFormat.PlainText);body.addWidget(self.state)
        self.table=make_table(['Документ / условие','Источник','Основание','Обязательность','Состояние'])
        body.addWidget(self.table)
        self.problems=QPlainTextEdit();self.problems.setReadOnly(True);self.problems.setMaximumHeight(115);body.addWidget(self.problems)
        actions=QHBoxLayout()
        self.context_button=QPushButton('Основание и условия');self.context_button.clicked.connect(self.edit_context)
        self.record_button=QPushButton('Учесть документ');self.record_button.clicked.connect(self.edit_record)
        self.form_button=QPushButton('Сформировать DOCX');self.form_button.clicked.connect(self.prepare_form)
        self.reuse_button=QPushButton('Использовать действующее');self.reuse_button.clicked.connect(self.reuse_document)
        for b in (self.context_button,self.record_button,self.form_button,self.reuse_button):actions.addWidget(b)
        body.addLayout(actions)
        more=QHBoxLayout()
        self.copy_button=QPushButton('Получить вложение');self.copy_button.clicked.connect(self.copy_document)
        self.revoke_button=QPushButton('Отозвать согласие');self.revoke_button.clicked.connect(self.revoke_document)
        history=QPushButton('История');history.clicked.connect(self.history)
        templates=QPushButton('Шаблоны');templates.clicked.connect(self.templates_requested.emit)
        self.order_button=QPushButton('К зачислению');self.order_button.clicked.connect(self.create_order)
        for b in (self.copy_button,self.revoke_button,history,templates,self.order_button):more.addWidget(b)
        body.addLayout(more)
        rules_page=QWidget();rules_layout=QVBoxLayout(rules_page);tabs.addTab(rules_page,'Требования приёма')
        label=QLabel('Рабочий перечень разделяет закон, локальные акты и требования программы. Уточните локальный перечень по утверждённым актам организации. Дополнительное согласие не является условием зачисления.')
        label.setWordWrap(True);rules_layout.addWidget(label)
        self.rules_table=make_table(['Требование','Вид','Источник','Основание','Версия']);rules_layout.addWidget(self.rules_table)
        row=QHBoxLayout();add_rule=QPushButton('Добавить требование');add_rule.clicked.connect(lambda:self.edit_rule(False))
        edit_rule=QPushButton('Новая версия требования');edit_rule.clicked.connect(lambda:self.edit_rule(True))
        row.addWidget(add_rule);row.addWidget(edit_rule);rules_layout.addLayout(row)
        self.training.currentIndexChanged.connect(self.refresh);self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.itemDoubleClicked.connect(lambda _:self.edit_record())
        self.search.editingFinished.connect(self.reload_trainings)
        self.reload_trainings()

    def reload_trainings(self,selected=None):
        current=selected or self.training.currentData();self.training.blockSignals(True);self.training.clear()
        if self.repository:
            for key,name,snapshot,start in self.repository.training_options(query=self.search.text()):
                p=json.loads(snapshot);self.training.addItem(f'{name} — {p["full_name"]} ({start}, ID {key})',key)
        self.training.setCurrentIndex(self.training.findData(current) if current is not None else (0 if self.training.count() else -1));self.training.blockSignals(False);self.refresh()

    def open_training(self,training_id):
        self.search.clear();self.reload_trainings(training_id)

    def new_training(self):
        if not self.repository:return
        dialog=TrainingDialog(self.repository.trainings,parent=self)
        dialog.exec()
        if dialog.saved_training:self.reload_trainings(dialog.saved_training.id)

    def refresh(self):
        self.assessment=None;self.items.clear();self.table.setRowCount(0);self.problems.clear()
        try:
            if self.repository:
                fill_table(self.rules_table,[(r.rule_key,r.title,r.program_kind or 'Все',SOURCES[r.source],r.reference,r.version) for r in self.repository.rules()])
            key=self.training.currentData()
            if key is not None and self.repository:
                self.assessment=self.repository.review(key);self.items={i.key:i for i in self.assessment.items}
                fill_table(self.table,[(i.key,i.title,SOURCES[i.source],i.reference,'Обязательно' if i.required else 'Не блокирует',STATES[i.status]) for i in self.assessment.items])
                text='Историческая комплектность на момент зачисления; изменения запрещены.' if self.assessment.historical else 'Готов к зачислению' if self.assessment.ready else 'Не готов к зачислению'
                if self.assessment.historical and not self.assessment.items:text='Прежнее зачисление: комплектность до P03.5 не фиксировалась.'
                self.state.setText(text);self.problems.setPlainText('\n'.join(self.assessment.problems))
            else:self.state.setText('Выберите слушателя и программу через существующее обучение либо создайте новое.')
        except ERRORS as error:self.state.setText(str(error))
        self.selection_changed()

    def selection_changed(self):
        item=self.items.get(selected_id(self.table));editable=bool(self.assessment and not self.assessment.historical and self.repository.trainings.load(self.assessment.training_id).state=='preparation')
        self.context_button.setEnabled(editable);self.record_button.setEnabled(bool(item and editable))
        rule=next((r for r in self.repository.applicable(self.assessment.training_id) if item and r.rule_key==item.key),None) if self.repository and self.assessment and not self.assessment.historical else None
        self.form_button.setEnabled(bool(editable and rule and rule.document_type in ('application','consent')))
        self.reuse_button.setEnabled(bool(editable and rule and rule.reusable))
        doc=self.repository.document(item.document_id) if item and item.document_id else None
        self.copy_button.setEnabled(bool(doc and doc['file_path']))
        details=list(self.assessment.problems) if self.assessment else []
        if doc:
            event=self.repository.document_event(doc['id'])
            details.extend([f"Документ ID {doc['id']} · дата {doc['document_date']} · действует до {doc['valid_until'] or 'не указано'}",
                'Область действия: '+doc['scope'],f"Последнее событие: {STATES.get(event['state'],'Отозвано')} · {event['day']} · {event['responsible']}",
                'Подписание: '+('подтверждено' if event['signed'] else 'не подтверждено'),
                'Вложение: '+(doc['file_path'] or 'не сохранялось')])
            if doc['template_id']:
                template=self.repository.templates.load(doc['template_id'])
                details.append(f"Шаблон: {template.name}, версия {template.version}, SHA-256 {doc['template_sha256']}")
        self.problems.setPlainText('\n'.join(details))
        is_consent=bool(item and item.document_id and self.repository.document(item.document_id)['document_type']=='consent')
        self.revoke_button.setEnabled(is_consent)
        self.order_button.setEnabled(bool(editable and self.assessment.ready))

    def edit_context(self):
        if not self.assessment:return
        try:AdmissionContextDialog(self.repository,self.assessment.training_id,self).exec()
        except ERRORS as error:warn(self,error)
        self.refresh()

    def edit_record(self):
        item=self.items.get(selected_id(self.table))
        if not item:return
        try:AdmissionRecordDialog(self.repository,self.assessment.training_id,item,self).exec()
        except ERRORS as error:warn(self,error)
        self.refresh()

    def prepare_form(self):
        item=self.items.get(selected_id(self.table))
        if not item:return
        try:
            dialog=AdmissionFormDialog(self.repository,self.assessment.training_id,item,self);dialog.exec()
            if dialog.document_id:
                path=self.repository.storage.root/'exports'/('admission-'+uuid4().hex+'.docx')
                self.repository.copy(dialog.document_id,path);open_local_docx(path)
        except ERRORS as error:warn(self,error)
        self.refresh()

    def reuse_document(self):
        item=self.items.get(selected_id(self.table))
        if not item:return
        try:
            docs=self.repository.reusable_documents(self.assessment.training_id,item.key)
            if not docs:raise ValueError('Нет действующих проверенных подтверждений той же области действия. Сначала учтите документ.')
            dialog=QDialog(self);dialog.setWindowTitle('Выберите действующее подтверждение');layout=QVBoxLayout(dialog);form=QFormLayout()
            select=QComboBox()
            for d in docs:select.addItem(f'ID {d["id"]} · {d["title"]} · {d["document_date"]} · до {d["valid_until"] or "без указанного срока"}',d['id'])
            form.addRow('Подтверждение',select);who=line(form,'Ответственное лицо *');layout.addLayout(form)
            buttons(dialog,layout,dialog.accept)
            if dialog.exec()==QDialog.DialogCode.Accepted:
                self.repository.record(self.assessment.training_id,item.key,state='verified',reason='Явно проверена применимость действующего подтверждения.',day=date.today().isoformat(),responsible=who.text(),document_id=select.currentData(),expected_signature=self.assessment.signature)
        except ERRORS as error:warn(self,error)
        self.refresh()

    def copy_document(self):
        item=self.items.get(selected_id(self.table))
        if not item or not item.document_id:return
        try:
            source=self.repository.file(item.document_id)
            target,_=QFileDialog.getSaveFileName(self,'Получить неизменённую копию',source.name)
            if target:self.repository.copy(item.document_id,target)
        except ERRORS as error:warn(self,error)

    def revoke_document(self):
        item=self.items.get(selected_id(self.table))
        if not item or not item.document_id:return
        dialog=QDialog(self);dialog.setWindowTitle('Отзыв согласия');layout=QVBoxLayout(dialog);form=QFormLayout()
        day=line(form,'Дата отзыва',date.today().isoformat());who=line(form,'Ответственное лицо *');reason=line(form,'Основание отзыва *')
        layout.addLayout(form);buttons(dialog,layout,dialog.accept)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            try:self.repository.revoke(item.document_id,day=day.text(),responsible=who.text(),reason=reason.text())
            except ERRORS as error:warn(self,error)
        self.refresh()

    def edit_rule(self,existing):
        if not self.repository:return
        rule=next((r for r in self.repository.rules() if r.rule_key==selected_id(self.rules_table)),None) if existing else None
        if existing and rule is None:return
        try:AdmissionRuleDialog(self.repository,rule,self).exec()
        except ERRORS as error:warn(self,error)
        self.refresh()

    def history(self):
        if not self.assessment:return
        key=self.assessment.training_id;dialog=QDialog(self);dialog.setWindowTitle('История приёма');dialog.resize(850,500)
        layout=QVBoxLayout(dialog);text=QPlainTextEdit();text.setReadOnly(True)
        lines=self.repository.history_lines(key)
        text.setPlainText('\n'.join(lines));layout.addWidget(text);dialog.exec()

    def create_order(self):
        if self.assessment and self.assessment.ready:
            self.enrollment_requested.emit([self.assessment.training_id],self.repository.trainings.load(self.assessment.training_id).group_id)

    def showEvent(self,event):
        super().showEvent(event);self.reload_trainings()
