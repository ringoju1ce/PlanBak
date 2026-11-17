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
from .i18n import LanguageContext


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
        action.setProperty("planbak_shortcut_hint", shortcut)
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


def translate_command(button, text):
    action = button.defaultAction()
    shortcut = action.property("planbak_shortcut_hint")
    action.setText(text)
    action.setToolTip(f"{text} ({shortcut})" if shortcut else text)
    button.setAccessibleName(text)


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
        self._fallback_language = LanguageContext(parent=self)
        self.retranslate()
        self.details_button.clicked.connect(self.show_details)
        layout.addWidget(self.details_button)
        self.details_button.setVisible(details)
        self._refresh()

    def retranslate(self):
        context = getattr(self.window(), "language_context", None) or self._fallback_language
        tr = context.translate
        self.details_button.setText(tr("Details…"))
        self.details_button.setAccessibleName(tr("View full status and details"))
        self.details_button.setToolTip(tr("View and copy full status and details"))

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
        context = getattr(self.window(), "language_context", None) or self._fallback_language

        def title():
            return context.translate("Status and Details")

        self.show_text_details(title, self.full_text)

    def show_text_details(self, title, contents):
        dialog = FixedDialog(self.window())
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        layout.addWidget(text)
        buttons = document_buttons(QDialogButtonBox.StandardButton.Close, dialog)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        context = dialog.language_context or self._fallback_language

        def update_text():
            heading = title() if callable(title) else title
            body = contents() if callable(contents) else contents
            dialog.setWindowTitle(heading)
            text.setAccessibleName(heading)
            if text.toPlainText() != body:
                text.setPlainText(body)
            close.setText(context.translate("Close"))

        update_text()
        if context:
            context.changed.connect(update_text)
        dialog.lock_size(QSize(680, 360))
        dialog.exec()
        if context:
            context.changed.disconnect(update_text)
        dialog.deleteLater()
