"""SQLite и последовательные транзакционные миграции."""

from contextlib import closing
from pathlib import Path
import uuid
import sqlite3

from proficentr.settings import _require_local


# Каждая новая миграция добавляется в конец; уже применённые не изменяются.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    ("""CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",),
    ("""CREATE TABLE training_center (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        full_name TEXT NOT NULL DEFAULT '', short_name TEXT NOT NULL DEFAULT '',
        inn TEXT NOT NULL DEFAULT '', kpp TEXT NOT NULL DEFAULT '', ogrn TEXT NOT NULL DEFAULT '',
        legal_address TEXT NOT NULL DEFAULT '', actual_address TEXT NOT NULL DEFAULT '',
        postal_address TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '', website TEXT NOT NULL DEFAULT '',
        bank_name TEXT NOT NULL DEFAULT '', bik TEXT NOT NULL DEFAULT '',
        settlement_account TEXT NOT NULL DEFAULT '', correspondent_account TEXT NOT NULL DEFAULT '',
        director_name TEXT NOT NULL DEFAULT '', director_position TEXT NOT NULL DEFAULT '',
        director_genitive TEXT NOT NULL DEFAULT '', license_number TEXT NOT NULL DEFAULT '',
        license_date TEXT NOT NULL DEFAULT '', license_authority TEXT NOT NULL DEFAULT '',
        logo_path TEXT NOT NULL DEFAULT ''
    )""",),
    ("""CREATE TABLE students (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        surname TEXT NOT NULL, first_name TEXT NOT NULL,
        patronymic TEXT NOT NULL DEFAULT '', birth_date TEXT NOT NULL,
        snils TEXT NOT NULL DEFAULT '', citizenship TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
        registration_address TEXT NOT NULL DEFAULT '', residential_address TEXT NOT NULL DEFAULT '',
        passport_series TEXT NOT NULL DEFAULT '', passport_number TEXT NOT NULL DEFAULT '',
        passport_issued_by TEXT NOT NULL DEFAULT '', passport_issue_date TEXT NOT NULL DEFAULT '',
        passport_department_code TEXT NOT NULL DEFAULT '', education_level TEXT NOT NULL DEFAULT '',
        education_document TEXT NOT NULL DEFAULT '', education_details TEXT NOT NULL DEFAULT '',
        workplace TEXT NOT NULL DEFAULT '', job_title TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT ''
    )""",
     "CREATE INDEX students_snils_idx ON students(snils)",
     "CREATE INDEX students_birth_date_idx ON students(birth_date)"),
    ("""CREATE TABLE counterparties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ('legal_entity', 'sole_proprietor')),
        full_name TEXT NOT NULL CHECK (length(trim(full_name)) > 0),
        short_name TEXT NOT NULL DEFAULT '', inn TEXT NOT NULL DEFAULT '',
        kpp TEXT NOT NULL DEFAULT '' CHECK (kind != 'sole_proprietor' OR kpp = ''),
        ogrn TEXT NOT NULL DEFAULT '', legal_address TEXT NOT NULL DEFAULT '',
        actual_address TEXT NOT NULL DEFAULT '', postal_address TEXT NOT NULL DEFAULT '',
        phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', website TEXT NOT NULL DEFAULT '',
        contact_name TEXT NOT NULL DEFAULT '', contact_position TEXT NOT NULL DEFAULT '',
        bank_name TEXT NOT NULL DEFAULT '', bik TEXT NOT NULL DEFAULT '',
        settlement_account TEXT NOT NULL DEFAULT '', correspondent_account TEXT NOT NULL DEFAULT '',
        representative_name TEXT NOT NULL DEFAULT '', representative_position TEXT NOT NULL DEFAULT '',
        authority_basis TEXT NOT NULL DEFAULT '', power_of_attorney TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT ''
    )""",
     "CREATE INDEX counterparties_inn_idx ON counterparties(inn)"),
    ("""CREATE TABLE programs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT NOT NULL CHECK (length(trim(full_name)) > 0),
        short_name TEXT NOT NULL DEFAULT '', code TEXT NOT NULL DEFAULT '',
        direction TEXT NOT NULL, kind TEXT NOT NULL,
        hours INTEGER NOT NULL CHECK (typeof(hours) = 'integer' AND hours > 0),
        qualification TEXT NOT NULL DEFAULT '', grade TEXT NOT NULL DEFAULT '',
        approval_date TEXT NOT NULL DEFAULT '', approval_number TEXT NOT NULL DEFAULT '',
        approval_details TEXT NOT NULL DEFAULT '', edition TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        CHECK ((direction = 'dpo' AND kind IN ('dpo_upskilling', 'dpo_retraining')) OR
               (direction = 'po' AND kind IN ('po_initial_training', 'po_retraining', 'po_upskilling')))
    )""",
     "CREATE INDEX programs_active_direction_idx ON programs(active, direction)"),
    ("""CREATE TABLE training_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL CHECK (length(trim(name)) > 0),
        program_id INTEGER NOT NULL REFERENCES programs(id),
        start_date TEXT NOT NULL, end_date TEXT NOT NULL CHECK (end_date >= start_date),
        note TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        UNIQUE (id, program_id)
    )""",
     """CREATE TABLE trainings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id INTEGER NOT NULL REFERENCES students(id),
        program_id INTEGER NOT NULL REFERENCES programs(id),
        group_id INTEGER,
        start_date TEXT NOT NULL, end_date TEXT NOT NULL CHECK (end_date >= start_date),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        state TEXT NOT NULL DEFAULT 'preparation' CHECK (state IN ('preparation', 'excluded')),
        note TEXT NOT NULL DEFAULT '', program_snapshot TEXT NOT NULL,
        CHECK (state != 'excluded' OR group_id IS NOT NULL),
        FOREIGN KEY (group_id, program_id) REFERENCES training_groups(id, program_id)
            DEFERRABLE INITIALLY DEFERRED
    )""",
     "CREATE INDEX training_groups_program_idx ON training_groups(program_id)",
     "CREATE INDEX trainings_group_idx ON trainings(group_id)",
     "CREATE INDEX trainings_program_idx ON trainings(program_id)",
     "CREATE INDEX trainings_student_dates_idx ON trainings(student_id, start_date, end_date)",
     "CREATE UNIQUE INDEX trainings_group_student_idx ON trainings(group_id, student_id) WHERE state = 'preparation'"),
    ("""CREATE TABLE document_templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        document_type TEXT NOT NULL CHECK (length(trim(document_type)) > 0),
        variant TEXT NOT NULL CHECK (length(trim(variant)) > 0),
        name TEXT NOT NULL CHECK (length(trim(name)) > 0),
        version INTEGER NOT NULL CHECK (version > 0),
        relative_path TEXT NOT NULL UNIQUE,
        sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
        added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        active INTEGER NOT NULL CHECK (active IN (0, 1)),
        UNIQUE (document_type, variant, version)
    )""",
     "CREATE UNIQUE INDEX document_templates_active_idx ON document_templates(document_type, variant) WHERE active = 1"),
    ("""CREATE TABLE contracts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_type TEXT NOT NULL CHECK (customer_type IN ('student','person','legal_entity','sole_proprietor')),
        counterparty_id INTEGER REFERENCES counterparties(id), student_id INTEGER REFERENCES students(id),
        person_data TEXT NOT NULL DEFAULT '{}', contract_date TEXT NOT NULL,
        historical_number TEXT NOT NULL DEFAULT '', total_cents INTEGER NOT NULL CHECK (typeof(total_cents) = 'integer' AND total_cents >= 0),
        payment_terms TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','registered','annulled')),
        number TEXT, number_key TEXT UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        registered_at TEXT NOT NULL DEFAULT '', template_id INTEGER REFERENCES document_templates(id),
        document_path TEXT NOT NULL DEFAULT '', document_sha256 TEXT NOT NULL DEFAULT '', snapshot TEXT NOT NULL DEFAULT '{}',
        annul_reason TEXT NOT NULL DEFAULT '', annulled_at TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
        CHECK ((number IS NULL) = (number_key IS NULL)),
        CHECK ((state = 'draft' AND number IS NULL AND registered_at = '' AND document_path = '' AND snapshot = '{}') OR
               (state IN ('registered','annulled') AND length(number) > 0 AND length(number_key) > 0
                AND registered_at != '' AND template_id IS NOT NULL AND document_path != '' AND length(document_sha256) = 64 AND snapshot != '{}')),
        CHECK (state != 'annulled' OR (length(trim(annul_reason)) > 0 AND annulled_at != ''))
    )""",
     """CREATE TABLE contract_trainings (
        contract_id INTEGER NOT NULL REFERENCES contracts(id), training_id INTEGER NOT NULL REFERENCES trainings(id),
        position INTEGER NOT NULL, PRIMARY KEY (contract_id, training_id), UNIQUE (contract_id, position)
    )""",
     "CREATE INDEX contract_trainings_training_idx ON contract_trainings(training_id)",
     "CREATE INDEX contracts_counterparty_idx ON contracts(counterparty_id)",
     "CREATE INDEX contracts_student_idx ON contracts(student_id)",
     "CREATE INDEX contracts_template_idx ON contracts(template_id)",
     "CREATE INDEX contracts_date_idx ON contracts(contract_date)",
     "CREATE TABLE contract_sequences (year INTEGER PRIMARY KEY, last_value INTEGER NOT NULL CHECK (last_value > 0))",
     """CREATE TRIGGER contracts_no_delete BEFORE DELETE ON contracts WHEN OLD.state != 'draft'
        BEGIN SELECT RAISE(ABORT, 'Registered contract is immutable'); END""",
     """CREATE TRIGGER contracts_immutable BEFORE UPDATE ON contracts WHEN OLD.state != 'draft' AND NOT (
        OLD.state = 'registered' AND NEW.state = 'annulled' AND NEW.annul_reason != '' AND NEW.annulled_at != ''
        AND NEW.id IS OLD.id AND NEW.customer_type IS OLD.customer_type AND NEW.counterparty_id IS OLD.counterparty_id
        AND NEW.student_id IS OLD.student_id AND NEW.person_data IS OLD.person_data AND NEW.contract_date IS OLD.contract_date
        AND NEW.historical_number IS OLD.historical_number AND NEW.total_cents IS OLD.total_cents AND NEW.payment_terms IS OLD.payment_terms
        AND NEW.note IS OLD.note AND NEW.number IS OLD.number AND NEW.number_key IS OLD.number_key AND NEW.created_at IS OLD.created_at
        AND NEW.registered_at IS OLD.registered_at AND NEW.template_id IS OLD.template_id AND NEW.document_path IS OLD.document_path
        AND NEW.document_sha256 IS OLD.document_sha256 AND NEW.snapshot IS OLD.snapshot AND NEW.revision = OLD.revision + 1)
        BEGIN SELECT RAISE(ABORT, 'Registered contract is immutable'); END""",
     """CREATE TRIGGER contract_links_no_insert BEFORE INSERT ON contract_trainings
        WHEN (SELECT state FROM contracts WHERE id = NEW.contract_id) != 'draft'
        BEGIN SELECT RAISE(ABORT, 'Registered contract links are immutable'); END""",
     """CREATE TRIGGER contract_links_no_delete BEFORE DELETE ON contract_trainings
        WHEN (SELECT state FROM contracts WHERE id = OLD.contract_id) != 'draft'
        BEGIN SELECT RAISE(ABORT, 'Registered contract links are immutable'); END""",
     """CREATE TRIGGER contract_links_no_update BEFORE UPDATE ON contract_trainings
        WHEN (SELECT state FROM contracts WHERE id = OLD.contract_id) != 'draft'
          OR (SELECT state FROM contracts WHERE id = NEW.contract_id) != 'draft'
        BEGIN SELECT RAISE(ABORT, 'Registered contract links are immutable'); END"""),
    ("""CREATE TABLE enrollment_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, order_date TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('individual','group')), group_id INTEGER REFERENCES training_groups(id),
        historical_number TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', template_id INTEGER REFERENCES document_templates(id),
        state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','registered','signed')),
        number TEXT, number_key TEXT UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        registered_at TEXT NOT NULL DEFAULT '', document_path TEXT NOT NULL DEFAULT '', document_sha256 TEXT NOT NULL DEFAULT '',
        snapshot TEXT NOT NULL DEFAULT '{}', signed_date TEXT NOT NULL DEFAULT '', confirmed_at TEXT NOT NULL DEFAULT '',
        confirmed_by TEXT NOT NULL DEFAULT '', grounds_checked INTEGER NOT NULL DEFAULT 0 CHECK (grounds_checked IN (0,1)),
        education_checked INTEGER NOT NULL DEFAULT 0 CHECK (education_checked IN (0,1)), revision INTEGER NOT NULL DEFAULT 1,
        UNIQUE (id,state), CHECK ((number IS NULL) = (number_key IS NULL)),
        CHECK ((state = 'draft' AND number IS NULL AND registered_at = '' AND document_path = '' AND snapshot = '{}') OR
               (state != 'draft' AND length(number) > 0 AND length(number_key) > 0 AND registered_at != ''
                AND template_id IS NOT NULL AND document_path != '' AND length(document_sha256) = 64 AND snapshot != '{}')),
        CHECK ((state != 'signed' AND signed_date = '' AND confirmed_at = '' AND confirmed_by = '' AND grounds_checked = 0 AND education_checked = 0) OR
               (state = 'signed' AND signed_date >= order_date AND confirmed_at != '' AND length(trim(confirmed_by)) > 0 AND grounds_checked = 1 AND education_checked = 1))
    )""",
     """CREATE TABLE enrollment_order_trainings (
        order_id INTEGER NOT NULL REFERENCES enrollment_orders(id), training_id INTEGER NOT NULL REFERENCES trainings(id),
        position INTEGER NOT NULL, PRIMARY KEY (order_id,training_id), UNIQUE (order_id,position)
     )""",
     "CREATE INDEX enrollment_order_trainings_training_idx ON enrollment_order_trainings(training_id)",
     "CREATE TABLE enrollment_order_sequences (year INTEGER PRIMARY KEY, last_value INTEGER NOT NULL CHECK (last_value > 0))",
     """CREATE TABLE training_enrollments (
        training_id INTEGER PRIMARY KEY REFERENCES trainings(id), order_id INTEGER NOT NULL,
        order_state TEXT NOT NULL DEFAULT 'signed' CHECK (order_state = 'signed'),
        FOREIGN KEY (order_id,training_id) REFERENCES enrollment_order_trainings(order_id,training_id),
        FOREIGN KEY (order_id,order_state) REFERENCES enrollment_orders(id,state) DEFERRABLE INITIALLY DEFERRED
     )""",
     """CREATE TRIGGER enrollment_orders_insert BEFORE INSERT ON enrollment_orders WHEN NEW.state != 'draft'
        BEGIN SELECT RAISE(ABORT, 'Create a draft first'); END""",
     """CREATE TRIGGER enrollment_orders_no_delete BEFORE DELETE ON enrollment_orders WHEN OLD.state != 'draft'
        BEGIN SELECT RAISE(ABORT, 'Registered order is immutable'); END""",
     """CREATE TRIGGER enrollment_orders_immutable BEFORE UPDATE ON enrollment_orders WHEN OLD.state != 'draft' AND NOT (
        OLD.state = 'registered' AND NEW.state = 'signed' AND NEW.revision = OLD.revision + 1
        AND NEW.id IS OLD.id AND NEW.order_date IS OLD.order_date AND NEW.kind IS OLD.kind AND NEW.group_id IS OLD.group_id AND NEW.historical_number IS OLD.historical_number AND NEW.note IS OLD.note AND NEW.template_id IS OLD.template_id AND NEW.number IS OLD.number AND NEW.number_key IS OLD.number_key AND NEW.created_at IS OLD.created_at AND NEW.registered_at IS OLD.registered_at AND NEW.document_path IS OLD.document_path AND NEW.document_sha256 IS OLD.document_sha256 AND NEW.snapshot IS OLD.snapshot AND (SELECT count(*) FROM training_enrollments WHERE order_id = OLD.id) > 0
        AND (SELECT count(*) FROM training_enrollments WHERE order_id = OLD.id) =
            (SELECT count(*) FROM enrollment_order_trainings WHERE order_id = OLD.id))
        BEGIN SELECT RAISE(ABORT, 'Registered order is immutable'); END""",
     """CREATE TRIGGER enrollment_draft_no_sign BEFORE UPDATE ON enrollment_orders WHEN OLD.state = 'draft' AND NEW.state = 'signed'
        BEGIN SELECT RAISE(ABORT, 'Register order before signing'); END""",
     """CREATE TRIGGER enrollment_links_no_insert BEFORE INSERT ON enrollment_order_trainings
        WHEN (SELECT state FROM enrollment_orders WHERE id = NEW.order_id) != 'draft'
        BEGIN SELECT RAISE(ABORT, 'Registered order composition is immutable'); END""",
     """CREATE TRIGGER enrollment_links_no_delete BEFORE DELETE ON enrollment_order_trainings
        WHEN (SELECT state FROM enrollment_orders WHERE id = OLD.order_id) != 'draft'
        BEGIN SELECT RAISE(ABORT, 'Registered order composition is immutable'); END""",
     """CREATE TRIGGER enrollment_links_no_update BEFORE UPDATE ON enrollment_order_trainings
        WHEN (SELECT state FROM enrollment_orders WHERE id = OLD.order_id) != 'draft' OR (SELECT state FROM enrollment_orders WHERE id = NEW.order_id) != 'draft'
        BEGIN SELECT RAISE(ABORT, 'Registered order composition is immutable'); END""",
     """CREATE TRIGGER training_enrollments_insert BEFORE INSERT ON training_enrollments
        WHEN (SELECT state FROM enrollment_orders WHERE id = NEW.order_id) != 'registered'
          OR (SELECT state FROM trainings WHERE id = NEW.training_id) != 'preparation'
        BEGIN SELECT RAISE(ABORT, 'Only prepared trainings and registered orders may be confirmed'); END""",
     """CREATE TRIGGER training_enrollments_no_update BEFORE UPDATE ON training_enrollments
        BEGIN SELECT RAISE(ABORT, 'Enrollment is immutable'); END""",
     """CREATE TRIGGER training_enrollments_no_delete BEFORE DELETE ON training_enrollments
        BEGIN SELECT RAISE(ABORT, 'Enrollment is immutable'); END""",
     """CREATE TRIGGER enrolled_trainings_no_update BEFORE UPDATE ON trainings WHEN EXISTS (SELECT 1 FROM training_enrollments WHERE training_id = OLD.id)
        BEGIN SELECT RAISE(ABORT, 'Enrolled training is immutable'); END""",
     """CREATE TRIGGER enrolled_trainings_no_delete BEFORE DELETE ON trainings WHEN EXISTS (SELECT 1 FROM training_enrollments WHERE training_id = OLD.id)
        BEGIN SELECT RAISE(ABORT, 'Enrolled training is immutable'); END""",
     """CREATE TRIGGER enrolled_group_program BEFORE UPDATE OF program_id ON training_groups
        WHEN NEW.program_id != OLD.program_id AND EXISTS (SELECT 1 FROM trainings t JOIN training_enrollments e ON e.training_id = t.id WHERE t.group_id = OLD.id)
        BEGIN SELECT RAISE(ABORT, 'Enrolled group program is immutable'); END"""),
    ("""CREATE TABLE attestation_protocols (
        id INTEGER PRIMARY KEY AUTOINCREMENT, protocol_date TEXT NOT NULL, attestation_date TEXT NOT NULL,
        direction TEXT NOT NULL CHECK (direction IN ('dpo','po')), kind TEXT NOT NULL,
        group_id INTEGER REFERENCES training_groups(id), historical_number TEXT NOT NULL DEFAULT '',
        chair TEXT NOT NULL DEFAULT '', members TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
        template_id INTEGER REFERENCES document_templates(id),
        state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','registered','signed')),
        number TEXT, number_key TEXT UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        registered_at TEXT NOT NULL DEFAULT '', document_path TEXT NOT NULL DEFAULT '', document_sha256 TEXT NOT NULL DEFAULT '',
        snapshot TEXT NOT NULL DEFAULT '{}', signed_date TEXT NOT NULL DEFAULT '', confirmed_at TEXT NOT NULL DEFAULT '',
        confirmed_by TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
        CHECK ((direction = 'dpo' AND kind = 'Итоговая аттестация') OR (direction = 'po' AND kind = 'Квалификационный экзамен')),
        CHECK ((number IS NULL) = (number_key IS NULL)),
        CHECK ((state = 'draft' AND number IS NULL AND registered_at = '' AND document_path = '' AND snapshot = '{}') OR
               (state != 'draft' AND length(trim(number)) > 0 AND length(number_key) > 0 AND registered_at != ''
                AND template_id IS NOT NULL AND document_path != '' AND length(document_sha256) = 64 AND snapshot != '{}')),
        CHECK ((state != 'signed' AND signed_date = '' AND confirmed_at = '' AND confirmed_by = '') OR
               (state = 'signed' AND signed_date >= protocol_date AND signed_date >= attestation_date
                AND confirmed_at != '' AND length(trim(confirmed_by)) > 0))
    )""",
     """CREATE TABLE attestation_results (
        protocol_id INTEGER NOT NULL REFERENCES attestation_protocols(id),
        training_id INTEGER NOT NULL REFERENCES training_enrollments(training_id),
        position INTEGER NOT NULL CHECK (position >= 0),
        result TEXT NOT NULL DEFAULT '' CHECK (result IN ('','passed','failed','absent')),
        grade TEXT NOT NULL DEFAULT '', qualification TEXT NOT NULL DEFAULT '',
        qualification_grade TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (protocol_id,training_id), UNIQUE (protocol_id,position)
     )""",
     "CREATE INDEX attestation_results_training_idx ON attestation_results(training_id)",
     "CREATE TABLE attestation_sequences (year INTEGER PRIMARY KEY, last_value INTEGER NOT NULL CHECK (last_value > 0))",
     """CREATE TRIGGER attestation_sequences_insert BEFORE INSERT ON attestation_sequences
        WHEN EXISTS (SELECT 1 FROM attestation_sequences WHERE year = NEW.year AND last_value > NEW.last_value)
        BEGIN SELECT RAISE(ABORT, 'Protocol sequence cannot decrease'); END""",
     """CREATE TRIGGER attestation_sequences_no_delete BEFORE DELETE ON attestation_sequences
        BEGIN SELECT RAISE(ABORT, 'Protocol sequence cannot be deleted'); END""",
     """CREATE TRIGGER attestation_sequences_no_decrease BEFORE UPDATE ON attestation_sequences
        WHEN NEW.year != OLD.year OR NEW.last_value < OLD.last_value
        BEGIN SELECT RAISE(ABORT, 'Protocol sequence cannot decrease'); END""",
     """CREATE TRIGGER attestation_insert BEFORE INSERT ON attestation_protocols
        WHEN NEW.state != 'draft' OR EXISTS (SELECT 1 FROM attestation_protocols WHERE id = NEW.id AND state != 'draft')
        BEGIN SELECT RAISE(ABORT, 'Create a draft first'); END""",
     """CREATE TRIGGER attestation_no_delete BEFORE DELETE ON attestation_protocols WHEN OLD.state != 'draft'
        BEGIN SELECT RAISE(ABORT, 'Registered protocol is immutable'); END""",
     """CREATE TRIGGER attestation_register BEFORE UPDATE ON attestation_protocols
        WHEN OLD.state = 'draft' AND NEW.state != 'draft' AND
        (NEW.state != 'registered' OR NOT EXISTS (SELECT 1 FROM attestation_results WHERE protocol_id = OLD.id))
        BEGIN SELECT RAISE(ABORT, 'Register a populated draft before signing'); END""",
     """CREATE TRIGGER attestation_immutable BEFORE UPDATE ON attestation_protocols WHEN OLD.state != 'draft' AND NOT (
        OLD.state = 'registered' AND NEW.state = 'signed' AND NEW.revision = OLD.revision + 1
        AND NEW.id IS OLD.id AND NEW.protocol_date IS OLD.protocol_date AND NEW.attestation_date IS OLD.attestation_date
        AND NEW.direction IS OLD.direction AND NEW.kind IS OLD.kind AND NEW.group_id IS OLD.group_id
        AND NEW.historical_number IS OLD.historical_number AND NEW.chair IS OLD.chair AND NEW.members IS OLD.members
        AND NEW.note IS OLD.note AND NEW.template_id IS OLD.template_id AND NEW.number IS OLD.number
        AND NEW.number_key IS OLD.number_key AND NEW.created_at IS OLD.created_at AND NEW.registered_at IS OLD.registered_at
        AND NEW.document_path IS OLD.document_path AND NEW.document_sha256 IS OLD.document_sha256 AND NEW.snapshot IS OLD.snapshot
        AND EXISTS (SELECT 1 FROM attestation_results WHERE protocol_id = OLD.id)
        AND NOT EXISTS (SELECT 1 FROM attestation_results WHERE protocol_id = OLD.id AND result = ''))
        BEGIN SELECT RAISE(ABORT, 'Registered protocol and results are immutable; signing requires complete results'); END""",
     """CREATE TRIGGER attestation_results_insert BEFORE INSERT ON attestation_results
        WHEN (SELECT state FROM attestation_protocols WHERE id = NEW.protocol_id) != 'draft'
        BEGIN SELECT RAISE(ABORT, 'Registered results are immutable'); END""",
     """CREATE TRIGGER attestation_results_delete BEFORE DELETE ON attestation_results
        WHEN (SELECT state FROM attestation_protocols WHERE id = OLD.protocol_id) != 'draft'
        BEGIN SELECT RAISE(ABORT, 'Registered results are immutable'); END""",
     """CREATE TRIGGER attestation_results_update BEFORE UPDATE ON attestation_results
        WHEN (SELECT state FROM attestation_protocols WHERE id = OLD.protocol_id) != 'draft'
          OR (SELECT state FROM attestation_protocols WHERE id = NEW.protocol_id) != 'draft'
        BEGIN SELECT RAISE(ABORT, 'Registered results are immutable'); END"""),
    ("""CREATE TABLE completion_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, order_date TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('individual','group')), group_id INTEGER REFERENCES training_groups(id),
    direction TEXT NOT NULL CHECK (direction IN ('dpo','po')),
    historical_number TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', template_id INTEGER REFERENCES document_templates(id),
    state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','registered','signed')),
    number TEXT, number_key TEXT UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    registered_at TEXT NOT NULL DEFAULT '', document_path TEXT NOT NULL DEFAULT '', document_sha256 TEXT NOT NULL DEFAULT '',
    snapshot TEXT NOT NULL DEFAULT '{}', signed_date TEXT NOT NULL DEFAULT '', confirmed_at TEXT NOT NULL DEFAULT '',
    confirmed_by TEXT NOT NULL DEFAULT '', conditions_checked INTEGER NOT NULL DEFAULT 0 CHECK (conditions_checked IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1, UNIQUE (id,state),
    CHECK ((kind = 'individual' AND group_id IS NULL) OR (kind = 'group' AND group_id IS NOT NULL)),
    CHECK ((number IS NULL) = (number_key IS NULL)),
    CHECK ((state = 'draft' AND number IS NULL AND registered_at = '' AND document_path = '' AND snapshot = '{}') OR
           (state != 'draft' AND length(trim(number)) > 0 AND length(number_key) > 0 AND registered_at != ''
            AND template_id IS NOT NULL AND document_path != '' AND length(document_sha256) = 64 AND snapshot != '{}')),
    CHECK ((state != 'signed' AND signed_date = '' AND confirmed_at = '' AND confirmed_by = '' AND conditions_checked = 0) OR
           (state = 'signed' AND signed_date >= order_date AND confirmed_at != '' AND length(trim(confirmed_by)) > 0 AND conditions_checked = 1))
)""",
     """CREATE TABLE completion_order_trainings (
    order_id INTEGER NOT NULL REFERENCES completion_orders(id), training_id INTEGER NOT NULL REFERENCES training_enrollments(training_id),
    position INTEGER NOT NULL CHECK (position >= 0), completion_date TEXT NOT NULL,
    protocol_id INTEGER NOT NULL, basis_snapshot TEXT NOT NULL CHECK (basis_snapshot != '{}'),
    PRIMARY KEY (order_id,training_id), UNIQUE (order_id,position),
    UNIQUE (order_id,training_id,protocol_id,completion_date),
    FOREIGN KEY (protocol_id,training_id) REFERENCES attestation_results(protocol_id,training_id)
)""",
     """CREATE INDEX completion_order_trainings_training_idx ON completion_order_trainings(training_id)""",
     """CREATE TABLE completion_sequences (year INTEGER PRIMARY KEY, last_value INTEGER NOT NULL CHECK (last_value > 0))""",
     """CREATE TABLE training_completions (
    training_id INTEGER PRIMARY KEY REFERENCES training_enrollments(training_id), order_id INTEGER NOT NULL,
    protocol_id INTEGER NOT NULL, completion_date TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    order_state TEXT NOT NULL DEFAULT 'signed' CHECK (order_state = 'signed'),
    FOREIGN KEY (order_id,training_id,protocol_id,completion_date)
        REFERENCES completion_order_trainings(order_id,training_id,protocol_id,completion_date),
    FOREIGN KEY (order_id,order_state) REFERENCES completion_orders(id,state) DEFERRABLE INITIALLY DEFERRED
)""",
     """CREATE TRIGGER completion_insert BEFORE INSERT ON completion_orders
    WHEN NEW.state != 'draft' OR EXISTS (SELECT 1 FROM completion_orders WHERE id = NEW.id AND state != 'draft')
    BEGIN SELECT RAISE(ABORT, 'Create a draft first'); END""",
     """CREATE TRIGGER completion_no_delete BEFORE DELETE ON completion_orders WHEN OLD.state != 'draft'
    BEGIN SELECT RAISE(ABORT, 'Registered completion order is immutable'); END""",
     """CREATE TRIGGER completion_register BEFORE UPDATE ON completion_orders
    WHEN OLD.state = 'draft' AND NEW.state != 'draft' AND
    (NEW.state != 'registered' OR NOT EXISTS (SELECT 1 FROM completion_order_trainings WHERE order_id = OLD.id))
    BEGIN SELECT RAISE(ABORT, 'Register a populated draft before signing'); END""",
     """CREATE TRIGGER completion_immutable BEFORE UPDATE ON completion_orders WHEN OLD.state != 'draft' AND NOT (
    OLD.state = 'registered' AND NEW.state = 'signed' AND NEW.revision = OLD.revision + 1
    AND NEW.id IS OLD.id AND NEW.order_date IS OLD.order_date AND NEW.kind IS OLD.kind AND NEW.group_id IS OLD.group_id
    AND NEW.direction IS OLD.direction AND NEW.historical_number IS OLD.historical_number AND NEW.note IS OLD.note
    AND NEW.template_id IS OLD.template_id AND NEW.number IS OLD.number AND NEW.number_key IS OLD.number_key
    AND NEW.created_at IS OLD.created_at AND NEW.registered_at IS OLD.registered_at AND NEW.document_path IS OLD.document_path
    AND NEW.document_sha256 IS OLD.document_sha256 AND NEW.snapshot IS OLD.snapshot
    AND (SELECT count(*) FROM training_completions WHERE order_id = OLD.id) > 0
    AND (SELECT count(*) FROM training_completions WHERE order_id = OLD.id) =
        (SELECT count(*) FROM completion_order_trainings WHERE order_id = OLD.id)
    AND NOT EXISTS (SELECT 1 FROM completion_order_trainings WHERE order_id = OLD.id AND completion_date > NEW.signed_date))
    BEGIN SELECT RAISE(ABORT, 'Registered completion order is immutable; all completion facts required'); END""",
     """CREATE TRIGGER completion_links_insert BEFORE INSERT ON completion_order_trainings WHEN (SELECT state FROM completion_orders WHERE id = NEW.order_id) != 'draft' BEGIN SELECT RAISE(ABORT, 'Registered completion links are immutable'); END""",
     """CREATE TRIGGER completion_links_delete BEFORE DELETE ON completion_order_trainings WHEN (SELECT state FROM completion_orders WHERE id = OLD.order_id) != 'draft' BEGIN SELECT RAISE(ABORT, 'Registered completion links are immutable'); END""",
     """CREATE TRIGGER completion_links_update BEFORE UPDATE ON completion_order_trainings WHEN (SELECT state FROM completion_orders WHERE id = OLD.order_id) != 'draft' OR (SELECT state FROM completion_orders WHERE id = NEW.order_id) != 'draft' BEGIN SELECT RAISE(ABORT, 'Registered completion links are immutable'); END""",
     """CREATE TRIGGER completion_facts_insert BEFORE INSERT ON training_completions WHEN
    EXISTS (SELECT 1 FROM training_completions WHERE training_id = NEW.training_id)
    OR (SELECT state FROM completion_orders WHERE id = NEW.order_id) != 'registered'
    OR NOT EXISTS (SELECT 1 FROM trainings t JOIN attestation_results r ON r.training_id = t.id
        JOIN attestation_protocols p ON p.id = r.protocol_id
        WHERE t.id = NEW.training_id AND t.state != 'excluded' AND p.id = NEW.protocol_id
          AND p.state = 'signed' AND r.result = 'passed' AND NEW.completion_date >= t.start_date
          AND NEW.completion_date >= p.attestation_date
          AND p.id = (SELECT p2.id FROM attestation_protocols p2 JOIN attestation_results r2 ON r2.protocol_id = p2.id
              WHERE r2.training_id = NEW.training_id AND p2.state = 'signed'
              ORDER BY p2.attestation_date DESC,p2.id DESC LIMIT 1))
    BEGIN SELECT RAISE(ABORT, 'Completion requires a current signed successful attestation and valid dates'); END""",
     """CREATE TRIGGER completion_facts_no_update BEFORE UPDATE ON training_completions
    BEGIN SELECT RAISE(ABORT, 'Completion fact is immutable'); END""",
     """CREATE TRIGGER completion_facts_no_delete BEFORE DELETE ON training_completions
    BEGIN SELECT RAISE(ABORT, 'Completion fact is immutable'); END""",
     """CREATE TRIGGER completion_sequences_no_delete BEFORE DELETE ON completion_sequences
    BEGIN SELECT RAISE(ABORT, 'Completion sequence cannot be deleted'); END""",
     """CREATE TRIGGER completion_sequences_no_decrease BEFORE UPDATE ON completion_sequences
    WHEN NEW.year != OLD.year OR NEW.last_value < OLD.last_value
    BEGIN SELECT RAISE(ABORT, 'Completion sequence cannot decrease'); END""",
     """CREATE TRIGGER completion_sequences_insert BEFORE INSERT ON completion_sequences
    WHEN EXISTS (SELECT 1 FROM completion_sequences WHERE year = NEW.year AND last_value > NEW.last_value)
    BEGIN SELECT RAISE(ABORT, 'Completion sequence cannot decrease'); END"""),
    ("""CREATE TABLE issued_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, training_id INTEGER NOT NULL REFERENCES training_completions(training_id),
    kind TEXT NOT NULL CHECK (kind IN ('dpo_upskilling_certificate','dpo_retraining_diploma','po_profession_certificate')),
    full_name TEXT NOT NULL, program_name TEXT NOT NULL, hours INTEGER NOT NULL CHECK (hours > 0),
    direction TEXT NOT NULL, program_kind TEXT NOT NULL, qualification TEXT NOT NULL DEFAULT '', qualification_grade TEXT NOT NULL DEFAULT '',
    completion_date TEXT NOT NULL, completion_order_id INTEGER NOT NULL REFERENCES completion_orders(id),
    completion_order_number TEXT NOT NULL, completion_order_date TEXT NOT NULL,
    document_date TEXT NOT NULL, historical_number TEXT NOT NULL DEFAULT '', blank_series TEXT NOT NULL DEFAULT '',
    blank_number TEXT NOT NULL DEFAULT '', blank_key TEXT, template_id INTEGER REFERENCES document_templates(id),
    state TEXT NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','registered')), number TEXT, number_key TEXT,
    document_path TEXT NOT NULL DEFAULT '', document_sha256 TEXT NOT NULL DEFAULT '', snapshot TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, registered_at TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
    CHECK (document_date >= completion_date), CHECK ((number IS NULL) = (number_key IS NULL)),
    CHECK ((direction='dpo' AND program_kind='dpo_upskilling' AND kind='dpo_upskilling_certificate') OR
           (direction='dpo' AND program_kind='dpo_retraining' AND kind='dpo_retraining_diploma') OR
           (direction='po' AND program_kind IN ('po_initial_training','po_retraining','po_upskilling') AND kind='po_profession_certificate')),
    CHECK ((state='draft' AND number IS NULL AND blank_key IS NULL AND document_path='' AND registered_at='' AND snapshot='{}') OR
           (state='registered' AND length(trim(number))>0 AND length(number_key)>0 AND template_id IS NOT NULL AND document_path!=''
            AND length(document_sha256)=64 AND registered_at!='' AND snapshot!='{}')),
    CHECK ((blank_number='' AND blank_key IS NULL) OR (blank_number!='' AND (state='draft' OR blank_key IS NOT NULL)))
)""",
     """CREATE UNIQUE INDEX issued_primary_training ON issued_documents(training_id) WHERE state='registered'""",
     """CREATE UNIQUE INDEX issued_number_scope ON issued_documents(kind,number_key) WHERE state='registered'""",
     """CREATE UNIQUE INDEX issued_blank_scope ON issued_documents(kind,blank_key) WHERE state='registered' AND blank_key IS NOT NULL""",
     """CREATE TABLE issued_sequences (kind TEXT NOT NULL, year INTEGER NOT NULL, last_value INTEGER NOT NULL CHECK(last_value>0), PRIMARY KEY(kind,year))""",
     """CREATE TABLE document_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL REFERENCES issued_documents(id),
    method TEXT NOT NULL CHECK(method IN ('personal','post','post_confirmation','email')), event_date TEXT NOT NULL,
    recipient TEXT NOT NULL CHECK(length(trim(recipient))>0), confirmation TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', tracking_number TEXT NOT NULL DEFAULT '',
    postal_status TEXT NOT NULL, received_date TEXT NOT NULL DEFAULT '', parent_event_id INTEGER,
    medium TEXT NOT NULL CHECK(medium IN ('original','electronic_copy')), note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(id,document_id),
    FOREIGN KEY(parent_event_id,document_id) REFERENCES document_transfers(id,document_id),
    CHECK ((method='email' AND medium='electronic_copy' AND email!='' AND parent_event_id IS NULL AND received_date='' AND postal_status IN ('sent','confirmed')) OR
           (method='personal' AND medium='original' AND confirmation!='' AND parent_event_id IS NULL AND received_date='' AND postal_status='confirmed') OR
           (method='post' AND medium='original' AND address!='' AND parent_event_id IS NULL AND received_date='' AND postal_status IN ('sent','unconfirmed')) OR
           (method='post_confirmation' AND medium='original' AND parent_event_id IS NOT NULL AND received_date!='' AND received_date<=event_date AND confirmation!='' AND postal_status='confirmed'))
)""",
     """CREATE UNIQUE INDEX postal_confirmation_once ON document_transfers(parent_event_id) WHERE method='post_confirmation'""",
     """CREATE TABLE transfer_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL REFERENCES document_transfers(id),
    relative_path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL CHECK(length(sha256)=64),
    evidence_type TEXT NOT NULL CHECK(length(trim(evidence_type))>0), format TEXT NOT NULL CHECK(format IN ('pdf','png','jpg')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)""",
     """CREATE TRIGGER issued_insert BEFORE INSERT ON issued_documents WHEN NEW.state!='draft'
    OR EXISTS (SELECT 1 FROM issued_documents WHERE id=NEW.id AND state='registered')
    BEGIN SELECT RAISE(ABORT,'Create a draft first; registered documents are immutable'); END""",
     """CREATE TRIGGER issued_immutable BEFORE UPDATE ON issued_documents WHEN OLD.state='registered'
    BEGIN SELECT RAISE(ABORT,'Registered document is immutable'); END""",
     """CREATE TRIGGER issued_update_conflicts BEFORE UPDATE ON issued_documents WHEN EXISTS
    (SELECT 1 FROM issued_documents r WHERE r.state='registered' AND r.id!=OLD.id AND
        (r.id=NEW.id OR (NEW.state='registered' AND (r.training_id=NEW.training_id OR
            (r.kind=NEW.kind AND (r.number_key=NEW.number_key OR (NEW.blank_key IS NOT NULL AND r.blank_key=NEW.blank_key)))))))
    BEGIN SELECT RAISE(ABORT,'Registered documents cannot be replaced through another draft'); END""",
     """CREATE TRIGGER issued_no_delete BEFORE DELETE ON issued_documents WHEN OLD.state='registered'
    BEGIN SELECT RAISE(ABORT,'Registered document cannot be deleted'); END""",
     """CREATE TRIGGER issued_basis BEFORE INSERT ON issued_documents WHEN NOT EXISTS
    (SELECT 1 FROM training_completions c JOIN completion_orders o ON o.id=c.order_id WHERE c.training_id=NEW.training_id
        AND o.id=NEW.completion_order_id AND o.state='signed' AND c.completion_date=NEW.completion_date)
    BEGIN SELECT RAISE(ABORT,'Confirmed completion required'); END""",
     """CREATE TRIGGER issued_basis_update BEFORE UPDATE ON issued_documents WHEN NOT EXISTS
    (SELECT 1 FROM training_completions c JOIN completion_orders o ON o.id=c.order_id WHERE c.training_id=NEW.training_id
        AND o.id=NEW.completion_order_id AND o.state='signed' AND c.completion_date=NEW.completion_date)
    BEGIN SELECT RAISE(ABORT,'Confirmed completion required'); END""",
     """CREATE TRIGGER transfer_insert BEFORE INSERT ON document_transfers WHEN
    (SELECT state FROM issued_documents WHERE id=NEW.document_id)!='registered'
    OR NEW.event_date < (SELECT document_date FROM issued_documents WHERE id=NEW.document_id)
    OR EXISTS(SELECT 1 FROM document_transfers WHERE id=NEW.id)
    OR (NEW.method='post_confirmation' AND EXISTS(SELECT 1 FROM document_transfers WHERE method='post_confirmation' AND parent_event_id=NEW.parent_event_id))
    OR (NEW.method='post_confirmation' AND NOT EXISTS(SELECT 1 FROM document_transfers p WHERE p.id=NEW.parent_event_id
        AND p.document_id=NEW.document_id AND p.method='post' AND NEW.received_date>=p.event_date))
    BEGIN SELECT RAISE(ABORT,'Registered document and valid original postal event required'); END""",
     """CREATE TRIGGER evidence_insert BEFORE INSERT ON transfer_evidence WHEN EXISTS(SELECT 1 FROM transfer_evidence WHERE id=NEW.id OR relative_path=NEW.relative_path)
    BEGIN SELECT RAISE(ABORT,'Evidence cannot be replaced'); END""",
     """CREATE TRIGGER transfers_no_update BEFORE UPDATE ON document_transfers BEGIN SELECT RAISE(ABORT,'History is append only'); END""",
     """CREATE TRIGGER transfers_no_delete BEFORE DELETE ON document_transfers BEGIN SELECT RAISE(ABORT,'History is append only'); END""",
     """CREATE TRIGGER evidence_no_update BEFORE UPDATE ON transfer_evidence BEGIN SELECT RAISE(ABORT,'History is append only'); END""",
     """CREATE TRIGGER evidence_no_delete BEFORE DELETE ON transfer_evidence BEGIN SELECT RAISE(ABORT,'History is append only'); END""",
     """CREATE TRIGGER issued_sequences_no_delete BEFORE DELETE ON issued_sequences
    BEGIN SELECT RAISE(ABORT,'Sequence cannot be deleted'); END""",
     """CREATE TRIGGER issued_sequences_no_decrease BEFORE UPDATE ON issued_sequences WHEN
    NEW.kind!=OLD.kind OR NEW.year!=OLD.year OR NEW.last_value<OLD.last_value
    BEGIN SELECT RAISE(ABORT,'Sequence cannot decrease'); END""",
     """CREATE TRIGGER issued_sequences_insert BEFORE INSERT ON issued_sequences WHEN
    EXISTS(SELECT 1 FROM issued_sequences WHERE kind=NEW.kind AND year=NEW.year AND last_value>NEW.last_value)
    BEGIN SELECT RAISE(ABORT,'Sequence cannot decrease'); END"""),
    ("""CREATE TABLE frdo_templates (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('dpo','po')),
        version INTEGER NOT NULL CHECK(version>0), original_name TEXT NOT NULL,
        relative_path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL CHECK(length(sha256)=64),
        added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), UNIQUE(kind,version))""",
     "CREATE UNIQUE INDEX frdo_templates_active ON frdo_templates(kind) WHERE active=1"),
    ("ALTER TABLE students ADD COLUMN sex TEXT NOT NULL DEFAULT ''",
     "ALTER TABLE students ADD COLUMN citizenship_code TEXT NOT NULL DEFAULT ''",
     "ALTER TABLE programs ADD COLUMN dpo_area TEXT NOT NULL DEFAULT ''",
     "ALTER TABLE programs ADD COLUMN dpo_group TEXT NOT NULL DEFAULT ''",
     "ALTER TABLE programs ADD COLUMN po_profession TEXT NOT NULL DEFAULT ''",
     """CREATE TABLE frdo_training_details (
        training_id INTEGER PRIMARY KEY REFERENCES trainings(id),
        form TEXT NOT NULL DEFAULT '', funding TEXT NOT NULL DEFAULT '', relations TEXT NOT NULL DEFAULT '',
        education_level TEXT NOT NULL DEFAULT '', education_surname TEXT NOT NULL DEFAULT '',
        education_series TEXT NOT NULL DEFAULT '', education_number TEXT NOT NULL DEFAULT '',
        dpo_area TEXT NOT NULL DEFAULT '', dpo_group TEXT NOT NULL DEFAULT '',
        po_profession TEXT NOT NULL DEFAULT '', po_grade TEXT NOT NULL DEFAULT '', qualification TEXT NOT NULL DEFAULT '',
        patronymic TEXT NOT NULL DEFAULT '')""",
     """CREATE TABLE frdo_change_history (
        id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL REFERENCES issued_documents(id),
        changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        changes TEXT NOT NULL)""",
     """CREATE TRIGGER frdo_history_no_update BEFORE UPDATE ON frdo_change_history
        BEGIN SELECT RAISE(ABORT,'FRDO history is immutable'); END""",
     """CREATE TRIGGER frdo_history_no_delete BEFORE DELETE ON frdo_change_history
        BEGIN SELECT RAISE(ABORT,'FRDO history is immutable'); END""",
     """CREATE TRIGGER frdo_history_no_replace BEFORE INSERT ON frdo_change_history WHEN EXISTS
        (SELECT 1 FROM frdo_change_history WHERE id=NEW.id)
        BEGIN SELECT RAISE(ABORT,'FRDO history is immutable'); END"""),

    ("""CREATE TABLE frdo_packages (
        id TEXT PRIMARY KEY NOT NULL, request_key TEXT NOT NULL, part_index INTEGER NOT NULL CHECK(part_index>=0),
        kind TEXT NOT NULL CHECK(kind IN ('dpo','po')), ogrn TEXT NOT NULL, kpp TEXT NOT NULL,
        issue_year INTEGER NOT NULL, number INTEGER NOT NULL CHECK(number>0),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        template_id INTEGER NOT NULL REFERENCES frdo_templates(id), template_version INTEGER NOT NULL,
        template_sha256 TEXT NOT NULL, relative_path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
        row_count INTEGER NOT NULL CHECK(row_count>0 AND row_count<=CASE kind WHEN 'dpo' THEN 1000 ELSE 1001 END), UNIQUE(ogrn,kpp,issue_year,number), UNIQUE(request_key,part_index))""",
     """CREATE TABLE frdo_package_documents (
        package_id TEXT NOT NULL REFERENCES frdo_packages(id), position INTEGER NOT NULL CHECK(position>=0),
        document_id INTEGER NOT NULL REFERENCES issued_documents(id), source_signature TEXT NOT NULL,
        PRIMARY KEY(package_id,position), UNIQUE(package_id,document_id))""",
     "CREATE INDEX frdo_package_document_lookup ON frdo_package_documents(document_id)",
     """CREATE TRIGGER frdo_packages_no_update BEFORE UPDATE ON frdo_packages
        BEGIN SELECT RAISE(ABORT,'FRDO package is immutable'); END""",
     """CREATE TRIGGER frdo_packages_no_delete BEFORE DELETE ON frdo_packages
        BEGIN SELECT RAISE(ABORT,'FRDO package is immutable'); END""",
     """CREATE TRIGGER frdo_packages_no_replace BEFORE INSERT ON frdo_packages WHEN EXISTS
        (SELECT 1 FROM frdo_packages WHERE id=NEW.id OR relative_path=NEW.relative_path
         OR (ogrn=NEW.ogrn AND kpp=NEW.kpp AND issue_year=NEW.issue_year AND number=NEW.number)
         OR (request_key=NEW.request_key AND part_index=NEW.part_index))
        BEGIN SELECT RAISE(ABORT,'FRDO package is immutable'); END""",
     """CREATE TRIGGER frdo_package_documents_no_append BEFORE INSERT ON frdo_package_documents WHEN
        NEW.position >= (SELECT row_count FROM frdo_packages WHERE id=NEW.package_id)
        BEGIN SELECT RAISE(ABORT,'FRDO package is full'); END""",
     """CREATE TRIGGER frdo_package_documents_no_update BEFORE UPDATE ON frdo_package_documents
        BEGIN SELECT RAISE(ABORT,'FRDO package contents are immutable'); END""",
     """CREATE TRIGGER frdo_package_documents_no_delete BEFORE DELETE ON frdo_package_documents
        BEGIN SELECT RAISE(ABORT,'FRDO package contents are immutable'); END""",
     """CREATE TRIGGER frdo_package_documents_no_replace BEFORE INSERT ON frdo_package_documents WHEN EXISTS
        (SELECT 1 FROM frdo_package_documents WHERE package_id=NEW.package_id
         AND (position=NEW.position OR document_id=NEW.document_id))
        BEGIN SELECT RAISE(ABORT,'FRDO package contents are immutable'); END"""),

    (
        """CREATE TABLE admission_rules (
        rule_key TEXT NOT NULL, title TEXT NOT NULL, program_kind TEXT NOT NULL DEFAULT '', program_id INTEGER REFERENCES programs(id),
        condition_tag TEXT NOT NULL DEFAULT '', source TEXT NOT NULL CHECK(source IN ('law','local','special')), reference TEXT NOT NULL,
        required INTEGER NOT NULL CHECK(required IN (0,1)), document_type TEXT NOT NULL CHECK(document_type IN ('application','consent','evidence','condition')),
        sign_required INTEGER NOT NULL CHECK(sign_required IN (0,1)), reusable INTEGER NOT NULL CHECK(reusable IN (0,1)), scope TEXT NOT NULL DEFAULT '',
        template_variant TEXT NOT NULL DEFAULT 'admission', waiver_allowed INTEGER NOT NULL CHECK(waiver_allowed IN (0,1)), active INTEGER NOT NULL CHECK(active IN (0,1)),
        version INTEGER NOT NULL CHECK(version>0), id INTEGER PRIMARY KEY, UNIQUE(rule_key,version))""",
        """CREATE TABLE admission_rule_history (id INTEGER PRIMARY KEY,rule_key TEXT NOT NULL,version INTEGER NOT NULL,
        responsible TEXT NOT NULL,day TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        FOREIGN KEY(rule_key,version) REFERENCES admission_rules(rule_key,version))""",
        """CREATE TABLE admission_context_events (id INTEGER PRIMARY KEY,training_id INTEGER NOT NULL REFERENCES trainings(id),
        pd_basis TEXT NOT NULL CHECK(pd_basis IN ('unknown','contract','law','consent')),pd_reference TEXT NOT NULL,pd_scope TEXT NOT NULL,
        conditions TEXT NOT NULL,note TEXT NOT NULL,responsible TEXT NOT NULL,day TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
        """CREATE TABLE admission_documents (id INTEGER PRIMARY KEY,training_id INTEGER NOT NULL REFERENCES trainings(id),
        student_id INTEGER NOT NULL REFERENCES students(id),rule_key TEXT NOT NULL,title TEXT NOT NULL,document_type TEXT NOT NULL,scope TEXT NOT NULL,
        document_date TEXT NOT NULL,valid_until TEXT NOT NULL,reusable INTEGER NOT NULL CHECK(reusable IN (0,1)),file_path TEXT NOT NULL,sha256 TEXT NOT NULL,
        template_id INTEGER REFERENCES document_templates(id),template_sha256 TEXT NOT NULL DEFAULT '',context_snapshot TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
        """CREATE TABLE admission_document_events (id INTEGER PRIMARY KEY,document_id INTEGER NOT NULL REFERENCES admission_documents(id),
        state TEXT NOT NULL CHECK(state IN ('prepared','received','verified','revoked')),signed INTEGER NOT NULL CHECK(signed IN (0,1)),
        day TEXT NOT NULL,responsible TEXT NOT NULL,note TEXT NOT NULL,education_basis TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
        """CREATE TABLE admission_checks (id INTEGER PRIMARY KEY,training_id INTEGER NOT NULL REFERENCES trainings(id),rule_key TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('required','not_required','prepared','received','verified','absent')),
        document_id INTEGER REFERENCES admission_documents(id),reason TEXT NOT NULL,day TEXT NOT NULL,responsible TEXT NOT NULL,
        rule_signature TEXT NOT NULL,rule_snapshot TEXT NOT NULL,training_signature TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
        """CREATE TABLE admission_enrollment_checks (id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL UNIQUE REFERENCES enrollment_orders(id),
        assessment TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""",
        """CREATE INDEX admission_context_latest ON admission_context_events(training_id,id)""",
        """CREATE INDEX admission_check_latest ON admission_checks(training_id,rule_key,id)""",
        """CREATE INDEX admission_document_latest ON admission_document_events(document_id,id)""",
        """CREATE TRIGGER admission_rules_no_update BEFORE UPDATE ON admission_rules BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_rules_no_delete BEFORE DELETE ON admission_rules BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_rules_no_replace BEFORE INSERT ON admission_rules WHEN EXISTS (SELECT 1 FROM admission_rules WHERE id=NEW.id OR (rule_key=NEW.rule_key AND version=NEW.version)) BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_rule_history_no_update BEFORE UPDATE ON admission_rule_history BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_rule_history_no_delete BEFORE DELETE ON admission_rule_history BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_rule_history_no_replace BEFORE INSERT ON admission_rule_history WHEN EXISTS (SELECT 1 FROM admission_rule_history WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_context_events_no_update BEFORE UPDATE ON admission_context_events BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_context_events_no_delete BEFORE DELETE ON admission_context_events BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_context_events_no_replace BEFORE INSERT ON admission_context_events WHEN EXISTS (SELECT 1 FROM admission_context_events WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_documents_no_update BEFORE UPDATE ON admission_documents BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_documents_no_delete BEFORE DELETE ON admission_documents BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_documents_no_replace BEFORE INSERT ON admission_documents WHEN EXISTS (SELECT 1 FROM admission_documents WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_document_events_no_update BEFORE UPDATE ON admission_document_events BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_document_events_no_delete BEFORE DELETE ON admission_document_events BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_document_events_no_replace BEFORE INSERT ON admission_document_events WHEN EXISTS (SELECT 1 FROM admission_document_events WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_checks_no_update BEFORE UPDATE ON admission_checks BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_checks_no_delete BEFORE DELETE ON admission_checks BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_checks_no_replace BEFORE INSERT ON admission_checks WHEN EXISTS (SELECT 1 FROM admission_checks WHERE id=NEW.id) BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_enrollment_checks_no_update BEFORE UPDATE ON admission_enrollment_checks BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_enrollment_checks_no_delete BEFORE DELETE ON admission_enrollment_checks BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """CREATE TRIGGER admission_enrollment_checks_no_replace BEFORE INSERT ON admission_enrollment_checks WHEN EXISTS (SELECT 1 FROM admission_enrollment_checks WHERE id=NEW.id OR order_id=NEW.order_id) BEGIN SELECT RAISE(ABORT,'Admission history is immutable'); END""",
        """INSERT INTO admission_rules VALUES ('application','Заявление о приёме','',NULL,'','local','Рабочий локальный перечень организации: уточните по утверждённому акту',1,'application',1,0,'admission_application','admission',1,1,1,NULL)""",
        """INSERT INTO admission_rules VALUES ('identity','Проверка документа, удостоверяющего личность','',NULL,'','local','Рабочий локальный перечень: копия паспорта не объявляется федеральной обязанностью',0,'evidence',0,1,'admission_identity','admission',1,1,1,NULL)""",
        """INSERT INTO admission_rules VALUES ('dpo_education','Документ о СПО/ВО или справка об обучении в СПО/ВО','dpo',NULL,'','law','273-ФЗ, часть 3 статьи 76',1,'evidence',0,1,'admission_education','admission',0,1,1,NULL)""",
        """INSERT INTO admission_rules VALUES ('po_profession_retraining','Подтверждение имеющейся профессии рабочего / должности служащего','po_retraining',NULL,'','law','273-ФЗ, часть 3 статьи 73',1,'evidence',0,1,'prior_profession','admission',0,1,1,NULL)""",
        """INSERT INTO admission_rules VALUES ('po_profession_upskilling','Подтверждение имеющейся профессии рабочего / должности служащего','po_upskilling',NULL,'','law','273-ФЗ, часть 4 статьи 73',1,'evidence',0,1,'prior_profession','admission',0,1,1,NULL)""",
        """INSERT INTO admission_rules VALUES ('po_approved','Проверена утверждённая программа, её продолжительность и специальные требования','po',NULL,'','law','273-ФЗ, часть 8 статьи 73; применяется историческая редакция программы',1,'condition',0,0,'approved_program','admission',0,1,1,NULL)""",
    ),

    # N02: independent registry; existing templates and student documents are unchanged.
    (
        "CREATE TABLE regulatory_documents (id INTEGER PRIMARY KEY,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
        """CREATE TABLE regulatory_versions (
            id INTEGER PRIMARY KEY,document_id INTEGER NOT NULL REFERENCES regulatory_documents(id),
            revision INTEGER NOT NULL CHECK(revision>0),title TEXT NOT NULL CHECK(length(trim(title))>0),
            category TEXT NOT NULL CHECK(category IN ('charter','license','local','pedagogical','staff','founder','orders','privacy','other')),
            number TEXT NOT NULL DEFAULT '',document_date TEXT NOT NULL DEFAULT '',effective_date TEXT NOT NULL DEFAULT '',
            approval_basis TEXT NOT NULL DEFAULT '',edition TEXT NOT NULL DEFAULT '',note TEXT NOT NULL DEFAULT '',
            pdf_path TEXT NOT NULL DEFAULT '',pdf_sha256 TEXT NOT NULL DEFAULT '',
            docx_path TEXT NOT NULL DEFAULT '',docx_sha256 TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(document_id,revision),
            CHECK((pdf_path='' AND pdf_sha256='') OR (pdf_path<>'' AND length(pdf_sha256)=64)),
            CHECK((docx_path='' AND docx_sha256='') OR (docx_path<>'' AND length(docx_sha256)=64)))""",
        """CREATE TABLE regulatory_status_events (id INTEGER PRIMARY KEY,
            version_id INTEGER NOT NULL REFERENCES regulatory_versions(id),
            status TEXT NOT NULL CHECK(status IN ('draft','active','archived')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),note TEXT NOT NULL)""",
        """CREATE TABLE regulatory_links (version_id INTEGER NOT NULL REFERENCES regulatory_versions(id),
            target_version_id INTEGER NOT NULL REFERENCES regulatory_versions(id),
            PRIMARY KEY(version_id,target_version_id),CHECK(version_id<>target_version_id))""",
        "CREATE INDEX regulatory_latest_status ON regulatory_status_events(version_id,id)",
        *(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Regulatory history is immutable'); END"  # nosec B608 # Fixed internal table/action names.
          for table in ('regulatory_documents','regulatory_versions','regulatory_status_events','regulatory_links')
          for action in ('UPDATE','DELETE')),
        """CREATE TRIGGER regulatory_documents_no_replace BEFORE INSERT ON regulatory_documents
            WHEN EXISTS (SELECT 1 FROM regulatory_documents WHERE id=NEW.id)
            BEGIN SELECT RAISE(ABORT,'Regulatory history is immutable'); END""",
        """CREATE TRIGGER regulatory_versions_no_replace BEFORE INSERT ON regulatory_versions
            WHEN EXISTS (SELECT 1 FROM regulatory_versions WHERE id=NEW.id OR (document_id=NEW.document_id AND revision=NEW.revision))
            BEGIN SELECT RAISE(ABORT,'Regulatory history is immutable'); END""",
        """CREATE TRIGGER regulatory_events_no_replace BEFORE INSERT ON regulatory_status_events
            WHEN EXISTS (SELECT 1 FROM regulatory_status_events WHERE id=NEW.id)
            BEGIN SELECT RAISE(ABORT,'Regulatory history is immutable'); END""",
        """CREATE TRIGGER regulatory_links_no_replace BEFORE INSERT ON regulatory_links
            WHEN EXISTS (SELECT 1 FROM regulatory_links WHERE version_id=NEW.version_id AND target_version_id=NEW.target_version_id)
            BEGIN SELECT RAISE(ABORT,'Regulatory history is immutable'); END""",
    ),

)


class Database:
    def __init__(self, path: Path, *, create: bool = True):
        self.path = path.resolve()
        _require_local(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            f"{self.path.as_uri()}?mode={'rwc' if create else 'rw'}", uri=True, timeout=10
        )
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            self._migrate()
        except Exception:
            self.close()
            raise

    def _migrate(self) -> None:
        # Блокировка до чтения версии исключает гонку двух первых запусков.
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            exists = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            versions = [row[0] for row in self.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )] if exists else []
            if versions != list(range(1, len(versions) + 1)):
                raise RuntimeError("Нарушена последовательность миграций базы.")
            if len(versions) > len(MIGRATIONS):
                raise RuntimeError("База создана более новой версией приложения.")
            if versions and len(versions) < len(MIGRATIONS):
                # Reserved write lock blocks concurrent writers. Read via a separate
                # connection: backing up the connection holding a write lock would hang.
                from proficentr.storage import backup_database

                folder = self.path.parent / "migration-backups"
                folder.mkdir(exist_ok=True)
                backup = folder / (uuid.uuid4().hex + ".sqlite3")
                trial = folder / (uuid.uuid4().hex + ".trial.sqlite3")
                with closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)) as source:
                    backup_database(source, backup)
                try:
                    with closing(sqlite3.connect(backup)) as source:
                        backup_database(source, trial)
                    with closing(sqlite3.connect(trial)) as copy:
                        copy.execute("PRAGMA foreign_keys = ON")
                        with copy:
                            self._apply(copy, len(versions))
                        if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                            raise RuntimeError("Проверка пробной миграции не пройдена.")
                finally:
                    trial.unlink(missing_ok=True)
            self._apply(self.connection, len(versions))

    @staticmethod
    def _apply(connection, applied):
        for version, statements in enumerate(MIGRATIONS, start=1):
            if version <= applied:
                continue
            for statement in statements:
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (version,))

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
