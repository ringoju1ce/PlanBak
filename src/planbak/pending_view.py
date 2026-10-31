"""Paged final-difference list; file paths remain plain data in rows/tooltips."""

import html

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyle


class PendingChangesModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = ()
        self.visible_count = 0

    def set_changes(self, changes):
        self.beginResetModel()
        self.rows = tuple(changes)
        self.visible_count = min(500, len(self.rows))
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else self.visible_count

    def canFetchMore(self, parent):
        return not parent.isValid() and self.visible_count < len(self.rows)

    def fetchMore(self, parent):
        if not self.canFetchMore(parent):
            return
        count = min(500, len(self.rows) - self.visible_count)
        self.beginInsertRows(parent, self.visible_count, self.visible_count + count - 1)
        self.visible_count += count
        self.endInsertRows()

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= self.visible_count:
            return None
        change = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return change.title
        if role == Qt.ItemDataRole.ToolTipRole:
            return "<qt>" + html.escape(change.tooltip) + "</qt>"
        if role == Qt.ItemDataRole.UserRole:
            return change.token
        if role == Qt.ItemDataRole.DecorationRole:
            directory = (change.after or change.before).kind == "directory"
            return QIcon.fromTheme(
                "folder" if directory else "text-x-generic",
                QApplication.style().standardIcon(
                    QStyle.StandardPixmap.SP_DirIcon
                    if directory
                    else QStyle.StandardPixmap.SP_FileIcon
                ),
            )
        return None
