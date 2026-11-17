import math
import plistlib
from datetime import datetime

import pytest
from PySide6.QtCore import QDate, Qt, QTime
from PySide6.QtGui import QTextCharFormat, QTextCursor

from planbak.plist_editor import PlistDocument, PlistEditor, PlistNode, ValueDialog
from planbak.plist_xml import PlistXmlError, parse_xml


def editor(qtbot, value, fmt=plistlib.FMT_BINARY):
    saved = []
    w = PlistEditor(
        PlistDocument(plistlib.dumps(value, fmt=fmt, sort_keys=False)), "prefs.plist", saved.append
    )
    w.language_context.set_language("zh_CN")
    qtbot.addWidget(w, before_close_func=lambda x: x.discard_and_close())
    w.show()
    return w, saved


@pytest.mark.parametrize("fmt", [plistlib.FMT_XML, plistlib.FMT_BINARY])
def test_xml_roundtrip_save_and_format(qtbot, fmt):
    w, saved = editor(qtbot, {"name": "before", "flag": True}, fmt)
    w.switch_mode(1)
    assert w.pages.currentIndex() == 1 and not w.dirty
    text = w.xml.code.toPlainText().replace("before", "after")
    w.xml.code.setPlainText(text)
    assert w.dirty
    assert w.save_document()
    assert not w.dirty and not w.save_button.isEnabled()
    assert plistlib.loads(saved[-1]) == {"name": "after", "flag": True}
    assert saved[-1].startswith(b"bplist00") == (fmt == plistlib.FMT_BINARY)
    w.switch_mode(0)
    assert w.document.root.to_value()["name"] == "after"
    assert not w.dirty


def test_xml_invalid_text_blocks_switch_save_and_preserves_buffer(qtbot):
    w, saved = editor(qtbot, {"a": 1})
    w.switch_mode(1)
    invalid = "<plist>\n<dict><key>a</key><integer>bad</integer></dict></plist>"
    w.xml.code.setPlainText(invalid)
    w.switch_mode(0)
    assert w.pages.currentIndex() == 1 and w.pages.currentIndex() == 1
    assert w.xml.code.toPlainText() == invalid
    assert "第 2 行" in w.xml.error.text()
    assert w.xml.code.textCursor().blockNumber() == 1
    assert not w.save_document() and not saved and w.dirty
    assert w.document.root.to_value() == {"a": 1}


@pytest.mark.parametrize(
    "body",
    [
        "<dict><key>a</key><true/><key>a</key><false/></dict>",
        "<dict><key>a</key></dict>",
        "<array><key>a</key></array>",
        "<dict/><array/>",
        "<data>@@@</data>",
        "<true>text</true>",
        "<string><true/></string>",
    ],
)
def test_xml_strict_validation(body):
    with pytest.raises(PlistXmlError) as exc:
        parse_xml('<plist version="1.0">\n' + body + "\n</plist>")
    assert exc.value.line >= 1 and exc.value.column >= 1


def test_xml_entities_unicode_and_duplicate_keys():
    assert parse_xml("<plist><dict><key>中文</key><string>😀 &amp; x</string></dict></plist>") == {
        "中文": "😀 & x"
    }
    with pytest.raises(PlistXmlError, match="Duplicate dictionary key"):
        parse_xml("<plist><dict><key>a&amp;b</key><true/><key>a&#38;b</key><false/></dict></plist>")
    with pytest.raises(PlistXmlError, match="entities"):
        parse_xml(
            '<!DOCTYPE plist [<!ENTITY hidden SYSTEM "file:///etc/passwd">]><plist><string>&hidden;</string></plist>'
        )


def test_xml_error_retains_parameters_independently_of_display_language():
    from planbak.i18n import ENGLISH, SIMPLIFIED_CHINESE, LanguageContext

    with pytest.raises(PlistXmlError) as caught:
        parse_xml("<plist><dict><key>名字：{x}</key><true/><key>名字：{x}</key><false/></dict></plist>")
    error = caught.value
    assert error.source == "Duplicate dictionary key: {key}"
    assert error.values == {"key": "名字：{x}"}
    assert "重复键：名字：{x}" in error.localized(LanguageContext(SIMPLIFIED_CHINESE).translate)
    assert "Duplicate dictionary key: 名字：{x}" in error.localized(LanguageContext(ENGLISH).translate)


def test_xml_parser_syntax_error_is_localized():
    from planbak.i18n import ENGLISH, SIMPLIFIED_CHINESE, LanguageContext

    with pytest.raises(PlistXmlError) as caught:
        parse_xml("<plist><array></plist>")
    assert "标签不匹配" in caught.value.localized(LanguageContext(SIMPLIFIED_CHINESE).translate)
    assert "mismatched tag" in caught.value.localized(LanguageContext(ENGLISH).translate)


@pytest.mark.parametrize(
    "value", [plistlib.UID(0), None, datetime(2025, 1, 1, microsecond=123456), "contains\x00null"]
)
def test_xml_unrepresentable_values_disabled(qtbot, value):
    w, saved = editor(qtbot, {"value": value})
    assert not w.xml_button.isEnabled()
    assert w.xml_reason
    w.switch_mode(1)
    assert w.pages.currentIndex() == 0 and w.pages.currentIndex() == 0
    assert not w.dirty and not saved


def test_xml_search_replace_undo_and_switch_history(qtbot):
    w, saved = editor(qtbot, {"a": "Hello hello HELLO"}, plistlib.FMT_XML)
    w.switch_mode(1)
    pane = w.xml
    pane.find_field.setText("hello")
    pane.replace_field.setText("world")
    pane.replace_all()
    assert "world world world" in pane.code.toPlainText()
    pane.code.undo()
    assert "Hello hello HELLO" in pane.code.toPlainText() and not w.dirty
    pane.case_sensitive.setChecked(True)
    pane.replace_all()
    assert "Hello world HELLO" in pane.code.toPlainText()
    position = pane.code.textCursor().position()
    w.switch_mode(0)
    w.switch_mode(1)
    assert "Hello world HELLO" in pane.code.toPlainText()
    assert pane.code.textCursor().position() == position
    pane.code.undo()
    assert "Hello hello HELLO" in pane.code.toPlainText()
    assert not w.dirty
    assert w.save_document() and not saved


def test_xml_tree_edits_and_format_only_dirty(qtbot):
    w, saved = editor(qtbot, {"a": "before"}, plistlib.FMT_XML)
    w.document.root.children[0].value = "tree edit"
    w.rebuild()
    w.switch_mode(1)
    assert w.dirty
    w.xml.code.setPlainText(w.xml.code.toPlainText().replace("tree edit", "before"))
    assert not w.dirty
    w.switch_mode(0)
    assert not w.dirty and w.document.root.to_value() == {"a": "before"}
    w.switch_mode(1)
    text = w.xml.code.toPlainText().replace("<dict>", "<!-- note -->\n<dict>")
    w.xml.code.setPlainText(text)
    w.switch_mode(0)
    assert w.dirty
    assert w.save_document() and b"<!-- note -->" in saved[-1]
    assert not w.dirty


def test_multiselect_delete_ancestor_and_array_indices(qtbot, monkeypatch):
    from planbak.plist_editor import FixedMessageBox

    w, saved = editor(qtbot, {"nested": {"child": 1}, "array": ["a", "b", "c"]})
    root = w.tree.topLevelItem(0)
    nested, array = root.child(0), root.child(1)
    nested.setExpanded(True)
    array.setExpanded(True)
    w.tree.clearSelection()
    for item in (root, nested, nested.child(0), array.child(0), array.child(2)):
        item.setSelected(True)
    assert w.selected_node() is None and not w.edit_button.isEnabled()
    assert len(w.deletion_roots()) == 3
    prompts = []
    monkeypatch.setattr(
        FixedMessageBox,
        "question",
        lambda *a: prompts.append(a) or FixedMessageBox.StandardButton.Yes,
    )
    w.delete_node()
    assert w.document.root.to_value() == {"array": ["b"]}
    assert len(prompts) == 1 and "4 个节点" in prompts[0][2]
    item = w.tree.topLevelItem(0).child(0)
    assert item.child(0).text(0) == "0"


def test_tree_context_selection_and_visible_select_all(qtbot, monkeypatch):
    import planbak.plist_editor as module

    w, saved = editor(qtbot, {"a": {"hidden": 1}, "b": 2, "c": 3})
    root = w.tree.topLevelItem(0)
    w.select_visible()
    assert len(w.tree.selectedItems()) == 4
    assert root.child(0).childCount() == 0
    observed = []

    class Menu(module.QMenu):
        def exec(self, *args):
            observed.append([(a.text(), a.isEnabled()) for a in self.actions()])
            return None

    monkeypatch.setattr(module, "QMenu", Menu)
    w.context_menu(w.tree.visualItemRect(root.child(1)).center())
    assert len(w.tree.selectedItems()) == 4
    assert observed[-1] == [("编辑", False), ("删除", True)]
    w.tree.clearSelection()
    root.setSelected(True)
    w.context_menu(w.tree.visualItemRect(root).center())
    assert observed[-1] == [("编辑", True), ("删除", False)]
    w.context_menu(w.tree.visualItemRect(root.child(2)).center())
    assert len(w.tree.selectedItems()) == 1
    assert observed[-1] == [("编辑", True), ("删除", True)]


def test_boolean_date_and_numeric_controls(qtbot):
    node = PlistNode("Boolean", "flag", True)
    w = ValueDialog(node, "Dictionary", [node], True)
    qtbot.addWidget(w)
    assert w.boolean_field.currentData() is True
    w.boolean_field.setCurrentIndex(1)
    w.accept()
    assert w.result_node.value is False and type(w.result_node.value) is bool
    date = PlistNode("Date", "when", datetime(2024, 2, 29, 23, 59, 58, 123456))
    d = ValueDialog(date, "Dictionary", [date], True)
    qtbot.addWidget(d)
    assert d.date_field.date.date() == QDate(2024, 2, 29)
    assert d.date_field.fraction.text() == "123456"
    d.date_field.time.setTime(QTime(1, 2, 3))
    d.accept()
    assert d.result_node.value == datetime(2024, 2, 29, 1, 2, 3, 123456)
    d = ValueDialog(PlistNode("Date", "", datetime(2024, 1, 1)), None, [], False)
    qtbot.addWidget(d)
    assert not d.date_field.fraction.isEnabled()
    for kind, text, expected in [
        ("Integer", str(2**64 - 1), 2**64 - 1),
        ("Integer", str(-(2**63)), -(2**63)),
        ("UID", str(2**64 - 1), plistlib.UID(2**64 - 1)),
        ("Real", "1.2345678901234567e-100", 1.2345678901234567e-100),
    ]:
        n = PlistNode(kind, "", 0 if kind != "UID" else plistlib.UID(0))
        d = ValueDialog(n, None, [], True)
        qtbot.addWidget(d)
        d.number_field.setText(text)
        d.accept()
        assert d.result_node.value == expected
    d = ValueDialog(PlistNode("Real", "", float("nan")), None, [], True)
    qtbot.addWidget(d)
    d.accept()
    assert math.isnan(d.result_node.value)


def test_date_invalid_fraction_and_string_whitespace(qtbot):
    d = ValueDialog(PlistNode("Date", "", datetime(2024, 1, 1)), None, [], True)
    qtbot.addWidget(d)
    d.date_field.fraction.setText("12")
    d.accept()
    assert d.result_node is None
    d = ValueDialog(PlistNode("String", "", ""), None, [], False)
    qtbot.addWidget(d)
    d.value_field.setPlainText("  中文😀\nsecond\t ")
    d.accept()
    assert d.result_node.value == "  中文😀\nsecond\t "


def test_nan_xml_undo_and_type_switch_no_false_dirty(qtbot):
    w, saved = editor(qtbot, {"nan": float("nan"), "a": 1})
    w.switch_mode(1)
    original = w.xml.code.toPlainText()
    w.xml.code.setPlainText(original.replace("<integer>1</integer>", "<integer>2</integer>"))
    w.switch_mode(0)
    assert w.dirty
    w.switch_mode(1)
    w.xml.code.setPlainText(original)
    w.switch_mode(0)
    assert not w.dirty
    assert not saved


def test_boolean_control_keeps_single_row_height(qtbot):
    d = ValueDialog(PlistNode("Boolean", "enabled", True), "Dictionary", [], True)
    qtbot.addWidget(d)
    d.show()
    qtbot.wait(10)
    assert d.boolean_field.height() <= d.type_field.height() + 4
    assert d.values.height() > d.boolean_field.height() * 3
    assert d.boolean_field.y() == 0
    size = d.size()
    d.type_field.setCurrentText("String")
    d.type_field.setCurrentText("Boolean")
    assert d.size() == size
    assert d.boolean_field.height() <= d.type_field.height() + 4


def test_editor_toolbar_switch_and_undo_redo(qtbot):
    w, saved = editor(qtbot, {"value": "before"})
    assert not hasattr(w, "mode")
    labels = [action.text() for action in w.tree_actions.actions() if action.text()]
    assert labels == ["添加", "编辑", "删除", "XML 编辑器"]
    qtbot.mouseClick(w.xml_button, Qt.MouseButton.LeftButton)
    assert w.pages.currentIndex() == 1 and w.tree_actions.isHidden()
    assert [
        button.text()
        for button in (w.xml.undo_button, w.xml.redo_button, w.xml.search_button, w.xml.tree_button)
    ] == ["撤销", "重做", "搜索", "树形编辑器"]
    assert not w.xml.undo_button.isEnabled() and not w.xml.redo_button.isEnabled()
    w.xml.code.moveCursor(QTextCursor.MoveOperation.End)
    w.xml.code.insertPlainText("<!-- edit -->")
    assert w.xml.undo_button.isEnabled() and w.dirty
    qtbot.mouseClick(w.xml.undo_button, Qt.MouseButton.LeftButton)
    assert not w.dirty and w.xml.redo_button.isEnabled()
    qtbot.mouseClick(w.xml.redo_button, Qt.MouseButton.LeftButton)
    assert w.dirty
    qtbot.mouseClick(w.xml.tree_button, Qt.MouseButton.LeftButton)
    assert w.pages.currentIndex() == 0 and not w.tree_actions.isHidden()


def test_detached_search_navigation_highlights_and_shortcut(qtbot):
    w, saved = editor(qtbot, {"value": "hello HELLO hello"})
    w.xml_button.click()
    w.activateWindow()
    w.xml.code.setFocus()
    qtbot.wait(10)
    qtbot.keyClick(w.xml.code, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    pane = w.xml
    dialog = pane.search_dialog
    assert dialog is not None and dialog.isVisible() and dialog.isWindow()
    qtbot.waitUntil(lambda: dialog.find_field.hasFocus())
    dialog.find_field.setText("hello")
    assert len(pane.code.search_selections) >= 3
    qtbot.mouseClick(dialog.next_button, Qt.MouseButton.LeftButton)
    first = pane.code.textCursor().selectionStart()
    assert dialog.isVisible() and dialog.find_field.hasFocus()
    assert pane.code.textCursor().selectedText().lower() == "hello"
    qtbot.mouseClick(dialog.next_button, Qt.MouseButton.LeftButton)
    assert pane.code.textCursor().selectionStart() > first
    qtbot.mouseClick(dialog.previous_button, Qt.MouseButton.LeftButton)
    assert pane.code.textCursor().selectionStart() == first and dialog.isVisible()
    assert pane.code.search_selections[-1].format.hasProperty(
        QTextCharFormat.Property.BackgroundBrush
    )
    qtbot.keyClick(dialog.find_field, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert pane.search_dialog is dialog and dialog.isVisible()
    dialog.close()
    assert not dialog.isVisible() and not pane.code.search_selections
    pane.search_button.click()
    assert pane.search_dialog is dialog and dialog.isVisible()
    pane.tree_button.click()
    assert not dialog.isVisible() and w.pages.currentIndex() == 0


def test_search_close_lifecycle_and_replacement(qtbot):
    w, saved = editor(qtbot, {"value": "one one"})
    w.xml_button.click()
    pane = w.xml
    pane.show_search()
    dialog = pane.search_dialog
    pane.find_field.setText("one")
    pane.replace_field.setText("two")
    pane.replace_all()
    assert dialog.isVisible() and "two two" in pane.code.toPlainText()
    pane.code.undo()
    assert "one one" in pane.code.toPlainText() and not w.dirty
    w.close()
    assert not dialog.isVisible()


def test_ctrl_s_from_search_keeps_editor_save_shortcut(qtbot):
    w, saved = editor(qtbot, {"a": "before"}, plistlib.FMT_XML)
    w.xml_button.click()
    w.xml.code.moveCursor(QTextCursor.MoveOperation.End)
    w.xml.code.insertPlainText("<!-- saved -->")
    w.xml.show_search()
    qtbot.wait(10)
    qtbot.keyClick(w.xml.find_field, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert len(saved) == 1 and b"<!-- saved -->" in saved[0]
    assert w.xml.search_dialog.isVisible() and not w.dirty
