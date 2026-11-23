from datetime import date

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog

from test_contracts import records
from test_frdo_export import ready, second_ready
from test_frdo_preparation import save
from proficentr.ui.frdo_preparation_page import FrdoPreparationPage


@pytest.fixture
def screen(ready):
    app = QApplication.instance() or QApplication([])
    page = FrdoPreparationPage(ready.frdo)
    page.start.setDate(date(2026,11,1)); page.end.setDate(date(2026,11,30))
    yield ready,page
    page.close()


def test_select_export_retrieve_and_restore_from_journal(screen,monkeypatch,tmp_path):
    r,page = screen
    page.refresh()
    assert 'Готово: 1' in page.state.text()
    assert not page.export_button.isEnabled()
    page.select_ready_button.click()
    assert page.export_button.isEnabled()
    page.export_button.click()
    assert page.packages_table.rowCount() == 1
    package = page.exports.packages()[0]
    data = page.exports.file(package.id).read_bytes()
    assert package.filename in page.state.text()
    page.packages_table.selectRow(0)
    copy = tmp_path/'copy.xlsx'
    monkeypatch.setattr(QFileDialog,'getSaveFileName',lambda *args:(str(copy),''))
    page.copy_button.click()
    assert copy.read_bytes() == data
    page.exports.file(package.id).unlink()
    page.refresh_packages()
    assert 'недоступен' in page.packages_table.item(0,5).text()
    page.packages_table.selectRow(0)
    monkeypatch.setattr(QFileDialog,'getOpenFileName',lambda *args:(str(copy),''))
    page.restore_button.click()
    assert page.exports.file(package.id).read_bytes() == data
    assert len(page.exports.packages()) == 1


def test_selection_blocks_errors_and_stale_export_is_reported(screen,monkeypatch):
    r,page = screen
    second = second_ready(r)
    r.frdo.save_details(second.id,personal={'sex':''},expected_signature=r.frdo.review(second.id).signature)
    page.refresh()
    page.table.selectAll()
    assert not page.export_button.isEnabled()
    assert 'Пол получателя' in page.errors.toPlainText()
    page.select_ready_button.click()
    assert page.export_button.isEnabled() and len(page.selected_records()) == 1
    warnings = []
    monkeypatch.setattr(QMessageBox,'warning',lambda *args:warnings.append(args[-1]))
    save(r,training={'funding':'Местный бюджет'})
    page.export_button.click()
    assert 'изменились' in warnings[0]
    assert not page.exports.packages()


def test_repeat_gets_same_package_and_changed_data_needs_confirmation(screen,monkeypatch):
    r,page = screen
    page.refresh(); page.select_ready(); page.export_selected()
    original = page.exports.packages()[0]
    page.export_selected()
    assert page.exports.packages() == [original]
    save(r,training={'funding':'Местный бюджет'})
    page.refresh(); page.select_ready()
    monkeypatch.setattr(QMessageBox,'question',lambda *args:QMessageBox.StandardButton.No)
    page.export_selected()
    assert page.exports.packages() == [original]
    monkeypatch.setattr(QMessageBox,'question',lambda *args:QMessageBox.StandardButton.Yes)
    page.export_selected()
    assert len(page.exports.packages()) == 2
