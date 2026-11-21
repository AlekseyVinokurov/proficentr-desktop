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
