"""Реестр Н02: одна таблица и карточка с историей редакций."""
import sqlite3

from PySide6.QtCore import QBuffer, QIODevice, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QPainter
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtPrintSupport import QPrinter, QPrintDialog
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget)

from proficentr.regulatory_documents import CATEGORIES, STATUSES, RegulatoryVersion
from proficentr.ui.groups_page import make_table, fill_table, selected_id
from proficentr.ui.templates_page import open_local_docx

ERRORS = (ValueError, OSError, sqlite3.Error, PermissionError)
CAPTIONS = {'title': 'Наименование *', 'number': 'Номер документа', 'document_date': 'Дата документа',
            'effective_date': 'Дата вступления в силу', 'approval_basis': 'Основание утверждения',
            'edition': 'Редакция', 'note': 'Примечание'}


def report(parent, error):
    message = str(error) if isinstance(error, ValueError) else 'Операция недоступна. Проверьте права, хранилище и свободное место. Сохранённые редакции не заменены.'
    QMessageBox.warning(parent, 'Нормативные документы', message)


def pdf_document(data, parent):
    document = QPdfDocument(parent)
    buffer = QBuffer(document); buffer.setData(data); buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    document.load(buffer)
    if document.status() != QPdfDocument.Status.Ready or document.pageCount() == 0:
        raise ValueError('Не удалось открыть проверенный PDF.')
    return document, buffer


def print_pdf(data, printer):
    """Рендер страниц проверенного снимка через Qt; архив и внешние обработчики не изменяются."""
    owner = QObject(); document, buffer = pdf_document(data, owner)
    painter = QPainter()
    try:
        if not painter.begin(printer): raise OSError('Принтер недоступен.')
        first = max(1, printer.fromPage()); last = min(document.pageCount(), printer.toPage() or document.pageCount())
        pages = list(range(first - 1, last))
        if printer.pageOrder() == QPrinter.PageOrder.LastPageFirst: pages.reverse()
        for index, page in enumerate(pages):
            if index and not printer.newPage(): raise OSError('Не удалось напечатать страницу.')
            points = document.pagePointSize(page)
            scale = min(150 / 72, 4000 / max(points.width(), points.height(), 1))
            image = document.render(page, QSize(max(1, int(points.width() * scale)), max(1, int(points.height() * scale))))
            if image.isNull(): raise ValueError('Не удалось подготовить страницу PDF к печати.')
            area = printer.pageLayout().paintRectPixels(printer.resolution())
            size = image.size().scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio)
            from PySide6.QtCore import QRect
            rect = QRect(area.x() + (area.width() - size.width()) // 2,
                         area.y() + (area.height() - size.height()) // 2, size.width(), size.height())
            painter.drawImage(rect, image)
    finally:
        if painter.isActive(): painter.end()
        document.close(); buffer.close(); owner.deleteLater()


class RegulatoryCard(QDialog):
    def __init__(self, repository, version=None, *, new_revision=False, parent=None):
        super().__init__(parent); self.repository = repository
        self.previous = version if new_revision else None
        self.viewing = version is not None and not new_revision
        self.saved_version = None; self.sources = {'pdf': None, 'docx': None}
        self.setWindowTitle('Карточка нормативного документа' if self.viewing else 'Новая редакция' if new_revision else 'Добавить нормативный документ')
        self.resize(780, 750); outer = QVBoxLayout(self)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); outer.addWidget(scroll)
        body = QWidget(); layout = QVBoxLayout(body); scroll.setWidget(body)
        self.revisions = QComboBox(); layout.addWidget(self.revisions); self.revisions.setVisible(self.viewing)
        form = QFormLayout(); self.fields = {}
        for key, caption in CAPTIONS.items():
            edit = QPlainTextEdit() if key == 'note' else QLineEdit()
            if key == 'note': edit.setMaximumHeight(70)
            else: edit.setMaxLength(10000)
            edit.setAccessibleName(caption); self.fields[key] = edit; form.addRow(caption, edit)
            if key.endswith('date'): edit.setPlaceholderText('ГГГГ-ММ-ДД')
        self.category = QComboBox(); self.status = QComboBox()
        for key, caption in CATEGORIES.items(): self.category.addItem(caption, key)
        for key, caption in STATUSES.items(): self.status.addItem(caption, key)
        form.insertRow(1, 'Категория *', self.category); form.addRow('Статус', self.status)
        self.file_labels = {}
        for kind in ('pdf', 'docx'):
            row = QHBoxLayout(); label = QLabel('Не загружен'); label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True); self.file_labels[kind] = label; row.addWidget(label, 1)
            button = QPushButton('Выбрать ' + kind.upper()); button.clicked.connect(lambda checked=False,k=kind:self.pick(k))
            button.setVisible(not self.viewing); row.addWidget(button); form.addRow('Основной PDF' if kind == 'pdf' else 'Исходник DOCX (необязательно)', row)
        layout.addLayout(form)
        layout.addWidget(QLabel('Связанные приказы, протоколы и другие документы (необязательно):'))
        self.links = QListWidget(); self.links.setMaximumHeight(110); layout.addWidget(self.links)
        self.events = QPlainTextEdit(); self.events.setReadOnly(True); self.events.setMaximumHeight(100)
        self.events.setVisible(self.viewing); layout.addWidget(self.events)
        hint = QLabel('Действующий документ: PDF и дата документа. Для лицензии нужен номер; для устава и локальных актов — дата вступления в силу и основание утверждения. Новая редакция требует новой загрузки файлов; прежние сохраняются.')
        hint.setWordWrap(True); layout.addWidget(hint)
        row = QHBoxLayout()
        if not self.viewing:
            save = QPushButton('Сохранить редакцию'); save.clicked.connect(self.save); row.addWidget(save)
        else:
            for caption, callback in (('Открыть PDF', self.open_pdf), ('Исходник DOCX', self.open_docx), ('Сделать действующим', self.activate)):
                button = QPushButton(caption); button.clicked.connect(callback); row.addWidget(button)
                if caption == 'Сделать действующим': self.activate_button = button
        close = QPushButton('Закрыть'); close.clicked.connect(self.reject); row.addWidget(close); outer.addLayout(row)
        if self.viewing:
            for edit in self.fields.values(): edit.setReadOnly(True)
            self.category.setEnabled(False); self.status.setEnabled(False)
            self.revisions.currentIndexChanged.connect(self.change_revision)
            self.links.itemDoubleClicked.connect(self.follow_link)
            self.navigate(version)
        else:
            self.populate(version or RegulatoryVersion())
            if new_revision: self.status.setCurrentIndex(self.status.findData('draft')); self.fields['edition'].clear()
            available = {item.id: item for item in repository.search()}
            previous_targets = repository.links(version.id) if version else []
            available.update({item.id: item for item in previous_targets})
            for target in available.values():
                if version and target.document_id == version.document_id: continue
                item = QListWidgetItem(target.title + ' · ' + target.edition)
                item.setData(Qt.ItemDataRole.UserRole, target.id)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable); item.setCheckState(Qt.CheckState.Unchecked)
                self.links.addItem(item)
            if version:
                previous_links = {v.id for v in previous_targets}
                for index in range(self.links.count()):
                    item = self.links.item(index)
                    if item.data(Qt.ItemDataRole.UserRole) in previous_links: item.setCheckState(Qt.CheckState.Checked)

    def populate(self, version):
        self.current = version
        for key, edit in self.fields.items():
            if key == 'note': edit.setPlainText(version.note)
            else: edit.setText(getattr(version, key))
        self.category.setCurrentIndex(self.category.findData(version.category))
        self.status.setCurrentIndex(self.status.findData(version.status))
        for kind, label in self.file_labels.items():
            label.setText(('Загружен · SHA-256: ' + getattr(version, kind + '_sha256')) if self.viewing and getattr(version, kind + '_path') else 'Не загружен')
        if self.viewing:
            self.links.clear()
            for target in self.repository.links(version.id):
                item = QListWidgetItem(target.title + ' · ' + target.edition + ' · ' + STATUSES[target.status])
                item.setData(Qt.ItemDataRole.UserRole, target.id); self.links.addItem(item)
            self.events.setPlainText('\n'.join(f'{day} · {STATUSES[status]} · {note}' for status, day, note in self.repository.events(version.id)))
            self.activate_button.setEnabled(version.status == 'draft' and self.repository.can_manage())

    def navigate(self, version):
        self.revisions.blockSignals(True); self.revisions.clear()
        for item in self.repository.history(version.document_id):
            self.revisions.addItem(f'{item.edition} · {STATUSES[item.status]} · {item.created_at}', item.id)
        self.revisions.setCurrentIndex(self.revisions.findData(version.id)); self.revisions.blockSignals(False)
        self.populate(version)

    def change_revision(self):
        try: self.populate(self.repository.load(self.revisions.currentData()))
        except ERRORS as error: report(self, error)

    def follow_link(self, item):
        try: self.navigate(self.repository.load(item.data(Qt.ItemDataRole.UserRole)))
        except ERRORS as error: report(self, error)

    def pick(self, kind):
        path, _ = QFileDialog.getOpenFileName(self, 'Загрузить ' + kind.upper(), '', kind.upper() + ' (*.' + kind + ')')
        if path: self.sources[kind] = path; self.file_labels[kind].setText(path)

    def save(self):
        try:
            values = {k: e.toPlainText() if k == 'note' else e.text() for k, e in self.fields.items()}
            links = [self.links.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.links.count()) if self.links.item(i).checkState() == Qt.CheckState.Checked]
            version = RegulatoryVersion(**values, category=self.category.currentData(), status=self.status.currentData())
            self.saved_version = self.repository.add(version, **self.sources,
                previous_id=self.previous.id if self.previous else None, links=links)
            self.accept(); return True
        except ERRORS as error: report(self, error); return False

    def activate(self):
        try:
            version = self.repository.set_status(self.current.id, 'active', expected_status=self.current.status, note='Введён в действие ответственным сотрудником')
            self.navigate(version)
        except ERRORS as error: report(self, error)

    def open_pdf(self):
        try: show_pdf(self.repository.read_file(self.current.id), self)
        except ERRORS as error: report(self, error)

    def open_docx(self):
        try: open_local_docx(self.repository.editable_copy(self.current.id))
        except ERRORS as error: report(self, error)


def show_pdf(data, parent):
    dialog = QDialog(parent); dialog.setWindowTitle('Нормативный документ — проверенная копия PDF'); dialog.resize(850, 750)
    layout = QVBoxLayout(dialog); document, buffer = pdf_document(data, dialog)
    view = QPdfView(dialog); view.setDocument(document); view.setPageMode(QPdfView.PageMode.MultiPage)
    view.setZoomMode(QPdfView.ZoomMode.FitToWidth); layout.addWidget(view)
    dialog.exec(); view.setDocument(None); document.close(); buffer.close()


class RegulatoryPage(QWidget):
    def __init__(self, repository):
        super().__init__(); self.repository = repository; layout = QVBoxLayout(self)
        notice = QLabel('Редакции и файлы сохраняются в архиве. Полноценные роли пока не реализованы: изменения доступны локальному пользователю. Проверки доступа подготовлены для P06.')
        notice.setWordWrap(True); layout.addWidget(notice)
        filters = QHBoxLayout(); self.search = QLineEdit(); self.search.setPlaceholderText('Поиск по наименованию, номеру, основанию и примечанию')
        self.category = QComboBox(); self.category.addItem('Все категории', None)
        for key, value in CATEGORIES.items(): self.category.addItem(value, key)
        self.status = QComboBox(); self.status.addItem('Все статусы', None)
        for key, value in STATUSES.items(): self.status.addItem(value, key)
        for widget in (self.search, self.category, self.status): filters.addWidget(widget)
        layout.addLayout(filters)
        self.timer = QTimer(self); self.timer.setSingleShot(True); self.timer.setInterval(180)
        self.timer.timeout.connect(self.refresh); self.search.textChanged.connect(lambda: self.timer.start())
        self.category.currentIndexChanged.connect(self.refresh); self.status.currentIndexChanged.connect(self.refresh)
        self.table = make_table(('Наименование', 'Категория', 'Номер', 'Дата', 'Статус', 'Редакция'))
        self.table.itemSelectionChanged.connect(self.selection_changed); self.table.itemDoubleClicked.connect(self.card)
        layout.addWidget(self.table)
        self.buttons = {}
        for actions in ((('add','Добавить документ',self.add), ('open','Открыть',self.open), ('print','Распечатать',self.print), ('copy','Сохранить копию',self.copy)),
                        (('version','Добавить новую редакцию',self.new_revision), ('history','Карточка и история редакций',self.card), ('archive','Перевести в архив',self.archive))):
            row = QHBoxLayout()
            for key, caption, callback in actions:
                button = QPushButton(caption); button.clicked.connect(callback); row.addWidget(button); self.buttons[key] = button
            layout.addLayout(row)
        self.message = QLabel(); self.message.setWordWrap(True); self.message.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.message)
        self.recovery_notice = ''
        if repository and repository.can_manage():
            try:
                count = repository.recover_unregistered()
                if count: self.recovery_notice = f'Файлов после аварии: {count}. Сохранены отдельно в exports/regulatory-recovery. '
            except ERRORS:
                self.recovery_notice = 'Не удалось проверить незарегистрированные файлы. Проверьте доступ к хранилищу. '
        self.setEnabled(repository is not None); self.refresh()

    def refresh(self, *_):
        try:
            versions = self.repository.search(self.search.text(), self.category.currentData(), self.status.currentData()) if self.repository else []
            fill_table(self.table, [(v.id, v.title, CATEGORIES[v.category], v.number, v.document_date, STATUSES[v.status], v.edition) for v in versions])
            self.message.setText(self.recovery_notice + 'Документов: ' + str(len(versions)) + '. Двойной щелчок — карточка и связанные документы.')
        except ERRORS as error:
            fill_table(self.table, []); report(self, error)
        self.selection_changed()

    def selection_changed(self):
        key = selected_id(self.table); manage = self.repository.can_manage() if self.repository else False
        for name, button in self.buttons.items(): button.setEnabled((manage if name == 'add' else key is not None and (manage if name in ('version', 'archive') else True)))

    def showEvent(self, event):
        super().showEvent(event); self.refresh()

    def add(self): self.edit()
    def new_revision(self): self.edit(True)

    def edit(self, new_revision=False):
        try:
            self.repository.require_access('write')
            version = self.repository.load(selected_id(self.table)) if new_revision else None
            dialog = RegulatoryCard(self.repository, version, new_revision=new_revision, parent=self); dialog.exec()
            if dialog.saved_version: self.refresh()
        except ERRORS as error: report(self, error)

    def card(self, *_):
        key = selected_id(self.table)
        if key is None: return
        try: RegulatoryCard(self.repository, self.repository.load(key), parent=self).exec(); self.refresh()
        except ERRORS as error: report(self, error)

    def open(self):
        key = selected_id(self.table)
        if key is None: return
        try: show_pdf(self.repository.read_file(key), self)
        except ERRORS as error: report(self, error)

    def print(self):
        key = selected_id(self.table)
        if key is None: return
        try:
            data = self.repository.read_file(key)
            printer = QPrinter(QPrinter.PrinterMode.HighResolution); dialog = QPrintDialog(printer, self)
            if dialog.exec() == QDialog.DialogCode.Accepted: print_pdf(data, printer)
        except ERRORS as error: report(self, error)

    def copy(self):
        key = selected_id(self.table)
        if key is None: return
        try:
            path, selected_filter = QFileDialog.getSaveFileName(self, 'Новый файл копии', 'document-copy.pdf', 'PDF (*.pdf);;Исходник DOCX (*.docx)')
            kind = 'docx' if 'DOCX' in selected_filter else 'pdf'
            if path: self.repository.copy(key, path, kind); self.message.setText('Копия сохранена.')
        except ERRORS as error: report(self, error)

    def archive(self):
        from PySide6.QtWidgets import QInputDialog
        key = selected_id(self.table)
        if key is None: return
        try:
            version = self.repository.load(key)
            note, ok = QInputDialog.getText(self, 'Перевести в архив', 'Основание перевода в архив:')
            if ok: self.repository.set_status(key, 'archived', expected_status=version.status, note=note); self.refresh()
        except ERRORS as error: report(self, error)
