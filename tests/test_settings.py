from pathlib import Path

import pytest

from proficentr import settings
from proficentr.services import open_database


def test_override_and_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("PROFICENTR_DATA_DIR", str(tmp_path))
    with open_database() as database:
        assert database.path == tmp_path / "proficentr.sqlite3"
        stamp = database.connection.execute("SELECT applied_at FROM schema_migrations").fetchone()
    with open_database() as database:
        assert database.connection.execute("SELECT applied_at FROM schema_migrations").fetchone() == stamp


@pytest.mark.parametrize("value", ["", "relative/path"])
def test_invalid_override(value, monkeypatch):
    monkeypatch.setenv("PROFICENTR_DATA_DIR", value)
    with pytest.raises(ValueError):
        settings.data_directory()


def test_linux_location(tmp_path, monkeypatch):
    monkeypatch.delenv("PROFICENTR_DATA_DIR", raising=False)
    monkeypatch.setattr(settings.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert settings.data_directory() == tmp_path / "proficentr"


def test_windows_location(tmp_path, monkeypatch):
    monkeypatch.delenv("PROFICENTR_DATA_DIR", raising=False)
    monkeypatch.setattr(settings.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(settings, "_require_local", lambda path: None)
    assert settings.data_directory() == tmp_path / "Proficentr"


def test_unc_rejected():
    with pytest.raises(ValueError, match="локальном"):
        settings._require_local(Path("//server/share/database"))
