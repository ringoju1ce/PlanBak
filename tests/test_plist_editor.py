import plistlib
from datetime import datetime

import pytest
from PySide6.QtCore import Qt

from planbak.plist_editor import (
    FixedMessageBox,
    PlistDocument,
    PlistEditor,
    PlistNode,
    ValueDialog,
    parse_value,
)


@pytest.mark.parametrize("fmt", [plistlib.FMT_XML, plistlib.FMT_BINARY])
def test_plist_typed_roundtrip_and_format(fmt):
    value = {
        "string": "第一行\nsecond",
        "integer": 42,
        "real": 1.25,
        "boolean": True,
        "date": datetime(2025, 1, 2, 3, 4, 5),
        "data": b"\x00\xff",
        "array": [False, 7, {}],
    }
    if fmt == plistlib.FMT_BINARY:
        value["uid"] = plistlib.UID(3)
    doc = PlistDocument(plistlib.dumps(value, fmt=fmt, sort_keys=False))
    assert not doc.dirty
    doc.root.children[1].value = 43
    assert doc.dirty
    data = doc.serialize()
    assert data.startswith(b"bplist00") == (fmt == plistlib.FMT_BINARY)
    loaded = plistlib.loads(data)
    value["integer"] = 43
    assert loaded == value
    assert type(loaded["boolean"]) is bool
    assert type(loaded["integer"]) is int
    assert list(loaded) == list(value)
    doc.mark_saved()
    assert not doc.dirty


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("Integer", "1.2"),
        ("Integer", str(2**64)),
        ("Real", "nan"),
        ("Real", "inf"),
        ("Boolean", "yes"),
        ("Date", "yesterday"),
        ("UID", "-1"),
    ],
)
def test_plist_invalid_values(kind, text):
    with pytest.raises((ValueError, OverflowError)):
        parse_value(kind, text)


def test_plist_uid_archive_and_xml_precision():
    value = {
        "$archiver": "NSKeyedArchiver",
        "$objects": ["$null", {"reference": plistlib.UID(1)}],
        "$top": {"root": plistlib.UID(1)},
    }
    doc = PlistDocument(plistlib.dumps(value, fmt=plistlib.FMT_BINARY))
    assert plistlib.loads(doc.serialize()) == value
    doc = PlistDocument(plistlib.dumps({"date": datetime(2025, 1, 1)}))
    doc.root.children[0].value = datetime(2025, 1, 1, microsecond=1)
    with pytest.raises(ValueError, match="without loss"):
        doc.serialize()
    assert parse_value("Date", "2025-01-01T09:00:00+09:00") == datetime(2025, 1, 1)


def test_plist_value_dialog_validation(qtbot):
    a = PlistNode("Boolean", "a", True)
    b = PlistNode("Integer", "b", 1)
    dialog = ValueDialog(a, "Dictionary", [a, b], True)
    qtbot.addWidget(dialog)
    dialog.key_field.setText("b")
    dialog.accept()
    assert dialog.result_node is None
    assert dialog.error.text() == dialog.language_context.translate(
        "This dictionary already contains that key"
    )
    dialog.key_field.setText("renamed")
    dialog.boolean_field.setCurrentIndex(1)
    dialog.accept()
    assert dialog.result_node == PlistNode("Boolean", "renamed", False)
    assert a.value is True
    array_dialog = ValueDialog(b, "Array", [], False)
    qtbot.addWidget(array_dialog)
    assert not array_dialog.key_field.isEnabled()
    assert array_dialog.type_field.findText("UID") == -1
    array_dialog.type_field.setCurrentText("Integer")
    array_dialog.number_field.setText("abc")
    array_dialog.accept()
    assert array_dialog.result_node is None


def editor_widget(qtbot, value, save=lambda data: None):
    doc = PlistDocument(plistlib.dumps(value, fmt=plistlib.FMT_BINARY))
    editor = PlistEditor(doc, "test.plist", save)
    editor.language_context.set_language("zh_CN")
    qtbot.addWidget(editor, before_close_func=lambda w: w.discard_and_close())
    editor.show()
    return editor


def test_plist_lazy_tree_data_import_export_and_shortcut(qtbot, tmp_path, monkeypatch):
    from planbak.plist_editor import FixedFileDialog

    saved = []
    editor = editor_widget(qtbot, {"array": [{"nested": 1}], "blob": b"old"}, saved.append)
    root = editor.tree.topLevelItem(0)
    array = root.child(0)
    assert array.data(0, Qt.ItemDataRole.UserRole) is editor.document.root.children[0]
    assert array.childCount() == 0
    array.setExpanded(True)
    assert array.childCount() == 1 and array.child(0).childCount() == 0
    blob = root.child(1)
    editor.tree.clearSelection()
    editor.tree.setCurrentItem(blob)
    blob.setSelected(True)
    assert editor.selected_node() is editor.document.root.children[1]
    path = tmp_path / "bytes.bin"
    path.write_bytes(b"\x00\xffnew")
    monkeypatch.setattr(FixedFileDialog, "getOpenFileName", lambda *a: (str(path), ""))
    editor.import_data()
    assert editor.document.root.children[1].value == b"\x00\xffnew"
    target = tmp_path / "out.bin"
    monkeypatch.setattr(FixedFileDialog, "getSaveFileName", lambda *a: (str(target), ""))
    editor.export_data()
    assert target.read_bytes() == path.read_bytes()
    editor.activateWindow()
    editor.tree.setFocus()
    qtbot.wait(10)
    qtbot.keyClick(editor.tree, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    assert len(saved) == 1
    assert plistlib.loads(saved[0])["blob"] == path.read_bytes()
    assert not editor.document.dirty
    assert not editor.save_button.isEnabled()


def test_plist_add_edit_delete_array_indices(qtbot, monkeypatch):
    import planbak.plist_editor as module

    editor = editor_widget(qtbot, ["one", "two"])
    real_dialog = module.ValueDialog

    class EditDialog(real_dialog):
        def exec(self):
            self.value_field.setPlainText("edited")
            self.accept()
            return self.result()

    monkeypatch.setattr(module, "ValueDialog", EditDialog)
    root = editor.tree.topLevelItem(0)
    editor.tree.clearSelection()
    editor.tree.setCurrentItem(root.child(1))
    editor.edit_node()
    assert editor.document.root.to_value() == ["one", "edited"]
    editor.tree.clearSelection()
    editor.tree.setCurrentItem(editor.tree.topLevelItem(0))
    editor.add_node()
    assert editor.document.root.to_value() == ["one", "edited", "edited"]
    assert editor.selected_node() is editor.document.root.children[-1]
    editor.delete_node()
    assert editor.document.root.to_value() == ["one", "edited"]
    assert [editor.tree.topLevelItem(0).child(i).text(0) for i in range(2)] == ["0", "1"]


@pytest.mark.parametrize(
    "answer",
    [
        FixedMessageBox.StandardButton.Ok,
        FixedMessageBox.StandardButton.Cancel,
    ],
)
def test_plist_unsaved_close(qtbot, monkeypatch, answer):
    saved = []
    editor = editor_widget(qtbot, {"value": "before"}, saved.append)
    editor.document.root.children[0].value = "after"
    editor.update_actions()
    monkeypatch.setattr(FixedMessageBox, "exec", lambda self: answer)
    closed = editor.close()
    assert closed == (answer != FixedMessageBox.StandardButton.Cancel)
    assert not saved


def test_plist_failed_save_keeps_dirty(qtbot, monkeypatch):
    def fail(data):
        raise OSError("disk full")

    editor = editor_widget(qtbot, {"value": "before"}, fail)
    editor.document.root.children[0].value = "after"
    errors = []
    monkeypatch.setattr(editor, "show_error", errors.append)
    assert not editor.save_document()
    assert editor.document.dirty and errors


def test_plist_existing_binary_null_and_nan_survive_unrelated_edit():
    import math

    doc = PlistDocument(
        plistlib.dumps({"null": None, "nan": float("nan"), "change": 1}, fmt=plistlib.FMT_BINARY)
    )
    next(child for child in doc.root.children if child.key == "change").value = 2
    value = plistlib.loads(doc.serialize())
    assert value["null"] is None and math.isnan(value["nan"]) and value["change"] == 2


def test_data_actions_only_in_single_data_context_menu(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QItemSelectionModel

    import planbak.plist_editor as module

    editor = editor_widget(qtbot, {"blob": b"\x00\xffraw", "text": "not data"})
    qtbot.waitExposed(editor)
    root = editor.tree.topLevelItem(0)
    blob, text = root.child(0), root.child(1)
    events, menus = [], []
    editor.data_hex_requested.connect(lambda *args: events.append(args))
    assert [a.text() for a in editor.tree_actions.actions() if a.text()] == [
        "添加",
        "编辑",
        "删除",
        "XML 编辑器",
    ]
    choice = ["以十六进制查看"]

    class Menu(module.QMenu):
        def exec(self, *a):
            menus.append([action.text() for action in self.actions() if not action.isSeparator()])
            return next((action for action in self.actions() if action.text() == choice[0]), None)

    monkeypatch.setattr(module, "QMenu", Menu)
    editor.tree.setCurrentItem(blob, 0, QItemSelectionModel.SelectionFlag.ClearAndSelect)
    editor.context_menu(editor.tree.visualItemRect(blob).center())
    assert menus[-1] == ["编辑", "删除", "以十六进制查看", "导入 Data…", "导出 Data…"]
    assert events[0][0] == b"\x00\xffraw" and not editor.dirty
    source = tmp_path / "source"
    source.write_bytes(b"replacement")
    choice[0] = "导入 Data…"
    monkeypatch.setattr(module.FixedFileDialog, "getOpenFileName", lambda *a: (str(source), ""))
    editor.context_menu(editor.tree.visualItemRect(blob).center())
    assert editor.document.root.children[0].value == b"replacement"
    blob = editor.tree.topLevelItem(0).child(0)
    editor.tree.setCurrentItem(blob, 0, QItemSelectionModel.SelectionFlag.ClearAndSelect)
    output = tmp_path / "output"
    choice[0] = "导出 Data…"
    monkeypatch.setattr(module.FixedFileDialog, "getSaveFileName", lambda *a: (str(output), ""))
    editor.context_menu(editor.tree.visualItemRect(blob).center())
    assert output.read_bytes() == b"replacement"
    text = editor.tree.topLevelItem(0).child(1)
    text.setSelected(True)
    choice[0] = None
    editor.context_menu(editor.tree.visualItemRect(blob).center())
    assert menus[-1] == ["编辑", "删除"]
    editor.tree.setCurrentItem(text)
    editor.tree.clearSelection()
    text.setSelected(True)
    editor.context_menu(editor.tree.visualItemRect(text).center())
    assert menus[-1] == ["编辑", "删除"]


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"", "空 Data"),
        (b" \t\r\n ", "去除首尾空白后无可见内容"),
        (b" \tHello\nWorld\t\x00\xff\x7f\n ", "Hello\nWorld\t\\x00\\xFF\\x7F"),
    ],
)
def test_data_value_preview_is_trimmed_readonly_and_preserves_bytes(qtbot, data, expected):
    from planbak.plist_editor import PlistNode, ValueDialog

    node = PlistNode.from_value(data, "blob")
    dialog = ValueDialog(node, "Dictionary", [node], True)
    dialog.language_context.set_language("zh_CN")
    qtbot.addWidget(dialog)
    dialog.show()
    assert dialog.data_preview.isReadOnly()
    assert dialog.data_preview.toPlainText() == expected
    assert f"{len(data)} 字节" in dialog.data_info.text()
    dialog.data_preview.setFocus()
    qtbot.keyClicks(dialog.data_preview, "cannot edit")
    assert dialog.data_preview.toPlainText() == expected
    dialog.key_field.setText("renamed")
    dialog.accept()
    assert dialog.result_node.key == "renamed" and dialog.result_node.value == data
    assert node.value == data


def test_data_value_preview_is_bounded_and_type_switching_keeps_original(qtbot):
    from planbak.plist_editor import PlistNode, ValueDialog, data_ascii_preview

    data = b"\xff" * 100000
    text, truncated = data_ascii_preview(data)
    assert truncated and text == "\\xFF" * 4096
    node = PlistNode.from_value(data, "blob")
    dialog = ValueDialog(node, "Dictionary", [node], True)
    dialog.language_context.set_language("zh_CN")
    qtbot.addWidget(dialog)
    assert "100000 字节" in dialog.data_info.text() and "已截断" in dialog.data_info.text()
    dialog.type_field.setCurrentText("String")
    dialog.type_field.setCurrentText("Data")
    dialog.accept()
    assert dialog.result_node.value == data
