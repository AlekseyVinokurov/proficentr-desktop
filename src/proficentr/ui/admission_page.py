"""Комплект конкретного приёма и редактирование перечня без изменения кода."""
from dataclasses import replace
from datetime import date
import json
from uuid import uuid4
import sqlite3
from proficentr.training_repository import TrainingRepositoryError
from proficentr.student_repository import StudentRepositoryError

from PySide6.QtCore import Qt, Signal, QMarginsF
from PySide6.QtGui import QTextDocument, QPageSize
from PySide6.QtPrintSupport import QPrinter, QPrintDialog
from PySide6.QtWidgets import (QCheckBox,QComboBox,QDialog,QFileDialog,QFormLayout,QHBoxLayout,QLabel,
    QGroupBox,QLineEdit,QMessageBox,QPlainTextEdit,QPushButton,QScrollArea,QGridLayout,QHeaderView,QSizePolicy,QVBoxLayout,QWidget)

from proficentr.admission import AdmissionRule, STATES, SOURCES, TYPES, BASES, EDUCATION_BASES
from dataclasses import asdict
from proficentr.admission_checklist import GROUPS, checklist, memo_html
from proficentr.program_repository import ProgramRepository, ProgramRepositoryError
from proficentr.programs import KINDS
from proficentr.ui.groups_page import make_table,fill_table,selected_id
from proficentr.ui.training_dialogs import TrainingDialog
from proficentr.ui.templates_page import open_local_docx

ERRORS=(ValueError,OSError,sqlite3.Error,TrainingRepositoryError,StudentRepositoryError,ProgramRepositoryError)


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
            self.flags[tag]=edit;form.addRow(' / '.join(r.title for r in repository.applicable(training_id) if r.condition_tag==tag),edit)
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
            if t.active and t.variant==rule.template_variant:self.template.addItem(t.name,t.id)
        form.addRow('Утверждённый шаблон',self.template)
        hint=QLabel(f'Подключите утверждённую форму «{TYPES[rule.document_type]}» в «Настройки → Шаблоны документов». Вариант формы задаётся в настройках требования приёма. Создание файла не подтверждает подписание.')
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
    contract_requested=Signal(object,object)

    def __init__(self,repository):
        super().__init__();self.repository=repository;self.assessment=None;self.items={}
        layout=QVBoxLayout(self)
        title=QLabel('Приём на обучение')
        font=title.font();font.setPointSize(20);font.setBold(True);title.setFont(font);layout.addWidget(title)
        self.empty=QLabel('Выберите вид обучения или программу — перечень можно посмотреть и распечатать до оформления слушателя.')
        self.empty.setWordWrap(True);layout.addWidget(self.empty)
        self.preview_selectors=QWidget();preview_form=QFormLayout(self.preview_selectors)
        self.kind=QComboBox()
        for direction,kinds in KINDS.items():
            for key,label in kinds.items():self.kind.addItem(('ДПО' if direction=='dpo' else 'ПО')+' — '+label,key)
        self.program=QComboBox();self.program.addItem('Программа не выбрана',None)
        preview_form.addRow('Вид обучения',self.kind);preview_form.addRow('Программа из справочника',self.program)
        layout.addWidget(self.preview_selectors)
        self.print_button=QPushButton('Распечатать памятку');self.print_button.clicked.connect(self.print_memo)
        self.preview_button=QPushButton('Вернуться к перечню без слушателя');self.preview_button.clicked.connect(self.open_preview)
        preview_actions=QHBoxLayout();preview_actions.addWidget(self.print_button);preview_actions.addWidget(self.preview_button)
        layout.addLayout(preview_actions)
        self.kind.currentIndexChanged.connect(self.kind_changed)
        self.program.currentIndexChanged.connect(self.program_changed)
        filters=QHBoxLayout()
        self.choose_button=QPushButton('Выбрать обучение');self.choose_button.clicked.connect(self.choose_training)
        self.new_button=QPushButton('Новое обучение');self.new_button.clicked.connect(self.new_training)
        filters.addWidget(self.choose_button);filters.addWidget(self.new_button);filters.addStretch();layout.addLayout(filters)
        # The chooser uses the same training list and repository as the working page.
        self.training=QComboBox(self);self.training.setMinimumContentsLength(20)
        self.training.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.search=QLineEdit(self);self.search.setPlaceholderText('Поиск по ФИО слушателя')
        self.training.hide();self.search.hide()
        self.workspace=QWidget();body=QVBoxLayout(self.workspace);body.setContentsMargins(0,0,0,0)
        self.summary=QLabel();self.summary.setWordWrap(True);self.summary.setTextFormat(Qt.TextFormat.PlainText);body.addWidget(self.summary)
        self.state=QLabel();self.state.setWordWrap(True);self.state.setTextFormat(Qt.TextFormat.PlainText);body.addWidget(self.state)
        row=QHBoxLayout()
        self.context_button=QPushButton('Основание обработки ПД и условия приёма');self.context_button.clicked.connect(self.edit_context)
        self.check_button=QPushButton('Проверить готовность');self.check_button.clicked.connect(self.refresh)
        row.addWidget(self.context_button);row.addWidget(self.check_button);body.addLayout(row)
        self.table=make_table(['Документ или условие','Обязательность','Состояние','Действие'])
        self.table.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(180)
        # Existing selection and record dialogs use this model; the visible rows are grouped by action.
        self.table.setParent(self);self.table.hide()
        self.group_layouts=[]
        for heading in GROUPS:
            box=QGroupBox(heading);group_layout=QVBoxLayout(box);body.addWidget(box);self.group_layouts.append(group_layout)
        self.problems=QPlainTextEdit();self.problems.setReadOnly(True);self.problems.setMaximumHeight(115);body.addWidget(self.problems)
        self.action_hint=QLabel('Выберите документ в перечне, чтобы учесть получение или проверить подтверждение.')
        self.action_hint.setWordWrap(True);body.addWidget(self.action_hint)
        actions=QGridLayout()
        self.record_button=QPushButton('Учесть получение и подписание');self.record_button.clicked.connect(self.edit_record)
        self.form_button=QPushButton('Сформировать форму DOCX');self.form_button.clicked.connect(self.prepare_form)
        self.reuse_button=QPushButton('Использовать подтверждение');self.reuse_button.clicked.connect(self.reuse_document)
        self.copy_button=QPushButton('Получить копию вложения');self.copy_button.clicked.connect(self.copy_document)
        self.revoke_button=QPushButton('Отозвать согласие');self.revoke_button.clicked.connect(self.revoke_document)
        self.history_button=QPushButton('История приёма');self.history_button.clicked.connect(self.history)
        templates=QPushButton('Подключить утверждённый шаблон');templates.clicked.connect(self.templates_requested.emit)
        self.order_button=QPushButton('Перейти к зачислению');self.order_button.clicked.connect(self.create_order)
        for n,button in enumerate((self.form_button,self.record_button,self.reuse_button,self.copy_button,self.revoke_button,self.history_button,templates,self.order_button)):
            actions.addWidget(button,n//2,n%2)
        body.addLayout(actions)
        body.addStretch()
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(self.workspace)
        self.work_scroll=scroll;layout.addWidget(scroll,1)
        self.empty_space=QWidget();layout.addWidget(self.empty_space,1)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        self.table.itemDoubleClicked.connect(lambda _:self.edit_record() if self.record_button.isEnabled() else None)
        self.search.editingFinished.connect(self.reload_trainings)
        self.reload_programs()
        self.reload_trainings()

    def choose_training(self):
        if not self.repository:return
        current=self.training.currentData()
        dialog=QDialog(self);dialog.setWindowTitle('Выбрать обучение');dialog.resize(700,180)
        layout=QVBoxLayout(dialog)
        layout.addWidget(QLabel('Найдите слушателя и выберите его обучение:'))
        layout.addWidget(self.search);layout.addWidget(self.training)
        empty=QLabel('Нет обучений по этому запросу. Измените поиск или создайте новое обучение.');empty.setWordWrap(True);layout.addWidget(empty)
        self.search.show();self.training.show()
        self.reload_trainings()
        select=buttons(dialog,layout,dialog.accept);select.setText('Выбрать обучение')
        select.setEnabled(self.training.count()>0)
        def update_selection(index):
            select.setEnabled(index>=0);empty.setVisible(self.training.count()==0)
        def update_search():update_selection(self.training.currentIndex())
        self.training.currentIndexChanged.connect(update_selection)
        self.search.editingFinished.connect(update_search)
        update_search()
        # A choice is committed only after confirmation; opening/cancelling the picker is read-only.
        if self.training.currentIndex()<0 and self.training.count():self.training.setCurrentIndex(0)
        accepted=dialog.exec()==QDialog.DialogCode.Accepted
        chosen=self.training.currentData() if accepted else current
        self.training.currentIndexChanged.disconnect(update_selection)
        self.search.editingFinished.disconnect(update_search)
        self.training.setParent(self);self.training.hide();self.search.setParent(self);self.search.hide()
        self.training.setCurrentIndex(-1)
        self.search.clear();self.reload_trainings(chosen)

    def reload_trainings(self,selected=None):
        current=selected if selected is not None else self.training.currentData();self.training.blockSignals(True);self.training.clear()
        if self.repository:
            for key,name,snapshot,start in self.repository.training_options(query=self.search.text()):
                p=json.loads(snapshot);self.training.addItem(f'{name} — {p["full_name"]} ({start})',key)
        self.training.setCurrentIndex(self.training.findData(current) if current is not None else -1);self.training.blockSignals(False);self.refresh()

    def open_training(self,training_id):
        self.search.clear();self.reload_trainings(training_id)

    def new_training(self):
        if not self.repository:return
        dialog=TrainingDialog(self.repository.trainings,parent=self)
        if self.program.currentData() is not None:
            dialog.program.setCurrentIndex(dialog.program.findData(self.program.currentData()))
        dialog.exec()
        if dialog.saved_training:self.reload_trainings(dialog.saved_training.id)

    def reload_programs(self):
        selected=self.program.currentData();self.program.blockSignals(True);self.program.clear()
        self.program.addItem('Программа не выбрана',None)
        if self.repository:
            for program in ProgramRepository(self.repository.connection).search():self.program.addItem(program.full_name,program.id)
        self.program.setCurrentIndex(max(0,self.program.findData(selected)));self.program.blockSignals(False)

    def open_preview(self):
        self.training.setCurrentIndex(-1);self.refresh()

    def kind_changed(self):
        self.program.blockSignals(True);self.program.setCurrentIndex(0);self.program.blockSignals(False)
        self.open_preview()

    def program_changed(self):
        self.open_preview()

    def print_memo(self):
        self.refresh()
        if not self.checklist:return
        printer=QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        printer.setPageMargins(QMarginsF(12,12,12,12))
        dialog=QPrintDialog(printer,self);dialog.setWindowTitle('Распечатать памятку')
        if dialog.exec()==QDialog.DialogCode.Accepted:
            document=QTextDocument();document.setDefaultStyleSheet('body {font-size: 10pt;} h1 {font-size: 16pt;} h2 {font-size: 12pt;} p {margin: 6px 0;}')
            document.setHtml(memo_html(self.checklist));document.print_(printer)

    def render_groups(self):
        for group_layout in self.group_layouts:
            while group_layout.count():
                widget=group_layout.takeAt(0).widget()
                if widget:widget.hide();widget.deleteLater()
        if not self.checklist:return
        for group_layout,rows in zip(self.group_layouts,self.checklist.groups):
            for entry in rows:
                widget=QWidget();row=QHBoxLayout(widget);row.setContentsMargins(0,0,0,0)
                label=QLabel(entry.title+'\n'+entry.detail);label.setTextFormat(Qt.TextFormat.PlainText);label.setWordWrap(True);row.addWidget(label,1)
                label.setMinimumHeight(label.fontMetrics().height()*(2 if entry.detail else 1))
                widget.setSizePolicy(QSizePolicy.Policy.Preferred,QSizePolicy.Policy.Minimum)
                if self.assessment and entry.key in self.items:
                    number=list(self.items).index(entry.key)
                    original=self.table.cellWidget(number,3)
                    action=QPushButton(original.text());action.setEnabled(original.isEnabled());action.setToolTip(original.toolTip())
                    action.clicked.connect(lambda checked=False,n=number:self.table.cellWidget(n,3).click());row.addWidget(action)
                    details=QPushButton('Подробнее');details.setToolTip('Выбрать документ: сведения, получение, повторное использование и история.')
                    details.clicked.connect(lambda checked=False,n=number:self.table.selectRow(n));row.addWidget(details)
                elif self.assessment and entry.title=='Договор на обучение':
                    action=QPushButton('Распечатать договор');action.setEnabled(not self.assessment.historical)
                    action.clicked.connect(self.create_contract);row.addWidget(action)
                group_layout.addWidget(widget)
            if not rows:group_layout.addWidget(QLabel('По настроенным правилам отдельных документов не предусмотрено.'))

    def refresh(self):
        self.checklist=None
        self.assessment=None;self.items.clear();self.table.setRowCount(0);self.problems.clear()
        try:
            key=self.training.currentData()
            if key is not None and self.repository:
                self.assessment=self.repository.review(key);self.items={i.key:i for i in self.assessment.items}
                training=self.repository.trainings.load(key);program=json.loads(training.program_snapshot)
                student=self.repository.students.load(training.student_id)
                direction=program['direction'];kind=KINDS.get(direction,{}).get(program['kind'],program['kind'])
                self.summary.setText(f"{student.full_name}\n{program['full_name']}\n{'ДПО' if direction=='dpo' else 'ПО'} · {kind} · {program['hours']} ч.\n{training.start_date} — {training.end_date}")
                self.kind.blockSignals(True);self.kind.setCurrentIndex(self.kind.findData(program['kind']));self.kind.blockSignals(False)
                labels={'required':'Отсутствует','absent':'Отсутствует','prepared':'Подготовлен','received':'Получен','verified':'Проверен','not_required':'Не требуется'}
                fill_table(self.table,[(i.key,i.title,('Обязательно · ' if i.required else 'Не блокирует · ')+SOURCES[i.source],labels[i.status],'') for i in self.assessment.items])
                rules={r.rule_key:r for r in self.repository.applicable(key)} if not self.assessment.historical else {}
                for row,item in enumerate(self.assessment.items):
                    self.table.item(row,0).setToolTip(item.reference)
                    rule=rules.get(item.key)
                    form=bool(rule and rule.document_type in ('application','consent') and item.status in ('required','absent'))
                    button=QPushButton('Сформировать' if form else 'Проверить')
                    button.setEnabled(not self.assessment.historical and training.state=='preparation' and item.status!='not_required')
                    button.setToolTip('Исторический комплект защищён от изменений.' if self.assessment.historical else 'Требование не применяется.' if item.status=='not_required' else 'Созданная форма не считается подписанной.' if form else 'Учесть получение и проверить подтверждение.')
                    button.clicked.connect(lambda checked=False,row=row,form=form:self.row_action(row,form))
                    self.table.setCellWidget(row,3,button)
                self.table.resizeRowsToContents()
                text='Историческая комплектность на момент зачисления; изменения запрещены.' if self.assessment.historical else 'Можно зачислять' if self.assessment.ready else 'Не хватает документов или не выполнены условия допуска'
                if self.assessment.historical and not self.assessment.items:text='Прежнее зачисление: комплектность до P03.5 не фиксировалась.'
                self.checklist=checklist(self.repository,program,training.program_id,self.assessment)
                self.state.setText(text);self.problems.setPlainText('\n'.join(self.assessment.problems))
            elif self.repository:
                program_id=self.program.currentData()
                if program_id is not None:
                    program=asdict(ProgramRepository(self.repository.connection).load(program_id))
                    self.kind.blockSignals(True);self.kind.setCurrentIndex(self.kind.findData(program['kind']));self.kind.blockSignals(False)
                else:
                    kind=self.kind.currentData();program={'direction':'dpo' if kind in KINDS['dpo'] else 'po','kind':kind}
                self.checklist=checklist(self.repository,program,program_id)
                self.summary.setText(self.checklist.title+'\n'+self.checklist.subtitle)
                self.state.setText(self.checklist.notice)
            else:self.state.clear();self.summary.clear()
        except ERRORS as error:self.state.setText(str(error))
        self.empty.setVisible(self.assessment is None)
        self.empty_space.hide()
        self.work_scroll.setVisible(self.checklist is not None)
        self.print_button.setEnabled(self.checklist is not None)
        self.preview_button.setVisible(self.assessment is not None)
        self.preview_selectors.setVisible(self.assessment is None)
        self.kind.setEnabled(self.assessment is None);self.program.setEnabled(self.assessment is None)
        self.render_groups()
        self.choose_button.setEnabled(bool(self.repository))
        self.new_button.setEnabled(bool(self.repository))
        self.selection_changed()
        for widget in (self.context_button,self.check_button,self.problems,self.action_hint,self.record_button,self.form_button,self.reuse_button,self.copy_button,self.revoke_button,self.history_button,self.order_button):
            widget.setVisible(self.assessment is not None)

    def create_contract(self):
        if self.assessment and not self.assessment.historical:
            training=self.repository.trainings.load(self.assessment.training_id)
            self.contract_requested.emit([training.id],training.group_id)

    def row_action(self,row,form):
        self.table.selectRow(row)
        if form:self.prepare_form()
        else:self.edit_record()

    def selection_changed(self):
        item=self.items.get(selected_id(self.table));editable=bool(self.assessment and not self.assessment.historical and self.repository.trainings.load(self.assessment.training_id).state=='preparation')
        self.context_button.setToolTip('Исторический комплект защищён от изменений.' if not editable else 'Укажите основание обработки и применимость специальных условий.')
        self.context_button.setEnabled(editable);self.record_button.setEnabled(bool(item and editable))
        rule=next((r for r in self.repository.applicable(self.assessment.training_id) if item and r.rule_key==item.key),None) if self.repository and self.assessment and not self.assessment.historical else None
        self.form_button.setEnabled(bool(editable and rule and rule.document_type in ('application','consent') and item.status!='not_required'))
        self.reuse_button.setEnabled(bool(editable and rule and rule.reusable and item.status!='not_required' and self.repository.reusable_documents(self.assessment.training_id,item.key)))
        doc=self.repository.document(item.document_id) if item and item.document_id else None
        self.copy_button.setEnabled(bool(doc and doc['file_path']))
        details=list(self.assessment.problems) if self.assessment else []
        if item:details.extend([SOURCES[item.source]+': '+item.reference,item.reason])
        if doc:
            event=self.repository.document_event(doc['id'])
            details.extend([f"Документ · дата {doc['document_date']} · действует до {doc['valid_until'] or 'не указано'}",
                'Область действия: '+doc['scope'],f"Последнее событие: {STATES.get(event['state'],'Отозвано')} · {event['day']} · {event['responsible']}",
                'Подписание: '+('подтверждено' if event['signed'] else 'не подтверждено'),
                'Вложение: '+(doc['file_path'] or 'не сохранялось')])
            if doc['template_id']:
                template=self.repository.templates.load(doc['template_id'])
                details.append(f"Шаблон: {template.name}")
        self.problems.setPlainText('\n'.join(details))
        is_consent=bool(item and item.document_id and self.repository.document(item.document_id)['document_type']=='consent')
        self.revoke_button.setEnabled(bool(is_consent and self.repository.document_event(item.document_id)['state']!='revoked'))
        self.order_button.setEnabled(bool(editable and self.assessment.ready))
        self.history_button.setEnabled(bool(self.assessment))
        self.order_button.setToolTip('Сначала устраните замечания и проверьте готовность.' if not self.order_button.isEnabled() else 'Открыть оформление приказа о зачислении.')
        hint='Исторический комплект доступен для просмотра; изменения запрещены.' if self.assessment and self.assessment.historical else 'Выберите документ в перечне.' if not item else 'Создание DOCX не подтверждает подписание. Получение и подпись учитываются отдельно.'
        self.action_hint.setText(hint)
        for button,reason in ((self.form_button,'Для выбранного требования не предусмотрена форма DOCX либо оно не применяется.'),(self.record_button,'Выберите документ в редактируемом комплекте.'),(self.reuse_button,'Нет действующих подтверждений для этого требования либо повторное использование не предусмотрено.'),(self.copy_button,'У выбранного документа нет сохранённого вложения.'),(self.revoke_button,'Выберите зарегистрированное согласие.')):
            button.setToolTip(reason if not button.isEnabled() else button.text())

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
            for d in docs:select.addItem(f'{d["title"]} · {d["document_date"]} · до {d["valid_until"] or "без указанного срока"}',d['id'])
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
        super().showEvent(event);self.reload_programs();self.reload_trainings()


class AdmissionRulesPage(QWidget):
    """Versioned requirements editor reused in Settings, apart from the operator's case."""
    def __init__(self,repository):
        super().__init__();self.repository=repository
        layout=QVBoxLayout(self)
        label=QLabel('Требования законодательства, локальных актов и конкретных программ. Изменение создаёт новую версию и сохраняет историю. Дополнительное согласие не является условием зачисления.')
        label.setWordWrap(True);layout.addWidget(label)
        self.rules_table=make_table(['Требование','Вид','Источник','Основание','Версия']);layout.addWidget(self.rules_table)
        row=QHBoxLayout()
        self.add_button=QPushButton('Добавить требование');self.add_button.clicked.connect(lambda:self.edit_rule(False))
        self.edit_button=QPushButton('Новая версия требования');self.edit_button.clicked.connect(lambda:self.edit_rule(True))
        row.addWidget(self.add_button);row.addWidget(self.edit_button);layout.addLayout(row)
        self.rules_table.itemSelectionChanged.connect(self.selection_changed)
        self.refresh()

    def selection_changed(self):
        self.edit_button.setEnabled(bool(self.repository and selected_id(self.rules_table)))

    def refresh(self):
        if self.repository:
            fill_table(self.rules_table,[(r.rule_key,r.title,r.program_kind or 'Все',SOURCES[r.source],r.reference,r.version) for r in self.repository.rules()])
        self.add_button.setEnabled(bool(self.repository));self.selection_changed()

    def edit_rule(self,existing):
        if not self.repository:return
        rule=next((r for r in self.repository.rules() if r.rule_key==selected_id(self.rules_table)),None) if existing else None
        if existing and rule is None:return
        try:AdmissionRuleDialog(self.repository,rule,self).exec()
        except ERRORS as error:warn(self,error)
        self.refresh()

    def showEvent(self,event):
        super().showEvent(event);self.refresh()
