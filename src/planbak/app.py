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
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QByteArray,
    QEventLoop,
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
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .about import AboutDialog
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
from .backup_windows import BackupWindows
from .codec import BackupError, Cancelled, check_cancel
from .dialogs import FixedDialog as QDialog
from .dialogs import FixedFileDialog as QFileDialog
from .dialogs import FixedInputDialog as QInputDialog
from .dialogs import FixedMessageBox as QMessageBox
from .dialogs import FixedProgressDialog
from .external_edits import ExternalEdits, file_stamp
from .file_opening import identify_file, source_revision
from .i18n import ENGLISH, SIMPLIFIED_CHINESE, LanguageContext
from .icons import ApplicationIcons
from .image_session import ImageWindows
from .navigation import Location, ViewState, split_domain
from .pane_focus import BrowserPaneFocus
from .pending_view import PendingChangesModel
from .plist_editor import PlistDocument, PlistEditor
from .readonly_windows import ReadOnlyWindows
from .sidebar import DirectoryTree
from .sqlite_database import prepare_snapshot
from .transfers import OutgoingTransfers

INTERNAL_MIME = "application/x-planbak-entries"


def human_size(size):
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


def message(parent, title, text, icon=QMessageBox.Icon.Information):
    tr = parent.language_context.translate
    titles = {
        "文件夹不存在": "Folder Not Found",
        "无法创建备份副本": "Unable to Create Backup Copy",
        "无法切换副本": "Unable to Switch Copies",
        "备份已打开": "Backup Already Open",
        "无法打开备份": "Unable to Open Backup",
        "无法完成清理": "Unable to Complete Cleanup",
        "选择目标文件夹": "Choose a Destination Folder",
        "无法新建文件夹": "Unable to Create Folder",
        "无法重命名": "Unable to Rename",
        "无法启动默认应用": "Unable to Open Default Application",
        "无法应用更改": "Unable to Apply Changes",
        "应用完成": "Changes Applied",
        "无法处理拖放项目": "Unable to Handle Dropped Items",
        "外部文件仍在修改": "External File Still Changing",
        "读取备份": "Read Backup",
        "打开文件": "Open File",
        "打开项目": "Open Item",
        "添加项目": "Add Items",
        "删除项目": "Delete Items",
        "导出项目": "Export Items",
        "应用更改": "Apply Changes",
        "移动项目": "Move Items",
        "关闭备份并清理缓存": "Close Backup and Clear Cache",
        "切换到备份副本": "Switch to Backup Copy",
        "应用外部编辑": "Apply External Edits",
        "准备放弃更改": "Prepare to Discard Changes",
        "放弃更改": "Discard Changes",
        "重新加载文件": "Reload File",
        "保存外部编辑": "Save External Edits",
        "复制项目到当前备份": "Copy Items to This Backup",
        "清理旧会话缓存": "Clean Old Session Cache",
    }
    messages = {
        "此备份已在另一个窗口中打开。": "This backup is already open in another window.",
        "请先进入可添加项目的文件夹": "Open a folder where items can be added first.",
        "请将项目拖入可添加项目的文件夹": "Drop items into a folder where they can be added.",
        "请先在外部应用中保存文件，然后重试。": "Save the file in the external application, then try again.",
    }
    title = tr(titles[title]) if title in titles else title
    text = (
        parent.language_context.error(text)
        if isinstance(text, Exception)
        else tr(messages[text])
        if text in messages
        else text
    )
    box = QMessageBox(icon, title, "", parent=parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    box.exec()


class Task(QThread):
    progress = Signal(str, object, object)

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
        if self.language_context is None:
            self.language_context = LanguageContext(parent=self)
        layout = QVBoxLayout(self)
        self.prompt = QLabel(self)
        layout.addWidget(self.prompt)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self.password)
        self.reveal = QCheckBox(self)
        self.reveal.toggled.connect(
            lambda enabled: self.password.setEchoMode(
                QLineEdit.EchoMode.Normal if enabled else QLineEdit.EchoMode.Password
            )
        )
        layout.addWidget(self.reveal)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.language_context.changed.connect(self.retranslate)
        self.retranslate()
        self.lock_size(QSize(380, 180))

    def retranslate(self):
        tr = self.language_context.translate
        self.setWindowTitle(tr("Unlock Encrypted Backup"))
        self.prompt.setText(tr("Enter the password for this backup:"))
        self.password.setAccessibleName(tr("Backup password"))
        self.reveal.setText(tr("Show password"))
        self.ok_button.setText(tr("OK"))
        self.cancel_button.setText(tr("Cancel"))


class BackupChoiceDialog(QDialog):
    def __init__(self, candidates, parent=None):
        super().__init__(parent)
        if self.language_context is None:
            self.language_context = LanguageContext(parent=self)
        self.candidates = candidates
        self._rows = []
        layout = QVBoxLayout(self)
        self.prompt = QLabel(self)
        layout.addWidget(self.prompt)
        self.table = QTableWidget(len(candidates), 3, self)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for row, path in enumerate(candidates):
            try:
                info = load_plist(path / "Info.plist")
            except (OSError, BackupError):
                info = {}
            name = info.get("Device Name") or info.get("Display Name") or None
            date = info.get("Last Backup Date")
            for metadata in ("Status.plist", "Manifest.plist"):
                if isinstance(date, datetime):
                    break
                try:
                    date = load_plist(path / metadata).get("Date")
                except (OSError, BackupError):
                    date = None
            self._rows.append(
                (
                    str(name) if name is not None else None,
                    date.strftime("%Y-%m-%d %H:%M:%S") if isinstance(date, datetime) else None,
                    path.name,
                )
            )
            for column in range(3):
                self.table.setItem(row, column, QTableWidgetItem())
        self.table.selectRow(0)
        self.table.cellDoubleClicked.connect(lambda *_: self.accept())
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.language_context.changed.connect(self.retranslate)
        self.retranslate()
        self.lock_size(QSize(680, 360))

    def retranslate(self):
        tr = self.language_context.translate
        self.setWindowTitle(tr("Choose Backup"))
        self.prompt.setText(tr("Choose a backup to open:"))
        self.table.setHorizontalHeaderLabels(
            [tr("Device Name"), tr("Backup Date"), tr("Folder Name")]
        )
        for row, values in enumerate(self._rows):
            for column, value in enumerate(values):
                item = self.table.item(row, column)
                text = value if value is not None else tr("Unknown")
                item.setText(text)
                item.setToolTip(text)
        self.ok_button.setText(tr("OK"))
        self.cancel_button.setText(tr("Cancel"))

    def selected_path(self):
        row = self.table.currentRow()
        return self.candidates[row] if row >= 0 else None


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
            return self.parent().language_context.translate(
                ("Name", "Type", "Size", "Modified", "Status")[section]
            )
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
                if row.kind in ("domain", "group", "directory"):
                    return self.parent().language_context.translate("Folder")
                if row.kind == "parent":
                    return self.parent().language_context.translate("Parent Folder")
                if row.kind == "special":
                    return self.parent().language_context.translate("Link / Special Item")
                return Path(row.name).suffix.lstrip(
                    "."
                ).upper() or self.parent().language_context.translate("File")
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
                return (
                    self.parent().language_context.translate("Changed")
                    if entry and entry.token in self.pending_tokens
                    else ""
                )
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
                "\n"
                + self.parent().language_context.translate(
                    "Changes remain in this session until applied"
                )
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
    def __init__(self, parent=None):
        super().__init__(parent)
        self._name_width = 320
        self._fitting_name = False
        self.horizontalHeader().sectionResized.connect(self._column_resized)

    def _column_resized(self, column, before, after):
        if self._fitting_name:
            return
        if column == 0:
            self._name_width = max(160, after)
        self._fit_name_column()

    def _fit_name_column(self):
        if getattr(self, "_fitting_name", True) or self.model() is None:
            return
        others = sum(self.columnWidth(i) for i in range(1, self.model().columnCount()))
        width = max(self._name_width, self.viewport().width() - others)
        self._fitting_name = True
        try:
            self.setColumnWidth(0, width)
        finally:
            self._fitting_name = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_name_column()


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
class OpenedPlist:
    token: str
    editor: PlistEditor
    directory: Path
    source_digest: bytes
    observed_digest: bytes
    revision: tuple


class MainWindow(QMainWindow):
    def __init__(self, manager=None, *, language=None):
        super().__init__()
        self.language_context = LanguageContext(language, self)
        self.manager = manager or BackupWindows.current()
        self.manager.windows.add(self)
        self._backup_claim = None
        self._opening_claim = None
        self._output_claim = None
        self._transitioning = False
        self.transfers = OutgoingTransfers(
            self, lambda: self.session, self.can_start_outgoing_transfer
        )
        self.settings = QSettings("PlanBak", "PlanBak")
        self.session = None
        self._backup_container = None
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
        self._queued_transfer = None
        self._status_jobs = {}
        self._prefetch_jobs = {}
        self.work_label = QLabel("就绪", self)
        self.work_progress = QProgressBar(self)
        self.work_progress.setFixedWidth(160)
        self.work_progress.setFixedHeight(self.work_label.sizeHint().height())
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
        self.external_edits = ExternalEdits(self)
        self.plist_editors = {}
        self.readonly = ReadOnlyWindows(self)
        self.images = ImageWindows(self)
        self.image_viewers = self.images.viewers
        self._readonly_closing = False
        self.force_close = False
        self._exit_requested = False
        self.setWindowTitle("PlanBak")
        self.resize(1160, 760)
        self.model = BrowserModel(self)
        self.table = BrowserTable()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(0, 320)
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
            view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
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
        self.language_context.changed.connect(self.retranslate_main_actions)
        self.retranslate_main_actions()
        self.panes = BrowserPaneFocus(self.directory_tree, (self.table, self.grid), self)
        self.panes.changed.connect(self.update_actions)
        self.transfers.changed.connect(self.update_actions)
        self.transfers.status_started.connect(self.outgoing_transfer_started)
        self.transfers.status_finished.connect(self.untrack_status_job)
        self.transfers.close_ready.connect(lambda: QTimer.singleShot(0, self.close))
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
        open_action = menu.addAction(self.language_context.translate("Open"))
        delete_action = menu.addAction(self.language_context.translate("Delete"))
        rename_action = menu.addAction(self.language_context.translate("Rename"))
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
            "打开备份…", self.open_backup, "Ctrl+O", "document-open"
        )
        self.open_new_window_action = self.action(
            "在新窗口中打开备份…", lambda: self.open_backup(new_window=True)
        )
        self.close_backup_action = self.action("关闭此备份", self.close_backup, "Ctrl+W")
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
        enter = self.enter_action = self.action("打开 / 进入", self.activate_selection, "Return")
        clear = self.clear_action = self.action(
            "清除选择", lambda: self.active_view().clearSelection(), "Escape"
        )
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
        file_menu = self.file_menu = self.menuBar().addMenu("文件")
        file_menu.addActions(
            [
                self.open_backup_action,
                self.open_new_window_action,
                self.create_copy_action,
                self.add_action,
                self.add_folder_action,
                self.new_folder_action,
                self.extract_action,
                self.save_action,
            ]
        )
        file_menu.addSeparator()
        file_menu.addAction(self.close_backup_action)
        self.quit_action = self.action("退出", self.close, "Ctrl+Q")
        file_menu.addAction(self.quit_action)
        edit_menu = self.edit_menu = self.menuBar().addMenu("编辑")
        edit_menu.addActions(
            [
                select_all,
                self.rename_action,
                self.delete_action,
                self.discard_action,
            ]
        )
        view_menu = self.view_menu = self.menuBar().addMenu("视图")
        self.sidebar_action = QAction("文件夹树", self, checkable=True)
        self.sidebar_action.setShortcut(QKeySequence("F9"))
        self.sidebar_action.toggled.connect(self.toggle_sidebar)
        self.sidebar_action.setChecked(self.settings.value("sidebar_visible", True, type=bool))
        self.directory_tree.setVisible(self.sidebar_action.isChecked())
        self.addAction(self.sidebar_action)
        view_menu.addAction(self.sidebar_action)
        language_menu = self.menuBar().addMenu("Language")
        self.language_group = QActionGroup(self)
        self.language_group.setExclusive(True)
        self.language_actions = {}
        for language, name in ((SIMPLIFIED_CHINESE, "简体中文"), (ENGLISH, "English")):
            action = QAction(name, self, checkable=True)
            action.setChecked(self.language_context.language == language)
            action.triggered.connect(
                lambda _checked, value=language: self.language_context.set_language(value)
            )
            self.language_group.addAction(action)
            language_menu.addAction(action)
            self.language_actions[language] = action
        help_menu = self.help_menu = self.menuBar().addMenu("帮助")
        self.about_action = self.action("关于", self.show_about)
        help_menu.addAction(self.about_action)

    def retranslate_main_actions(self):
        tr = self.language_context.translate
        self.file_menu.setTitle(tr("File"))
        self.edit_menu.setTitle(tr("Edit"))
        self.view_menu.setTitle(tr("View"))
        self.help_menu.setTitle(tr("Help"))
        self.open_backup_action.setText(tr("Open Backup…"))
        self.open_new_window_action.setText(tr("Open Backup in New Window…"))
        self.close_backup_action.setText(tr("Close Backup"))
        self.create_copy_action.setText(tr("Create Backup Copy…"))
        self.add_action.setText(tr("Add Files…"))
        self.add_folder_action.setText(tr("Add Folder…"))
        self.new_folder_action.setText(tr("New Folder…"))
        self.open_action.setText(tr("Open"))
        self.extract_action.setText(tr("Export Items…"))
        self.rename_action.setText(tr("Rename…"))
        self.delete_action.setText(tr("Delete"))
        self.discard_action.setText(tr("Discard All Changes"))
        self.up_action.setText(tr("Parent Folder"))
        self.select_all_action.setText(tr("Select All"))
        self.quit_action.setText(tr("Quit"))
        self.sidebar_action.setText(tr("Folder Tree"))
        self.about_action.setText(tr("About"))
        self.list_action.setText(tr("List"))
        self.grid_action.setText(tr("Grid"))
        self.descending.setText(tr("Descending"))
        self.directories_first.setText(tr("Folders First"))
        self.enter_action.setText(tr("Open / Enter"))
        self.clear_action.setText(tr("Clear Selection"))
        self.directory_tree.setAccessibleName(tr("Backup folder tree"))
        for toolbar, source in (
            (self.file_toolbar, "File actions"),
            (self.navigation_toolbar, "Navigation"),
        ):
            toolbar.setWindowTitle(tr(source))
            toolbar.setAccessibleName(tr(source))
        self.language_actions[self.language_context.language].setChecked(True)
        self.changes_dock.setWindowTitle(tr("Changes"))
        self.changes_list.setAccessibleName(tr("Changes"))
        self.apply_changes_button.setText(tr("Apply Changes"))
        self.discard_button.setText(tr("Discard All Changes"))
        self.icon_label.setText(" " + tr("Icon") + " ")
        self.icon_slider.setAccessibleName(tr("Icon size"))
        self.location.setAccessibleName(tr("Folder path"))
        self.sort_combo.setToolTip(tr("Sort by"))
        self.work_progress.setAccessibleName(tr("Background task progress"))
        self.backup_status.setAccessibleName(tr("Backup status"))
        for i, title in enumerate(("Name", "Type", "Size", "Modified", "Status")):
            self.sort_combo.setItemText(i, tr(title))
        self.model.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, 4)
        if self.model.visible_count:
            self.model.dataChanged.emit(
                self.model.index(0, 0), self.model.index(self.model.visible_count - 1, 4)
            )
        root_model = self.directory_tree.directory_model
        root_index = root_model.index(0, 0)
        if root_index.isValid():
            root_model.dataChanged.emit(root_index, root_index)
        if self.changes_model.visible_count:
            self.changes_model.dataChanged.emit(
                self.changes_model.index(0, 0),
                self.changes_model.index(self.changes_model.visible_count - 1, 0),
            )
        if hasattr(self, "panes"):
            self.update_backup_summary()
            self.update_work_status()
        if hasattr(self, "panes"):
            self.update_actions()

    def show_about(self):
        dialog = AboutDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def _create_toolbar(self):
        toolbar = self.file_toolbar = QToolBar("文件操作", self)
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
        self.icon_label = QLabel(" 图标 ")
        toolbar.addWidget(self.icon_label)
        self.icon_slider = QSlider(Qt.Orientation.Horizontal)
        self.icon_slider.setRange(24, 128)
        self.icon_slider.setValue(self.settings.value("icons", 48, type=int))
        self.icon_slider.setFixedWidth(90)
        self.icon_slider.setAccessibleName("图标大小")
        self.icon_slider.valueChanged.connect(self.set_icon_size)
        toolbar.addWidget(self.icon_slider)
        self.set_icon_size(self.icon_slider.value())
        navigation = self.navigation_toolbar = QToolBar("导航", self)
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
        discard = self.discard_button = QPushButton("放弃所有更改")
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
        if self.task or self.transfers.active or self._readonly_closing or self._transitioning:
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
        busy = bool(
            self.task or self.transfers.active or self._transitioning or self._readonly_closing
        )
        for record in self.plist_editors.values():
            # 转场确认仍须允许编辑器处理保存/放弃/取消，尚未启动文件任务。
            record.editor.set_busy(bool(self.task or self.transfers.active))
        self.directory_tree.directory_model.suspended = busy
        self.directory_tree.setEnabled(not busy)
        rows = self.selected_rows()
        tokens = self.selected_tokens()
        session = self.session
        copying = self.copy_task is not None or self._copy_pending is not None
        self.open_backup_action.setEnabled(not busy and not copying)
        self.open_new_window_action.setVisible(session is not None)
        self.open_new_window_action.setEnabled(not busy and not copying)
        self.close_backup_action.setEnabled(session is not None and not busy and not copying)
        self.create_copy_action.setEnabled(not busy and not copying and session is not None)
        editable = bool(session and session.editable)
        self.save_action.setText(
            self.language_context.translate("Apply Changes" if editable else "Apply Changes…")
        )
        self.save_action.setToolTip(
            self.language_context.translate(
                "Apply changes to the editable copy"
                if editable
                else "Create a backup copy and apply changes"
            )
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
        if self.task or self.transfers.active or not hasattr(self, "sort_combo"):
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
        self.update_backup_summary()
        self.update_actions()
        self.update_work_status()

        QTimer.singleShot(0, self.check_plist_editors)

    def update_backup_summary(self):
        tr = self.language_context.translate
        if self.session:
            device = self.session.info.get("Device Name", self.session.root.name)
            dirty = self.session.dirty
            category = tr("Editable Copy") if self.session.editable else tr("Original Backup")
            self.setWindowTitle(f"{'* ' if dirty else ''}{device} · {category} — PlanBak")
            self.backup_status.setText(
                tr(
                    "{device} · {category} · {encryption} · {items} items · {changes} changed items",
                    device=device,
                    category=category,
                    encryption=tr("Encrypted") if self.session.encrypted else tr("Unencrypted"),
                    items=len(self.model.rows) - (0 if self.current_location().is_root else 1),
                    changes=len(self.session.changes),
                )
            )
            self.backup_status.setToolTip(f"{self.backup_status.text()}\n{self.session.root}")
            self.statusBar().setToolTip("")
            if dirty:
                self.changes_dock.show()
            else:
                self.changes_dock.hide()
        else:
            self.setWindowTitle("PlanBak")
            self.changes_dock.hide()
            self.backup_status.setText(tr("Open an iOS backup from File → Open Backup"))
            self.backup_status.setToolTip("")

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
                    raise BackupError("文件夹不存在", english="The folder does not exist")
        except BackupError as exc:
            message(self, "文件夹不存在", exc, QMessageBox.Icon.Warning)
            return
        self.navigate(location)

    def update_work_status(self):
        self.statusBar().clearMessage()
        if not self._status_jobs:
            self.work_label.setText(self.language_context.translate("Ready"))
            self.work_progress.hide()
            return
        jobs = list(self._status_jobs.items())
        job, (text, done, total) = next(
            (item for item in reversed(jobs) if item[0] not in self._prefetch_jobs),
            jobs[-1],
        )
        preloading = job in self._prefetch_jobs
        progress_sources = {
            "正在读取备份…": "Reading backup…",
            "正在建立备份索引…": "Building backup index…",
            "正在创建备份副本…": "Creating backup copy…",
            "正在打开文件…": "Opening file…",
            "正在向其他备份复制项目…": "Copying items to another backup…",
            "正在复制项目到当前备份…": "Copying items to this backup…",
            "正在清理旧会话缓存…": "Cleaning old session cache…",
        }
        operations = {
            "切换到备份副本": "Switching to backup copy",
            "应用外部编辑": "Applying external edits",
            "读取备份": "Reading backup",
            "关闭备份并清理缓存": "Closing backup and clearing cache",
            "添加项目": "Adding items",
            "删除项目": "Deleting items",
            "放弃更改": "Discarding changes",
            "准备放弃更改": "Preparing to discard changes",
            "导出项目": "Exporting items",
            "打开文件": "Opening file",
            "重新加载文件": "Reloading file",
            "打开项目": "Opening item",
            "保存外部编辑": "Saving external edits",
            "移动项目": "Moving items",
            "复制项目到当前备份": "Copying items to this backup",
            "清理旧会话缓存": "Cleaning old session cache",
        }
        if preloading:
            text = self.language_context.translate(
                "Preloading file: {name}", name=self._prefetch_jobs[job]
            )
        elif text in progress_sources:
            text = self.language_context.translate(progress_sources[text])
        elif text.startswith("正在") and text.endswith("…") and text[2:-1] in operations:
            text = self.language_context.translate(operations[text[2:-1]] + "…")
        elif text.startswith("正在加载文件："):
            text = self.language_context.translate(
                "Loading file: {name}", name=text[len("正在加载文件：") :]
            )
        elif text.startswith("正在导出："):
            text = self.language_context.translate(
                "Exporting: {name}", name=text[len("正在导出：") :]
            )
        self.work_label.setText(text)
        if preloading:
            self.work_progress.hide()
            return
        self.work_progress.setRange(0, 1000 if total > 0 else 0)
        if total > 0:
            self.work_progress.setValue(min(1000, int(done / total * 1000)))
        self.work_progress.show()

    def work_progress_changed(self, job, text, done, total):
        if job in self._status_jobs:
            self._status_jobs[job] = (text, done, total)
            self.update_work_status()

    def track_status_job(self, job, initial_text, *, connect_progress=True, prefetch_name=None):
        """Show any background job through the shared status-bar progress UI."""
        self._status_jobs[job] = (initial_text, 0, 0)
        if prefetch_name is not None:
            self._prefetch_jobs[job] = prefetch_name
        if connect_progress:
            job.progress.connect(
                lambda text, done, total: self.work_progress_changed(job, text, done, total)
            )
        self.update_work_status()

    def untrack_status_job(self, job):
        self._status_jobs.pop(job, None)
        self._prefetch_jobs.pop(job, None)
        self.update_work_status()

    def create_backup_copy(self, *_):
        if (
            not self.session
            or self.task
            or self.copy_task
            or self._copy_pending
            or self._transitioning
            or self.transfers.active
            or self._readonly_closing
        ):
            return
        session = self.session
        container = self._backup_container is not None
        suggested = (
            self._backup_container.parent / (self._backup_container.name + "-copy")
            if container
            else session.root.parent / (session.root.name + "-copy")
        )
        target, _ = QFileDialog.getSaveFileName(
            self,
            self.language_context.translate("Create Backup Copy"),
            str(suggested),
            self.language_context.translate("Backup folder (*)"),
        )
        if not target:
            return
        try:
            claim = self.manager.reserve(
                Path(target) / session.root.name if container else target, self
            )
        except Exception as exc:
            message(self, "无法创建备份副本", exc, QMessageBox.Icon.Warning)
            return
        self._output_claim = claim
        job = Task(
            lambda progress, cancel: session.create_copy(
                Path(target), progress=progress, cancel=cancel, container=container
            ),
            self,
        )
        self.copy_task = job
        self.track_status_job(job, "正在创建备份副本…")

        def finished():
            self.copy_task = None
            self.untrack_status_job(job)
            if not job.error and self.session is session:
                self._copy_pending = (session, job.result)
            if self._copy_exit_requested:
                self._copy_exit_requested = False
                self._copy_pending = None
                self._release_output_claim()
                QTimer.singleShot(0, self.close)
            else:
                if job.error and not isinstance(job.error, Cancelled):
                    message(self, "无法创建备份副本", job.error, QMessageBox.Icon.Warning)
                if self._copy_pending is None:
                    self._release_output_claim()
                self.attach_created_copy()
            self.update_actions()
            job.deleteLater()

        job.finished.connect(finished)
        self.update_actions()
        job.start()

    def attach_created_copy(self):
        if not self._copy_pending or self.task or self._exit_requested:
            return
        session, copied = self._copy_pending
        self._copy_pending = None
        if self.session is not session:
            self._release_output_claim()
            return
        try:
            claim = self._output_claim or self.manager.reserve(copied.path, self)
        except Exception as exc:
            message(self, "无法切换副本", exc, QMessageBox.Icon.Warning)
            return

        def adopted(_):
            self._adopt_claim(claim)
            if self._backup_container is not None:
                self._backup_container = copied.path.parent
            self.refresh(focus_browser=False)

        def finished():
            if session.root == copied.path and self.session is session:
                self._adopt_claim(claim)
            else:
                claim.release()
                if self._output_claim is claim:
                    self._output_claim = None

        self.run_task(
            "切换到备份副本",
            lambda progress, cancel: session.use_copy(copied),
            adopted,
            silent=True,
            on_done=finished,
        )

    def _release_output_claim(self):
        if self._output_claim:
            self._output_claim.release()
            self._output_claim = None

    def run_task(
        self,
        title,
        function,
        callback=None,
        *,
        silent=False,
        dialog_progress=False,
        on_error=None,
        on_done=None,
    ):
        if self.task or self.transfers.active:
            return
        job = Task(function, self)
        self.task = job
        dialog = None
        tracked_status = not silent and not dialog_progress
        if tracked_status:
            self.track_status_job(job, "正在" + title + "…")
        if not silent and dialog_progress:
            dialog = FixedProgressDialog(
                self.language_context.translate("Apply Changes"),
                self.language_context.translate("Cancel"),
                0,
                0,
                self,
            )
            self.progress_dialog = dialog
            dialog.setWindowTitle("PlanBak")
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
            dialog.canceled.connect(job.cancelled.set)

            progress_text = [title]

            def translate_progress():
                labels = {
                    "应用更改": "Apply Changes",
                    "正在应用更改…": "Applying changes…",
                    "估算备份大小…": "Estimating backup size…",
                    "复制备份到独立输出文件夹…": "Copying backup to output folder…",
                    "重新封装并验证清单…": "Repacking and verifying manifest…",
                    "建立新的保存基线…": "Establishing the new save baseline…",
                }
                text = progress_text[0]
                if text.startswith("写入："):
                    text = self.language_context.translate(
                        "Writing: {name}", name=text[len("写入：") :]
                    )
                elif text in labels:
                    text = self.language_context.translate(labels[text])
                dialog.setLabelText(text)
                dialog._cancel_button.setText(self.language_context.translate("Cancel"))

            self.language_context.changed.connect(translate_progress)

            def progress(text, done, total):
                progress_text[0] = text
                if total > 0:
                    dialog.setRange(0, 1000)
                    dialog.setValue(min(999, int(done / total * 1000)))
                else:
                    dialog.setRange(0, 0)
                translate_progress()
                if text == "正在应用更改…":
                    dialog.set_cancellable(False)

            job.progress.connect(progress)
            dialog.show()
        self.update_actions()

        def finished():
            self.task = None
            if tracked_status:
                self.untrack_status_job(job)
            if dialog:
                self.language_context.changed.disconnect(translate_progress)
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
            try:
                if job.error:
                    if isinstance(job.error, Cancelled):
                        self.statusBar().setToolTip(
                            self.language_context.translate("Operation canceled")
                        )
                    elif on_error:
                        on_error(job.error)
                    else:
                        message(self, title, job.error, QMessageBox.Icon.Critical)
                elif callback:
                    callback(job.result)
            except Exception as exc:
                message(self, title, exc, QMessageBox.Icon.Critical)
            finally:
                if on_done:
                    on_done()
                job.deleteLater()
                self.attach_created_copy()

        job.finished.connect(finished)
        job.start()
        return job

    def _focus_owned_modal(self, *, focus=True):
        owners = {self}
        owners.update(record.editor for record in self.plist_editors.values())
        owners.update(self.readonly.viewers())
        owners.update(self.image_viewers)
        # 不只检查全局activeModalWidget：其他备份可能同时有自己的WindowModal。
        for widget in QApplication.topLevelWidgets():
            if not widget.isVisible() or not widget.isModal():
                continue
            parent = widget.parentWidget()
            while parent is not None:
                if parent in owners:
                    if focus:
                        widget.raise_()
                        widget.activateWindow()
                    return True
                parent = parent.parentWidget()
        return False

    def confirm_leave(self, continuation, *, exiting=False, cancelled=None, purpose="切换备份"):
        if self.task or self.copy_task or self._copy_pending:
            if cancelled:
                cancelled()
            return
        if self._focus_owned_modal():
            if cancelled:
                cancelled()
            return
        if self.readonly.has_open() or self.image_viewers:
            self.close_readonly_viewers(
                lambda: self.confirm_leave(
                    continuation, exiting=exiting, cancelled=cancelled, purpose=purpose
                )
            )
            return
        for record in list(self.plist_editors.values()):
            if not record.editor.close():
                if cancelled:
                    cancelled()
                return
        if not self.session:
            continuation()
            return
        external = self.external_edits.changed_records(self.session)
        if not self.session.dirty and not external:
            continuation()
            return
        tr = self.language_context.translate
        save_description = tr(
            "apply changes to the editable copy"
            if self.session.editable
            else "create a backup copy and apply changes"
        )
        if exiting:
            description = tr(
                "There are unsaved changes.\nChoose Apply Changes to {action} and remain in the app; choose Discard Changes to clear temporary files and exit.",
                action=save_description,
            )
        else:
            destination = tr(
                {
                    "切换备份": "switch backups",
                    "关闭当前备份并打开所选备份": "close the current backup and open the selected backup",
                    "关闭此备份": "close this backup",
                }.get(purpose, purpose)
            )
            description = tr(
                "There are unsaved changes.\nChoose Apply Changes to {action}, then {purpose}; choose Discard Changes to clear the session and {purpose}.",
                action=save_description,
                purpose=destination,
            )
        box = QMessageBox(
            QMessageBox.Icon.Question,
            tr("Unsaved Changes"),
            description,
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            self,
        )
        box.button(QMessageBox.StandardButton.Save).setText(tr("Apply Changes"))
        box.button(QMessageBox.StandardButton.Discard).setText(tr("Discard Changes"))
        box.button(QMessageBox.StandardButton.Cancel).setText(tr("Cancel"))
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        result = box.exec()
        if result == QMessageBox.StandardButton.Save:
            after_save = None if exiting else continuation
            if not external:
                self.save(after=after_save, quiet=True, on_cancel=cancelled)
                return

            def stage_external(progress, cancel):
                return self.external_edits.stage(self.session, external, cancel)

            def staged(updates):
                self.external_edits.accept_staged(updates)
                self.refresh()
                if self.external_edits.has_unreviewed(external):
                    message(
                        self,
                        "外部文件仍在修改",
                        "请先在外部应用中保存文件，然后重试。",
                        QMessageBox.Icon.Warning,
                    )
                    if cancelled:
                        cancelled()
                    return
                self.save(after=after_save, quiet=True, on_cancel=cancelled)

            succeeded = [False]

            def done(updates):
                succeeded[0] = True
                staged(updates)

            self.run_task(
                "应用外部编辑",
                stage_external,
                done,
                on_done=lambda: cancelled() if not succeeded[0] and cancelled else None,
            )
        elif result == QMessageBox.StandardButton.Discard:
            continuation()
        elif cancelled:
            cancelled()

    def open_backup(self, *_, new_window=False):
        if (
            self.task
            or self.copy_task
            or self._copy_pending
            or self._transitioning
            or self.transfers.active
            or self._readonly_closing
        ):
            return
        path = QFileDialog.getExistingDirectory(
            self, self.language_context.translate("Open Backup")
        )
        if path:
            self.open_path(Path(path), new_window=new_window)

    def open_path(self, path, *, new_window=False, _container=None):
        if (
            self.task
            or self._exit_requested
            or self.copy_task
            or self._copy_pending
            or self._transitioning
            or self.transfers.active
            or self._readonly_closing
        ):
            return
        try:
            selected_directory = Path(path).expanduser().resolve()
            candidates = detect_backups(selected_directory)
            container = (
                _container
                if _container is not None
                else (selected_directory if candidates[0] != selected_directory else None)
            )
            if len(candidates) > 1:
                dialog = BackupChoiceDialog(candidates, self)
                accepted = dialog.exec() == QDialog.DialogCode.Accepted
                path = dialog.selected_path() if accepted else None
                dialog.deleteLater()
                if path is None:
                    return
            else:
                path = candidates[0]
            path = Path(path).resolve()
            owner = self.manager.owner_for(path)
            relocated = (
                owner is self
                and not new_window
                and self.session is not None
                and path != self.session.root
            )
            if owner is not None and not relocated:
                if owner is self and not new_window and self.session and path == self.session.root:
                    self._backup_container = container
                if owner.isMinimized():
                    owner.setWindowState(owner.windowState() & ~Qt.WindowState.WindowMinimized)
                owner.show()
                owner.raise_()
                owner.activateWindow()
                if owner is not self or new_window:
                    message(
                        self, "备份已打开", "此备份已在另一个窗口中打开。", QMessageBox.Icon.Warning
                    )
                return
            if new_window:
                window = MainWindow(self.manager, language=self.language_context.language)
                window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
                window.show()
                window.open_path(path, _container=container)
                return
            # 外部移动后目录身份不变，旧会话仍指向旧路径。重新打开必须
            # 完整清理并解锁/建索引，身份锁在新旧占用之间连续共享。
            claim = self.manager.reserve(
                path, self, shared=self._backup_claim if relocated else None
            )
        except Exception as exc:
            message(self, "无法打开备份", exc, QMessageBox.Icon.Warning)
            return
        self._opening_claim = claim
        self._transitioning = True
        self.update_actions()

        def proceed():
            self._clear_backup(lambda: self._load_backup(path, claim, container))

        self.confirm_leave(
            proceed, cancelled=self._cancel_open, purpose="关闭当前备份并打开所选备份"
        )

    def _cancel_open(self):
        if self._opening_claim:
            self._opening_claim.release()
            self._opening_claim = None
        self._transitioning = False
        self.update_actions()

    def _adopt_claim(self, claim):
        claim.promote()
        previous = self._backup_claim
        self._backup_claim = claim
        if previous and previous is not claim:
            previous.release()
        if self._opening_claim is claim:
            self._opening_claim = None
        if self._output_claim is claim:
            self._output_claim = None

    def _load_backup(self, path, claim, container=None):
        password = ""
        try:
            encrypted = load_plist(path / "Manifest.plist").get("IsEncrypted", False)
        except Exception as exc:
            self._cancel_open()
            message(self, "无法打开备份", exc, QMessageBox.Icon.Warning)
            return
        if encrypted:
            dialog = PasswordDialog(self)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            password = dialog.password.text() if accepted else ""
            dialog.password.clear()
            dialog.deleteLater()
            if not accepted:
                self._cancel_open()
                return

        def loaded(session):
            try:
                self._adopt_claim(claim)
            except Exception:
                session.close()
                raise
            self.session = session
            self._backup_container = container
            self._transitioning = False
            self.navigate(Location())

        def finished():
            if self._opening_claim is claim:
                self._cancel_open()

        self.run_task(
            "读取备份",
            lambda progress, cancel: BackupSession(
                Path(path), password, progress=progress, cancel=cancel
            ),
            loaded,
            on_done=finished,
        )

    def close_backup(self, *_):
        if (
            not self.session
            or self.task
            or self.copy_task
            or self._copy_pending
            or self._transitioning
            or self.transfers.active
            or self._readonly_closing
        ):
            return
        self._transitioning = True
        self.update_actions()

        def cancelled():
            self._transitioning = False
            self.update_actions()

        self.confirm_leave(
            lambda: self._clear_backup(cancelled), cancelled=cancelled, purpose="关闭此备份"
        )

    def _clear_backup(self, after):
        session, claim = self.session, self._backup_claim
        if not session:
            after()
            return
        self._release_queued_transfer()
        cache = self.images.cache
        self.images.cache = None
        self.clear_opened_files(clear_images=False)
        self.clear_navigation_state()
        self.session = None
        self._backup_container = None
        self.domain, self.path, self.domain_group = None, "", None
        self.refresh()
        self.panes.activate(self.active_view(), focus=True)

        def release():
            if claim:
                claim.release()
            if self._backup_claim is claim:
                self._backup_claim = None

        def cleanup(progress, cancel):
            if cache is not None:
                cache.clear()
            session.close()
            if session.workspace.exists():
                raise BackupError(
                    "未能完全清理会话临时文件夹，请检查文件占用或权限",
                    english="The session temporary folder could not be fully cleared; check file usage and permissions",
                )

        def cleaned(_):
            release()
            after()

        def failed(exc):
            self._cancel_open()
            message(self, "无法完成清理", exc, QMessageBox.Icon.Warning)

        self.run_task("关闭备份并清理缓存", cleanup, cleaned, on_error=failed, on_done=release)

    def clear_opened_files(self, *, clear_images=True):
        if clear_images:
            self.images.clear_cache()
        self.external_edits.clear()

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, self.language_context.translate("Add Files to Backup Folder")
        )
        if paths:
            self.import_paths(paths, self.domain, self.path)

    def add_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, self.language_context.translate("Add Folder to Backup Folder")
        )
        if path:
            self.import_paths([path], self.domain, self.path)

    def collision_policy(self, *, exporting=False):
        box = QMessageBox(
            QMessageBox.Icon.Question,
            self.language_context.translate("Name Conflict"),
            self.language_context.translate(
                "The destination contains items with the same names. Choose how to handle all conflicts in this operation."
            ),
            parent=self,
        )
        replace = box.addButton(
            self.language_context.translate("Replace / Merge" if exporting else "Replace"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        both = box.addButton(
            self.language_context.translate("Keep Both"), QMessageBox.ButtonRole.ActionRole
        )
        skip = box.addButton(
            self.language_context.translate("Skip"), QMessageBox.ButtonRole.ActionRole
        )
        box.addButton(self.language_context.translate("Cancel"), QMessageBox.ButtonRole.RejectRole)
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
                "添加项目",
                lambda progress, cancel: self.session.import_paths(
                    paths, domain, parent, collision=policy, progress=progress, cancel=cancel
                ),
                lambda _: self.refresh(),
            )

    def new_folder(self):
        name, ok = QInputDialog.getText(
            self,
            self.language_context.translate("New Folder"),
            self.language_context.translate("Name:"),
        )
        if ok and name:
            try:
                self.session.new_directory(self.domain, self.path, name)
                self.refresh()
            except BackupError as exc:
                message(self, "无法新建文件夹", exc, QMessageBox.Icon.Warning)

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
        name, ok = QInputDialog.getText(
            self,
            self.language_context.translate("Rename"),
            self.language_context.translate("New name:"),
            text=entry.name,
        )
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
                    updated = self.session.find(current.token)
                    location = (
                        Location(updated.domain, updated.path)
                        if updated
                        else self.current_location()
                    )
                    # 替换可删除当前身份；地址存在时展示替代者，否则退回上层。
                    while location.path:
                        container = self.session.at(location.domain, location.path)
                        if container and container.kind == "directory":
                            break
                        location = location.parent
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
                message(self, "无法重命名", exc, QMessageBox.Icon.Warning)

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
            self.language_context.translate("Delete Items"),
            "",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(
            self.language_context.translate(
                "Delete {names}?\nThis affects {count} files and folders. You can discard this change before applying changes to the backup copy.",
                names=names[:250],
                count=count,
            )
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
        revert = menu.addAction(self.language_context.translate("Discard This Change"))
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
        if self._focus_owned_modal():
            return
        session = self.session
        directory = session.at(self.domain, self.path) if self.domain else None

        def prepared(plan):
            if self._focus_owned_modal():
                return
            if not plan.updates:
                return
            editors = [
                record for key, record in self.plist_editors.items() if key in plan.reset_contents
            ]
            external = self.external_edits.records_for(plan.reset_contents)
            dirty = any(record.editor.dirty for record in editors) or self.external_edits.has_changes(
                external
            )
            if dirty:
                box = QMessageBox(
                    QMessageBox.Icon.Question,
                    self.language_context.translate("Discard This Change"),
                    self.language_context.translate(
                        "Related editor copies have unsaved changes. Discard those edits and this change?"
                    ),
                    QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                    self,
                )
                box.button(QMessageBox.StandardButton.Discard).setText(
                    self.language_context.translate("Discard Edits and Revert")
                )
                box.button(QMessageBox.StandardButton.Cancel).setText(
                    self.language_context.translate("Cancel")
                )
                box.setDefaultButton(QMessageBox.StandardButton.Cancel)
                if box.exec() != QMessageBox.StandardButton.Discard:
                    return
            for record in editors:
                record.editor.discard_and_close()
            self.external_edits.forget(external)
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
                self.language_context.translate("Discard All Changes"),
                self.language_context.translate("The changes you made will not be applied."),
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
        target = QFileDialog.getExistingDirectory(
            self, self.language_context.translate("Export to Folder")
        )
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
                lambda _: self.statusBar().setToolTip(
                    self.language_context.translate("Exported to {path}", path=target)
                ),
            )

    def open_file(self, *, external=False):
        if (
            self._transitioning
            or self.transfers.active
            or self._readonly_closing
            or self.task
            or not self.session
            or self.sidebar_active()
        ):
            return
        rows = self.selected_rows()
        if len(rows) != 1 or rows[0].kind != "file":
            return
        token = rows[0].entry.token
        if external and self._focus_owned_modal():
            return
        # 明确指定系统应用时，先关闭可编辑窗口，沿用保存/放弃/取消保护。
        if external:
            record = self.plist_editors.get(token)
            if record and not record.editor.close():
                return
        else:
            if self.images.reuse(token):
                return
            if self.readonly.reuse_database(token):
                return
            if token in self.plist_editors:
                editor = self.plist_editors[token].editor
                editor.showNormal()
                editor.raise_()
                editor.activateWindow()
                editor.tree.setFocus()
                self.check_plist_editors()
                return
        if self.external_edits.reopen(token):
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

            def extract():
                entry = session.get(token)
                cached = Path(entry.staged) if entry.staged else session.cache_path(token)
                source = cached if cached.is_file() else session.payload_path(entry.original_id)
                total = source.stat().st_size
                done = 0
                progress("正在打开文件…", 0, total)

                def advanced(amount):
                    nonlocal done
                    done += amount
                    progress("正在打开文件…", done, total)

                session.extract_file(token, target, cancel=cancel, progress=advanced)
                progress("正在打开文件…", 0, 0)

            if external:
                extract()
                return "external", preparers["external"](target, cancel)
            with cache.session_lock(cancel):
                revision = source_revision(session, token)
                with cache.prepared_source(token, revision) as existing:
                    if existing and identify_file(existing) == "image":
                        return "image", None
                entry = session.get(token)
                source = Path(entry.staged) if entry.staged else session.cache_path(token)
                if not source.is_file():
                    extract()
                    source = target
                kind = identify_file(source)
                check_cancel(cancel)
                # 后缀仅用于无法识别内容时解释错误，不决定使用哪个查看器。
                if kind == "external":
                    if name.endswith(("-wal", "-shm", "-journal")):
                        raise BackupError(
                            "这是数据库的配套文件，请打开对应的主文件。",
                            english="This is a database companion file; open its corresponding main file.",
                        )
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
                            "可通过右键菜单选择“以十六进制查看”“使用系统应用打开”，或导出项目。",
                            english="The built-in viewer supports standard SQLite only; this file may use another format or app-specific encryption. Use the context menu to view it as hexadecimal, open it in a system application, or export it.",
                        )
                if kind != "image":
                    return kind, preparers[kind](source, cancel)
                if source != target:
                    copy_file(source, target, cancel)
            # 同图任务锁在会话锁之外获取，避免与图片导航线程互相等待。
            return kind, preparers[kind](target, cancel)

        def open_external(fingerprint):
            self.external_edits.register(token, target, fingerprint)
            resources["keep_file"] = True
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))):
                message(
                    self,
                    "无法启动默认应用",
                    self.language_context.translate(
                        "The system could not open this file. The temporary file is located at:\n{path}",
                        path=target,
                    ),
                    QMessageBox.Icon.Warning,
                )

        def open_database(snapshot):
            self.readonly.show_database(token, snapshot)
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

        self.run_task("打开文件", prepare, opened, on_done=cleanup)

    def close_readonly_viewers(self, after):
        if self._readonly_closing:
            return
        self._readonly_closing = True
        self.update_actions()
        scheduled = False

        def resume():
            self._readonly_closing = False
            self.update_actions()
            after()

        def closed(_):
            nonlocal scheduled
            if not self.readonly.has_open() and not self.image_viewers:
                if not scheduled:
                    scheduled = True
                    QTimer.singleShot(0, resume)

        self.readonly.request_close_all(closed)
        for viewer in list(self.image_viewers):
            viewer._skip_prompt = True
            viewer.finished.connect(closed)
            viewer.reject()
        closed(None)

    def plist_revision(self, token):
        entry = self.session.get(token)
        source = (
            Path(entry.staged) if entry.staged else self.session.payload_path(entry.original_id)
        )
        return (str(source), file_stamp(source))

    def open_plist(self, token, name, *, prepared=None):
        if self._transitioning or self.transfers.active or self._readonly_closing:
            return
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
                    raise BackupError(
                        "备份操作进行中，请稍后再保存",
                        english="A backup operation is in progress; try saving later",
                    )
                if self._focus_owned_modal(focus=False):
                    raise BackupError(
                        "请先完成或取消当前编辑弹窗，再保存 plist",
                        english="Finish or cancel the current edit dialog before saving the plist",
                    )
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
                        self.language_context.translate("plist File Changed"),
                        self.language_context.translate(
                            "The backup file has changed. Reload it or confirm that your edits should overwrite the updated version."
                        ),
                        parent=editor,
                    )
                    reload_button = box.addButton(
                        self.language_context.translate("Reload"), QMessageBox.ButtonRole.AcceptRole
                    )
                    overwrite = box.addButton(
                        self.language_context.translate("Overwrite Updated Version"),
                        QMessageBox.ButtonRole.DestructiveRole,
                    )
                    cancel_button = box.addButton(
                        self.language_context.translate("Cancel"), QMessageBox.ButtonRole.RejectRole
                    )
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
                lambda data, identity, label: self.readonly.open_data_hex(
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
            self.run_task("打开文件", prepare, opened, on_done=cleanup)
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
                or self._focus_owned_modal(focus=False)
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
        if self.transfers.active or self._transitioning or self._readonly_closing:
            return False
        if (
            self.task
            or not self.session
            or self.force_close
            or self._focus_owned_modal(focus=False)
        ):
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
                if self._focus_owned_modal(focus=False):
                    return
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
                text = self.language_context.translate(
                    "This plist has changed. Reload the updated version?"
                )
                if rec.editor.dirty:
                    text += self.language_context.translate(
                        "\nReloading will discard unsaved changes in the editor."
                    )
                answer = QMessageBox.question(
                    rec.editor,
                    self.language_context.translate("plist File Changed"),
                    text,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self.reload_plist(rec, data)

            def failed(exc, rec=record, version=revision):
                if self._focus_owned_modal(focus=False):
                    return
                rec.revision = version
                rec.editor.show_error(exc)

            self.run_task("检查 plist 更新", read, checked, silent=True, on_error=failed)
            return True
        return False

    def poll_open_files(self):
        if self.transfers.active or self._transitioning or self._readonly_closing:
            return
        if self.task or not self.session or self._focus_owned_modal(focus=False):
            return
        if self.check_plist_editors() or self.readonly.check_database_viewers() or self.readonly.check_hex_viewers():
            return
        self.external_edits.poll()

    def save(self, *_args, after=None, quiet=False, on_cancel=None):
        if (
            not self.session
            or self.task
            or self.copy_task
            or self._copy_pending
            or self.transfers.active
            or self._readonly_closing
        ):
            if on_cancel:
                on_cancel()
            return
        for record in list(self.plist_editors.values()):
            if record.editor.dirty and not record.editor.save_document():
                if on_cancel:
                    on_cancel()
                return
        session = self.session
        if not session.dirty:
            if after:
                after()
            return
        target = None
        direct = session.editable
        container = self._backup_container is not None
        if not direct:
            suggested = str(
                self._backup_container.parent / (self._backup_container.name + "-copy")
                if container
                else session.root.parent / (session.root.name + "-edited")
            )
            target, _ = QFileDialog.getSaveFileName(
                self,
                self.language_context.translate("Create Backup Copy and Apply Changes"),
                suggested,
                self.language_context.translate("Backup folder (*)"),
            )
            if not target:
                if on_cancel:
                    on_cancel()
                return
        source_root = session.root
        claim = None
        try:
            if not direct:
                claim = self.manager.reserve(
                    Path(target) / session.root.name if container else target, self
                )
                self._output_claim = claim
            token = session.begin_apply()  # 在线程启动/弹窗事件循环之前关闭数据层暂挂入口。
        except Exception as exc:
            if claim:
                self._release_output_claim()
            message(self, "无法应用更改", exc, QMessageBox.Icon.Warning)
            if on_cancel:
                on_cancel()
            return
        completed = [False]

        def saved(destination):
            if claim:
                self._adopt_claim(claim)
                if container:
                    self._backup_container = destination.parent
            completed[0] = True
            self.refresh()
            if not quiet:
                text = self.language_context.translate(
                    "Changes applied to:\n{path}", path=destination
                )
                if not direct:
                    text += self.language_context.translate(
                        "\n\nOriginal backup preserved at:\n{path}", path=source_root
                    )
                message(self, "应用完成", text)
            if after:
                after()

        def finished():
            session.end_apply(token)
            if claim:
                if self.session is session and session.root == claim.path:
                    self._adopt_claim(claim)
                else:
                    claim.release()
                    if self._output_claim is claim:
                        self._output_claim = None
            if not completed[0] and on_cancel:
                on_cancel()

        self.run_task(
            "应用更改",
            lambda progress, cancel: session.save(
                Path(target) if target else None,
                progress=progress,
                cancel=cancel,
                _token=token,
                container=container,
            ),
            saved,
            dialog_progress=True,
            on_done=finished,
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
            action = menu.addAction(self.language_context.translate("Open Folder"))
            action.triggered.connect(lambda: self.activate(index))
        else:
            menu.addAction(self.open_action)
        menu.addAction(self.extract_action)
        rows = self.selected_rows()
        if len(rows) == 1 and rows[0].kind == "file":
            hex_action = menu.addAction(self.language_context.translate("View as Hexadecimal"))
            hex_action.setEnabled(not self.task)
            hex_action.triggered.connect(self.readonly.open_hex)
            external = menu.addAction(
                self.language_context.translate("Open with System Application")
            )
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
        if (
            self.task
            or self._transitioning
            or self.transfers.active
            or self._readonly_closing
            or self._drop_queued
            or self.force_close
            or self._exit_requested
        ):
            return None
        internal = mime.hasFormat(INTERNAL_MIME)
        if internal and self.session is None:
            return None
        if not internal and not mime.hasUrls():
            return None
        snapshot = QMimeData(self)
        borrowed = None
        if internal:
            # 不能读取内部拖放的 URL：ExportMimeData 会为外部消费者即时导出。
            snapshot.setData(INTERNAL_MIME, bytes(mime.data(INTERNAL_MIME)))
            try:
                content = json.loads(bytes(snapshot.data(INTERNAL_MIME)))
                if not isinstance(content, dict):
                    raise ValueError("无效拖放内容")
                local = self.session and content["session"] == self.session.session_id
                if not local:
                    # 仅取得快速的会话租约，不在原生事件内弹窗或导出文件。
                    borrowed = self.borrow_transfer(content["session"])
            except (ValueError, KeyError, TypeError, BackupError):
                snapshot.deleteLater()
                return None
        else:
            snapshot.setUrls(mime.urls())
        target = self.drop_location(index)
        session = self.session
        self._drop_queued = True
        self._queued_transfer = borrowed

        def consume():
            try:
                if (
                    self.session is session
                    and not self.task
                    and not self.force_close
                    and not self._exit_requested
                ):
                    self.handle_drop(snapshot, QModelIndex(), target=target, borrowed=borrowed)
            finally:
                if borrowed and not borrowed.started:
                    borrowed.release()
                if self._queued_transfer is borrowed:
                    self._queued_transfer = None
                self._drop_queued = False
                snapshot.deleteLater()

        QTimer.singleShot(0, self, consume)
        return (
            Qt.DropAction.MoveAction if internal and borrowed is None else Qt.DropAction.CopyAction
        )

    def _release_queued_transfer(self):
        borrowed = self._queued_transfer
        if borrowed and not borrowed.started:
            borrowed.release()
            self._queued_transfer = None

    def handle_drop(self, mime, index, *, target=None, borrowed=None):
        if self.task or self._transitioning or self.transfers.active or self._readonly_closing:
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
                    return self.copy_from_window(content, domain, parent, borrowed=borrowed)
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
                message(self, "无法处理拖放项目", exc, QMessageBox.Icon.Warning)
                return False
        paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
        if paths:
            self.import_paths(paths, domain, parent)
            return True
        return False

    def can_start_outgoing_transfer(self):
        return not (
            self.task
            or self.copy_task
            or self._copy_pending
            or self._transitioning
            or self._readonly_closing
            or self.force_close
        )

    def outgoing_transfer_started(self, token):
        self.track_status_job(token, "正在向其他备份复制项目…", connect_progress=False)

    def borrow_transfer(self, session_id):
        source = self.manager.window_for_session(session_id)
        if source is None or source is self:
            raise BackupError(
                "来源备份会话已关闭，或不属于当前程序；请先导出项目再添加。",
                english="The source backup session is closed or belongs to another application; export items before adding them.",
            )
        return source.transfers.acquire()

    def copy_from_window(self, content, domain, parent, *, borrowed=None):
        borrowed = borrowed or self.borrow_transfer(content["session"])
        session = self.session
        try:
            tokens = content["tokens"]
            if (
                not isinstance(tokens, list)
                or not tokens
                or any(not isinstance(t, str) for t in tokens)
            ):
                raise BackupError("无效的拖放项目列表", english="Invalid list of dropped items")
            borrowed.show_status()
            roots = borrowed.session.roots(tokens)
            conflict = any(session.at(domain, f"{parent}/{e.name}".lstrip("/")) for e in roots)
            policy = self.collision_policy() if conflict else "error"
            if (
                not policy
                or self.session is not session
                or self.force_close
                or self._transitioning
                or self.task
            ):
                return False

            def copy(progress, cancel):
                borrowed.copy_to(session, tokens, domain, parent, policy, progress, cancel)

            job = self.run_task(
                "复制项目到当前备份", copy, lambda _: self.refresh(), on_done=borrowed.release
            )
            borrowed.started = job is not None
            return borrowed.started
        finally:
            if not borrowed.started:
                borrowed.release()

    def start_drag(self, view):
        tokens = self.selected_tokens()
        if (
            not tokens
            or not self.session
            or self.task
            or self._transitioning
            or self.transfers.active
        ):
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

        def export(progress, cancel):
            try:
                return self.session.export(tokens, target, progress=progress, cancel=cancel)
            except BaseException:
                shutil.rmtree(target, ignore_errors=True)
                raise

        job = self.run_task(
            "导出项目",
            export,
            done,
            on_done=loop.quit,
        )
        if job is None:
            return []
        loop.exec()
        return result

    def closeEvent(self, event):
        if self.transfers.active:
            event.ignore()
            self.transfers.defer_close()
            return
        if not self.task and self._focus_owned_modal():
            event.ignore()
            self.force_close = False
            return
        if self.copy_task:
            event.ignore()
            self._copy_exit_requested = True
            self.copy_task.cancelled.set()
            return
        self._copy_pending = None
        if self.force_close:
            if self.task:
                event.ignore()
                return
            if self.readonly.has_open() or self.image_viewers:
                event.ignore()
                self.close_readonly_viewers(self.close)
                return
            if self.session:
                event.ignore()
                self._transitioning = True
                self.poll_timer.stop()
                self._clear_backup(lambda: QTimer.singleShot(0, self.close))
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
            # DeleteOnClose会取消尚未消费的上下文定时器，不能只靠consume的finally。
            self._release_queued_transfer()
            self.clear_opened_files()
            self.clear_navigation_state()
            self.directory_tree.sync(None, Location())
            if self.session:
                self.session.close()
                self.session = None
            self.manager.remove(self)
            self._backup_claim = self._opening_claim = self._output_claim = None
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
                    progress("正在清理旧会话缓存…", number, len(paths))
                    shutil.rmtree(path)

        self.run_task("清理旧会话缓存", cleanup, on_done=after)


def main():
    parser = argparse.ArgumentParser(description="离线浏览与编辑 iOS 备份")
    parser.add_argument("backup", nargs="?", type=Path, help="备份文件夹（可选）")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("PlanBak")
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
