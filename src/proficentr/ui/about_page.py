"""About and optional, non-blocking official update checks."""

import json
from pathlib import Path
import shutil
import tempfile
import time

from PySide6.QtCore import QThread, QTimer, Signal, Qt
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from proficentr import settings, updates, updater

PRIVACY = (
    "Программа работает локально. База, сведения слушателей и организаций, документы и вложения "
    "хранятся в выбранном вами локальном хранилище и не передаются разработчику. "
    "Телеметрии, аналитики и удалённого доступа к базе нет. Основные функции доступны без интернета.\n\n"
    "Для проверки и загрузки обновлений программа обращается только к официальной инфраструктуре GitHub. "
    "Документы и содержимое базы в запросы не включаются. GitHub получает IP-адрес и стандартные "
    "технические сведения подключения. Проверка при запуске выполняется не чаще раза в сутки; "
    "установка — только после подтверждения.\n\n"
    "SQLite не шифрует данные. Одного пароля Windows недостаточно для комплексной защиты. "
    "За доступ, место хранения, резервные копии и необходимые организационные и технические меры "
    "защиты при эксплуатации отвечает образовательная организация в пределах своих законных обязанностей."
)


class Worker(QThread):
    done = Signal(object)
    failed = Signal()

    def __init__(self, operation, parent):
        super().__init__(parent)
        self.operation = operation

    def run(self):
        try:
            self.done.emit(self.operation())
        except Exception:
            self.failed.emit()  # Do not expose transport details or filesystem paths.


class AboutPage(QWidget):
    def __init__(self, storage):
        super().__init__()
        self.storage = storage
        self.info = None
        self.worker = None
        self.work = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Proficentr Desktop\nУстановленная версия: {updates.VERSION}"))
        source = QLabel(updates.OFFICIAL_URL)
        source.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(source)
        self.state = QLabel("Обновления не проверены.")
        self.state.setWordWrap(True)
        layout.addWidget(self.state)
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(False)
        layout.addWidget(self.notes)
        self.check = QPushButton("Проверить обновления")
        self.check.clicked.connect(self.check_updates)
        layout.addWidget(self.check)
        self.install = QPushButton("Обновить и перезапустить")
        self.install.setEnabled(False)
        self.install.clicked.connect(self.install_update)
        layout.addWidget(self.install)
        label = QLabel("Данные хранятся локально и не загружаются разработчику. Обновления проверяются по подписи.")
        label.setWordWrap(True)
        layout.addWidget(label)
        policy = QPushButton("Конфиденциальность и хранение данных")
        policy.clicked.connect(lambda: QMessageBox.information(self, "Хранение данных", PRIVACY))
        layout.addWidget(policy)

    def schedule_check(self):
        QTimer.singleShot(1000, self._daily)

    def _daily(self):
        stamp = settings.config_file().with_name("update-check.json")
        try:
            last = float(json.loads(stamp.read_text())["attempt"])
            if not 0 <= last <= time.time():
                last = 0
        except (OSError, ValueError, KeyError, TypeError):
            last = 0
        if time.time() - last >= 86400:
            self.check_updates()

    def _run(self, operation, done):
        self.check.setEnabled(False)
        self.install.setEnabled(False)
        self.worker = Worker(operation, self)
        self.worker.done.connect(done)
        self.worker.failed.connect(self._failed)
        self.worker.finished.connect(self._finished)
        # Never destroy a live QThread when the application quits.
        QApplication.instance().aboutToQuit.connect(self.worker.wait)
        self.worker.start()

    def _finished(self):
        self.window().setEnabled(True)
        self.check.setEnabled(True)
        self.install.setEnabled(self.info is not None)

    def _failed(self):
        self.window().setEnabled(True)
        self.state.setText("Не удалось проверить или подготовить обновление. Данные сохранены; работа доступна без интернета.")
        if self.work:
            shutil.rmtree(self.work, ignore_errors=True)
            self.work = None

    def check_updates(self):
        if self.worker and self.worker.isRunning():
            return
        self.info = None
        self.state.setText("Проверка официального GitHub…")
        try:
            stamp = settings.config_file().with_name("update-check.json")
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(json.dumps({"attempt": time.time()}), encoding="ascii")
        except OSError:
            self.state.setText("Не удалось сохранить время проверки. Проверьте доступ к настройкам.")
            return

        def fetch():
            with tempfile.TemporaryDirectory() as folder:
                return updates.release_info(Path(folder))
        self._run(fetch, self._checked)

    def _checked(self, info):
        self.info = info
        self.state.setText(f"Доступна версия {info['version']}. Можно отложить установку." if info else "Установлена актуальная версия.")
        self.notes.setPlainText(info["notes"] if info else "")

    def install_update(self):
        if not self.info or (self.worker and self.worker.isRunning()):
            return
        # Check unsaved organization fields before preparing shutdown.
        if not self.window().settings_page.confirm_leave():
            return
        if QMessageBox.question(self, "Установка обновления", "Скачать проверенный пакет, закрыть программу и перезапустить? Закройте другие экземпляры программы.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.work = updater.work_directory()
        except (OSError, RuntimeError) as error:
            QMessageBox.information(self, "Установка недоступна", str(error))
            return
        self.state.setText("Загрузка и проверка подписи пакета…")
        info, work = self.info, self.work
        self.window().setEnabled(False)
        self._run(lambda: updates.prepare_release(info, work), self._prepared)

    def _prepared(self, staged):
        self.window().setEnabled(True)
        try:
            if not self.window().settings_page.confirm_leave():
                self._failed()
                return
            updater.start_helper(staged, self.storage.database_path, self.storage.root)
        except Exception:
            self._failed()
            return
        QApplication.instance().quit()
