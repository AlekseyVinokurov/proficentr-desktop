"""Единственная карточка учебного центра, валидация и логотип."""

from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
import os
import re
import sqlite3
from uuid import uuid4

from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QImageReader

from proficentr.storage import Storage

MAX_LOGO_BYTES = 5 * 1024 * 1024
MAX_LOGO_PIXELS = 25_000_000


@dataclass
class TrainingCenter:
    full_name: str = ""
    short_name: str = ""
    inn: str = ""
    kpp: str = ""
    ogrn: str = ""
    legal_address: str = ""
    actual_address: str = ""
    postal_address: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    bank_name: str = ""
    bik: str = ""
    settlement_account: str = ""
    correspondent_account: str = ""
    director_name: str = ""
    director_position: str = ""
    director_genitive: str = ""
    license_number: str = ""
    license_date: str = ""
    license_authority: str = ""
    logo_path: str = ""


def validate_center(center: TrainingCenter) -> TrainingCenter:
    center = TrainingCenter(**{key: value.strip() for key, value in asdict(center).items()})
    rules = {
        "inn": (r"(?:[0-9]{10}|[0-9]{12})", "ИНН: укажите 10 или 12 цифр."),
        "kpp": (r"[0-9]{4}[0-9A-Za-z]{2}[0-9]{3}", "КПП: укажите 9 знаков (цифры, в позициях 5–6 допустимы латинские буквы)."),
        "ogrn": (r"(?:[0-9]{13}|[0-9]{15})", "ОГРН: укажите 13 цифр, для ОГРНИП — 15."),
        "bik": (r"[0-9]{9}", "БИК: укажите 9 цифр."),
        "settlement_account": (r"[0-9]{20}", "Расчётный счёт: укажите 20 цифр."),
        "correspondent_account": (r"[0-9]{20}", "Корреспондентский счёт: укажите 20 цифр."),
        "email": (r"[^\s@]+@[^\s@]+\.[^\s@]+", "Проверьте email, например info@example.org."),
    }
    for field, (pattern, message) in rules.items():
        value = getattr(center, field)
        if value and not re.fullmatch(pattern, value):
            raise ValueError(message)
    if center.license_date:
        try:
            parsed = date.fromisoformat(center.license_date)
            if parsed.isoformat() != center.license_date:
                raise ValueError
        except ValueError as error:
            raise ValueError("Дата лицензии: укажите существующую дату в формате ГГГГ-ММ-ДД.") from error
    return center


def read_logo(path: Path) -> tuple[bytes, str]:
    """Ограничиваем чтение и проверяем содержимое, а не только расширение."""
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        raise ValueError("Логотип должен быть файлом PNG или JPG.")
    with path.open("rb") as stream:
        data = stream.read(MAX_LOGO_BYTES + 1)
    if len(data) > MAX_LOGO_BYTES:
        raise ValueError("Размер логотипа не должен превышать 5 МиБ.")
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    kind = bytes(reader.format()).decode("ascii", errors="replace")
    if kind not in {"png", "jpeg", "jpg"}:
        raise ValueError("Содержимое логотипа должно иметь формат PNG или JPG.")
    size = reader.size()
    if size.width() <= 0 or size.height() <= 0 or size.width() * size.height() > MAX_LOGO_PIXELS:
        raise ValueError("Недопустимые размеры логотипа (не более 25 млн пикселей).")
    if reader.read().isNull():
        raise ValueError("Не удалось прочитать изображение логотипа.")
    return data, ".png" if kind == "png" else ".jpg"


class TrainingCenterRepository:
    def __init__(self, connection: sqlite3.Connection, storage: Storage):
        self.connection = connection
        self.storage = storage
        self.cleanup_warning = ""

    def load(self) -> TrainingCenter:
        fields = tuple(asdict(TrainingCenter()))
        row = self.connection.execute(
            f"SELECT {', '.join(fields)} FROM training_center WHERE id = 1"  # nosec B608 # field names are fixed constants; values are bound parameters.
        ).fetchone()
        return TrainingCenter(**dict(zip(fields, row))) if row else TrainingCenter()

    def logo_file(self, relative: str) -> Path | None:
        if not relative:
            return None
        path = Path(relative)
        # База не может направлять чтение или удаление за пределы organization/.
        if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != "organization":
            raise ValueError("Некорректный относительный путь к логотипу.")
        file = self.storage.root / path
        if not file.resolve().is_relative_to((self.storage.root / "organization").resolve()):
            raise ValueError("Логотип находится вне каталога организации.")
        return file

    def save(self, center: TrainingCenter, *, logo_source: Path | None = None,
             remove_logo: bool = False) -> TrainingCenter:
        center = validate_center(center)
        if self.connection.in_transaction:
            raise RuntimeError("Завершите текущую операцию с базой перед сохранением.")
        self.storage.require_available()
        previous = self.load()
        # Изменение привязки доступно только через проверенные действия сервиса.
        center = replace(center, logo_path=previous.logo_path)
        created = None
        self.cleanup_warning = ""
        try:
            if logo_source is not None:
                data, extension = read_logo(logo_source)
                relative = f"organization/logo-{uuid4().hex}{extension}"
                created = self.logo_file(relative)
                with created.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                center = replace(center, logo_path=relative)
            elif remove_logo:
                center = replace(center, logo_path="")
            values = asdict(center)
            fields = tuple(values)
            with self.connection:
                self.connection.execute(
                    f"INSERT INTO training_center (id, {', '.join(fields)}) "  # nosec B608 # field names are fixed constants; values are bound parameters.
                    f"VALUES (1, {', '.join('?' for _ in fields)}) "
                    f"ON CONFLICT(id) DO UPDATE SET {', '.join(f'{field}=excluded.{field}' for field in fields)}",
                    tuple(values.values()),
                )
        except Exception:
            if created is not None:
                created.unlink(missing_ok=True)
            raise
        if previous.logo_path and previous.logo_path != center.logo_path:
            try:
                old = self.logo_file(previous.logo_path)
                old.unlink(missing_ok=True)
            except (OSError, ValueError):
                self.cleanup_warning = "Реквизиты сохранены, но старый файл логотипа не удалось удалить."
        return center
