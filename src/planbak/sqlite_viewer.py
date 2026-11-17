"""Nonmodal SQLite browser; every query runs on a cancellable worker."""

from __future__ import annotations

from html import escape

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSignalBlocker,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QFontDatabase, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QSplitter,
    QTableView,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .dialogs import DocumentWindow, FixedDialog
from .document_ui import DocumentStatus, DocumentToolBar, document_buttons, translate_command
from .i18n import SIMPLIFIED_CHINESE, LanguageContext
from .pane_focus import BrowserPaneFocus
from .sqlite_database import PAGE_SIZE, QueryResult, query, quote_identifier, schema
from .viewer_tasks import ViewerTask


def cell_text(value, limit=512, translate=None):
    if translate is None:
        translate = LanguageContext(SIMPLIFIED_CHINESE).translate
    if value is None:
        return "NULL"
    if isinstance(value, bytes):
        prefix = translate("BLOB ({count} bytes)", count=len(value))
        text = prefix + "  " + value[: limit // 2].hex(" ")
    elif isinstance(value, str):
        text = value if value else translate('"" (empty string)')
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
            return cell_text(value, translate=self.parent().language_context.translate)
        if role == Qt.ItemDataRole.ToolTipRole:
            return (
                "<qt><pre>"
                + escape(cell_text(value, 4096, self.parent().language_context.translate))
                + "</pre></qt>"
            )
        return None

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        self.sort_requested.emit(column, order)


class SQLiteViewer(DocumentWindow):
    reload_requested = Signal()

    def __init__(self, snapshot, name, parent=None):
        super().__init__(parent)
        if self.language_context is None:
            self.language_context = LanguageContext(parent=self)
        self._status_source, self._status_values = None, {}
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
        self.update_title()
        self.setModal(False)
        layout = QVBoxLayout(self)
        toolbar = self.toolbar = DocumentToolBar(
            self.language_context.translate("SQLite actions"), self
        )
        self.reload_button = toolbar.command(
            self.language_context.translate("Reload"), self.reload_requested, icon="view-refresh"
        )
        self.cancel_button = toolbar.command(
            self.language_context.translate("Cancel Query"), self.cancel_query, icon="process-stop"
        )
        toolbar.stretch()
        layout.addWidget(toolbar)
        splitter = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            [
                self.language_context.translate("Tables and Views"),
                self.language_context.translate("Type"),
            ]
        )
        self.tree.setAccessibleName(self.language_context.translate("Database tables and views"))
        self.tree.setMinimumWidth(150)
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
        page_layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableView()
        self.model = ResultModel(self)
        self.table.setModel(self.model)
        self.table.setAccessibleName(self.language_context.translate("Read-only query results"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setTabKeyNavigation(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self.table.horizontalHeader().setSortIndicatorShown(False)
        self.model.sort_requested.connect(self.sort_table)
        self.table.doubleClicked.connect(self.show_cell)
        self.table.horizontalHeader().setDefaultSectionSize(160)
        page_layout.addWidget(self.table)
        pages = self.pages = DocumentToolBar(
            self.language_context.translate("Query result pages"), self
        )
        self.previous_button = pages.command(
            self.language_context.translate("Previous Page"),
            lambda: self.load_page(-PAGE_SIZE),
            icon="go-previous",
        )
        self.next_button = pages.command(
            self.language_context.translate("Next Page"),
            lambda: self.load_page(PAGE_SIZE),
            icon="go-next",
        )
        self.page_label = QLabel()
        self.page_label.setTextFormat(Qt.TextFormat.PlainText)
        pages.addWidget(self.page_label)
        pages.stretch()
        page_layout.addWidget(pages)
        self.tabs.addTab(page, self.language_context.translate("Data"))
        self.structure = QPlainTextEdit()
        self.structure.setReadOnly(True)
        self.structure.setAccessibleName(
            self.language_context.translate("Table structure and index SQL")
        )
        self.structure.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.tabs.addTab(self.structure, self.language_context.translate("Structure and Indexes"))
        sql_page = QWidget()
        sql_layout = QVBoxLayout(sql_page)
        sql_layout.setContentsMargins(0, 0, 0, 0)
        self.sql_hint = QLabel(
            self.language_context.translate(
                "Enter a single read-only SELECT / WITH query; results appear under Data."
            )
        )
        sql_layout.addWidget(self.sql_hint)
        self.code = QPlainTextEdit()
        self.code.setAccessibleName(self.language_context.translate("Read-only SQL query"))
        self.code.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.code.setPlaceholderText("SELECT * FROM sqlite_schema;")
        sql_layout.addWidget(self.code)
        query_bar = self.query_bar = DocumentToolBar(
            self.language_context.translate("SQL query actions"), self
        )
        self.execute_button = query_bar.command(
            self.language_context.translate("Run Query"), self.execute_sql, hint="Ctrl+Return"
        )
        sql_layout.addWidget(query_bar)
        self.tabs.addTab(sql_page, self.language_context.translate("SQL Query"))
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self.status = DocumentStatus(details=False)
        layout.addWidget(self.status)
        buttons = document_buttons(QDialogButtonBox.StandardButton.Close, self)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setText(self.language_context.translate("Close"))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        action = QAction(self)
        action.setShortcut(QKeySequence("Ctrl+Return"))
        action.triggered.connect(self.execute_sql)
        self.addAction(action)
        self.panes = BrowserPaneFocus(self.tree, (self.table, self.structure, self.code), self)
        self.setMinimumSize(720, 480)
        self.resize(1080, 720)
        self.language_context.changed.connect(self.retranslate)
        self.retranslate()
        self.load_schema()

    def update_title(self):
        self.setWindowTitle(
            self.language_context.translate("{name} — SQLite Viewer (Read-only)", name=self.name)
        )

    def set_status(self, source=None, **values):
        self._status_source, self._status_values = source, values
        values = {
            k: self.language_context.error(v) if isinstance(v, Exception) else v
            for k, v in values.items()
        }
        message = self.language_context.translate(source, **values) if source else ""
        warning = (
            self.language_context.translate(self.snapshot.warning) if self.snapshot.warning else ""
        )
        self.status.setText("\n\n".join(part for part in (message, warning) if part))

    def retranslate(self):
        tr = self.language_context.translate
        self.update_title()
        for toolbar, source in (
            (self.toolbar, "SQLite actions"),
            (self.pages, "Query result pages"),
            (self.query_bar, "SQL query actions"),
        ):
            toolbar.setWindowTitle(tr(source))
            toolbar.setAccessibleName(tr(source))
        self.update_structure()
        self.tree.setHeaderLabels([tr("Tables and Views"), tr("Type")])
        self.tree.setAccessibleName(tr("Database tables and views"))
        self.table.setAccessibleName(tr("Read-only query results"))
        self.structure.setAccessibleName(tr("Table structure and index SQL"))
        self.code.setAccessibleName(tr("Read-only SQL query"))
        self.tabs.setTabText(0, tr("Data"))
        self.tabs.setTabText(1, tr("Structure and Indexes"))
        self.tabs.setTabText(2, tr("SQL Query"))
        self.sql_hint.setText(
            tr("Enter a single read-only SELECT / WITH query; results appear under Data.")
        )
        self.close_button.setText(tr("Close"))
        for button, label in (
            (self.reload_button, "Reload"),
            (self.cancel_button, "Cancel Query"),
            (self.previous_button, "Previous Page"),
            (self.next_button, "Next Page"),
            (self.execute_button, "Run Query"),
        ):
            translate_command(button, tr(label))
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            item.setText(
                1, tr("Table" if item.data(1, Qt.ItemDataRole.UserRole) == "table" else "View")
            )
        self.update_page_label()
        if self.model.rowCount() and self.model.columnCount():
            self.model.dataChanged.emit(
                self.model.index(0, 0),
                self.model.index(self.model.rowCount() - 1, self.model.columnCount() - 1),
            )
        self.set_status(self._status_source, **self._status_values)

    def update_page_label(self):
        result = self.model.result
        if not self.object_name and self.mode == "table":
            self.page_label.setText(self.language_context.translate("Up to 200 rows per page"))
        else:
            start = self.offset + 1 if result.rows else 0
            self.page_label.setText(
                self.language_context.translate(
                    "Rows {start}–{end} · Up to 200 rows per page",
                    start=start,
                    end=self.offset + len(result.rows),
                )
            )

    def update_controls(self):
        busy = self.task is not None
        self.execute_button.defaultAction().setEnabled(not busy)
        self.reload_button.defaultAction().setEnabled(not busy and self.available)
        self.cancel_button.defaultAction().setEnabled(busy)
        self.previous_button.defaultAction().setEnabled(not busy and self.offset > 0)
        self.next_button.defaultAction().setEnabled(not busy and self.model.result.more)
        self.table.horizontalHeader().setSectionsClickable(not busy and self.mode == "table")

    def start(self, function, callback):
        if self.task or self._closing:
            return
        task = ViewerTask(function, self)
        self.task = task
        self.set_status("Reading…")
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
                self.set_status("Query canceled")
            elif task.error:
                self.set_status("Unable to read: {error}", error=task.error)
            else:
                callback(task.result)
            self.update_controls()
            task.deleteLater()

        task.finished.connect(finished)
        task.start()

    def load_schema(self):
        self._pending_object = None
        self.sort_column = None
        self.sync_sort_indicator()
        self.tree.clear()

        def loaded(objects):
            self.objects = objects
            self.tree.clear()
            for name, kind, sql, table in objects:
                if kind in ("table", "view"):
                    item = QTreeWidgetItem(
                        [
                            name,
                            self.language_context.translate("Table" if kind == "table" else "View"),
                        ]
                    )
                    item.setData(1, Qt.ItemDataRole.UserRole, kind)
                    item.setData(0, Qt.ItemDataRole.UserRole, name)
                    item.setToolTip(0, "<qt><pre>" + escape(name) + "</pre></qt>")
                    self.tree.addTopLevelItem(item)
            self.set_status()
            self.model.set_result(QueryResult([], [], False))
            self.update_page_label()

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
        self.sync_sort_indicator()
        self.update_structure()
        self.tabs.setCurrentIndex(0)
        self.run_query()

    def update_structure(self):
        if not self.object_name:
            return
        tr = self.language_context.translate
        content = "\n\n".join(
            sql or tr("-- {name}: created automatically by SQLite", name=name)
            for name, kind, sql, table in self.objects
            if table == self.object_name
        )
        if self.structure.toPlainText() != content:
            cursor = self.structure.textCursor()
            position, anchor = cursor.position(), cursor.anchor()
            vertical = self.structure.verticalScrollBar().value()
            self.structure.setPlainText(content)
            cursor = self.structure.textCursor()
            limit = self.structure.document().characterCount() - 1
            cursor.setPosition(min(anchor, limit))
            cursor.setPosition(min(position, limit), cursor.MoveMode.KeepAnchor)
            self.structure.setTextCursor(cursor)
            self.structure.verticalScrollBar().setValue(vertical)

    def sync_sort_indicator(self, *, applied=False):
        header = self.table.horizontalHeader()
        ordered = applied and self.mode == "table" and self.sort_column is not None
        with QSignalBlocker(header):
            header.setSortIndicator(
                self.sort_column if ordered else -1,
                Qt.SortOrder.DescendingOrder if self.descending else Qt.SortOrder.AscendingOrder,
            )
            header.setSortIndicatorShown(ordered)

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
            self.set_status("Enter a read-only query.")
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
            self.sync_sort_indicator(applied=True)
            self.update_page_label()
            self.set_source_state(available=self.available, stale=self.stale)

        self.sync_sort_indicator()
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
        dialog.setWindowTitle(self.language_context.translate("View Cell (Read-only)"))
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setAccessibleName(self.language_context.translate("Complete cell content"))
        text.setPlainText(cell_text(value, 1024 * 1024, self.language_context.translate))
        layout.addWidget(text)
        buttons = document_buttons(QDialogButtonBox.StandardButton.Close, dialog)
        close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        close_button.setText(self.language_context.translate("Close"))

        def retranslate_dialog():
            tr = self.language_context.translate
            dialog.setWindowTitle(tr("View Cell (Read-only)"))
            text.setAccessibleName(tr("Complete cell content"))
            text.setPlainText(cell_text(value, 1024 * 1024, tr))
            close_button.setText(tr("Close"))

        self.language_context.changed.connect(retranslate_dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.lock_size(QSize(760, 460))
        self.panes.activate(self.table, focus=True)
        dialog.exec()
        self.language_context.changed.disconnect(retranslate_dialog)

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
            self.set_status("The source file is unavailable; showing the previous snapshot.")
        elif stale:
            self.set_status(
                "The source file has changed; showing the previous snapshot. You can reload it."
            )
        else:
            self.set_status()
        self.update_controls()

    def replace_snapshot(self, snapshot, name):
        self.snapshot, self.name = snapshot, name
        self.available, self.stale = True, False
        self.object_name, self.offset = None, 0
        self.structure.clear()
        self.update_title()
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
