import hashlib
import sqlite3
import threading
from pathlib import Path

import pytest
from conftest import make_backup

from planbak.backup import BackupSession
from planbak.codec import BackupError, Cancelled
from planbak.sqlite_database import (
    PAGE_SIZE,
    DatabaseSnapshot,
    is_sqlite,
    prepare_snapshot,
    query,
    schema,
)
from planbak.sqlite_viewer import SQLiteViewer, cell_text


def make_database(path):
    with sqlite3.connect(path) as c:
        c.executescript("""
            CREATE TABLE "items weird" (id INTEGER PRIMARY KEY, title TEXT, value BLOB);
            CREATE INDEX title_index ON "items weird"(title);
            CREATE VIEW sample_view AS SELECT id, title FROM "items weird";
        """)
        c.executemany(
            'INSERT INTO "items weird" VALUES(?,?,?)',
            [
                (i, "" if i == 0 else f"item-{i}", None if i == 0 else b"\x00\xff")
                for i in range(405)
            ],
        )


def test_database_query_schema_paging_and_readonly(tmp_path):
    path = tmp_path / "unusual.sql"
    make_database(path)
    original = path.read_bytes()
    assert is_sqlite(path)
    assert {r[1] for r in schema(path)} == {"table", "view", "index"}
    result = query(path, 'SELECT * FROM "items weird" ORDER BY id DESC')
    assert len(result.rows) == PAGE_SIZE and result.more and result.rows[0][0] == 404
    final = query(path, 'SELECT * FROM "items weird" ORDER BY id DESC', offset=400)
    assert len(final.rows) == 5 and not final.more
    assert query(path, "SELECT * FROM sample_view LIMIT 1").rows == [(0, "")]
    assert path.read_bytes() == original
    assert cell_text(None) == "NULL"
    assert "空字符串" in cell_text("") and "BLOB" in cell_text(b"")


@pytest.mark.parametrize(
    "sql",
    [
        'DELETE FROM "items weird"',
        'DROP TABLE "items weird"',
        "CREATE TABLE bad(x)",
        "ATTACH ':memory:' AS bad",
        "PRAGMA writable_schema=ON",
        "SELECT load_extension('/not-loaded')",
        "SELECT 1; SELECT 2",
        "WITH x AS (SELECT 1) DELETE FROM 'items weird'",
    ],
)
def test_database_refuses_mutations(tmp_path, sql):
    path = tmp_path / "test.db"
    make_database(path)
    original = path.read_bytes()
    with pytest.raises(sqlite3.Error):
        query(path, sql)
    assert path.read_bytes() == original


def test_snapshot_merges_wal_without_touching_inputs(tmp_path):
    root = make_backup(tmp_path / "backup")
    session = BackupSession(root)
    source = tmp_path / "active.db"
    c = sqlite3.connect(source)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA wal_autocheckpoint=0")
        c.execute("CREATE TABLE data(value)")
        c.execute("INSERT INTO data VALUES ('only in wal')")
        c.commit()
        session.import_paths([source, Path(str(source) + "-wal")], "HomeDomain", "Documents")
        token = session.at("HomeDomain", "Documents/active.db").token
        stamps = {p: p.read_bytes() for p in (source, Path(str(source) + "-wal"))}
        snapshot = prepare_snapshot(session, token, session.workspace / "snapshot-test")
        assert query(snapshot.path, "SELECT * FROM data").rows == [("only in wal",)]
        assert not (snapshot.directory / "input").exists()
        assert all(p.read_bytes() == data for p, data in stamps.items())
        other = prepare_snapshot(session, token, session.workspace / "snapshot-again")
        assert snapshot.fingerprint == other.fingerprint
    finally:
        c.close()
        session.close()


def test_snapshot_unknown_and_hot_journal_cleanup(tmp_path):
    session = BackupSession(make_backup(tmp_path / "backup"))
    try:
        source = tmp_path / "not.db"
        source.write_bytes(b"not sqlite")
        session.import_paths([source], "HomeDomain", "Documents")
        token = session.at("HomeDomain", "Documents/not.db").token
        folder = session.workspace / "bad"
        with pytest.raises(BackupError, match="not a standard SQLite"):
            prepare_snapshot(session, token, folder)
        assert not folder.exists()
        source = tmp_path / "real.db"
        make_database(source)
        journal = tmp_path / "real.db-journal"
        journal.write_bytes(b"nonempty")
        session.import_paths([source, journal], "HomeDomain", "Documents")
        token = session.at("HomeDomain", "Documents/real.db").token
        with pytest.raises(BackupError, match="rollback journal"):
            prepare_snapshot(session, token, folder)
        assert not folder.exists()
    finally:
        session.close()


def test_query_can_be_cancelled(tmp_path):
    path = tmp_path / "cancel.db"
    make_database(path)
    event = threading.Event()
    event.set()
    with pytest.raises((sqlite3.OperationalError, Cancelled)):
        query(
            path,
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT SUM(x) FROM n",
            cancel=event.is_set,
        )


def viewer(qtbot, tmp_path):
    path = tmp_path / "view.sqlite"
    make_database(path)
    snapshot = DatabaseSnapshot(tmp_path, path, hashlib.sha256(path.read_bytes()).digest())
    v = SQLiteViewer(snapshot, "view.sqlite")
    qtbot.addWidget(v)
    v.show()
    qtbot.waitUntil(lambda: v.task is None)
    return v


def test_viewer_table_sort_query_and_close(qtbot, tmp_path):
    from PySide6.QtCore import Qt

    v = viewer(qtbot, tmp_path)
    item = next(
        v.tree.topLevelItem(i)
        for i in range(v.tree.topLevelItemCount())
        if v.tree.topLevelItem(i).text(0) == "items weird"
    )
    v.tree.setCurrentItem(item)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.model.rowCount() == 200 and v.next_button.isEnabled()
    assert "title_index" in v.structure.toPlainText()
    v.sort_table(0, Qt.SortOrder.DescendingOrder)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.model.result.rows[0][0] == 404
    v.load_page(PAGE_SIZE)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.model.result.rows[0][0] == 204
    v.code.setPlainText('SELECT count(*) FROM "items weird"')
    v.execute_sql()
    qtbot.waitUntil(lambda: v.task is None)
    assert v.model.result.rows == [(405,)]
    v.close()
    assert not v.isVisible()


def test_viewer_close_cancels_worker_without_blocking(qtbot, tmp_path):
    v = viewer(qtbot, tmp_path)
    v.code.setPlainText(
        "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT SUM(x) FROM n"
    )
    v.execute_sql()
    assert v.task
    v.close()
    qtbot.waitUntil(lambda: not v.isVisible() and v.task is None, timeout=3000)


@pytest.mark.parametrize("module", ["fts3", "fts5"])
def test_query_supports_builtin_fulltext_tables(tmp_path, module):
    path = tmp_path / "fts.sqlite"
    c = sqlite3.connect(path)
    c.execute(f"CREATE VIRTUAL TABLE items USING {module}(text)")
    c.execute("INSERT INTO items VALUES ('searchable')")
    c.commit()
    c.close()
    assert query(path, "SELECT * FROM items WHERE items MATCH 'searchable'").rows == [
        ("searchable",)
    ]


def test_snapshot_reads_saved_plain_or_encrypted_backup(backup_root, tmp_path):
    from conftest import PASSWORD

    original_manifest = (backup_root / "Manifest.db").read_bytes()
    session = BackupSession(backup_root, PASSWORD)
    path = tmp_path / "database.store"
    make_database(path)
    output = tmp_path / "saved-backup"
    try:
        session.import_paths([path], "HomeDomain", "Documents")
        session.save(output)
    finally:
        session.close()
    reader = BackupSession(output, PASSWORD)
    try:
        token = reader.at("HomeDomain", "Documents/database.store").token
        assert not reader.cache_path(token).exists()
        snapshot = prepare_snapshot(reader, token, reader.workspace / "database-test")
        assert query(snapshot.path, 'SELECT count(*) FROM "items weird"').rows == [(405,)]
        assert not reader.dirty
        assert (backup_root / "Manifest.db").read_bytes() == original_manifest
    finally:
        reader.close()


def test_sidebar_keeps_focus_and_tracks_latest_keyboard_selection(qtbot, tmp_path, monkeypatch):
    import time

    from PySide6.QtCore import Qt

    import planbak.sqlite_viewer as module

    path = tmp_path / "navigation.db"
    c = sqlite3.connect(path)
    for name in ("alpha", "beta", "gamma"):
        c.execute(f"CREATE TABLE {name}(value)")
        c.execute(f"INSERT INTO {name} VALUES (?)", (name,))
    c.execute("CREATE VIEW zeta AS SELECT * FROM alpha")
    c.commit()
    c.close()
    v = SQLiteViewer(DatabaseSnapshot(tmp_path, path, b""), "navigation.db")
    qtbot.addWidget(v)
    v.show()
    v.activateWindow()
    qtbot.waitUntil(lambda: v.task is None and v.isActiveWindow())
    original_query = module.query
    started = threading.Event()
    count = [0]

    def delayed(*args, cancel, **kwargs):
        count[0] += 1
        if count[0] == 1:
            started.set()
            while not cancel():
                time.sleep(0.001)
            raise Cancelled()
        return original_query(*args, cancel=cancel, **kwargs)

    monkeypatch.setattr(module, "query", delayed)
    first = v.tree.topLevelItem(0)
    v.tree.setCurrentItem(None)
    qtbot.mouseClick(
        v.tree.viewport(), Qt.MouseButton.LeftButton, pos=v.tree.visualItemRect(first).center()
    )
    qtbot.waitUntil(started.is_set)
    assert v.tree.hasFocus() and v.tree.isEnabled()
    qtbot.keyClick(v.tree, Qt.Key.Key_Down)
    qtbot.keyClick(v.tree, Qt.Key.Key_Down)
    qtbot.keyClick(v.tree, Qt.Key.Key_Up)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.tree.currentItem().text(0) == "beta"
    assert v.object_name == "beta" and v.model.result.rows == [("beta",)]
    assert v.tree.hasFocus() and v.panes.sidebar_active
    assert v.tree.columnWidth(1) < v.tree.width() / 3
    assert v.tree.columnWidth(0) > v.tree.columnWidth(1) * 2
    qtbot.mouseClick(
        v.table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=v.table.visualRect(v.model.index(0, 0)).center(),
    )
    assert v.table.hasFocus() and not v.panes.sidebar_active
    assert v.tree.property("activeBrowserPane") is False
    assert v.table.property("activeBrowserPane") is True
    qtbot.keyClick(v.table, Qt.Key.Key_Up)
    assert v.tree.currentItem().text(0) == "beta"
    qtbot.mouseClick(
        v.tree.viewport(),
        Qt.MouseButton.LeftButton,
        pos=v.tree.visualItemRect(v.tree.currentItem()).center(),
    )
    assert v.tree.hasFocus() and v.panes.sidebar_active
    assert v.table.property("activeBrowserPane") is False
    v.close()


def test_sqlite_cell_dialog_returns_focus_to_data(qtbot, tmp_path):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    v = viewer(qtbot, tmp_path)
    v.tree.setCurrentItem(v.tree.topLevelItem(0))
    qtbot.waitUntil(lambda: v.task is None)
    v.activateWindow()
    qtbot.waitUntil(v.isActiveWindow)
    index = v.model.index(0, 0)

    def close_detail():
        dialog = QApplication.activeModalWidget()
        assert dialog is not None
        dialog.reject()
        v.tree.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    QTimer.singleShot(10, close_detail)
    v.show_cell(index)
    qtbot.waitUntil(v.table.hasFocus)
    assert not v.panes.sidebar_active
    v.close()


def test_sqlite_unselected_current_cell_does_not_paint_focus_rectangle(qtbot, tmp_path):
    from PySide6.QtCore import Qt

    v = viewer(qtbot, tmp_path)
    v.tree.setCurrentItem(v.tree.topLevelItem(0))
    qtbot.waitUntil(lambda: v.task is None)
    v.activateWindow()
    qtbot.waitUntil(v.isActiveWindow)
    v.panes.activate(v.table, focus=True)
    index = v.model.index(0, 0)
    v.table.setCurrentIndex(index)
    selected = v.table.viewport().grab().toImage()
    v.table.clearSelection()
    assert v.table.currentIndex() == index
    unselected_current = v.table.viewport().grab().toImage()
    assert selected != unselected_current
    v.table.selectionModel().clearCurrentIndex()
    assert unselected_current == v.table.viewport().grab().toImage()
    v.table.setCurrentIndex(index)
    qtbot.keyClick(v.table, Qt.Key.Key_Down)
    assert v.table.currentIndex().row() == 1
    v.close()
