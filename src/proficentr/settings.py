"""Расположение локальных пользовательских данных."""

import os
from pathlib import Path
import sys


def data_directory() -> Path:
    override = os.environ.get("PROFICENTR_DATA_DIR")
    if override is not None:
        path = Path(override).expanduser()
        if not override.strip() or not path.is_absolute():
            raise ValueError("PROFICENTR_DATA_DIR должен содержать абсолютный локальный путь.")
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        path = (Path(base) if base else Path.home() / "AppData" / "Local") / "Proficentr"
    else:
        base = os.environ.get("XDG_DATA_HOME", "")
        root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".local" / "share"
        path = root / "proficentr"
    path = path.resolve()
    _require_local(path)
    return path


def _require_local(path: Path) -> None:
    """Отсекаем UNC, подключённые сетевые диски и известные сетевые ФС Linux."""
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("Базу SQLite необходимо хранить на локальном диске.")
    if sys.platform == "win32":
        import ctypes

        if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4:
            raise ValueError("Базу SQLite нельзя хранить на сетевом диске.")
    elif sys.platform.startswith("linux"):
        mounts = Path("/proc/mounts")
        if mounts.exists():
            remote_types = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "9p"}
            matches = []
            for line in mounts.read_text().splitlines():
                fields = line.split()
                mount = Path(fields[1].replace("\\040", " ").replace("\\134", "\\"))
                if path.is_relative_to(mount):
                    matches.append((len(mount.parts), fields[2]))
            if matches and max(matches)[1] in remote_types:
                raise ValueError("Базу SQLite нельзя хранить на сетевой файловой системе.")
