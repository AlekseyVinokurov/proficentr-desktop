from dataclasses import replace

import pytest
from PySide6.QtWidgets import QMessageBox

from test_contracts_ui import app, context
from proficentr.enrollment_orders import EnrollmentOrder
from proficentr.ui.main_window import SECTIONS
from proficentr.ui.enrollment_orders_page import OrderDialog, SigningDialog
import proficentr.ui.enrollment_orders_page as ui


def test_navigation_and_quick_actions(context,monkeypatch):
    window,contracts,group,members,student=context
    calls=[]
    monkeypatch.setattr(window.orders_page,'open_card',lambda **args:calls.append(args))
    window.groups_page.order_requested.emit([members[0].id],group.id)
    assert window.pages.currentWidget() is window.documents_page
    assert window.documents_page.currentWidget() is window.orders_page
    assert calls==[dict(training_ids=[members[0].id],group_id=group.id)]
    window.groups_page.refresh(group.id); window.groups_page.order_button.click()
    assert calls[-1]==dict(training_ids=[],group_id=group.id)
    window.groups_page.individual.order_requested.emit([members[0].id],None)
    assert calls[-1]['group_id'] is None


def test_single_order_card_preview_register_then_sign(context,monkeypatch,tmp_path):
    window,contracts,group,members,student=context; repo=window.orders_page.repository
    repo.templates.register(tmp_path/'template.docx','enrollment_order','dpo-group','Форма приказа')
    monkeypatch.setattr(ui,'open_local_docx',lambda path:None)
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Yes)
    dialog=OrderDialog(repo,training_ids=[members[0].id],group_id=group.id)
    assert dialog.group.currentData()==group.id and dialog.kind.currentData()=='group'
    assert 'не учитывается' in dialog.contracts.toPlainText()
    dialog.day.setText('2020-01-01')
    assert dialog.save() and dialog.original.number is None
    assert dialog.preview() and dialog.register_button.isEnabled()
    assert dialog.register()
    saved=dialog.saved_order
    assert saved.state=='registered'
    page=window.orders_page; page.refresh(saved.id)
    assert page.sign_button.isEnabled() and page.package_button.isEnabled()
    sign=SigningDialog(repo,saved)
    monkeypatch.setattr(QMessageBox,'warning',lambda *a:None)
    assert not sign.confirm()
    sign.day.setText('2020-01-02'); sign.responsible.setText('Ответственный Примеров')
    sign.grounds.setChecked(True); sign.education.setChecked(True)
    from admission_fixtures import ready_for_admission
    ready_for_admission(repo.connection,repo.storage,[members[0].id])
    assert sign.confirm()
    page.refresh(saved.id); assert not page.sign_button.isEnabled()
    window.groups_page.individual.refresh()
    from proficentr.ui.groups_page import TrainingList
    listing=TrainingList(window.groups_page.repository,group_id=group.id)
    listing.refresh(members[0].id)
    assert listing.table.item(listing.table.currentRow(),4).text()=='Зачислен'
    assert not listing.exclude_button.isEnabled()
    dialog.close(); sign.close(); listing.close()


def test_dirty_draft_and_changed_preview(context,monkeypatch,tmp_path):
    window,contracts,group,members,student=context; repo=window.orders_page.repository
    repo.templates.register(tmp_path/'template.docx','enrollment_order','dpo-individual','Форма приказа')
    monkeypatch.setattr(ui,'open_local_docx',lambda path:None)
    dialog=OrderDialog(repo,training_ids=[members[0].id])
    assert dialog.save() and not dialog.is_dirty()
    assert dialog.preview()
    dialog.note.setPlainText('Изменения')
    assert dialog.is_dirty() and not dialog.register_button.isEnabled()
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Cancel)
    dialog.reject(); assert dialog.is_dirty()
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Save)
    dialog.reject(); assert not dialog.is_dirty()
