from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QAbstractItemView, QDialog, QMessageBox, QScrollArea
import pytest

from proficentr.counterparties import Counterparty
from proficentr.counterparty_repository import CounterpartyRepository
from proficentr.services import open_database, prepare_storage
from proficentr.ui.counterparty_dialog import CounterpartyDialog, DuplicateDialog
from proficentr.ui.main_window import MainWindow, SECTIONS


@pytest.fixture
def application():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path):
    storage = prepare_storage(tmp_path/'archive')
    with open_database(storage) as db:
        yield CounterpartyRepository(db.connection)


def sample():
    return Counterparty(full_name='ООО Тестовый заказчик', inn='1234567890', phone='+7 (000) 123-45-67', contact_name='Тестовый представитель')


def test_create_edit_search_and_reload_table(application, tmp_path, monkeypatch):
    storage = prepare_storage(tmp_path/'archive')
    with open_database(storage) as db:
        window = MainWindow(db.path, storage, db)
        window.navigation.setCurrentRow(SECTIONS.index('Организации'))
        page = window.counterparties_page
        assert window.pages.currentWidget() is page
        assert page.table.rowCount() == 0 and not page.open_button.isEnabled()
        assert page.table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers

        def create(dialog):
            assert dialog.findChild(QScrollArea) is not None
            for field in ('full_name', 'inn', 'phone', 'contact_name'):
                dialog.fields[field].setText(getattr(sample(), field))
            dialog.save_button.click()
            return dialog.result()

        page.search.setText('Другой фильтр')
        monkeypatch.setattr(CounterpartyDialog, 'exec', create)
        page.add_button.click()
        assert page.table.rowCount() == 1 and page.search.text() == ''
        assert [page.table.item(0,c).text() for c in range(5)] == [sample().full_name, sample().inn, 'Юридическое лицо', sample().phone, sample().contact_name]
        assert page.open_button.isEnabled()
        saved_id = page.table.item(0,0).data(Qt.ItemDataRole.UserRole)

        def edit(dialog):
            assert dialog.original.id == saved_id
            dialog.fields['full_name'].setText('ООО Изменённый пример')
            assert dialog.save()
            return dialog.result()

        monkeypatch.setattr(CounterpartyDialog, 'exec', edit)
        page.open_button.click()
        assert page.table.item(0,0).text() == 'ООО Изменённый пример'
        for query, count in [('ИЗМЕНЁН',1), ('1234567890',1), ('123 45 67',1), ('Нет такой организации',0)]:
            page.search.setText(query);page.refresh()
            assert page.table.rowCount() == count
        window.close()
    with open_database(storage) as db:
        window = MainWindow(db.path, storage, db)
        assert window.counterparties_page.repository.load(saved_id).full_name == 'ООО Изменённый пример'
        assert window.counterparties_page.table.rowCount() == 1
        window.close()


def test_ip_form_does_not_require_kpp(application, repo):
    card = CounterpartyDialog(repo)
    card.kind.setCurrentIndex(card.kind.findData('sole_proprietor'))
    assert not card.fields['kpp'].isEnabled()
    card.fields['full_name'].setText('ИП Учебный Пример')
    card.fields['inn'].setText('123456789012')
    assert card.save()
    saved = repo.load(card.saved_counterparty.id)
    assert saved.kind == 'sole_proprietor' and not saved.kpp


def test_changing_kind_never_silently_deletes_kpp(application, repo, monkeypatch):
    original = repo.save(replace(sample(), kpp='123456789'))
    card = CounterpartyDialog(repo, original)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.No)
    card.kind.setCurrentIndex(card.kind.findData('sole_proprietor'))
    assert card.kind.currentData() == 'legal_entity'
    assert card.fields['kpp'].text() == original.kpp and not card.is_dirty()
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Yes)
    card.kind.setCurrentIndex(card.kind.findData('sole_proprietor'))
    assert not card.fields['kpp'].isEnabled() and not card.fields['kpp'].text()
    assert repo.load(original.id) == original  # Type change is still unsaved.
    card.fields['inn'].setText('123456789012')
    assert card.save()
    assert repo.load(original.id).kind == 'sole_proprietor'


def test_cancel_discard_and_failed_save_preserve_record(application, repo, monkeypatch):
    original = repo.save(sample())
    card = CounterpartyDialog(repo, original)
    card.show();application.processEvents()
    card.fields['full_name'].setText('Несохранённый пример')
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Cancel)
    card.close();application.processEvents()
    assert card.isVisible() and repo.load(original.id) == original
    warnings = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda *a: warnings.append(a[-1]))
    card.fields['inn'].setText('bad')
    assert not card.save() and warnings
    assert repo.load(original.id) == original
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Discard)
    card.reject()
    assert not card.isVisible() and repo.load(original.id) == original


def test_save_when_closing_dirty_card(application, repo, monkeypatch):
    original = repo.save(sample())
    card = CounterpartyDialog(repo, original)
    card.fields['note'].setPlainText('Тестовое примечание')
    monkeypatch.setattr(QMessageBox, 'question', lambda *a: QMessageBox.StandardButton.Save)
    card.reject()
    assert card.result() == QDialog.DialogCode.Accepted
    assert repo.load(original.id).note == 'Тестовое примечание'


@pytest.mark.parametrize('choice', ['cancel', 'open', 'separate'])
def test_duplicate_choices(application, repo, monkeypatch, choice):
    existing = repo.save(sample())
    def decision(dialog):
        assert dialog.matches[0].reason == 'inn'
        assert 'Совпадение ИНН' in dialog.list.item(0).text()
        if choice == 'open':dialog.open_selected()
        elif choice == 'separate':dialog.confirm_separate()
        else:dialog.reject()
        return dialog.result()
    monkeypatch.setattr(DuplicateDialog, 'exec', decision)
    card = CounterpartyDialog(repo)
    card.fields['full_name'].setText(sample().full_name)
    card.fields['inn'].setText(sample().inn)
    assert card.save() == (choice == 'separate')
    assert len(repo.search()) == (2 if choice == 'separate' else 1)
    assert repo.load(existing.id) == existing
    assert card.open_counterparty_id == (existing.id if choice == 'open' else None)


def test_open_duplicate_from_registry(application, tmp_path, monkeypatch):
    storage = prepare_storage(tmp_path/'archive')
    with open_database(storage) as db:
        window = MainWindow(db.path, storage, db)
        page = window.counterparties_page
        existing = page.repository.save(sample())
        opened = []
        def execute(dialog):
            opened.append(dialog.original.id)
            if dialog.original.id is None:dialog.open_counterparty_id = existing.id
            return QDialog.DialogCode.Rejected
        monkeypatch.setattr(CounterpartyDialog, 'exec', execute)
        page.open_card()
        assert opened == [None, existing.id]
        assert len(page.repository.search()) == 1
        window.close()
