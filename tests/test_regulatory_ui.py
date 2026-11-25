from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QPageSize
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox, QInputDialog

from test_regulatory_documents import registry, active
from proficentr.regulatory_documents import RegulatoryRepository
from proficentr.ui.regulatory_page import RegulatoryCard, RegulatoryPage, print_pdf
from proficentr.ui.settings_page import SettingsPage


def test_settings_registry_import_filter_card_links_and_restart(registry,monkeypatch):
    r=registry;settings=SettingsPage(r.db.path,r.storage,r.db)
    page=settings.regulatory_page
    assert settings.tabs.tabText(settings.tabs.indexOf(page))=='Нормативные документы'
    order=r.repo.add(replace(active(),title='Вымышленный приказ об утверждении',category='orders'),pdf=r.pdf)
    def add(dialog):
        dialog.fields['title'].setText('Вымышленное положение')
        dialog.category.setCurrentIndex(dialog.category.findData('local'))
        dialog.fields['document_date'].setText('2020-01-02')
        dialog.fields['effective_date'].setText('2020-02-01')
        dialog.status.setCurrentIndex(dialog.status.findData('active'))
        dialog.sources={'pdf':r.pdf,'docx':r.docx}
        dialog.links.item(0).setCheckState(Qt.CheckState.Checked)
        assert dialog.save();return dialog.result()
    monkeypatch.setattr(RegulatoryCard,'exec',add)
    page.buttons['add'].click()
    assert page.table.rowCount()==2
    page.search.setText('ПОЛОЖЕНИЕ');page.refresh();assert page.table.rowCount()==1
    page.category.setCurrentIndex(page.category.findData('license'));assert page.table.rowCount()==0
    page.category.setCurrentIndex(0);assert page.table.rowCount()==1
    version=r.repo.search('положение')[0]
    card=RegulatoryCard(r.repo,version)
    assert card.links.count()==1
    card.follow_link(card.links.item(0))
    assert card.current.id==order.id and card.fields['title'].text()==order.title
    card.close()
    r.repo.add(replace(order,id=None),pdf=r.pdf,previous_id=order.id)
    revision_card=RegulatoryCard(r.repo,version,new_revision=True)
    checked=[revision_card.links.item(i).data(Qt.ItemDataRole.UserRole) for i in range(revision_card.links.count())
             if revision_card.links.item(i).checkState()==Qt.CheckState.Checked]
    assert checked==[order.id]
    revision_card.close();settings.close()
    # Recreate the settings UI against persisted SQLite and managed files.
    settings=SettingsPage(r.db.path,r.storage,r.db)
    assert settings.regulatory_page.table.rowCount()==2
    settings.close()


def test_print_open_and_export_use_checked_snapshot(registry,monkeypatch,tmp_path):
    import proficentr.ui.regulatory_page as module
    r=registry;version=r.repo.add(active(),pdf=r.pdf)
    page=RegulatoryPage(r.repo);page.table.selectRow(0)
    opened=[];monkeypatch.setattr(module,'show_pdf',lambda data,parent:opened.append(data))
    page.buttons['open'].click();assert opened==[r.pdf.read_bytes()]
    output=tmp_path/'printed.pdf';printers=[]
    def print_dialog(printer,parent):
        printers.append(printer);printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setPageSize(QPageSize(QPageSize.PageSizeId.A4));printer.setOutputFileName(str(output))
        class Dialog:
            def exec(self):return QDialog.DialogCode.Accepted
        return Dialog()
    monkeypatch.setattr(module,'QPrintDialog',print_dialog)
    page.buttons['print'].click();assert output.read_bytes().startswith(b'%PDF-') and len(printers)==1
    target=tmp_path/'copy.pdf';monkeypatch.setattr(QFileDialog,'getSaveFileName',lambda *args:(str(target),'PDF (*.pdf)'))
    page.buttons['copy'].click();assert target.read_bytes()==r.pdf.read_bytes()
    (r.storage.root/version.pdf_path).write_bytes(b'corruption')
    errors=[];monkeypatch.setattr(QMessageBox,'warning',lambda *args:errors.append(args[-1]))
    page.buttons['open'].click();page.buttons['print'].click()
    assert len(errors)==2 and len(opened)==1 and len(printers)==1
    page.close()


def test_new_revision_archive_and_history_same_card(registry,monkeypatch):
    r=registry;first=r.repo.add(active(),pdf=r.pdf,docx=r.docx)
    page=RegulatoryPage(r.repo);page.table.selectRow(0)
    def save(dialog):
        assert dialog.previous.id==first.id
        assert dialog.sources=={'pdf':None,'docx':None}
        dialog.sources={'pdf':r.pdf,'docx':r.docx}
        dialog.status.setCurrentIndex(dialog.status.findData('active'))
        dialog.fields['edition'].setText('Вторая редакция')
        assert dialog.save();return dialog.result()
    monkeypatch.setattr(RegulatoryCard,'exec',save)
    page.buttons['version'].click()
    current=r.repo.search()[0]
    assert current.revision==2 and r.repo.load(first.id).status=='archived'
    card=RegulatoryCard(r.repo,current)
    assert card.revisions.count()==2
    card.revisions.setCurrentIndex(card.revisions.findData(first.id))
    assert card.current.id==first.id and card.status.currentData()=='archived'
    card.close();page.table.selectRow(0)
    monkeypatch.setattr(QInputDialog,'getText',lambda *args:('Вымышленное основание',True))
    page.buttons['archive'].click()
    assert r.repo.load(current.id).status=='archived'
    page.close()


def test_cancel_readonly_and_docx_copy(registry,monkeypatch,tmp_path):
    import proficentr.ui.regulatory_page as module
    r=registry;v=r.repo.add(active(),pdf=r.pdf,docx=r.docx)
    def employee(permission):
        if permission=='regulatory.write':raise PermissionError('no write')
    repo=RegulatoryRepository(r.db.connection,r.storage,access_check=employee)
    page=RegulatoryPage(repo);page.table.selectRow(0)
    assert not page.buttons['add'].isEnabled() and not page.buttons['archive'].isEnabled()
    assert not page.buttons['version'].isEnabled() and page.buttons['copy'].isEnabled()
    card=RegulatoryCard(repo,v);opened=[]
    monkeypatch.setattr(module,'open_local_docx',lambda path:opened.append(path))
    card.open_docx();assert opened[0].read_bytes()==r.docx.read_bytes()
    out=tmp_path/'source.docx'
    monkeypatch.setattr(QFileDialog,'getSaveFileName',lambda *args:(str(out),'Исходник DOCX (*.docx)'))
    page.buttons['copy'].click();assert out.read_bytes()==r.docx.read_bytes()
    before=r.db.connection.total_changes
    card.close();dialog=RegulatoryCard(r.repo);dialog.fields['title'].setText('Не сохранять');dialog.reject()
    assert r.db.connection.total_changes==before
    page.close()


def test_builtin_pdf_viewer_receives_checked_bytes(registry,monkeypatch):
    from PySide6.QtPdfWidgets import QPdfView
    from proficentr.ui.regulatory_page import show_pdf
    seen=[]
    def inspect(dialog):
        view=dialog.findChildren(QPdfView)[0]
        seen.append(view.document().pageCount())
        assert view.pageMode()==QPdfView.PageMode.MultiPage
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(QDialog,'exec',inspect)
    show_pdf(registry.pdf.read_bytes(),None)
    assert seen==[1]
