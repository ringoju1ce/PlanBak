"""应用介绍；复用统一的主题、固定尺寸和关闭行为。"""

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QDialogButtonBox, QLabel, QVBoxLayout

from . import __version__
from .dialogs import FixedDialog
from .i18n import LanguageContext


class AboutDialog(FixedDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        if self.language_context is None:
            self.language_context = LanguageContext(parent=self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)

        title = QLabel("PlanBak", self)
        font = title.font()
        font.setPointSizeF(font.pointSizeF() * 1.8)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        self.version_label = QLabel(self)
        layout.addWidget(self.version_label)

        self.subtitle = QLabel(self)
        font = self.subtitle.font()
        font.setBold(True)
        self.subtitle.setFont(font)
        layout.addWidget(self.subtitle)

        self.description_labels = []
        for _ in range(3):
            label = QLabel(self)
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            layout.addWidget(label)
            self.description_labels.append(label)

        layout.addSpacing(4)
        self.credits = QLabel(self)
        layout.addWidget(self.credits)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.language_context.changed.connect(self.retranslate)
        self.retranslate()

    def retranslate(self):
        tr = self.language_context.translate
        self.setWindowTitle(tr("About PlanBak"))
        self.version_label.setText(tr("Version {version}", version=__version__))
        self.subtitle.setText(tr("iOS backup browser and editor"))
        for label, source in zip(
            self.description_labels,
            (
                "Browse local backups like a file manager, manage files and folders, and copy items between backup windows.",
                "Built-in plist editor and SQLite, image, and hexadecimal viewers.",
                "Supports encrypted and unencrypted backups. Changes are kept in the session until applied to an editable copy, preserving the original backup.",
            ),
        ):
            label.setText(tr(source))
        self.credits.setText(tr("Built with Python and Qt"))
        self.close_button.setText(tr("Close"))
        self.layout().invalidate()
        self.lock_size(self.sizeHint().expandedTo(QSize(520, 0)))
