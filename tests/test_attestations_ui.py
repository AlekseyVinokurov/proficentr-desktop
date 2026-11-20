from PySide6.QtWidgets import QMessageBox

from test_contracts_ui import app, context
from proficentr.enrollment_orders import EnrollmentOrder
from proficentr.ui.attestations_page import ProtocolDialog, ProtocolSigningDialog
import proficentr.ui.attestations_page as ui


def enroll(window, group, members):
    repo=window.orders_page.repository
    # The existing test fixture's source is already registered for contracts.
    source=repo.templates.source(window.contracts_page.repository.templates.list_versions()[0].id).path
    repo.templates.register(source,'enrollment_order','dpo-group','Вымышленный приказ')
    p=repo.save(EnrollmentOrder(order_date='2020-01-01',kind='group',group_id=group.id),[t.id for t in members])
    p=repo.register(p.id,repo.prepare(p.id).signature)
    repo.confirm_signing(p.id,signed_date='2020-01-02',confirmed_by='Ответственный',grounds_checked=True,education_checked=True)
    repo.templates.register(source,'protocol_dpo','standard','Вымышленный протокол')


def test_navigation_and_quick_actions(context,monkeypatch):
    window,contracts,group,members,student=context
    calls=[]; monkeypatch.setattr(window.attestations_page,'open_card',lambda **args:calls.append(args))
    window.groups_page.attestation_requested.emit([members[0].id],group.id)
    assert window.documents_page.currentWidget() is window.attestations_page
    assert calls==[dict(training_ids=[members[0].id],group_id=group.id)]
    window.groups_page.refresh(group.id); window.groups_page.attestation_button.click()
    assert calls[-1]==dict(training_ids=[],group_id=group.id)
    window.groups_page.individual.attestation_requested.emit([members[0].id],None)
    assert calls[-1]['group_id'] is None


def test_results_table_preview_register_sign(context,monkeypatch):
    window,contracts,group,members,student=context
    enroll(window,group,members)
    repo=window.attestations_page.repository
    monkeypatch.setattr(ui,'open_local_docx',lambda path:None)
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox,'warning',lambda *a,**k:None)
    dialog=ProtocolDialog(repo,training_ids=[t.id for t in members],group_id=group.id)
    assert all(not r.result for r in dialog.results())
    dialog.day.setText('2020-04-01'); dialog.attestation_day.setText('2020-04-01')
    dialog.results_table.cellWidget(0,1).setCurrentIndex(1)
    dialog.results_table.cellWidget(1,1).setCurrentIndex(3)
    dialog.results_table.item(0,2).setText('зачёт')
    assert dialog.save() and not dialog.is_dirty() and dialog.original.number is None
    assert dialog.preview() and dialog.register_button.isEnabled()
    dialog.chair.setText('Председатель Тестов')
    assert dialog.is_dirty() and not dialog.register_button.isEnabled()
    assert dialog.preview() and dialog.register()
    p=dialog.saved_protocol; page=window.attestations_page; page.refresh(p.id)
    assert page.sign_button.isEnabled() and page.copy_button.isEnabled()
    signing=ProtocolSigningDialog(repo,p)
    assert not signing.confirm()
    signing.day.setText('2020-04-02'); signing.responsible.setText('Ответственный Примеров'); signing.signed.setChecked(True)
    assert signing.confirm()
    page.refresh(p.id); assert not page.sign_button.isEnabled()
    assert repo.results(p.id)[1].result=='absent'
    dialog.close(); signing.close()


def test_selection_add_remove_and_dirty_close(context,monkeypatch):
    window,contracts,group,members,student=context
    enroll(window,group,members)
    dialog=ProtocolDialog(window.attestations_page.repository,group_id=group.id)
    assert dialog.available.rowCount()==len(members)
    dialog.available.selectRow(0); dialog.add_trainings()
    assert len(dialog.results())==1 and not dialog.results()[0].result
    dialog.results_table.selectRow(0); dialog.remove_trainings(); assert not dialog.results()
    dialog.available.selectRow(0); dialog.add_trainings()
    assert dialog.is_dirty()
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Cancel)
    dialog.reject(); assert dialog.is_dirty()
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Save)
    dialog.reject(); assert not dialog.is_dirty()
    dialog.close()
