from PySide6.QtWidgets import QMessageBox
from test_contracts_ui import app, context
from test_attestations_ui import enroll
from proficentr.attestations import AttestationProtocol, AttestationResult
from proficentr.ui.completion_orders_page import CompletionDialog, CompletionSigningDialog
from proficentr.ui.groups_page import TrainingList
import proficentr.ui.completion_orders_page as ui


def prepare(window, group, members, tmp_path):
    enroll(window,group,members)
    repo=window.attestations_page.repository
    p=repo.save(AttestationProtocol(protocol_date='2020-02-01',attestation_date='2020-02-01',group_id=group.id),[AttestationResult(t.id,'passed') for t in members])
    p=repo.register(p.id,repo.prepare(p.id).signature)
    repo.confirm_signing(p.id,signed_date='2020-02-02',confirmed_by='Ответственный')
    from docx import Document
    source=tmp_path/'completion.docx'; doc=Document(); doc.add_paragraph('{{ document.number }} {{ completion.date }}')
    doc.add_paragraph('{% for row in students %}{{ row.full_name }} {{ row.completion_date }} {{ row.protocol_number }}{% endfor %}'); doc.save(source)
    repo.templates.register(source,'completion_order','dpo-group','Вымышленный приказ о завершении')
    repo.templates.register(source,'completion_order','dpo-individual','Вымышленный индивидуальный приказ')
    return p


def test_navigation_quick_actions_and_template_settings(context,monkeypatch):
    window,contracts,group,members,student=context; calls=[]
    monkeypatch.setattr(window.completion_orders_page,'open_card',lambda **args:calls.append(args))
    window.groups_page.completion_requested.emit([members[0].id],group.id)
    assert window.documents_page.currentWidget() is window.completion_orders_page
    assert calls==[dict(training_ids=[members[0].id],group_id=group.id)]
    window.groups_page.refresh(group.id); window.groups_page.completion_button.click()
    assert calls[-1]==dict(training_ids=[],group_id=group.id)
    window.groups_page.individual.completion_requested.emit([members[0].id],None)
    assert calls[-1]['group_id'] is None
    window.completion_orders_page.templates_requested.emit()
    assert window.pages.currentWidget() is window.settings_page
    assert window.settings_page.tabs.currentWidget() is window.settings_page.templates_page


def test_group_dates_preview_registration_then_signing(context,monkeypatch,tmp_path):
    window,contracts,group,members,student=context; protocol=prepare(window,group,members,tmp_path)
    repo=window.completion_orders_page.repository
    monkeypatch.setattr(ui,'open_local_docx',lambda path:None)
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox,'warning',lambda *a,**k:None)
    dialog=CompletionDialog(repo,training_ids=[t.id for t in members],group_id=group.id)
    assert [e.completion_date for e in dialog.entries()]==[t.end_date for t in members]
    assert protocol.number in dialog.selected.item(0,2).text()
    dialog.day.setText('2020-02-03'); dialog.selected.item(1,1).setText('2020-02-04')
    assert dialog.save() and dialog.original.number is None
    assert dialog.preview() and dialog.register_button.isEnabled()
    dialog.note.setPlainText('Примечание'); assert not dialog.register_button.isEnabled()
    assert dialog.preview() and dialog.register()
    order=dialog.saved_order; page=window.completion_orders_page; page.refresh(order.id)
    assert page.sign_button.isEnabled() and page.copy_button.isEnabled()
    assert all(window.groups_page.repository.load(t.id).state=='enrolled' for t in members)
    signing=CompletionSigningDialog(repo,order); signing.day.setText('2020-02-05'); signing.responsible.setText('Ответственный')
    assert not signing.confirm()
    signing.signed.setChecked(True); assert not signing.confirm()
    signing.conditions.setChecked(True); assert signing.confirm()
    page.refresh(order.id); assert not page.sign_button.isEnabled()
    listing=TrainingList(window.groups_page.repository,group_id=group.id,attestations=repo.attestations)
    listing.refresh(members[0].id)
    assert listing.table.item(listing.table.currentRow(),4).text()=='Завершён'
    assert 'Успешно прошёл' in listing.table.item(listing.table.currentRow(),5).text()
    assert not listing.completion_button.isEnabled() and not listing.exclude_button.isEnabled()
    dialog.close(); signing.close(); listing.close()


def test_selection_dirty_close_and_template_navigation(context,monkeypatch,tmp_path):
    window,contracts,group,members,student=context; prepare(window,group,members,tmp_path)
    dialog=CompletionDialog(window.completion_orders_page.repository,group_id=group.id)
    dialog.available.selectRow(0); dialog.add_trainings(); assert len(dialog.entries())==1
    dialog.selected.selectRow(0); dialog.remove_trainings(); assert not dialog.entries()
    dialog.available.selectRow(0); dialog.add_trainings(); assert dialog.is_dirty()
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Cancel)
    dialog.open_templates(); assert not dialog.go_to_templates
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Save)
    dialog.open_templates(); assert dialog.go_to_templates and not dialog.is_dirty()
    dialog.close()


def test_unsuccessful_visible_but_cannot_be_added(context,monkeypatch,tmp_path):
    window,contracts,group,members,student=context; prepare(window,group,members,tmp_path)
    repo=window.attestations_page.repository
    p=repo.save(AttestationProtocol(protocol_date='2021-01-01',attestation_date='2021-01-01'),[AttestationResult(members[0].id,'failed')])
    p=repo.register(p.id,repo.prepare(p.id).signature)
    repo.confirm_signing(p.id,signed_date='2021-01-02',confirmed_by='Ответственный')
    dialog=CompletionDialog(window.completion_orders_page.repository,group_id=group.id)
    errors=[]; monkeypatch.setattr(QMessageBox,'warning',lambda *a,**k:errors.append(a[2]))
    for i in range(dialog.available.rowCount()):
        if dialog.available.item(i,0).text().startswith('Вымышленный'):
            dialog.available.selectRow(i); break
    dialog.add_trainings(); assert not dialog.entries() and errors
    dialog.close()


def test_individual_completed_state_and_basis_refresh(context,monkeypatch,tmp_path):
    window,contracts,group,members,student=context; prepare(window,group,members,tmp_path)
    repo=window.completion_orders_page.repository
    dialog=CompletionDialog(repo,training_ids=[members[0].id])
    dialog.day.setText('2020-02-03'); assert dialog.save()
    protocol_repo=repo.attestations
    p=protocol_repo.save(AttestationProtocol(protocol_date='2020-02-02',attestation_date='2020-02-02'),[AttestationResult(members[0].id,'passed')])
    p=protocol_repo.register(p.id,protocol_repo.prepare(p.id).signature)
    protocol_repo.confirm_signing(p.id,signed_date='2020-02-03',confirmed_by='Тест')
    monkeypatch.setattr(QMessageBox,'warning',lambda *a,**k:None)
    assert not dialog.save()
    dialog.refresh_basis(); dialog.selected.item(0,1).setText('2020-02-02')
    assert dialog.entries()[0].protocol_id==p.id and dialog.save()
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**k:QMessageBox.StandardButton.Discard)
    dialog.close()
