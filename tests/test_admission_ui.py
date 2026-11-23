from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox,QFileDialog
import pytest

from test_contracts import records
from test_admission import admission,context,record,form,TODAY
from admission_fixtures import ready_for_admission
from proficentr.ui.admission_page import (AdmissionPage,AdmissionContextDialog,AdmissionRecordDialog,AdmissionFormDialog,AdmissionRuleDialog)
from proficentr.ui.main_window import MainWindow,SECTIONS
from proficentr.ui.training_dialogs import TrainingDialog


@pytest.fixture
def screen(admission):
    app=QApplication.instance() or QApplication([])
    page=AdmissionPage(admission.admission);page.open_training(admission.key)
    yield admission,page
    page.close()


def select(page,key):
    for row in range(page.table.rowCount()):
        if page.table.item(row,0).data(Qt.ItemDataRole.UserRole)==key:
            page.table.selectRow(row);return
    raise AssertionError(key)


def test_initial_group_training_and_edit_basis_recheck(screen,monkeypatch):
    r,page=screen
    assert page.training.currentData()==r.key
    assert 'Не готов' in page.state.text()
    assert 'правовое основание' in page.problems.toPlainText()
    import proficentr.ui.admission_page as module
    def edit(dialog):
        dialog.basis.setCurrentIndex(dialog.basis.findData('contract'))
        dialog.reference.setText('Вымышленное обращение о заключении договора')
        dialog.scope.setText('Приём и обучение: необходимые сведения')
        dialog.responsible.setText('Тестов')
        assert dialog.save();return dialog.result()
    monkeypatch.setattr(module.AdmissionContextDialog,'exec',edit)
    page.context_button.click()
    consent=next(i for i in page.assessment.items if i.key=='pd_consent')
    assert not consent.required and consent.status=='not_required'
    assert not page.order_button.isEnabled()
    ready_for_admission(r.db.connection,r.storage,[r.key])
    page.refresh()
    assert page.order_button.isEnabled() and 'Готов' in page.state.text()
    requested=[];page.enrollment_requested.connect(lambda ids,group:requested.append((ids,group)))
    page.order_button.click();assert requested==[([r.key],r.group.id)]


def test_form_prepared_unsigned_then_record_verified(screen,monkeypatch):
    r,page=screen;context(r)
    # Register the fictional form without preparing it in advance.
    from docx import Document
    source=r.source.parent/'application.docx';doc=Document();doc.add_paragraph('TEST {{ student.full_name }} {{ admission.title }}');doc.save(source)
    r.admission.templates.register(source,'application','admission','Вымышленная форма')
    page.refresh();select(page,'application')
    assert page.form_button.isEnabled()
    import proficentr.ui.admission_page as module
    opened=[];monkeypatch.setattr(module,'open_local_docx',lambda path:opened.append(path))
    def prepare(dialog):
        dialog.responsible.setText('Тестов');assert dialog.save();return dialog.result()
    monkeypatch.setattr(module.AdmissionFormDialog,'exec',prepare)
    page.form_button.click();assert len(opened)==1 and opened[0].is_file()
    item=page.items['application'];assert item.status=='prepared'
    select(page,'application')
    def confirm(dialog):
        assert not dialog.signed.isChecked()
        dialog.state.setCurrentIndex(dialog.state.findData('verified'));dialog.signed.setChecked(True)
        dialog.reason.setText('Проверен вымышленный подписанный оригинал');dialog.responsible.setText('Тестов')
        assert dialog.save();return dialog.result()
    monkeypatch.setattr(module.AdmissionRecordDialog,'exec',confirm)
    page.record_button.click();assert page.items['application'].status=='verified'


def test_rule_editor_requires_basis_and_separate_optional_consent(screen,monkeypatch):
    r,page=screen;messages=[]
    monkeypatch.setattr(QMessageBox,'warning',lambda *args:messages.append(args[-1]))
    dialog=AdmissionRuleDialog(r.admission)
    dialog.title.setText('Дополнительное согласие на публикацию фотографий')
    dialog.typ.setCurrentIndex(dialog.typ.findData('consent'))
    assert not dialog.required.isChecked() and dialog.signed.isChecked()
    dialog.scope.setText('Публикация фотографий на сайте организации')
    dialog.responsible.setText('Тестов')
    assert not dialog.save() and messages
    dialog.reference.setText('Вымышленный локальный акт о публикациях')
    assert dialog.save()
    latest=r.admission.rules()[-1];assert latest.document_type=='consent' and not latest.required
    page.refresh();assert latest.rule_key in page.items


def test_main_menu_card_and_frdo_open_existing_pages(admission):
    r=admission;app=QApplication.instance() or QApplication([])
    window=MainWindow(r.db.path,r.storage,r.db)
    window._open_admission(r.key)
    assert window.pages.currentWidget() is window.admission_page
    assert window.admission_page.training.currentData()==r.key
    dialog=TrainingDialog(r.trainings,r.trainings.load(r.key))
    requested=[];dialog.admission_requested.connect(requested.append)
    dialog.admission_button.click();assert requested==[r.key]
    window.navigation.setCurrentRow(SECTIONS.index('ФИС ФРДО'))
    assert window.pages.currentWidget() is window.documents_page
    assert window.documents_page.currentWidget() is window.frdo_preparation_page
    assert window.documents_page.indexOf(window.frdo_preparation_page)>=0
    assert window.groups_page.individual.admission_button.text()=='Приём на обучение'
    window.close()


def test_cancel_and_stale_confirmation_leave_data_unchanged(screen,monkeypatch):
    r,page=screen;context(r);before=r.admission.history(r.key)
    item=next(i for i in r.admission.review(r.key).items if i.key=='application')
    dialog=AdmissionRecordDialog(r.admission,r.key,item)
    dialog.reason.setText('Не сохранять');dialog.reject()
    assert r.admission.history(r.key)==before
    dialog=AdmissionRecordDialog(r.admission,r.key,item)
    context(r,scope='Уточнённая область действия')
    messages=[];monkeypatch.setattr(QMessageBox,'warning',lambda *args:messages.append(args[-1]))
    dialog.reason.setText('Вымышленный оригинал');dialog.responsible.setText('Тестов')
    assert not dialog.save() and 'изменились' in messages[-1]
