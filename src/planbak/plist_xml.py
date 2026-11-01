"""Strict plist XML validation and a text pane with line numbers and literal search."""

from __future__ import annotations

import base64
import plistlib
from xml.parsers import expat

from PySide6.QtCore import QEvent, QRect, QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QFontDatabase,
    QKeySequence,
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

from .appearance import marked_background, sync_background, xml_colors
from .dialogs import FixedDialog
from .document_ui import DocumentStatus, DocumentToolBar, translate_command


class PlistXmlError(ValueError):
    def __init__(self, message, line=1, column=1):
        self.line, self.column, self.message = line, column, message
        super().__init__(f"第 {line} 行，第 {column} 列：{message}")

    def localized(self, translate):
        known = {
            "XML 编辑模式使用 UTF-8 编码，请修改 encoding 声明": "XML editing uses UTF-8; change the encoding declaration",
            "根元素必须是唯一的 plist": "The root element must be a single plist",
            "plist 必须且只能包含一个值": "A plist must contain exactly one value",
            "字典键后必须是有效的值": "A dictionary key must be followed by a valid value",
            "字典中的值必须有对应的 key": "A dictionary value must have a key",
            "数组中必须是有效的 plist 值": "Arrays must contain valid plist values",
            "此值不能包含子元素": "This value cannot contain child elements",
            "不支持的元素属性": "Unsupported element attributes",
            "此元素不能包含文本值": "This element cannot contain text",
            "plist 必须包含一个值": "A plist must contain a value",
            "字典键缺少对应的值": "A dictionary key is missing its value",
            "不支持自定义或外部 XML 实体": "Custom or external XML entities are not supported",
        }
        message = self.message
        if message in known:
            message = translate(known[message])
        elif message.startswith("不支持的 plist XML 元素："):
            message = translate(
                "Unsupported plist XML element: {tag}", tag=message.split("：", 1)[1]
            )
        elif message.startswith("字典中存在重复键："):
            message = translate("Duplicate dictionary key: {key}", key=message.split("：", 1)[1])
        elif message.startswith("无效的 ") and " 值：" in message:
            tag, detail = message[len("无效的 ") :].split(" 值：", 1)
            message = translate("Invalid {tag} value: {error}", tag=tag, error=detail)
        return translate(
            "Line {line}, column {column}: {message}",
            line=self.line,
            column=self.column,
            message=message,
        )


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

        colors = xml_colors(self.editor.palette())
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
        comment.setForeground(QColor(colors[3]))
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
        sync_background(self, editor.palette(), QPalette.ColorRole.AlternateBase)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            sync_background(self, self.editor.palette(), QPalette.ColorRole.AlternateBase)

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
    palette_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.error_selections = []
        self.search_selections = []
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.gutter = LineNumbers(self)
        self.blockCountChanged.connect(self.update_gutter)
        self.updateRequest.connect(self.scroll_gutter)
        self.highlighter = XmlHighlighter(self)
        self.update_gutter()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "highlighter"):
            sync_background(self.gutter, self.palette(), QPalette.ColorRole.AlternateBase)
            with QSignalBlocker(self):
                self.highlighter.rehighlight()
            for selection in self.error_selections:
                selection.format.setBackground(marked_background(self.palette(), error=True))
                selection.format.setForeground(self.palette().color(QPalette.ColorRole.Text))
            self.update_selections()
            self.palette_changed.emit()

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
        selection.format.setBackground(marked_background(self.palette(), error=True))
        selection.format.setForeground(self.palette().color(QPalette.ColorRole.Text))
        selection.format.setProperty(QTextCharFormat.Property.FullWidthSelection, True)
        self.error_selections = [selection]
        self.update_selections()
        self.setFocus()

    def update_selections(self):
        self.setExtraSelections(self.error_selections + self.search_selections)

    def clear_error(self):
        self.error_selections = []
        self.update_selections()

    def set_search_selections(self, selections):
        self.search_selections = selections
        self.update_selections()


class XmlSearchDialog(FixedDialog):
    def __init__(self, pane):
        super().__init__(pane)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setWindowTitle("搜索与替换")
        layout = QVBoxLayout(self)
        self.find_field = QLineEdit()
        self.find_field.setPlaceholderText("查找（纯文本）")
        self.find_field.setAccessibleName("查找文本")
        self.case_sensitive = QCheckBox("区分大小写")
        row = QHBoxLayout()
        row.addWidget(self.find_field, 1)
        self.previous_button = QPushButton("上一个")
        self.next_button = QPushButton("下一个")
        for button, reverse in ((self.previous_button, True), (self.next_button, False)):
            button.setAutoDefault(False)
            button.clicked.connect(lambda checked=False, backwards=reverse: pane.find(backwards))
            row.addWidget(button)
        row.addWidget(self.case_sensitive)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.replace_field = QLineEdit()
        self.replace_field.setPlaceholderText("替换为")
        self.replace_field.setAccessibleName("替换文本")
        row.addWidget(self.replace_field, 1)
        for label, method in (
            ("replace_button", pane.replace_one),
            ("replace_all_button", pane.replace_all),
        ):
            button = QPushButton()
            setattr(self, label, button)
            button.setAutoDefault(False)
            button.clicked.connect(method)
            row.addWidget(button)
        layout.addLayout(row)
        self.result = QLabel()
        layout.addWidget(self.result)
        layout.addStretch()
        self.find_field.returnPressed.connect(pane.find)
        self.find_field.textChanged.connect(pane.refresh_matches)
        self.case_sensitive.toggled.connect(pane.refresh_matches)
        self.finished.connect(lambda _: pane.code.set_search_selections([]))
        self.save_action = QAction(self)
        self.save_action.setShortcut("Ctrl+S")
        self.save_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.save_action.triggered.connect(pane.save_requested)
        self.addAction(self.save_action)
        self.language_context.changed.connect(self.retranslate)
        self.retranslate()
        self.lock_size(QSize(640, 190))

    def retranslate(self):
        tr = self.language_context.translate
        self.setWindowTitle(tr("Find and Replace"))
        self.find_field.setPlaceholderText(tr("Find (plain text)"))
        self.find_field.setAccessibleName(tr("Find text"))
        self.replace_field.setPlaceholderText(tr("Replace with"))
        self.replace_field.setAccessibleName(tr("Replacement text"))
        self.case_sensitive.setText(tr("Case sensitive"))
        self.previous_button.setText(tr("Previous"))
        self.next_button.setText(tr("Next"))
        self.replace_button.setText(tr("Replace"))
        self.replace_all_button.setText(tr("Replace All"))
        self.parentWidget().refresh_matches()


class XmlPane(QWidget):
    tree_requested = Signal()
    save_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.language_context = parent.language_context
        self.search_dialog = None
        self._replacing = False
        self.search_actions = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.code = XmlTextEdit()
        self.code.setAccessibleName("plist XML 内容")
        self.code.palette_changed.connect(self.refresh_matches)
        toolbar = self.toolbar = DocumentToolBar("XML 编辑操作", self)
        self.undo_button = toolbar.command(
            "撤销", self.code.undo, icon="edit-undo", hint=QKeySequence.StandardKey.Undo
        )
        self.redo_button = toolbar.command(
            "重做", self.code.redo, icon="edit-redo", hint=QKeySequence.StandardKey.Redo
        )
        toolbar.addSeparator()
        self.search_button = toolbar.command(
            "搜索", lambda: self.show_search(), icon="edit-find", hint="Ctrl+F"
        )
        toolbar.addSeparator()
        self.tree_button = toolbar.command("树形编辑器", self.tree_requested)
        toolbar.stretch()
        layout.addWidget(toolbar)
        self.undo_button.defaultAction().setEnabled(False)
        self.redo_button.defaultAction().setEnabled(False)
        self.code.undoAvailable.connect(self.undo_button.defaultAction().setEnabled)
        self.code.redoAvailable.connect(self.redo_button.defaultAction().setEnabled)
        layout.addWidget(self.code, 1)
        self.error = DocumentStatus(parent=self)
        layout.addWidget(self.error)
        self.code.textChanged.connect(self.clear_error)
        for sequence, replacement in (("Ctrl+F", False), ("Ctrl+H", True)):
            action = QAction(self)
            action.setShortcut(sequence)
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(
                lambda checked=False, replace=replacement: self.show_search(replace)
            )
            self.addAction(action)
            self.search_actions.append(action)

    def retranslate(self):
        tr = self.language_context.translate
        self.toolbar.setWindowTitle(tr("XML editing"))
        self.toolbar.setAccessibleName(tr("XML editing"))
        self.code.setAccessibleName(tr("plist XML content"))
        self.error.retranslate()
        for button, source in (
            (self.undo_button, "Undo"),
            (self.redo_button, "Redo"),
            (self.search_button, "Find"),
            (self.tree_button, "Tree Editor"),
        ):
            translate_command(button, tr(source))

    def ensure_search(self):
        if self.search_dialog is None:
            self.search_dialog = XmlSearchDialog(self)
            for action in self.search_actions:
                self.search_dialog.addAction(action)
        return self.search_dialog

    @property
    def find_field(self):
        return self.ensure_search().find_field

    @property
    def replace_field(self):
        return self.ensure_search().replace_field

    @property
    def case_sensitive(self):
        return self.ensure_search().case_sensitive

    @property
    def result(self):
        return self.ensure_search().result

    def show_search(self, replacement=False):
        dialog = self.ensure_search()
        if not dialog.isVisible():
            selected = self.code.textCursor().selectedText()
            if selected and "\u2029" not in selected:
                dialog.find_field.setText(selected)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        field = dialog.replace_field if replacement else dialog.find_field
        field.setFocus()
        field.selectAll()
        self.refresh_matches()

    def hide_search(self):
        if self.search_dialog is not None:
            self.search_dialog.reject()

    def clear_error(self):
        self.error.clear()
        self.code.clear_error()
        if not self._replacing:
            self.refresh_matches()

    def flags(self, backwards=False):
        flags = QTextDocument.FindFlag(0)
        if backwards:
            flags |= QTextDocument.FindFlag.FindBackward
        if self.case_sensitive.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        return flags

    def refresh_matches(self, *_):
        dialog = self.search_dialog
        if dialog is None or not dialog.isVisible():
            self.code.set_search_selections([])
            return
        query = dialog.find_field.text()
        selections = []
        count = 0
        if query:
            cursor = QTextCursor(self.code.document())
            # Bound extra selections for large Data fields; navigation still searches all text.
            while count < 1000:
                cursor = self.code.document().find(query, cursor, self.flags())
                if cursor.isNull():
                    break
                selection = QTextEdit.ExtraSelection()
                selection.cursor = QTextCursor(cursor)
                selection.format.setBackground(marked_background(self.code.palette()))
                selection.format.setForeground(self.code.palette().color(QPalette.ColorRole.Text))
                selections.append(selection)
                count += 1
            current = self.code.textCursor()
            text = current.selectedText()
            matches = (
                text == query
                if dialog.case_sensitive.isChecked()
                else text.casefold() == query.casefold()
            )
            if current.hasSelection() and matches:
                selection = QTextEdit.ExtraSelection()
                selection.cursor = current
                selection.format.setBackground(
                    self.code.palette().color(QPalette.ColorRole.Highlight)
                )
                selection.format.setForeground(
                    self.code.palette().color(QPalette.ColorRole.HighlightedText)
                )
                selections.append(selection)
        self.code.set_search_selections(selections)
        dialog.result.setText(
            self.language_context.translate(
                "{count} matches", count=f"{count}{'+' if count == 1000 else ''}"
            )
            if query
            else ""
        )

    def find(self, backwards=False):
        query = self.find_field.text()
        if not query:
            return False
        found = self.code.find(query, self.flags(backwards))
        if not found:
            cursor = self.code.textCursor()
            cursor.movePosition(
                QTextCursor.MoveOperation.End if backwards else QTextCursor.MoveOperation.Start
            )
            self.code.setTextCursor(cursor)
            found = self.code.find(query, self.flags(backwards))
        self.refresh_matches()
        if not found:
            self.result.setText(self.language_context.translate("Not found"))
        if self.search_dialog.isVisible():
            self.find_field.setFocus()
        return found

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
        count = 0
        self._replacing = True
        cursor.beginEditBlock()
        try:
            while True:
                found = self.code.document().find(query, cursor, self.flags())
                if found.isNull():
                    break
                found.insertText(self.replace_field.text())
                cursor.setPosition(found.position())
                count += 1
        finally:
            cursor.endEditBlock()
            self._replacing = False
        self.refresh_matches()
        self.result.setText(
            self.language_context.translate("Replaced {count} matches", count=count)
        )
