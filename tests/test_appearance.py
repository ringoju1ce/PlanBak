import plistlib

import pytest
from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QColor, QPalette, QTextCursor
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout
from test_sqlite_viewer import make_database
from test_ui import window as window

from planbak.app import PasswordDialog
from planbak.appearance import configure_system_appearance, marked_background, xml_colors
from planbak.dialogs import FixedDialog
from planbak.hex_viewer import HexViewer
from planbak.item_style import ItemViewStyle
from planbak.plist_editor import PlistDocument, PlistEditor
from planbak.sqlite_database import DatabaseSnapshot
from planbak.sqlite_viewer import SQLiteViewer


@pytest.fixture
def restore_palette(qapp):
    original = QPalette(qapp.palette())
    yield
    qapp.setPalette(original)
    qapp.processEvents()


def palette(dark):
    result = QPalette()
    for role, color in {
        QPalette.ColorRole.Window: "#242424" if dark else "#fafafa",
        QPalette.ColorRole.Base: "#1e1e1e" if dark else "#ffffff",
        QPalette.ColorRole.AlternateBase: "#303030" if dark else "#f0f0f0",
        QPalette.ColorRole.Text: "#f0f0f0" if dark else "#202020",
        QPalette.ColorRole.WindowText: "#f0f0f0" if dark else "#202020",
        QPalette.ColorRole.Button: "#303030" if dark else "#eeeeee",
        QPalette.ColorRole.ButtonText: "#f0f0f0" if dark else "#202020",
        QPalette.ColorRole.Midlight: "#454545" if dark else "#d5d5d5",
        QPalette.ColorRole.Highlight: "#326abb" if dark else "#235eaa",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: "#303030" if dark else "#ffffdd",
        QPalette.ColorRole.ToolTipText: "#f0f0f0" if dark else "#202020",
    }.items():
        result.setColor(role, QColor(color))
    result.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#999999"))
    return result


def test_startup_uses_platform_scheme_and_never_installs_explicit_palette(qapp, monkeypatch):
    calls = []

    class Hints:
        def unsetColorScheme(self):
            calls.append("follow-system")

    class Application:
        def setStyle(self, style):
            assert isinstance(style, ItemViewStyle)
            calls.append("fusion")
            style.deleteLater()

        def styleHints(self):
            return Hints()

        def setPalette(self, value):
            pytest.fail("Explicit application palette would mask future system changes")

    import sys

    if sys.platform.startswith("linux"):
        monkeypatch.setattr(
            "planbak.portal_appearance.PortalAppearance", lambda app: calls.append("portal")
        )
    configure_system_appearance(Application())
    assert calls == ["fusion", "follow-system"] + (
        ["portal"] if sys.platform.startswith("linux") else []
    )


def contrast(a, b):
    def luminance(color):
        values = [x / 255 for x in color.getRgb()[:3]]
        values = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in values]
        return sum(x * weight for x, weight in zip(values, (0.2126, 0.7152, 0.0722)))

    x, y = sorted([luminance(a), luminance(b)])
    return (y + 0.05) / (x + 0.05)


@pytest.mark.parametrize("dark", [False, True])
def test_custom_xml_colors_have_readable_contrast(dark):
    p = palette(dark)
    for color in xml_colors(p):
        assert contrast(QColor(color), p.base().color()) >= 4.5
    for error in (False, True):
        assert contrast(marked_background(p, error=error), p.text().color()) >= 4.5


def test_open_views_follow_palette_without_modifying_data(
    request, qtbot, tmp_path, restore_palette
):
    w = request.getfixturevalue("window")
    password = PasswordDialog(w)
    qtbot.addWidget(password)
    password.show()
    editor = PlistEditor(
        PlistDocument(plistlib.dumps({"value": "original"})), "theme.plist", lambda _: True, w
    )
    qtbot.addWidget(editor)
    editor.show()
    editor.switch_mode(1)
    data = tmp_path / "hex.bin"
    data.write_bytes(b"abc")
    hex_viewer = HexViewer(data, "hex.bin", w)
    qtbot.addWidget(hex_viewer)
    hex_viewer.show()
    hex_viewer.table.setCurrentIndex(hex_viewer.model.index(0, 1))
    hex_viewer.match = (0, 1)
    hex_viewer.model.match = hex_viewer.match
    database = tmp_path / "sample.db"
    make_database(database)
    sql_viewer = SQLiteViewer(DatabaseSnapshot(tmp_path, database, b""), "sample.db", w)
    qtbot.addWidget(sql_viewer)
    sql_viewer.show()
    qtbot.waitUntil(lambda: sql_viewer.task is None)
    sql_viewer.tree.setCurrentItem(sql_viewer.tree.topLevelItem(0))
    qtbot.waitUntil(lambda: sql_viewer.task is None)
    rows = sql_viewer.model.result.rows
    text = editor.xml.code.toPlainText()
    owner, preferred = w.panes.owner, w.panes.preferred_view
    for dark in (True, False):
        p = palette(dark)
        QApplication.setPalette(p)
        expected_title = p.base().color() if dark else p.window().color().darker(106)
        qtbot.waitUntil(lambda: password._title_bar.palette().window().color() == expected_title)
        qtbot.wait(10)
        assert password._title_bar.title.palette().windowText().color() == p.windowText().color()
        assert (
            password._title_bar.close_button.palette().buttonText().color()
            == p.buttonText().color()
        )
        for header in (
            w.table.horizontalHeader(),
            sql_viewer.table.horizontalHeader(),
            sql_viewer.tree.header(),
        ):
            assert header.palette().button().color() == p.button().color()
            assert header.palette().buttonText().color() == p.buttonText().color()
        assert editor.xml.code.gutter.palette().window().color() == p.alternateBase().color()
        for widget in (
            w.table,
            w.grid,
            w.directory_tree,
            editor.tree,
            editor.xml.code,
            password.password,
            sql_viewer.table,
            sql_viewer.tree,
            hex_viewer.table,
        ):
            assert widget.palette().base().color() == p.base().color()
        assert (
            hex_viewer.model.data(
                hex_viewer.model.index(0, 1), Qt.ItemDataRole.BackgroundRole
            ).color()
            == p.highlight().color()
        )
        assert sql_viewer.model.result.rows == rows
        assert editor.xml.code.toPlainText() == text and not editor.dirty
        assert not w.session.dirty
        assert (w.panes.owner, w.panes.preferred_view) == (owner, preferred)
        assert hex_viewer.selected_bytes() == b"a"
    hex_viewer.close()
    sql_viewer.close()
    editor.close()
    password.reject()


def test_xml_palette_change_keeps_search_error_undo_and_unsaved_text(qtbot, restore_palette):
    editor = PlistEditor(
        PlistDocument(plistlib.dumps({"value": "original"})), "theme.plist", lambda _: True
    )
    qtbot.addWidget(editor, before_close_func=lambda w: w.discard_and_close())
    editor.show()
    editor.switch_mode(1)
    pane = editor.xml
    code = pane.code
    cursor = code.textCursor()
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertText("\n<!-- modified -->")
    code.setTextCursor(cursor)
    pane.show_search()
    pane.find_field.setText("value")
    pane.find()
    code.show_error(3, 1)
    pane.refresh_matches()
    original = code.toPlainText()
    position = code.textCursor().position()
    undo_available = code.document().isUndoAvailable()
    assert editor.dirty and code.error_selections and code.search_selections
    colors = []
    for dark in (True, False):
        QApplication.setPalette(palette(dark))
        qtbot.wait(10)
        assert code.toPlainText() == original and editor.dirty
        assert code.textCursor().position() == position
        assert code.document().isUndoAvailable() == undo_available
        assert code.error_selections and code.search_selections
        formats = code.document().findBlockByNumber(1).layout().formats()
        colors.append([f.format.foreground().color().name() for f in formats])
        assert code.gutter.palette().window().color() == code.palette().alternateBase().color()
    assert colors[0] != colors[1]
    pane.hide_search()
    code.undo()
    assert "modified" not in code.toPlainText()
    editor.discard_and_close()


def test_close_hover_and_pressed_paint_follow_theme_changes(
    qtbot, qapp, restore_palette
):
    qapp.setPalette(palette(False))
    dialog = FixedDialog()
    qtbot.addWidget(dialog)
    dialog.setWindowTitle("主题切换")
    QVBoxLayout(dialog).addWidget(QLabel("内容"))
    dialog.lock_size(QSize(480, 220))
    dialog.show()
    qtbot.waitExposed(dialog)
    button = dialog._title_bar.close_button
    dialog.activateWindow()
    qtbot.waitUntil(dialog.isActiveWindow)
    qtbot.mouseMove(dialog, QPoint(30, 150))
    qtbot.mouseMove(button, QPoint(8, 8))
    for dark in (False, True, False, True):
        p = palette(dark)
        p.setColor(QPalette.ColorRole.Mid, QColor("#383838" if dark else "#b0b0b0"))
        qapp.setPalette(p)
        qapp.processEvents()
        assert button.underMouse()
        # Inspect the rendered hover background, not just the updated palette:
        # before the fix these disagree after a theme change.
        assert button.grab().toImage().pixelColor(8, 8) == p.midlight().color()
        assert dialog._title_bar.palette().window().color() != dialog.palette().window().color()
        assert dialog.palette().base().color() == p.base().color()
        assert (
            contrast(
                dialog._title_bar.palette().window().color(),
                dialog._title_bar.title.palette().windowText().color(),
            )
            >= 4.5
        )
        qtbot.mousePress(button, Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
        assert button.grab().toImage().pixelColor(8, 8) == p.mid().color()
        qtbot.mouseRelease(button, Qt.MouseButton.LeftButton, pos=QPoint(-5, -5))
        assert dialog.isVisible()
        qtbot.mouseMove(dialog, QPoint(30, 150))
        qtbot.mouseMove(button, QPoint(8, 8))
        assert button.grab().toImage().pixelColor(8, 8) == p.midlight().color()
    dialog.close()
