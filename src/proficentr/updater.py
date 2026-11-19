"""Short-lived external helper. Never restores or deletes the user's database."""

import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess  # nosec B404 # explicit signed onedir executable paths, no shell.
import sys
import time
import uuid


def wait_for_exit(pid: int, timeout: int = 60) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Установка обновлений поддерживается в Windows.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # process already exited
            return
        raise OSError("Не удалось дождаться закрытия приложения.")
    try:
        if kernel.WaitForSingleObject(handle, timeout * 1000) != 0:
            raise TimeoutError("Приложение не закрыто.")
    finally:
        kernel.CloseHandle(handle)


def switch(install: Path, staged: Path, database: Path, work: Path, *, launch=subprocess.Popen) -> None:
    previous = install.with_name(install.name + ".previous")
    if previous.exists():
        raise RuntimeError("Сохранена предыдущая версия. Сначала проверьте её и перенесите вручную.")
    # Probe runs the new code against a copy. No production migration before successful probe.
    probe = subprocess.run([str(staged / "Proficentr.exe"), "--update-probe", str(database)], timeout=120, check=False)  # nosec B603 # executable from verified staged package.
    if probe.returncode:
        raise RuntimeError("Пробный запуск или проверка миграций не прошли.")
    ready = work / "ready"
    ready.unlink(missing_ok=True)
    install.rename(previous)
    try:
        staged.rename(install)
        process = launch([str(install / "Proficentr.exe"), "--update-ready", str(ready)], cwd=install)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if ready.is_file():
                return  # User may now work. No automatic database restore is ever allowed.
            if process.poll() is not None:
                break
            time.sleep(0.2)
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=20)
        raise RuntimeError("Новая версия не подтвердила запуск.")
    except Exception:
        if install.exists():
            install.rename(work / "failed-version")
        previous.rename(install)
        # Do not silently start old code against a possibly migrated database.
        raise


def apply(plan_path: Path) -> int:
    work = plan_path.parent
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        wait_for_exit(plan["pid"])
        from proficentr.instance import exclusive
        with exclusive("update"):
            # Refuse if another regular instance started while the old one exited.
            with exclusive("app"):
                pass
            switch(Path(plan["install"]), Path(plan["staged"]), Path(plan["database"]), work)
        (work / "result.txt").write_text("Обновление установлено. Предыдущая версия сохранена.", encoding="utf-8")
        return 0
    except Exception:
        message = ("Обновление не выполнено. Прежние программные файлы сохранены; база не восстанавливалась. "
                   "При изменении схемы старая программа отклонит базу. Сохраните хранилище и резервные копии; "
                   "для восстановления используйте совместимую версию. Подробности: docs/updates.md в официальном репозитории.")
        (work / "result.txt").write_text(message, encoding="utf-8")
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, message, "Proficentr — обновление", 0x10)
        return 1


def start_helper(staged: Path, database: Path, storage: Path) -> None:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        raise RuntimeError("Установка доступна в Windows-сборке из официального ZIP.")
    install = Path(sys.executable).resolve().parent
    if install == storage or install.is_relative_to(storage) or storage.is_relative_to(install):
        raise ValueError("Хранилище должно находиться вне папки программы.")
    if staged.parent.parent.parent != install.parent:
        raise ValueError("Неверный каталог подготовки обновления.")
    # Fail before shutting down if the directory is not user-writable. No elevation.
    work = staged.parent.parent
    helper = work / "helper"
    shutil.copytree(install, helper)
    plan = {"pid": os.getpid(), "install": str(install), "staged": str(staged), "database": str(database)}
    path = work / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    subprocess.Popen([str(helper / "Proficentr.exe"), "--apply-update", str(path)], cwd=work,  # nosec B603 # copied current executable.
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def work_directory() -> Path:
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        raise RuntimeError("Для установки используйте официальную Windows-сборку.")
    install = Path(sys.executable).resolve().parent
    if install.with_name(install.name + ".previous").exists():
        raise RuntimeError("Предыдущая версия сохранена рядом с программой. Проверьте и перенесите её перед новым обновлением.")
    try:
        work = install.parent / (".proficentr-update-" + uuid.uuid4().hex)
        work.mkdir()
        return work
    except OSError as error:
        raise RuntimeError("Распакуйте официальный ZIP в доступную вам папку, например %LOCALAPPDATA%\\Programs\\Proficentr. Хранилище данных оставьте на месте.") from error
