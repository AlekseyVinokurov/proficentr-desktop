import os
import pytest

from proficentr import settings

os.environ["QT_QPA_PLATFORM"] = "offscreen"


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "config_file", lambda: tmp_path / "config" / "settings.json")
    # Изоляция настроек дочерних процессов, запускаемых тестом точки входа.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "child-config"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "child-appdata"))
    monkeypatch.delenv("PROFICENTR_DATA_DIR", raising=False)


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    """Ordinary operations must never reach a real network, including Qt requests."""
    import socket
    from PySide6.QtNetwork import QNetworkAccessManager

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected network access in test")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(QNetworkAccessManager, "createRequest", forbidden)
