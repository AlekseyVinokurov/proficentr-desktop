import json
import time

from PySide6.QtWidgets import QApplication

from proficentr import settings, updates
from proficentr.storage import Storage
from proficentr.ui.about_page import AboutPage


def finish(app, page):
    deadline=time.monotonic()+5
    while page.worker.isRunning() and time.monotonic()<deadline:
        app.processEvents()
        time.sleep(.001)
    assert page.worker.wait(1000)
    app.processEvents()


def test_background_check_and_plain_release_notes(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    page=AboutPage(Storage(tmp_path))
    monkeypatch.setattr(updates,'release_info',lambda *a:dict(version='0.3.0',notes='<b>untrusted release text</b>',assets={}))
    page.check_updates()
    assert not page.check.isEnabled()
    finish(app,page)
    assert '0.3.0' in page.state.text()
    assert page.notes.toPlainText()=='<b>untrusted release text</b>'
    assert page.install.isEnabled()
    page.close()


def test_check_throttled_after_failure(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    page=AboutPage(Storage(tmp_path))
    calls=[]
    def offline(*args):
        calls.append(True)
        raise OSError('private path must not reach the interface')
    monkeypatch.setattr(updates,'release_info',offline)
    page._daily();finish(app,page)
    assert len(calls)==1
    assert 'private path' not in page.state.text()
    assert page.check.isEnabled() and not page.install.isEnabled()
    page._daily()
    assert len(calls)==1
    page.check_updates();finish(app,page)
    assert len(calls)==2  # Manual check bypasses the daily throttle.
    page.close()
