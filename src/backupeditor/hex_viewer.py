"""Read-only hex display with bounded pages, explicit byte selection and search."""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QItemSelection,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QFontDatabase, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMenu,
    QTableView,
    QVBoxLayout,
)

from .codec import BackupError
from .dialogs import FixedDocumentWindow
from .document_ui import DocumentStatus, DocumentToolBar, document_buttons
from .hex_data import PAGE_SIZE, parse_offset, parse_pattern, read_page, search_bytes
from .viewer_tasks import ViewerTask


class HexModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.offset, self.data_bytes, self.match = 0, b"", None

    def set_page(self, offset, data, match=None):
        self.beginResetModel()
        self.offset, self.data_bytes, self.match = offset, data, match
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else (len(self.data_bytes) + 15) // 16

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 18

    def byte_offset(self, index):
        position = index.row() * 16 + index.column() - 1
        if index.isValid() and 1 <= index.column() <= 16 and position < len(self.data_bytes):
            return self.offset + position
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | (
            Qt.ItemFlag.ItemIsSelectable
            if self.byte_offset(index) is not None
            else Qt.ItemFlag.NoItemFlags
        )

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return "偏移" if section == 0 else "ASCII" if section == 17 else f"{section - 1:02X}"
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        start = index.row() * 16
        position = self.byte_offset(index)
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return f"{self.offset + start:08X}"
            if index.column() == 17:
                return "".join(
                    chr(byte) if 32 <= byte <= 126 else "."
                    for byte in self.data_bytes[start : start + 16]
                )
            if position is not None:
                return f"{self.data_bytes[position - self.offset]:02X}"
        if role == Qt.ItemDataRole.ToolTipRole and position is not None:
            return f"偏移 0x{position:X}（{position}）"
        if role == Qt.ItemDataRole.BackgroundRole and position is not None and self.match:
            if self.match[0] <= position < self.match[0] + self.match[1]:
                return QApplication.palette().highlight()
        if role == Qt.ItemDataRole.ForegroundRole and position is not None and self.match:
            if self.match[0] <= position < self.match[0] + self.match[1]:
                return QApplication.palette().highlightedText()
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return (
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                if index.column() == 17
                else int(Qt.AlignmentFlag.AlignCenter)
            )
        return None


class HexViewer(FixedDocumentWindow):
    reload_requested = Signal()

    def __init__(self, path, name, parent=None):
        super().__init__(parent)
        self.path, self.name = path, name
        self.file_size = path.stat().st_size
        self.offset, self.cursor_offset = 0, 0
        self.task, self.match, self._pattern = None, None, None
        self._closing, self._skip_prompt = False, False
        self.available, self.stale = True, False
        self.setWindowTitle(f"{name} — 十六进制查看器（只读）")
        layout = QVBoxLayout(self)
        toolbar = DocumentToolBar("十六进制导航", self)
        self.previous_button = toolbar.command(
            "上一页", lambda: self.go(max(0, self.offset - PAGE_SIZE)), icon="go-previous"
        )
        self.next_button = toolbar.command(
            "下一页", lambda: self.go(self.offset + PAGE_SIZE), icon="go-next"
        )
        toolbar.addSeparator()
        self.address = QLineEdit()
        self.address.setPlaceholderText("偏移：十进制或 0x…")
        self.address.setAccessibleName("跳转偏移")
        self.address.setToolTip("输入十进制或 0x 开头的偏移，按 Enter 跳转；Ctrl+G 定位此输入框")
        self.address.setMinimumWidth(
            self.address.fontMetrics().horizontalAdvance("0x00000000") + 24
        )
        self.address.returnPressed.connect(self.jump)
        toolbar.addWidget(self.address)
        self.jump_button = toolbar.command("跳转", self.jump)
        toolbar.addSeparator()
        self.reload_button = toolbar.command("重新加载", self.reload_requested, icon="view-refresh")
        layout.addWidget(toolbar)
        search = DocumentToolBar("十六进制搜索", self)
        self.search_mode = QComboBox()
        self.search_mode.setAccessibleName("搜索内容类型")
        self.search_mode.addItems(["十六进制字节", "UTF-8 文本"])
        search.addWidget(self.search_mode)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索内容（Ctrl+F）")
        self.search_input.setAccessibleName("搜索内容")
        self.search_input.setToolTip("Enter 查找下一个匹配；Ctrl+F 定位此输入框")
        self.search_input.returnPressed.connect(self.find_next)
        search.addWidget(self.search_input)
        self.find_previous_button = search.command(
            "上一个", lambda: self.find_next(backwards=True), icon="go-previous"
        )
        self.find_next_button = search.command("下一个", self.find_next, icon="go-next")
        self.cancel_button = search.command("取消搜索", self.cancel_search, icon="process-stop")
        layout.addWidget(search)
        self.table = QTableView()
        self.model = HexModel(self)
        self.table.setModel(self.model)
        self.table.setAccessibleName("十六进制字节与 ASCII")
        self.table.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTabKeyNavigation(False)
        self.table.horizontalHeader().setMinimumSectionSize(20)
        self.table.setColumnWidth(0, 100)
        for column in range(1, 17):
            self.table.setColumnWidth(column, self.table.fontMetrics().horizontalAdvance("FF") + 14)
        self.table.setColumnWidth(17, self.table.fontMetrics().horizontalAdvance("M" * 16) + 14)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.table.selectionModel().selectionChanged.connect(self.selection_changed)
        layout.addWidget(self.table, 1)
        self.position_label = QLabel()
        self.position_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.position_label)
        self.status = DocumentStatus(note="只读快照；选择字节后右键复制，Ctrl+A 选中当前页。")
        layout.addWidget(self.status)
        buttons = document_buttons(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for key, callback in (
            ("Ctrl+F", self.focus_search),
            ("Ctrl+G", self.focus_address),
            ("Ctrl+C", self.copy_hex),
        ):
            action = QAction(self.table if key == "Ctrl+C" else self)
            action.setShortcut(QKeySequence(key))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(callback)
            (self.table if key == "Ctrl+C" else self).addAction(action)
        self.lock_size(QSize(1080, 720))
        self.go(0)

    def update_controls(self):
        busy = self.task is not None
        for widget in (self.address, self.search_mode, self.search_input):
            widget.setEnabled(not busy)
        for button in (self.jump_button, self.find_previous_button, self.find_next_button):
            button.defaultAction().setEnabled(not busy)
        self.previous_button.defaultAction().setEnabled(not busy and self.offset > 0)
        self.next_button.defaultAction().setEnabled(
            not busy and self.offset + PAGE_SIZE < self.file_size
        )
        self.reload_button.defaultAction().setEnabled(not busy and self.available)
        self.cancel_button.defaultAction().setEnabled(busy)

    def go(self, offset, *, select=False):
        if self.task or offset < 0 or offset >= max(1, self.file_size):
            return
        try:
            base = offset // PAGE_SIZE * PAGE_SIZE
            data = read_page(self.path, base)
        except OSError as exc:
            self.status.setText(f"无法读取：{exc}")
            return
        self.offset, self.cursor_offset = base, offset
        if self.match and offset != self.match[0]:
            self.match = None
        self.model.set_page(base, data, self.match)
        self.address.setText(f"0x{offset:X}")
        self.position_label.setText(
            f"大小 {self.file_size} 字节 · 当前页 0x{base:X}–0x{base + len(data) - 1:X}"
            if data
            else "大小 0 字节 · 空文件"
        )
        if select and data:
            index = self.model.index((offset - base) // 16, (offset - base) % 16 + 1)
            self.table.setCurrentIndex(index)
            if self.match:
                selection = QItemSelection()
                first = offset - base
                end = min(first + self.match[1], len(data))
                for row in range(first // 16, (end - 1) // 16 + 1):
                    left = max(0, first - row * 16) + 1
                    right = min(16, end - row * 16)
                    selection.select(self.model.index(row, left), self.model.index(row, right))
                self.table.selectionModel().select(
                    selection, QItemSelectionModel.SelectionFlag.ClearAndSelect
                )
            self.table.scrollTo(index)
        else:
            self.table.scrollToTop()
        self.update_controls()

    def jump(self):
        try:
            self.go(parse_offset(self.address.text(), self.file_size), select=True)
        except BackupError as exc:
            self.status.setText(str(exc))

    def focus_search(self):
        self.search_input.setFocus()
        self.search_input.selectAll()

    def focus_address(self):
        self.address.setFocus()
        self.address.selectAll()

    def find_next(self, _checked=False, *, backwards=False):
        if self.task:
            return
        try:
            pattern = parse_pattern(self.search_input.text(), self.search_mode.currentIndex() == 0)
        except BackupError as exc:
            self.status.setText(str(exc))
            return
        start = self.cursor_offset
        if pattern == self._pattern and self.match:
            start = self.match[0] + (-1 if backwards else 1)
        self._pattern = pattern
        task = ViewerTask(
            lambda cancel: search_bytes(
                self.path, pattern, start, backwards=backwards, cancel=cancel
            ),
            self,
        )
        self.task = task
        self.status.setText("正在搜索…")
        self.update_controls()

        def finished():
            self.task = None
            if self._closing:
                task.deleteLater()
                super(HexViewer, self).reject()
                return
            if task.cancelled.is_set():
                self.status.setText("搜索已取消")
            elif task.error:
                self.status.setText(f"无法搜索：{task.error}")
            elif task.result.offset is None:
                self.status.setText("未找到匹配内容。")
                self.match = None
                self.go(self.cursor_offset)
            else:
                self.match = (task.result.offset, len(pattern))
                self.go(task.result.offset, select=True)
                self.status.setText(
                    f"匹配偏移 0x{task.result.offset:X} · {len(pattern)} 字节"
                    + (" · 已循环搜索" if task.result.wrapped else "")
                )
            self.update_controls()
            task.deleteLater()

        task.finished.connect(finished)
        task.start()

    def selected_offsets(self):
        return sorted(
            {
                position
                for index in self.table.selectedIndexes()
                if (position := self.model.byte_offset(index)) is not None
            }
        )

    def selection_changed(self, *_):
        selected = self.selected_offsets()
        if selected:
            self.cursor_offset = selected[0]
            self.address.setText(f"0x{selected[0]:X}")
            if self.match and selected[0] != self.match[0]:
                self.match = self.model.match = None
                if self.model.rowCount():
                    self.model.dataChanged.emit(
                        self.model.index(0, 1), self.model.index(self.model.rowCount() - 1, 16)
                    )

    def selected_bytes(self):
        return bytes(
            self.model.data_bytes[position - self.offset] for position in self.selected_offsets()
        )

    def copy_hex(self):
        if data := self.selected_bytes():
            QApplication.clipboard().setText(data.hex(" ").upper())

    def copy_bytes(self):
        if data := self.selected_bytes():
            mime = QMimeData()
            mime.setData("application/octet-stream", QByteArray(data))
            mime.setText(data.hex(" ").upper())
            QApplication.clipboard().setMimeData(mime)

    def copy_offset(self):
        if selected := self.selected_offsets():
            QApplication.clipboard().setText(f"0x{selected[0]:X}")

    def context_menu(self, point):
        index = self.table.indexAt(point)
        if self.model.byte_offset(index) is not None and not self.table.selectionModel().isSelected(
            index
        ):
            self.table.setCurrentIndex(index)
        menu = QMenu(self)
        for title, callback in (
            ("复制字节", self.copy_bytes),
            ("复制十六进制", self.copy_hex),
            ("复制起始偏移", self.copy_offset),
        ):
            action = menu.addAction(title)
            action.setEnabled(bool(self.selected_offsets()))
            action.triggered.connect(callback)
        menu.exec(self.table.viewport().mapToGlobal(point))

    def replace_snapshot(self, path, name):
        self.path, self.name, self.file_size = path, name, path.stat().st_size
        self.match, self._pattern, self.available = None, None, True
        self.stale = False
        self.setWindowTitle(f"{name} — 十六进制查看器（只读）")
        self.go(min(self.cursor_offset, max(0, self.file_size - 1)), select=True)
        self.status.setText("已重新加载只读快照。")

    def set_source_state(self, *, available=True, stale=False):
        self.available, self.stale = available, stale
        self.status.setText(
            "源文件已不可用，当前显示旧快照。"
            if not available
            else "源文件已更改，当前显示旧快照；可重新加载。"
            if stale
            else "只读快照。"
        )
        self.update_controls()

    def cancel_search(self):
        if self.task:
            self.task.cancelled.set()

    def reject(self):
        if self.task:
            self._closing = True
            self.cancel_search()
            self.setEnabled(False)
        else:
            super().reject()

    def closeEvent(self, event):
        event.ignore()
        self.reject()
