"""Закрытая схема данных шаблонов; без объектов моделей, SQLite и Qt."""

from copy import deepcopy
import math
import re

CENTER_FIELDS = "full_name short_name inn kpp ogrn legal_address actual_address postal_address phone email website bank_name bik settlement_account correspondent_account director_name director_position director_genitive license_number license_date license_authority".split()
STUDENT_FIELDS = "full_name surname first_name patronymic birth_date snils citizenship phone email registration_address residential_address passport_series passport_number passport_issued_by passport_issue_date passport_department_code education_level education_document education_details workplace job_title".split()
CUSTOMER_FIELDS = "full_name short_name kind inn kpp ogrn legal_address actual_address postal_address phone email bank_name bik settlement_account correspondent_account representative_name representative_position authority_basis power_of_attorney birth_date registration_address passport_series passport_number passport_issued_by passport_issue_date passport_department_code".split()
PROGRAM_FIELDS = "name direction kind hours code edition qualification grade".split()
TRAINING_FIELDS = "start_date end_date state".split()
ATTESTATION_FIELDS = "attestation_result grade qualification qualification_grade attestation_note".split()
COMPLETION_FIELDS = "completion_date completion_basis protocol_number protocol_date attestation_date".split()
SCHEMA = {
    "completion": dict.fromkeys(("date", "basis")),
    "protocol": dict.fromkeys("number date attestation_date kind chair members note".split()),
    "order": dict.fromkeys(("kind", "student_count")),
    "contract": dict.fromkeys(("total_price", "payment_terms", "currency", "student_count")),
    "center": dict.fromkeys(CENTER_FIELDS),
    "student": dict.fromkeys(STUDENT_FIELDS),
    "customer": dict.fromkeys(CUSTOMER_FIELDS),
    "program": dict.fromkeys(PROGRAM_FIELDS),
    "training": dict.fromkeys(TRAINING_FIELDS),
    "document": dict.fromkeys(("number", "date", "is_test", "is_draft")),
    "group": dict.fromkeys(("name", "start_date", "end_date")),
    "students": [dict.fromkeys(STUDENT_FIELDS + ["start_date", "end_date"] + ATTESTATION_FIELDS + COMPLETION_FIELDS)],
}
MAX_STUDENTS = 500
MAX_CONTEXT_BYTES = 1024 * 1024
MAX_STRING = 10_000
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


class DocxError(ValueError):
    """Ошибка без содержимого шаблонов, реквизитов и внутренних traceback."""


def validate_context(context):
    """Копируем только точные встроенные типы; методы и подклассы исключены."""
    budget = 0

    def visit(value, schema):
        nonlocal budget
        if type(schema) is dict:
            if type(value) is not dict or any(type(key) is not str or key not in schema for key in value):
                raise DocxError("Контекст содержит неизвестные поля или объекты вместо словарей.")
            return {key: visit(item, schema[key]) for key, item in value.items()}
        if type(schema) is list:
            if type(value) is not list or len(value) > MAX_STUDENTS:
                raise DocxError("Список слушателей должен содержать не более 500 записей.")
            return [visit(item, schema[0]) for item in value]
        if type(value) not in (str, int, float, bool, type(None)):
            raise DocxError("В контексте допустимы только простые значения, словари и списки.")
        if type(value) is str:
            if len(value) > MAX_STRING or INVALID_XML.search(value):
                raise DocxError("Недопустимая длина строки или управляющие символы в контексте.")
            budget += len(value.encode('utf-8'))
        elif type(value) is int and value.bit_length() > 63:
            raise DocxError("Число в контексте слишком велико.")
        elif type(value) is float and not math.isfinite(value):
            raise DocxError("В контексте недопустимы бесконечность и NaN.")
        if budget > MAX_CONTEXT_BYTES:
            raise DocxError("Контекст превышает 1 МиБ.")
        return value

    return visit(context, SCHEMA)


def fictional_context():
    context = {root: {field: '' for field in fields} for root, fields in SCHEMA.items() if root != 'students'}
    context['center'].update(full_name='Учебный центр «Пример» (вымышленный)', short_name='УЦ «Пример»', director_name='Примеров Пример Примерович')
    context['student'].update(full_name='Тестов Иван Примерович', surname='Тестов', first_name='Иван', patronymic='Примерович', birth_date='1990-01-01')
    context['customer'].update(full_name='Заказчик «Образец» (вымышленный)', kind='legal_entity')
    context['program'].update(name='Вымышленная учебная программа', direction='dpo', kind='dpo_upskilling', hours=72, code='ПРИМЕР', edition='1')
    context['training'].update(start_date='2020-01-10', end_date='2020-02-10', state='Подготовка')
    context['group'].update(name='ТЕСТ-01', start_date='2020-01-10', end_date='2020-02-10')
    context['order'].update(kind='group', student_count=2)
    context['contract'].update(total_price='1200,50', payment_terms='Вымышленный порядок оплаты', currency='RUB', student_count=2)
    context['document'].update(is_draft=False, number='ТЕСТ — НЕ ЗАРЕГИСТРИРОВАН', date='2020-01-10', is_test=True)
    context['completion'].update(date='2020-02-10', basis='Протокол ТЕСТ-П-2020-0001 от 2020-02-10')
    context['protocol'].update(number='ТЕСТ-П-2020-0001', date='2020-02-10', attestation_date='2020-02-10', kind='Итоговая аттестация', chair='Председатель Примеров', members='Член комиссии Тестов')
    context['students'] = [dict(context['student'], start_date='2020-01-10', end_date='2020-02-10'),
                           dict(context['student'], full_name='Образцова Анна Тестовна', surname='Образцова', first_name='Анна', patronymic='Тестовна', start_date='2020-01-10', end_date='2020-02-10')]
    for row in context['students']:
        row.update(dict.fromkeys(ATTESTATION_FIELDS, ''))
        row['attestation_result'] = 'Успешно прошёл'
        row.update(completion_date='2020-02-10', completion_basis='Протокол ТЕСТ-П-2020-0001 от 2020-02-10', protocol_number='ТЕСТ-П-2020-0001', protocol_date='2020-02-10', attestation_date='2020-02-10')
    return deepcopy(context)


def field_guide():
    lines = [
        'Теги вводятся в Microsoft Word целиком, с пробелами: {{ center.full_name }}.',
        'Поле сохраняет оформление своего участка текста. Не делите тег разным форматированием.',
        '',
        'Разрешённые поля (только простые значения):',
    ]
    for root, fields in SCHEMA.items():
        if root != 'students':
            lines.append(root + ': ' + ', '.join(root + '.' + field for field in fields))
    lines += [
        '',
        'students: список до 500 слушателей. В строке цикла доступны те же поля, что у student,',
        'а также row.start_date, row.end_date, row.attestation_result, row.grade,',
        'row.qualification, row.qualification_grade, row.attestation_note, row.completion_date,',
        'row.completion_basis, row.protocol_number, row.protocol_date, row.attestation_date. Имя row можно заменить.',
        '',
        'Условия: {% if customer.full_name %}Текст{% else %}Другой текст{% endif %}.',
        'Допустимы not, and, or и сравнение поля с константой через == или !=.',
        'Для целого абзаца используйте отдельные абзацы с {%p if document.is_test %}',
        'и {%p endif %}. Между ними поместите условный текст.',
        '',
        'Повторяющиеся строки таблицы: первая служебная строка содержит',
        '{%tr for row in students %}; следующая строка — {{ row.full_name }} и другие поля;',
        'последняя служебная строка содержит {%tr endfor %}. Служебные строки удаляются.',
        'Служебные теги должны занимать отдельные строки/абзацы.',
        '',
        'Функции, фильтры, арифметика, вложенные циклы, include/import, RichText,',
        'поддокументы, макросы, вложения и внешние ссылки не поддерживаются.',
        'Из полей Word разрешены только PAGE, NUMPAGES и SECTIONPAGES.',
        'Файл: до 10 МиБ; распакованный архив: до 32 МиБ, часть: до 8 МиБ, до 1000 частей.',
        'Для изображений используйте встроенные PNG/JPEG/GIF/BMP/TIFF.',
        '',
        'Тестовая генерация использует только вымышленные данные. Это не зарегистрированный документ.',
        'Для правок откройте копию, сохраните её в Word и загрузите как новую версию.',
        'Идентификатор варианта (например, dpo или po) задаётся вами и различает формы одного типа.',
        'Для пары «тип + вариант» активна одна версия. Возврат выполняется активацией старой версии.',
    ]
    return '\n'.join(lines)
