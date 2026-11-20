from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox
from test_contracts_ui import app
from test_contracts import records
from test_issued_documents import issued,register,draft,png
from proficentr.ui.main_window import MainWindow
from proficentr.ui.issued_documents_page import IssueBatchDialog,CompletedPicker,TransferDialog,DeliveryHistoryDialog
import proficentr.ui.issued_documents_page as ui


@pytest.fixture
def window(issued,app):
    w=MainWindow(issued.db.path,issued.storage,issued.db)
    yield w,issued
    w.close()


def test_navigation_and_quick_actions(window,monkeypatch):
    w,r=window;calls=[]
    monkeypatch.setattr(w.issued_documents_page,'open_training',lambda **args:calls.append(args))
    w.groups_page.issuance_requested.emit([r.members[0].id],r.group.id)
    assert w.documents_page.currentWidget() is w.issued_documents_page
    assert calls==[dict(training_ids=[r.members[0].id],group_id=r.group.id)]
    w.groups_page.refresh(r.group.id);w.groups_page.issued_button.click()
    assert calls[-1]==dict(training_ids=[],group_id=r.group.id)
    w.groups_page.individual.issuance_requested.emit([r.members[0].id],None)
    assert calls[-1]['group_id'] is None
    w.issued_documents_page.templates_requested.emit()
    assert w.settings_page.tabs.currentWidget() is w.settings_page.templates_page


def test_batch_per_person_dates_exclusion_preview_register(window,monkeypatch):
    w,r=window;monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Yes)
    dialog=IssueBatchDialog(r.repo,[t.id for t in r.members])
    assert [dialog.table.item(i,3).text() for i in (0,1)]==['2020-04-01','2020-04-02']
    dialog.table.item(0,3).setText('2020-04-05');dialog.table.item(0,5).setText('TEST-1')
    dialog.table.cellWidget(1,0).setChecked(False)
    assert dialog.preview() and len(dialog.reviews)==1 and dialog.register()
    assert r.repo.registered_for(r.members[0].id) and not r.repo.registered_for(r.members[1].id)
    first=r.repo.registered_for(r.members[0].id);assert first.blank_number=='TEST-1' and first.document_date=='2020-04-05'
    dialog.table.cellWidget(1,0).setChecked(True);assert dialog.preview() and dialog.register()
    assert r.repo.registered_for(r.members[1].id).document_date=='2020-04-02'
    page=w.issued_documents_page;page.refresh(select_id=first.id)
    assert page.delivery_button.isEnabled() and page.copy_button.isEnabled()
    assert 'не подтверждена' in page.table.item(page.table.currentRow(),6).text()
    dialog.close()


def test_pending_picker_and_registry_filters(window):
    w,r=window;d=register(r,draft(r));picker=CompletedPicker(r.repo,group_id=r.group.id)
    assert picker.table.rowCount()==1;picker.pending.setChecked(False);assert picker.table.rowCount()==2
    page=w.issued_documents_page;page.refresh(select_id=d.id);assert page.table.rowCount()==1
    page.year.setText('2021');page.refresh();assert page.table.rowCount()==0
    page.year.clear();page.search.setText('Тестов');page.refresh();assert page.table.rowCount()==1
    picker.close()


def test_transfer_history_email_then_personal_with_attachment(window,monkeypatch,tmp_path):
    w,r=window;d=register(r,draft(r));monkeypatch.setattr(QMessageBox,'warning',lambda *a,**k:None)
    email=TransferDialog(r.delivery,d);email.method.setCurrentIndex(email.method.findData('email'));email.day.setText('2020-04-05');email.email.setText('sample@example.test')
    assert email.save();assert 'не подтверждена' in r.repo.delivery_summary(d.id)[0]
    personal=TransferDialog(r.delivery,d);personal.day.setText('2020-04-06');assert not personal.save()
    personal.confirmation.setText('Подписанная расписка');personal.evidence=png(tmp_path/'receipt.png');assert personal.save()
    assert 'Лично 2020-04-06' in r.repo.delivery_summary(d.id)[0]
    history=DeliveryHistoryDialog(r.delivery,d);assert history.table.rowCount()==2
    history.table.selectRow(1);history.refresh_evidence();assert history.files.rowCount()==1
    email.close();personal.close();history.close()


def test_dirty_batch_and_template_navigation(window,monkeypatch):
    w,r=window;dialog=IssueBatchDialog(r.repo,[r.members[0].id]);dialog.table.item(0,5).setText('TEST')
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Cancel)
    assert not dialog.reject()
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Save)
    dialog.open_templates();assert dialog.go_to_templates and dialog.documents[0].blank_number=='TEST'
    dialog.close()


def test_xlsx_export_ui(window,monkeypatch):
    w,r=window;register(r,draft(r));messages=[]
    monkeypatch.setattr(QMessageBox,'information',lambda *args:messages.append(args[2]))
    page=w.issued_documents_page;page.start.setText('2020-01-01');page.end.setText('2020-12-31');page.export()
    assert messages and 'не выгрузка ФИС ФРДО' in messages[0]
    assert list((r.storage.root/'exports'/'issued-registers').glob('*.xlsx'))
