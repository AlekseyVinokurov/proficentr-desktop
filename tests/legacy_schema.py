"""Подготовка прежних схем для тестов миграций без полей P05.2."""

from proficentr import student_repository, program_repository

NEW_FIELDS = {'students': {'sex', 'citizenship_code'}, 'programs': {'dpo_area', 'dpo_group', 'po_profession'}}


def legacy_columns(connection, table):
    return ','.join(row[1] for row in connection.execute(f'PRAGMA table_info({table})')
                    if row[1] not in NEW_FIELDS.get(table, set()))


def use_legacy_card_fields(patch):
    for module, table in ((student_repository, 'students'), (program_repository, 'programs')):
        fields = tuple(name for name in module.FIELDS if name not in NEW_FIELDS[table])
        patch.setattr(module, 'FIELDS', fields)
        patch.setattr(module, 'DATA_FIELDS', fields[1:])


def remove_admission_schema(connection):
    for table in ('working_template_generations','template_supplements','working_template_events',
                  'regulatory_links','regulatory_status_events','regulatory_versions','regulatory_documents',
                  'admission_enrollment_checks','admission_checks','admission_document_events','admission_documents',
                  'admission_context_events','admission_rule_history','admission_rules'):
        connection.execute('DROP TABLE '+table)
    connection.execute('DELETE FROM schema_migrations WHERE version>=16')


def remove_preparation_schema(connection):
    remove_admission_schema(connection)
    connection.execute('DROP TABLE frdo_package_documents')
    connection.execute('DROP TABLE frdo_packages')
    connection.execute('DROP TABLE frdo_change_history')
    connection.execute('DROP TABLE frdo_training_details')
    for table, fields in NEW_FIELDS.items():
        for name in fields:
            connection.execute(f'ALTER TABLE {table} DROP COLUMN {name}')
