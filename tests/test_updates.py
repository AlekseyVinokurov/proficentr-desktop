import hashlib
import io
import json
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from proficentr import updates, updater
from proficentr.database import Database


@pytest.fixture
def signed(monkeypatch):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(updates, 'PUBLIC_KEY', key.public_key().public_bytes_raw().hex())
    return key


def package(tmp_path, names=None):
    path = tmp_path / 'package.zip'
    with zipfile.ZipFile(path, 'w') as z:
        for name in (names or ['Proficentr/Proficentr.exe', 'Proficentr/_internal/runtime']):
            z.writestr(name, b'synthetic')
    return path


def manifest(path):
    return dict(format=1, version='0.3.0', filename='Proficentr-0.3.0-Windows.zip',
                size=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                platform='windows-x86_64', min_updater=1, schema=3)


@pytest.mark.parametrize('a,b', [('0.9.0','0.10.0'), ('v1.0.0','2.0.0'), ('1.2.9','1.2.10')])
def test_version_order(a,b):
    assert updates.version(a) < updates.version(b)


@pytest.mark.parametrize('value', ['1.0', '1.0.0-beta', '1.02.0', '../1.0.0', '1.0.0\n'])
def test_invalid_version(value):
    with pytest.raises(ValueError):
        updates.version(value)


@pytest.mark.parametrize('url', [
    'http://github.com/AlekseyVinokurov/proficentr-desktop/releases/download/v1.0.0/a',
    'https://github.com/attacker/proficentr-desktop/releases/download/v1.0.0/a',
    'https://api.github.com/repos/other/repo/releases/latest',
    'https://github.com.evil.test/a', 'https://github.com@evil.test/a',
    'https://release-assets.githubusercontent.com/file', 'https://example.com/a',
])
def test_unofficial_source(url, tmp_path):
    with pytest.raises(ValueError):
        updates.download(url, tmp_path/'out', 100)


def test_redirect_policy():
    assert updates.allowed_url('https://release-assets.githubusercontent.com/file?sig=abc', redirect=True)
    assert not updates.allowed_url('https://evil.test/file', redirect=True)
    req = updates.urllib.request.Request(updates.API_URL)
    with pytest.raises(ValueError):
        updates._Redirect().redirect_request(req, None, 302, '', {}, 'https://evil.test/file')


@pytest.mark.parametrize('tag,result', [('v0.3.0',True),('v0.2.0',False),('v0.1.0',False)])
def test_release_discovery(tag, result, monkeypatch, tmp_path):
    def download(url, dest, limit):
        assert url == updates.API_URL
        dest.write_text(json.dumps(dict(tag_name=tag, draft=False, prerelease=False, body='Changes', assets=[])))
    monkeypatch.setattr(updates,'download',download)
    info = updates.release_info(tmp_path)
    assert bool(info) == result


def test_offline(monkeypatch, tmp_path):
    def offline(*args):
        raise OSError('offline')
    monkeypatch.setattr(updates,'download',offline)
    with pytest.raises(OSError):
        updates.release_info(tmp_path)


def test_signed_package(signed, tmp_path):
    path=package(tmp_path)
    raw=json.dumps(manifest(path)).encode()
    checked=updates.verify_manifest(raw,signed.sign(raw),'0.3.0')
    assert (updates.unpack(path,checked,tmp_path/'stage')/'Proficentr.exe').read_bytes()==b'synthetic'


def test_wrong_signature(signed,tmp_path):
    raw=json.dumps(manifest(package(tmp_path))).encode()
    with pytest.raises(InvalidSignature):
        updates.verify_manifest(raw,b'0'*64,'0.3.0')
    changed=raw.replace(b'0.3.0',b'0.4.0')
    with pytest.raises(InvalidSignature):
        updates.verify_manifest(changed,signed.sign(raw),'0.4.0')


@pytest.mark.parametrize('field,value', [('platform','linux'),('min_updater',2),('version','0.1.0'),('filename','evil.exe'),('size',-1)])
def test_signed_incompatible_manifest(signed,tmp_path,field,value):
    data=manifest(package(tmp_path));data[field]=value
    raw=json.dumps(data).encode()
    with pytest.raises(ValueError):
        updates.verify_manifest(raw,signed.sign(raw),'0.3.0')


def test_corrupt_archive(tmp_path):
    path=tmp_path/'bad.zip';path.write_bytes(b'not a zip')
    with pytest.raises(zipfile.BadZipFile):
        updates.unpack(path,manifest(path),tmp_path/'stage')
    assert not (tmp_path/'stage').exists()


def test_hash_mismatch(tmp_path):
    path=package(tmp_path);data=manifest(path);data['sha256']='0'*64
    with pytest.raises(ValueError,match='сумма'):
        updates.unpack(path,data,tmp_path/'stage')


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'Proficentr/../../outside', 'Proficentr/C:/x',
                                  'Proficentr/aux.txt', 'Proficentr/x:stream', 'Proficentr/x.','Proficentr\\x'])
def test_unsafe_zip_path(tmp_path,name):
    path=package(tmp_path,[name])
    with pytest.raises(ValueError):
        updates.unpack(path,manifest(path),tmp_path/'stage')
    assert not (tmp_path/'stage').exists()


def test_zip_symlink(tmp_path):
    path=tmp_path/'link.zip'
    with zipfile.ZipFile(path,'w') as z:
        info=zipfile.ZipInfo('Proficentr/link');info.external_attr=0o120777<<16
        z.writestr(info,'../../outside')
    with pytest.raises(ValueError):
        updates.unpack(path,manifest(path),tmp_path/'stage')


def test_transport_contains_no_user_data(monkeypatch,tmp_path):
    seen=[]
    class Opener:
        def open(self,request,timeout):
            seen.append(request)
            return io.BytesIO(b'{}')
    monkeypatch.setattr(updates.urllib.request,'build_opener',lambda *a:Opener())
    updates.download(updates.API_URL,tmp_path/'out',100)
    request=seen[0]
    assert request.data is None
    assert request.full_url==updates.API_URL
    assert request.headers=={'User-agent':'Proficentr-Updater','Accept':'application/json'}


def test_no_network():
    with pytest.raises(AssertionError,match='network'):
        socket.create_connection(('example.com',443))


def test_switch_success_and_preserve_storage(tmp_path,monkeypatch):
    install=tmp_path/'app';install.mkdir();(install/'old').write_text('old')
    staged=tmp_path/'stage';staged.mkdir();(staged/'new').write_text('new')
    data=tmp_path/'storage';data.mkdir();db=data/'test.sqlite3'
    with Database(db) as connection:
        connection.connection.execute("INSERT INTO training_center(id,full_name) VALUES (1,'Synthetic')")
        connection.connection.commit()
    before=db.read_bytes()
    monkeypatch.setattr(updater.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0))
    def launch(args,**kwargs):
        Path(args[-1]).write_text('ready')
        return SimpleNamespace(poll=lambda:None)
    updater.switch(install,staged,db,tmp_path,launch=launch)
    assert (install/'new').exists()
    assert (tmp_path/'app.previous'/'old').exists()
    assert db.read_bytes()==before


@pytest.mark.parametrize('probe_code', [0,1])
def test_failed_switch_keeps_old_files(tmp_path,monkeypatch,probe_code):
    install=tmp_path/'app';install.mkdir();(install/'old').write_text('old')
    staged=tmp_path/'stage';staged.mkdir();(staged/'new').write_text('new')
    db=tmp_path/'test.sqlite3'
    with Database(db):pass
    before=db.read_bytes()
    monkeypatch.setattr(updater.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=probe_code))
    with pytest.raises(RuntimeError):
        updater.switch(install,staged,db,tmp_path,launch=lambda *a,**k:SimpleNamespace(poll=lambda:1))
    assert (install/'old').read_text()=='old'
    assert db.read_bytes()==before


def test_security_policy():
    import importlib.util
    path=Path(__file__).resolve().parents[1]/'scripts'/'security_check.py'
    spec=importlib.util.spec_from_file_location('policy',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert module.check()==[]
