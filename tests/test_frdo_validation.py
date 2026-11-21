from datetime import date

import pytest

from proficentr.frdo_templates import FrdoError, TemplateStructure
from proficentr.frdo_profiles import PROFILES
from proficentr.frdo_validation import FIELDS, PreExportValidator, export_filename, normalize_text


def sample(kind):
    f = FIELDS[kind]
    values = {'A': 'Удостоверение о повышении квалификации' if kind == 'dpo' else 'Свидетельство о профессии рабочего, должности служащего',
              'B': 'Оригинал', 'C': 'Нет', 'D': 'Нет', 'E': 'Нет', 'F': 'ТЕСТ', 'G': '000001',
              'H': '20.11.2026', 'I': 'ТЕСТ/1', 'K': 'Тестовая программа'}
    values.update({f[k]: v for k, v in dict(start='2025', end='2026', hours='72', surname='Тестов',
        first_name='Тест', patronymic='Нет', birth='01.01.1990', sex='Муж', citizenship='000',
        form='Очная', funding='Платное обучение', relations='в образовательной организации').items()})
    values['N' if kind == 'dpo' else 'L'] = 'Тестовая квалификация'
    dictionaries = {'A': (values['A'], 'Справка об обучении'), 'B': ('Оригинал', 'Дубликат'),
                    **{c: ('Нет', 'Да') for c in ('C', 'D', 'E')},
                    f['citizenship']: ('000', '004', '643'), f['sex']: ('Муж', 'Жен'),
                    f['form']: ('Очная', 'Очно-заочная (вечерняя)', 'Заочная'),
                    f['funding']: ('Платное обучение',), f['relations']: ('в образовательной организации',)}
    return PreExportValidator(TemplateStructure(kind, PROFILES[kind]['headers'], dictionaries, {})), values


def validate(validator, values):
    return validator.validate('ТЕСТ-7', values, today=date(2026, 11, 21))


@pytest.mark.parametrize('kind', ['dpo', 'po'])
def test_valid_original_does_not_mutate_input_or_lose_zeros(kind):
    validator, values = sample(kind)
    original = values.copy()
    result = validate(validator, values)
    assert result.valid, [i.message() for i in result.issues]
    assert values == original
    assert result.values['G'] == '000001' and result.values[FIELDS[kind]['citizenship']] == '000'
    assert result.values.get(FIELDS[kind]['change'], '') == ''


@pytest.mark.parametrize('value,expected', [('  Тест  Тест  ', 'Тест Тест'), ('Тест\u00a0\u202fТест', 'Тест Тест'),
    ('Тест\t\r\nТест', 'Тест Тест'), ('  00  01  ', '00  01')])
def test_normalization(value, expected):
    assert normalize_text(value, preserve_internal_spaces='00' in value) == expected


@pytest.mark.parametrize('bad', ['\u200b', '\ufeff', '\x00', '\x07', '\u202e', '\u00ad', '\u2007'])
def test_invisible_control_and_unprintable_characters_rejected(bad):
    validator, values = sample('dpo')
    values['K'] += bad
    assert any('Unicode' in i.reason for i in validate(validator, values).issues)


@pytest.mark.parametrize('column,value', [('V', 'Тестoв'), ('V', '-Тестов'), ('G', '00  01'), ('G', '123@'),
    ('F', '1' * 21), ('I', '1' * 31), ('K', 'Тест☆'), ('K', 'А' * 601), ('AE', 643), ('H', '31.02.2026'),
    ('H', '2026-11-20'), ('AA', '123-456-789 10'), ('AA', '11223344595'), ('S', '1977'), ('T', '2024'), ('U', '0')])
def test_field_errors_include_record_field_value_and_reason(column, value):
    validator, values = sample('dpo')
    values[column] = value
    result = validate(validator, values)
    errors = [i for i in result.issues if i.column == column]
    assert errors and errors[0].value == value and errors[0].record == 'ТЕСТ-7'
    assert errors[0].field in PROFILES['dpo']['headers']
    assert 'ТЕСТ-7' in errors[0].message()
    assert values[column] == value


@pytest.mark.parametrize('snils,valid', [('112-233-445 95', True), ('112-233-445 96', False),
    ('000-000-000 00', False), ('001-001-998 00', True)])
def test_snils_format_and_checksum(snils, valid):
    validator, values = sample('dpo')
    values['AA'] = snils
    assert validate(validator, values).valid == valid


@pytest.mark.parametrize('kind', ['dpo', 'po'])
def test_conditional_requirements(kind):
    validator, values = sample(kind)
    f = FIELDS[kind]
    values[f['citizenship']] = '643'
    assert any(i.column == f['snils'] for i in validate(validator, values).issues)
    values[f['citizenship']] = '004'
    assert validate(validator, values).valid
    values['H'] = '20.11.2020'
    values[f['start']] = '2019'
    values[f['end']] = '2020'
    for n in ('birth', 'form', 'funding', 'relations', 'citizenship'):
        values.pop(f[n])
    assert validate(validator, values).valid
    values['H'] = '20.11.2026'
    result = validate(validator, values)
    assert all(any(i.column == f[n] for i in result.issues) for n in ('birth', 'form', 'funding', 'relations', 'citizenship'))


@pytest.mark.parametrize('kind', ['dpo', 'po'])
def test_dictionary_mismatch_suggests_exact_spelling_and_explicit_mapping_is_separate(kind):
    validator, values = sample(kind)
    column = FIELDS[kind]['sex']
    values[column] = 'муж'
    result = validate(validator, values)
    assert not result.valid and result.values[column] == 'муж'
    assert next(i for i in result.issues if i.column == column).suggestions == ('Муж',)
    mappings = {kind: {column: {'муж': 'Муж'}}}
    explicit = PreExportValidator(validator.structure, mappings=mappings)
    assert validate(explicit, values).valid
    other = PreExportValidator(validator.structure, mappings={'po' if kind == 'dpo' else 'dpo': mappings[kind]})
    assert not validate(other, values).valid


def test_dictionary_exact_spaces_are_preserved():
    validator, values = sample('dpo')
    validator.structure.dictionaries['AB'] = ('Очная  форма',)
    values['AB'] = 'Очная  форма'
    assert validate(validator, values).values['AB'] == 'Очная  форма'
    assert validate(validator, values).valid


@pytest.mark.parametrize('kind', ['dpo', 'po'])
def test_original_rejects_duplicate_and_correction_fields(kind):
    validator, values = sample(kind)
    values[FIELDS[kind]['change']] = '123'
    assert not validate(validator, values).valid
    values.pop(FIELDS[kind]['change'])
    values['B'] = 'Дубликат'
    assert not validate(validator, values).valid


def test_po_program_limit_hours_and_arts_exception():
    validator, values = sample('po')
    values['K'] = 'А' * 256
    values['P'] = '5'
    assert {'K', 'P'} <= {i.column for i in validate(validator, values).issues}
    values['K'] = 'Тестовая программа'
    values['P'] = '6'
    arts = 'Свидетельство об освоении дополнительных предпрофессиональных программ в области искусств'
    validator.structure.dictionaries['A'] += (arts,)
    values['A'] = arts
    assert any(i.column == 'L' for i in validate(validator, values).issues)
    values.pop('L')
    assert validate(validator, values).valid


def test_filename_uses_explicit_year_number_and_kpp():
    assert export_filename('1125900003360', '590301001', 2026, 9) == '1125900003360_590301001_26_9.xlsx'
    assert export_filename('0000000000001', '000000001', 2003, 1) == '0000000000001_000000001_03_1.xlsx'


@pytest.mark.parametrize('args', [('1', '590301001', 2026, 1), ('1125900003360', '5903010010', 2026, 1),
    ('1125900003360', '590301001', True, 1), ('1125900003360', '590301001', 26, 1),
    ('1125900003360', '590301001', 2026, 0), ('1125900003360', '590301001', 2026, True)])
def test_invalid_filename_arguments(args):
    with pytest.raises(FrdoError):
        export_filename(*args)


@pytest.mark.parametrize('value', [' 00001 ', '00\u00a001', '00\t01', '00  01'])
def test_document_number_whitespace_requires_confirmation(value):
    validator, values = sample('dpo')
    values['G'] = value
    result = validate(validator, values)
    assert result.values['G'] == value and any(i.column == 'G' for i in result.issues)
    assert 'G' not in result.changes
