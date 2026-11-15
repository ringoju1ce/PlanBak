from PySide6.QtCore import Qt
from test_ui import window as window

from planbak.navigation import Location


def test_file_columns_scroll_in_narrow_pane_and_fill_wide_pane(window, qtbot):
    w = window
    w.poll_timer.stop()
    w.session.new_directory('HomeDomain', 'Documents', 'pending-folder')
    w.navigate(Location('HomeDomain', 'Documents'))
    w.resize(1000, 650)
    w.splitter.setSizes([180, 550])
    qtbot.waitUntil(lambda: w.table.horizontalScrollBar().maximum() > 0)
    assert w.table.columnWidth(0) >= 320
    bar = w.table.horizontalScrollBar()
    bar.setValue(bar.maximum())
    assert w.table.columnViewportPosition(4) + w.table.columnWidth(4) <= w.table.viewport().width() + 1
    w.resize(1900, 650)
    qtbot.waitUntil(lambda: bar.maximum() == 0)
    assert w.table.columnWidth(0) > 320
    assert abs(sum(w.table.columnWidth(i) for i in range(5)) - w.table.viewport().width()) <= 1
    w.table.setColumnWidth(0, 440)
    w.resize(1000, 650)
    qtbot.waitUntil(lambda: bar.maximum() > 0)
    assert w.table.columnWidth(0) == 440


def test_directory_tree_scrolls_full_long_name_and_recomputes_when_collapsed(window, qtbot):
    w = window
    w.poll_timer.stop()
    name = 'long-directory-name-' * 10
    w.session.new_directory('HomeDomain', 'Documents', name)
    w.navigate(Location('HomeDomain', 'Documents'))
    tree = w.directory_tree
    w.splitter.setSizes([180, 700])
    index = tree.directory_model.ensure_index(Location('HomeDomain', 'Documents/' + name))
    tree.expand(index.parent())
    tree.scrollTo(index)
    bar = tree.horizontalScrollBar()
    qtbot.waitUntil(lambda: bar.maximum() > tree.viewport().width())
    widest = bar.maximum()
    bar.setValue(widest)
    assert bar.value() == widest
    assert tree.textElideMode() == Qt.TextElideMode.ElideNone
    tree.collapse(index.parent())
    qtbot.waitUntil(lambda: bar.maximum() < widest)
