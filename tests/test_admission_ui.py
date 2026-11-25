from datetime import date

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox,QFileDialog,QLabel
import pytest

from test_contracts import records
from test_admission import admission,context,record,form,TODAY
from admission_fixtures import ready_for_admission
from proficentr.ui.admission_page import (AdmissionPage,AdmissionContextDialog,AdmissionRecordDialog,AdmissionFormDialog,AdmissionRuleDialog,AdmissionRulesPage)
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
    assert 'Не хватает документов' in page.state.text()
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
    assert page.order_button.isEnabled() and 'Можно зачислять' in page.state.text()
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
    assert window.pages.currentWidget() is window.frdo_preparation_page
    assert window.documents_page.indexOf(window.frdo_preparation_page)==-1
    assert [window.documents_page.tabText(i) for i in range(window.documents_page.count())]==[
        'Договоры','Приказы о зачислении','Протоколы аттестации','Приказы о завершении','Выпускные документы']
    page=window.frdo_preparation_page
    page.kind.setCurrentIndex(1)
    start=page.start.date()
    window.navigation.setCurrentRow(SECTIONS.index('Документы'))
    assert window.pages.currentWidget() is window.documents_page
    window.navigation.setCurrentRow(SECTIONS.index('ФИС ФРДО'))
    assert window.pages.currentWidget() is page
    assert page.kind.currentIndex()==1 and page.start.date()==start
    window.navigation.setCurrentRow(SECTIONS.index('Настройки'))
    settings=window.settings_page
    settings.tabs.setCurrentWidget(settings.admission_rules_page)
    assert settings.tabs.tabText(settings.tabs.currentIndex())=='Требования приёма'
    assert settings.admission_rules_page.rules_table.rowCount()==len(r.admission.rules())
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


def test_empty_screen_and_explicit_selection(admission,monkeypatch):
    r=admission;app=QApplication.instance() or QApplication([])
    page=AdmissionPage(r.admission);page.show();app.processEvents()
    assert page.assessment is None and page.training.currentIndex()==-1
    assert 'Выберите вид обучения или программу' in page.empty.text()
    assert page.work_scroll.isVisible() and page.checklist and page.print_button.isEnabled()
    assert page.choose_button.isEnabled() and page.new_button.isEnabled()
    monkeypatch.setattr(QDialog,'exec',lambda self:QDialog.DialogCode.Rejected)
    page.choose_button.click()
    assert page.assessment is None and page.training.currentIndex()==-1
    def choose(dialog):
        page.training.setCurrentIndex(page.training.findData(r.key))
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog,'exec',choose)
    page.choose_button.click()
    assert page.assessment.training_id==r.key and page.work_scroll.isVisible()
    assert not page.empty.isVisible()
    assert r.admission.students.load(r.members[0].student_id).full_name in page.summary.text()
    assert r.program.full_name in page.summary.text() and 'ДПО' in page.summary.text()
    assert str(r.program.hours)+' ч.' in page.summary.text()
    assert r.members[0].start_date in page.summary.text()
    page.close()


def test_new_training_uses_existing_dialog_and_selects_saved_case(admission,monkeypatch):
    r=admission;app=QApplication.instance() or QApplication([]);page=AdmissionPage(r.admission)
    def create(dialog):
        dialog.student_id=r.members[0].student_id;dialog.update_student_label()
        dialog.program.setCurrentIndex(dialog.program.findData(r.program.id))
        dialog.fields['start_date'].setText('2027-01-01');dialog.fields['end_date'].setText('2027-01-31')
        assert dialog.save();return dialog.result()
    monkeypatch.setattr(TrainingDialog,'exec',create)
    page.new_button.click()
    assert page.assessment and page.assessment.training_id!=r.key
    assert '2027-01-01' in page.summary.text()
    page.close()


def test_row_actions_and_historical_snapshot(screen,monkeypatch):
    from dataclasses import replace
    r,page=screen
    old=r.admission.trainings.load(r.key).program_snapshot
    from proficentr.program_repository import ProgramRepository
    ProgramRepository(r.db.connection).save(replace(r.program,full_name='Новая редакция — не историческая'))
    page.refresh()
    assert 'Новая редакция' not in page.summary.text()
    assert r.admission.trainings.load(r.key).program_snapshot==old
    selected=[]
    monkeypatch.setattr(page,'prepare_form',lambda:selected.append(('form',page.table.currentRow())))
    monkeypatch.setattr(page,'edit_record',lambda:selected.append(('record',page.table.currentRow())))
    select(page,'application');row=page.table.currentRow();button=page.table.cellWidget(row,3)
    assert button.text()=='Сформировать';button.click();assert selected[-1]==('form',row)
    select(page,'dpo_education');row=page.table.currentRow();page.table.cellWidget(row,3).click()
    assert selected[-1]==('record',row)
    assert page.form_button.toolTip() and not page.form_button.isEnabled()
    context(r);page.refresh();select(page,'pd_consent')
    assert page.table.item(page.table.currentRow(),2).text()=='Не требуется'
    assert not page.table.cellWidget(page.table.currentRow(),3).isEnabled()
    assert not page.form_button.isEnabled()


def test_rules_editor_moved_and_versions_preserved(admission,monkeypatch):
    r=admission;app=QApplication.instance() or QApplication([])
    page=AdmissionRulesPage(r.admission)
    assert not page.edit_button.isEnabled()
    page.rules_table.selectRow(0);assert page.edit_button.isEnabled()
    before=r.admission.rules()[0]
    def edit(dialog):
        dialog.reference.setText('Уточнённый вымышленный локальный акт')
        dialog.responsible.setText('Тестов');assert dialog.save();return dialog.result()
    monkeypatch.setattr(AdmissionRuleDialog,'exec',edit)
    page.edit_button.click()
    after=next(rule for rule in r.admission.rules() if rule.rule_key==before.rule_key)
    assert after.version==before.version+1
    assert r.db.connection.execute('SELECT COUNT(*) FROM admission_rules WHERE rule_key=?',(before.rule_key,)).fetchone()[0]==2
    page.close()


@pytest.mark.parametrize('kind,expected',[
    ('dpo_upskilling','dpo_education'),('dpo_retraining','dpo_education'),
    ('po_initial_training','po_approved'),('po_retraining','po_profession_retraining'),
    ('po_upskilling','po_profession_upskilling')])
def test_preview_all_five_kinds_without_creating_records(admission,kind,expected):
    r=admission;app=QApplication.instance() or QApplication([])
    before=r.db.connection.total_changes
    page=AdmissionPage(r.admission);page.show();app.processEvents()
    assert page.kind.count()==5
    page.kind.setCurrentIndex(page.kind.findData(kind))
    assert page.assessment is None and page.training.currentIndex()==-1
    assert expected in {row.key for group in page.checklist.groups for row in group}
    assert 'Предварительный' in page.checklist.notice and 'не учтены' in page.checklist.notice
    assert page.work_scroll.isVisible() and page.print_button.isEnabled()
    assert len(page.group_layouts)==3
    assert r.db.connection.total_changes==before
    page.close()


def test_preview_program_uses_kind_hours_and_conditional_special_rules(admission):
    from dataclasses import replace
    from proficentr.admission import AdmissionRule
    from proficentr.program_repository import ProgramRepository
    r=admission;app=QApplication.instance() or QApplication([])
    program=ProgramRepository(r.db.connection).save(replace(r.program,id=None,full_name='Вымышленная переподготовка',kind='dpo_retraining',hours=249))
    r.admission.save_rule(AdmissionRule('medical_test','Вымышленная медицинская справка',program_id=program.id,
        condition_tag='internal_medical',source='special',reference='Вымышленное требование программы'),responsible='Тестов',day=TODAY)
    page=AdmissionPage(r.admission)
    assert 'medical_test' not in {row.key for group in page.checklist.groups for row in group}
    before=r.db.connection.total_changes
    page.program.setCurrentIndex(page.program.findData(program.id))
    assert page.kind.currentData()=='dpo_retraining'
    assert program.full_name in page.summary.text() and '249 ч.' in page.summary.text()
    rows=[row for group in page.checklist.groups for row in group]
    special=next(row for row in rows if row.key=='medical_test')
    assert 'Требование программы' in special.detail and 'При применимости' in special.detail
    assert 'internal_medical' not in special.detail
    assert any('минимум 250' in row.title for row in rows)
    assert r.db.connection.total_changes==before
    page.kind.setCurrentIndex(page.kind.findData('po_initial_training'))
    assert page.program.currentData() is None
    assert not any('минимум' in row.title for group in page.checklist.groups for row in group)
    page.close()


def test_print_preview_memo_a4_without_records(admission,monkeypatch,tmp_path):
    import proficentr.ui.admission_page as module
    from PySide6.QtGui import QPageSize,QTextDocument
    from PySide6.QtPrintSupport import QPrinter
    from proficentr.admission_checklist import memo_html,GROUPS
    r=admission;app=QApplication.instance() or QApplication([]);page=AdmissionPage(r.admission)
    page.program.setCurrentIndex(page.program.findData(r.program.id))
    before=r.db.connection.total_changes
    output=tmp_path/'memo.pdf';printers=[]
    def print_dialog(printer,parent):
        printers.append(printer)
        assert printer.pageLayout().pageSize().id()==QPageSize.PageSizeId.A4
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat);printer.setOutputFileName(str(output))
        class Dialog:
            def setWindowTitle(self,title):pass
            def exec(self):return QDialog.DialogCode.Accepted
        return Dialog()
    monkeypatch.setattr(module,'QPrintDialog',print_dialog)
    page.print_button.click()
    assert len(printers)==1 and output.read_bytes().startswith(b'%PDF-')
    text=QTextDocument();text.setHtml(memo_html(page.checklist));plain=text.toPlainText()
    assert r.program.full_name in plain and str(r.program.hours)+' ч.' in plain
    assert all(group in plain for group in GROUPS) and '☐' in plain and 'Ответственный' in plain
    assert r.db.connection.total_changes==before
    page.close()


def test_memo_escapes_program_and_rule_text(admission):
    from dataclasses import asdict,replace
    from proficentr.admission_checklist import checklist,memo_html
    value=checklist(admission.admission,asdict(replace(admission.program,full_name='<img src="secret">')),admission.program.id)
    html=memo_html(value)
    assert '<img' not in html and '&lt;img' in html


def test_consent_is_conditional_and_generated_only_when_applicable(screen,monkeypatch):
    r,page=screen
    assert 'если применимо' in ' '.join(row.title for row in page.checklist.groups[1])
    context(r);page.refresh()
    consent=next(row for row in page.checklist.groups[1] if row.key=='pd_consent')
    assert 'Не требуется' in consent.detail
    context(r,basis='consent');page.refresh();select(page,'pd_consent')
    dialog=AdmissionFormDialog(r.admission,r.key,page.items['pd_consent'])
    assert not dialog.save_button.isEnabled()
    assert 'Согласие' in ' '.join(label.text() for label in dialog.findChildren(QLabel))
    dialog.close()
    form(r,'pd_consent','consent');page.refresh()
    assert page.items['pd_consent'].status=='prepared' and not page.assessment.ready
    assert not page.order_button.isEnabled()


def test_contract_action_reuses_existing_workflow(screen,monkeypatch):
    r,page=screen;app=QApplication.instance()
    window=MainWindow(r.db.path,r.storage,r.db)
    calls=[]
    monkeypatch.setattr(window.contracts_page,'open_card',lambda **kwargs:calls.append(kwargs))
    window._open_admission(r.key)
    from PySide6.QtWidgets import QPushButton
    button=next(button for button in window.admission_page.workspace.findChildren(QPushButton) if button.text()=='Распечатать договор')
    button.click()
    assert calls==[dict(training_ids=[r.key],group_id=r.group.id)]
    assert window.documents_page.currentWidget() is window.contracts_page
    window.close()


def test_preview_new_training_keeps_selected_program(admission,monkeypatch):
    r=admission;app=QApplication.instance() or QApplication([]);page=AdmissionPage(r.admission)
    page.program.setCurrentIndex(page.program.findData(r.program.id))
    def cancel(dialog):
        assert dialog.program.currentData()==r.program.id
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(TrainingDialog,'exec',cancel)
    before=r.db.connection.total_changes;page.new_button.click()
    assert r.db.connection.total_changes==before and page.assessment is None
    page.close()


def test_historical_checklist_uses_original_rule_versions(screen):
    from dataclasses import replace
    from proficentr.admission_checklist import checklist
    import json
    r,page=screen
    frozen=replace(r.admission.review(r.key),historical=True)
    program=json.loads(r.trainings.load(r.key).program_snapshot)
    before=checklist(r.admission,program,r.program.id,frozen)
    old=next(rule for rule in r.admission.rules() if rule.rule_key=='application')
    r.admission.save_rule(replace(old,document_type='condition',title='Новая редакция'),
        responsible='Тестов',day=TODAY,expected_version=old.version)
    assert checklist(r.admission,program,r.program.id,frozen)==before
    assert any(row.key=='application' for row in before.groups[1])


def test_contract_missing_template_names_form_and_uses_connected_template(screen):
    from proficentr.ui.contracts_page import ContractDialog
    r,page=screen
    dialog=ContractDialog(r.repo,training_ids=[r.key])
    assert dialog.customer_student.currentData()==r.first.id
    assert 'Договор с физическим лицом' in dialog.template_hint.text()
    assert 'Настройки → Шаблоны документов' in dialog.template_hint.text()
    template=r.template();dialog.refresh_templates()
    assert dialog.template.currentData()==template.id and not dialog.template_hint.text()
    dialog.close()
