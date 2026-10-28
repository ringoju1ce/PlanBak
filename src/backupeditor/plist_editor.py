"""Typed plist editing, with original-format serialization and session staging."""

from __future__ import annotations

import copy
import math
import plistlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .dialogs import FixedDialog, FixedFileDialog, FixedMessageBox

TYPES = (
    "Dictionary",
    "Array",
    "String",
    "Integer",
    "Real",
    "Boolean",
    "Date",
    "Data",
    "UID",
    "Null",
)


def same_value(left, right):
    """Typed equality for round-trip checks, including valid binary NaN values."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_value(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_value(a, b) for a, b in zip(left, right))
    if isinstance(left, float) and math.isnan(left):
        return math.isnan(right)
    return left == right


@dataclass(eq=True)
class PlistNode:
    kind: str
    key: str = ""
    value: object = None
    children: list[PlistNode] = field(default_factory=list)

    @classmethod
    def from_value(cls, value, key=""):
        if isinstance(value, dict):
            return cls("Dictionary", key, children=[cls.from_value(v, k) for k, v in value.items()])
        if isinstance(value, list):
            return cls("Array", key, children=[cls.from_value(v) for v in value])
        kind = {
            str: "String",
            int: "Integer",
            float: "Real",
            bool: "Boolean",
            datetime: "Date",
            bytes: "Data",
            plistlib.UID: "UID",
            type(None): "Null",
        }.get(type(value))
        if kind is None:
            raise ValueError(f"不支持的 plist 类型：{type(value).__name__}")
        return cls(kind, key, value)

    def to_value(self):
        if self.kind == "Dictionary":
            keys = [child.key for child in self.children]
            if len(set(keys)) != len(keys):
                raise ValueError("字典中存在重复的键")
            return {child.key: child.to_value() for child in self.children}
        if self.kind == "Array":
            return [child.to_value() for child in self.children]
        return self.value

    @property
    def container(self):
        return self.kind in ("Dictionary", "Array")

    @property
    def preview(self):
        if self.container:
            return f"{len(self.children)} 个条目"
        if self.kind == "Data":
            return f"{len(self.value)} 字节"
        if self.kind == "Boolean":
            return "true" if self.value else "false"
        if self.kind == "Date":
            return self.value.isoformat() + "Z"
        if self.kind == "UID":
            return str(self.value.data)
        if self.kind == "Null":
            return "null"
        return str(self.value)


def parse_value(kind, text):
    if kind == "String":
        return text
    text = text.strip()
    if kind == "Integer":
        value = int(text, 10)
        if not -(2**63) <= value < 2**64:
            raise ValueError("整数必须介于 -2⁶³ 和 2⁶⁴−1 之间")
        return value
    if kind == "Real":
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("请输入有限实数")
        return value
    if kind == "Boolean":
        if text.lower() not in ("true", "false"):
            raise ValueError("布尔值只能是 true 或 false")
        return text.lower() == "true"
    if kind == "Date":
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if value.tzinfo:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if kind == "UID":
        return plistlib.UID(int(text, 10))
    raise ValueError("不支持的值类型")


class PlistDocument:
    def __init__(self, data):
        self.format = plistlib.FMT_BINARY if data.startswith(b"bplist00") else plistlib.FMT_XML
        self.root = PlistNode.from_value(plistlib.loads(data))
        self.saved_root = copy.deepcopy(self.root)

    @property
    def dirty(self):
        return self.root != self.saved_root

    def serialize(self):
        value = self.root.to_value()
        data = plistlib.dumps(value, fmt=self.format, sort_keys=False)
        # Refuse values that would silently lose precision, e.g. XML fractional dates.
        if not same_value(plistlib.loads(data), value):
            raise ValueError("当前格式无法无损保存这些值；XML 日期只支持整秒")
        return data

    def mark_saved(self):
        self.saved_root = copy.deepcopy(self.root)


class ValueDialog(FixedDialog):
    def __init__(self, node, parent_kind, siblings, binary, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑 plist 条目")
        self.node, self.siblings, self.parent_kind = node, siblings, parent_kind
        self.binary = binary
        self.result_node = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.key_field = QLineEdit(node.key)
        self.key_field.setEnabled(parent_kind == "Dictionary")
        form.addRow("键：", self.key_field)
        self.type_field = QComboBox()
        self.type_field.addItems(TYPES if binary else TYPES[:-2])
        self.type_field.setCurrentText(node.kind)
        form.addRow("类型：", self.type_field)
        layout.addLayout(form)
        self.value_field = QPlainTextEdit()
        self.value_field.setTabChangesFocus(True)
        layout.addWidget(self.value_field, 1)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.type_field.currentTextChanged.connect(self.update_value)
        self.update_value(node.kind)
        self.lock_size(QSize(600, 460))

    def update_value(self, kind):
        self.value_field.setReadOnly(kind in ("Dictionary", "Array", "Data", "Null"))
        if kind == self.node.kind:
            text = self.node.preview
        else:
            text = {
                "Boolean": "false",
                "Integer": "0",
                "Real": "0.0",
                "UID": "0",
                "Date": "2001-01-01T00:00:00Z",
            }.get(kind, "")
        if kind == "Data":
            text = f"{len(self.node.value) if self.node.kind == 'Data' else 0} 字节；使用编辑器的 Data 导入/导出按钮。"
        elif kind in ("Dictionary", "Array"):
            text = "容器的子项目在目录树中编辑。非空容器需先清空才能更改类型。"
        self.value_field.setPlainText(text)

    def accept(self):
        try:
            key = self.key_field.text() if self.parent_kind == "Dictionary" else self.node.key
            if self.parent_kind == "Dictionary" and any(
                n is not self.node and n.key == key for n in self.siblings
            ):
                raise ValueError("该字典已存在同名键")
            kind = self.type_field.currentText()
            if self.node.container and self.node.children and kind != self.node.kind:
                raise ValueError("请先清空容器的子项目，再更改类型")
            children = self.node.children if kind == self.node.kind and self.node.container else []
            if kind in ("Dictionary", "Array", "Null"):
                value = None
            elif kind == "Data":
                value = self.node.value if self.node.kind == "Data" else b""
            else:
                value = parse_value(kind, self.value_field.toPlainText())
            # Validate XML control characters and scalar serialization before accepting.
            fmt = plistlib.FMT_BINARY if self.binary else plistlib.FMT_XML
            plistlib.dumps({key: ""}, fmt=fmt)
            if kind not in ("Dictionary", "Array"):
                probe = {key: value}
                encoded = plistlib.dumps(probe, fmt=fmt, sort_keys=False)
                if not same_value(plistlib.loads(encoded), probe):
                    raise ValueError("当前格式无法无损保存该值；XML 日期只支持整秒")
            self.result_node = PlistNode(kind, key, value, children)
        except (ValueError, TypeError, OverflowError) as exc:
            self.error.setText(str(exc))
            return
        super().accept()


class PlistEditor(FixedDialog):
    contents_changed = Signal()

    def __init__(self, document, name, save_callback, parent=None):
        super().__init__(parent)
        self.document, self.name, self.save_callback = document, name, save_callback
        self._skip_prompt = False
        self._busy = False
        self._available = True
        self._conflict = False
        layout = QVBoxLayout(self)
        actions = QHBoxLayout()
        for label, method, attribute in (
            ("添加", self.add_node, "add_button"),
            ("编辑", self.edit_node, "edit_button"),
            ("删除", self.delete_node, "delete_button"),
            ("导入 Data…", self.import_data, "import_button"),
            ("导出 Data…", self.export_data, "export_button"),
        ):
            button = QPushButton(label)
            button.clicked.connect(method)
            setattr(self, attribute, button)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["键 / 索引", "类型", "值"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(0, 280)
        self.tree.setColumnWidth(1, 105)
        self.tree.itemExpanded.connect(self.populate)
        self.tree.itemSelectionChanged.connect(self.update_actions)
        self.tree.itemDoubleClicked.connect(self.edit_node)
        layout.addWidget(self.tree, 1)
        note = "保存会将修改暂挂到会话；主窗口“保存更改”才会生成备份副本。"
        if (
            isinstance(document.root.to_value(), dict)
            and document.root.to_value().get("$archiver") == "NSKeyedArchiver"
        ):
            note += "\nNSKeyedArchiver：按原始结构编辑，UID 表示对象引用。"
        self.note = note
        self.status = QLabel(note)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        self.save_button.clicked.connect(self.save_document)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for key, method, target in (
            ("Ctrl+S", self.save_document, self),
            ("F2", self.edit_node, self.tree),
            ("Delete", self.delete_node, self.tree),
        ):
            action = QAction(self)
            action.setShortcut(QKeySequence(key))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(method)
            target.addAction(action)
        self.rebuild()
        self.lock_size(QSize(940, 650))

    def selected_node(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def make_item(self, node, label, parent=None):
        item = QTreeWidgetItem([label, node.kind, node.preview[:160].replace("\n", " ↵ ")])
        item.setData(0, Qt.ItemDataRole.UserRole, node)
        item.setChildIndicatorPolicy(
            QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator
            if node.children
            else QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicator
        )
        if parent:
            parent.addChild(item)
        else:
            self.tree.addTopLevelItem(item)
        return item

    def populate(self, item):
        node = item.data(0, Qt.ItemDataRole.UserRole)
        if item.childCount() or not node.children:
            return
        for i, child in enumerate(node.children):
            self.make_item(child, child.key if node.kind == "Dictionary" else str(i), item)

    def rebuild(self, selected=None):
        expanded = set()

        def collect(item):
            if item.isExpanded():
                expanded.add(id(item.data(0, Qt.ItemDataRole.UserRole)))
            for i in range(item.childCount()):
                collect(item.child(i))

        if self.tree.topLevelItemCount():
            collect(self.tree.topLevelItem(0))
        self.tree.clear()
        root = self.make_item(self.document.root, "(根)")

        def restore(item):
            node = item.data(0, Qt.ItemDataRole.UserRole)
            if item is root or id(node) in expanded:
                self.populate(item)
                item.setExpanded(True)
                for i in range(item.childCount()):
                    restore(item.child(i))
            if node is selected:
                self.tree.setCurrentItem(item)

        restore(root)
        if not self.tree.currentItem():
            self.tree.setCurrentItem(root)
        self.update_actions()
        self.contents_changed.emit()

    def update_actions(self):
        node = self.selected_node()
        self.add_button.setEnabled(bool(node and node.container))
        self.edit_button.setEnabled(node is not None)
        self.delete_button.setEnabled(bool(node and self.tree.currentItem().parent()))
        self.import_button.setEnabled(bool(node and node.kind == "Data"))
        self.export_button.setEnabled(bool(node and node.kind == "Data"))
        self.save_button.setEnabled(self.document.dirty and not self._busy and self._available)
        self.setWindowTitle(
            f"{'* ' if self.document.dirty else ''}{self.name} — plist 编辑器 ({'二进制' if self.document.format == plistlib.FMT_BINARY else 'XML'})"
        )

    def set_busy(self, busy):
        self._busy = busy
        self.update_actions()

    def set_source_state(self, *, available=True, conflict=False):
        self._available, self._conflict = available, conflict
        extra = ""
        if not available:
            extra = "\n源文件已被删除或替换；撤销删除后可继续保存。"
        elif conflict:
            extra = "\n备份中的文件已有更新；保存前将要求确认覆盖。"
        self.status.setText(self.note + extra)
        self.update_actions()

    def reload_document(self, document):
        self.document = document
        self.set_source_state()
        self.tree.clear()
        self.rebuild()

    def edit_node(self, *_):
        item = self.tree.currentItem()
        node = self.selected_node()
        if not node:
            return
        parent_node = item.parent().data(0, Qt.ItemDataRole.UserRole) if item.parent() else None
        dialog = ValueDialog(
            node,
            parent_node.kind if parent_node else None,
            parent_node.children if parent_node else [],
            self.document.format == plistlib.FMT_BINARY,
            self,
        )
        if dialog.exec() == FixedDialog.DialogCode.Accepted:
            replacement = dialog.result_node
            node.kind, node.key, node.value, node.children = (
                replacement.kind,
                replacement.key,
                replacement.value,
                replacement.children,
            )
            self.rebuild(node)
        dialog.deleteLater()

    def add_node(self):
        parent_node = self.selected_node()
        if not parent_node or not parent_node.container:
            return
        key = "新项目"
        n = 1
        while any(child.key == key for child in parent_node.children):
            key = f"新项目 {n}"
            n += 1
        dialog = ValueDialog(
            PlistNode("String", key, ""),
            parent_node.kind,
            parent_node.children,
            self.document.format == plistlib.FMT_BINARY,
            self,
        )
        if dialog.exec() == FixedDialog.DialogCode.Accepted:
            parent_node.children.append(dialog.result_node)
            self.tree.currentItem().setExpanded(True)
            self.rebuild(dialog.result_node)
        dialog.deleteLater()

    def delete_node(self):
        item = self.tree.currentItem()
        if not item or not item.parent():
            return
        node = self.selected_node()
        parent_node = item.parent().data(0, Qt.ItemDataRole.UserRole)
        if node.container and node.children:
            answer = FixedMessageBox.question(
                self,
                "删除条目",
                "删除该容器及全部子项目？",
                FixedMessageBox.StandardButton.Yes | FixedMessageBox.StandardButton.No,
                FixedMessageBox.StandardButton.No,
            )
            if answer != FixedMessageBox.StandardButton.Yes:
                return
        parent_node.children = [child for child in parent_node.children if child is not node]
        self.rebuild(parent_node)

    def import_data(self):
        node = self.selected_node()
        if not node or node.kind != "Data":
            return
        path, _ = FixedFileDialog.getOpenFileName(self, "导入 Data")
        if path:
            try:
                node.value = Path(path).read_bytes()
                self.rebuild(node)
            except OSError as exc:
                self.show_error(exc)

    def export_data(self):
        node = self.selected_node()
        if not node or node.kind != "Data":
            return
        path, _ = FixedFileDialog.getSaveFileName(self, "导出 Data")
        if path:
            try:
                Path(path).write_bytes(node.value)
            except OSError as exc:
                self.show_error(exc)

    def show_error(self, exc):
        box = FixedMessageBox(
            FixedMessageBox.Icon.Warning,
            "无法完成 plist 操作",
            "",
            FixedMessageBox.StandardButton.Ok,
            self,
        )
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(str(exc))
        box.exec()
        box.deleteLater()

    def save_document(self):
        if self._busy or not self._available:
            return False
        if not self.document.dirty:
            return True
        try:
            data = self.document.serialize()
            if self.save_callback(data) is False:
                return False
        except Exception as exc:
            self.show_error(exc)
            return False
        self.document.mark_saved()
        self.update_actions()
        self.contents_changed.emit()
        return True

    def reject(self):
        if self._busy and not self._skip_prompt:
            return
        if self.document.dirty and not self._skip_prompt:
            answer = FixedMessageBox.question(
                self,
                "未保存的 plist 修改",
                "是否将修改保存到暂挂会话？",
                FixedMessageBox.StandardButton.Save
                | FixedMessageBox.StandardButton.Discard
                | FixedMessageBox.StandardButton.Cancel,
                FixedMessageBox.StandardButton.Cancel,
            )
            if answer == FixedMessageBox.StandardButton.Cancel:
                return
            if answer == FixedMessageBox.StandardButton.Save and not self.save_document():
                return
        super().reject()

    def discard_and_close(self):
        self._skip_prompt = True
        self.reject()
