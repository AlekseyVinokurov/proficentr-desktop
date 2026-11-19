"""Расположение локальных пользовательских данных."""

import os
import json
from pathlib import Path
import sys
import tempfile


def data_directory() -> Path:
    override = os.environ.get("PROFICENTR_DATA_DIR")
    if override is not None:
        path = Path(override).expanduser()
        if not override.strip() or not path.is_absolute():
            raise ValueError("PROFICENTR_DATA_DIR должен содержать абсолютный локальный путь.")
    elif (configured := configured_directory()) is not None:
        path = configured
    else:
        path = default_data_directory()
    return validate_directory(path)


def default_data_directory() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        path = (Path(base) if base else Path.home() / "AppData" / "Local") / "Proficentr"
    else:
        base = os.environ.get("XDG_DATA_HOME", "")
        root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".local" / "share"
        path = root / "proficentr"
    return path.resolve()


def config_file() -> Path:
    if sys.platform == "win32":
        return default_data_directory() / "settings.json"
    base = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".config"
    return root / "proficentr" / "settings.json"


def configured_directory() -> Path | None:
    file = config_file()
    if not file.exists():
        return None
    try:
        return validate_directory(Path(json.loads(file.read_text(encoding="utf-8"))["data_directory"]))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"Не удалось прочитать настройки хранилища: {file}") from error


def save_directory(path: Path) -> None:
    path = validate_directory(path)
    file = config_file()
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=file.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"data_directory": str(path)}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("Укажите абсолютный локальный путь к хранилищу.")
    _require_local(path)
    path = path.resolve()
    _require_local(path)
    return path


def _require_local(path: Path) -> None:
    """Отсекаем UNC, подключённые сетевые диски и известные сетевые ФС Linux."""
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("Базу SQLite необходимо хранить на локальном диске.")
    sync_names = {"onedrive", "dropbox", "google drive", "googledrive", "google-drive", "мой диск", "my drive"}
    if any(part.casefold() in sync_names or part.casefold().startswith("onedrive - ") for part in path.parts):
        raise ValueError("Активную базу нельзя хранить в каталоге облачной синхронизации.")
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "DROPBOX_HOME", "GOOGLE_DRIVE_HOME"):
        value = os.environ.get(name)
        if value and path.is_relative_to(Path(value).resolve()):
            raise ValueError("Активную базу нельзя хранить в каталоге облачной синхронизации.")
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
