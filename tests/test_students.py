from dataclasses import replace
from datetime import date
import logging

import pytest

from proficentr import database as module
from proficentr.database import Database
from proficentr.students import Student, is_adult, normalize_snils, validate_student
from proficentr.student_repository import DuplicateStudentError, StudentRepository, StudentRepositoryError

TODAY = date(2026, 11, 19)


def sample(**changes):
    # Все значения вымышлены; СНИЛС сформирован для проверки алгоритма, не взят из документов.
    return replace(Student(surname="Тестовый", first_name="Пример", birth_date="1990-02-15"), **changes)


def test_create_reload_edit_restart_and_all_fields(tmp_path):
    path = tmp_path / "students.sqlite3"
    original = sample(
        patronymic="Примерович", citizenship="Тестовое гражданство", snils="900-100-200 93",
        phone="+7 (000) 000-00-01", email="student@example.org",
        registration_address="Вымышленный адрес регистрации", residential_address="Вымышленный адрес проживания",
        passport_series="00 00", passport_number="000001", passport_issued_by="Вымышленный орган",
        passport_issue_date="2010-01-01", passport_department_code="000001",
        education_level="Тестовый уровень", education_document="Тестовый диплом",
        education_details="Вымышленные реквизиты", workplace="Тестовое место работы",
        job_title="Тестовая должность", note="Тестовое примечание\nВторая строка",
    )
    with Database(path) as db:
        repo = StudentRepository(db.connection)
        saved = repo.save(original, today=TODAY)
        assert saved.id is not None and original.id is None
        assert repo.load(saved.id) == saved
        assert saved.passport_series == "0000"
        assert saved.passport_department_code == "000-001"
        assert saved.snils == "90010020093"
        changed = repo.save(replace(saved, surname="Исправленный", snils="", passport_number="000002"), today=TODAY)
        assert changed.id == saved.id
        assert len(repo.search()) == 1
    with Database(path) as db:
        assert StudentRepository(db.connection).load(saved.id) == changed


@pytest.mark.parametrize("query", ["тЕсТоВ", "ПРИМЕР", "тестовый пример", "000-00-01", "+7(000)", "900-100-200", "020093"])
def test_search_unicode_phone_snils(tmp_path, query):
    with Database(tmp_path / "test.sqlite3") as db:
        repo = StudentRepository(db.connection)
        student = repo.save(sample(phone="+7 (000) 000-00-01", snils="90010020093"), today=TODAY)
        assert [row.id for row in repo.search(query)] == [student.id]
        assert repo.search("%' OR 1=1 --") == []


@pytest.mark.parametrize("birth,adult", [
    ("2008-11-18", True), ("2008-11-19", True), ("2008-11-20", False),
    ("2026-11-19", False),
])
def test_adult_boundary(birth, adult):
    assert is_adult(date.fromisoformat(birth), TODAY) == adult
    if adult:
        assert validate_student(sample(birth_date=birth), today=TODAY)
    else:
        with pytest.raises(ValueError, match="18"):
            validate_student(sample(birth_date=birth), today=TODAY)


def test_leap_birthday():
    assert not is_adult(date(2008, 2, 29), date(2026, 2, 28))
    assert is_adult(date(2008, 2, 29), date(2026, 3, 1))


@pytest.mark.parametrize("field", ["surname", "first_name", "birth_date"])
def test_required_fields(field):
    with pytest.raises(ValueError, match="Заполните"):
        validate_student(sample(**{field: "  "}), today=TODAY)


@pytest.mark.parametrize("field,value", [
    ("birth_date", "2026-11-20"), ("birth_date", "2000-02-30"), ("birth_date", "19900215"),
    ("email", "wrong"), ("passport_series", "abc"), ("passport_number", "123"),
    ("passport_department_code", "12-34"), ("passport_issue_date", "1980-01-01"),
    ("passport_issue_date", "2026-11-20"),
])
def test_invalid_fields(field, value):
    with pytest.raises(ValueError):
        validate_student(sample(**{field: value}), today=TODAY)


def test_whitespace_and_optional_fields():
    saved = validate_student(sample(surname="  теСТовый  ", first_name="Пример   Составной"), today=TODAY)
    assert saved.surname == "теСТовый"
    assert saved.first_name == "Пример Составной"
    assert saved.snils == saved.passport_number == ""


@pytest.mark.parametrize("number", ["90010020093", "90010020700", "90010020800", "90010020901", "98010020056", "00100199800"])
def test_snils_checksum_branches(number):
    assert normalize_snils(number) == number


@pytest.mark.parametrize("number", ["90010020094", "00000000000", "letters", "9001002009", "900100200９３"])
def test_invalid_snils(number):
    with pytest.raises(ValueError):
        normalize_snils(number)


def test_duplicates_and_namesakes(tmp_path):
    with Database(tmp_path / "test.sqlite3") as db:
        repo = StudentRepository(db.connection)
        first = repo.save(sample(snils="90010020093"), today=TODAY)
        with pytest.raises(DuplicateStudentError) as error:
            repo.save(sample(), today=TODAY)
        assert error.value.matches[0].reason == "name_birth_date"
        assert error.value.matches[0].student.id == first.id
        with pytest.raises(DuplicateStudentError) as exact:
            repo.save(sample(surname="Другой", snils="90010020093"), today=TODAY)
        assert exact.value.matches[0].reason == "snils"
        namesake = repo.save(sample(birth_date="1991-02-15"), today=TODAY)
        assert namesake.id != first.id
        separate = repo.save(sample(), allow_duplicates=True, today=TODAY)
        assert separate.id not in {first.id, namesake.id}
        # Редактирование исключает саму карточку из проверки совпадений.
        assert repo.save(namesake, today=TODAY).id == namesake.id
        assert len(repo.search()) == 3


def test_upgrade_p021_preserves_center(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(module, "MIGRATIONS", module.MIGRATIONS[:2])
        with Database(path) as db:
            with db.connection:
                db.connection.execute("INSERT INTO training_center(id, short_name) VALUES (1, 'Synthetic')")
    for _ in range(2):
        with Database(path) as db:
            assert db.connection.execute("SELECT short_name FROM training_center").fetchone() == ("Synthetic",)
            assert StudentRepository(db.connection).search() == []
            assert db.connection.execute("SELECT version FROM schema_migrations").fetchall() == [(v,) for v in range(1, len(module.MIGRATIONS) + 1)]


def test_transaction_failure_and_no_personal_data_in_diagnostics(tmp_path, caplog, capsys):
    with Database(tmp_path / "test.sqlite3") as db:
        repo = StudentRepository(db.connection)
        original = repo.save(sample(note="PrivateSyntheticMarker"), today=TODAY)
        db.connection.execute("""CREATE TRIGGER reject_student BEFORE UPDATE ON students
            BEGIN SELECT RAISE(ABORT, 'PrivateSyntheticMarker'); END""")
        with caplog.at_level(logging.DEBUG), pytest.raises(StudentRepositoryError) as error:
            repo.save(replace(original, first_name="Изменение"), today=TODAY)
        assert repo.load(original.id) == original
        captured = capsys.readouterr()
        for sensitive in (original.surname, original.first_name, original.note):
            assert sensitive not in str(error.value) + caplog.text + captured.out + captured.err
        assert "PrivateSyntheticMarker" not in repr(original)


def test_update_missing_id_does_not_create_record(tmp_path):
    with Database(tmp_path / "test.sqlite3") as db:
        repo = StudentRepository(db.connection)
        with pytest.raises(StudentRepositoryError):
            repo.save(sample(id=123), today=TODAY)
        assert repo.search() == []
