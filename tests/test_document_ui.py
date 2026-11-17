import plistlib
import sqlite3

import pytest
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QPlainTextEdit

from planbak.document_ui import DocumentStatus, DocumentToolBar, document_buttons
from planbak.hex_viewer import HexViewer
from planbak.plist_editor import PlistDocument, PlistEditor
from planbak.sqlite_database import DatabaseSnapshot
from planbak.sqlite_viewer import SQLiteViewer


@pytest.mark.parametrize("key", [Qt.Key.Key_Return, Qt.Key.Key_Enter])
def test_hex_return_only_jumps_or_searches(qtbot, tmp_path, key):
    path = tmp_path / "bytes"
    path.write_bytes(b"x" * 4096 + b"NEEDLE" + b"x" * 4096)
    v = HexViewer(path, "bytes")
    qtbot.addWidget(v)
    v.show()
    v.activateWindow()
    qtbot.waitUntil(v.isActiveWindow)
    clicks = []
    for button in (v.previous_button, v.next_button, v.reload_button):
        button.clicked.connect(lambda: clicks.append(True))
    v.address.setFocus()
    v.address.setText("0x1000")
    qtbot.keyClick(v.address, key)
    assert v.cursor_offset == 4096 and v.offset == 4096
    assert v.isVisible() and not clicks
    v.search_mode.setCurrentIndex(1)
    v.search_input.setText("NEEDLE")
    v.search_input.setFocus()
    qtbot.keyClick(v.search_input, key)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.match == (4096, 6) and not clicks
    assert v.selected_bytes() == b"NEEDLE"
    v.close()


def test_sql_sort_arrow_follows_successful_query_and_table_switch(qtbot, tmp_path):
    path = tmp_path / "sort.db"
    with sqlite3.connect(path) as db:
        for name in ("alpha", "beta"):
            db.execute(f"CREATE TABLE {name}(id INTEGER)")
            db.executemany(f"INSERT INTO {name} VALUES (?)", [(3,), (1,), (2,)])
    v = SQLiteViewer(DatabaseSnapshot(tmp_path, path, b""), "sort.db")
    qtbot.addWidget(v)
    v.show()
    qtbot.waitUntil(lambda: v.task is None)
    v.tree.setCurrentItem(v.tree.topLevelItem(0))
    qtbot.waitUntil(lambda: v.task is None)
    header = v.table.horizontalHeader()
    assert v.model.result.rows == [(3,), (1,), (2,)]
    assert not header.isSortIndicatorShown()
    point = QPoint(header.sectionSize(0) // 2, header.height() // 2)
    qtbot.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=point)
    qtbot.waitUntil(lambda: v.task is None)
    assert header.isSortIndicatorShown()
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    assert v.model.result.rows == [(1,), (2,), (3,)]
    qtbot.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=point)
    qtbot.waitUntil(lambda: v.task is None)
    assert header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
    assert v.model.result.rows == [(3,), (2,), (1,)]
    v.tree.setCurrentItem(v.tree.topLevelItem(1))
    qtbot.waitUntil(lambda: v.task is None)
    assert not header.isSortIndicatorShown()
    assert v.model.result.rows == [(3,), (1,), (2,)]
    v.sort_table(0, Qt.SortOrder.AscendingOrder)
    qtbot.waitUntil(lambda: v.task is None)
    v.code.setPlainText("SELECT id FROM beta ORDER BY id DESC")
    v.execute_sql()
    qtbot.waitUntil(lambda: v.task is None)
    assert not header.isSortIndicatorShown() and not header.sectionsClickable()
    assert v.model.result.rows == [(3,), (2,), (1,)]
    fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
    assert v.code.font().family() == fixed == v.structure.font().family()
    v.close()


def test_document_return_does_not_save_but_ctrl_s_does(qtbot):
    saved = []
    v = PlistEditor(PlistDocument(plistlib.dumps({"key": 1})), "x.plist", saved.append)
    qtbot.addWidget(v)
    v.show()
    v.activateWindow()
    v.switch_mode(1)
    v.xml.code.setPlainText(v.xml.code.toPlainText().replace("<integer>1", "<integer>2"))
    v.switch_mode(0)
    v.tree.setFocus()
    qtbot.waitUntil(v.isActiveWindow)
    qtbot.keyClick(v.tree, Qt.Key.Key_Return)
    assert not saved and v.dirty
    qtbot.keyClick(v.tree, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert len(saved) == 1
    v._skip_prompt = True
    v.close()


def test_status_long_plain_text_stays_bounded_and_details_are_copyable(qtbot):
    text = "<b>not markup</b>\n" + "完整错误" * 2000
    status = DocumentStatus("短消息", note="持续说明")
    qtbot.addWidget(status)
    status.resize(320, status.sizeHint().height())
    status.show()
    qtbot.waitExposed(status)
    height = status.height()
    status.setText(text)
    QApplication.processEvents()
    assert status.height() == height
    assert status.text() == text
    assert status.full_text() == text + "\n\n持续说明"
    assert "&lt;b&gt;" in status.toolTip()
    assert status.label.width() > 0
    assert status.label.fontMetrics().horizontalAdvance(status.label.text()) <= status.label.width()
    checked = []

    def inspect():
        dialog = QApplication.activeModalWidget()
        editor = dialog.findChild(QPlainTextEdit)
        assert editor.isReadOnly() and editor.toPlainText() == status.full_text()
        editor.selectAll()
        editor.copy()
        assert QApplication.clipboard().text() == status.full_text()
        checked.append(True)
        dialog.reject()

    QTimer.singleShot(0, inspect)
    status.show_details()
    assert checked
    status.close()


@pytest.mark.parametrize("language, expected", [("en", "Details…"), ("zh_CN", "详情…")])
def test_status_initial_controls_follow_parent_language(qtbot, language, expected):
    from PySide6.QtWidgets import QWidget

    from planbak.i18n import LanguageContext

    parent = QWidget()
    parent.language_context = LanguageContext(language, parent)
    qtbot.addWidget(parent)
    status = DocumentStatus(parent=parent)
    assert status.details_button.text() == expected


def test_toolbar_action_and_button_share_state_without_dialog_default(qtbot):
    toolbar = DocumentToolBar("测试工具栏")
    qtbot.addWidget(toolbar)
    fired = []
    button = toolbar.command("编辑", lambda: fired.append(True), hint="F2")
    action = button.defaultAction()
    assert "F2" in button.toolTip()
    action.setEnabled(False)
    assert not button.isEnabled()
    action.setEnabled(True)
    button.click()
    assert fired == [True]
    box = document_buttons(
        QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
    )
    qtbot.addWidget(box)
    assert all(not b.autoDefault() and not b.isDefault() for b in box.buttons())
