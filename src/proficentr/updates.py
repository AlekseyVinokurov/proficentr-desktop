"""Official releases only. No storage objects or user information enter this transport."""

import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import time
import urllib.request
from urllib.parse import urlsplit
import zipfile

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

VERSION = "0.2.0"
REPOSITORY = "AlekseyVinokurov/proficentr-desktop"
OFFICIAL_URL = "https://github.com/" + REPOSITORY
API_URL = "https://api.github.com/repos/" + REPOSITORY + "/releases/latest"
# Provisioned independently of downloaded release metadata. Rotation requires a reviewed build.
PUBLIC_KEY = "21a35b5eadc91073fd39e84bb8a6efd5bf439610de06cff2b8841293e35da84c"
MAX_PACKAGE = 512 * 1024 * 1024
MAX_UNPACKED = 2 * 1024 * 1024 * 1024


def version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise ValueError("Требуется стабильная версия X.Y.Z.")
    return tuple(map(int, value.removeprefix("v").split(".")))


def allowed_url(url: str, *, redirect: bool = False) -> bool:
    p = urlsplit(url)
    if p.scheme != "https" or p.port not in (None, 443) or p.username or p.password or p.fragment:
        return False
    if p.hostname == "api.github.com":
        return url == API_URL
    if p.hostname == "github.com":
        return p.path.startswith(f"/{REPOSITORY}/releases/download/v") and not p.query
    # GitHub's signed, expiring asset redirect. Never accepted as an initial source.
    return redirect and p.hostname == "release-assets.githubusercontent.com"


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl, redirect=True):
            raise ValueError("Источник обновления не разрешён.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, destination: Path, limit: int) -> None:
    if not allowed_url(url):
        raise ValueError("Источник обновления не разрешён.")
    # Ignore environment proxies; send fixed headers only, never identifiers/configuration.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _Redirect())
    request = urllib.request.Request(url, headers={"User-Agent": "Proficentr-Updater", "Accept": "application/json"})
    total = 0
    deadline = time.monotonic() + (120 if limit <= 1024 * 1024 else 600)
    try:
        with opener.open(request, timeout=20) as response, destination.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > limit or time.monotonic() > deadline:
                    raise ValueError("Превышен размер загрузки.")
                output.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def release_info(directory: Path) -> dict | None:
    path = directory / "release.json"
    download(API_URL, path, 1024 * 1024)
    info = json.loads(path.read_bytes())
    if info.get("draft") or info.get("prerelease"):
        raise ValueError("Релиз не является стабильным.")
    tag = info["tag_name"]
    if tag != "v" + tag.removeprefix("v"):
        raise ValueError("Неверный тег релиза.")
    if version(tag) <= version(VERSION):
        return None
    assets = {}
    for asset in info["assets"]:
        name = asset["name"]
        url = asset["browser_download_url"]
        expected = OFFICIAL_URL + f"/releases/download/{tag}/{name}"
        if url != expected or not allowed_url(url) or name in assets:
            raise ValueError("Неверный источник файлов релиза.")
        assets[name] = url
    return {"version": tag.removeprefix("v"), "notes": str(info.get("body") or "")[:12000], "assets": assets}


def verify_manifest(raw: bytes, signature: bytes, expected_version: str) -> dict:
    if not PUBLIC_KEY:
        raise ValueError("Ключ официальных обновлений ещё не настроен в этой сборке.")
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY)).verify(signature, raw)
    manifest = json.loads(raw)
    if set(manifest) != {"format", "version", "filename", "size", "sha256", "platform", "min_updater", "schema"}:
        raise ValueError("Неподдерживаемый манифест.")
    if (manifest["format"] != 1 or manifest["platform"] != "windows-x86_64"
            or manifest["min_updater"] != 1 or manifest["version"] != expected_version
            or version(manifest["version"]) <= version(VERSION)
            or not isinstance(manifest["schema"], int) or manifest["schema"] < 1):
        raise ValueError("Несовместимое обновление.")
    if manifest["filename"] != f"Proficentr-{expected_version}-Windows.zip":
        raise ValueError("Неверное имя пакета.")
    if type(manifest["size"]) is not int or not 0 < manifest["size"] <= MAX_PACKAGE:
        raise ValueError("Неверный размер пакета.")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]):
        raise ValueError("Неверная контрольная сумма.")
    return manifest


def unpack(package: Path, manifest: dict, destination: Path) -> Path:
    if package.stat().st_size != manifest["size"]:
        raise ValueError("Размер пакета не совпадает.")
    with package.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != manifest["sha256"]:
        raise ValueError("Контрольная сумма пакета не совпадает.")
    destination.mkdir()  # Must be new; never overlay existing code or data.
    try:
        with zipfile.ZipFile(package) as archive:
            entries = archive.infolist()
            if len(entries) > 30000 or sum(e.file_size for e in entries) > MAX_UNPACKED:
                raise ValueError("Превышен размер распакованного пакета.")
            seen = set()
            for entry in entries:
                name = entry.filename
                path = PurePosixPath(name)
                windows = PureWindowsPath(name)
                if (not path.parts or path.parts[0] != "Proficentr" or path.is_absolute()
                        or windows.drive or "\\" in name or ".." in path.parts
                        or any(":" in p or p.endswith((".", " ")) or PureWindowsPath(p).is_reserved() for p in path.parts)
                        or stat.S_ISLNK(entry.external_attr >> 16)
                        or name.casefold() in seen or entry.flag_bits & 1):
                    raise ValueError("Недопустимый путь внутри архива.")
                seen.add(name.casefold())
            # All names validated before any file is written. extractall is deliberately avoided.
            for entry in entries:
                target = destination.joinpath(*PurePosixPath(entry.filename).parts)
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
        result = destination / "Proficentr"
        if (not (result / "Proficentr.exe").is_file() or not (result / "_internal").is_dir()
                or not (result / "ProficentrUpdater.exe").is_file()):
            raise ValueError("Неполная onedir-сборка.")
        return result
    except Exception:
        shutil.rmtree(destination)
        raise


def prepare_release(info: dict, directory: Path) -> Path:
    assets = info["assets"]
    raw = directory / "manifest.json"
    sig = directory / "manifest.sig"
    download(assets["manifest.json"], raw, 16384)
    download(assets["manifest.sig"], sig, 64)
    manifest = verify_manifest(raw.read_bytes(), sig.read_bytes(), info["version"])
    package = directory / manifest["filename"]
    download(assets[manifest["filename"]], package, manifest["size"])
    return unpack(package, manifest, directory / "staged")
