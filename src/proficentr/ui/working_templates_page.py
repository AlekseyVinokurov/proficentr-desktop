"""Каталог стандартных форм и дополнение данных проекта без редактора макета."""
from copy import deepcopy
import json
import sqlite3

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox,QDialog,QFileDialog,QFormLayout,QHBoxLayout,QLabel,
    QLineEdit,QMessageBox,QPlainTextEdit,QPushButton,QScrollArea,QTableWidget,QTableWidgetItem,QVBoxLayout,QWidget)

from proficentr.docx_context import empty_context, ROW_WORK_FIELDS
from proficentr.working_library import CATEGORIES, MODES, catalog
from proficentr.ui.groups_page import make_table,fill_table,selected_id
from proficentr.ui.templates_page import open_local_docx
ERRORS=(ValueError,OSError,sqlite3.Error,PermissionError)


def report(parent,error):
    QMessageBox.warning(parent,'Рабочие шаблоны',str(error) if isinstance(error,ValueError) else 'Операция недоступна. Проверьте права, хранилище и свободное место.')


class WorkingDocumentDialog(QDialog):
    def __init__(self, library, code, parent=None):
        super().__init__(parent);library.access('use');self.library=library;self.code=code;self.item=library.item(code);self.path=None
        self.setWindowTitle('Подготовить DOCX / дополнительные сведения');self.resize(800,750)
        outer=QVBoxLayout(self)
        hint=QLabel('Это неподписанный проект. Дополнительные сведения сохраняются для выбранного состава данных и используются рабочими сценариями. Досрочное отчисление, справка, реестр и подтверждение здесь не регистрируют юридические события. Основные сведения исправляйте в соответствующих карточках.')
        hint.setWordWrap(True);outer.addWidget(hint)
        self.record=QComboBox();self.record.addItem('Реквизиты для отдельного проекта',None)
        self.sources=[('training','trainings','Обучение'),('contract','contracts','Договор'),('enrollment','enrollment_orders','Зачисление'),('protocol','attestation_protocols','Протокол'),('completion','completion_orders','Завершение'),('issued','issued_documents','Выпускной документ')]
        for kind,table,label in self.sources:
            for key, in library.connection.execute(f'SELECT id FROM {table} ORDER BY id DESC LIMIT 100'):  # nosec B608 # Fixed internal table names.
                self.record.addItem(label+' № '+str(key),(kind,key))
        outer.addWidget(self.record)
        scroll=QScrollArea();scroll.setWidgetResizable(True);body=QWidget();self.form=QFormLayout(body);scroll.setWidget(body);outer.addWidget(scroll)
        self.edits={};self.lists={};self.list_fields={};self.add_buttons={}
        for variable in self.item['variables']:
            root,field=variable.split('.')
            if root in ('students','modules','commission','documents'):continue
            edit=QLineEdit();self.form.addRow(variable+(' *' if variable in self.item['required_variables'] else ''),edit);self.edits[variable]=edit
        for root in ('students','modules','commission','documents'):
            if any(v.startswith(root+'.') for v in self.item['variables']):
                fields=list(dict.fromkeys(v.split('.')[1] for v in self.item['variables'] if v.startswith(root+'.')))
                table=QTableWidget(0,len(fields));table.setHorizontalHeaderLabels(fields);table.setMinimumHeight(130);table.setMaximumHeight(180)
                self.form.addRow(root,table);self.lists[root]=table;self.list_fields[root]=fields
                add=QPushButton('Добавить строку');add.clicked.connect(lambda checked=False,r=root:self.add_row(r));self.form.addRow('',add);self.add_buttons[root]=add
        row=QHBoxLayout();save=QPushButton('Сохранить дополнительные сведения');save.clicked.connect(self.save_supplement)
        generate=QPushButton('Подготовить неподписанный DOCX');generate.clicked.connect(self.generate)
        self.generate_button=generate;self.save_button=save
        close=QPushButton('Закрыть');close.clicked.connect(self.reject)
        for b in (save,generate,close):row.addWidget(b)
        outer.addLayout(row);self.record.currentIndexChanged.connect(self.load_context);self.load_context()

    def load_context(self):
        self.original=None;self.generate_button.setEnabled(False);self.save_button.setEnabled(False)
        try:
            selected=self.record.currentData();self.original=self.library.context(*selected) if selected else empty_context()
            context=self.library.templates.complete_context(self.original)
            for variable,edit in self.edits.items():
                root,field=variable.split('.');edit.setText(str(context.get(root,{}).get(field,'')))
            for root,table in self.lists.items():
                rows=context.get(root,[]);table.setRowCount(0)
                for values in rows:self.add_row(root,values)
                self.add_buttons[root].setEnabled(not (selected and root=='students'))
            self.generate_button.setEnabled(True);self.save_button.setEnabled(True)
        except ERRORS as error:report(self,error)

    def add_row(self,root,values=None):
        table=self.lists[root];index=table.rowCount();table.insertRow(index);values=values or {}
        for column,field in enumerate(self.list_fields[root]):
            item=QTableWidgetItem(str(values.get(field,'')))
            if root=='students' and self.record.currentData() and field not in ROW_WORK_FIELDS:item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(index,column,item)

    def values(self):
        if self.original is None:raise ValueError('Выберите доступную исходную запись.')
        context=deepcopy(self.original)
        for variable,edit in self.edits.items():
            root,field=variable.split('.');context[root][field]=edit.text()
        for root,table in self.lists.items():
            rows=[]
            for index in range(table.rowCount()):
                values=deepcopy(self.original.get(root,[])[index]) if index<len(self.original.get(root,[])) else {}
                values.update({field:table.item(index,col).text() for col,field in enumerate(self.list_fields[root])})
                rows.append(values)
            context[root]=rows
        return context

    def save_supplement(self):
        try:
            context=self.values();extra={'work':context['work']}
            if 'students' in self.lists and [r.get('full_name') for r in context['students']] != [r.get('full_name') for r in self.original['students']]:raise ValueError('Состав и порядок слушателей изменяются в исходной карточке.')
            for root in ('modules','commission','documents'):
                if root in self.lists:extra[root]=context[root]
            if 'students' in self.lists:extra['rows']=[{k:v for k,v in row.items() if k in ROW_WORK_FIELDS} for row in context['students']]
            self.library.save_supplement(self.original,extra);return True
        except ERRORS as error:report(self,error);return False

    def generate(self):
        try:
            self.path=self.library.generate(self.code,self.values());open_local_docx(self.path);self.accept();return True
        except ERRORS as error:report(self,error);return False


class WorkingTemplatesPage(QWidget):
    def __init__(self, library):
        super().__init__();self.library=library;layout=QVBoxLayout(self)
        notice=QLabel('24 подготовленных формы T01–T05. Перед рабочим применением организация утверждает формы. Полноценные роли ещё не реализованы; предусмотрены проверки доступа P06. PDF-экспорт и PDF-фоны не поддерживаются.')
        notice.setWordWrap(True);layout.addWidget(notice)
        self.category=QComboBox();self.category.addItem('Все категории',None)
        for key,label in CATEGORIES.items():self.category.addItem(label,key)
        self.category.currentIndexChanged.connect(self.refresh);layout.addWidget(self.category)
        self.table=make_table(('Шаблон','Категория','Назначение','Источник','Фон'))
        self.table.itemSelectionChanged.connect(self.selection_changed);self.table.itemDoubleClicked.connect(self.card)
        layout.addWidget(self.table)
        self.buttons={}
        for actions in ((('card','Карточка',self.card),('standard','Подключить / восстановить стандартный',self.standard),('custom','Подключить свой DOCX',self.custom)),
                        (('background','Подключить PNG/JPG',self.background),('none','Отключить фон',lambda:self.set_mode('none')),('preprinted','Преднапечатанный бланк',lambda:self.set_mode('preprinted'))),
                        (('generate','Подготовить / дополнить сведения',self.generate),('open','Открыть копию DOCX',self.open))):
            row=QHBoxLayout()
            for key,label,callback in actions:
                button=QPushButton(label);button.clicked.connect(callback);row.addWidget(button);self.buttons[key]=button
            layout.addLayout(row)
        self.setEnabled(library is not None);self.refresh()

    def refresh(self,*_):
        try:
            rows=[]
            if self.library:
                for item in catalog():
                    if self.category.currentData() not in (None,item['category']):continue
                    current=self.library.current(item['code'])
                    rows.append((item['code'],item['name'],CATEGORIES[item['category']],item['scenario'],
                        'Пользовательский' if current and current['origin']=='custom' else 'Стандартный' if current else 'Стандартный — не подключён',MODES[current['mode']] if current else MODES['none']))
            fill_table(self.table,rows)
        except ERRORS as error:report(self,error)
        self.selection_changed()

    def selection_changed(self):
        code=selected_id(self.table);manage=self.library.can_manage() if self.library else False
        item=self.library.item(code) if code else None
        issued=bool(item and item['document_type'] in ('dpo_upskilling_certificate','dpo_retraining_diploma','po_profession_certificate'))
        for key,button in self.buttons.items():
            button.setEnabled(bool(code and (manage if key in ('standard','custom','background','none','preprinted') else True) and (issued if key in ('background','none','preprinted') else True)))

    def card(self,*_):
        code=selected_id(self.table)
        if not code:return
        try:
            item=self.library.item(code);current=self.library.current(code)
            dialog=QDialog(self);dialog.setWindowTitle(item['name']);dialog.resize(750,650);layout=QVBoxLayout(dialog)
            text=QPlainTextEdit();text.setReadOnly(True)
            info=[item['name'],'Категория: '+CATEGORIES[item['category']],'Назначение: '+item['scenario'],
                'Версия поставки: '+str(item['version']),'Групповое оформление: '+('да' if item['group'] else 'нет'),'PDF: не поддерживается',
                'Источник: '+('Пользовательский' if current and current['origin']=='custom' else 'Стандартный'),
                'Обязательные переменные: '+', '.join(item['required_variables']),'Все переменные: '+', '.join(item['variables']),
                'История (UTC):']
            info.extend(str(row) for row in self.library.history(code));text.setPlainText('\n\n'.join(info));layout.addWidget(text);dialog.exec()
        except ERRORS as error:report(self,error)

    def configure(self,**kwargs):
        code=selected_id(self.table)
        if not code:return
        try:self.library.configure(code,**kwargs);self.refresh()
        except ERRORS as error:report(self,error)

    def standard(self):
        if QMessageBox.question(self,'Стандартный шаблон','Подключить стандартную форму новой версией? Прежние файлы сохранятся.',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)==QMessageBox.StandardButton.Yes:self.configure(restore=True)

    def custom(self):
        path,_=QFileDialog.getOpenFileName(self,'Пользовательский DOCX','','DOCX (*.docx)')
        if path:self.configure(source=path)

    def background(self):
        path,_=QFileDialog.getOpenFileName(self,'Фоновый бланк','','Изображения (*.png *.jpg *.jpeg)')
        if path:self.configure(background=path,mode='background')

    def set_mode(self,mode):self.configure(mode=mode)

    def generate(self):
        code=selected_id(self.table)
        if not code:return
        try:WorkingDocumentDialog(self.library,code,self).exec()
        except ERRORS as error:report(self,error)

    def open(self):
        code=selected_id(self.table)
        if not code:return
        try:
            current=self.library.current(code)
            if not current:raise ValueError('Сначала подключите стандартную форму.')
            open_local_docx(self.library.templates.editable_copy(current['effective_version_id']))
        except ERRORS as error:report(self,error)

    def showEvent(self,event):super().showEvent(event);self.refresh()
