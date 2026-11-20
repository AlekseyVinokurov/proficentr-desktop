from pathlib import Path

from docx import Document
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox, QPlainTextEdit

from proficentr.services import prepare_storage, open_database
from proficentr.document_templates import TemplateRepository
from proficentr.ui.main_window import MainWindow, SECTIONS
from proficentr.ui.templates_page import TemplateImportDialog, open_local_docx
import proficentr.ui.templates_page as ui_module


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def context(app, tmp_path):
    storage = prepare_storage(tmp_path / 'storage')
    source = tmp_path / 'sample.docx'
    document = Document()
    document.add_paragraph('ТЕСТ {{ center.full_name }} · {{ student.full_name }}')
    document.save(source)
    with open_database(storage) as db:
        window = MainWindow(db.path, storage, db)
        yield window, TemplateRepository(db.connection, storage), source
        window.close()


def test_settings_tab_import_new_version_activate_old_and_filter(app, context, monkeypatch):
    window, repo, source = context
    window.navigation.setCurrentRow(SECTIONS.index('Настройки'))
    settings = window.settings_page
    index = settings.tabs.indexOf(settings.templates_page)
    assert settings.tabs.tabText(index) == 'Шаблоны документов'
    settings.tabs.setCurrentIndex(index)
    page = settings.templates_page
    assert not page.version_button.isEnabled()
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: (str(source), ''))
    def register(dialog):
        dialog.document_type.setCurrentIndex(dialog.document_type.findData('application'))
        dialog.variant.setText('dpo')
        dialog.name.setText('Пример')
        assert dialog.save()
        return dialog.result()
    monkeypatch.setattr(TemplateImportDialog, 'exec', register)
    page.add_button.click()
    assert page.table.rowCount() == 1 and page.test_button.isEnabled()
    first_id = page.table.item(0, 0).data(Qt.ItemDataRole.UserRole)
    def version(dialog):
        assert not dialog.document_type.isEnabled() and dialog.variant.isReadOnly()
        assert dialog.document_type.currentData() == 'application' and dialog.variant.text() == 'dpo'
        assert dialog.save()
        return dialog.result()
    monkeypatch.setattr(TemplateImportDialog, 'exec', version)
    page.version_button.click()
    assert page.table.rowCount() == 2
    assert repo.active_version('application', 'dpo').version == 2
    page.table.selectRow(1)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    page.activate_button.click()
    assert repo.active_version('application', 'dpo').version == 2
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    page.activate_button.click()
    assert repo.active_version('application', 'dpo').id == first_id
    page.type_filter.setCurrentIndex(page.type_filter.findData('protocol_po'))
    assert page.table.rowCount() == 0
    page.type_filter.setCurrentIndex(page.type_filter.findData('application'))
    assert page.table.rowCount() == 2


def test_import_cancel_and_invalid_file_keep_registry(app, context, monkeypatch):
    window, repo, source = context
    page = window.settings_page.templates_page
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: ('', ''))
    page.add_button.click()
    assert not repo.list_versions()
    dialog = TemplateImportDialog(repo, source)
    dialog.name.setText('Несохранённое название')
    dialog.reject()
    assert not repo.list_versions()
    source.write_bytes(b'not DOCX')
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a[-1]))
    dialog = TemplateImportDialog(repo, source)
    dialog.show(); app.processEvents()
    assert not dialog.save() and dialog.isVisible() and warnings
    assert not repo.list_versions()
    dialog.reject()


def test_edit_copy_export_test_document_and_help(app, context, monkeypatch, tmp_path):
    window, repo, source = context
    saved = repo.register(source, 'other', 'default', 'Тест')
    page = window.settings_page.templates_page
    page.refresh(saved.id)
    calls = []
    monkeypatch.setattr(ui_module, 'open_local_docx', lambda path: calls.append(path))
    page.edit_button.click()
    assert len(calls) == 1 and calls[0].is_file()
    assert calls[0] != repo.source(saved.id).path
    assert calls[0].read_bytes() == source.read_bytes()
    export = tmp_path / 'export.docx'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (str(export), ''))
    page.export_button.click()
    assert export.read_bytes() == source.read_bytes()
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    page.test_button.click()
    assert len(calls) == 2 and calls[1].is_file()
    assert 'ТЕСТОВЫЙ ДОКУМЕНТ, НЕ ЗАРЕГИСТРИРОВАН' in page.state.text()
    assert 'Учебный центр «Пример»' in Document(calls[1]).paragraphs[0].text
    def guide(dialog):
        text = dialog.findChild(QPlainTextEdit).toPlainText()
        assert 'student.full_name' in text and '{%tr for row in students %}' in text
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(QDialog, 'exec', guide)
    page.help_button.click()


def test_failed_generation_and_tamper_report_without_mutation(app, context, monkeypatch):
    window, repo, source = context
    saved = repo.register(source, 'consent', 'default', 'Тест')
    page = window.settings_page.templates_page
    page.refresh(saved.id)
    path = repo.source(saved.id).path
    path.write_bytes(b'corrupted')
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a[-1]))
    page.test_button.click()
    assert warnings and 'SHA-256' in warnings[0]
    assert not list((repo.storage.root / 'exports').rglob('*.docx'))
    assert repo.load(saved.id) == saved


def test_open_handler_rejects_non_docx_without_launch(tmp_path, monkeypatch):
    path = tmp_path / 'malicious.exe'
    path.write_bytes(b'not a document')
    with pytest.raises(ValueError):
        open_local_docx(path)


def test_open_handler_uses_only_local_copy_and_no_shell(context, monkeypatch):
    _, repo, source = context
    calls = []
    if ui_module.sys.platform == 'win32':
        monkeypatch.setattr(ui_module.os, 'startfile', lambda path: calls.append(path))
        open_local_docx(source)
        assert calls == [str(source.resolve())]
    else:
        monkeypatch.setattr(ui_module.shutil, 'which', lambda name: '/usr/bin/xdg-open')
        monkeypatch.setattr(ui_module.subprocess, 'Popen', lambda args, **kw: calls.append((args, kw)))
        open_local_docx(source)
        assert calls == [(['/usr/bin/xdg-open', str(source.resolve())], {'shell': False})]
