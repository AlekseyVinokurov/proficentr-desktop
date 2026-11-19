from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QScrollArea
import pytest

from proficentr.programs import KINDS, Program
from proficentr.program_repository import ProgramRepository
from proficentr.services import open_database, prepare_storage
from proficentr.ui.main_window import MainWindow, SECTIONS
from proficentr.ui.program_dialog import ProgramDialog, DuplicateDialog


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path):
    storage=prepare_storage(tmp_path/'archive')
    with open_database(storage) as db:
        yield ProgramRepository(db.connection)


def sample(**changes):
    return replace(Program(full_name='Учебная программа — пример',direction='dpo',kind='dpo_upskilling',hours=72),**changes)


@pytest.mark.parametrize('direction,kind',[(d,k) for d,kinds in KINDS.items() for k in kinds])
def test_direction_limits_kinds_and_card_can_save(app,repo,direction,kind):
    card=ProgramDialog(repo)
    assert not card.kind.isEnabled() and not card.is_dirty()
    card.direction.setCurrentIndex(card.direction.findData(direction))
    assert {card.kind.itemData(i) for i in range(1,card.kind.count())}==set(KINDS[direction])
    card.kind.setCurrentIndex(card.kind.findData(kind))
    card.fields['full_name'].setText('Тестовая программа')
    card.fields['hours'].setText('144')
    assert card.save() and card.saved_program.kind==kind
    assert not card.saved_program.grade and not card.saved_program.qualification


def test_direction_change_cannot_keep_incompatible_kind(app,repo,monkeypatch):
    saved=repo.save(sample())
    card=ProgramDialog(repo,saved)
    assert not card.is_dirty()
    card.direction.setCurrentIndex(card.direction.findData('po'))
    assert card.kind.currentData()==''
    assert card.kind.findData('dpo_upskilling')==-1
    messages=[]
    monkeypatch.setattr(QMessageBox,'warning',lambda *a:messages.append(a[-1]))
    assert not card.save() and messages
    assert repo.load(saved.id)==saved
    card.kind.setCurrentIndex(card.kind.findData('po_retraining'))
    assert card.save()
    assert repo.load(saved.id).kind=='po_retraining'


def test_invalid_hours_unsaved_close_cancel_and_discard(app,repo,monkeypatch):
    saved=repo.save(sample())
    card=ProgramDialog(repo,saved)
    card.show();app.processEvents()
    card.fields['hours'].setText('не число')
    assert card.is_dirty()
    warnings=[]
    monkeypatch.setattr(QMessageBox,'warning',lambda *a:warnings.append(a[-1]))
    monkeypatch.setattr(QMessageBox,'question',lambda *a:QMessageBox.StandardButton.Cancel)
    card.close();app.processEvents()
    assert card.isVisible() and repo.load(saved.id)==saved
    assert not card.save() and warnings
    monkeypatch.setattr(QMessageBox,'question',lambda *a:QMessageBox.StandardButton.Save)
    card.reject()
    assert card.isVisible() and repo.load(saved.id)==saved
    monkeypatch.setattr(QMessageBox,'question',lambda *a:QMessageBox.StandardButton.Discard)
    card.reject()
    assert not card.isVisible() and repo.load(saved.id)==saved


def test_save_dirty_card_and_archived_status_reload(app,repo,monkeypatch):
    saved=repo.save(sample())
    card=ProgramDialog(repo,saved)
    card.fields['edition'].setText('Редакция 2')
    card.active.setCurrentIndex(card.active.findData(False))
    monkeypatch.setattr(QMessageBox,'question',lambda *a:QMessageBox.StandardButton.Save)
    card.reject()
    assert card.result()==QDialog.DialogCode.Accepted
    reloaded=repo.load(saved.id)
    assert not reloaded.active and reloaded.edition=='Редакция 2'
    other=ProgramDialog(repo,reloaded)
    assert not other.is_dirty() and other.active.currentData() is False


@pytest.mark.parametrize('choice',['cancel','open','separate'])
def test_duplicate_choice_distinguishes_editions_and_archived_programs(app,repo,monkeypatch,choice):
    saved=repo.save(sample(edition='1',active=False))
    def decision(dialog):
        assert dialog.matches==[saved]
        assert 'редакция 1' in dialog.list.item(0).text()
        assert 'Архивная' in dialog.list.item(0).text()
        if choice=='open':dialog.open_selected()
        elif choice=='separate':dialog.confirm_separate()
        else:dialog.reject()
        return dialog.result()
    monkeypatch.setattr(DuplicateDialog,'exec',decision)
    card=ProgramDialog(repo,sample(edition='2',hours=108))
    assert card.save()==(choice=='separate')
    assert card.open_program_id==(saved.id if choice=='open' else None)
    assert repo.load(saved.id)==saved
    assert len(repo.search(active=None))==(2 if choice=='separate' else 1)


def test_registry_create_edit_and_filters_refresh_immediately(app,tmp_path,monkeypatch):
    storage=prepare_storage(tmp_path/'archive')
    with open_database(storage) as db:
        window=MainWindow(db.path,storage,db)
        window.resize(760,480)
        window.navigation.setCurrentRow(SECTIONS.index('Программы'))
        page=window.programs_page
        assert window.pages.currentWidget() is page
        assert page.active_filter.currentData() is True
        assert page.table.rowCount()==0 and not page.archive_button.isEnabled()
        def create(dialog):
            assert dialog.findChild(QScrollArea) is not None
            dialog.fields['full_name'].setText('Программа ПО — пример')
            dialog.fields['code'].setText('По-1')
            dialog.fields['hours'].setText('120')
            dialog.direction.setCurrentIndex(dialog.direction.findData('po'))
            dialog.kind.setCurrentIndex(dialog.kind.findData('po_initial_training'))
            dialog.save_button.click()
            return dialog.result()
        monkeypatch.setattr(ProgramDialog,'exec',create)
        page.search.setText('Старый фильтр')
        page.direction_filter.setCurrentIndex(page.direction_filter.findData('dpo'))
        page.add_button.click()
        assert page.table.rowCount()==1 and not page.search.text()
        assert page.direction_filter.currentData() is None
        assert page.table.item(0,1).text()=='ПО' and page.table.item(0,3).text()=='120'
        saved_id=page.table.item(0,0).data(Qt.ItemDataRole.UserRole)
        assert page.archive_button.isEnabled()
        def edit(dialog):
            assert dialog.original.id==saved_id
            dialog.fields['edition'].setText('2')
            dialog.fields['hours'].setText('150')
            assert dialog.save()
            return dialog.result()
        monkeypatch.setattr(ProgramDialog,'exec',edit)
        page.open_button.click()
        assert page.table.item(0,3).text()=='150'
        page.direction_filter.setCurrentIndex(page.direction_filter.findData('dpo'))
        assert page.table.rowCount()==0
        page.direction_filter.setCurrentIndex(page.direction_filter.findData('po'))
        assert page.table.rowCount()==1
        page.search.setText('по-1');page.refresh()
        assert page.table.rowCount()==1
        page.search.setText('Нет такого кода');page.refresh()
        assert page.table.rowCount()==0
        window.close()
    with open_database(storage) as db:
        assert ProgramRepository(db.connection).load(saved_id).edition=='2'


def test_archive_restore_buttons_and_cancel(app,tmp_path,monkeypatch):
    storage=prepare_storage(tmp_path/'archive')
    with open_database(storage) as db:
        window=MainWindow(db.path,storage,db)
        page=window.programs_page
        saved=page.repository.save(sample())
        page.refresh(saved.id)
        monkeypatch.setattr(QMessageBox,'question',lambda *a:QMessageBox.StandardButton.No)
        page.archive_button.click()
        assert page.repository.load(saved.id).active and page.table.rowCount()==1
        monkeypatch.setattr(QMessageBox,'question',lambda *a:QMessageBox.StandardButton.Yes)
        page.archive_button.click()
        assert page.table.rowCount()==0 and not page.repository.load(saved.id).active
        page.active_filter.setCurrentIndex(page.active_filter.findData(False))
        assert page.table.rowCount()==1
        page.table.selectRow(0)
        assert page.archive_button.text()=='Вернуть в активные'
        def inspect(dialog):
            assert dialog.original.id==saved.id and not dialog.original.active
            return QDialog.DialogCode.Rejected
        monkeypatch.setattr(ProgramDialog,'exec',inspect)
        page.open_button.click()
        page.archive_button.click()
        assert page.table.rowCount()==0 and page.repository.load(saved.id).active
        page.active_filter.setCurrentIndex(page.active_filter.findData(True))
        assert page.table.rowCount()==1
        window.close()


def test_open_duplicate_routes_to_archived_card(app,tmp_path,monkeypatch):
    storage=prepare_storage(tmp_path/'archive')
    with open_database(storage) as db:
        window=MainWindow(db.path,storage,db)
        page=window.programs_page
        saved=page.repository.save(sample(active=False))
        opened=[]
        def execute(dialog):
            opened.append(dialog.original.id)
            if dialog.original.id is None:dialog.open_program_id=saved.id
            return QDialog.DialogCode.Rejected
        monkeypatch.setattr(ProgramDialog,'exec',execute)
        page.open_card()
        assert opened==[None,saved.id] and page.table.rowCount()==0
        window.close()
