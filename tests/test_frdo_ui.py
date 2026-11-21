import pytest
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from frdo_fixtures import synthetic_template
from proficentr.services import prepare_storage, open_database
from proficentr.ui.settings_page import SettingsPage


@pytest.fixture
def page(tmp_path):
    app = QApplication.instance() or QApplication([])
    storage = prepare_storage(tmp_path / 'storage')
    with open_database(storage) as db:
        settings = SettingsPage(db.path, storage, db)
        assert settings.tabs.tabText(settings.tabs.indexOf(settings.frdo_page)) == 'ФИС ФРДО'
        yield settings.frdo_page, tmp_path
        settings.close()
    app.processEvents()


def test_import_both_through_settings_and_failed_replacement(page, monkeypatch):
    widget, folder = page
    for kind in ('dpo', 'po'):
        source = folder / f'{kind}.xlsx'
        source.write_bytes(synthetic_template(kind))
        monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a, s=source: (str(s), ''))
        widget.buttons[kind].click()
        assert widget.repository.active(kind).version == 1
        assert 'SHA-256' in widget.labels[kind].text()
    old = widget.repository.active('po')
    source.write_bytes(b'invalid')
    errors = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: errors.append(a[-1]))
    widget.buttons['po'].click()
    assert errors and widget.repository.active('po') == old
    assert widget.repository.active('dpo').version == 1


def test_cancel_does_not_create_version(page, monkeypatch):
    widget, _ = page
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: ('', ''))
    widget.buttons['dpo'].click()
    assert widget.repository.active('dpo') is None


def test_replacement_shows_new_version_and_detects_tamper(page, monkeypatch):
    widget, folder = page
    source = folder / 'dpo.xlsx'
    source.write_bytes(synthetic_template('dpo'))
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: (str(source), ''))
    widget.buttons['dpo'].click()
    widget.buttons['dpo'].click()
    version = widget.repository.active('dpo')
    assert version.version == 2 and 'версия 2' in widget.labels['dpo'].text()
    (widget.repository.storage.root / version.relative_path).write_bytes(b'tampered')
    widget.refresh()
    assert 'не совпадает' in widget.labels['dpo'].text()
