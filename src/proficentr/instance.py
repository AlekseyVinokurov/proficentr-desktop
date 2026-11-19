"""Per-user Windows launch/update exclusion; handles are released on process exit."""
from contextlib import contextmanager
import ctypes
import hashlib
import sys

from proficentr.settings import config_file


@contextmanager
def exclusive(name: str):
    if sys.platform != "win32":
        yield
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    suffix = hashlib.sha256(str(config_file().resolve()).casefold().encode()).hexdigest()[:24]
    handle = kernel.CreateMutexW(None, False, "Local\\Proficentr-" + suffix + "-" + name)
    error = ctypes.get_last_error()
    if not handle:
        raise OSError("Не удалось установить блокировку запуска.")
    try:
        if error == 183:
            raise RuntimeError("Proficentr уже запущен или выполняет обновление. Закройте другие экземпляры и повторите запуск.")
        yield
    finally:
        kernel.CloseHandle(handle)
