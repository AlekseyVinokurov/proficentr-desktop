"""Независимое подключение XLSX ДПО и ПО через настройки."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from proficentr.frdo_templates import FrdoError


class FrdoPage(QWidget):
    def __init__(self, repository):
        super().__init__()
        self.repository = repository
        layout = QVBoxLayout(self)
        note = QLabel('Подключите исходные XLSX ФИС ФРДО отдельно для ДПО и ПО. '
                      'Файлы копируются в выбранное хранилище без изменений; прежние версии сохраняются. '
                      'Происхождение и актуальность шаблонов на портале автоматически не подтверждаются. '
                      'Формирование реестров будет доступно на следующем этапе.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.labels = {}
        self.buttons = {}
        for kind, caption in [('dpo', 'ДПО'), ('po', 'ПО')]:
            label = QLabel()
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(label)
            self.labels[kind] = label
            button = QPushButton(f'Подключить / заменить шаблон {caption}…')
            button.setEnabled(repository is not None)
            button.clicked.connect(lambda checked=False, k=kind: self.choose_template(k))
            layout.addWidget(button)
            self.buttons[kind] = button
        layout.addStretch()
        self.refresh()

    def refresh(self):
        for kind, label in self.labels.items():
            version = self.repository.active(kind) if self.repository else None
            if version is None:
                label.setText(f'{kind.upper()}: шаблон не подключён.')
                continue
            try:
                structure = self.repository.structure(kind)
                state = f'Структура проверена; справочников: {len(structure.dictionaries)}.'
            except (FrdoError, OSError) as error:
                state = str(error)
            label.setText(f'{kind.upper()}: {version.original_name}, версия {version.version}\n'
                          f'Подключён: {version.added_at}\nSHA-256: {version.sha256}\n{state}')

    def choose_template(self, kind):
        source, _ = QFileDialog.getOpenFileName(self, f'Шаблон ФИС ФРДО {kind.upper()}', '', 'Excel (*.xlsx)')
        if not source:
            return
        try:
            self.repository.register(source, kind)
        except (FrdoError, OSError) as error:
            QMessageBox.warning(self, 'Шаблон ФИС ФРДО', str(error))
        self.refresh()
