"""Fixed application dialogs, including widget-based replacements for static helpers."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
)


class FixedDialogMixin:
    """Keep normal dialog geometry and exclude resize/maximize/fullscreen controls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._locked_size = None
        self._normalizing_state = False
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.timeout.connect(self._restore_geometry)
        unwanted = (
            Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowFullscreenButtonHint
            | Qt.WindowType.WindowContextHelpButtonHint
        )
        self.setWindowFlags(
            (self.windowFlags() & ~unwanted)
            | Qt.WindowType.CustomizeWindowHint
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowCloseButtonHint
            | Qt.WindowType.MSWindowsFixedSizeDialogHint
        )
        self.setSizeGripEnabled(False)

    def lock_size(self, size):
        screen = self.screen()
        if screen:
            available = screen.availableGeometry().size() - QSize(40, 80)
            size = size.boundedTo(available.expandedTo(QSize(240, 100)))
        self._locked_size = size
        self.setFixedSize(size)

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

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest and getattr(self, "_locked_size", None):
            if self.layout():
                self.layout().setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
            self.setFixedSize(self._locked_size)
        return result

    def resizeEvent(self, event):
        super().resizeEvent(event)
        size = getattr(self, "_locked_size", None)
        if size is not None and self.size() != size:
            self._geometry_timer.start(0)

    def _restore_geometry(self):
        if self._locked_size is not None and self.isVisible():
            self.setFixedSize(self._locked_size)
            # Wayland can report a smaller client area after a window-state
            # transition even when the min/max hints did not change.
            if self.size() != self._locked_size:
                self.resize(self._locked_size)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange and not getattr(
            self, "_normalizing_state", False
        ):
            forbidden = Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen
            if self.windowState() & forbidden:
                self._normalizing_state = True
                try:
                    self.setWindowState(self.windowState() & ~forbidden)
                    if self._locked_size is not None:
                        self.setFixedSize(self._locked_size)
                finally:
                    self._normalizing_state = False
                self._geometry_timer.start(0)
        super().changeEvent(event)


class FixedDialog(FixedDialogMixin, QDialog):
    pass


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

    def showEvent(self, event):
        if not self.buttons():
            self.setStandardButtons(QMessageBox.StandardButton.Ok)
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


class FixedProgressDialog(FixedDialogMixin, QProgressDialog):
    """A stable horizontal progress window; long status text cannot resize it."""

    def __init__(self, text, cancel_text, minimum, maximum, parent=None):
        super().__init__("", cancel_text, minimum, maximum, parent)
        self.lock_size(QSize(640, 160))
        self._full_label_text = text
        self._status_label = QLabel(self)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setTextFormat(Qt.TextFormat.PlainText)
        self._status_label.setWordWrap(False)
        self._status_label.setMaximumWidth(self.width() - 40)
        self.setLabel(self._status_label)
        self.setLabelText(text)

    def setLabelText(self, text):
        self._full_label_text = text
        if not hasattr(self, "_status_label"):
            return super().setLabelText(text)
        single_line = " ".join(text.splitlines())
        elided = self._status_label.fontMetrics().elidedText(
            single_line, Qt.TextElideMode.ElideMiddle, max(1, self.width() - 48)
        )
        self._status_label.setToolTip(text)
        super().setLabelText(elided)
