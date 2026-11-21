"""Искусственный XLSX для CI. Не является шаблоном для реального экспорта."""
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.table import Table, TableColumn
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.utils.cell import range_boundaries

from proficentr.frdo_profiles import PROFILES


def synthetic_template(kind):
    profile = PROFILES[kind]
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = 'Шаблон'
    lookup = workbook.create_sheet('Проверки')
    lookup.sheet_state = 'hidden'
    sheet.append(profile['headers'])
    sheet.cell(profile['last_row'], len(profile['headers'])).font = Font(name='Arial')
    for name, expression in profile['names'].items():
        workbook.defined_names.add(DefinedName(name, attr_text=expression))
        if expression.startswith('Таблица1'):
            continue
        coordinate = expression.split('!')[1]
        left, top, right, bottom = range_boundaries(coordinate)
        # Два искусственных варианта вместо копирования исходных справочников.
        for col in range(left, right + 1):
            lookup.cell(top, col, 'Тестовый вариант')
            if bottom > top:
                lookup.cell(top + 1, col, '000')
    left, top, right, bottom = range_boundaries(profile['table_ref'])
    captions = [f'Тестовый столбец {i}' for i in range(right - left + 1)]
    if kind == 'dpo':
        captions[5] = ' Наименование области профессиональной деятельности'
    for i, caption in enumerate(captions):
        lookup.cell(top, left + i, caption)
        lookup.cell(top + 1, left + i, 'Тестовый вариант')
    table = Table(displayName='Таблица1', ref=profile['table_ref'])
    table.tableColumns = [TableColumn(id=i + 1, name=caption) for i, caption in enumerate(captions)]
    lookup.add_table(table)
    for typ, sqref, formula1, formula2 in profile['validations']:
        validation = DataValidation(type=typ, sqref=sqref, formula1=formula1, formula2=formula2)
        sheet.add_data_validation(validation)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def preparation_template(kind):
    """Короткие вымышленные справочники для проверок адаптера и frozen smoke."""
    from openpyxl import load_workbook
    from proficentr.frdo_preparation import PROGRAM_TYPES
    from proficentr.frdo_validation import FIELDS
    from proficentr.issued_documents import KINDS
    workbook = load_workbook(BytesIO(synthetic_template(kind)))
    f = FIELDS[kind]
    dictionaries = {'A': tuple(v for k, v in KINDS.items() if k.startswith(kind)), 'B': ('Оригинал', 'Дубликат'),
        **{col: ('Нет',) for col in ('C', 'D', 'E')},
        'J': tuple(v for k, v in PROGRAM_TYPES.items() if k.startswith(kind)),
        f['sex']: ('Муж', 'Жен'), f['citizenship']: ('000', '004', '643'),
        f['form']: ('Очная', 'Заочная'), f['funding']: ('Платное обучение', 'Местный бюджет'),
        f['relations']: ('в образовательной организации', 'вне образовательной организации')}
    dictionaries.update({'L': ('Тестовая область',), 'M': ('Тестовая группа',), 'O': ('Высшее образование',)}
                        if kind == 'dpo' else {'L': ('Тестовая профессия',), 'M': ('Нет', '2', '3')})
    for validation in workbook['Шаблон'].data_validations.dataValidation:
        if validation.type != 'list':
            continue
        from openpyxl.utils import get_column_letter
        column = get_column_letter(next(iter(validation.sqref.ranges)).min_col)
        if column not in dictionaries:
            continue
        definition = workbook.defined_names[validation.formula1]
        if definition.attr_text.startswith('Таблица1'):
            cells = [workbook['Проверки'].cell(row, 7) for row in range(2, 35)]
        else:
            title, coordinate = next(definition.destinations)
            cells = [c for row in workbook[title][coordinate] for c in row]
        for index, cell in enumerate(cells):
            cell.value = dictionaries[column][index] if index < len(dictionaries[column]) else None
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
