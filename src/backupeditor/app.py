"""Qt desktop file manager. The backup format and persistence live in backup.py."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QEventLoop,
    QFileSystemWatcher,
    QItemSelection,
    QItemSelectionModel,
    QMimeData,
    QModelIndex,
    QPoint,
    QSettings,
    QSignalBlocker,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QDrag, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QDockWidget,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMenu,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .appearance import configure_system_appearance
from .backup import (
    BackupSession,
    Entry,
    copy_file,
    detect_backups,
    digest,
    load_plist,
    stale_workspaces,
)
from .codec import CHUNK, BackupError, Cancelled, check_cancel
from .dialogs import FixedDialog as QDialog
from .dialogs import FixedFileDialog as QFileDialog
from .dialogs import FixedInputDialog as QInputDialog
from .dialogs import FixedMessageBox as QMessageBox
from .dialogs import FixedProgressDialog
from .file_opening import identify_file, source_revision
from .hex_viewer import HexViewer
from .icons import ApplicationIcons
from .image_session import ImageWindows
from .navigation import Location, ViewState, split_domain
from .pane_focus import BrowserPaneFocus
from .pending_view import PendingChangesModel
from .plist_editor import PlistDocument, PlistEditor
from .sidebar import DirectoryTree
from .sqlite_database import DatabaseSnapshot, companions, prepare_snapshot
from .sqlite_viewer import SQLiteViewer

INTERNAL_MIME = "application/x-backupeditor-entries"


def human_size(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def message(parent, title, text, icon=QMessageBox.Icon.Information):
    box = QMessageBox(icon, title, "", parent=parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    box.exec()


class Task(QThread):
    progress = Signal(str, int, int)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function
        self.cancelled = threading.Event()
        self.result = None
        self.error = None

    def run(self):
        last_progress = [0.0, ""]

        def notify(text, done, total):
            now = time.monotonic()
            # 阶段切换不能被限频吞掉；同阶段的逐文件/字节通知仍限频。
            phase = text.partition("：")[0]
            if (
                phase != last_progress[1]
                or now - last_progress[0] > 0.07
                or (total and done == total)
            ):
                last_progress[:] = [now, phase]
                self.progress.emit(text, done, total)

        try:
            self.result = self.function(notify, self.cancelled.is_set)
        except Exception as exc:
            self.error = exc
        finally:
            self.function = None  # Do not retain an unlock-password closure.


class PasswordDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("解锁加密备份")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("输入这份备份的加密密码："))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setAccessibleName("备份加密密码")
        layout.addWidget(self.password)
        reveal = QCheckBox("显示密码")
        reveal.toggled.connect(
            lambda enabled: self.password.setEchoMode(
                QLineEdit.EchoMode.Normal if enabled else QLineEdit.EchoMode.Password
            )
        )
        layout.addWidget(reveal)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.lock_size(QSize(380, 180))


@dataclass(frozen=True)
class BrowserRow:
    name: str
    domain: str
    path: str
    kind: str
    entry: Entry | None = None

    @property
    def identity(self):
        if self.entry:
            return ("entry", self.entry.token)
        if self.kind == "group":
            return ("group", self.name)
        if self.kind == "domain":
            return ("domain", self.domain)
        if self.kind == "parent":
            return ("parent",)
        return ("path", self.domain, self.path)


class BrowserModel(QAbstractTableModel):
    headers = ("名称", "类型", "大小", "修改时间", "状态")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []
        self.visible_count = 0
        self.pending_tokens = set()
        self.application_icons = ApplicationIcons()

    def set_rows(self, rows, pending_tokens=()):
        self.beginResetModel()
        self.rows = rows
        self.pending_tokens = set(pending_tokens)
        self.visible_count = min(len(rows), 500)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.visible_count

    def columnCount(self, parent=QModelIndex()):
        return len(self.headers)

    def canFetchMore(self, parent):
        return not parent.isValid() and self.visible_count < len(self.rows)

    def fetchMore(self, parent):
        count = min(500, len(self.rows) - self.visible_count)
        if count:
            self.beginInsertRows(QModelIndex(), self.visible_count, self.visible_count + count - 1)
            self.visible_count += count
            self.endInsertRows()

    def headerData(self, section, orientation, role):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]
        return None

    @staticmethod
    def type_label(row):
        return {
            "domain": "文件夹",
            "group": "文件夹",
            "parent": "上层文件夹",
            "directory": "文件夹",
            "file": Path(row.name).suffix.lstrip(".").upper() or "文件",
            "special": "链接 / 特殊条目",
        }.get(row.kind, row.kind)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= self.visible_count:
            return None
        row = self.rows[index.row()]
        entry = row.entry
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return row.name
            if index.column() == 1:
                return self.type_label(row)
            if index.column() == 2:
                return human_size(entry.size) if entry and entry.kind == "file" else ""
            if index.column() == 3:
                try:
                    return (
                        datetime.fromtimestamp(entry.mtime).strftime("%Y-%m-%d %H:%M")
                        if entry and entry.mtime
                        else ""
                    )
                except (ValueError, OverflowError, OSError):
                    return ""
            if index.column() == 4:
                return "已暂挂" if entry and entry.token in self.pending_tokens else ""
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
            if row.kind == "domain":
                application_icon = self.application_icons.for_directory(row.domain, row.path)
                if application_icon is not None:
                    return application_icon
            icon = (
                "folder"
                if row.kind in ("group", "domain", "parent", "directory")
                else "text-x-generic"
            )
            if row.kind == "parent":
                icon = "go-up"
            return QIcon.fromTheme(
                icon,
                QApplication.style().standardIcon(
                    QApplication.style().StandardPixmap.SP_DirIcon
                    if icon != "text-x-generic"
                    else QApplication.style().StandardPixmap.SP_FileIcon
                ),
            )
        if role == Qt.ItemDataRole.ToolTipRole:
            if row.kind == "group":
                return f"/{row.name}"
            return f"{row.domain}/{row.path}" + (
                "\n修改在保存前只暂挂于当前会话"
                if entry and entry.token in self.pending_tokens
                else ""
            )
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.ItemIsDropEnabled
        row = self.rows[index.row()]
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if row.entry and row.path:
            flags |= Qt.ItemFlag.ItemIsDragEnabled
        if row.kind in ("directory", "parent", "domain"):
            flags |= Qt.ItemFlag.ItemIsDropEnabled
        return flags


class BrowserInteraction:
    def setup_interaction(self, window):
        self.controller = window
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setTabKeyNavigation(False)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda point: window.context_menu(self, point))
        self.doubleClicked.connect(window.activate)

    def startDrag(self, supportedActions):
        self.controller.start_drag(self)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(INTERNAL_MIME) or event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self.controller.session:
            domain, _ = self.controller.drop_location(self.indexAt(event.position().toPoint()))
            if domain is None:
                event.ignore()
                return
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        action = self.controller.queue_drop(
            event.mimeData(), self.indexAt(event.position().toPoint())
        )
        if action is not None:
            event.setDropAction(action)
            event.accept()
        else:
            event.ignore()


class BrowserTable(BrowserInteraction, QTableView):
    pass


class BrowserGrid(BrowserInteraction, QListView):
    pass


class ExportMimeData(QMimeData):
    """Prepare file URLs only when an external drag consumer requests them."""

    def __init__(self, window, tokens):
        super().__init__()
        self.window = window
        self.tokens = tokens
        self.exported = None
        self.setData(
            INTERNAL_MIME,
            json.dumps({"session": window.session.session_id, "tokens": tokens}).encode(),
        )

    def formats(self):
        return super().formats() + ["text/uri-list"]

    def hasFormat(self, mime):
        return mime == "text/uri-list" or super().hasFormat(mime)

    def retrieveData(self, mime, preferred_type):
        if mime == "text/uri-list":
            if self.exported is None:
                self.exported = self.window.drag_export(self.tokens)
            return QByteArray(
                b"\r\n".join(QUrl.fromLocalFile(str(p)).toEncoded().data() for p in self.exported)
            )
        return super().retrieveData(mime, preferred_type)


@dataclass
class OpenedFile:
    token: str
    path: Path
    fingerprint: bytes
    stamp: tuple
    seen: tuple | None = None
    stable: int = 0
    later_until: float = 0
    unreviewed: bool = False


@dataclass
class OpenedPlist:
    token: str
    editor: PlistEditor
    directory: Path
    source_digest: bytes
    observed_digest: bytes
    revision: tuple


@dataclass
class OpenedDatabase:
    token: str
    viewer: SQLiteViewer
    snapshot: DatabaseSnapshot
    revision: tuple


@dataclass
class OpenedHex:
    token: str
    viewer: HexViewer
    path: Path
    source_digest: bytes
    observed_digest: bytes
    revision: tuple
    detached: bool = False


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("BackupEditor", "BackupEditor")
        self.session = None
        self.domain = None
        self.path = ""
        self.domain_group = None
        self._navigation_session = None
        self._displayed_location = None
        self._view_states = {}
        self._latest_view_states = {}
        self._restore_generation = 0
        self._rendered_sort = ()
        self.task = None
        self.progress_dialog = None
        self.copy_task = None
        self._copy_pending = None
        self._copy_exit_requested = False
        self._drop_queued = False
        self._status_jobs = {}
        self.work_label = QLabel("就绪", self)
        self.work_progress = QProgressBar(self)
        self.work_progress.setFixedWidth(160)
        self.work_progress.setMaximumHeight(max(18, self.work_progress.fontMetrics().height() + 6))
        self.work_progress.setAccessibleName("后台任务进度")
        self.work_progress.hide()
        self.backup_status = QLabel(self)
        self.backup_status.setTextFormat(Qt.TextFormat.PlainText)
        self.backup_status.setAccessibleName("备份状态")
        self.backup_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.work_label.setTextFormat(Qt.TextFormat.PlainText)
        self.work_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.summary_panel = QWidget(self)
        summary_layout = QHBoxLayout(self.summary_panel)
        summary_layout.setContentsMargins(0, 0, 8, 0)
        summary_layout.addWidget(self.backup_status, 1)
        summary_layout.addWidget(self.work_label)
        self.work_panel = QWidget(self)
        work_layout = QHBoxLayout(self.work_panel)
        work_layout.setContentsMargins(0, 0, 0, 0)
        work_layout.addWidget(self.work_progress)
        work_layout.addStretch()
        self.status_panel = QWidget(self)
        status_layout = QHBoxLayout(self.status_panel)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(0)
        status_layout.addWidget(self.summary_panel, 3)
        status_layout.addWidget(self.work_panel, 1)
        self.statusBar().addPermanentWidget(self.status_panel, 1)
        self.opened: dict[str, OpenedFile] = {}
        self.plist_editors = {}
        self.database_viewers = {}
        self.hex_viewers = {}
        self.images = ImageWindows(self)
        self.image_viewers = self.images.viewers
        self._readonly_closing = False
        self.force_close = False
        self._exit_requested = False
        self.setWindowTitle("BackupEditor")
        self.resize(1160, 760)
        self.model = BrowserModel(self)
        self.table = BrowserTable()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in ((1, 155), (2, 100), (3, 150), (4, 100)):
            self.table.setColumnWidth(col, width)
        self.grid = BrowserGrid()
        self.grid.setModel(self.model)
        self.grid.setModelColumn(0)
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setWordWrap(True)
        for view in (self.table, self.grid):
            view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
            view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setup_interaction(self)
        self.grid.setup_interaction(self)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.grid)
        self._create_sidebar()
        for view in (self.table, self.grid):
            view.selectionModel().selectionChanged.connect(self.update_actions)
        self._create_actions()
        self._create_toolbar()
        self._create_changes()
        self.panes = BrowserPaneFocus(self.directory_tree, (self.table, self.grid), self)
        self.panes.changed.connect(self.update_actions)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._file_event)
        self.watcher.directoryChanged.connect(self._directory_event)
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(2000)
        self.poll_timer.timeout.connect(self.poll_open_files)
        self.poll_timer.start()
        self.grid_action.setChecked(self.settings.value("grid", False, type=bool))
        self.set_view(self.grid_action.isChecked())
        self.active_view().setFocus(Qt.FocusReason.OtherFocusReason)
        self.refresh()

    def _create_sidebar(self):
        self.directory_tree = DirectoryTree(self)
        self.directory_tree.directory_model.application_icons = self.model.application_icons
        self._sidebar_width = max(
            140, min(480, self.settings.value("sidebar_width", 240, type=int))
        )
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(self.directory_tree)
        self.splitter.addWidget(self.stack)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([self._sidebar_width, self.width() - self._sidebar_width])
        self.splitter.splitterMoved.connect(self.remember_sidebar_width)
        self.setCentralWidget(self.splitter)
        self.directory_tree.navigate_requested.connect(self.navigate_from_tree)
        self.directory_tree.open_requested.connect(self.open_from_tree)
        self.directory_tree.focus_changed.connect(self.update_actions)
        self.directory_tree.selectionModel().currentChanged.connect(self.update_actions)
        self.directory_tree.selectionModel().selectionChanged.connect(self.update_actions)
        self.directory_tree.customContextMenuRequested.connect(self.sidebar_context_menu)

    def remember_sidebar_width(self, *_):
        width = self.splitter.sizes()[0]
        if not self.directory_tree.isHidden() and width > 0:
            self._sidebar_width = width

    def toggle_sidebar(self, visible):
        self.directory_tree.setVisible(visible)
        if visible:
            self.splitter.setSizes(
                [self._sidebar_width, max(320, self.width() - self._sidebar_width)]
            )
        elif hasattr(self, "panes"):
            self.panes.activate(self.active_view(), focus=True)

    def sidebar_active(self):
        return hasattr(self, "panes") and self.panes.sidebar_active

    def navigate_from_tree(self, index):
        self.panes.activate(self.directory_tree)
        node = self.directory_tree.directory_model.node(index)
        if node and self.session and not self.task and node.location != self.current_location():
            self.navigate(node.location, focus_browser=False)

    def open_from_tree(self, index):
        node = self.directory_tree.directory_model.node(index)
        if node and self.session and not self.task:
            self.navigate(node.location, focus_browser=False)
            self.panes.activate(self.active_view(), focus=True)

    def sidebar_context_menu(self, point):
        if self.task or not self.session:
            return
        tree = self.directory_tree
        self.panes.activate(tree, focus=True)
        index = tree.indexAt(point)
        node = tree.directory_model.node(index)
        if not node:
            return
        tree.setCurrentIndex(index)
        menu = QMenu(self)
        open_action = menu.addAction("打开")
        delete_action = menu.addAction("删除")
        rename_action = menu.addAction("重命名")
        mutable = bool(node.entry and node.entry.path)
        delete_action.setEnabled(mutable)
        rename_action.setEnabled(mutable)
        chosen = menu.exec(tree.viewport().mapToGlobal(point))
        if chosen == open_action:
            self.open_from_tree(index)
        elif chosen == delete_action and mutable:
            self.delete_entries([node.entry.token], node.name, focus_browser=False)
        elif chosen == rename_action and mutable:
            self.rename_entry(node.entry, focus_browser=False)

    def action(self, text, function, shortcut=None, icon=None):
        action = QAction(QIcon.fromTheme(icon) if icon else QIcon(), text, self)
        action.triggered.connect(function)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        self.addAction(action)
        return action

    def _create_actions(self):
        self.open_backup_action = self.action(
            "选择备份文件夹…", self.open_backup, "Ctrl+O", "document-open"
        )
        self.add_action = self.action("添加文件…", self.add_files, icon="list-add")
        self.add_folder_action = self.action("添加文件夹…", self.add_folder)
        self.new_folder_action = self.action("新建文件夹…", self.new_folder, "Ctrl+Shift+N")
        self.open_action = self.action("打开", self.open_file, icon="document-open")
        self.extract_action = self.action(
            "导出项目…", self.extract_selected, icon="document-save-as"
        )
        self.rename_action = self.action("重命名…", self.rename_selected, "F2")
        self.delete_action = self.action("删除", self.delete_selected, "Delete", "edit-delete")
        self.save_action = self.action("应用更改…", self.save, "Ctrl+S", "document-save")
        self.create_copy_action = self.action("创建备份副本…", self.create_backup_copy)
        self.discard_action = self.action("放弃所有更改", self.discard)
        self.up_action = self.action("上层文件夹", self.up, "Backspace", "go-up")
        select_all = self.action("全选", self.select_all, "Ctrl+A")
        self.select_all_action = select_all
        enter = self.action("打开 / 进入", self.activate_selection, "Return")
        clear = self.action("清除选择", lambda: self.active_view().clearSelection(), "Escape")
        for action in (
            select_all,
            enter,
            clear,
            self.delete_action,
            self.rename_action,
            self.up_action,
        ):
            self.removeAction(action)
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.table.addAction(action)
            self.grid.addAction(action)
        for action in (self.rename_action, self.delete_action):
            self.directory_tree.addAction(action)
        file_menu = self.menuBar().addMenu("文件")
        file_menu.addActions(
            [
                self.open_backup_action,
                self.create_copy_action,
                self.add_action,
                self.add_folder_action,
                self.new_folder_action,
                self.extract_action,
                self.save_action,
            ]
        )
        file_menu.addSeparator()
        file_menu.addAction(self.action("退出", self.close, "Ctrl+Q"))
        edit_menu = self.menuBar().addMenu("编辑")
        edit_menu.addActions(
            [
                select_all,
                self.rename_action,
                self.delete_action,
                self.discard_action,
            ]
        )
        view_menu = self.menuBar().addMenu("视图")
        self.sidebar_action = QAction("文件夹树", self, checkable=True)
        self.sidebar_action.setShortcut(QKeySequence("F9"))
        self.sidebar_action.toggled.connect(self.toggle_sidebar)
        self.sidebar_action.setChecked(self.settings.value("sidebar_visible", True, type=bool))
        self.directory_tree.setVisible(self.sidebar_action.isChecked())
        self.addAction(self.sidebar_action)
        view_menu.addAction(self.sidebar_action)
        help_menu = self.menuBar().addMenu("帮助")
        help_menu.addAction(
            self.action(
                "关于",
                lambda: message(
                    self,
                    "关于 BackupEditor",
                    f"BackupEditor {__version__}\n离线浏览与编辑 Manifest.db 格式的 iOS 备份。\n"
                    "编辑先暂挂，保存生成独立副本。\n加密输出已做离线校验，尚未验证真机恢复。",
                ),
            )
        )

    def _create_toolbar(self):
        toolbar = QToolBar("文件操作", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        add_button = QToolButton()
        add_button.setDefaultAction(self.add_action)
        add_menu = QMenu(add_button)
        add_menu.addActions([self.add_action, self.add_folder_action, self.new_folder_action])
        add_button.setMenu(add_menu)
        add_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        toolbar.addWidget(add_button)
        toolbar.addAction(self.open_action)
        toolbar.addSeparator()
        self.list_action = QAction("列表", self, checkable=True)
        self.grid_action = QAction("网格", self, checkable=True)
        group = QActionGroup(self)
        group.addAction(self.list_action)
        group.addAction(self.grid_action)
        self.list_action.setChecked(True)
        self.list_action.triggered.connect(lambda: self.set_view(False))
        self.grid_action.triggered.connect(lambda: self.set_view(True))
        toolbar.addActions([self.list_action, self.grid_action])
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(BrowserModel.headers)
        self.sort_combo.setToolTip("排序方式")
        self.sort_combo.setCurrentIndex(self.settings.value("sort", 0, type=int))
        self.sort_combo.currentIndexChanged.connect(self.refresh)
        toolbar.addWidget(self.sort_combo)
        self.descending = QAction("降序", self, checkable=True)
        self.descending.setChecked(self.settings.value("descending", False, type=bool))
        self.descending.toggled.connect(self.refresh)
        toolbar.addAction(self.descending)
        self.directories_first = QAction("文件夹优先", self, checkable=True)
        self.directories_first.setChecked(self.settings.value("directories_first", True, type=bool))
        self.directories_first.toggled.connect(self.refresh)
        toolbar.addAction(self.directories_first)
        header = self.table.horizontalHeader()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self.sort_by_column)
        toolbar.addWidget(QLabel(" 图标 "))
        self.icon_slider = QSlider(Qt.Orientation.Horizontal)
        self.icon_slider.setRange(24, 128)
        self.icon_slider.setValue(self.settings.value("icons", 48, type=int))
        self.icon_slider.setFixedWidth(90)
        self.icon_slider.setAccessibleName("图标大小")
        self.icon_slider.valueChanged.connect(self.set_icon_size)
        toolbar.addWidget(self.icon_slider)
        toolbar.addSeparator()
        toolbar.addAction(self.extract_action)
        self.set_icon_size(self.icon_slider.value())
        navigation = QToolBar("导航", self)
        navigation.setMovable(False)
        self.addToolBarBreak()
        self.addToolBar(navigation)
        navigation.addAction(self.up_action)
        self.location = QLineEdit("/")
        self.location.setAccessibleName("文件夹路径")
        self.location.returnPressed.connect(self.go_location)
        navigation.addWidget(self.location)

    def _create_changes(self):
        self.changes_dock = QDockWidget("更改列表", self)
        container = QWidget()
        layout = QVBoxLayout(container)
        self.changes_list = QListView()
        self.changes_list.setAccessibleName("更改列表")
        self.changes_list.setUniformItemSizes(True)
        self.changes_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.changes_model = PendingChangesModel(self)
        self.changes_list.setModel(self.changes_model)
        self.changes_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.changes_list.customContextMenuRequested.connect(self.pending_context_menu)
        layout.addWidget(self.changes_list)
        self.apply_changes_button = QPushButton("应用更改")
        self.apply_changes_button.clicked.connect(self.save_action.trigger)
        layout.addWidget(self.apply_changes_button)
        discard = QPushButton("放弃所有更改")
        discard.clicked.connect(self.discard)
        layout.addWidget(discard)
        self.changes_dock.setWidget(container)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.changes_dock)
        self.changes_dock.hide()

    def active_view(self):
        return self.stack.currentWidget()

    def current_location(self):
        if self.domain is not None:
            return Location(self.domain, self.path)
        return Location(group=self.domain_group)

    def navigate(self, location, *, focus_browser=True):
        if self.task:
            return
        self.domain, self.path, self.domain_group = location.domain, location.path, location.group
        self.refresh(focus_browser=focus_browser)

    def clear_navigation_state(self):
        self.model.application_icons.set_session(None)
        self._view_states.clear()
        self._latest_view_states.clear()
        self._displayed_location = None
        self._navigation_session = None
        self._restore_generation += 1

    def view_layout(self):
        view = self.active_view()
        return (
            self.stack.currentIndex(),
            self._rendered_sort,
            view.iconSize().width(),
            view.iconSize().height(),
            view.viewport().width(),
            view.viewport().height(),
        )

    def remember_view_state(self):
        if self._displayed_location is None or not self.model.rows:
            return None
        view = self.active_view()
        selection = view.selectionModel()
        current = selection.currentIndex()
        anchor = QModelIndex()
        # Grid cell margins do not necessarily hit an item at (0, 0).
        for y in range(0, view.viewport().height(), 8):
            for x in range(0, view.viewport().width(), 16):
                anchor = view.indexAt(QPoint(x, y))
                if anchor.isValid():
                    break
            if anchor.isValid():
                break
        state = ViewState(
            selected=tuple(row.identity for row in self.selected_rows()),
            current=self.model.rows[current.row()].identity if current.isValid() else None,
            current_row=current.row(),
            anchor=self.model.rows[anchor.row()].identity if anchor.isValid() else None,
            anchor_row=anchor.row(),
            anchor_offset=view.visualRect(anchor).top() if anchor.isValid() else 0,
            vertical=view.verticalScrollBar().value(),
            horizontal=view.horizontalScrollBar().value(),
            loaded=self.model.rowCount(),
            layout=self.view_layout(),
            had_focus=view.hasFocus() or view.viewport().hasFocus(),
        )
        self._view_states[self._displayed_location, self.stack.currentIndex()] = state
        self._latest_view_states[self._displayed_location] = state
        return state

    def restore_view_state(self, state, *, focus=False, ensure_current=False):
        self._restore_generation += 1
        generation = self._restore_generation
        location = self._displayed_location
        view = self.active_view()
        focus_generation = self.panes.generation
        if state is None:
            view.clearSelection()
            view.selectionModel().setCurrentIndex(
                QModelIndex(), QItemSelectionModel.SelectionFlag.NoUpdate
            )
            view.verticalScrollBar().setValue(0)
            view.horizontalScrollBar().setValue(0)
            if focus:
                view.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        positions = {row.identity: i for i, row in enumerate(self.model.rows)}
        count = len(self.model.rows)
        current_row = positions.get(state.current, -1)
        if current_row < 0 and state.current is not None and count:
            current_row = min(max(state.current_row, 0), count - 1)
            if self.model.rows[current_row].kind == "parent" and count > 1:
                current_row = 1
        anchor_row = positions.get(state.anchor, -1)
        if anchor_row < 0 and count:
            anchor_row = min(max(state.anchor_row, 0), count - 1)
        selected = [positions[key] for key in state.selected if key in positions]
        required = min(
            count, max([state.loaded, current_row + 1, anchor_row + 1] + [i + 1 for i in selected])
        )
        while self.model.rowCount() < required and self.model.canFetchMore(QModelIndex()):
            self.model.fetchMore(QModelIndex())
        selection = QItemSelection()
        for row in selected:
            selection.select(
                self.model.index(row, 0), self.model.index(row, self.model.columnCount() - 1)
            )
        view.selectionModel().select(
            selection,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        current = self.model.index(current_row, 0) if current_row >= 0 else QModelIndex()
        view.selectionModel().setCurrentIndex(current, QItemSelectionModel.SelectionFlag.NoUpdate)

        def apply_scroll():
            if (
                generation != self._restore_generation
                or location != self._displayed_location
                or view is not self.active_view()
            ):
                return
            view.doItemsLayout()
            same_layout = state.layout == self.view_layout()
            if same_layout and positions.get(state.anchor) == state.anchor_row:
                view.verticalScrollBar().setValue(state.vertical)
            elif anchor_row >= 0:
                anchor = self.model.index(anchor_row, 0)
                view.scrollTo(anchor, QAbstractItemView.ScrollHint.PositionAtTop)
                view.verticalScrollBar().setValue(
                    view.verticalScrollBar().value() - state.anchor_offset
                )
            if current.isValid() and (focus or ensure_current or not same_layout):
                if not view.viewport().rect().intersects(view.visualRect(current)):
                    view.scrollTo(current, QAbstractItemView.ScrollHint.EnsureVisible)
            view.horizontalScrollBar().setValue(state.horizontal)
            if (focus or state.had_focus) and focus_generation == self.panes.generation:
                view.setFocus(Qt.FocusReason.OtherFocusReason)

        apply_scroll()
        # Qt may schedule another icon-grid layout after a model reset or resize.
        QTimer.singleShot(0, apply_scroll)

    def selected_rows(self):
        selection = self.active_view().selectionModel()
        indices = (
            selection.selectedRows(0)
            if self.active_view() == self.table
            else selection.selectedIndexes()
        )
        return [self.model.rows[i] for i in sorted({index.row() for index in indices})]

    def selected_tokens(self):
        return [row.entry.token for row in self.selected_rows() if row.entry and row.path]

    def select_all(self):
        if self.sidebar_active():
            return
        # Fetch every row before selecting, including directories with >500 items.
        while self.model.canFetchMore(QModelIndex()):
            self.model.fetchMore(QModelIndex())
        self.active_view().selectAll()

    def update_actions(self, *_):
        busy = self.task is not None
        for record in self.plist_editors.values():
            record.editor.set_busy(busy)
        self.directory_tree.directory_model.suspended = busy
        self.directory_tree.setEnabled(not busy)
        rows = self.selected_rows()
        tokens = self.selected_tokens()
        session = self.session
        copying = self.copy_task is not None or self._copy_pending is not None
        self.open_backup_action.setEnabled(not busy and not copying)
        self.create_copy_action.setEnabled(not busy and not copying and session is not None)
        editable = bool(session and session.editable)
        self.save_action.setText("应用更改" if editable else "应用更改…")
        self.save_action.setToolTip(
            "应用更改到当前可编辑副本" if editable else "创建新的备份副本并应用更改"
        )
        self.location.setEnabled(not busy) if hasattr(self, "location") else None
        self.open_action.setEnabled(not busy and len(rows) == 1 and rows[0].kind == "file")
        self.extract_action.setEnabled(not busy and bool(tokens))
        self.rename_action.setEnabled(not busy and len(rows) == 1 and bool(tokens))
        self.delete_action.setEnabled(not busy and bool(tokens))
        if self.sidebar_active():
            entry = self.directory_tree.current_entry()
            self.open_action.setEnabled(False)
            self.extract_action.setEnabled(False)
            self.rename_action.setEnabled(not busy and entry is not None)
            self.delete_action.setEnabled(not busy and entry is not None)
        for action in (self.add_action, self.add_folder_action, self.new_folder_action):
            action.setEnabled(not busy and session is not None and self.domain is not None)
        self.save_action.setEnabled(
            not busy
            and not copying
            and bool(
                session
                and (
                    session.dirty
                    or any(record.editor.dirty for record in self.plist_editors.values())
                )
            )
        )
        self.apply_changes_button.setEnabled(self.save_action.isEnabled())
        self.apply_changes_button.setToolTip(self.save_action.toolTip())
        self.discard_action.setEnabled(not busy and bool(session and session.dirty))
        self.up_action.setEnabled(not busy and not self.current_location().is_root)
        self.select_all_action.setEnabled(not busy and not self.sidebar_active())
        self.stack.setEnabled(not busy)
        self.sort_combo.setEnabled(not busy) if hasattr(self, "sort_combo") else None
        if hasattr(self, "descending"):
            self.descending.setEnabled(not busy)
            self.directories_first.setEnabled(not busy)
        if hasattr(self, "changes_dock"):
            self.changes_dock.setEnabled(not busy)

    def sort_by_column(self, column):
        if self.task or not 0 <= column < self.sort_combo.count():
            return
        descending = (
            not self.descending.isChecked() if column == self.sort_combo.currentIndex() else False
        )
        with QSignalBlocker(self.sort_combo), QSignalBlocker(self.descending):
            self.sort_combo.setCurrentIndex(column)
            self.descending.setChecked(descending)
        self.refresh()

    def refresh(self, *_, focus_browser=True):
        if self.task or not hasattr(self, "sort_combo"):
            return
        session_id = self.session.session_id if self.session else None
        if session_id != self._navigation_session:
            self.clear_navigation_state()
            self._navigation_session = session_id
        else:
            self.remember_view_state()
        self.model.application_icons.set_session(self.session)
        location = self.current_location()
        navigating = location != self._displayed_location
        rows = []
        if self.session:
            if self.domain is None:
                groups = set()
                for domain in self.session.domains():
                    group, identifier = split_domain(domain)
                    if location.group:
                        if group == location.group:
                            rows.append(BrowserRow(identifier, domain, "", "domain"))
                    elif group:
                        groups.add(group)
                    else:
                        rows.append(BrowserRow(domain, domain, "", "domain"))
                rows.extend(BrowserRow(group, "", "", "group") for group in groups)
            else:
                while self.path and not self.session.at(self.domain, self.path):
                    self.path = self.path.rpartition("/")[0]
                rows = [
                    BrowserRow(e.name, e.domain, e.path, e.kind, e)
                    for e in self.session.children(self.domain, self.path)
                ]
                location = self.current_location()
        sort = self.sort_combo.currentIndex()
        pending_tokens = self.session.pending_tokens if self.session else set()
        self.table.horizontalHeader().setSortIndicator(
            sort,
            Qt.SortOrder.DescendingOrder
            if self.descending.isChecked()
            else Qt.SortOrder.AscendingOrder,
        )

        def key(row):
            if sort == 1:
                return (BrowserModel.type_label(row).casefold(), row.name.casefold())
            if sort == 2:
                return (row.entry.size if row.entry else 0, row.name.casefold())
            if sort == 3:
                return (row.entry.mtime if row.entry else 0, row.name.casefold())
            if sort == 4:
                return (bool(row.entry and row.entry.token in pending_tokens), row.name.casefold())
            return row.name.casefold()

        rows.sort(key=key, reverse=self.descending.isChecked())
        if self.directories_first.isChecked():
            rows.sort(key=lambda row: row.kind not in ("group", "domain", "directory"))
        if not location.is_root:
            parent = location.parent
            rows.insert(0, BrowserRow("..", parent.domain or "", parent.path, "parent"))
        self.model.set_rows(rows, pending_tokens)
        self._rendered_sort = (
            sort,
            self.descending.isChecked(),
            self.directories_first.isChecked(),
        )
        self._displayed_location = location
        self.location.setText(location.address)
        cached = self._view_states.get((location, self.stack.currentIndex()))
        latest = self._latest_view_states.get(location)
        state = cached or latest
        if latest:
            state = replace(
                state,
                selected=latest.selected,
                current=latest.current,
                current_row=latest.current_row,
                had_focus=latest.had_focus,
            )
        focus_browser = focus_browser and not self.sidebar_active()
        if state and not focus_browser:
            state = replace(state, had_focus=False)
        self.restore_view_state(state, focus=navigating and focus_browser)
        self.directory_tree.sync(self.session, location)
        self.changes_model.set_changes(self.session.changes if self.session else ())
        if self.session:
            device = self.session.info.get("Device Name", self.session.root.name)
            dirty = self.session.dirty
            category = "可编辑副本" if self.session.editable else "原始备份"
            self.setWindowTitle(f"{'* ' if dirty else ''}{device} · {category} — BackupEditor")
            self.backup_status.setText(
                f"{device} · {category} · {'加密' if self.session.encrypted else '未加密'} · "
                f"{len(rows) - (0 if location.is_root else 1)} 个项目 · "
                f"{len(self.session.changes)} 个已更改项目"
            )
            self.backup_status.setToolTip(f"{self.backup_status.text()}\n{self.session.root}")
            self.statusBar().setToolTip("")
            if dirty:
                self.changes_dock.show()
            else:
                self.changes_dock.hide()
        else:
            self.backup_status.setText("从“文件 → 选择备份文件夹”打开 iOS 备份")
            self.backup_status.setToolTip("")
        self.update_actions()
        self.update_work_status()

        QTimer.singleShot(0, self.check_plist_editors)

    def set_view(self, grid):
        outgoing = self.remember_view_state()
        self.stack.setCurrentIndex(1 if grid else 0)
        self.grid_action.setChecked(grid)
        self.list_action.setChecked(not grid)
        self.settings.setValue("grid", grid)
        QWidget.setTabOrder(self.directory_tree, self.active_view())
        if outgoing:
            cached = self._view_states.get((self._displayed_location, self.stack.currentIndex()))
            state = replace(
                cached or outgoing,
                selected=outgoing.selected,
                current=outgoing.current,
                current_row=outgoing.current_row,
                had_focus=outgoing.had_focus,
            )
            self.restore_view_state(state, ensure_current=True)
        self.update_actions()

    def set_icon_size(self, size):
        state = self.remember_view_state()
        self.grid.setIconSize(QSize(size, size))
        self.grid.setGridSize(QSize(max(110, size + 44), size + 56))
        self.table.setIconSize(QSize(min(size, 48), min(size, 48)))
        self.table.verticalHeader().setDefaultSectionSize(min(size, 48) + 12)
        if state:
            self.restore_view_state(state, ensure_current=True)

    def activate(self, index):
        if not index.isValid() or self.task:
            return
        self.panes.activate(self.active_view())
        row = self.model.rows[index.row()]
        self.active_view().selectionModel().setCurrentIndex(
            self.model.index(index.row(), 0), QItemSelectionModel.SelectionFlag.NoUpdate
        )
        if row.kind == "parent":
            self.up()
        elif row.kind == "group":
            self.navigate(Location(group=row.name))
        elif row.kind in ("domain", "directory"):
            self.navigate(Location(row.domain, row.path))
        elif row.kind == "file":
            self.open_file()

    def activate_selection(self):
        rows = self.selected_rows()
        if len(rows) == 1:
            self.activate(self.model.index(self.model.rows.index(rows[0]), 0))

    def up(self):
        if self.current_location().is_root:
            return
        self.navigate(self.current_location().parent)

    def go_location(self):
        if not self.session:
            return
        try:
            location = Location.from_address(self.location.text(), self.session.domains())
            if location.domain:
                entry = self.session.at(location.domain, location.path)
                if not entry or entry.kind != "directory":
                    raise BackupError("文件夹不存在")
        except BackupError as exc:
            message(self, "文件夹不存在", str(exc), QMessageBox.Icon.Warning)
            return
        self.navigate(location)

    def update_work_status(self):
        self.statusBar().clearMessage()
        if not self._status_jobs:
            self.work_label.setText("就绪")
            self.work_progress.hide()
            return
        text, done, total = next(reversed(self._status_jobs.values()))
        self.work_label.setText(text)
        self.work_progress.setRange(0, 1000 if total > 0 else 0)
        if total > 0:
            self.work_progress.setValue(min(1000, int(done / total * 1000)))
        self.work_progress.show()

    def work_progress_changed(self, job, text, done, total):
        if job in self._status_jobs:
            self._status_jobs[job] = (text, done, total)
            self.update_work_status()

    def create_backup_copy(self, *_):
        if not self.session or self.task or self.copy_task or self._copy_pending:
            return
        session = self.session
        target, _ = QFileDialog.getSaveFileName(
            self,
            "创建备份副本",
            str(session.root.parent / (session.root.name + "-copy")),
            "备份文件夹 (*)",
        )
        if not target:
            return
        job = Task(
            lambda progress, cancel: session.create_copy(
                Path(target), progress=progress, cancel=cancel
            ),
            self,
        )
        self.copy_task = job
        self._status_jobs[job] = ("正在创建备份副本…", 0, 0)
        job.progress.connect(
            lambda text, done, total: self.work_progress_changed(job, text, done, total)
        )

        def finished():
            self.copy_task = None
            self._status_jobs.pop(job, None)
            self.update_work_status()
            if not job.error and self.session is session:
                self._copy_pending = (session, job.result)
            if self._copy_exit_requested:
                self._copy_exit_requested = False
                self._copy_pending = None
                QTimer.singleShot(0, self.close)
            else:
                if job.error and not isinstance(job.error, Cancelled):
                    message(self, "无法创建备份副本", str(job.error), QMessageBox.Icon.Warning)
                self.attach_created_copy()
            self.update_actions()
            job.deleteLater()

        job.finished.connect(finished)
        self.update_actions()
        self.update_work_status()
        job.start()

    def attach_created_copy(self):
        if not self._copy_pending or self.task or self._exit_requested:
            return
        session, copied = self._copy_pending
        self._copy_pending = None
        if self.session is not session:
            return
        self.run_task(
            "切换到备份副本",
            lambda progress, cancel: session.use_copy(copied),
            lambda _: self.refresh(focus_browser=False),
            silent=True,
        )

    def run_task(
        self,
        title,
        function,
        callback=None,
        *,
        silent=False,
        status=False,
        on_error=None,
        on_done=None,
    ):
        if self.task:
            return
        job = Task(function, self)
        self.task = job
        dialog = None
        if status:
            self._status_jobs[job] = (title + "…", 0, 0)
            job.progress.connect(
                lambda text, done, total: self.work_progress_changed(job, text, done, total)
            )
            self.update_work_status()
        if not silent and not status:
            dialog = FixedProgressDialog(title, "取消", 0, 0, self)
            self.progress_dialog = dialog
            dialog.setWindowTitle("BackupEditor")
            dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
            dialog.canceled.connect(job.cancelled.set)

            def progress(text, done, total):
                if total > 0:
                    dialog.setRange(0, 1000)
                    dialog.setValue(min(999, int(done / total * 1000)))
                else:
                    dialog.setRange(0, 0)
                dialog.setLabelText(text)
                if text == "正在应用更改…":
                    dialog.set_cancellable(False)

            job.progress.connect(progress)
            dialog.show()
        self.update_actions()

        def finished():
            self.task = None
            self._status_jobs.pop(job, None)
            self.update_work_status()
            if dialog:
                self.progress_dialog = None
                dialog.finish()
                dialog.deleteLater()
            self.update_actions()
            if self._exit_requested:
                if isinstance(job.result, BackupSession) and job.result is not self.session:
                    job.result.close()
                if on_done:
                    on_done()
                self._exit_requested = False
                job.deleteLater()
                QTimer.singleShot(0, self.close)
                return
            if job.error:
                if isinstance(job.error, Cancelled):
                    self.statusBar().setToolTip("操作已取消")
                elif on_error:
                    on_error(job.error)
                else:
                    message(self, title, str(job.error), QMessageBox.Icon.Critical)
            elif callback:
                callback(job.result)
            if on_done:
                on_done()
            job.deleteLater()
            self.attach_created_copy()

        job.finished.connect(finished)
        job.start()

    def confirm_leave(self, continuation, *, exiting=False):
        if self.task or self.copy_task or self._copy_pending:
            return
        if self.database_viewers or self.hex_viewers or self.image_viewers:
            self.close_readonly_viewers(lambda: self.confirm_leave(continuation, exiting=exiting))
            return
        for record in list(self.plist_editors.values()):
            if not record.editor.close():
                return
        if not self.session:
            continuation()
            return
        external = []
        for record in self.opened.values():
            if record.unreviewed or self.file_stamp(record.path) != record.stamp:
                try:
                    self.session.get(record.token)
                except BackupError:
                    # A staged deletion takes precedence over its old editor copy.
                    continue
                external.append(record)
        if not self.session.dirty and not external:
            continuation()
            return
        save_description = (
            "应用更改到当前可编辑副本" if self.session.editable else "创建备份副本并应用更改"
        )
        box = QMessageBox(
            QMessageBox.Icon.Question,
            "未保存更改",
            (
                f"当前有未保存的更改。\n选择“应用更改”将{save_description}并留在应用中；选择“放弃更改”会清理临时文件并退出。"
                if exiting
                else f"是否{save_description}？"
            ),
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            self,
        )
        box.button(QMessageBox.StandardButton.Save).setText("应用更改")
        box.button(QMessageBox.StandardButton.Discard).setText("放弃更改")
        box.button(QMessageBox.StandardButton.Cancel).setText("取消")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        result = box.exec()
        if result == QMessageBox.StandardButton.Save:
            after_save = None if exiting else continuation
            if not external:
                self.save(after=after_save, quiet=exiting)
                return

            def stage_external(progress, cancel):
                updates = []
                for record in external:
                    stamp = self.file_stamp(record.path)
                    if stamp == (None,):
                        raise BackupError(f"外部编辑文件已被移动或删除：{record.path.name}")
                    self.session.stage_edit(record.token, record.path, cancel)
                    staged = Path(self.session.get(record.token).staged)
                    updates.append((record, digest(staged, cancel=cancel), stamp))
                return updates

            def staged(updates):
                for record, fingerprint, stamp in updates:
                    record.fingerprint, record.stamp = fingerprint, stamp
                    record.unreviewed = self.file_stamp(record.path) != stamp
                self.refresh()
                if any(record.unreviewed for record in external):
                    message(
                        self,
                        "外部文件仍在修改",
                        "请先在外部应用中保存文件，然后重试。",
                        QMessageBox.Icon.Warning,
                    )
                    return
                self.save(after=after_save, quiet=exiting)

            self.run_task("应用外部编辑", stage_external, staged)
        elif result == QMessageBox.StandardButton.Discard:
            continuation()

    def open_backup(self, *_):
        if self.task or self.copy_task or self._copy_pending:
            return
        path = QFileDialog.getExistingDirectory(self, "选择 iOS 备份文件夹")
        if path:
            self.confirm_leave(lambda: self.open_path(Path(path)))

    def open_path(self, path):
        if self._exit_requested or self.copy_task or self._copy_pending:
            return
        try:
            candidates = detect_backups(Path(path))
            if len(candidates) > 1:
                selected, ok = QInputDialog.getItem(
                    self,
                    "选择备份",
                    "此文件夹包含多个备份：",
                    [p.name for p in candidates],
                    0,
                    False,
                )
                if not ok:
                    return
                path = next(p for p in candidates if p.name == selected)
            else:
                path = candidates[0]
            encrypted = load_plist(path / "Manifest.plist").get("IsEncrypted", False)
        except Exception as exc:
            message(self, "无法打开备份", str(exc), QMessageBox.Icon.Warning)
            return
        password = ""
        if encrypted:
            dialog = PasswordDialog(self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            password = dialog.password.text()
            dialog.password.clear()
            dialog.deleteLater()

        def loaded(session):
            self.clear_opened_files()
            if self.session:
                self.session.close()
            self.session = session
            self.navigate(Location())

        self.run_task(
            "读取备份",
            lambda progress, cancel: BackupSession(
                Path(path), password, progress=progress, cancel=cancel
            ),
            loaded,
            status=True,
        )

    def clear_opened_files(self):
        self.images.clear_cache()
        paths = self.watcher.files() + self.watcher.directories()
        if paths:
            self.watcher.removePaths(paths)
        self.opened.clear()

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "添加文件到当前备份文件夹")
        if paths:
            self.import_paths(paths, self.domain, self.path)

    def add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "添加整个文件夹到当前备份文件夹")
        if path:
            self.import_paths([path], self.domain, self.path)

    def collision_policy(self, *, exporting=False):
        box = QMessageBox(
            QMessageBox.Icon.Question,
            "名称冲突",
            "目标包含同名项目，请选择处理方式。该选择应用于本次操作的全部同名项目。",
            parent=self,
        )
        replace = box.addButton(
            "替换 / 合并" if exporting else "替换", QMessageBox.ButtonRole.AcceptRole
        )
        both = box.addButton("保留两者", QMessageBox.ButtonRole.ActionRole)
        skip = box.addButton("跳过", QMessageBox.ButtonRole.ActionRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return {replace: "replace", both: "keep_both", skip: "skip"}.get(box.clickedButton())

    def import_paths(self, paths, domain, parent):
        if not self.session or domain is None:
            message(self, "选择目标文件夹", "请先进入可添加项目的文件夹")
            return
        conflict = any(
            self.session.at(domain, f"{parent}/{Path(p).name}".lstrip("/")) for p in paths
        )
        policy = self.collision_policy() if conflict else "error"
        if policy:
            self.run_task(
                "添加文件",
                lambda progress, cancel: self.session.import_paths(
                    paths, domain, parent, collision=policy, progress=progress, cancel=cancel
                ),
                lambda _: self.refresh(),
            )

    def new_folder(self):
        name, ok = QInputDialog.getText(self, "新建文件夹", "名称：")
        if ok and name:
            try:
                self.session.new_directory(self.domain, self.path, name)
                self.refresh()
            except BackupError as exc:
                message(self, "无法新建文件夹", str(exc), QMessageBox.Icon.Warning)

    def rename_selected(self):
        if self.sidebar_active():
            entry = self.directory_tree.current_entry()
            if entry:
                self.rename_entry(entry, focus_browser=False)
            return
        rows = self.selected_rows()
        if len(rows) != 1 or not rows[0].entry:
            return
        entry = rows[0].entry
        self.rename_entry(entry)

    def rename_entry(self, entry, *, focus_browser=True):
        if self.task or not entry.path:
            return
        name, ok = QInputDialog.getText(self, "重命名", "新名称：", text=entry.name)
        if ok and name:
            try:
                target = f"{entry.parent}/{name}".lstrip("/")
                existing = self.session.at(entry.domain, target)
                policy = (
                    self.collision_policy()
                    if existing and existing.token != entry.token
                    else "error"
                )
                if not policy:
                    return
                current = self.session.at(self.domain, self.path) if self.domain else None
                state = self.remember_view_state()
                self.session.move(
                    [entry.token], entry.domain, entry.parent, name=name, collision=policy
                )
                if current:
                    updated = self.session.get(current.token)
                    location = Location(updated.domain, updated.path)
                    if location != self.current_location():
                        self.domain, self.path, self.domain_group = (
                            location.domain,
                            location.path,
                            None,
                        )
                        if state:
                            self._view_states[location, self.stack.currentIndex()] = state
                            self._latest_view_states[location] = state
                self.refresh(focus_browser=focus_browser)
            except BackupError as exc:
                message(self, "无法重命名", str(exc), QMessageBox.Icon.Warning)

    def delete_selected(self):
        if self.sidebar_active():
            entry = self.directory_tree.current_entry()
            if entry:
                self.delete_entries([entry.token], entry.name, focus_browser=False)
            return
        tokens = self.selected_tokens()
        names = ", ".join(row.name for row in self.selected_rows() if row.entry)
        self.delete_entries(tokens, names)

    def delete_entries(self, tokens, names, *, focus_browser=True):
        if self.task:
            return
        if not tokens:
            return
        count = sum(len(self.session.subtree(e)) for e in self.session.roots(tokens))
        box = QMessageBox(
            QMessageBox.Icon.Question,
            "删除项目",
            "",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(
            f"删除 {names[:250]}？\n共影响 {count} 个文件和文件夹。保存备份副本前可以在更改列表中放弃此更改。"
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() == QMessageBox.StandardButton.Yes:
            self.run_task(
                "删除项目",
                lambda progress, cancel: self.session.delete(tokens),
                lambda _: self.refresh(focus_browser=focus_browser),
            )

    def pending_context_menu(self, point):
        if self.task or not self.session:
            return
        index = self.changes_list.indexAt(point)
        if not index.isValid():
            return
        self.changes_list.setCurrentIndex(index)
        token = index.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        revert = menu.addAction("放弃此更改")
        if menu.exec(self.changes_list.viewport().mapToGlobal(point)) == revert:
            self.revert_pending(token)

    def refresh_after_revert(self, directory_token):
        entry = self.session.find(directory_token) if directory_token else None
        if entry:
            self.domain, self.path, self.domain_group = entry.domain, entry.path, None
        self.refresh()

    def revert_pending(self, token=None):
        if self.task or not self.session:
            return
        session = self.session
        directory = session.at(self.domain, self.path) if self.domain else None

        def prepared(plan):
            if not plan.updates:
                return
            editors = [
                record for key, record in self.plist_editors.items() if key in plan.reset_contents
            ]
            external = [record for key, record in self.opened.items() if key in plan.reset_contents]
            dirty = any(record.editor.dirty for record in editors) or any(
                record.unreviewed or self.file_stamp(record.path) != record.stamp
                for record in external
            )
            if dirty:
                box = QMessageBox(
                    QMessageBox.Icon.Question,
                    "放弃此更改",
                    "相关编辑副本中有未保存的修改。丢弃这些编辑并放弃所选更改？",
                    QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                    self,
                )
                box.button(QMessageBox.StandardButton.Discard).setText("丢弃编辑并还原")
                box.button(QMessageBox.StandardButton.Cancel).setText("取消")
                box.setDefaultButton(QMessageBox.StandardButton.Cancel)
                if box.exec() != QMessageBox.StandardButton.Discard:
                    return
            for record in editors:
                record.editor.discard_and_close()
            for record in external:
                self.opened.pop(record.token, None)
                if str(record.path) in self.watcher.files():
                    self.watcher.removePath(str(record.path))
                if not any(r.path.parent == record.path.parent for r in self.opened.values()):
                    if str(record.path.parent) in self.watcher.directories():
                        self.watcher.removePath(str(record.path.parent))
            self.run_task(
                "放弃更改",
                lambda progress, cancel: session.revert(plan, progress=progress, cancel=cancel),
                lambda _: self.refresh_after_revert(directory.token if directory else None),
            )

        self.run_task(
            "准备放弃更改",
            lambda progress, cancel: session.plan_revert(token, cancel=cancel),
            prepared,
        )

    def discard(self):
        if self.task:
            return
        if (
            self.session
            and self.session.dirty
            and QMessageBox.question(
                self,
                "放弃所有更改",
                "恢复到最近一次保存后的会话状态？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.revert_pending()

    def extract_selected(self):
        if self.sidebar_active():
            return
        tokens = self.selected_tokens()
        if not tokens:
            return
        target = QFileDialog.getExistingDirectory(self, "导出到文件夹")
        if not target:
            return
        conflict = any((Path(target) / self.session.get(t).name).exists() for t in tokens)
        policy = self.collision_policy(exporting=True) if conflict else "error"
        if policy:
            self.run_task(
                "导出项目",
                lambda progress, cancel: self.session.export(
                    tokens, Path(target), collision=policy, progress=progress, cancel=cancel
                ),
                lambda _: self.statusBar().setToolTip(f"已导出到 {target}"),
            )

    def open_file(self, *, external=False):
        if self.task or not self.session or self.sidebar_active():
            return
        rows = self.selected_rows()
        if len(rows) != 1 or rows[0].kind != "file":
            return
        token = rows[0].entry.token
        # 明确指定系统应用时，先关闭可编辑窗口，沿用保存/放弃/取消保护。
        if external:
            record = self.plist_editors.get(token)
            if record and not record.editor.close():
                return
        else:
            if self.images.reuse(token):
                return
            if token in self.database_viewers:
                viewer = self.database_viewers[token].viewer
                viewer.showNormal()
                viewer.raise_()
                viewer.activateWindow()
                viewer.table.setFocus()
                return
            if token in self.plist_editors:
                editor = self.plist_editors[token].editor
                editor.showNormal()
                editor.raise_()
                editor.activateWindow()
                editor.tree.setFocus()
                self.check_plist_editors()
                return
        current = self.opened.get(token)
        if current and current.path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(current.path)))
            return
        session = self.session
        name = rows[0].name
        target = session.workspace / "opened" / uuid.uuid4().hex / name
        cache = self.images.session_cache() if not external else None
        resources = {"snapshot": None, "keep_file": False}
        revision = None

        def prepare_plist(source, cancel):
            data = source.read_bytes()
            return PlistDocument(data), hashlib.sha256(data).digest()

        def prepare_database(source, cancel):
            directory = session.workspace / "sqlite-viewer" / uuid.uuid4().hex
            snapshot = prepare_snapshot(session, token, directory, cancel=cancel, source=source)
            resources["snapshot"] = snapshot
            return snapshot

        def prepare_image(source, cancel):
            cache.adopt(token, target, revision, cancel)

        def prepare_external(source, cancel):
            if source != target:
                copy_file(source, target, cancel)
            return digest(target, cancel=cancel)

        # 识别器在 file_opening 登记；此处负责子系统的准备与交接。
        preparers = {
            "plist": prepare_plist,
            "sqlite": prepare_database,
            "image": prepare_image,
            "external": prepare_external,
        }

        def prepare(progress, cancel):
            nonlocal revision
            if external:
                session.extract_file(token, target, cancel=cancel)
                return "external", preparers["external"](target, cancel)
            with cache.session_lock(cancel):
                revision = source_revision(session, token)
                with cache.prepared_source(token, revision) as existing:
                    if existing and identify_file(existing) == "image":
                        return "image", None
                entry = session.get(token)
                source = Path(entry.staged) if entry.staged else session.cache_path(token)
                if not source.is_file():
                    session.extract_file(token, target, cancel=cancel)
                    source = target
                kind = identify_file(source)
                check_cancel(cancel)
                # 后缀仅用于无法识别内容时解释错误，不决定使用哪个查看器。
                if kind == "external":
                    if name.endswith(("-wal", "-shm", "-journal")):
                        raise BackupError("这是数据库的配套文件，请打开对应的主文件。")
                    if target.suffix.lower() in {
                        ".db",
                        ".sqlite",
                        ".sqlite3",
                        ".sqlitedb",
                        ".db3",
                        ".realm",
                        ".mdb",
                        ".ldb",
                    }:
                        raise BackupError(
                            "内置查看器只支持标准 SQLite；此文件可能使用其他格式或应用自身的加密。"
                            "可通过右键菜单选择“以十六进制查看”“使用系统应用打开”，或导出项目。"
                        )
                if kind != "image":
                    return kind, preparers[kind](source, cancel)
                if source != target:
                    copy_file(source, target, cancel)
            # 同图任务锁在会话锁之外获取，避免与图片导航线程互相等待。
            return kind, preparers[kind](target, cancel)

        def open_external(fingerprint):
            self.opened[token] = OpenedFile(token, target, fingerprint, self.file_stamp(target))
            resources["keep_file"] = True
            self.watcher.addPath(str(target))
            self.watcher.addPath(str(target.parent))
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
                message(
                    self,
                    "无法启动默认应用",
                    f"系统没有成功打开此文件。临时文件位于：\n{target}",
                    QMessageBox.Icon.Warning,
                )

        def open_database(snapshot):
            self.show_database(token, snapshot)
            resources["snapshot"] = None

        openers = {
            "plist": lambda result: self.open_plist(token, name, prepared=result),
            "sqlite": open_database,
            "image": lambda _: self.images.open(token),
            "external": open_external,
        }

        def opened(result):
            kind, payload = result
            openers[kind](payload)

        def cleanup():
            if not resources["keep_file"]:
                shutil.rmtree(target.parent, ignore_errors=True)
            if resources["snapshot"]:
                shutil.rmtree(resources["snapshot"].directory, ignore_errors=True)

        self.run_task("读取并打开文件", prepare, opened, silent=True, on_done=cleanup)

    def database_revision(self, token):
        return tuple(
            (entry.token, self.plist_revision(entry.token))
            for entry in companions(self.session, token)
        )

    def show_database(self, token, snapshot):
        session = self.session
        viewer = SQLiteViewer(snapshot, session.get(token).name, self)
        record = OpenedDatabase(token, viewer, snapshot, self.database_revision(token))
        self.database_viewers[token] = record
        viewer.reload_requested.connect(lambda: self.reload_database(record))

        def closed(_):
            self.database_viewers.pop(token, None)
            shutil.rmtree(record.snapshot.directory, ignore_errors=True)
            if not viewer._skip_prompt:
                self.restore_browser_after_editor(session, viewer)
            viewer.deleteLater()

        viewer.finished.connect(closed)
        self.panes.activate(self.active_view(), focus=True)
        viewer.show()

    def close_readonly_viewers(self, after):
        if self._readonly_closing:
            return
        self._readonly_closing = True

        def closed(_):
            if not self.database_viewers and not self.hex_viewers and not self.image_viewers:
                self._readonly_closing = False
                QTimer.singleShot(0, after)

        viewers = [
            record.viewer
            for record in list(self.database_viewers.values()) + list(self.hex_viewers.values())
        ] + list(self.image_viewers)
        for viewer in viewers:
            viewer._skip_prompt = True
            viewer.finished.connect(closed)
            viewer.reject()

    def open_hex(self):
        if self.task or not self.session or self.sidebar_active():
            return
        rows = self.selected_rows()
        if len(rows) != 1 or rows[0].kind != "file":
            return
        token = rows[0].entry.token
        if token in self.hex_viewers:
            viewer = self.hex_viewers[token].viewer
            viewer.showNormal()
            viewer.raise_()
            viewer.activateWindow()
            viewer.table.setFocus()
            return
        session = self.session
        path = session.workspace / "hex-viewer" / uuid.uuid4().hex / "snapshot.bin"

        def prepare(progress, cancel):
            session.extract_file(token, path, cancel=cancel)
            return digest(path, cancel=cancel)

        def opened(fingerprint):
            viewer = HexViewer(path, session.get(token).name, self)
            record = OpenedHex(
                token, viewer, path, fingerprint, fingerprint, self.plist_revision(token)
            )
            self.hex_viewers[token] = record
            viewer.reload_requested.connect(lambda: self.reload_hex(record))

            def closed(_):
                self.hex_viewers.pop(token, None)
                shutil.rmtree(record.path.parent, ignore_errors=True)
                if not viewer._skip_prompt:
                    self.restore_browser_after_editor(session, viewer)
                viewer.deleteLater()

            viewer.finished.connect(closed)
            self.panes.activate(self.active_view(), focus=True)
            viewer.show()

        def cleanup():
            if token not in self.hex_viewers:
                shutil.rmtree(path.parent, ignore_errors=True)

        self.run_task("读取十六进制快照", prepare, opened, on_done=cleanup)

    def reload_hex(self, record, *, automatic=False):
        if record.detached:
            return
        if self.task or not self.session or record.viewer.task or record.viewer._closing:
            return
        session = self.session
        try:
            revision = self.plist_revision(record.token)
        except BackupError:
            record.viewer.set_source_state(available=False)
            return
        path = session.workspace / "hex-viewer" / uuid.uuid4().hex / "snapshot.bin"

        def prepare(progress, cancel):
            session.extract_file(record.token, path, cancel=cancel)
            return digest(path, cancel=cancel)

        def loaded(fingerprint):
            if self.hex_viewers.get(record.token) is not record:
                return
            record.revision = revision
            changed = fingerprint != record.source_digest
            prompt = changed and fingerprint != record.observed_digest
            record.observed_digest = fingerprint
            record.viewer.set_source_state(stale=changed)
            accept = not automatic
            if automatic and prompt:
                accept = (
                    QMessageBox.question(
                        record.viewer,
                        "文件已更改",
                        "源文件的内容已更改，是否重新加载更新版本？",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    == QMessageBox.StandardButton.Yes
                )
            if accept and not record.viewer.task and not record.viewer._closing:
                previous = record.path
                record.path, record.source_digest = path, fingerprint
                record.viewer.replace_snapshot(path, session.get(record.token).name)
                shutil.rmtree(previous.parent, ignore_errors=True)

        def failed(exc):
            record.revision = revision
            if self.hex_viewers.get(record.token) is record:
                record.viewer.set_source_state(stale=True)
                record.viewer.status.setText(f"无法重新加载文件：{exc}")

        def cleanup():
            if self.hex_viewers.get(record.token) is not record or record.path != path:
                shutil.rmtree(path.parent, ignore_errors=True)

        self.run_task(
            "读取十六进制快照", prepare, loaded, silent=automatic, on_error=failed, on_done=cleanup
        )

    def check_hex_viewers(self):
        for record in list(self.hex_viewers.values()):
            if record.detached or record.viewer.task or record.viewer._closing:
                continue
            try:
                entry = self.session.get(record.token)
                revision = self.plist_revision(record.token)
            except BackupError:
                record.viewer.set_source_state(available=False)
                continue
            if record.viewer.name != entry.name:
                record.viewer.name = entry.name
                record.viewer.setWindowTitle(f"{entry.name} — 十六进制查看器（只读）")
            if not record.viewer.available:
                record.viewer.set_source_state(stale=record.viewer.stale)
            if revision != record.revision:
                self.reload_hex(record, automatic=True)
                return True
        return False

    def open_data_hex(self, data, identity, label, *, owner=None):
        if self.task or not self.session:
            self.statusBar().setToolTip("备份操作进行中，请稍后再查看 Data。")
            return
        session = self.session
        path = session.workspace / "hex-viewer" / uuid.uuid4().hex / "data.bin"
        retained = False
        owner_ref = weakref.ref(owner) if owner is not None else lambda: None

        def prepare(progress, cancel):
            path.parent.mkdir(parents=True, exist_ok=True)
            fingerprint = hashlib.sha256()
            with path.open("wb") as stream:
                view = memoryview(data)
                for offset in range(0, len(view), CHUNK):
                    check_cancel(cancel)
                    block = view[offset : offset + CHUNK]
                    fingerprint.update(block)
                    stream.write(block)
            check_cancel(cancel)
            return fingerprint.digest()

        def opened(fingerprint):
            nonlocal retained
            token = f"plist-data:{identity}:{fingerprint.hex()}"
            if token in self.hex_viewers:
                viewer = self.hex_viewers[token].viewer
                viewer.name = label
                viewer.setWindowTitle(f"{label} — 十六进制查看器（只读）")
                viewer.showNormal()
                viewer.raise_()
                viewer.activateWindow()
                viewer.table.setFocus()
                return
            viewer = HexViewer(path, label, self)
            viewer.reload_button.defaultAction().setVisible(False)
            viewer.status.setText("plist Data 的只读快照；重新从 plist 打开可查看更新后的内容。")
            record = OpenedHex(token, viewer, path, fingerprint, fingerprint, (), detached=True)
            self.hex_viewers[token] = record
            retained = True

            def closed(_):
                self.hex_viewers.pop(token, None)
                shutil.rmtree(path.parent, ignore_errors=True)
                if not viewer._skip_prompt:
                    target = owner_ref()
                    if target is not None and any(
                        r.editor is target for r in self.plist_editors.values()
                    ):

                        def restore():
                            if (
                                any(r.editor is target for r in self.plist_editors.values())
                                and target.isVisible()
                                and not target.isMinimized()
                                and not QApplication.activeModalWidget()
                                and QApplication.activeWindow() in (None, self, viewer, target)
                            ):
                                target.activateWindow()
                                (
                                    target.tree
                                    if target.pages.currentIndex() == 0
                                    else target.xml.code
                                ).setFocus()

                        QTimer.singleShot(0, restore)
                    else:
                        self.restore_browser_after_editor(session, viewer)
                viewer.deleteLater()

            viewer.finished.connect(closed)
            viewer.show()

        def cleanup():
            if not retained:
                shutil.rmtree(path.parent, ignore_errors=True)

        self.run_task("读取 Data 十六进制快照", prepare, opened, on_done=cleanup)

    def reload_database(self, record, *, automatic=False):
        if self.task or not self.session or record.viewer.task or record.viewer._closing:
            return
        session = self.session
        try:
            revision = self.database_revision(record.token)
        except BackupError:
            record.viewer.set_source_state(available=False)
            return
        directory = session.workspace / "sqlite-viewer" / uuid.uuid4().hex

        def loaded(snapshot):
            if record.token not in self.database_viewers:
                shutil.rmtree(snapshot.directory, ignore_errors=True)
                return
            record.revision = revision
            changed = snapshot.fingerprint != record.snapshot.fingerprint
            record.viewer.set_source_state(stale=changed)
            accept = not automatic
            if automatic and changed:
                accept = (
                    QMessageBox.question(
                        record.viewer,
                        "数据库已更改",
                        "源数据库的内容已更改，是否重新加载更新版本？",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    == QMessageBox.StandardButton.Yes
                )
            if not accept or record.viewer.task or record.viewer._closing:
                shutil.rmtree(snapshot.directory, ignore_errors=True)
                return
            previous = record.snapshot
            record.snapshot = snapshot
            record.viewer.replace_snapshot(snapshot, session.get(record.token).name)
            shutil.rmtree(previous.directory, ignore_errors=True)

        def failed(exc):
            record.revision = revision
            if record.token in self.database_viewers:
                record.viewer.set_source_state(stale=True)
                record.viewer.status.setText(f"无法重新加载数据库：{exc}")

        self.run_task(
            "读取数据库快照",
            lambda progress, cancel: prepare_snapshot(
                session, record.token, directory, cancel=cancel
            ),
            loaded,
            silent=automatic,
            on_error=failed,
        )

    def check_database_viewers(self):
        for record in list(self.database_viewers.values()):
            if record.viewer.task or record.viewer._closing:
                continue
            try:
                entry = self.session.get(record.token)
                revision = self.database_revision(record.token)
            except BackupError:
                record.viewer.set_source_state(available=False)
                continue
            if record.viewer.name != entry.name:
                record.viewer.name = entry.name
                record.viewer.setWindowTitle(f"{entry.name} — SQLite 查看器（只读）")
            if not record.viewer.available:
                record.viewer.set_source_state(stale=record.viewer.stale)
                record.viewer.status.setText("源文件已恢复可用。")
            if revision != record.revision:
                self.reload_database(record, automatic=True)
                return True
        return False

    def plist_revision(self, token):
        entry = self.session.get(token)
        source = (
            Path(entry.staged) if entry.staged else self.session.payload_path(entry.original_id)
        )
        return (str(source), self.file_stamp(source))

    def open_plist(self, token, name, *, prepared=None):
        session = self.session
        directory = session.workspace / "plist-editor" / uuid.uuid4().hex

        def prepare(progress, cancel):
            directory.mkdir(parents=True, exist_ok=True)
            target = session.cached_file(token, cancel=cancel)
            data = target.read_bytes()
            return PlistDocument(data), hashlib.sha256(data).digest()

        def opened(result):
            document, fingerprint = result

            def stage(data):
                if self.task or self.session is not session:
                    raise BackupError("备份操作进行中，请稍后再保存")
                latest = directory / "before-save.plist"
                try:
                    session.extract_file(token, latest)
                    current = latest.read_bytes()
                finally:
                    latest.unlink(missing_ok=True)
                current_digest = hashlib.sha256(current).digest()
                if current_digest != record.source_digest and current != data:
                    box = QMessageBox(
                        QMessageBox.Icon.Question,
                        "plist 文件已更改",
                        "备份中的文件已有更新。请选择重新加载，或确认用当前编辑覆盖更新版本。",
                        parent=editor,
                    )
                    reload_button = box.addButton("重新加载", QMessageBox.ButtonRole.AcceptRole)
                    overwrite = box.addButton(
                        "覆盖更新版本", QMessageBox.ButtonRole.DestructiveRole
                    )
                    cancel_button = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
                    box.setDefaultButton(cancel_button)
                    box.exec()
                    record.observed_digest = current_digest
                    record.revision = self.plist_revision(token)
                    editor.set_source_state(conflict=True)
                    if box.clickedButton() == reload_button:
                        self.reload_plist(record, current)
                        return False
                    if box.clickedButton() != overwrite:
                        return False
                if current != data:
                    session.stage_bytes(token, data)
                record.source_digest = record.observed_digest = hashlib.sha256(data).digest()
                record.revision = self.plist_revision(token)
                editor.set_source_state()
                self.refresh(focus_browser=False)
                return True

            editor = PlistEditor(document, name, stage, self)
            editor.data_hex_requested.connect(
                lambda data, identity, label: self.open_data_hex(
                    data, identity, label, owner=editor
                )
            )
            record = OpenedPlist(
                token, editor, directory, fingerprint, fingerprint, self.plist_revision(token)
            )
            self.plist_editors[token] = record
            editor.contents_changed.connect(self.update_actions)

            def closed(_):
                self.plist_editors.pop(token, None)
                shutil.rmtree(directory, ignore_errors=True)
                if not editor._skip_prompt:
                    self.restore_browser_after_editor(session, editor)
                editor.deleteLater()
                self.update_actions()

            editor.finished.connect(closed)
            self.panes.activate(self.active_view(), focus=True)
            editor.show()

        def cleanup():
            if token not in self.plist_editors:
                shutil.rmtree(directory, ignore_errors=True)

        if prepared is None:
            self.run_task("读取 plist", prepare, opened, on_done=cleanup)
        else:
            directory.mkdir(parents=True, exist_ok=True)
            try:
                opened(prepared)
            finally:
                cleanup()

    def restore_browser_after_editor(self, session, editor):
        def restore():
            if (
                self.force_close
                or self._exit_requested
                or self.task
                or self.session is not session
                or not self.isVisible()
                or self.isMinimized()
                or QApplication.activeModalWidget()
                or QApplication.activePopupWidget()
                or QApplication.activeWindow() not in (None, self, editor)
            ):
                return
            focused = QApplication.focusWidget()
            if (
                focused
                and self.isAncestorOf(focused)
                and self.panes.view_for_widget(focused) is None
            ):
                return
            tree = self.panes.preferred_view is self.directory_tree
            view = (
                self.directory_tree
                if tree and not self.directory_tree.isHidden()
                else self.active_view()
            )
            self.activateWindow()
            self.panes.activate(view, focus=True, explicit=False)

        # Let QDialog finish its native focus restoration before choosing the
        # last intentional pane, rather than the parent's first tab-stop widget.
        QTimer.singleShot(0, restore)

    def reload_plist(self, record, data):
        try:
            document = PlistDocument(data)
        except Exception as exc:
            record.editor.show_error(exc)
            return False
        record.source_digest = record.observed_digest = hashlib.sha256(data).digest()
        record.editor.reload_document(document)
        return True

    def check_plist_editors(self):
        if self.task or not self.session or self.force_close or QApplication.activeModalWidget():
            return False
        for record in list(self.plist_editors.values()):
            try:
                entry = self.session.get(record.token)
                revision = self.plist_revision(record.token)
            except BackupError:
                record.editor.set_source_state(available=False)
                continue
            record.editor.name = entry.name
            record.editor.set_source_state(conflict=record.editor._conflict)
            if revision == record.revision:
                continue
            target = record.directory / "latest.plist"
            session = self.session

            def read(progress, cancel, rec=record, path=target):
                try:
                    session.extract_file(rec.token, path, cancel=cancel)
                    return path.read_bytes()
                finally:
                    path.unlink(missing_ok=True)

            def checked(data, rec=record, version=revision):
                if rec.token not in self.plist_editors:
                    return
                try:
                    if self.plist_revision(rec.token) != version:
                        return
                except BackupError:
                    return
                rec.revision = version
                fingerprint = hashlib.sha256(data).digest()
                if fingerprint == rec.observed_digest:
                    return
                rec.observed_digest = fingerprint
                if fingerprint == rec.source_digest:
                    rec.editor.set_source_state()
                    return
                rec.editor.set_source_state(conflict=True)
                text = "这份 plist 的内容已更改，是否重新加载更新版本？"
                if rec.editor.dirty:
                    text += "\n重新加载将丢弃编辑器中未保存的修改。"
                answer = QMessageBox.question(
                    rec.editor,
                    "plist 文件已更改",
                    text,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self.reload_plist(rec, data)

            def failed(exc, rec=record, version=revision):
                rec.revision = version
                rec.editor.show_error(exc)

            self.run_task("检查 plist 更新", read, checked, silent=True, on_error=failed)
            return True
        return False

    @staticmethod
    def file_stamp(path):
        try:
            st = path.stat()
            return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
        except FileNotFoundError:
            return (None,)

    def _file_event(self, path):
        for record in self.opened.values():
            if str(record.path) == path:
                record.stable = 0
                record.unreviewed = self.file_stamp(record.path) != record.stamp

    def _directory_event(self, path):
        for record in self.opened.values():
            if str(record.path.parent) == path:
                self._file_event(str(record.path))

    def poll_open_files(self):
        if self.task or not self.session or QApplication.activeModalWidget():
            return
        if self.check_plist_editors():
            return
        if self.check_database_viewers():
            return
        if self.check_hex_viewers():
            return
        for record in list(self.opened.values()):
            stamp = self.file_stamp(record.path)
            if stamp == record.stamp:
                continue
            record.unreviewed = True
            if record.seen != stamp:
                record.seen, record.stable = stamp, 0
                continue
            record.stable += 1
            if record.stable < 1 or time.monotonic() < record.later_until:
                continue
            if stamp == (None,):
                choice = QMessageBox.question(
                    self,
                    "临时文件已删除",
                    "外部应用删除或移动了临时文件。停止跟踪此副本？备份中的原文件不会因此删除。",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if choice == QMessageBox.StandardButton.Yes:
                    self.opened.pop(record.token, None)
                else:
                    record.later_until = time.monotonic() + 30
                return
            if str(record.path) not in self.watcher.files():
                self.watcher.addPath(str(record.path))

            def checked(fingerprint, rec=record, observed=stamp):
                if self.file_stamp(rec.path) != observed:
                    rec.seen, rec.stable = None, 0
                    return
                if fingerprint == rec.fingerprint:
                    rec.stamp, rec.unreviewed = observed, False
                    return
                self.review_external(rec, fingerprint, observed)

            self.run_task(
                "检查外部编辑",
                lambda progress, cancel, rec=record: digest(rec.path, cancel=cancel),
                checked,
                silent=True,
            )
            return

    def review_external(self, record, fingerprint, stamp):
        box = QMessageBox(QMessageBox.Icon.Question, "检测到外部编辑", "", parent=self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(f"{record.path.name} 已修改。是否将新内容暂挂到备份会话？")
        stage = box.addButton("暂挂更改", QMessageBox.ButtonRole.AcceptRole)
        ignore = box.addButton("忽略本次修改", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("稍后处理", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() == stage:

            def staged(_):
                record.fingerprint, record.stamp, record.unreviewed = fingerprint, stamp, False
                self.refresh()

            self.run_task(
                "暂挂外部编辑",
                lambda progress, cancel: self.session.stage_edit(record.token, record.path, cancel),
                staged,
            )
        elif box.clickedButton() == ignore:
            record.fingerprint, record.stamp, record.unreviewed = fingerprint, stamp, False
        else:
            record.later_until = time.monotonic() + 30

    def save(self, *_args, after=None, quiet=False):
        if not self.session or self.task or self.copy_task or self._copy_pending:
            return
        for record in list(self.plist_editors.values()):
            if record.editor.dirty and not record.editor.save_document():
                return
        session = self.session
        if not session.dirty:
            return
        target = None
        direct = session.editable
        if not direct:
            suggested = str(session.root.parent / (session.root.name + "-edited"))
            target, _ = QFileDialog.getSaveFileName(
                self, "创建备份副本并应用更改", suggested, "备份文件夹 (*)"
            )
            if not target:
                return
        source_root = session.root
        try:
            token = session.begin_apply()  # 在线程启动/弹窗事件循环之前关闭数据层暂挂入口。
        except BackupError as exc:
            message(self, "无法应用更改", str(exc), QMessageBox.Icon.Warning)
            return

        def saved(destination):
            self.refresh()
            if not quiet:
                text = f"更改已应用到：\n{destination}"
                if not direct:
                    text += f"\n\n原备份保留在：\n{source_root}"
                message(self, "应用完成", text)
            if after:
                after()

        self.run_task(
            "应用更改",
            lambda progress, cancel: session.save(
                Path(target) if target else None, progress=progress, cancel=cancel, _token=token
            ),
            saved,
            on_done=lambda: session.end_apply(token),
        )

    def context_menu(self, view, point: QPoint):
        self.panes.activate(view, focus=True)
        index = view.indexAt(point)
        if not index.isValid():
            view.clearSelection()
        if index.isValid() and not view.selectionModel().isSelected(index):
            view.clearSelection()
            view.setCurrentIndex(index)
            view.selectionModel().select(
                index,
                view.selectionModel().SelectionFlag.Select
                | view.selectionModel().SelectionFlag.Rows,
            )
        self.update_actions()
        menu = QMenu(self)
        if not index.isValid():
            menu.addActions([self.add_action, self.add_folder_action, self.new_folder_action])
            menu.exec(view.viewport().mapToGlobal(point))
            return
        if self.model.rows[index.row()].kind in (
            "group",
            "domain",
            "directory",
            "parent",
        ):
            action = menu.addAction("打开文件夹")
            action.triggered.connect(lambda: self.activate(index))
        else:
            menu.addAction(self.open_action)
        menu.addAction(self.extract_action)
        rows = self.selected_rows()
        if len(rows) == 1 and rows[0].kind == "file":
            hex_action = menu.addAction("以十六进制查看")
            hex_action.setEnabled(not self.task)
            hex_action.triggered.connect(self.open_hex)
            external = menu.addAction("使用系统应用打开")
            external.setEnabled(not self.task)
            external.triggered.connect(lambda: self.open_file(external=True))
        menu.addSeparator()
        menu.addActions([self.rename_action, self.delete_action])
        menu.exec(view.viewport().mapToGlobal(point))

    def drop_location(self, index):
        domain, parent = self.domain, self.path
        if index.isValid():
            row = self.model.rows[index.row()]
            if row.kind in ("domain", "directory"):
                domain, parent = row.domain, row.path
            elif row.kind == "parent":
                parent_location = self.current_location().parent
                domain, parent = parent_location.domain, parent_location.path
            elif row.kind == "group":
                domain, parent = None, ""
        return domain, parent

    def queue_drop(self, mime, index):
        # 在原生拖放回调中只复制数据并确认投递。模态对话框/模型更新必须
        # 等 dropEvent 返回，不能跨嵌套事件循环持有平台借出的事件和 MIME。
        if self.task or self._drop_queued or self.force_close or self._exit_requested:
            return None
        internal = mime.hasFormat(INTERNAL_MIME)
        if not internal and not mime.hasUrls():
            return None
        snapshot = QMimeData(self)
        if internal:
            # 不能读取内部拖放的 URL：ExportMimeData 会为外部消费者即时导出。
            snapshot.setData(INTERNAL_MIME, bytes(mime.data(INTERNAL_MIME)))
        else:
            snapshot.setUrls(mime.urls())
        target = self.drop_location(index)
        session = self.session
        self._drop_queued = True

        def consume():
            try:
                if (
                    self.session is session
                    and not self.task
                    and not self.force_close
                    and not self._exit_requested
                ):
                    self.handle_drop(snapshot, QModelIndex(), target=target)
            finally:
                self._drop_queued = False
                snapshot.deleteLater()

        QTimer.singleShot(0, self, consume)
        return Qt.DropAction.MoveAction if internal else Qt.DropAction.CopyAction

    def handle_drop(self, mime, index, *, target=None):
        if self.task:
            return False
        domain, parent = target if target is not None else self.drop_location(index)
        if self.session is None:
            if mime.hasUrls() and len(mime.urls()) == 1:
                self.open_path(Path(mime.urls()[0].toLocalFile()))
                return True
            return False
        if domain is None:
            message(self, "选择目标文件夹", "请将项目拖入可添加项目的文件夹")
            return False
        if mime.hasFormat(INTERNAL_MIME):
            try:
                content = json.loads(bytes(mime.data(INTERNAL_MIME)))
                if content["session"] != self.session.session_id:
                    raise BackupError("不支持从其他会话直接移动；请先导出文件再添加")
                tokens = content["tokens"]
                entries = self.session.roots(tokens)
                conflict = any(
                    self.session.at(domain, f"{parent}/{e.name}".lstrip("/"))
                    and (domain, f"{parent}/{e.name}".lstrip("/")) != (e.domain, e.path)
                    for e in entries
                )
                policy = self.collision_policy() if conflict else "error"
                if not policy:
                    return False
                self.run_task(
                    "移动项目",
                    lambda progress, cancel: self.session.move(
                        tokens, domain, parent, collision=policy
                    ),
                    lambda _: self.refresh(),
                )
                return True
            except (ValueError, KeyError, TypeError, BackupError) as exc:
                message(self, "无法移动项目", str(exc), QMessageBox.Icon.Warning)
                return False
        paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
        if paths:
            self.import_paths(paths, domain, parent)
            return True
        return False

    def start_drag(self, view):
        tokens = self.selected_tokens()
        if not tokens or not self.session or self.task:
            return
        drag = QDrag(view)
        drag.setMimeData(ExportMimeData(self, tokens))
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction, Qt.DropAction.CopyAction)

    def drag_export(self, tokens):
        if self.task or not self.session:
            return []
        target = self.session.workspace / "exports" / uuid.uuid4().hex
        loop = QEventLoop()
        result = []

        def done(paths):
            result.extend(paths)

        self.run_task(
            "准备拖出文件",
            lambda progress, cancel: self.session.export(
                tokens, target, progress=progress, cancel=cancel
            ),
            done,
            on_done=loop.quit,
        )
        loop.exec()
        return result

    def closeEvent(self, event):
        if self.copy_task:
            event.ignore()
            self._copy_exit_requested = True
            self.copy_task.cancelled.set()
            return
        self._copy_pending = None
        if self.force_close:
            if self.database_viewers or self.hex_viewers or self.image_viewers:
                event.ignore()
                self.close_readonly_viewers(self.close)
                return
            self.panes.stopped = True
            for record in list(self.plist_editors.values()):
                record.editor.discard_and_close()
            self.remember_sidebar_width()
            self.settings.setValue("sidebar_width", self._sidebar_width)
            self.settings.setValue("sidebar_visible", self.sidebar_action.isChecked())
            self.settings.setValue("icons", self.icon_slider.value())
            self.settings.setValue("sort", self.sort_combo.currentIndex())
            self.settings.setValue("descending", self.descending.isChecked())
            self.settings.setValue("directories_first", self.directories_first.isChecked())
            self.poll_timer.stop()
            self.clear_opened_files()
            self.clear_navigation_state()
            self.directory_tree.sync(None, Location())
            if self.session:
                self.session.close()
                self.session = None
            event.accept()
            return
        event.ignore()
        if self.task:
            self._exit_requested = True
            self.task.cancelled.set()
            return

        def leave():
            self.force_close = True
            QTimer.singleShot(0, self.close)

        self.confirm_leave(leave, exiting=True)

    def offer_temp_cleanup(self, after=None):
        paths = stale_workspaces()
        if not paths:
            if after:
                after()
            return

        def cleanup(progress, cancel):
            # 只清理已结束且仍满足私有会话标记的工作区，不触碰活动实例。
            valid = set(stale_workspaces())
            for number, path in enumerate(paths, 1):
                check_cancel(cancel)
                if path in valid:
                    progress("清理旧会话缓存…", number, len(paths))
                    shutil.rmtree(path)

        self.run_task("清理旧会话缓存", cleanup, status=True, on_done=after)


def main():
    parser = argparse.ArgumentParser(description="离线浏览与编辑 iOS 备份")
    parser.add_argument("backup", nargs="?", type=Path, help="备份文件夹（可选）")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("BackupEditor")
    app.setApplicationVersion(__version__)
    configure_system_appearance(app)
    window = MainWindow()
    window.show()
    QTimer.singleShot(
        0,
        lambda: window.offer_temp_cleanup(
            (lambda: window.open_path(args.backup)) if args.backup else None
        ),
    )
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
