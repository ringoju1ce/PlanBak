"""Small shared controls for document editors and read-only viewers."""

from html import escape

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QAction, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .dialogs import FixedDialog


class DocumentToolBar(QToolBar):
    """Standard action buttons, without QDialog's implicit Return activation."""

    def __init__(self, name, parent=None):
        super().__init__(name, parent)
        self.setAccessibleName(name)
        self.setMovable(False)
        self.setFloatable(False)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def command(self, text, callback, *, icon=None, hint=None):
        action = QAction(QIcon.fromTheme(icon) if icon else QIcon(), text, self)
        shortcut = (
            QKeySequence(hint).toString(QKeySequence.SequenceFormat.NativeText) if hint else ""
        )
        action.setToolTip(f"{text}（{shortcut}）" if shortcut else text)
        action.triggered.connect(callback)
        self.addAction(action)
        button = self.widgetForAction(action)
        button.setAccessibleName(text)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return button

    def stretch(self):
        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.addWidget(spacer)


class DocumentButtonBox(QDialogButtonBox):
    """Qt promotes an AcceptRole button on show even when autoDefault is false."""

    def clear_defaults(self):
        for button in self.buttons():
            button.setAutoDefault(False)
            button.setDefault(False)

    def showEvent(self, event):
        super().showEvent(event)
        self.clear_defaults()


def document_buttons(standards, parent=None):
    """Document footers require deliberate clicks/shortcuts, unlike confirmations."""
    box = DocumentButtonBox(standards, parent)
    box.clear_defaults()
    return box


class DocumentStatus(QWidget):
    """One bounded line; the complete plain-text message remains inspectable."""

    def __init__(self, text="", parent=None, *, note="", details=True):
        super().__init__(parent)
        self._text, self._note = str(text), note
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.installEventFilter(self)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.label, 1)
        self.details_button = QToolButton()
        self.details_button.setText("详情…")
        self.details_button.setAccessibleName("查看完整状态与说明")
        self.details_button.setToolTip("查看并复制完整状态与说明")
        self.details_button.clicked.connect(self.show_details)
        layout.addWidget(self.details_button)
        self.details_button.setVisible(details)
        self._refresh()

    def text(self):
        return self._text

    def setText(self, text):
        self._text = str(text)
        self._refresh()

    def clear(self):
        self.setText("")

    def full_text(self):
        return "\n\n".join(part for part in (self._text, self._note) if part)

    def _refresh(self):
        full = self.full_text()
        self.setToolTip("<qt><pre>" + escape(full) + "</pre></qt>" if full else "")
        self.label.setAccessibleName(full)
        summary = " ".join((self._text or self._note).splitlines())
        self.label.setText(
            self.fontMetrics().elidedText(summary, Qt.TextElideMode.ElideRight, self.label.width())
        )
        self.details_button.setEnabled(bool(full))
        if self.parentWidget() is not None:
            self.setVisible(bool(full))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def eventFilter(self, watched, event):
        if watched is self.label and event.type() == QEvent.Type.Resize:
            self._refresh()
        return super().eventFilter(watched, event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(
            self, "label"
        ):
            self._refresh()

    def show_details(self):
        self.show_text_details("状态与说明", self.full_text())

    def show_text_details(self, title, contents):
        dialog = FixedDialog(self.window())
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setAccessibleName(title)
        text.setReadOnly(True)
        text.setPlainText(contents)
        layout.addWidget(text)
        buttons = document_buttons(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.lock_size(QSize(680, 360))
        dialog.exec()
        dialog.deleteLater()
