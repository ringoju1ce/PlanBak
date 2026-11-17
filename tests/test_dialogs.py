import pytest
from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QStyleFactory,
    QToolButton,
    QWidget,
)

from planbak.app import PasswordDialog
from planbak.dialogs import (
    FixedFileDialog,
    FixedInputDialog,
    FixedMessageBox,
    FixedProgressDialog,
)


def make_dialog(kind):
    if kind == "password":
        return PasswordDialog(None)
    if kind == "progress":
        return FixedProgressDialog("建立备份索引…", "取消", 0, 100)
    if kind == "message":
        return FixedMessageBox(
            FixedMessageBox.Icon.Warning, "提示", "测试消息", FixedMessageBox.StandardButton.Ok
        )
    if kind == "input":
        dialog = FixedInputDialog()
        dialog.setLabelText("新名称：")
        return dialog
    return FixedFileDialog()


@pytest.mark.parametrize("kind", ["password", "progress", "message", "input", "file"])
def test_dialog_cannot_resize_maximize_or_fullscreen(qtbot, kind):
    dialog = make_dialog(kind)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    original = dialog.size()
    assert dialog.minimumSize() == dialog.maximumSize() == original
    assert not dialog.isSizeGripEnabled()
    flags = dialog.windowFlags()
    assert not flags & Qt.WindowType.WindowMaximizeButtonHint
    assert not flags & Qt.WindowType.WindowMinimizeButtonHint
    assert not flags & Qt.WindowType.WindowFullscreenButtonHint
    dialog.resize(original + QSize(300, 200))
    assert dialog.size() == original
    dialog.showMaximized()
    qtbot.wait(10)
    assert not dialog.isMaximized() and dialog.size() == original
    dialog.showFullScreen()
    qtbot.wait(10)
    assert not dialog.isFullScreen() and dialog.size() == original


def test_progress_text_updates_keep_wide_geometry_and_full_tooltip(qtbot):
    dialog = FixedProgressDialog("解锁并读取备份清单…", "取消", 0, 0)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    original = dialog.size()
    assert original.width() / original.height() >= 3.5
    text = "正在提取：" + "/long-directory/文件名称" * 100 + "\n第二行"
    dialog.setRange(0, 1000)
    dialog.setValue(860)
    dialog.setLabelText(text)
    qtbot.wait(10)
    assert dialog.size() == original
    label = next(label for label in dialog.findChildren(QLabel) if label.toolTip() == text)
    assert label.text() != text and "\n" not in label.text()
    assert label.textFormat() == Qt.TextFormat.PlainText
    assert not label.wordWrap()
    assert dialog.value() == 860
    canceled = []
    dialog.canceled.connect(lambda: canceled.append(True))
    next(button for button in dialog.findChildren(QPushButton) if button.text() == "取消").click()
    assert canceled


def accept_active():
    dialog = QApplication.activeModalWidget()
    try:
        assert isinstance(dialog, QDialog)
        assert dialog.minimumSize() == dialog.maximumSize()
    finally:
        if dialog:
            dialog.accept()


def test_input_helpers_preserve_result_contract(qtbot):
    QTimer.singleShot(30, accept_active)
    assert FixedInputDialog.getText(None, "名称", "名称：", text="new.txt") == ("new.txt", True)
    QTimer.singleShot(30, accept_active)
    assert FixedInputDialog.getItem(None, "备份", "选择：", ["one", "two"], 1, False) == (
        "two",
        True,
    )


def test_message_question_preserves_buttons_and_result(qtbot):
    def answer():
        box = QApplication.activeModalWidget()
        assert isinstance(box, FixedMessageBox)
        assert box.minimumSize() == box.maximumSize()
        box.button(FixedMessageBox.StandardButton.No).click()

    QTimer.singleShot(30, answer)
    result = FixedMessageBox.question(
        None,
        "确认",
        "继续？",
        FixedMessageBox.StandardButton.Yes | FixedMessageBox.StandardButton.No,
        FixedMessageBox.StandardButton.No,
    )
    assert result == FixedMessageBox.StandardButton.No


def test_message_box_fits_backup_error_after_layout(qtbot):
    text = "文件夹内没有 iOS 备份（需要 Info.plist 和 Manifest.plist）"
    box = FixedMessageBox(FixedMessageBox.Icon.Warning, "无法打开备份", "")
    box.setStyle(QStyleFactory.create("Windows"))
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    qtbot.addWidget(box)
    box.show()
    qtbot.waitExposed(box)
    label = box.findChild(QLabel, "qt_msgbox_label")
    if label.wordWrap():
        assert label.heightForWidth(label.width()) <= label.height()
    else:
        assert label.fontMetrics().horizontalAdvance(text) <= label.width()
    assert box.minimumSize() == box.maximumSize() == box.size()


@pytest.mark.parametrize("method", ["getExistingDirectory", "getOpenFileNames", "getSaveFileName"])
def test_file_helpers_are_fixed_widget_dialogs(qtbot, tmp_path, method):
    target = tmp_path / "selected.txt"
    target.write_text("sample")

    def choose():
        dialog = QApplication.activeModalWidget()
        assert isinstance(dialog, FixedFileDialog)
        assert dialog.testOption(FixedFileDialog.Option.DontUseNativeDialog)
        assert dialog.minimumSize() == dialog.maximumSize()
        if method == "getExistingDirectory":
            dialog.setDirectory(str(tmp_path))
        else:
            edit = dialog.findChild(QLineEdit, "fileNameEdit")
            edit.setText(str(target if method == "getOpenFileNames" else tmp_path / "new.txt"))
        dialog.accept()

    QTimer.singleShot(40, choose)
    directory = str(tmp_path)
    value = getattr(FixedFileDialog, method)(None, "选择", directory)
    if method == "getExistingDirectory":
        assert value == str(tmp_path)
    elif method == "getOpenFileNames":
        assert value[0] == [str(target)]
    else:
        assert value[0] == str(tmp_path / "new.txt")


class TransitionSpy(QObject):
    def __init__(self):
        super().__init__()
        self.events = []

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Hide, QEvent.Type.WindowStateChange):
            self.events.append(event.type())
        return False


@pytest.mark.parametrize("kind", ["password", "progress", "message", "input", "file"])
def test_title_bar_has_only_close_and_no_transition_on_double_click(qtbot, kind):
    dialog = make_dialog(kind)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    assert dialog.windowFlags() & Qt.WindowType.FramelessWindowHint
    bar = dialog.findChild(QWidget, "dialogTitleBar")
    assert bar and bar.isVisible()
    buttons = bar.findChildren(QToolButton)
    assert len(buttons) == 1
    assert buttons[0].accessibleName() == bar._language_context.translate("Close")
    spy = TransitionSpy()
    dialog.installEventFilter(spy)
    size = dialog.size()
    qtbot.mouseDClick(bar, Qt.MouseButton.LeftButton)
    dialog.showMaximized()
    dialog.showFullScreen()
    QApplication.sendEvent(dialog, QEvent(QEvent.Type.LayoutRequest))
    qtbot.wait(20)
    assert dialog.isVisible() and dialog.size() == size
    assert dialog.minimumSize() == dialog.maximumSize() == size
    assert not spy.events
    if kind == "progress":
        status = next(
            label for label in dialog.findChildren(QLabel) if label.toolTip() == "建立备份索引…"
        )
        assert bar.geometry().bottom() < status.geometry().top()


def test_title_bar_close_cancels_progress_and_content_stays_below_title(qtbot):
    dialog = FixedProgressDialog("建立备份索引…", "取消", 0, 100)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    title = dialog.findChild(QWidget, "dialogTitleBar")
    status = next(
        label for label in dialog.findChildren(QLabel) if label.toolTip() == "建立备份索引…"
    )
    progress = dialog.findChild(QProgressBar)
    cancel = dialog.findChild(QPushButton)
    assert title.geometry().bottom() < status.geometry().top()
    assert status.geometry().bottom() < progress.geometry().top()
    assert progress.geometry().bottom() < cancel.geometry().top()
    canceled = []
    dialog.canceled.connect(lambda: canceled.append(True))
    qtbot.mouseClick(title.findChild(QToolButton), Qt.MouseButton.LeftButton)
    assert canceled and not dialog.isVisible()


def test_title_drag_uses_system_move_without_geometry_workaround(qtbot, monkeypatch):
    dialog = PasswordDialog(None)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitExposed(dialog)
    moved = []

    class Handle:
        def startSystemMove(self):
            moved.append(True)

    monkeypatch.setattr(dialog, "windowHandle", lambda: Handle())
    qtbot.mouseClick(dialog.findChild(QWidget, "dialogTitleBar"), Qt.MouseButton.LeftButton)
    assert moved == [True]


@pytest.mark.parametrize("mode", ["show", "exec"])
@pytest.mark.parametrize("via_escape", [False, True])
def test_automatic_ok_message_can_close_from_title_or_escape(qtbot, mode, via_escape):
    dialog = FixedMessageBox(FixedMessageBox.Icon.Information, "关于", "程序说明")
    qtbot.addWidget(dialog)
    assert not dialog.buttons()

    def dismiss():
        if via_escape:
            qtbot.keyClick(dialog, Qt.Key.Key_Escape)
        else:
            qtbot.mouseClick(dialog._title_bar.close_button, Qt.MouseButton.LeftButton)
        assert not dialog.isVisible()

    if mode == "exec":
        QTimer.singleShot(0, dismiss)
        assert dialog.exec() == FixedMessageBox.StandardButton.Ok
    else:
        dialog.show()
        qtbot.waitExposed(dialog)
        dismiss()
    assert dialog.standardButtons() == FixedMessageBox.StandardButton.Ok
    assert dialog.clickedButton() == dialog.button(FixedMessageBox.StandardButton.Ok)


@pytest.mark.parametrize("via_escape", [False, True])
def test_progress_title_close_and_escape_cancel_once(qtbot, via_escape):
    dialog = FixedProgressDialog("建立备份索引…", "取消", 0, 100)
    qtbot.addWidget(dialog)
    cancellations = []
    dialog.canceled.connect(lambda: cancellations.append(True))
    dialog.show()
    qtbot.waitExposed(dialog)
    if via_escape:
        qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    else:
        qtbot.mouseClick(dialog._title_bar.close_button, Qt.MouseButton.LeftButton)
    assert not dialog.isVisible() and cancellations == [True]
    dialog.reject()
    assert cancellations == [True]


def test_custom_message_choices_do_not_gain_ok_or_implicit_accept(qtbot):
    dialog = FixedMessageBox(title="选择操作", text="请选择")
    qtbot.addWidget(dialog)
    accept = dialog.addButton("执行", FixedMessageBox.ButtonRole.AcceptRole)
    destructive = dialog.addButton("丢弃", FixedMessageBox.ButtonRole.DestructiveRole)
    dialog.show()
    qtbot.waitExposed(dialog)
    assert dialog.buttons() == [accept, destructive]
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    assert dialog.isVisible() and dialog.clickedButton() is None
    dialog._title_bar.close_button.click()
    assert dialog.isVisible() and dialog.clickedButton() is None
    dialog.done(0)


def test_progress_noncancellable_phase_stays_visible_until_finished(qtbot):
    dialog = FixedProgressDialog('应用更改', '取消', 0, 0)
    qtbot.addWidget(dialog)
    cancelled = []
    dialog.canceled.connect(lambda: cancelled.append(True))
    dialog.show()
    dialog.set_cancellable(False)
    assert not dialog._cancel_button.isEnabled() and not dialog._title_bar.close_button.isEnabled()
    qtbot.keyClick(dialog, Qt.Key.Key_Escape)
    dialog.close()
    assert dialog.isVisible() and not cancelled
    dialog.finish()
    assert not dialog.isVisible() and not cancelled
