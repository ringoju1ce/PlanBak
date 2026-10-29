"""Nonmodal SQLite browser; every query runs on a cancellable worker."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .dialogs import FixedDialog, FixedDocumentWindow
from .pane_focus import BrowserPaneFocus
from .sqlite_database import PAGE_SIZE, QueryResult, query, quote_identifier, schema
from .viewer_tasks import ViewerTask


def cell_text(value, limit=512):
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        text = f"BLOB ({len(value)} 字节)  " + value[: limit // 2].hex(" ")
    elif isinstance(value, str):
        text = value if value else '"" （空字符串）'
    else:
        text = str(value)
    return text[:limit] + ("…" if len(text) > limit else "")


class ResultModel(QAbstractTableModel):
    sort_requested = Signal(int, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.result = QueryResult([], [], False)

    def set_result(self, result):
        self.beginResetModel()
        self.result = result
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.result.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.result.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return (
                self.result.columns[section]
                if orientation == Qt.Orientation.Horizontal
                else str(section + 1)
            )
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        value = self.result.rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            return cell_text(value)
        if role == Qt.ItemDataRole.ToolTipRole:
            return "<qt><pre>" + escape(cell_text(value, 4096)) + "</pre></qt>"
        return None

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.sort_requested.emit(column, order)


class SQLiteViewer(FixedDocumentWindow):
    reload_requested = Signal()

    def __init__(self, snapshot, name, parent=None):
        super().__init__(parent)
        self.snapshot = snapshot
        self.name = name
        self.task = None
        self._closing = False
        self._skip_prompt = False
        self.available = True
        self.stale = False
        self.offset = 0
        self.object_name = None
        self.sort_column = None
        self.descending = False
        self.sql = ""
        self.mode = "table"
        self.objects = []
        self._pending_object = None
        self.setWindowTitle(f"{name} — SQLite 查看器（只读）")
        self.setModal(False)
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.reload_button = QPushButton("重新加载")
        self.reload_button.clicked.connect(self.reload_requested)
        self.cancel_button = QPushButton("取消查询")
        self.cancel_button.clicked.connect(self.cancel_query)
        toolbar.addWidget(self.reload_button)
        toolbar.addWidget(self.cancel_button)
        toolbar.addStretch()
        toolbar.addWidget(QLabel("只读；查询不会暂挂更改"))
        layout.addLayout(toolbar)
        splitter = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["表与视图", "类型"])
        self.tree.setMinimumWidth(190)
        self.tree.setRootIsDecorated(False)
        self.tree.setTabKeyNavigation(False)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.currentItemChanged.connect(self.choose_object)
        splitter.addWidget(self.tree)
        self.tabs = QTabWidget()
        page = QWidget()
        page_layout = QVBoxLayout(page)
        self.table = QTableView()
        self.model = ResultModel(self)
        self.table.setModel(self.model)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setTabKeyNavigation(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.model.sort_requested.connect(self.sort_table)
        self.table.doubleClicked.connect(self.show_cell)
        self.table.horizontalHeader().setDefaultSectionSize(160)
        page_layout.addWidget(self.table)
        pages = QHBoxLayout()
        self.previous_button = QPushButton("上一页")
        self.next_button = QPushButton("下一页")
        self.page_label = QLabel()
        self.previous_button.clicked.connect(lambda: self.load_page(-PAGE_SIZE))
        self.next_button.clicked.connect(lambda: self.load_page(PAGE_SIZE))
        pages.addWidget(self.previous_button)
        pages.addWidget(self.next_button)
        pages.addWidget(self.page_label)
        pages.addStretch()
        page_layout.addLayout(pages)
        self.tabs.addTab(page, "数据")
        self.structure = QPlainTextEdit()
        self.structure.setReadOnly(True)
        self.tabs.addTab(self.structure, "结构与索引")
        sql_page = QWidget()
        sql_layout = QVBoxLayout(sql_page)
        sql_layout.addWidget(QLabel("输入单条只读 SELECT / WITH 查询；结果显示在“数据”中。"))
        self.code = QPlainTextEdit()
        self.code.setPlaceholderText("SELECT * FROM sqlite_schema;")
        sql_layout.addWidget(self.code)
        self.execute_button = QPushButton("执行查询（Ctrl+Enter）")
        self.execute_button.clicked.connect(self.execute_sql)
        sql_layout.addWidget(self.execute_button)
        self.tabs.addTab(sql_page, "SQL 查询")
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        action = QAction(self)
        action.setShortcut(QKeySequence("Ctrl+Return"))
        action.triggered.connect(self.execute_sql)
        self.addAction(action)
        self.panes = BrowserPaneFocus(self.tree, (self.table, self.structure, self.code), self)
        self.lock_size(QSize(1080, 720))
        self.load_schema()

    def update_controls(self):
        busy = self.task is not None
        self.execute_button.setEnabled(not busy)
        self.reload_button.setEnabled(not busy and self.available)
        self.cancel_button.setEnabled(busy)
        self.previous_button.setEnabled(not busy and self.offset > 0)
        self.next_button.setEnabled(not busy and self.model.result.more)
        self.table.horizontalHeader().setSectionsClickable(not busy and self.mode == "table")

    def start(self, function, callback):
        if self.task or self._closing:
            return
        task = ViewerTask(function, self)
        self.task = task
        self.status.setText("正在读取…")
        self.update_controls()

        def finished():
            self.task = None
            if self._closing:
                task.deleteLater()
                super(SQLiteViewer, self).reject()
                return
            if self._pending_object is not None:
                self._pending_object = None
                task.deleteLater()
                self.choose_object(self.tree.currentItem())
                self.update_controls()
                return
            if task.cancelled.is_set():
                self.status.setText("查询已取消")
            elif task.error:
                self.status.setText(f"无法读取：{task.error}")
            else:
                callback(task.result)
            self.update_controls()
            task.deleteLater()

        task.finished.connect(finished)
        task.start()

    def load_schema(self):
        self._pending_object = None
        self.tree.clear()

        def loaded(objects):
            self.objects = objects
            self.tree.clear()
            for name, kind, sql, table in objects:
                if kind in ("table", "view"):
                    item = QTreeWidgetItem([name, "表" if kind == "table" else "视图"])
                    item.setData(0, Qt.ItemDataRole.UserRole, name)
                    item.setToolTip(0, "<qt><pre>" + escape(name) + "</pre></qt>")
                    self.tree.addTopLevelItem(item)
            self.status.setText(
                self.snapshot.warning or "请选择表或视图；也可以执行只读 SQL 查询。"
            )
            self.model.set_result(QueryResult([], [], False))
            self.page_label.setText("每页最多 200 行")

        self.start(lambda cancel: schema(self.snapshot.path, cancel), loaded)

    def choose_object(self, item, previous=None):
        if not item:
            return
        if self.task:
            # Keep the sidebar focused and responsive during rapid arrow-key
            # navigation; only the latest selection needs a result page.
            self._pending_object = item.data(0, Qt.ItemDataRole.UserRole)
            self.task.cancelled.set()
            return
        self.object_name = item.data(0, Qt.ItemDataRole.UserRole)
        self.mode, self.offset, self.sort_column = "table", 0, None
        self.structure.setPlainText(
            "\n\n".join(
                sql or f"-- {name}：由 SQLite 自动创建"
                for name, kind, sql, table in self.objects
                if table == self.object_name
            )
        )
        self.tabs.setCurrentIndex(0)
        self.run_query()

    def sort_table(self, column, order):
        if self.mode != "table" or not self.object_name or self.task or column < 0:
            return
        self.sort_column, self.descending, self.offset = (
            column,
            order == Qt.SortOrder.DescendingOrder,
            0,
        )
        self.run_query()

    def execute_sql(self):
        if self.task:
            return
        self.sql = self.code.toPlainText().strip()
        if not self.sql:
            self.status.setText("请输入只读查询。")
            return
        self.mode, self.offset = "sql", 0
        self.tabs.setCurrentIndex(0)
        self.run_query()

    def run_query(self):
        if self.mode == "table":
            if not self.object_name:
                return
            sql = "SELECT * FROM " + quote_identifier(self.object_name)
            if self.sort_column is not None:
                sql += f" ORDER BY {self.sort_column + 1} {'DESC' if self.descending else 'ASC'}"
            sql += f" LIMIT {PAGE_SIZE + 1} OFFSET {self.offset}"
            offset = 0
        else:
            sql, offset = self.sql, self.offset

        def loaded(result):
            self.model.set_result(result)
            start = self.offset + 1 if result.rows else 0
            self.page_label.setText(
                f"第 {start}–{self.offset + len(result.rows)} 行 · 每页最多 200 行"
            )
            self.status.setText(
                " · ".join(
                    filter(
                        None,
                        [
                            "只读查询完成",
                            self.snapshot.warning,
                            "源文件已更改，当前显示旧快照；可重新加载。" if self.stale else "",
                            "源文件已不可用，当前显示旧快照。" if not self.available else "",
                        ],
                    )
                )
            )

        self.start(
            lambda cancel: query(self.snapshot.path, sql, offset=offset, cancel=cancel), loaded
        )

    def load_page(self, delta):
        if not self.task:
            self.offset = max(0, self.offset + delta)
            self.run_query()

    def cancel_query(self):
        self._pending_object = None
        if self.task:
            self.task.cancelled.set()

    def show_cell(self, index):
        if not index.isValid():
            return
        value = self.model.result.rows[index.row()][index.column()]
        dialog = FixedDialog(self)
        dialog.setWindowTitle("查看单元格（只读）")
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(cell_text(value, 1024 * 1024))
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.lock_size(QSize(760, 460))
        self.panes.activate(self.table, focus=True)
        dialog.exec()

        def restore():
            if (
                not self._closing
                and self.isVisible()
                and not QApplication.activeModalWidget()
                and QApplication.activeWindow() in (None, self, dialog)
            ):
                self.activateWindow()
                self.panes.activate(self.panes.preferred_view, focus=True, explicit=False)

        QTimer.singleShot(0, restore)
        dialog.deleteLater()

    def set_source_state(self, *, available=True, stale=False):
        self.available, self.stale = available, stale
        if not available:
            self.status.setText("源文件已不可用，当前显示旧快照。")
        elif stale:
            self.status.setText("源文件已更改，当前显示旧快照；可重新加载。")
        self.update_controls()

    def replace_snapshot(self, snapshot, name):
        self.snapshot, self.name = snapshot, name
        self.available, self.stale = True, False
        self.object_name, self.offset = None, 0
        self.structure.clear()
        self.setWindowTitle(f"{name} — SQLite 查看器（只读）")
        self.load_schema()

    def reject(self):
        self.panes.stopped = True
        if self.task:
            self._closing = True
            self.cancel_query()
            self.setEnabled(False)
        else:
            super().reject()

    def closeEvent(self, event):
        event.ignore()
        self.reject()
