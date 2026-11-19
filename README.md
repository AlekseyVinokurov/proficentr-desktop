# Proficentr Desktop

Настольное Windows-приложение для автоматизации документооборота учебных центров ДПО и ПО. Первый пользователь — ЧУ ДПО «Профицентр-2»; каркас не привязан к одной организации.

Этап **P01**: окно PySide6 с восемью разделами, локальная SQLite, миграции и Windows-сборка. Карточки слушателей, организаций, договоры, документы и обмен с ФИС ФРДО пока не реализованы.

## Запуск из исходного кода

Нужен Python 3.12. В Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[test]"
.venv\Scripts\python -m proficentr
```

В Linux / WSL:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m proficentr
```

Для обычного окна Linux нужен графический дисплей (в WSL — WSLg). Проверка без дисплея:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m proficentr --smoke-test
.venv/bin/python -m pytest -q
```

`--smoke-test` открывает окно, проверяет все переходы и автоматически завершает приложение. Тесты используют временные базы и синтетические данные.

## Данные

SQLite создаётся при первом запуске и сохраняется между запусками:

- Windows: `%LOCALAPPDATA%\Proficentr\proficentr.sqlite3`.
- Linux: `$XDG_DATA_HOME/proficentr/proficentr.sqlite3`, по умолчанию `~/.local/share/proficentr/proficentr.sqlite3`.
- Переопределение: `PROFICENTR_DATA_DIR` — абсолютный путь к локальному каталогу, например `$env:PROFICENTR_DATA_DIR = 'C:\ProficentrData'` в PowerShell.

Храните данные вне исходников и папки EXE. Не используйте сетевые диски, сетевые и синхронизируемые папки. Приложение отклоняет UNC, подключённые сетевые диски Windows и известные сетевые файловые системы Linux; произвольные облачные клиенты автоматически не определяются. Для резервного копирования закройте приложение и скопируйте каталог данных.

Схема P01 содержит только `schema_migrations`. Миграции в `database.py` применяются последовательно в транзакции; новые добавляются в конец списка. Более новая схема отклоняется, соединение закрывается при выходе и при ошибке запуска.

## Структура

```text
src/proficentr/
  __main__.py       точка входа и жизненный цикл
  settings.py       пользовательские пути
  database.py       SQLite и миграции
  services.py       сценарий запуска, место бизнес-логики
  ui/main_window.py окно, навигация и страницы
run_proficentr.py   точка входа PyInstaller
tests/             тесты БД, настроек, импорта и интерфейса
.github/workflows/windows.yml
```

Документы и ФРДО пока представлены отдельными страницами. Их реализация на следующих этапах будет вынесена в модули `documents` и `frdo`; пустые пакеты заранее не создаются.

## Windows-сборка

Тяжёлая сборка выполняется только в GitHub Actions: при `push` или вручную через **Actions → Windows — тесты и сборка → Run workflow**. Workflow устанавливает Python 3.12 и зависимости `.[test,build]`, запускает pytest, собирает приложение PyInstaller в режиме `onedir`, проверяет запуск EXE дважды и создание SQLite, затем загружает артефакт `Proficentr-Desktop-Windows`.

Скачайте артефакт со страницы успешного запуска Actions. Внутри находится `Proficentr-Desktop-Windows.zip`; распакуйте его целиком и запустите `Proficentr/Proficentr.exe`. Папка `_internal` должна оставаться рядом с EXE. Python на компьютере пользователя не нужен. Срок хранения артефакта — 14 дней. Автоматическая публикация GitHub Releases не настроена.

Команда сборки в CI соответствует [документации PyInstaller](https://pyinstaller.org/en/stable/usage.html):

```text
python -m PyInstaller --noconfirm --clean --onedir --windowed --name Proficentr --paths src run_proficentr.py
```

Лицензия проекта на этапе P01 не выбрана. Не добавляйте в Git персональные данные, сканы, рабочие базы и секреты; соответствующие файлы и каталоги включены в `.gitignore`.
