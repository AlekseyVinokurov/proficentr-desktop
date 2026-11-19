"""CI-only end-to-end frozen helper test, using synthetic data and ephemeral signing key."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from proficentr import settings, updates
from proficentr.database import Database, MIGRATIONS
from proficentr.storage import Storage
from proficentr.counterparties import Counterparty
from proficentr.counterparty_repository import CounterpartyRepository


def wait_file(path, process, seconds=120):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        if path.exists():return
        if (path.parent/'result.txt').exists():
            raise RuntimeError((path.parent/'result.txt').read_text(encoding='utf-8'))
        if process.poll() is not None:raise RuntimeError('Helper exited before confirmation')
        time.sleep(.2)
    raise TimeoutError('Frozen update did not finish')


def main():
    bundle=Path('dist/Proficentr').resolve()
    archive=next(Path('dist').glob('Proficentr-*-Windows.zip')).resolve()
    with tempfile.TemporaryDirectory(prefix='proficentr-update-') as folder:
        root=Path(folder)
        os.environ['LOCALAPPDATA']=str(root/'config')
        os.environ['PROFICENTR_DATA_DIR']=str(root/'user-data')
        storage=Storage(root/'user-data');storage.initialize()
        with Database(storage.database_path) as db:
            db.connection.execute("INSERT INTO training_center(id,full_name) VALUES (1,'Synthetic CI center')")
            db.connection.execute("INSERT INTO students(surname,first_name,birth_date) VALUES ('Synthetic','Student','1990-01-01')")
            db.connection.commit()
        with Database(storage.database_path) as db:
            counterparty = CounterpartyRepository(db.connection).save(Counterparty(full_name='Synthetic CI customer'))
        settings.save_directory(storage.root)
        (storage.root/'attachments'/'synthetic.txt').write_text('preserve me')
        config_before=settings.config_file().read_bytes()
        install=root/'Программа с пробелами';shutil.copytree(bundle,install)
        (install/'old-marker').write_text('previous')
        work=root/'.proficentr-update-test';work.mkdir()
        # Only this CI process trusts the ephemeral fixture key. Bundled app key is unchanged.
        key=Ed25519PrivateKey.generate()
        updates.PUBLIC_KEY=key.public_key().public_bytes_raw().hex()
        target=updates.VERSION
        updates.VERSION='0.1.0'
        name=f'Proficentr-{target}-Windows.zip'
        raw=json.dumps(dict(format=1,version=target,filename=name,size=archive.stat().st_size,
                            sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),platform='windows-x86_64',
                            min_updater=1,schema=len(MIGRATIONS))).encode()
        assets={n:updates.OFFICIAL_URL+f'/releases/download/v{target}/{n}' for n in ['manifest.json','manifest.sig',name]}
        def fixture_download(url,destination,limit):
            assert url in assets.values()
            if destination.name=='manifest.json':destination.write_bytes(raw)
            elif destination.name=='manifest.sig':destination.write_bytes(key.sign(raw))
            else:shutil.copyfile(archive,destination)
        updates.download=fixture_download
        staged=updates.prepare_release(dict(version=target,assets=assets),work)
        helper=work/'ProficentrUpdater.exe';shutil.copyfile(install/'ProficentrUpdater.exe',helper)
        # Real frozen old app launches against populated storage; helper waits for its exit.
        old=subprocess.Popen([str(install/'Proficentr.exe'),'--smoke-test'])
        plan=dict(pid=old.pid,install=str(install),staged=str(staged),database=str(storage.database_path))
        plan_path=work/'plan.json';plan_path.write_text(json.dumps(plan))
        helper_process=subprocess.Popen([str(helper),str(plan_path)])
        new_pid=None
        try:
            assert old.wait(timeout=30)==0
            wait_file(work/'ready',helper_process)
            new_pid=json.loads((work/'ready').read_text())['pid']
            assert helper_process.wait(timeout=30)==0
            assert not (install/'old-marker').exists()
            assert (root/(install.name+'.previous')/'old-marker').read_text()=='previous'
            assert settings.config_file().read_bytes()==config_before
            assert (storage.root/'attachments'/'synthetic.txt').read_text()=='preserve me'
            with Database(storage.database_path) as db:
                assert db.connection.execute('SELECT full_name FROM training_center').fetchone()==('Synthetic CI center',)
                assert db.connection.execute('SELECT surname FROM students').fetchall()==[('Synthetic',)]
                assert CounterpartyRepository(db.connection).load(counterparty.id) == counterparty
        finally:
            if new_pid:subprocess.run(['taskkill','/PID',str(new_pid),'/T','/F'],check=False,capture_output=True)
            if helper_process.poll() is None:helper_process.kill();helper_process.wait()
        # Actual frozen probe rejects newer schema, leaving the installed files in place.
        with Database(storage.database_path) as db:
            db.connection.execute('INSERT INTO schema_migrations(version) VALUES (?)', (len(MIGRATIONS)+1,));db.connection.commit()
        result=subprocess.run([str(install/'Proficentr.exe'),'--update-probe',str(storage.database_path)],timeout=30)
        assert result.returncode!=0
        assert (install/'Proficentr.exe').is_file()
    print('Frozen Windows update: signature, ZIP, helper, restart, data preservation and incompatible DB OK')


if __name__=='__main__':main()
