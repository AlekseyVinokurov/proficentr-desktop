"""Главное окно и навигация P01."""

from pathlib import Path

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QMainWindow, QStackedWidget,
    QVBoxLayout, QWidget, QTabWidget,
)
from proficentr.storage import Storage
from proficentr.ui.settings_page import SettingsPage
from proficentr.frdo_preparation import FrdoPreparationRepository
from proficentr.ui.frdo_preparation_page import FrdoPreparationPage
from proficentr.student_repository import StudentRepository
from proficentr.ui.students_page import StudentsPage
from proficentr.counterparty_repository import CounterpartyRepository
from proficentr.ui.counterparties_page import CounterpartiesPage
from proficentr.program_repository import ProgramRepository
from proficentr.ui.programs_page import ProgramsPage
from proficentr.training_repository import TrainingRepository
from proficentr.ui.groups_page import GroupsPage
from proficentr.contract_repository import ContractRepository
from proficentr.ui.contracts_page import ContractsPage
from proficentr.attestation_repository import AttestationRepository
from proficentr.ui.attestations_page import AttestationsPage
from proficentr.issued_repository import IssuedDocumentRepository
from proficentr.ui.issued_documents_page import IssuedDocumentsPage
from proficentr.completion_repository import CompletionRepository
from proficentr.ui.completion_orders_page import CompletionOrdersPage
from proficentr.enrollment_order_repository import EnrollmentOrderRepository
from proficentr.ui.enrollment_orders_page import EnrollmentOrdersPage


SECTIONS = (
    "Главная", "Слушатели", "Организации", "Программы", "Группы",
    "Документы", "ФИС ФРДО", "Настройки",
)


class MainWindow(QMainWindow):
    def __init__(self, database_path: Path, storage: Storage | None = None, database=None):
        super().__init__()
        self.setWindowTitle("Proficentr Desktop")
        self.resize(1080, 700)
        self.setMinimumSize(760, 480)
        root = database_path.parent.parent if database_path.parent.name == "database" else database_path.parent
        self.attestation_repository = AttestationRepository(database.connection, storage or Storage(root)) if database else None
        container = QWidget()
        layout = QHBoxLayout(container)
        self.navigation = QListWidget()
        self.navigation.setAccessibleName("Разделы приложения")
        self.navigation.addItems(SECTIONS)
        self.navigation.setFixedWidth(190)
        self.pages = QStackedWidget()
        for section in SECTIONS:
            if section == "Слушатели":
                self.students_page = StudentsPage(StudentRepository(database.connection) if database else None)
                self.pages.addWidget(self.students_page)
                continue
            if section == "Организации":
                self.counterparties_page = CounterpartiesPage(CounterpartyRepository(database.connection) if database else None)
                self.pages.addWidget(self.counterparties_page)
                continue
            if section == "Программы":
                self.programs_page = ProgramsPage(ProgramRepository(database.connection) if database else None)
                self.pages.addWidget(self.programs_page)
                continue
            if section == "Группы":
                self.groups_page = GroupsPage(TrainingRepository(database.connection) if database else None, attestations=self.attestation_repository)
                self.pages.addWidget(self.groups_page)
                continue
            if section == "Документы":
                root = database_path.parent.parent if database_path.parent.name == "database" else database_path.parent
                self.contracts_page = ContractsPage(ContractRepository(database.connection, storage or Storage(root)) if database else None)
                self.orders_page = EnrollmentOrdersPage(EnrollmentOrderRepository(database.connection, storage or Storage(root)) if database else None)
                self.attestations_page = AttestationsPage(AttestationRepository(database.connection, storage or Storage(root)) if database else None)
                self.completion_orders_page = CompletionOrdersPage(CompletionRepository(database.connection, storage or Storage(root)) if database else None)
                self.issued_documents_page = IssuedDocumentsPage(IssuedDocumentRepository(database.connection, storage or Storage(root)) if database else None)
                self.documents_page = QTabWidget()
                self.documents_page.addTab(self.contracts_page, "Договоры")
                self.documents_page.addTab(self.orders_page, "Приказы о зачислении")
                self.documents_page.addTab(self.attestations_page, "Протоколы аттестации")
                self.documents_page.addTab(self.completion_orders_page, "Приказы о завершении")
                self.documents_page.addTab(self.issued_documents_page, "Выпускные документы")
                self.frdo_preparation_page = FrdoPreparationPage(FrdoPreparationRepository(database.connection, storage or Storage(root)) if database else None)
                self.documents_page.addTab(self.frdo_preparation_page, "Подготовка ФРДО")
                self.pages.addWidget(self.documents_page)
                continue
            if section == "Настройки":
                root = database_path.parent.parent if database_path.parent.name == "database" else database_path.parent
                self.settings_page = SettingsPage(database_path, storage or Storage(root), database)
                self.pages.addWidget(self.settings_page)
                continue
            page = QWidget()
            content = QVBoxLayout(page)
            content.setContentsMargins(24, 20, 24, 20)
            title = QLabel(section)
            font = title.font()
            font.setPointSize(20)
            font.setBold(True)
            title.setFont(font)
            content.addWidget(title)
            if section == "Главная":
                description = "Добро пожаловать в Proficentr Desktop.\nВыберите раздел в меню слева."
            else:
                description = "Раздел подготовлен. Функции будут добавлены на следующих этапах."
            label = QLabel(description)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            content.addWidget(label)
            content.addStretch()
            self.pages.addWidget(page)
        layout.addWidget(self.navigation)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(container)
        self.navigation.currentRowChanged.connect(self._navigate)
        self.navigation.setCurrentRow(0)
        self.groups_page.contract_requested.connect(self._create_contract)
        self.groups_page.order_requested.connect(self._create_order)
        self.groups_page.attestation_requested.connect(self._create_attestation)
        self.groups_page.completion_requested.connect(self._create_completion)
        self.groups_page.issuance_requested.connect(self._create_issued)
        self.issued_documents_page.templates_requested.connect(self._open_templates)
        self.completion_orders_page.templates_requested.connect(self._open_templates)

    def _create_contract(self, training_ids, group_id):
        self.navigation.setCurrentRow(SECTIONS.index("Документы"))
        self.documents_page.setCurrentWidget(self.contracts_page)
        self.contracts_page.open_card(training_ids=training_ids, group_id=group_id)

    def _open_templates(self):
        self.navigation.setCurrentRow(SECTIONS.index("Настройки"))
        self.settings_page.tabs.setCurrentWidget(self.settings_page.templates_page)

    def _create_issued(self, training_ids, group_id):
        self.navigation.setCurrentRow(SECTIONS.index("Документы"))
        self.documents_page.setCurrentWidget(self.issued_documents_page)
        self.issued_documents_page.open_training(training_ids=training_ids, group_id=group_id)

    def _create_completion(self, training_ids, group_id):
        self.navigation.setCurrentRow(SECTIONS.index("Документы"))
        self.documents_page.setCurrentWidget(self.completion_orders_page)
        self.completion_orders_page.open_card(training_ids=training_ids, group_id=group_id)

    def _create_attestation(self, training_ids, group_id):
        self.navigation.setCurrentRow(SECTIONS.index("Документы"))
        self.documents_page.setCurrentWidget(self.attestations_page)
        self.attestations_page.open_card(training_ids=training_ids, group_id=group_id)

    def _create_order(self, training_ids, group_id):
        self.navigation.setCurrentRow(SECTIONS.index("Документы"))
        self.documents_page.setCurrentWidget(self.orders_page)
        self.orders_page.open_card(training_ids=training_ids, group_id=group_id)

    def _navigate(self, index: int) -> None:
        if 0 <= index < len(SECTIONS):
            if self.pages.currentWidget() is self.settings_page and index != SECTIONS.index("Настройки"):
                if not self.settings_page.confirm_leave():
                    with QSignalBlocker(self.navigation):
                        self.navigation.setCurrentRow(SECTIONS.index("Настройки"))
                    return
            self.pages.setCurrentIndex(index)
            self.statusBar().showMessage(f"Готово • {SECTIONS[index]}")

    def closeEvent(self, event):
        if self.settings_page.confirm_leave():
            event.accept()
        else:
            event.ignore()
