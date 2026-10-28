"""Strict plist XML validation and a text pane with line numbers and literal search."""

from __future__ import annotations

import base64
import plistlib
from xml.parsers import expat

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import (
    QAction,
    QColor,
    QFontDatabase,
    QPalette,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class PlistXmlError(ValueError):
    def __init__(self, message, line=1, column=1):
        self.line, self.column = line, column
        super().__init__(f"第 {line} 行，第 {column} 列：{message}")


def parse_xml(text):
    """Validate structure/duplicate keys before plistlib can discard information."""
    parser = expat.ParserCreate()
    stack = []
    values = {"dict", "array", "string", "integer", "real", "true", "false", "date", "data"}
    roots = 0

    def fail(message, frame=None):
        line, column = (
            (frame["line"], frame["column"])
            if frame
            else (parser.CurrentLineNumber, parser.CurrentColumnNumber + 1)
        )
        raise PlistXmlError(message, line, column)

    def declaration(version, encoding, standalone):
        if encoding and encoding.lower().replace("-", "") != "utf8":
            fail("XML 编辑模式使用 UTF-8 编码，请修改 encoding 声明")

    def start(tag, attrs):
        nonlocal roots
        if not stack:
            if tag != "plist" or roots:
                fail("根元素必须是唯一的 plist")
            roots += 1
        else:
            parent = stack[-1]
            if parent["tag"] == "plist":
                if tag not in values or parent["count"]:
                    fail("plist 必须且只能包含一个值")
            elif parent["tag"] == "dict":
                if parent["pending"]:
                    if tag not in values:
                        fail("字典键后必须是有效的值")
                    parent["pending"] = False
                elif tag != "key":
                    fail("字典中的值必须有对应的 key")
            elif parent["tag"] == "array":
                if tag not in values:
                    fail("数组中必须是有效的 plist 值")
            else:
                fail("此值不能包含子元素")
            parent["count"] += 1
        if tag not in values | {"plist", "key"}:
            fail(f"不支持的 plist XML 元素：{tag}")
        if attrs and not (tag == "plist" and attrs == {"version": "1.0"}):
            fail("不支持的元素属性")
        stack.append(
            dict(
                tag=tag,
                text=[],
                keys=set(),
                pending=False,
                count=0,
                line=parser.CurrentLineNumber,
                column=parser.CurrentColumnNumber + 1,
            )
        )

    def end(tag):
        frame = stack.pop()
        value = "".join(frame["text"])
        if tag in {"dict", "array", "plist", "true", "false"} and value.strip():
            fail("此元素不能包含文本值", frame)
        if tag == "plist" and frame["count"] != 1:
            fail("plist 必须包含一个值", frame)
        if tag == "dict" and frame["pending"]:
            fail("字典键缺少对应的值", frame)
        if tag == "key":
            parent = stack[-1]
            if value in parent["keys"]:
                fail(f"字典中存在重复键：{value}", frame)
            parent["keys"].add(value)
            parent["pending"] = True
        try:
            if tag == "data":
                base64.b64decode("".join(value.split()), validate=True)
            elif tag in {"integer", "real", "date"}:
                # Isolate scalar parsing to report the offending element's location.
                plistlib.loads(f'<plist version="1.0"><{tag}>{value}</{tag}></plist>'.encode())
        except Exception as exc:
            fail(f"无效的 {tag} 值：{exc}", frame)

    def characters(text):
        if stack:
            stack[-1]["text"].append(text)

    def entity(*args):
        fail("不支持自定义或外部 XML 实体")

    parser.XmlDeclHandler = declaration
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    parser.EntityDeclHandler = entity
    parser.ExternalEntityRefHandler = entity
    try:
        parser.Parse(text.encode("utf-8"), True)
        return plistlib.loads(text.encode("utf-8"))
    except PlistXmlError:
        raise
    except expat.ExpatError as exc:
        raise PlistXmlError(str(exc), exc.lineno, exc.offset + 1) from exc
    except Exception as exc:
        raise PlistXmlError(
            str(exc), parser.CurrentLineNumber, parser.CurrentColumnNumber + 1
        ) from exc


class XmlHighlighter(QSyntaxHighlighter):
    def __init__(self, editor):
        super().__init__(editor.document())
        self.editor = editor

    def highlightBlock(self, text):
        import re

        dark = self.editor.palette().base().color().lightness() < 128
        colors = ("#80bfff", "#e6b673", "#83c794") if dark else ("#175ca3", "#a04310", "#39733b")
        for pattern, color in (
            (r"</?[\w:.-]+|/?>|<\?.*?\?>", colors[0]),
            (r'"[^"<>]*"|\b[\w:.-]+(?==)', colors[1]),
            (r"&[\w#]+;", colors[2]),
        ):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            for match in re.finditer(pattern, text):
                self.setFormat(match.start(), match.end() - match.start(), fmt)
        self.setCurrentBlockState(0)
        offset = 0 if self.previousBlockState() == 1 else text.find("<!--")
        comment = QTextCharFormat()
        comment.setForeground(QColor("#829182" if dark else "#687468"))
        while offset >= 0:
            end = text.find(
                "-->", offset + (0 if self.previousBlockState() == 1 and offset == 0 else 4)
            )
            if end < 0:
                self.setFormat(offset, len(text) - offset, comment)
                self.setCurrentBlockState(1)
                break
            self.setFormat(offset, end + 3 - offset, comment)
            offset = text.find("<!--", end + 3)


class LineNumbers(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor
        self.labels = []
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(
            QPalette.ColorRole.Window, editor.palette().color(QPalette.ColorRole.AlternateBase)
        )
        self.setPalette(palette)

    def sizeHint(self):
        return QSize(self.editor.gutter_width(), 0)

    def update_numbers(self):
        editor = self.editor
        block = editor.firstVisibleBlock()
        top = int(editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top())
        used = 0
        while block.isValid() and top < editor.viewport().height():
            height = int(editor.blockBoundingRect(block).height())
            if block.isVisible() and top + height > 0:
                if used == len(self.labels):
                    label = QLabel(self)
                    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    self.labels.append(label)
                label = self.labels[used]
                label.setFont(editor.font())
                label.setText(str(block.blockNumber() + 1))
                label.setGeometry(0, top, max(1, self.width() - 7), height)
                label.show()
                used += 1
            top += height
            block = block.next()
        for label in self.labels[used:]:
            label.hide()


class XmlTextEdit(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.gutter = LineNumbers(self)
        self.blockCountChanged.connect(self.update_gutter)
        self.updateRequest.connect(self.scroll_gutter)
        self.highlighter = XmlHighlighter(self)
        self.update_gutter()

    def gutter_width(self):
        return 14 + self.fontMetrics().horizontalAdvance("9") * len(str(max(1, self.blockCount())))

    def update_gutter(self, *_):
        width = self.gutter_width()
        self.setViewportMargins(width, 0, 0, 0)
        self.gutter.setFixedWidth(width)
        self.gutter.update_numbers()

    def scroll_gutter(self, rect, dy):
        self.gutter.update_numbers()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        rect = self.contentsRect()
        self.gutter.setGeometry(QRect(rect.left(), rect.top(), self.gutter_width(), rect.height()))
        self.gutter.update_numbers()

    def show_error(self, line, column):
        block = self.document().findBlockByNumber(max(0, line - 1))
        if not block.isValid():
            block = self.document().lastBlock()
        cursor = QTextCursor(block)
        prefix = block.text()[: max(0, column - 1)]
        cursor.setPosition(block.position() + len(prefix.encode("utf-16-le")) // 2)
        self.setTextCursor(cursor)
        self.centerCursor()
        selection = QTextEdit.ExtraSelection()
        selection.cursor = QTextCursor(block)
        selection.format.setBackground(QColor(200, 60, 60, 55))
        selection.format.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
        self.setExtraSelections([selection])
        self.setFocus()


class XmlPane(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.find_field = QLineEdit()
        self.find_field.setPlaceholderText("查找（纯文本）")
        self.case_sensitive = QCheckBox("区分大小写")
        row = QHBoxLayout()
        row.addWidget(self.find_field, 1)
        for label, reverse in [("上一个", True), ("下一个", False)]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, backwards=reverse: self.find(backwards))
            row.addWidget(button)
        row.addWidget(self.case_sensitive)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.replace_field = QLineEdit()
        self.replace_field.setPlaceholderText("替换为")
        row.addWidget(self.replace_field, 1)
        for label, method in [("替换", self.replace_one), ("全部替换", self.replace_all)]:
            button = QPushButton(label)
            button.clicked.connect(method)
            row.addWidget(button)
        self.result = QLabel()
        row.addWidget(self.result)
        layout.addLayout(row)
        self.code = XmlTextEdit()
        layout.addWidget(self.code, 1)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        self.find_field.returnPressed.connect(self.find)
        self.code.textChanged.connect(self.clear_error)
        for sequence, field in [("Ctrl+F", self.find_field), ("Ctrl+H", self.replace_field)]:
            action = QAction(self)
            action.setShortcut(sequence)
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(
                lambda checked=False, control=field: (control.setFocus(), control.selectAll())
            )
            self.addAction(action)

    def clear_error(self):
        self.error.clear()
        self.code.setExtraSelections([])

    def flags(self, backwards=False):
        flags = QTextDocument.FindFlag(0)
        if backwards:
            flags |= QTextDocument.FindFlag.FindBackward
        if self.case_sensitive.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        return flags

    def find(self, backwards=False):
        query = self.find_field.text()
        if not query:
            return False
        if not self.code.find(query, self.flags(backwards)):
            cursor = self.code.textCursor()
            cursor.movePosition(
                QTextCursor.MoveOperation.End if backwards else QTextCursor.MoveOperation.Start
            )
            self.code.setTextCursor(cursor)
            if not self.code.find(query, self.flags(backwards)):
                self.result.setText("未找到")
                return False
        self.result.clear()
        return True

    def replace_one(self):
        query = self.find_field.text()
        if not query:
            return
        selected = self.code.textCursor().selectedText()
        matches = (
            selected == query
            if self.case_sensitive.isChecked()
            else selected.casefold() == query.casefold()
        )
        if not matches and not self.find():
            return
        self.code.textCursor().insertText(self.replace_field.text())
        self.find()

    def replace_all(self):
        query = self.find_field.text()
        if not query:
            return
        cursor = QTextCursor(self.code.document())
        cursor.beginEditBlock()
        count = 0
        while True:
            found = self.code.document().find(query, cursor, self.flags())
            if found.isNull():
                break
            found.insertText(self.replace_field.text())
            cursor.setPosition(found.position())
            count += 1
        cursor.endEditBlock()
        self.result.setText(f"已替换 {count} 处")
