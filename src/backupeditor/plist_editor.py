"""Typed plist editing, with original-format serialization and session staging."""

from __future__ import annotations

import copy
import math
import plistlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QDate, QRegularExpression, QSize, Qt, QTime, Signal
from PySide6.QtGui import QAction, QFontDatabase, QKeySequence, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTimeEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .dialogs import FixedDialog, FixedFileDialog, FixedMessageBox
from .plist_xml import PlistXmlError, XmlPane, parse_xml

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

    def same_as(self, other):
        return (
            self.kind == other.kind
            and self.key == other.key
            and same_value(self.value, other.value)
            and len(self.children) == len(other.children)
            and all(a.same_as(b) for a, b in zip(self.children, other.children))
        )

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
        return not self.root.same_as(self.saved_root)

    def serialize(self):
        value = self.root.to_value()
        data = plistlib.dumps(value, fmt=self.format, sort_keys=False)
        # Refuse values that would silently lose precision, e.g. XML fractional dates.
        if not same_value(plistlib.loads(data), value):
            raise ValueError("当前格式无法无损保存这些值；XML 日期只支持整秒")
        return data

    def mark_saved(self):
        self.saved_root = copy.deepcopy(self.root)


class DateValue(QWidget):
    def __init__(self, binary, parent=None):
        super().__init__(parent)
        layout = QFormLayout(self)
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDateRange(QDate(1, 1, 1), QDate(9999, 12, 31))
        self.date.setDisplayFormat("yyyy-MM-dd")
        self.time = QTimeEdit()
        self.time.setDisplayFormat("HH:mm:ss")
        self.fraction = QLineEdit()
        self.fraction.setValidator(
            QRegularExpressionValidator(QRegularExpression("[0-9]{6}"), self)
        )
        self.fraction.setMaxLength(6)
        self.fraction.setEnabled(binary)
        self.fraction.setToolTip("小数秒的六位数字；XML 日期只支持整秒")
        layout.addRow("日期（UTC）：", self.date)
        layout.addRow("时间（UTC）：", self.time)
        layout.addRow("小数秒（六位）：0.", self.fraction)

    def set_value(self, value):
        self.date.setDate(QDate(value.year, value.month, value.day))
        self.time.setTime(QTime(value.hour, value.minute, value.second))
        self.fraction.setText(f"{value.microsecond:06d}")

    def value(self):
        if not all(
            control.hasAcceptableInput() for control in (self.date, self.time, self.fraction)
        ):
            raise ValueError("请输入合法的 UTC 日期、时间及六位小数秒")
        day, time = self.date.date(), self.time.time()
        return datetime(
            day.year(),
            day.month(),
            day.day(),
            time.hour(),
            time.minute(),
            time.second(),
            int(self.fraction.text()),
        )


def data_ascii_preview(data, limit=4096):
    """Bounded display only: retain undecodable bytes as visible escapes."""
    truncated = len(data) > limit
    sample = data[:limit].strip(b" \t\r\n")
    if not data:
        return "空 Data", False
    if not sample:
        return "预览片段去除首尾空白后为空" if truncated else "去除首尾空白后无可见内容", truncated
    return "".join(
        chr(byte) if 32 <= byte <= 126 or byte in (9, 10, 13) else f"\\x{byte:02X}"
        for byte in sample
    ), truncated


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
        self.values = QStackedWidget()
        self.value_field = QPlainTextEdit()
        self.value_field.setTabChangesFocus(True)
        self.number_field = QLineEdit()
        self.number_help = QLabel()
        number_page = QWidget()
        number_layout = QVBoxLayout(number_page)
        number_layout.addWidget(self.number_field)
        number_layout.addWidget(self.number_help)
        number_layout.addStretch()
        self.boolean_field = QComboBox()
        self.boolean_field.addItem("true", True)
        self.boolean_field.addItem("false", False)
        boolean_page = QWidget()
        boolean_layout = QVBoxLayout(boolean_page)
        boolean_layout.setContentsMargins(0, 0, 0, 0)
        boolean_layout.addWidget(self.boolean_field)
        boolean_layout.addStretch()
        self.date_field = DateValue(binary)
        self.fixed_value = QLabel()
        self.fixed_value.setWordWrap(True)
        self.fixed_value.setTextFormat(Qt.TextFormat.PlainText)
        data_page = QWidget()
        data_layout = QVBoxLayout(data_page)
        self.data_info = QLabel()
        self.data_preview = QPlainTextEdit()
        self.data_preview.setReadOnly(True)
        self.data_preview.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.data_preview.setAccessibleName("Data ASCII 预览（只读）")
        data_note = QLabel(
            "预览仅移除首尾空白，不改变原始字节。替换或查看完整内容请使用 Data 条目的右键菜单。"
        )
        data_note.setWordWrap(True)
        data_layout.addWidget(self.data_info)
        data_layout.addWidget(self.data_preview, 1)
        data_layout.addWidget(data_note)
        for widget in (
            self.value_field,
            number_page,
            boolean_page,
            self.date_field,
            self.fixed_value,
            data_page,
        ):
            self.values.addWidget(widget)
        layout.addWidget(self.values, 1)
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
        self.error.clear()
        existing = kind == self.node.kind
        if kind == "String":
            self.values.setCurrentIndex(0)
            self.value_field.setPlainText(self.node.value if existing else "")
        elif kind in ("Integer", "Real", "UID"):
            self.values.setCurrentIndex(1)
            expression = {
                "Integer": r"[+-]?[0-9]+",
                "UID": r"[+]?[0-9]+",
                "Real": r"[-+]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?|inf|nan)",
            }[kind]
            self.number_field.setValidator(
                QRegularExpressionValidator(QRegularExpression(expression), self.number_field)
            )
            self.number_field.setText(
                self.node.preview if existing else ("0.0" if kind == "Real" else "0")
            )
            self.number_help.setText(
                {
                    "Integer": "整数范围：−2⁶³ 至 2⁶⁴−1",
                    "Real": "支持小数和科学计数法；保留原值精度",
                    "UID": "对象引用（UID）：0 至 2⁶⁴−1；不会自动调整对象关系",
                }[kind]
            )
        elif kind == "Boolean":
            self.values.setCurrentIndex(2)
            self.boolean_field.setCurrentIndex(0 if existing and self.node.value else 1)
        elif kind == "Date":
            self.values.setCurrentIndex(3)
            self.date_field.set_value(self.node.value if existing else datetime(2001, 1, 1))
        elif kind == "Data":
            self.values.setCurrentIndex(5)
            data = self.node.value if existing else b""
            preview, truncated = data_ascii_preview(data)
            self.data_info.setText(
                f"{len(data)} 字节 · ASCII 预览（只读）"
                + (" · 仅显示前 4096 字节，已截断" if truncated else "")
            )
            self.data_preview.setPlainText(preview)
        else:
            self.values.setCurrentIndex(4)
            self.fixed_value.setText(
                ("null" if kind == "Null" else "子项目在树中管理；非空容器需先清空才能更改类型。")
            )

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
            elif kind == "Boolean":
                value = bool(self.boolean_field.currentData())
            elif kind == "Date":
                value = self.date_field.value()
            elif kind == "String":
                value = self.value_field.toPlainText()
            elif (
                kind == "Real"
                and self.node.kind == "Real"
                and self.number_field.text() == self.node.preview
            ):
                value = self.node.value
            else:
                value = parse_value(kind, self.number_field.text())
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
    data_hex_requested = Signal(object, str, str)

    def __init__(self, document, name, save_callback, parent=None):
        super().__init__(parent)
        self.document, self.name, self.save_callback = document, name, save_callback
        self._skip_prompt = False
        self._data_identity = uuid.uuid4().hex
        self._busy = False
        self._available = True
        self._conflict = False
        self._xml_initialized = False
        self._xml_saved_text = ""
        self._xml_parsed_text = ""
        self._xml_snapshot = None
        self._xml_cache = ""
        self.xml_reason = ""
        layout = QVBoxLayout(self)
        actions = QHBoxLayout()
        for label, method, attribute in (
            ("添加", self.add_node, "add_button"),
            ("编辑", self.edit_node, "edit_button"),
            ("删除", self.delete_node, "delete_button"),
            ("XML 编辑器", lambda: self.switch_mode(1), "xml_button"),
        ):
            button = QPushButton(label)
            button.clicked.connect(method)
            setattr(self, attribute, button)
            actions.addWidget(button)
        actions.addStretch()
        action_bar = QWidget()
        action_bar.setLayout(actions)
        layout.addWidget(action_bar)
        self.tree_actions = action_bar
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["键 / 索引", "类型", "值"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.context_menu)
        self.tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(0, 280)
        self.tree.setColumnWidth(1, 105)
        self.tree.itemExpanded.connect(self.populate)
        self.tree.itemSelectionChanged.connect(self.update_actions)
        self.tree.itemDoubleClicked.connect(self.edit_node)
        self.pages = QStackedWidget()
        self.pages.addWidget(self.tree)
        self.xml = XmlPane()
        self.pages.addWidget(self.xml)
        self.xml.code.textChanged.connect(self.xml_changed)
        self.xml.tree_requested.connect(lambda: self.switch_mode(0))
        self.xml.save_requested.connect(self.save_document)
        layout.addWidget(self.pages, 1)
        note = "保存会将修改暂挂到会话；主窗口“保存备份副本”才会生成备份副本。"
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
            ("Ctrl+A", self.select_visible, self.tree),
        ):
            action = QAction(self)
            action.setShortcut(QKeySequence(key))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(method)
            target.addAction(action)
        self.rebuild()
        self.lock_size(QSize(940, 650))

    @property
    def dirty(self):
        if self.pages.currentIndex() == 1 and self._xml_initialized:
            return self.xml.code.toPlainText() != self._xml_saved_text
        return self.document.dirty or (
            self._xml_initialized
            and same_value(self.document.root.to_value(), self._xml_snapshot)
            and self.xml.code.toPlainText() != self._xml_saved_text
        )

    def selected_node(self):
        items = self.tree.selectedItems()
        return items[0].data(0, Qt.ItemDataRole.UserRole) if len(items) == 1 else None

    def refresh_xml_support(self):
        try:
            pending = [self.document.root]
            while pending:
                node = pending.pop()
                if node.kind in ("UID", "Null"):
                    raise ValueError(f"标准 plist XML 无法表示 {node.kind} 类型")
                if node.kind == "Date" and node.value.microsecond:
                    raise ValueError("此 Date 包含小数秒，XML 日期只支持整秒")
                pending.extend(node.children)
            value = self.document.root.to_value()
            data = plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=False)
            if not same_value(parse_xml(data.decode("utf-8")), value):
                raise ValueError("当前值无法无损转换为 XML（日期的小数秒会丢失）")
            self._xml_cache = data.decode("utf-8")
            self.xml_reason = ""
        except Exception as exc:
            self._xml_cache = ""
            self.xml_reason = f"无法使用 XML 模式：{exc}"
        self.xml_button.setEnabled(not self.xml_reason)
        self.xml_button.setToolTip(self.xml_reason or "使用 XML 编辑此 plist")

    def xml_changed(self):
        self.update_actions()
        self.contents_changed.emit()

    def sync_xml(self):
        text = self.xml.code.toPlainText()
        if text == self._xml_parsed_text:
            return True
        try:
            value = parse_xml(text)
            # Validate against the original file format before changing the typed tree.
            data = plistlib.dumps(value, fmt=self.document.format, sort_keys=False)
            if not same_value(plistlib.loads(data), value):
                raise ValueError("源文件格式无法无损保存这些值")
        except Exception as exc:
            self.xml.error.setText(str(exc))
            if isinstance(exc, PlistXmlError):
                self.xml.code.show_error(exc.line, exc.column)
            return False
        self.document.root = PlistNode.from_value(value)
        self._xml_snapshot = copy.deepcopy(value)
        self._xml_parsed_text = text
        self.xml.clear_error()
        return True

    def switch_mode(self, index):
        if index == 1:
            self.refresh_xml_support()
            if self.xml_reason:
                return False
            value = self.document.root.to_value()
            if not self._xml_initialized or not same_value(value, self._xml_snapshot):
                self.set_xml_text(self._xml_cache)
                self._xml_snapshot = copy.deepcopy(value)
                self._xml_parsed_text = self._xml_cache
            self.pages.setCurrentIndex(1)
            self.xml.code.setFocus()
        else:
            if not self.sync_xml():
                return False
            self.xml.hide_search()
            self.pages.setCurrentIndex(0)
            self.rebuild()
            self.tree.setFocus()
        self.tree_actions.setVisible(index == 0)
        self.update_actions()
        return True

    def set_xml_text(self, text, *, saved=False):
        first = not self._xml_initialized
        self._xml_initialized = True
        if first:
            try:
                baseline = self.document.saved_root.to_value()
                xml = plistlib.dumps(baseline, fmt=plistlib.FMT_XML, sort_keys=False)
                self._xml_saved_text = (
                    xml.decode("utf-8")
                    if same_value(parse_xml(xml.decode("utf-8")), baseline)
                    else None
                )
            except Exception:
                self._xml_saved_text = None
        if saved:
            self._xml_saved_text = text
        self.xml.code.setPlainText(text)

    def select_visible(self):
        self.tree.clearSelection()
        item = self.tree.topLevelItem(0)
        while item:
            item.setSelected(True)
            item = self.tree.itemBelow(item)

    def deletion_roots(self):
        selected = self.tree.selectedItems()
        identities = {id(item) for item in selected if item.parent()}
        roots = []
        for item in selected:
            if not item.parent():
                continue
            ancestor = item.parent()
            while ancestor and id(ancestor) not in identities:
                ancestor = ancestor.parent()
            if ancestor is None:
                roots.append(item)
        return roots

    def context_menu(self, point):
        item = self.tree.itemAt(point)
        if not item:
            return
        if not item.isSelected():
            self.tree.clearSelection()
            self.tree.setCurrentItem(item)
            item.setSelected(True)
        menu = QMenu(self)
        edit = menu.addAction("编辑")
        delete = menu.addAction("删除")
        edit.setEnabled(len(self.tree.selectedItems()) == 1)
        delete.setEnabled(bool(self.deletion_roots()))
        data_actions = {}
        node = self.selected_node()
        if node and node.kind == "Data":
            menu.addSeparator()
            for title, callback in (
                ("以十六进制查看", self.view_data_hex),
                ("导入 Data…", self.import_data),
                ("导出 Data…", self.export_data),
            ):
                action = menu.addAction(title)
                action.setEnabled(not self._busy)
                data_actions[action] = callback
        chosen = menu.exec(self.tree.viewport().mapToGlobal(point))
        if chosen == edit and edit.isEnabled():
            self.edit_node()
        elif chosen == delete and delete.isEnabled():
            self.delete_node()
        elif chosen in data_actions and chosen.isEnabled():
            data_actions[chosen]()

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
        if self.pages.currentIndex() == 0:
            self.refresh_xml_support()
        self.update_actions()
        self.contents_changed.emit()

    def update_actions(self):
        node = self.selected_node()
        self.add_button.setEnabled(bool(node and node.container))
        self.edit_button.setEnabled(node is not None)
        self.delete_button.setEnabled(bool(self.deletion_roots()))
        self.save_button.setEnabled(self.dirty and not self._busy and self._available)
        self.setWindowTitle(
            f"{'* ' if self.dirty else ''}{self.name} — plist 编辑器 ({'二进制' if self.document.format == plistlib.FMT_BINARY else 'XML'})"
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
        previous_mode = self.pages.currentIndex()
        self.document = document
        self._xml_initialized = False
        self._xml_saved_text = self._xml_parsed_text = ""
        self._xml_snapshot = None
        self.pages.setCurrentIndex(0)
        self.tree_actions.show()
        self.set_source_state()
        self.tree.clear()
        self.rebuild()
        if previous_mode == 1 and not self.xml_reason:
            self.switch_mode(1)
        else:
            self.xml.hide_search()

    def edit_node(self, *_):
        node = self.selected_node()
        if not node:
            return
        item = self.tree.selectedItems()[0]
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
            self.tree.selectedItems()[0].setExpanded(True)
            self.rebuild(dialog.result_node)
        dialog.deleteLater()

    def delete_node(self):
        items = self.deletion_roots()
        if not items:
            return

        def count(node):
            return 1 + sum(count(child) for child in node.children)

        nodes = [item.data(0, Qt.ItemDataRole.UserRole) for item in items]
        if len(items) > 1 or any(node.children for node in nodes):
            affected = sum(count(node) for node in nodes)
            answer = FixedMessageBox.question(
                self,
                "删除条目",
                f"删除选中的 {len(items)} 个项目？共影响 {affected} 个节点（包含子项目）。",
                FixedMessageBox.StandardButton.Yes | FixedMessageBox.StandardButton.No,
                FixedMessageBox.StandardButton.No,
            )
            if answer != FixedMessageBox.StandardButton.Yes:
                return
        parent = items[0].parent().data(0, Qt.ItemDataRole.UserRole)
        grouped = {}
        for item, node in zip(items, nodes):
            owner = item.parent().data(0, Qt.ItemDataRole.UserRole)
            grouped.setdefault(id(owner), (owner, set()))[1].add(id(node))
        for owner, removed in grouped.values():
            owner.children = [child for child in owner.children if id(child) not in removed]
        self.rebuild(parent)

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

    def view_data_hex(self):
        node = self.selected_node()
        if node and node.kind == "Data" and not self._busy:
            label = self.tree.currentItem().text(0)
            self.data_hex_requested.emit(
                node.value, f"{self._data_identity}:{id(node)}", f"{self.name} — Data {label}"
            )

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
        if self.pages.currentIndex() == 1 and not self.sync_xml():
            return False
        if not self.dirty:
            return True
        try:
            data = self.document.serialize()
            if (
                self.document.format == plistlib.FMT_XML
                and self._xml_initialized
                and same_value(self.document.root.to_value(), self._xml_snapshot)
            ):
                data = self.xml.code.toPlainText().encode("utf-8")
            if self.save_callback(data) is False:
                return False
        except Exception as exc:
            self.show_error(exc)
            return False
        self.document.mark_saved()
        if self._xml_initialized:
            if not same_value(self.document.root.to_value(), self._xml_snapshot):
                self.refresh_xml_support()
                if not self.xml_reason:
                    self.set_xml_text(self._xml_cache, saved=True)
                    self._xml_snapshot = copy.deepcopy(self.document.root.to_value())
                    self._xml_parsed_text = self._xml_cache
                else:
                    self._xml_initialized = False
            self._xml_saved_text = self.xml.code.toPlainText()
            self.xml.code.document().setModified(False)
        self.update_actions()
        self.contents_changed.emit()
        return True

    def reject(self):
        if self._busy and not self._skip_prompt:
            return
        if self.dirty and not self._skip_prompt:
            answer = FixedMessageBox.question(
                self,
                "未保存的 plist 修改",
                "是否保存修改并暂挂到当前会话？",
                FixedMessageBox.StandardButton.Save
                | FixedMessageBox.StandardButton.Discard
                | FixedMessageBox.StandardButton.Cancel,
                FixedMessageBox.StandardButton.Cancel,
            )
            if answer == FixedMessageBox.StandardButton.Cancel:
                return
            if answer == FixedMessageBox.StandardButton.Save and not self.save_document():
                return
        self.xml.hide_search()
        super().reject()

    def discard_and_close(self):
        self._skip_prompt = True
        self.reject()
