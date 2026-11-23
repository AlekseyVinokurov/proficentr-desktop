"""CI-only end-to-end frozen helper test, using synthetic data and ephemeral signing key."""
import hashlib
import json
import os
import runpy
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
from proficentr.programs import Program
from proficentr.program_repository import ProgramRepository
from proficentr.training_repository import TrainingRepository
from proficentr.trainings import TrainingGroup, Training
from proficentr.document_templates import TemplateRepository
from proficentr.frdo_templates import FrdoTemplateRepository
from proficentr.frdo_preparation import FrdoPreparationRepository
from proficentr.contract_repository import ContractRepository
from proficentr.issued_documents import IssuedDocument, TransferEvent
from proficentr.issued_repository import IssuedDocumentRepository
from proficentr.document_delivery import DeliveryRepository
from proficentr.completion_orders import CompletionOrder, CompletionEntry
from proficentr.completion_repository import CompletionRepository
from proficentr.attestations import AttestationProtocol, AttestationResult
from proficentr.attestation_repository import AttestationRepository
from proficentr.enrollment_orders import EnrollmentOrder
from proficentr.enrollment_order_repository import EnrollmentOrderRepository
from proficentr.contracts import Contract
from docx import Document


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
            db.connection.execute("INSERT INTO training_center(id,full_name,ogrn,kpp) VALUES (1,'Synthetic CI center','1000000000000','000000001')")
            db.connection.execute("INSERT INTO students(surname,first_name,birth_date) VALUES ('Тестов','Иван','1990-01-01')")
            db.connection.commit()
        with Database(storage.database_path) as db:
            counterparty = CounterpartyRepository(db.connection).save(Counterparty(full_name='Synthetic CI customer'))
        with Database(storage.database_path) as db:
            programs = ProgramRepository(db.connection)
            active_program = programs.save(Program(full_name='Synthetic DPO program', direction='dpo', kind='dpo_upskilling', hours=72))
            archived_program = programs.save(Program(full_name='Synthetic PO program', direction='po', kind='po_retraining', hours=144, active=False, edition='old'))
        with Database(storage.database_path) as db:
            trainings = TrainingRepository(db.connection)
            active_group = trainings.save_group(TrainingGroup(name='Synthetic active group', program_id=active_program.id,
                                                               start_date='2020-01-01', end_date='2020-02-01'))
            archived_group = trainings.save_group(TrainingGroup(name='Synthetic archived group', program_id=active_program.id,
                                                                 start_date='2019-01-01', end_date='2019-02-01'))
            group_training = trainings.add_students(active_group.id, [1])[0]
            archived_training = trainings.add_students(archived_group.id, [1])[0]
            archived_group = trainings.set_active(archived_group.id, False)
            individual_training = trainings.save(Training(student_id=1, program_id=active_program.id,
                                                          start_date='2018-01-01', end_date='2018-02-01'))
        synthetic_frdo = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tests' / 'frdo_fixtures.py'))['preparation_template']
        with Database(storage.database_path) as db:
            frdo = FrdoTemplateRepository(db.connection, storage)
            frdo_versions = {}
            frdo_bytes = {}
            for kind in ('dpo', 'po'):
                source = root / f'synthetic-{kind}.xlsx'
                source.write_bytes(synthetic_frdo(kind))
                frdo_versions[kind] = frdo.register(source, kind)
                frdo_bytes[kind] = source.read_bytes()
        source_template = root / 'synthetic-template.docx'
        sample_docx = Document()
        sample_docx.add_paragraph('TEST {{ center.full_name }} - {{ student.full_name }}')
        sample_docx.save(source_template)
        with Database(storage.database_path) as db:
            templates = TemplateRepository(db.connection, storage)
            first_template = templates.register(source_template, 'other', 'ci', 'Synthetic template')
            sample_docx.add_paragraph('{{ program.hours }}')
            sample_docx.save(source_template)
            second_template = templates.register(source_template, 'other', 'ci', 'Synthetic template v2')
            first_template = templates.load(first_template.id)
            test_docx = templates.test_document(first_template.id)
            template_files = {version.relative_path: (storage.root / version.relative_path).read_bytes()
                              for version in (first_template, second_template)}
            original_test_docx = test_docx.read_bytes()
        sample_docx = Document()
        sample_docx.add_paragraph('SYNTHETIC CONTRACT {{ document.number }} {{ customer.full_name }} {{ contract.total_price }}')
        sample_docx.save(source_template)
        with Database(storage.database_path) as db:
            templates = TemplateRepository(db.connection, storage)
            templates.register(source_template, 'contract_person', 'dpo', 'Synthetic contract')
            contracts = ContractRepository(db.connection, storage)
            draft_contract = contracts.save(Contract(customer_type='student', student_id=1, contract_date='2020-01-10',
                                                      total_cents=12345, payment_terms='Synthetic payment terms'), [group_training.id])
            registered_contract = contracts.register(draft_contract.id, contracts.prepare(draft_contract.id).signature)
            historical = contracts.save(Contract(customer_type='person', person_data={'full_name': 'Synthetic payer'},
                                                 contract_date='2018-01-10', historical_number='CI-HISTORICAL-01',
                                                 total_cents=999, payment_terms='Synthetic offset terms'), [individual_training.id])
            historical = contracts.register(historical.id, contracts.prepare(historical.id).signature)
            annulled_contract = contracts.annul(historical.id, 'Synthetic cancellation reason')
            draft_contract = contracts.save(Contract(customer_type='student', student_id=1, contract_date='2020-02-01',
                                                     total_cents=500, payment_terms='Synthetic draft'), [group_training.id])
            contract_files = {contract.id: contracts.verify_document(contract.id).read_bytes()
                              for contract in (registered_contract, annulled_contract)}
        from proficentr.admission import AdmissionRepository
        ready_admission = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tests' / 'admission_fixtures.py'))['ready_for_admission']
        with Database(storage.database_path) as db:
            ready_admission(db.connection,storage,[group_training.id,individual_training.id])
            admission = AdmissionRepository(db.connection,storage)
            source_admission = root/'admission-test.docx'
            sample_admission = Document()
            sample_admission.add_paragraph('TEST {{ student.full_name }} {{ program.name }} {{ admission.title }} {{ admission.scope }}')
            sample_admission.save(source_admission)
            admission_template = admission.templates.register(source_admission,'application','admission','Synthetic application')
            admission_document = admission.prepare_form(group_training.id,'application',template_id=admission_template.id,
                day='2020-01-01',responsible='Synthetic responsible',expected_signature=admission.review(group_training.id).signature)
            admission.record(group_training.id,'application',state='verified',reason='Synthetic signed original checked',day='2020-01-01',
                responsible='Synthetic responsible',signed=True,document_id=admission_document,expected_signature=admission.review(group_training.id).signature)
            admission_bytes = admission.file(admission_document).read_bytes()
            admission_history = admission.history(group_training.id)
            admission_rules = admission.rules()
        sample_docx = Document()
        sample_docx.add_paragraph('{{ document.number }} {{ document.date }} {{ program.name }}')
        sample_docx.add_paragraph('{% for row in students %}{{ row.full_name }} {{ row.start_date }} {{ row.end_date }}{% endfor %}')
        sample_docx.save(source_template)
        with Database(storage.database_path) as db:
            orders = EnrollmentOrderRepository(db.connection, storage)
            orders.templates.register(source_template, 'enrollment_order', 'dpo-group', 'Synthetic group order')
            orders.templates.register(source_template, 'enrollment_order', 'dpo-individual', 'Synthetic individual order')
            registered_order = orders.save(EnrollmentOrder(order_date='2020-01-01',kind='group',group_id=active_group.id), [group_training.id])
            registered_order = orders.register(registered_order.id, orders.prepare(registered_order.id).signature)
            signed_order = orders.save(EnrollmentOrder(order_date='2020-01-01'), [individual_training.id])
            signed_order = orders.register(signed_order.id, orders.prepare(signed_order.id).signature)
            signed_order = orders.confirm_signing(signed_order.id, signed_date='2020-01-02',confirmed_by='Synthetic responsible',grounds_checked=True,education_checked=True)
            individual_training = TrainingRepository(db.connection).load(individual_training.id)
            order_files = {o.id: orders.verify_document(o.id).read_bytes() for o in (registered_order,signed_order)}
            order_templates = {v.id: v for v in orders.templates.list_versions('enrollment_order')}
            order_template_files = {v.relative_path: (storage.root / v.relative_path).read_bytes() for v in order_templates.values()}
        sample_docx = Document()
        sample_docx.add_paragraph('{{ protocol.number }} {{ protocol.date }} {{ protocol.kind }} {{ protocol.attestation_date }}')
        sample_docx.add_paragraph('{% for row in students %}{{ row.full_name }} {{ row.attestation_result }} {{ row.grade }}{% endfor %}')
        sample_docx.save(source_template)
        with Database(storage.database_path) as db:
            protocols = AttestationRepository(db.connection, storage)
            protocol_template = protocols.templates.register(source_template, 'protocol_dpo', 'synthetic', 'Synthetic attestation')
            protocol_files = {}
            saved_protocols = []
            for result, day in (('failed', '2020-02-01'), ('passed', '2020-03-01')):
                p = protocols.save(AttestationProtocol(protocol_date=day, attestation_date=day), [AttestationResult(individual_training.id, result)])
                p = protocols.register(p.id, protocols.prepare(p.id).signature)
                p = protocols.confirm_signing(p.id, signed_date=day, confirmed_by='Synthetic responsible')
                saved_protocols.append(p)
                protocol_files[p.id] = protocols.verify_document(p.id).read_bytes()
            registered_protocol = protocols.save(AttestationProtocol(protocol_date='2020-04-01', attestation_date='2020-04-01'), [AttestationResult(individual_training.id)])
            registered_protocol = protocols.register(registered_protocol.id, protocols.prepare(registered_protocol.id).signature)
            saved_protocols.append(registered_protocol)
            protocol_files[registered_protocol.id] = protocols.verify_document(registered_protocol.id).read_bytes()
            protocol_results = {p.id: protocols.results(p.id) for p in saved_protocols}
            protocol_sequence = db.connection.execute('SELECT * FROM attestation_sequences').fetchall()
            protocol_template_bytes = (storage.root / protocol_template.relative_path).read_bytes()
        sample_docx = Document()
        sample_docx.add_paragraph('{{ document.number }} {{ document.date }} {{ completion.date }}')
        sample_docx.add_paragraph('{% for row in students %}{{ row.full_name }} {{ row.completion_date }} {{ row.protocol_number }} {{ row.attestation_result }}{% endfor %}')
        sample_docx.save(source_template)
        with Database(storage.database_path) as db:
            completions = CompletionRepository(db.connection, storage)
            completion_template = completions.templates.register(source_template, 'completion_order', 'dpo-individual', 'Synthetic completion order')
            completion_orders = []
            for day in ('2020-03-02', '2020-03-03'):
                order = completions.save(CompletionOrder(order_date=day), [CompletionEntry(individual_training.id, '2020-03-01')])
                completion_orders.append(completions.register(order.id, completions.prepare(order.id).signature))
            completion_orders[0] = completions.confirm_signing(completion_orders[0].id, signed_date='2020-03-02', confirmed_by='Synthetic responsible', conditions_checked=True)
            completion_files = {order.id: completions.verify_document(order.id).read_bytes() for order in completion_orders}
            completion_entries = {order.id: completions.entries(order.id) for order in completion_orders}
            completion_fact = completions.completion(individual_training.id)
            completion_sequence = db.connection.execute('SELECT * FROM completion_sequences').fetchall()
            completion_template_bytes = (storage.root / completion_template.relative_path).read_bytes()
            individual_training = TrainingRepository(db.connection).load(individual_training.id)
        sample_docx = Document()
        sample_docx.add_paragraph('{{ issued.kind }} {{ issued.number }} {{ issued.date }} {{ student.full_name }} {{ program.name }}')
        sample_docx.add_paragraph('{{ issued.blank_series }} {{ issued.blank_number }} {{ issued.completion_order_number }} {{ issued.completion_date }}')
        sample_docx.save(source_template)
        from PySide6.QtGui import QImage, QColor
        evidence_source = root / 'synthetic-evidence.png'
        image = QImage(2, 2, QImage.Format.Format_RGB32); image.fill(QColor('red'))
        assert image.save(str(evidence_source), 'PNG')
        with Database(storage.database_path) as db:
            issued = IssuedDocumentRepository(db.connection, storage)
            issued_template = issued.templates.register(source_template, 'dpo_upskilling_certificate', 'synthetic', 'Synthetic issued certificate')
            issued_document = issued.save(IssuedDocument(training_id=individual_training.id, document_date='2020-03-04', blank_series='TEST', blank_number='0001'))
            issued_document = issued.register(issued_document.id, issued.prepare(issued_document.id).signature)
            preparation = FrdoPreparationRepository(db.connection, storage)
            prepared_frdo = preparation.save_details(issued_document.id,
                personal={'sex': 'Муж', 'citizenship_code': '000'},
                training={'form': 'Очная', 'funding': 'Платное обучение', 'relations': 'в образовательной организации', 'patronymic': 'Нет', 'qualification': 'Нет'},
                expected_signature=preparation.review(issued_document.id).signature)
            from proficentr.frdo_export import FrdoExportRepository
            frdo_exports = FrdoExportRepository(db.connection, storage)
            assert prepared_frdo.ready, prepared_frdo.messages()
            frdo_package = frdo_exports.create(frdo_exports.select([prepared_frdo]))[0]
            frdo_package_bytes = frdo_exports.file(frdo_package.id).read_bytes()
            frdo_history = db.connection.execute('SELECT * FROM frdo_change_history').fetchall()
            issued_bytes = issued.verify_document(issued_document.id).read_bytes()
            issued_sequence = db.connection.execute('SELECT * FROM issued_sequences').fetchall()
            issued_template_bytes = (storage.root / issued_template.relative_path).read_bytes()
            delivery = DeliveryRepository(issued)
            email_event = delivery.add(TransferEvent(document_id=issued_document.id, method='email', event_date='2020-03-05', recipient='Synthetic recipient', email='synthetic@example.test'))
            post_event = delivery.add(TransferEvent(document_id=issued_document.id, method='post', event_date='2020-03-05', recipient='Synthetic recipient', address='Synthetic postal address', tracking_number='SYNTHETIC-TRACK'))
            delivery.add(TransferEvent(document_id=issued_document.id, method='post_confirmation', event_date='2020-03-07', parent_event_id=post_event.id, received_date='2020-03-06', confirmation='Synthetic receipt'), evidence=evidence_source)
            transfer_history = delivery.history(issued_document.id)
            evidence_records = db.connection.execute('SELECT id,event_id,relative_path,sha256 FROM transfer_evidence').fetchall()
            evidence_bytes = {row[0]: delivery.verify_evidence(row[0]).read_bytes() for row in evidence_records}
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
                assert db.connection.execute('SELECT surname FROM students').fetchall()==[('Тестов',)]
                assert CounterpartyRepository(db.connection).load(counterparty.id) == counterparty
                assert ProgramRepository(db.connection).search() == [active_program]
                assert ProgramRepository(db.connection).load(archived_program.id) == archived_program
                trainings = TrainingRepository(db.connection)
                assert trainings.load_group(active_group.id) == active_group
                assert trainings.load_group(archived_group.id) == archived_group
                for training in (group_training, archived_training, individual_training):
                    assert trainings.load(training.id) == training
                assert db.connection.execute('PRAGMA foreign_key_check').fetchall() == []
                templates = TemplateRepository(db.connection, storage)
                assert templates.load(first_template.id) == first_template
                assert templates.load(second_template.id) == second_template
                for relative_path, data in template_files.items():
                    assert (storage.root / relative_path).read_bytes() == data
                assert test_docx.read_bytes() == original_test_docx
                # --smoke-test generated a new fictional DOCX inside the frozen EXE.
                assert len(list((storage.root / 'exports' / 'template-tests').glob('*.docx'))) >= 3
                assert templates.test_document(second_template.id).is_file()
                frdo = FrdoTemplateRepository(db.connection, storage)
                for kind, version in frdo_versions.items():
                    assert frdo.active(kind) == version
                    assert frdo.source(version) == frdo_bytes[kind]
                    assert frdo.structure(kind).dictionaries
                preparation = FrdoPreparationRepository(db.connection, storage)
                current_frdo = preparation.review(issued_document.id)
                frdo_exports = FrdoExportRepository(db.connection, storage)
                assert frdo_exports.load(frdo_package.id) == frdo_package
                assert frdo_exports.file(frdo_package.id).read_bytes() == frdo_package_bytes
                assert frdo_exports.create(frdo_exports.select([current_frdo])) == [frdo_package]
                assert current_frdo.cells == prepared_frdo.cells and current_frdo.signature == prepared_frdo.signature
                assert current_frdo.values['AE'] == '000' and current_frdo.values['AB'] == 'Очная'
                assert db.connection.execute('SELECT * FROM frdo_change_history').fetchall() == frdo_history
                contracts = ContractRepository(db.connection, storage)
                assert contracts.load(registered_contract.id) == registered_contract
                assert contracts.load(annulled_contract.id) == annulled_contract
                assert contracts.load(draft_contract.id) == draft_contract
                for contract_id, data in contract_files.items():
                    assert contracts.verify_document(contract_id).read_bytes() == data
                assert contracts.training_ids(registered_contract.id) == [group_training.id]
                assert trainings.load(group_training.id).state == 'preparation'
                admission = AdmissionRepository(db.connection,storage)
                assert admission.file(admission_document).read_bytes() == admission_bytes
                assert admission.history(group_training.id) == admission_history
                assert admission.rules() == admission_rules
                assert admission.review(group_training.id).ready
                assert admission.review(individual_training.id).historical
                assert db.connection.execute('SELECT count(*) FROM admission_enrollment_checks').fetchone() == (1,)
                orders = EnrollmentOrderRepository(db.connection, storage)
                assert orders.load(registered_order.id) == registered_order
                assert orders.load(signed_order.id) == signed_order
                for order_id, data in order_files.items():
                    assert orders.verify_document(order_id).read_bytes() == data
                for template_id, version in order_templates.items():
                    assert templates.load(template_id) == version
                for relative_path, data in order_template_files.items():
                    assert (storage.root / relative_path).read_bytes() == data
                assert orders.training_ids(registered_order.id) == [group_training.id]
                assert orders.training_ids(signed_order.id) == [individual_training.id]
                assert trainings.load(individual_training.id).state == 'completed'
                assert db.connection.execute('SELECT order_id FROM training_enrollments WHERE training_id=?', (individual_training.id,)).fetchone() == (signed_order.id,)
                protocols = AttestationRepository(db.connection, storage)
                for p in saved_protocols:
                    assert protocols.load(p.id) == p
                    assert protocols.results(p.id) == protocol_results[p.id]
                    assert protocols.verify_document(p.id).read_bytes() == protocol_files[p.id]
                assert protocols.current_confirmed_result(individual_training.id)[0] == saved_protocols[1].id
                assert protocols.templates.load(protocol_template.id) == protocol_template
                assert (storage.root / protocol_template.relative_path).read_bytes() == protocol_template_bytes
                assert db.connection.execute('SELECT * FROM attestation_sequences').fetchall() == protocol_sequence
                completions = CompletionRepository(db.connection, storage)
                for order in completion_orders:
                    assert completions.load(order.id) == order
                    assert completions.entries(order.id) == completion_entries[order.id]
                    assert completions.verify_document(order.id).read_bytes() == completion_files[order.id]
                assert completions.completion(individual_training.id) == completion_fact
                assert completions.templates.load(completion_template.id) == completion_template
                assert (storage.root / completion_template.relative_path).read_bytes() == completion_template_bytes
                assert db.connection.execute('SELECT * FROM completion_sequences').fetchall() == completion_sequence
                try:
                    completions.confirm_signing(completion_orders[1].id, signed_date='2020-03-03', confirmed_by='Synthetic responsible', conditions_checked=True)
                except ValueError:
                    pass
                else:
                    raise AssertionError('Duplicate completion accepted after update')
                issued = IssuedDocumentRepository(db.connection, storage)
                assert issued.load(issued_document.id) == issued_document
                assert issued.verify_document(issued_document.id).read_bytes() == issued_bytes
                assert issued.templates.load(issued_template.id) == issued_template
                assert (storage.root / issued_template.relative_path).read_bytes() == issued_template_bytes
                assert db.connection.execute('SELECT * FROM issued_sequences').fetchall() == issued_sequence
                delivery = DeliveryRepository(issued)
                assert delivery.history(issued_document.id) == transfer_history
                assert db.connection.execute('SELECT id,event_id,relative_path,sha256 FROM transfer_evidence').fetchall() == evidence_records
                for evidence_id, data in evidence_bytes.items():
                    assert delivery.verify_evidence(evidence_id).read_bytes() == data
                assert '2020-03-06' in issued.delivery_summary(issued_document.id)[0]
                assert issued.export_xlsx('2020-01-01', '2020-12-31').is_file()
                assert issued.draft_for(individual_training.id).id == issued_document.id
                assert list((storage.root / 'exports' / 'issued-registers').glob('*.xlsx'))
                package, warnings = orders.prepare_package(registered_order.id)
                assert (package / 'Приказ.docx').is_file()
                assert len(list(package.glob('*.docx'))) == 2
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
