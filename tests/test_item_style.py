import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import (
    QListWidget,
    QProxyStyle,
    QStyle,
    QStyleOptionButton,
    QStyleOptionFrame,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
)

from planbak.item_style import ItemViewStyle


@pytest.mark.parametrize("kind", ["list", "table", "tree"])
def test_future_item_views_inherit_global_style_without_delegate(qtbot, kind):
    if kind == "list":
        view = QListWidget()
        view.addItems(["first", "second"])
    elif kind == "table":
        view = QTableWidget(2, 1)
        view.setItem(0, 0, QTableWidgetItem("first"))
        view.setItem(1, 0, QTableWidgetItem("second"))
    else:
        view = QTreeWidget()
        view.addTopLevelItems([QTreeWidgetItem(["first"]), QTreeWidgetItem(["second"])])
    qtbot.addWidget(view)
    view.resize(300, 150)
    view.show()
    view.activateWindow()
    qtbot.waitUntil(view.isActiveWindow)
    view.setFocus()
    index = view.model().index(0, 0)
    view.setCurrentIndex(index)
    selected = view.viewport().grab().toImage()
    view.clearSelection()
    assert view.currentIndex() == index
    current_only = view.viewport().grab().toImage()
    assert current_only != selected
    view.selectionModel().clearCurrentIndex()
    assert view.viewport().grab().toImage() == current_only
    view.setCurrentIndex(index)
    qtbot.keyClick(view, Qt.Key.Key_Down)
    assert view.currentIndex().row() == 1
    view.close()


def test_global_style_changes_only_item_paint_focus_and_not_original_options():
    captured = []

    class RecordingStyle(QProxyStyle):
        def drawControl(self, element, option, painter, widget=None):
            captured.append((element, option.state))

        def drawPrimitive(self, element, option, painter, widget=None):
            captured.append((element, option.state))

    style = ItemViewStyle(RecordingStyle("Fusion"))
    image = QImage(20, 20, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    item = QStyleOptionViewItem()
    item.state = QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus
    style.drawControl(QStyle.ControlElement.CE_ItemViewItem, item, painter)
    assert captured[-1][1] & QStyle.StateFlag.State_Selected
    assert not captured[-1][1] & QStyle.StateFlag.State_HasFocus
    assert item.state & QStyle.StateFlag.State_HasFocus
    button = QStyleOptionButton()
    button.state |= QStyle.StateFlag.State_HasFocus
    style.drawControl(QStyle.ControlElement.CE_PushButton, button, painter)
    assert captured[-1][1] & QStyle.StateFlag.State_HasFocus
    entry = QStyleOptionFrame()
    entry.state |= QStyle.StateFlag.State_HasFocus
    style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelLineEdit, entry, painter)
    assert captured[-1][1] & QStyle.StateFlag.State_HasFocus
    painter.end()
