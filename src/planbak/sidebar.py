"""Lazy directory-only model and a file-manager style navigation tree."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QObject, QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QAbstractItemView, QApplication, QHeaderView, QStyle, QTreeView

from .backup import Entry
from .codec import BackupError
from .icons import ApplicationIcons
from .navigation import Location, split_domain


@dataclass(eq=False)
class DirectoryNode:
    name: str
    location: Location
    identity: tuple
    parent: DirectoryNode | None = None
    row: int = 0
    entry: Entry | None = None
    children: list[DirectoryNode] | None = None
    has_children: bool | None = None


class DirectoryTreeModel(QAbstractItemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.session = None
        self.signature = None
        self.root = None
        self.domains = []
        self.suspended = False
        self.application_icons = ApplicationIcons()

    def set_session(self, session):
        self.beginResetModel()
        self.session = session
        self.application_icons.set_session(session)
        self.signature = session.signature if session else None
        self.domains = session.domains() if session else []
        name = None
        if session:
            for key in ("Device Name", "Display Name"):
                value = session.info.get(key)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
                    break
        title = f"“{name}”的备份" if name else "备份"
        self.root = DirectoryNode(title, Location(), ("root",)) if session else None
        self.endResetModel()

    def node(self, index):
        return index.internalPointer() if index.isValid() else None

    def index_for_node(self, node):
        return self.createIndex(node.row, 0, node) if node else QModelIndex()

    def index(self, row, column, parent=QModelIndex()):
        if column != 0 or row < 0:
            return QModelIndex()
        if not parent.isValid():
            return self.index_for_node(self.root) if self.root and row == 0 else QModelIndex()
        node = self.node(parent)
        children = node.children or []
        return self.index_for_node(children[row]) if row < len(children) else QModelIndex()

    def parent(self, index):
        node = self.node(index)
        return self.index_for_node(node.parent) if node else QModelIndex()

    def columnCount(self, parent=QModelIndex()):
        return 1

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            return 1 if self.root else 0
        node = self.node(parent)
        return len(node.children or []) if parent.column() == 0 else 0

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        node = self.node(index)
        if not node:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            if node is self.root and self.session:
                name = next(
                    (
                        self.session.info.get(key).strip()
                        for key in ("Device Name", "Display Name")
                        if isinstance(self.session.info.get(key), str)
                        and self.session.info.get(key).strip()
                    ),
                    None,
                )
                context = getattr(QObject.parent(self).window(), "language_context", None)
                if context:
                    return (
                        context.translate("{name}'s Backup", name=name)
                        if name
                        else context.translate("Backup")
                    )
            return node.name
        if role == Qt.ItemDataRole.ToolTipRole:
            return node.location.address
        if role == Qt.ItemDataRole.DecorationRole:
            application_icon = self.application_icons.for_directory(
                node.location.domain, node.location.path
            )
            if application_icon is not None:
                return application_icon
            return QIcon.fromTheme(
                "folder", QApplication.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
            )
        return None

    def flags(self, index):
        if index.isValid():
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return Qt.ItemFlag.NoItemFlags

    def hasChildren(self, parent=QModelIndex()):
        if not parent.isValid():
            return self.root is not None
        node = self.node(parent)
        if node.children is not None:
            return bool(node.children)
        if node.has_children is None:
            if self.suspended:
                return True
            if node.location.is_root:
                node.has_children = bool(self.domains)
            elif node.location.group:
                node.has_children = any(
                    split_domain(d)[0] == node.location.group for d in self.domains
                )
            else:
                node.has_children = self.session.has_directory_children(
                    node.location.domain, node.location.path
                )
        return node.has_children

    def canFetchMore(self, parent):
        node = self.node(parent)
        return bool(
            node and node.children is None and not self.suspended and self.hasChildren(parent)
        )

    def fetchMore(self, parent):
        if not self.canFetchMore(parent):
            return
        node = self.node(parent)
        children = []
        if node.location.is_root:
            groups = set()
            for domain in self.domains:
                group, _ = split_domain(domain)
                if group:
                    groups.add(group)
                else:
                    children.append(DirectoryNode(domain, Location(domain), ("domain", domain)))
            children.extend(
                DirectoryNode(group, Location(group=group), ("group", group)) for group in groups
            )
        elif node.location.group:
            for domain in self.domains:
                group, identifier = split_domain(domain)
                if group == node.location.group:
                    children.append(DirectoryNode(identifier, Location(domain), ("domain", domain)))
        else:
            for entry in self.session.directory_children(node.location.domain, node.location.path):
                children.append(
                    DirectoryNode(
                        entry.name,
                        Location(entry.domain, entry.path),
                        ("entry", entry.token),
                        entry=entry,
                    )
                )
        children.sort(key=lambda child: child.name.casefold())
        if children:
            self.beginInsertRows(parent, 0, len(children) - 1)
        node.children = children
        for row, child in enumerate(children):
            child.parent, child.row = node, row
        if children:
            self.endInsertRows()

    def ensure_index(self, location):
        if self.root is None:
            return QModelIndex()
        chain = []
        current = location
        while not current.is_root:
            chain.append(current)
            current = current.parent
        node = self.root
        for child_location in reversed(chain):
            self.fetchMore(self.index_for_node(node))
            node = next(
                (child for child in node.children or [] if child.location == child_location), None
            )
            if node is None:
                return QModelIndex()
        return self.index_for_node(node)

    def loaded_nodes(self):
        pending = [self.root] if self.root else []
        while pending:
            node = pending.pop()
            yield node
            pending.extend(node.children or [])

    def location_for_identity(self, identity):
        kind, *value = identity
        if kind == "root":
            return Location()
        if kind == "group":
            return Location(group=value[0])
        if kind == "domain":
            return Location(value[0])
        try:
            entry = self.session.get(value[0])
            return Location(entry.domain, entry.path) if entry.kind == "directory" else None
        except BackupError:
            return None


class DirectoryTree(QTreeView):
    navigate_requested = Signal(QModelIndex)
    open_requested = Signal(QModelIndex)
    focus_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.directory_model = DirectoryTreeModel(self)
        self.setModel(self.directory_model)
        self.setHeaderHidden(True)
        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.header().setResizeContentsPrecision(0)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setAccessibleName("备份文件夹树")
        self.setUniformRowHeights(True)
        self.setIndentation(18)
        self.setIconSize(QSize(18, 18))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setTabKeyNavigation(False)
        self.setMinimumWidth(140)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._location = None

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self.currentIndex().isValid():
                self.open_requested.emit(self.currentIndex())
            event.accept()
            return
        before = self.currentIndex()
        super().keyPressEvent(event)
        current = self.currentIndex()
        if current.isValid() and (
            current != before
            or event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right)
        ):
            self.navigate_requested.emit(current)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        index = self.indexAt(event.position().toPoint())
        if (
            event.button() == Qt.MouseButton.LeftButton
            and index.isValid()
            and self.visualRect(index).contains(event.position().toPoint())
        ):
            self.navigate_requested.emit(index)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.focus_changed.emit()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.focus_changed.emit()

    def current_entry(self):
        if not self.selectionModel().isSelected(self.currentIndex()):
            return None
        node = self.directory_model.node(self.currentIndex())
        return node.entry if node and node.entry and node.entry.path else None

    def sync(self, session, location):
        model = self.directory_model
        new_session = model.session is not session
        changed = new_session or model.signature != (session.signature if session else None)
        if not changed and self._location == location:
            return
        expanded = []
        selected = model.node(self.currentIndex())
        selected_identity = selected.identity if selected else None
        if changed and not new_session:
            expanded = [
                node.identity
                for node in model.loaded_nodes()
                if self.isExpanded(model.index_for_node(node))
            ]
        self.setUpdatesEnabled(False)
        try:
            if changed:
                model.set_session(session)
                if session:
                    if new_session:
                        self.expand(model.index_for_node(model.root))
                    else:
                        for identity in expanded:
                            target = model.location_for_identity(identity)
                            if target is not None:
                                self.expand(model.ensure_index(target))
            if not session:
                self._location = None
                return
            target_location = location
            if changed and not new_session and location == self._location and selected_identity:
                target_location = model.location_for_identity(selected_identity) or location
            index = model.ensure_index(target_location)
            if index.isValid():
                ancestor = index.parent()
                while ancestor.isValid():
                    self.expand(ancestor)
                    ancestor = ancestor.parent()
                self.setCurrentIndex(index)
                self.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)
            self._location = location
        finally:
            self.setUpdatesEnabled(True)
