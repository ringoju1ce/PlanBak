"""Fixed application dialogs, including widget-based replacements for static helpers."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .appearance import is_dark, sync_background


class DialogTitleBar(QWidget):
    """Dialog-only chrome: one close button and native window dragging."""

    def __init__(self, dialog):
        super().__init__(dialog)
        self.setObjectName("dialogTitleBar")
        self.setAutoFillBackground(True)
        self.sync_background()
        self.setFixedHeight(max(36, self.fontMetrics().height() + 14))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.addSpacing(28)
        self.title = QLabel(dialog.windowTitle(), self)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        layout.addWidget(self.title, 1)
        self.close_button = QToolButton(self)
        self.close_button.setObjectName("dialogCloseButton")
        self.close_button.setAccessibleName("关闭")
        self.close_button.setToolTip("关闭")
        self.close_button.setAutoRaise(True)
        self.close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_button.setFixedSize(28, 28)
        self.close_button.setText("×")
        close_font = self.close_button.font()
        close_font.setPointSizeF(close_font.pointSizeF() + 4)
        self.close_button.setFont(close_font)
        self.close_button.setStyleSheet(
            "QToolButton { border: none; border-radius: 12px; background: transparent; }"
            "QToolButton:hover { background: palette(midlight); }"
            "QToolButton:pressed { background: palette(mid); }"
        )
        self.close_button.clicked.connect(dialog.close)
        layout.addWidget(self.close_button)
        dialog.windowTitleChanged.connect(self.title.setText)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self.sync_background()
            if hasattr(self, "close_button"):
                self.close_button.setPalette(QPalette())
                # QStyleSheetStyle caches palette(...) colors for hover/pressed
                # rules independently of the button's effective palette.
                self.close_button.setStyleSheet(self.close_button.styleSheet())

    def sync_background(self):
        source = self.parentWidget().palette()
        if not is_dark(source):
            for group in (
                QPalette.ColorGroup.Active,
                QPalette.ColorGroup.Inactive,
                QPalette.ColorGroup.Disabled,
            ):
                source.setColor(
                    group,
                    QPalette.ColorRole.Base,
                    source.color(group, QPalette.ColorRole.Window).darker(106),
                )
        sync_background(self, source, QPalette.ColorRole.Base)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle:
                handle.startSystemMove()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        # A title double-click must not request maximize from the compositor.
        event.accept()


class FixedDialogMixin:
    """Own the dialog title bar; do not bounce native window-state changes."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.parentWidget() is not None:
            self.setWindowModality(Qt.WindowModality.WindowModal)
        self._locked_size = None
        unwanted = (
            Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowFullscreenButtonHint
            | Qt.WindowType.WindowContextHelpButtonHint
        )
        self.setWindowFlags((self.windowFlags() & ~unwanted) | Qt.WindowType.FramelessWindowHint)
        self.setSizeGripEnabled(False)
        # Qt's Adwaita Wayland decoration ignores per-window button hints.
        # Reserve application-owned chrome only for dialogs, leaving the main
        # window and desktop decoration settings alone.
        self._title_bar = DialogTitleBar(self)
        self.setContentsMargins(1, self._title_bar.height() + 1, 1, 1)

    def lock_size(self, size):
        screen = self.screen()
        if screen:
            available = screen.availableGeometry().size() - QSize(40, 80)
            size = size.boundedTo(available.expandedTo(QSize(240, 100)))
        self._locked_size = size
        self.setFixedSize(size)

    def _position_title_bar(self):
        bar = getattr(self, "_title_bar", None)
        if bar:
            bar.setGeometry(1, 1, max(0, self.width() - 2), bar.height())
            bar.raise_()

    def showEvent(self, event):
        if self.layout():
            self.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        if self._locked_size is None:
            if self.minimumSize() == self.maximumSize():
                size = self.minimumSize()
            else:
                size = self.sizeHint().expandedTo(self.minimumSizeHint())
                size = size.boundedTo(self.maximumSize())
            self.lock_size(size)
        super().showEvent(event)
        self.setFixedSize(self._locked_size)
        self._position_title_bar()

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest and getattr(self, "_locked_size", None):
            if self.layout():
                self.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
            self.setFixedSize(self._locked_size)
        return result

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_title_bar()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.Mid))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def showMaximized(self):
        # Decline application requests before any native state transition.
        if not self.isVisible():
            self.show()

    def showFullScreen(self):
        if not self.isVisible():
            self.show()


class FixedDialog(FixedDialogMixin, QDialog):
    pass


class DocumentWindow(QDialog):
    """Independent, resizable document window with native system decoration."""

    def __init__(self, parent=None):
        flags = (
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        super().__init__(None, flags)
        if parent is not None:
            self.setScreen(parent.screen())
        self.setWindowModality(Qt.WindowModality.NonModal)


class FixedMessageBox(FixedDialogMixin, QMessageBox):
    def __init__(
        self,
        icon=QMessageBox.Icon.NoIcon,
        title="",
        text="",
        buttons=QMessageBox.StandardButton.NoButton,
        parent=None,
    ):
        super().__init__(parent)
        self.setOption(QMessageBox.Option.DontUseNativeDialog, True)
        self.setIcon(icon)
        self.setWindowTitle(title)
        self.setText(text)
        self.setStandardButtons(buttons)

    def setVisible(self, visible):
        # QMessageBox resolves Escape/close before delivering showEvent.
        # Supply fallback buttons before that setup, leaving custom choices alone.
        if visible and not self.buttons():
            self.setStandardButtons(QMessageBox.StandardButton.Ok)
            self.setEscapeButton(QMessageBox.StandardButton.Ok)
        super().setVisible(visible)

    def showEvent(self, event):
        for label in self.findChildren(QLabel):
            if label.objectName() == "qt_msgbox_label":
                label.setWordWrap(True)
                label.setMaximumWidth(560)
        super().showEvent(event)

    @classmethod
    def question(
        cls,
        parent,
        title,
        text,
        buttons=QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        defaultButton=QMessageBox.StandardButton.NoButton,
    ):
        box = cls(QMessageBox.Icon.Question, title, "", buttons, parent)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(text)
        if defaultButton != QMessageBox.StandardButton.NoButton:
            box.setDefaultButton(defaultButton)
        result = QMessageBox.StandardButton(box.exec())
        box.deleteLater()
        return result


class FixedInputDialog(FixedDialogMixin, QInputDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMinimumWidth(400)
        self.setMaximumWidth(520)

    def showEvent(self, event):
        if self._locked_size is None:
            height = max(110, self.sizeHint().height(), self.minimumSizeHint().height())
            self.lock_size(QSize(420, height))
        super().showEvent(event)

    @classmethod
    def getText(cls, parent, title, label, echo=QLineEdit.EchoMode.Normal, text=""):
        dialog = cls(parent)
        dialog.setWindowTitle(title)
        dialog.setLabelText(label)
        dialog.setInputMode(QInputDialog.InputMode.TextInput)
        dialog.setTextEchoMode(echo)
        dialog.setTextValue(text)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        result = (dialog.textValue() if accepted else "", accepted)
        dialog.deleteLater()
        return result

    @classmethod
    def getItem(cls, parent, title, label, items, current=0, editable=True):
        dialog = cls(parent)
        dialog.setWindowTitle(title)
        dialog.setLabelText(label)
        dialog.setComboBoxItems(items)
        dialog.setComboBoxEditable(editable)
        if 0 <= current < len(items):
            dialog.setTextValue(items[current])
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        result = (dialog.textValue() if accepted else "", accepted)
        dialog.deleteLater()
        return result


class FixedFileDialog(FixedDialogMixin, QFileDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Native platform pickers cannot consistently enforce application geometry.
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        self.lock_size(QSize(760, 480))

    @classmethod
    def getExistingDirectory(cls, parent, caption="", directory=""):
        dialog = cls(parent, caption, directory)
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        paths = dialog.selectedFiles() if accepted else []
        result = paths[0] if paths else ""
        dialog.deleteLater()
        return result

    @classmethod
    def getOpenFileNames(cls, parent, caption="", directory="", filter=""):
        dialog = cls(parent, caption, directory, filter)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        result = (dialog.selectedFiles(), dialog.selectedNameFilter()) if accepted else ([], "")
        dialog.deleteLater()
        return result

    @classmethod
    def getSaveFileName(cls, parent, caption="", directory="", filter=""):
        dialog = cls(parent, caption, directory, filter)
        dialog.setFileMode(QFileDialog.FileMode.AnyFile)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        paths = dialog.selectedFiles() if accepted else []
        result = (paths[0] if paths else "", dialog.selectedNameFilter() if accepted else "")
        dialog.deleteLater()
        return result


class FixedProgressDialog(FixedDialog):
    """A compact progress dialog with a normal layout below its close-only title."""

    canceled = Signal()

    def __init__(self, text, cancel_text, minimum, maximum, parent=None):
        super().__init__(parent)
        self.lock_size(QSize(640, 160))
        self._cancel_requested = False
        self._cancellable = True
        self._finished = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 10)
        layout.setSpacing(8)
        self._status_label = QLabel(self)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setTextFormat(Qt.TextFormat.PlainText)
        self._status_label.setWordWrap(False)
        self._status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self._status_label, 1)
        self._progress = QProgressBar(self)
        self._progress.setRange(minimum, maximum)
        layout.addWidget(self._progress)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._cancel_button = QPushButton(cancel_text, self)
        self._cancel_button.clicked.connect(self.close)
        buttons.addWidget(self._cancel_button)
        layout.addLayout(buttons)
        self.setLabelText(text)

    def setRange(self, minimum, maximum):
        self._progress.setRange(minimum, maximum)

    def setValue(self, value):
        self._progress.setValue(value)

    def value(self):
        return self._progress.value()

    def setLabelText(self, text):
        single_line = " ".join(text.splitlines())
        elided = self._status_label.fontMetrics().elidedText(
            single_line, Qt.TextElideMode.ElideMiddle, max(1, self.width() - 48)
        )
        self._status_label.setToolTip(text)
        self._status_label.setText(elided)

    def reject(self):
        self.close()

    def set_cancellable(self, enabled):
        self._cancellable = enabled
        self._cancel_button.setEnabled(enabled)
        self._title_bar.close_button.setEnabled(enabled)

    def finish(self):
        self._finished = True
        self.close()

    def closeEvent(self, event):
        if self._finished:
            event.accept()
            return
        if not self._cancellable:
            event.ignore()
            return
        if not self._cancel_requested:
            self._cancel_requested = True
            self.canceled.emit()
        event.accept()
