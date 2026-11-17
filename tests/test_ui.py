from pathlib import Path

import pytest
from conftest import make_backup
from PySide6.QtCore import QItemSelectionModel, QMimeData, QModelIndex, QUrl

from planbak.app import (
    INTERNAL_MIME,
    BrowserRow,
    ExportMimeData,
    MainWindow,
    QFileDialog,
    QInputDialog,
)
from planbak.backup import BackupSession


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    monkeypatch.setattr(
        "planbak.app.QSettings",
        lambda *args: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
    )
    from planbak.backup_windows import BackupWindows

    w = MainWindow(BackupWindows(tmp_path / "locks"), language="zh_CN")
    w.session = BackupSession(make_backup(tmp_path / "backup"))
    w.refresh()
    w.show()
    yield w
    if w.task:
        w.task.cancelled.set()
        qtbot.waitUntil(lambda: w.task is None)
    w.force_close = True
    w.close()
    qtbot.waitUntil(lambda: not w.isVisible())


def choose(window, name):
    row = next(i for i, r in enumerate(window.model.rows) if r.name == name)
    index = window.model.index(row, 0)
    window.active_view().selectionModel().select(
        index,
        QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows,
    )
    return index


def test_navigation_selection_grid(window):
    w = window
    assert not w.open_action.isEnabled()
    w.activate(choose(w, "HomeDomain"))
    assert w.model.rows[0].name == ".."
    w.activate(choose(w, "Documents"))
    assert w.path == "Documents"
    choose(w, "a.txt")
    assert w.open_action.isEnabled()
    w.set_view(True)
    assert w.active_view() == w.grid
    w.select_all()
    assert len(w.selected_tokens()) == 3
    w.up()
    assert w.path == "" and w.domain == "HomeDomain"
    w.up()
    assert w.domain is None


def test_async_import_rename_move_and_save(window, qtbot, tmp_path, monkeypatch):
    w = window
    w.domain, w.path = "HomeDomain", "Documents"
    w.refresh()
    source = tmp_path / "added.txt"
    source.write_text("UI-added content")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source))])
    assert w.handle_drop(mime, QModelIndex())
    qtbot.waitUntil(lambda: w.task is None, timeout=5000)
    choose(w, "added.txt")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("renamed.txt", True))
    w.rename_selected()
    assert w.session.at("HomeDomain", "Documents/renamed.txt")
    token = w.session.at("HomeDomain", "Documents/renamed.txt").token
    import json

    moving = QMimeData()
    moving.setData(
        INTERNAL_MIME, json.dumps({"session": w.session.session_id, "tokens": [token]}).encode()
    )
    target = w.model.index(next(i for i, r in enumerate(w.model.rows) if r.name == "sub"), 0)
    assert w.handle_drop(moving, target)
    qtbot.waitUntil(lambda: w.task is None)
    assert w.session.at("HomeDomain", "Documents/sub/renamed.txt")
    output = tmp_path / "saved"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(output), ""))
    monkeypatch.setattr("planbak.app.message", lambda *a, **k: None)
    w.save()
    qtbot.waitUntil(lambda: w.task is None, timeout=5000)
    assert output.is_dir()
    assert not w.session.dirty
    assert not w.save_action.isEnabled()


def test_external_open_and_detection(window, qtbot, monkeypatch):
    w = window
    w.domain, w.path = "HomeDomain", "Documents"
    w.refresh()
    choose(w, "a.txt")
    monkeypatch.setattr("planbak.app.QDesktopServices.openUrl", lambda url: True)
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None)
    record = next(iter(w.external_edits.records.values()))
    assert record.path.read_bytes() == b"hello world"
    record.path.write_bytes(b"updated outside")
    observed = []
    monkeypatch.setattr(w.external_edits, "review_external", lambda rec, fp, stamp: observed.append((rec, fp)))
    w.poll_open_files()
    w.poll_open_files()
    qtbot.waitUntil(lambda: w.task is None)
    assert observed and observed[0][0] is record
    assert record.unreviewed


def test_drag_export_lazy(window, qtbot):
    entry = window.session.at("HomeDomain", "Documents/a.txt")
    data = ExportMimeData(window, [entry.token])
    assert data.exported is None
    assert data.hasFormat(INTERNAL_MIME)
    urls = bytes(data.data("text/uri-list")).decode().strip()
    assert urls.startswith("file:")
    assert Path(QUrl(urls).toLocalFile()).read_bytes() == b"hello world"


def test_paged_model_select_all(window):
    rows = [BrowserRow(f"file{i}", "HomeDomain", f"f{i}", "file") for i in range(1200)]
    window.model.set_rows(rows)
    assert window.model.rowCount() == 500
    window.select_all()
    assert window.model.rowCount() == 1200
    assert len(window.selected_rows()) == 1200


def test_shortcuts_do_not_delete_when_editing_location(window, qtbot):
    from PySide6.QtCore import Qt

    w = window
    w.domain, w.path = "HomeDomain", "Documents"
    w.refresh()
    choose(w, "a.txt")
    w.location.setFocus()
    qtbot.keyClick(w.location, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    qtbot.keyClick(w.location, Qt.Key.Key_Delete)
    assert w.location.text() == ""
    assert not w.session.dirty
    qtbot.keyClick(w.location, Qt.Key.Key_Backspace)
    assert w.path == "Documents"


def test_stage_external_edit_prompt(window, qtbot):
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    from planbak.backup import digest
    from planbak.external_edits import OpenedFile, file_stamp

    w = window
    entry = w.session.at("HomeDomain", "Documents/a.txt")
    path = w.session.workspace / "external.txt"
    path.write_bytes(b"new editor contents")
    record = OpenedFile(entry.token, path, b"old", (0,))

    def accept():
        box = QApplication.activeModalWidget()
        assert isinstance(box, QMessageBox)
        next(button for button in box.buttons() if button.text() == "暂挂更改").click()

    QTimer.singleShot(30, accept)
    w.external_edits.review_external(record, digest(path), file_stamp(path))
    qtbot.waitUntil(lambda: w.task is None)
    assert w.session.dirty
    staged = Path(w.session.get(entry.token).staged)
    assert staged.read_bytes() == b"new editor contents"


def test_save_before_leaving(window, qtbot, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    w = window
    w.session.new_directory("HomeDomain", "", "unsaved")
    output = tmp_path / "on-exit"
    monkeypatch.setattr(QMessageBox, "exec", lambda _: QMessageBox.StandardButton.Save)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(output), ""))
    monkeypatch.setattr("planbak.app.message", lambda *a, **k: None)
    called = []
    w.confirm_leave(lambda: called.append(True))
    qtbot.waitUntil(lambda: w.task is None)
    assert called == [True] and output.is_dir() and not w.session.dirty


def test_startup_continues_after_temp_cleanup(window, monkeypatch):
    monkeypatch.setattr("planbak.app.stale_workspaces", lambda: [])
    called = []
    window.offer_temp_cleanup(lambda: called.append(True))
    assert called == [True]


def seed_domains(session, domains):
    with session.db:
        session.db.executemany(
            "INSERT INTO EditorEntries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f"root-{d}", None, d, "", "", "", "directory", 0, 0, None, 0, 0) for d in domains],
        )


def paged_choose(window, name):
    number = next(i for i, row in enumerate(window.model.rows) if row.name == name)
    while window.model.rowCount() <= number:
        window.model.fetchMore(QModelIndex())
    return choose(window, name)


def test_grouped_domains_navigation_and_drop_mapping(window):
    from planbak.navigation import DOMAIN_GROUPS, Location

    w = window
    domains = [family + "-com.example.with-hyphen" for family in DOMAIN_GROUPS]
    seed_domains(w.session, domains + ["UnknownDomain-hyphen"])
    w.refresh()
    names = [row.name for row in w.model.rows]
    assert all(family in names for family in DOMAIN_GROUPS)
    assert "AppDomain-com.example.with-hyphen" not in names
    assert "UnknownDomain-hyphen" in names and "HomeDomain" in names
    group = choose(w, "AppDomain")
    assert w.drop_location(group) == (None, "")
    assert not w.rename_action.isEnabled()
    assert not w.delete_action.isEnabled()
    w.activate(group)
    assert w.current_location() == Location(group="AppDomain")
    assert w.location.text() == "/AppDomain"
    assert not w.add_action.isEnabled() and w.up_action.isEnabled()
    identifier = choose(w, "com.example.with-hyphen")
    assert w.drop_location(identifier) == ("AppDomain-com.example.with-hyphen", "")
    w.activate(identifier)
    assert w.domain == "AppDomain-com.example.with-hyphen"
    assert w.location.text() == "/AppDomain/com.example.with-hyphen"
    assert w.add_action.isEnabled()
    assert w.drop_location(w.model.index(0, 0)) == (None, "")
    w.up()
    assert w.current_location() == Location(group="AppDomain")
    w.up()
    assert w.current_location().is_root
    w.location.setText("/AppDomain-com.example.with-hyphen")
    w.go_location()
    assert w.location.text() == "/AppDomain/com.example.with-hyphen"
    assert set(domains) <= set(w.session.domains())


def test_grouped_domain_import_save_keeps_original_domain(window, qtbot, tmp_path, monkeypatch):
    from planbak.navigation import Location

    w = window
    domain = "AppDomain-com.example"
    seed_domains(w.session, [domain])
    w.navigate(Location(group="AppDomain"))
    source = tmp_path / "example.txt"
    source.write_bytes(b"belongs to actual domain")
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source))])
    target = choose(w, "com.example")
    assert w.handle_drop(mime, target)
    qtbot.waitUntil(lambda: w.task is None)
    assert w.session.at(domain, "example.txt")
    output = tmp_path / "grouped-output"
    w.session.save(output)
    reader = BackupSession(output)
    try:
        assert domain in reader.domains() and "AppDomain" not in reader.domains()
        assert reader.at(domain, "example.txt")
    finally:
        reader.close()


@pytest.mark.parametrize("grid", [False, True], ids=["list", "grid"])
def test_return_restores_paged_group_position_selection_and_focus(window, qtbot, grid):
    from PySide6.QtWidgets import QAbstractItemView

    from planbak.navigation import Location

    w = window
    seed_domains(w.session, [f"AppDomain-com.example.app{i:04}" for i in range(1400)])
    w.set_view(grid)
    w.navigate(Location(group="AppDomain"))
    index = paged_choose(w, "com.example.app1100")
    view = w.active_view()
    view.setFocus()
    view.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
    second = w.model.index(index.row() + 1, 0)
    view.selectionModel().select(
        second, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    )
    view.doItemsLayout()
    view.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
    qtbot.wait(30)
    scroll = view.verticalScrollBar().value()
    names = {row.name for row in w.selected_rows()}
    assert scroll > 0 and len(names) == 2
    w.activate(index)
    assert w.domain == "AppDomain-com.example.app1100"
    qtbot.wait(10)
    w.up()
    qtbot.wait(30)
    assert w.current_location() == Location(group="AppDomain")
    assert {row.name for row in w.selected_rows()} == names
    current = view.selectionModel().currentIndex()
    assert w.model.rows[current.row()].name == "com.example.app1100"
    assert w.model.rowCount() > 1100
    assert abs(view.verticalScrollBar().value() - scroll) <= 2
    assert view.viewport().rect().intersects(view.visualRect(current))
    assert view.hasFocus()


@pytest.mark.parametrize("grid", [False, True])
def test_return_after_switching_views_and_sorting(window, qtbot, grid):
    from PySide6.QtWidgets import QAbstractItemView

    from planbak.navigation import Location

    w = window
    seed_domains(w.session, [f"CustomDomain{i:04}" for i in range(1000)])
    w.set_view(grid)
    w.navigate(Location())
    index = paged_choose(w, "CustomDomain0850")
    w.active_view().selectionModel().setCurrentIndex(
        index, QItemSelectionModel.SelectionFlag.NoUpdate
    )
    w.active_view().doItemsLayout()
    w.active_view().scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
    qtbot.wait(20)
    w.activate(index)
    w.set_view(not grid)
    w.up()
    qtbot.wait(30)
    assert [row.name for row in w.selected_rows()] == ["CustomDomain0850"]
    view = w.active_view()
    current = view.selectionModel().currentIndex()
    assert view.viewport().rect().intersects(view.visualRect(current))
    w.descending.setChecked(not w.descending.isChecked())
    qtbot.wait(30)
    assert [row.name for row in w.selected_rows()] == ["CustomDomain0850"]
    current = view.selectionModel().currentIndex()
    assert w.model.rows[current.row()].name == "CustomDomain0850"
    assert view.viewport().rect().intersects(view.visualRect(current))


def test_refresh_preserves_renamed_identity_and_does_not_select_deleted_neighbor(window, qtbot):
    from planbak.navigation import Location

    w = window
    w.navigate(Location("HomeDomain", "Documents"))
    index = choose(w, "a.txt")
    view = w.active_view()
    view.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
    token = w.session.at("HomeDomain", "Documents/a.txt").token
    w.session.move([token], "HomeDomain", "Documents", name="renamed.txt")
    w.refresh()
    qtbot.wait(10)
    assert [row.name for row in w.selected_rows()] == ["renamed.txt"]
    w.session.delete([token])
    w.refresh()
    qtbot.wait(10)
    assert not w.selected_rows()
    assert view.selectionModel().currentIndex().isValid()
    assert w.model.rows[view.selectionModel().currentIndex().row()].name != "renamed.txt"


def test_new_session_clears_navigation_state(window, tmp_path):
    from planbak.navigation import Location

    w = window
    w.navigate(Location("HomeDomain", "Documents"))
    w.navigate(Location("HomeDomain", "Documents/sub"))
    assert w._view_states
    previous = w.session
    w.session = BackupSession(make_backup(tmp_path / "other-backup"))
    w.navigate(Location())
    assert not w._view_states and not w._latest_view_states
    previous.close()


@pytest.mark.parametrize("grid", [False, True])
def test_actual_directory_return_and_address_restore(window, qtbot, grid):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QAbstractItemView

    from planbak.navigation import Location

    w = window
    with w.session.db:
        w.session.db.executemany(
            "INSERT INTO EditorEntries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f"folder-{i}",
                    None,
                    "HomeDomain",
                    f"Documents/folder{i:04}",
                    "Documents",
                    f"folder{i:04}",
                    "directory",
                    0,
                    0,
                    None,
                    0,
                    0,
                )
                for i in range(1000)
            ],
        )
    w.set_view(grid)
    w.navigate(Location("HomeDomain", "Documents"))
    index = paged_choose(w, "folder0850")
    view = w.active_view()
    view.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
    view.doItemsLayout()
    view.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
    qtbot.wait(20)
    original_scroll = view.verticalScrollBar().value()
    w.location.setText("/HomeDomain/Documents/folder0850")
    w.go_location()
    qtbot.keyClick(view, Qt.Key.Key_Backspace)
    qtbot.wait(30)
    assert w.current_location() == Location("HomeDomain", "Documents")
    assert [row.name for row in w.selected_rows()] == ["folder0850"]
    assert abs(view.verticalScrollBar().value() - original_scroll) <= 2
    w.activate(w.model.index(view.selectionModel().currentIndex().row(), 0))
    w.location.setText("/HomeDomain/Documents")
    w.go_location()
    qtbot.wait(30)
    assert [row.name for row in w.selected_rows()] == ["folder0850"]
    assert abs(view.verticalScrollBar().value() - original_scroll) <= 2


def test_virtual_group_rejects_import_and_supports_enter_menu(window, monkeypatch):
    from PySide6.QtCore import Qt

    from planbak.navigation import Location

    w = window
    seed_domains(w.session, ["AppDomain-com.example"])
    w.refresh()
    index = choose(w, "AppDomain")
    assert not (w.model.flags(index) & Qt.ItemFlag.ItemIsDropEnabled)
    messages = []
    monkeypatch.setattr("planbak.app.message", lambda *args, **kwargs: messages.append(args))
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile("/not-imported")])
    assert not w.handle_drop(mime, index)
    assert not w.session.dirty and messages
    w.navigate(Location(group="AppDomain"))
    assert not w.add_action.isEnabled()
    assert not w.new_folder_action.isEnabled()
    assert not w.rename_action.isEnabled()


def test_exit_clean_removes_workspace_on_first_close(window, qtbot):
    workspace = window.session.workspace
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert window.session is None
    assert not workspace.exists()
    assert not window.poll_timer.isActive()


def test_exit_discard_removes_pending_files_without_changing_source(
    window, qtbot, tmp_path, monkeypatch
):
    from planbak.app import QMessageBox

    w = window
    source = tmp_path / "unsaved.txt"
    source.write_bytes(b"discard me")
    w.session.import_paths([source], "HomeDomain", "Documents")
    workspace = w.session.workspace
    manifest = w.session.root / "Manifest.db"
    original = manifest.read_bytes()

    def discard(box):
        assert box.button(QMessageBox.StandardButton.Save).text() == "应用更改"
        assert box.button(QMessageBox.StandardButton.Discard).text() == "放弃更改"
        return QMessageBox.StandardButton.Discard

    monkeypatch.setattr(QMessageBox, "exec", discard)
    monkeypatch.setattr(w, "save", lambda **kwargs: pytest.fail("discard must not save"))
    w.close()
    qtbot.waitUntil(lambda: not w.isVisible())
    assert not workspace.exists()
    assert manifest.read_bytes() == original


def test_exit_apply_saves_and_stays_open_without_extra_dialog(window, qtbot, tmp_path, monkeypatch):
    from planbak.app import QMessageBox

    w = window
    w.session.new_directory("HomeDomain", "", "applied-on-exit")
    workspace = w.session.workspace
    output = tmp_path / "saved-on-exit"
    monkeypatch.setattr(QMessageBox, "exec", lambda box: QMessageBox.StandardButton.Save)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(output), ""))
    monkeypatch.setattr(
        "planbak.app.message", lambda *args, **kwargs: pytest.fail("unexpected extra dialog")
    )
    w.close()
    qtbot.waitUntil(lambda: w.task is None, timeout=5000)
    assert output.is_dir() and workspace.exists()
    assert w.isVisible() and not w.session.dirty and not w.force_close
    assert w.session.root == output
    w.close()
    qtbot.waitUntil(lambda: not w.isVisible())
    assert not workspace.exists()
    reader = BackupSession(output)
    try:
        assert reader.at("HomeDomain", "applied-on-exit")
    finally:
        reader.close()


@pytest.mark.parametrize("cancel_destination", [False, True])
def test_exit_cancellation_keeps_edits_and_workspace(
    window, qtbot, monkeypatch, cancel_destination
):
    from planbak.app import QMessageBox

    w = window
    w.session.new_directory("HomeDomain", "", "keep-pending")
    workspace = w.session.workspace
    result = (
        QMessageBox.StandardButton.Save if cancel_destination else QMessageBox.StandardButton.Cancel
    )
    monkeypatch.setattr(QMessageBox, "exec", lambda box: result)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    w.close()
    qtbot.wait(20)
    assert w.isVisible() and workspace.exists() and w.session.dirty


def test_exit_applies_unreviewed_external_editor_copy(window, qtbot, tmp_path, monkeypatch):
    from planbak.app import QMessageBox
    from planbak.backup import digest
    from planbak.external_edits import OpenedFile, file_stamp

    w = window
    entry = w.session.at("HomeDomain", "Documents/a.txt")
    path = w.session.workspace / "opened.txt"
    path.write_bytes(b"original")
    record = OpenedFile(entry.token, path, digest(path), file_stamp(path))
    w.external_edits.records[entry.token] = record
    path.write_bytes(b"external change at exit")
    workspace = w.session.workspace
    output = tmp_path / "external-on-exit"
    monkeypatch.setattr(QMessageBox, "exec", lambda box: QMessageBox.StandardButton.Save)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(output), ""))
    monkeypatch.setattr(
        "planbak.app.message", lambda *args, **kwargs: pytest.fail("unexpected extra dialog")
    )
    w.close()
    qtbot.waitUntil(lambda: w.task is None, timeout=5000)
    assert w.isVisible() and workspace.exists() and not w.session.dirty
    assert not w.force_close and w.session.root == output
    reader = BackupSession(output)
    try:
        target = tmp_path / "verified.txt"
        reader.extract_file(reader.at("HomeDomain", "Documents/a.txt").token, target)
        assert target.read_bytes() == b"external change at exit"
    finally:
        reader.close()


def test_exit_waits_for_worker_and_cleans_late_open_result(window, qtbot, tmp_path):
    import time

    w = window
    workspace = w.session.workspace
    root = make_backup(tmp_path / "late-backup")
    created = []

    def opening(progress, cancel):
        while not cancel():
            time.sleep(0.002)
        session = BackupSession(root)
        created.append(session.workspace)
        return session

    w.run_task("读取备份", opening, lambda result: pytest.fail("must not attach late session"))
    w.close()
    qtbot.waitUntil(lambda: not w.isVisible(), timeout=5000)
    assert not workspace.exists()
    assert created and not created[0].exists()


def test_exit_failed_save_preserves_workspace(window, qtbot, tmp_path, monkeypatch):
    from planbak.app import QMessageBox

    w = window
    w.session.new_directory("HomeDomain", "", "still-pending")
    workspace = w.session.workspace
    existing = tmp_path / "existing-output"
    existing.mkdir()
    errors = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: QMessageBox.StandardButton.Save)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(existing), ""))
    monkeypatch.setattr("planbak.app.message", lambda *args, **kwargs: errors.append(args))
    w.close()
    qtbot.waitUntil(lambda: w.task is None)
    assert errors and w.isVisible() and workspace.exists() and w.session.dirty


def test_sidebar_lazy_directories_and_sync(window, monkeypatch):
    from planbak.navigation import Location

    w = window
    tree = w.directory_tree
    model = tree.directory_model
    monkeypatch.setattr(
        w.session, "extract_file", lambda *a, **k: pytest.fail("tree extracted content")
    )
    home = model.ensure_index(Location("HomeDomain"))
    assert model.node(home).children is None
    model.fetchMore(home)
    names = [model.index(i, 0, home).data() for i in range(model.rowCount(home))]
    assert names == ["Documents", "implicit"]
    w.navigate(Location("HomeDomain", "Documents/sub"))
    selected = tree.currentIndex()
    assert model.node(selected).location == w.current_location()
    assert tree.isExpanded(selected.parent())
    assert model.rowCount(selected) == 0


def test_sidebar_keyboard_focus(window, qtbot):
    from PySide6.QtCore import Qt

    from planbak.navigation import Location

    w = window
    tree = w.directory_tree
    model = tree.directory_model
    home = model.ensure_index(Location("HomeDomain"))
    tree.setCurrentIndex(home)
    tree.setFocus()
    qtbot.keyClick(tree, Qt.Key.Key_Right)
    assert tree.isExpanded(home)
    assert w.current_location() == Location("HomeDomain")
    qtbot.keyClick(tree, Qt.Key.Key_Right)
    assert w.current_location() == Location("HomeDomain", "Documents")
    qtbot.wait(10)
    assert tree.hasFocus()
    qtbot.keyClick(tree, Qt.Key.Key_Right)
    qtbot.keyClick(tree, Qt.Key.Key_Right)
    assert w.path == "Documents/sub"
    qtbot.keyClick(tree, Qt.Key.Key_Left)
    assert w.path == "Documents"
    qtbot.keyClick(tree, Qt.Key.Key_Return)
    qtbot.wait(10)
    assert w.active_view().hasFocus()


def test_sidebar_context_menu_and_staged_rename_delete(window, qtbot, monkeypatch):
    from planbak.app import QMenu, QMessageBox
    from planbak.navigation import Location

    w = window
    tree = w.directory_tree
    w.navigate(Location("HomeDomain", "Documents/sub"))
    documents = tree.directory_model.ensure_index(Location("HomeDomain", "Documents"))
    observed = []

    def menu_exec(menu, *args):
        actions = menu.actions()
        observed.append([(a.text(), a.isEnabled()) for a in actions])
        return actions[2]

    class TestMenu(QMenu):
        def exec(self, *args):
            return menu_exec(self, *args)

    monkeypatch.setattr("planbak.app.QMenu", TestMenu)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Renamed", True))
    w.sidebar_context_menu(tree.visualRect(documents).center())
    assert observed == [[("打开", True), ("删除", True), ("重命名", True)]]
    assert w.path == "Renamed/sub"
    assert w.session.dirty
    assert tree.directory_model.node(tree.currentIndex()).location == w.current_location()
    monkeypatch.setattr(QMessageBox, "exec", lambda *a: QMessageBox.StandardButton.Yes)
    entry = w.session.at("HomeDomain", "Renamed")
    w.delete_entries([entry.token], entry.name, focus_browser=False)
    qtbot.waitUntil(lambda: w.task is None)
    assert w.path == "" and w.domain == "HomeDomain"
    w.session.revert(w.session.plan_revert(entry.token))
    w.refresh()
    restored = tree.directory_model.ensure_index(Location("HomeDomain", "Documents/sub"))
    assert restored.isValid()
    root = tree.directory_model.ensure_index(Location())

    def root_menu_exec(menu, *args):
        observed.append([(a.text(), a.isEnabled()) for a in menu.actions()])

    monkeypatch.setattr(TestMenu, "exec", root_menu_exec)
    w.sidebar_context_menu(tree.visualRect(root).center())
    assert observed[-1] == [("打开", True), ("删除", False), ("重命名", False)]


def test_sidebar_expansion_and_visibility(window):
    from planbak.navigation import Location

    w = window
    tree = w.directory_tree
    documents = tree.directory_model.ensure_index(Location("HomeDomain", "Documents"))
    tree.expand(documents)
    w.session.new_directory("HomeDomain", "", "New")
    w.refresh()
    documents = tree.directory_model.ensure_index(Location("HomeDomain", "Documents"))
    assert tree.isExpanded(documents)
    w.sidebar_action.setChecked(False)
    assert tree.isHidden()
    w.sidebar_action.setChecked(True)
    assert not tree.isHidden()
    assert w.splitter.sizes()[0] >= 140


def test_sidebar_groups_and_context_open(window, qtbot, monkeypatch):
    from planbak.app import QMenu
    from planbak.navigation import DOMAIN_GROUPS, Location

    w = window
    tree = w.directory_tree
    seed_domains(w.session, [family + "-com.example.app" for family in DOMAIN_GROUPS])
    tree.directory_model.signature = None
    w.refresh()
    for family in DOMAIN_GROUPS:
        index = tree.directory_model.ensure_index(Location(family + "-com.example.app"))
        assert index.data() == "com.example.app"
        assert index.parent().data() == family
    target = tree.directory_model.ensure_index(Location("HomeDomain", "Documents"))
    tree.scrollTo(target)
    qtbot.wait(10)

    class OpenMenu(QMenu):
        def exec(self, *args):
            return self.actions()[0]

    monkeypatch.setattr("planbak.app.QMenu", OpenMenu)
    w.sidebar_context_menu(tree.visualRect(target).center())
    qtbot.wait(10)
    assert w.current_location() == Location("HomeDomain", "Documents")
    assert w.active_view().hasFocus()


def test_application_icons_root_only_and_shared_cache(window):
    from PySide6.QtCore import QBuffer, QIODevice, Qt
    from PySide6.QtGui import QColor, QImage

    from planbak.navigation import Location

    w = window
    image = QImage(32, 32, QImage.Format.Format_ARGB32)
    image.fill(QColor("#123abc"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    seed_domains(
        w.session,
        [
            "AppDomain-com.example.app",
            "AppDomainGroup-com.example.app",
            "AppDomainPlugin-com.example.app",
        ],
    )
    w.session.info["Applications"] = {"com.example.app": {"PlaceholderIcon": bytes(buffer.data())}}
    w.model.application_icons.set_session(None)
    w.directory_tree.directory_model.signature = None
    w.navigate(Location(group="AppDomain"))
    index = w.model.index(1, 0)  # .., then app root
    icon = w.model.data(index, Qt.ItemDataRole.DecorationRole)
    assert icon.pixmap(32, 32).toImage().pixelColor(16, 16) == QColor("#123abc")
    tree_model = w.directory_tree.directory_model
    tree_index = tree_model.ensure_index(Location("AppDomain-com.example.app"))
    tree_icon = tree_model.data(tree_index, Qt.ItemDataRole.DecorationRole)
    assert tree_icon.cacheKey() == icon.cacheKey()
    cache = w.model.application_icons
    assert cache.for_directory("AppDomain-com.example.app") is icon
    assert cache.for_directory("AppDomain-com.example.app", "Documents") is None
    assert cache.for_directory("AppDomainGroup-com.example.app") is None
    assert cache.for_directory("AppDomainPlugin-com.example.app") is None
    assert cache.for_directory("HomeDomain") is None
    w.set_view(True)
    assert w.grid.model().data(index, Qt.ItemDataRole.DecorationRole).cacheKey() == icon.cacheKey()


def test_application_icons_invalid_metadata_and_session_reset(qapp):
    from types import SimpleNamespace

    from planbak.icons import ApplicationIcons

    icons = ApplicationIcons()
    icons.set_session(
        SimpleNamespace(
            session_id="first",
            info={
                "Applications": {
                    "missing": {},
                    "invalid": {"PlaceholderIcon": b"invalid png"},
                    "wrongtype": {"PlaceholderIcon": "base64 text"},
                    "badentry": [],
                }
            },
        )
    )
    for name in ("missing", "invalid", "wrongtype", "badentry", "unknown"):
        assert icons.for_directory("AppDomain-" + name) is None
    assert len(icons.cache) == 5
    icons.set_session(SimpleNamespace(session_id="second", info={"Applications": []}))
    assert not icons.cache and icons.applications == {}
    icons.for_directory("AppDomain-missing")
    icons.set_session(None)
    assert icons.session_id is None and not icons.cache and not icons.applications


@pytest.mark.parametrize("mode", ["tree", "xml"])
@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("binary", [False, True])
def test_internal_plist_edit_stage_and_restore(
    window, qtbot, tmp_path, monkeypatch, encrypted, binary, mode
):
    import plistlib

    from conftest import PASSWORD

    from planbak.codec import file_id
    from planbak.navigation import Location

    w = window
    w.session.close()
    source = make_backup(tmp_path / "plist-backup", encrypted=encrypted)
    w.session = BackupSession(source, PASSWORD if encrypted else "")
    if binary:
        initial = tmp_path / "binary.plist"
        initial.write_bytes(plistlib.dumps({"sample": True}, fmt=plistlib.FMT_BINARY))
        w.session.stage_edit(w.session.at("SystemPreferencesDomain", "config.plist").token, initial)
    original_manifest = (source / "Manifest.db").read_bytes()
    fid = file_id("SystemPreferencesDomain", "config.plist")
    original_payload = (source / fid[:2] / fid).read_bytes()
    w.navigate(Location("SystemPreferencesDomain"))
    monkeypatch.setattr(
        "planbak.app.QDesktopServices.openUrl", lambda *a: pytest.fail("launched external app")
    )
    w.activate(choose(w, "config.plist"))
    qtbot.waitUntil(lambda: w.task is None and bool(w.plist_editors))
    editor = next(iter(w.plist_editors.values())).editor
    temporary_directory = w.session.workspace / "plist-editor"
    if mode == "xml":
        editor.switch_mode(1)
        editor.xml.code.setPlainText(editor.xml.code.toPlainText().replace("<true/>", "<false/>"))
    else:
        editor.document.root.children[0].value = False
    editor.update_actions()
    assert editor.save_document()
    assert w.session.dirty and not editor.document.dirty
    assert not w.external_edits.records
    editor.close()
    assert not w.plist_editors
    assert not list(temporary_directory.iterdir())
    token = w.session.at("SystemPreferencesDomain", "config.plist").token
    extracted = tmp_path / "edited.plist"
    w.session.extract_file(token, extracted)
    assert plistlib.loads(extracted.read_bytes()) == {"sample": False}
    edited_data = extracted.read_bytes()
    w.session.revert(w.session.plan_revert(token))
    w.session.extract_file(token, extracted)
    assert plistlib.loads(extracted.read_bytes()) == {"sample": True}
    w.session.stage_bytes(token, edited_data)
    destination = tmp_path / "saved-plist-backup"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(destination), ""))
    monkeypatch.setattr("planbak.app.message", lambda *a, **k: None)
    w.save()
    qtbot.waitUntil(lambda: w.task is None, timeout=5000)
    restored = BackupSession(destination, PASSWORD if encrypted else "")
    try:
        restored.extract_file(
            restored.at("SystemPreferencesDomain", "config.plist").token, extracted
        )
        data = extracted.read_bytes()
        assert plistlib.loads(data) == {"sample": False}
        assert data.startswith(b"bplist00") == binary
    finally:
        restored.close()
    assert (source / "Manifest.db").read_bytes() == original_manifest
    assert (source / fid[:2] / fid).read_bytes() == original_payload


def open_test_plist(window, qtbot):
    from planbak.navigation import Location

    window.poll_timer.stop()
    window.navigate(Location("SystemPreferencesDomain"))
    window.activate(choose(window, "config.plist"))
    qtbot.waitUntil(lambda: window.task is None and bool(window.plist_editors))
    return next(iter(window.plist_editors.values()))


def test_plist_single_instance_multiple_files(
    window, qtbot, tmp_path, monkeypatch
):
    import plistlib

    w = window
    record = open_test_plist(w, qtbot)
    editor = record.editor
    calls = []
    monkeypatch.setattr("planbak.app.QDesktopServices.openUrl", lambda *a: calls.append(a))
    w.open_file()
    assert w.plist_editors[record.token].editor is editor
    assert not calls and not w.external_edits.records and len(w.plist_editors) == 1
    path = tmp_path / "second.plist"
    path.write_bytes(plistlib.dumps({"other": 123}))
    w.session.import_paths([path], "SystemPreferencesDomain", "")
    w.refresh()
    w.activate(choose(w, "second.plist"))
    qtbot.waitUntil(lambda: w.task is None and len(w.plist_editors) == 2)
    assert w.plist_editors[record.token].editor is editor
    editor.document.root.children[0].value = False
    editor.rebuild()
    assert w.save_action.isEnabled()


def test_plist_no_false_change_alert_after_identical_bytes_metadata_and_rename(
    window, qtbot, tmp_path, monkeypatch
):
    from planbak.app import QMessageBox

    w = window
    rec = open_test_plist(w, qtbot)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a: pytest.fail("false content change notification")
    )
    original = tmp_path / "same.plist"
    w.session.extract_file(rec.token, original)
    w.session.stage_edit(rec.token, original)
    w.check_plist_editors()
    qtbot.waitUntil(lambda: w.task is None)
    assert not rec.editor.document.dirty
    staged = w.session.cached_file(rec.token)
    import os

    stat = staged.stat()
    os.utime(staged, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
    w.check_plist_editors()
    qtbot.waitUntil(lambda: w.task is None)
    w.session.move([rec.token], "SystemPreferencesDomain", "", name="renamed.plist")
    assert not w.check_plist_editors()
    assert rec.editor.name == "renamed.plist"
    w.session.new_directory("SystemPreferencesDomain", "", "unrelated")
    w.refresh()
    qtbot.wait(10)
    assert not w.check_plist_editors()
    w.session.save(tmp_path / "identical-content-backup")
    w.refresh()
    w.check_plist_editors()
    qtbot.waitUntil(lambda: w.task is None)
    assert not w.check_plist_editors()


def test_plist_changed_reload_exact_once_and_own_save_no_alert(
    window, qtbot, tmp_path, monkeypatch
):
    import plistlib

    from planbak.app import QMessageBox

    w = window
    rec = open_test_plist(w, qtbot)
    prompts = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a: prompts.append(a) or QMessageBox.StandardButton.Yes
    )
    changed = tmp_path / "changed.plist"
    changed.write_bytes(plistlib.dumps({"sample": False, "new": "from source"}))
    rec.editor.document.root.children[0].value = False
    rec.editor.rebuild()
    w.session.stage_edit(rec.token, changed)
    assert w.check_plist_editors()
    qtbot.waitUntil(lambda: w.task is None)
    assert len(prompts) == 1
    assert rec.editor.document.root.to_value() == {"sample": False, "new": "from source"}
    assert not rec.editor.document.dirty
    assert not w.check_plist_editors()
    rec.editor.document.root.children[0].value = "updated again"
    rec.editor.rebuild()
    assert rec.editor.save_document()
    qtbot.wait(10)
    assert not w.check_plist_editors()
    assert len(prompts) == 1


def test_plist_save_checks_conflict_before_poll_and_requires_overwrite(
    window, qtbot, tmp_path, monkeypatch
):
    import plistlib

    import planbak.app as module

    w = window
    rec = open_test_plist(w, qtbot)
    rec.editor.document.root.children[0].value = "local"
    rec.editor.rebuild()
    changed = tmp_path / "new-version.plist"
    changed.write_bytes(plistlib.dumps({"sample": "newer source"}))
    w.session.stage_edit(rec.token, changed)
    chosen = ["取消"]
    original_box = module.QMessageBox

    class ConflictBox(original_box):
        def exec(self):
            return 0

        def clickedButton(self):
            return next(b for b in self.buttons() if b.text() == chosen[0])

    monkeypatch.setattr(module, "QMessageBox", ConflictBox)
    assert not rec.editor.save_document()
    assert rec.editor.document.dirty
    target = tmp_path / "current.plist"
    w.session.extract_file(rec.token, target)
    assert plistlib.loads(target.read_bytes()) == {"sample": "newer source"}
    chosen[0] = "覆盖更新版本"
    assert rec.editor.save_document()
    w.session.extract_file(rec.token, target)
    assert plistlib.loads(target.read_bytes()) == {"sample": "local"}
    assert not rec.editor.document.dirty


def test_plist_decline_reload_and_delete_revert(window, qtbot, tmp_path, monkeypatch):
    import plistlib

    from planbak.app import QMessageBox

    w = window
    rec = open_test_plist(w, qtbot)
    prompts = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a: prompts.append(a) or QMessageBox.StandardButton.No
    )
    changed = tmp_path / "new.plist"
    changed.write_bytes(plistlib.dumps({"sample": False}))
    w.session.stage_edit(rec.token, changed)
    w.check_plist_editors()
    qtbot.waitUntil(lambda: w.task is None)
    assert len(prompts) == 1
    assert rec.editor.document.root.to_value() == {"sample": True}
    assert not w.check_plist_editors()
    assert len(prompts) == 1
    w.session.delete([rec.token])
    w.check_plist_editors()
    assert not rec.editor._available and not rec.editor.save_button.isEnabled()
    w.session.revert(w.session.plan_revert(rec.token))
    w.check_plist_editors()
    assert rec.editor._available
    assert len(prompts) == 1


def test_plist_app_close_cancel_retains_editor_and_workspace(window, qtbot, monkeypatch):
    from planbak.plist_editor import FixedMessageBox

    w = window
    rec = open_test_plist(w, qtbot)
    rec.editor.document.root.children[0].value = False
    rec.editor.rebuild()
    monkeypatch.setattr(
        FixedMessageBox, "exec", lambda *a: FixedMessageBox.StandardButton.Cancel
    )
    w.close()
    qtbot.wait(10)
    assert w.isVisible() and rec.editor.isVisible()
    assert rec.directory.exists() and w.session.workspace.exists()


def test_plist_exit_applies_saved_editor_then_saves_backup(window, qtbot, tmp_path, monkeypatch):
    import plistlib

    from planbak.app import QMessageBox

    w = window
    rec = open_test_plist(w, qtbot)
    rec.editor.document.root.children[0].value = False
    rec.editor.rebuild()
    assert rec.editor.save_document()
    output = tmp_path / "exit-plist-backup"

    # Apply the editor’s saved changes to the backup copy.
    class SaveBox(QMessageBox):
        def exec(self):
            return QMessageBox.StandardButton.Save

    monkeypatch.setattr("planbak.app.QMessageBox", SaveBox)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(output), ""))
    workspace = w.session.workspace
    w.close()
    qtbot.waitUntil(lambda: w.task is None, timeout=5000)
    assert workspace.exists() and not w.plist_editors
    assert w.isVisible() and not w.session.dirty and w.session.root == output
    with_session = BackupSession(output)
    try:
        extracted = tmp_path / "restored.plist"
        with_session.extract_file(
            with_session.at("SystemPreferencesDomain", "config.plist").token, extracted
        )
        assert plistlib.loads(extracted.read_bytes()) == {"sample": False}
    finally:
        with_session.close()


def test_plist_main_save_includes_open_editor_buffer_without_false_alert(
    window, qtbot, tmp_path, monkeypatch
):
    import plistlib

    from planbak.app import QMessageBox

    w = window
    rec = open_test_plist(w, qtbot)
    rec.editor.document.root.children[0].value = False
    rec.editor.rebuild()
    assert w.save_action.isEnabled()
    output = tmp_path / "main-saved-plist"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(output), ""))
    monkeypatch.setattr("planbak.app.message", lambda *a, **k: None)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a: pytest.fail("own save is not an external change")
    )
    w.save()
    qtbot.waitUntil(lambda: w.task is None and output.exists(), timeout=5000)
    w.check_plist_editors()
    qtbot.waitUntil(lambda: w.task is None)
    assert rec.editor.isVisible() and not rec.editor.document.dirty and not w.session.dirty
    path = tmp_path / "saved.plist"
    w.session.extract_file(rec.token, path)
    assert plistlib.loads(path.read_bytes()) == {"sample": False}


def test_xml_buffer_source_reload_and_main_save(window, qtbot, tmp_path, monkeypatch):
    import plistlib

    from planbak.app import QMessageBox

    w = window
    rec = open_test_plist(w, qtbot)
    e = rec.editor
    e.switch_mode(1)
    e.xml.code.setPlainText(e.xml.code.toPlainText().replace("<true/>", "<false/>"))
    assert e.dirty and w.save_action.isEnabled()
    changed = tmp_path / "changed.plist"
    changed.write_bytes(plistlib.dumps({"sample": "source update"}))
    prompts = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a: prompts.append(a) or QMessageBox.StandardButton.Yes
    )
    w.session.stage_edit(rec.token, changed)
    w.check_plist_editors()
    qtbot.waitUntil(lambda: w.task is None)
    assert len(prompts) == 1 and "未保存" in prompts[0][2]
    assert e.pages.currentIndex() == 1 and "source update" in e.xml.code.toPlainText()
    assert not e.dirty
    e.xml.code.setPlainText(e.xml.code.toPlainText().replace("source update", "XML saved"))
    output = tmp_path / "xml-saved"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a: (str(output), ""))
    monkeypatch.setattr("planbak.app.message", lambda *a, **k: None)
    w.save()
    qtbot.waitUntil(lambda: w.task is None and output.exists())
    w.check_plist_editors()
    qtbot.waitUntil(lambda: w.task is None)
    assert len(prompts) == 1 and not e.dirty and not w.session.dirty
    file = tmp_path / "saved.plist"
    w.session.extract_file(rec.token, file)
    assert plistlib.loads(file.read_bytes()) == {"sample": "XML saved"}


def test_invalid_xml_cancel_close_retains_text(window, qtbot, monkeypatch):
    from planbak.plist_editor import FixedMessageBox

    w = window
    rec = open_test_plist(w, qtbot)
    e = rec.editor
    e.switch_mode(1)
    e.xml.code.setPlainText("<plist><dict>invalid")
    monkeypatch.setattr(FixedMessageBox, "exec", lambda *a: FixedMessageBox.StandardButton.Cancel)
    w.close()
    qtbot.wait(10)
    assert w.isVisible() and e.isVisible() and rec.directory.exists()
    assert e.xml.code.toPlainText() == "<plist><dict>invalid" and e.dirty
    assert not w.session.dirty
    monkeypatch.setattr(
        FixedMessageBox, "exec", lambda *a: FixedMessageBox.StandardButton.Ok
    )
    e.close()
    assert not w.plist_editors and not rec.directory.exists()


def test_discard_runs_in_worker_and_keeps_event_loop_responsive(window, qtbot, monkeypatch):
    import threading

    from PySide6.QtCore import QTimer

    from planbak.app import QMessageBox
    from planbak.codec import Cancelled

    w = window
    w.session.new_directory("HomeDomain", "", "pending")
    w.refresh()
    original = w.session.revert
    started, release = threading.Event(), threading.Event()
    thread_ids = []

    def blocked(plan, *, progress=None, cancel=None):
        with w.session.lock:
            thread_ids.append(threading.get_ident())
            started.set()
            while not release.wait(0.01):
                if cancel():
                    raise Cancelled("cancelled")
            return original(plan, progress=progress, cancel=cancel)

    monkeypatch.setattr(w.session, "revert", blocked)
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    ticks = []
    try:
        w.discard()
        QTimer.singleShot(0, lambda: ticks.append(True))
        qtbot.waitUntil(lambda: started.is_set() and bool(ticks))
        assert w.task is not None and thread_ids[0] != threading.get_ident()
        assert not w.save_action.isEnabled()
    finally:
        release.set()
    qtbot.waitUntil(lambda: w.task is None)
    assert not w.session.dirty and w.session.at("HomeDomain", "pending") is None


def test_pending_cache_reopen_uses_latest_single_file(window, qtbot, monkeypatch):
    w = window
    rec = open_test_plist(w, qtbot)
    token = rec.token
    rec.editor.document.root.children[0].value = False
    rec.editor.rebuild()
    assert rec.editor.save_document()
    path = w.session.cached_file(token)
    rec.editor.document.root.children[0].value = "second"
    rec.editor.rebuild()
    assert rec.editor.save_document()
    assert w.session.cached_file(token) == path
    assert len(w.session.changes) == 1 and w.changes_model.rowCount() == 1
    assert len(list((w.session.workspace / "pending").iterdir())) == 1
    assert not list(rec.directory.iterdir())
    rec.editor.close()
    assert path.exists() and not w.plist_editors
    monkeypatch.setattr(
        w.session, "_extract_entry", lambda *a, **k: pytest.fail("re-extracted cached plist")
    )
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.plist_editors))
    current = w.plist_editors[token]
    assert current.editor.document.root.to_value() == {"sample": "second"}
    assert w.session.cached_file(token) == path


def test_pending_view_labels_tooltip_context_revert_and_no_history_actions(
    window, qtbot, monkeypatch
):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QAction

    import planbak.app as module

    w = window
    file = w.session.at("HomeDomain", "Documents/a.txt")
    w.session.move([file.token], "HomeDomain", "", name="renamed.txt")
    w.refresh()
    index = w.changes_model.index(0, 0)
    assert "重命名" in index.data()
    tooltip = index.data(Qt.ItemDataRole.ToolTipRole)
    assert "/HomeDomain/Documents/a.txt" in tooltip and "renamed.txt" not in tooltip
    assert not hasattr(w, "undo_action") and not hasattr(w, "redo_action")
    assert not any(action.text() in ("撤销", "重做") for action in w.findChildren(QAction))
    seen = []

    class Menu(module.QMenu):
        def exec(self, *args):
            seen.append([action.text() for action in self.actions()])
            return self.actions()[0]

    monkeypatch.setattr(module, "QMenu", Menu)
    w.pending_context_menu(w.changes_list.visualRect(index).center())
    qtbot.waitUntil(lambda: w.task is None)
    assert seen == [["放弃此更改"]]
    assert w.session.at("HomeDomain", "Documents/a.txt")
    assert not w.session.dirty and w.changes_model.rowCount() == 0


def test_pending_directory_recursive_revert_preserves_other_modifications(window, qtbot, tmp_path):
    w = window
    parent = w.session.new_directory("HomeDomain", "", "added")
    source = tmp_path / "inside.txt"
    source.write_bytes(b"added")
    w.session.import_paths([source], "HomeDomain", "added")
    original = w.session.at("HomeDomain", "Documents/a.txt")
    w.session.stage_bytes(original.token, b"other change")
    w.refresh()
    from planbak.staging import ChangeKind

    assert {ChangeKind.CREATE_FOLDER, ChangeKind.ADD, ChangeKind.MODIFY} == {
        kind for change in w.session.changes for kind in change.kinds
    }
    w.revert_pending(parent.token)
    qtbot.waitUntil(lambda: w.task is None)
    assert w.session.at("HomeDomain", "added") is None
    assert len(w.session.changes) == 1 and w.session.changes[0].token == original.token


def test_pending_revert_dirty_editor_cancel_then_discard(window, qtbot, monkeypatch):
    import planbak.app as module

    w = window
    rec = open_test_plist(w, qtbot)
    rec.editor.document.root.children[0].value = False
    rec.editor.rebuild()
    assert rec.editor.save_document()
    rec.editor.document.root.children[0].value = "unsaved"
    rec.editor.rebuild()
    answer = [module.QMessageBox.StandardButton.Cancel]

    class Box(module.QMessageBox):
        def exec(self):
            return answer[0]

    monkeypatch.setattr(module, "QMessageBox", Box)
    w.revert_pending(rec.token)
    qtbot.waitUntil(lambda: w.task is None)
    assert rec.editor.isVisible() and rec.editor.dirty and w.session.dirty
    answer[0] = module.QMessageBox.StandardButton.Discard
    w.revert_pending(rec.token)
    qtbot.waitUntil(lambda: w.task is None)
    assert not w.plist_editors and not w.session.dirty
    assert not w.session.cache_path(rec.token).exists()


@pytest.mark.parametrize("grid", [False, True])
def test_pane_mouse_focus_routes_delete_and_rename_to_right(window, qtbot, monkeypatch, grid):
    from PySide6.QtCore import Qt

    from planbak.navigation import Location

    w = window
    w.navigate(Location("HomeDomain", "Documents"))
    w.set_view(grid)
    qtbot.wait(10)
    tree, view = w.directory_tree, w.active_view()
    tree.setFocus()
    assert w.sidebar_active()
    # Reproduce a platform policy that selects rows without focusing on click.
    view.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    index = w.model.index(next(i for i, row in enumerate(w.model.rows) if row.name == "a.txt"), 0)
    qtbot.mouseClick(
        view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(index).center()
    )
    assert view.hasFocus() and not tree.hasFocus() and not w.sidebar_active()
    assert view.property("activeBrowserPane") is True
    assert tree.property("activeBrowserPane") is False
    selected = w.session.at("HomeDomain", "Documents/a.txt")
    deleted, renamed = [], []
    monkeypatch.setattr(w, "delete_entries", lambda tokens, *a, **k: deleted.extend(tokens))
    monkeypatch.setattr(w, "rename_entry", lambda entry, **k: renamed.append(entry.token))
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    qtbot.keyClick(view, Qt.Key.Key_F2)
    assert deleted == [selected.token] and renamed == [selected.token]


def test_pane_menu_and_toolbar_keep_last_active_owner(window, qtbot, monkeypatch):
    from planbak.navigation import Location

    w = window
    w.navigate(Location("HomeDomain", "Documents"))
    choose(w, "a.txt")
    w.directory_tree.setFocus()
    assert w.sidebar_active()
    w.location.setFocus()
    assert w.sidebar_active() and not w.open_action.isEnabled() and not w.extract_action.isEnabled()
    assert not w.select_all_action.isEnabled()
    deleted, renamed = [], []
    monkeypatch.setattr(w, "delete_entries", lambda tokens, *a, **k: deleted.extend(tokens))
    monkeypatch.setattr(w, "rename_entry", lambda entry, **k: renamed.append(entry.token))
    directory = w.session.at("HomeDomain", "Documents")
    w.delete_action.trigger()
    w.rename_action.trigger()
    assert deleted == [directory.token] and renamed == [directory.token]


def test_pane_right_context_menu_overrides_previous_tree_owner(window, qtbot, monkeypatch):
    import planbak.app as module
    from planbak.navigation import Location

    w = window
    w.navigate(Location("HomeDomain", "Documents"))
    w.directory_tree.setFocus()
    assert w.sidebar_active()
    calls = []
    monkeypatch.setattr(w, "delete_entries", lambda tokens, *a, **k: calls.extend(tokens))

    class Menu(module.QMenu):
        def exec(self, *args):
            w.location.setFocus()  # Popup focus must not change the action's pane.
            w.delete_action.trigger()

    monkeypatch.setattr(module, "QMenu", Menu)
    view = w.active_view()
    index = w.model.index(next(i for i, row in enumerate(w.model.rows) if row.name == "a.txt"), 0)
    w.context_menu(view, view.visualRect(index).center())
    assert calls == [w.session.at("HomeDomain", "Documents/a.txt").token]
    assert not w.sidebar_active()


@pytest.mark.parametrize("grid", [False, True])
def test_pane_tab_navigation_and_late_restore_do_not_steal_focus(window, qtbot, grid):
    from PySide6.QtCore import Qt

    from planbak.navigation import Location

    w = window
    w.navigate(Location("HomeDomain", "Documents"))
    w.set_view(grid)
    qtbot.wait(10)
    tree, view = w.directory_tree, w.active_view()
    view.setCurrentIndex(choose(w, "a.txt"))
    tree.setFocus()
    assert not w.open_action.isEnabled()
    qtbot.keyClick(tree, Qt.Key.Key_Tab)
    assert view.hasFocus() and not w.sidebar_active()
    assert w.open_action.isEnabled() and w.extract_action.isEnabled()
    qtbot.keyClick(view, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
    assert tree.hasFocus() and w.sidebar_active()
    view.setFocus()
    choose(w, "a.txt")
    w.refresh()  # Queues the old right-pane focus restoration.
    tree.setFocus()
    qtbot.wait(10)
    assert tree.hasFocus() and not view.hasFocus() and w.sidebar_active()
    assert tree.property("activeBrowserPane") is True
    assert view.property("activeBrowserPane") is False


def test_pane_hiding_sidebar_and_empty_tree_selection(window, qtbot, monkeypatch):
    from planbak.navigation import Location

    w = window
    w.navigate(Location("HomeDomain", "Documents"))
    view = w.active_view()
    view.setCurrentIndex(choose(w, "a.txt"))
    w.directory_tree.setFocus()
    w.directory_tree.clearSelection()
    assert not w.delete_action.isEnabled() and not w.rename_action.isEnabled()
    calls = []
    monkeypatch.setattr(w, "delete_entries", lambda tokens, *a, **k: calls.extend(tokens))
    w.delete_selected()
    assert not calls
    w.sidebar_action.setChecked(False)
    assert view.hasFocus() and not w.sidebar_active()
    w.delete_selected()
    assert calls == [w.session.at("HomeDomain", "Documents/a.txt").token]


@pytest.mark.parametrize("grid", [False, True])
@pytest.mark.parametrize("closing", ["button", "close", "escape"])
def test_plist_close_restores_right_pane_after_native_tree_focus(
    window, qtbot, tmp_path, monkeypatch, grid, closing
):
    import plistlib

    from PySide6.QtCore import Qt, QTimer

    from planbak.navigation import Location

    w = window
    source = tmp_path / "focus.plist"
    source.write_bytes(plistlib.dumps({"value": True}))
    w.session.import_paths([source], "HomeDomain", "Documents")
    w.navigate(Location("HomeDomain", "Documents"))
    w.set_view(grid)
    qtbot.wait(10)
    view = w.active_view()
    index = w.model.index(
        next(i for i, row in enumerate(w.model.rows) if row.name == "focus.plist"), 0
    )
    point = view.visualRect(index).center()
    qtbot.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    qtbot.waitUntil(lambda: w.task is None and bool(w.plist_editors))
    record = next(iter(w.plist_editors.values()))
    assert not w.sidebar_active()
    # Model the compositor returning focus to the first parent tab-stop on close.
    QTimer.singleShot(0, lambda: w.directory_tree.setFocus(Qt.FocusReason.ActiveWindowFocusReason))
    if closing == "button":
        from PySide6.QtWidgets import QDialogButtonBox

        buttons = record.editor.findChild(QDialogButtonBox)
        qtbot.mouseClick(
            buttons.button(QDialogButtonBox.StandardButton.Close), Qt.MouseButton.LeftButton
        )
    elif closing == "close":
        record.editor.close()
    else:
        qtbot.keyClick(record.editor, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not w.plist_editors and view.hasFocus() and not w.sidebar_active())
    assert not w.directory_tree.hasFocus()
    assert [row.name for row in w.selected_rows()] == ["focus.plist"]
    deleted = []
    monkeypatch.setattr(w, "delete_entries", lambda tokens, *a, **k: deleted.extend(tokens))
    qtbot.keyClick(view, Qt.Key.Key_Delete)
    assert deleted == [record.token]


@pytest.mark.parametrize("when", ["before", "after"])
def test_plist_close_respects_newer_explicit_pane_choice(window, qtbot, when):
    from PySide6.QtCore import Qt

    w = window
    record = open_test_plist(w, qtbot)
    tree = w.directory_tree
    index = tree.currentIndex()
    if when == "after":
        record.editor.close()
    qtbot.mouseClick(
        tree.viewport(), Qt.MouseButton.LeftButton, pos=tree.visualRect(index).center()
    )
    assert w.sidebar_active()
    if when == "before":
        record.editor.close()
    qtbot.wait(10)
    assert tree.hasFocus() and w.sidebar_active()


def test_plist_closing_inactive_editor_does_not_steal_other_editor_focus(window, qtbot, tmp_path):
    import plistlib

    from PySide6.QtWidgets import QApplication

    w = window
    first = open_test_plist(w, qtbot)
    source = tmp_path / "other.plist"
    source.write_bytes(plistlib.dumps({"other": True}))
    w.session.import_paths([source], "SystemPreferencesDomain", "")
    w.refresh()
    w.activate(choose(w, "other.plist"))
    qtbot.waitUntil(lambda: w.task is None and len(w.plist_editors) == 2)
    second = next(record for record in w.plist_editors.values() if record is not first)
    second.editor.activateWindow()
    second.editor.tree.setFocus()
    qtbot.wait(10)
    first.editor.close()
    qtbot.wait(10)
    assert QApplication.activeWindow() is second.editor
    assert second.editor.tree.hasFocus() and not w.active_view().hasFocus()


@pytest.mark.parametrize(
    "info, expected",
    [
        ({"Device Name": "我的 iPhone", "Display Name": "其他名称"}, "“我的 iPhone”的备份"),
        ({"Device Name": "  iPad  "}, "“iPad”的备份"),
        ({"Display Name": "旧设备"}, "“旧设备”的备份"),
        ({"Device Name": " ", "Display Name": ""}, "备份"),
        ({"Device Name": 123, "Display Name": []}, "备份"),
        ({}, "备份"),
    ],
)
def test_sidebar_backup_device_title(window, info, expected):
    model = window.directory_tree.directory_model
    window.session.info = info
    model.set_session(window.session)
    assert model.index(0, 0).data() == expected
    assert model.root.location.address == "/"
    model.set_session(None)
    assert model.rowCount() == 0


def test_ui_canonical_terms(window):
    w = window
    assert w.save_action.text() == "应用更改…"
    assert w.extract_action.text() == "导出项目…"
    assert w.open_backup_action.text() == "打开备份…"
    assert w.changes_dock.windowTitle() == "更改列表"
    assert w.changes_list.accessibleName() == "更改列表"
    assert w.discard_action.text() == "放弃所有更改"


def test_domain_categories_use_folder_type(window):
    from PySide6.QtCore import Qt

    from planbak.navigation import Location

    w = window
    seed_domains(w.session, ["AppDomain-com.example"])
    w.refresh()
    for name in ("AppDomain", "HomeDomain"):
        index = choose(w, name)
        assert w.model.index(index.row(), 1).data() == "文件夹"
        assert "虚拟" not in index.data(Qt.ItemDataRole.ToolTipRole)
        assert "类别" not in index.data(Qt.ItemDataRole.ToolTipRole)
    w.navigate(Location(group="AppDomain"))
    index = choose(w, "com.example")
    assert w.model.index(index.row(), 1).data() == "文件夹"


def open_test_database(w, qtbot, tmp_path):
    from test_sqlite_viewer import make_database

    from planbak.navigation import Location

    source = tmp_path / "system-data"  # Recognition must not depend on extensions.
    make_database(source)
    w.session.import_paths([source], "HomeDomain", "Documents")
    w.navigate(Location("HomeDomain", "Documents"))
    w.panes.activate(w.active_view(), focus=True)
    choose(w, "system-data")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None, timeout=5000)
    token = w.session.at("HomeDomain", "Documents/system-data").token
    qtbot.waitUntil(lambda: token in w.readonly.database_viewers)
    rec = w.readonly.database_viewers[token]
    qtbot.waitUntil(lambda: rec.viewer.task is None)
    return rec


def test_database_open_single_instance_close_focus_and_cleanup(
    window, qtbot, tmp_path, monkeypatch
):
    from PySide6.QtCore import Qt

    monkeypatch.setattr(
        "planbak.app.QDesktopServices.openUrl",
        lambda *a: pytest.fail("SQLite opened externally"),
    )
    w = window
    rec = open_test_database(w, qtbot, tmp_path)
    snapshot_dir = rec.snapshot.directory
    before = list(w.session.changes)
    w.open_file()
    assert len(w.readonly.database_viewers) == 1 and w.readonly.database_viewers[rec.token] is rec
    rec.viewer.code.setPlainText("SELECT 42")
    rec.viewer.execute_sql()
    qtbot.waitUntil(lambda: rec.viewer.task is None)
    assert rec.viewer.model.result.rows == [(42,)] and list(w.session.changes) == before
    qtbot.keyClick(rec.viewer, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not w.readonly.database_viewers and w.active_view().hasFocus())
    assert not snapshot_dir.exists() and not w.sidebar_active()


def test_database_reloads_only_when_actual_content_changes(window, qtbot, tmp_path, monkeypatch):
    from planbak.app import QMessageBox
    from planbak.sqlite_database import query

    w = window
    rec = open_test_database(w, qtbot, tmp_path)
    prompts = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a: prompts.append(a) or QMessageBox.StandardButton.Yes
    )
    # A revision change without a byte change must not prompt.
    cached = w.session.cache_path(rec.token)
    cached.touch()
    assert w.readonly.check_database_viewers()
    qtbot.waitUntil(lambda: w.task is None)
    assert not prompts
    source = tmp_path / "replacement.db"
    source.write_bytes(cached.read_bytes())
    import sqlite3

    c = sqlite3.connect(source)
    c.execute('INSERT INTO "items weird" VALUES (999, "new", NULL)')
    c.commit()
    c.close()
    w.session.stage_bytes(rec.token, source.read_bytes())
    old = rec.snapshot.directory
    assert w.readonly.check_database_viewers()
    qtbot.waitUntil(lambda: w.task is None)
    qtbot.waitUntil(lambda: rec.viewer.task is None)
    assert len(prompts) == 1 and not old.exists()
    assert query(rec.snapshot.path, 'SELECT max(id) FROM "items weird"').rows == [(999,)]
    w.session.delete([rec.token])
    w.readonly.check_database_viewers()
    assert not rec.viewer.available
    rec.viewer.close()


def test_database_declined_reload_keeps_snapshot_and_does_not_repeat(
    window, qtbot, tmp_path, monkeypatch
):
    from planbak.app import QMessageBox

    w = window
    rec = open_test_database(w, qtbot, tmp_path)
    prompts = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a: prompts.append(a) or QMessageBox.StandardButton.No
    )
    # Alter SQLite user_version: valid file change with no table mutation.
    data = bytearray(w.session.cached_file(rec.token).read_bytes())
    data[60:64] = (123).to_bytes(4, "big")
    w.session.stage_bytes(rec.token, bytes(data))
    old = rec.snapshot
    assert w.readonly.check_database_viewers()
    qtbot.waitUntil(lambda: w.task is None)
    assert rec.snapshot is old and rec.viewer.stale and len(prompts) == 1
    assert not w.readonly.check_database_viewers() and len(prompts) == 1
    rec.viewer.close()


def test_database_exit_cancels_active_query_before_session_cleanup(
    window, qtbot, tmp_path, monkeypatch
):
    from planbak.app import QMessageBox

    w = window
    rec = open_test_database(w, qtbot, tmp_path)
    workspace = w.session.workspace
    rec.viewer.code.setPlainText(
        "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT SUM(x) FROM n"
    )
    rec.viewer.execute_sql()
    monkeypatch.setattr(QMessageBox, "exec", lambda *a: QMessageBox.StandardButton.Discard)
    w.close()
    qtbot.waitUntil(lambda: not w.isVisible(), timeout=5000)
    assert not workspace.exists() and not w.readonly.database_viewers and w.session is None


def test_database_viewer_closed_before_switching_backup(window, qtbot, tmp_path, monkeypatch):
    from planbak.app import QMessageBox

    w = window
    rec = open_test_database(w, qtbot, tmp_path)
    snapshot = rec.snapshot.directory
    next_backup = make_backup(tmp_path / "other-backup")
    monkeypatch.setattr(QMessageBox, "exec", lambda *a: QMessageBox.StandardButton.Discard)
    w.confirm_leave(lambda: w.open_path(next_backup))
    qtbot.waitUntil(
        lambda: not w.readonly.database_viewers and w.task is None and w.session.root == next_backup,
        timeout=5000,
    )
    assert not snapshot.exists()


def test_database_unknown_format_and_sidecars_report_clear_error(
    window, qtbot, tmp_path, monkeypatch
):
    from planbak.navigation import Location

    errors = []
    monkeypatch.setattr("planbak.app.message", lambda *a, **k: errors.append(a[2]))
    monkeypatch.setattr(
        "planbak.app.QDesktopServices.openUrl",
        lambda *a: pytest.fail("unexpected external open"),
    )
    w = window
    for name, expected in [("private.db", "只支持标准 SQLite"), ("data-wal", "配套文件")]:
        path = tmp_path / name
        path.write_bytes(b"not a sqlite database")
        w.session.import_paths([path], "HomeDomain", "Documents")
        w.navigate(Location("HomeDomain", "Documents"))
        w.refresh()
        w.panes.activate(w.active_view(), focus=True)
        choose(w, name)
        w.open_file()
        qtbot.waitUntil(lambda: w.task is None)
        assert expected in w.language_context.error(errors[-1])
    assert not w.readonly.database_viewers and not w.external_edits.records


def open_test_hex(w, qtbot):
    from planbak.navigation import Location

    w.navigate(Location("HomeDomain", "Documents"))
    w.panes.activate(w.active_view(), focus=True)
    choose(w, "a.txt")
    w.readonly.open_hex()
    qtbot.waitUntil(lambda: w.task is None)
    token = w.session.at("HomeDomain", "Documents/a.txt").token
    return w.readonly.hex_viewers[token]


def test_hex_menu_single_instance_and_close_preserves_focus(window, qtbot, monkeypatch):
    from PySide6.QtCore import Qt

    from planbak.app import QMenu
    from planbak.navigation import Location

    w = window
    w.navigate(Location("HomeDomain", "Documents"))

    class HexMenu(QMenu):
        def exec(self, *args):
            next(a for a in self.actions() if a.text() == "以十六进制查看").trigger()

    monkeypatch.setattr("planbak.app.QMenu", HexMenu)
    index = choose(w, "a.txt")
    w.directory_tree.setFocus()
    w.context_menu(w.active_view(), w.active_view().visualRect(index).center())
    qtbot.waitUntil(lambda: w.task is None)
    token = w.session.at("HomeDomain", "Documents/a.txt").token
    record = w.readonly.hex_viewers[token]
    assert record.path.read_bytes() == b"hello world" and not w.session.dirty
    w.readonly.open_hex()
    assert len(w.readonly.hex_viewers) == 1 and w.readonly.hex_viewers[token] is record
    directory = record.path.parent
    qtbot.keyClick(record.viewer, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not w.readonly.hex_viewers and w.active_view().hasFocus())
    assert not directory.exists() and not w.sidebar_active()


def test_hex_can_coexist_with_plist_and_does_not_redirect_normal_open(window, qtbot):
    w = window
    plist = open_test_plist(w, qtbot)
    w.readonly.open_hex()
    qtbot.waitUntil(lambda: w.task is None)
    assert plist.token in w.readonly.hex_viewers and plist.token in w.plist_editors
    assert w.readonly.hex_viewers[plist.token].path.read_bytes().startswith(b"<?xml")
    w.open_file()
    qtbot.waitUntil(lambda: plist.editor.isActiveWindow())
    assert len(w.readonly.hex_viewers) == 1
    w.readonly.hex_viewers[plist.token].viewer.close()
    plist.editor.close()


def test_hex_source_revision_checks_exact_bytes_and_manual_reload(window, qtbot, monkeypatch):
    from planbak.app import QMessageBox

    w = window
    record = open_test_hex(w, qtbot)
    calls = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a: calls.append(a) or QMessageBox.StandardButton.No
    )
    # Touching original bytes changes the observed revision but not the contents.
    source = w.session.payload_path(w.session.get(record.token).original_id)
    source.touch()
    assert w.readonly.check_hex_viewers()
    qtbot.waitUntil(lambda: w.task is None)
    assert not calls
    old_path = record.path
    w.session.stage_bytes(record.token, b"replacement")
    assert w.readonly.check_hex_viewers()
    qtbot.waitUntil(lambda: w.task is None)
    assert len(calls) == 1 and record.viewer.stale
    assert record.path == old_path and old_path.read_bytes() == b"hello world"
    # Same bytes with another stat change must not repeat a declined prompt.
    w.session.cache_path(record.token).touch()
    assert w.readonly.check_hex_viewers()
    qtbot.waitUntil(lambda: w.task is None)
    assert len(calls) == 1
    w.readonly.reload_hex(record)
    qtbot.waitUntil(lambda: w.task is None)
    assert record.path.read_bytes() == b"replacement" and not old_path.exists()
    assert not record.viewer.stale
    w.session.delete([record.token])
    w.readonly.check_hex_viewers()
    assert not record.viewer.available
    record.viewer.close()


def test_hex_and_sqlite_exit_cancel_both_workers_before_cleanup(
    window, qtbot, tmp_path, monkeypatch
):
    import time

    from planbak.app import QMessageBox
    from planbak.codec import Cancelled

    w = window
    database = open_test_database(w, qtbot, tmp_path)
    hexdump = open_test_hex(w, qtbot)
    workspace = w.session.workspace

    def search(*a, cancel, **k):
        while not cancel():
            time.sleep(0.001)
        raise Cancelled()

    monkeypatch.setattr("planbak.hex_viewer.search_bytes", search)
    hexdump.viewer.search_input.setText("FF")
    hexdump.viewer.find_next()
    database.viewer.code.setPlainText(
        "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT SUM(x) FROM n"
    )
    database.viewer.execute_sql()
    monkeypatch.setattr(QMessageBox, "exec", lambda *a: QMessageBox.StandardButton.Discard)
    w.close()
    qtbot.waitUntil(lambda: not w.isVisible(), timeout=5000)
    assert not workspace.exists() and not w.readonly.hex_viewers and not w.readonly.database_viewers


@pytest.mark.parametrize("data", [b"\x00\xffraw-data", b""])
def test_plist_data_hex_uses_unsaved_bytes_and_reuses_snapshot(window, qtbot, data):
    from planbak.plist_editor import PlistNode

    w = window
    plist = open_test_plist(w, qtbot)
    editor = plist.editor
    node = PlistNode.from_value(data, "blob")
    editor.document.root.children[0] = node
    editor.rebuild(node)
    before = list(w.session.changes)
    editor.view_data_hex()
    qtbot.waitUntil(lambda: w.task is None)
    record = next(iter(w.readonly.hex_viewers.values()))
    assert record.detached and record.path.read_bytes() == data
    assert record.viewer.model.data_bytes == data
    assert not record.viewer.reload_button.isVisible()
    assert list(w.session.changes) == before and editor.dirty
    assert not w.readonly.check_hex_viewers()
    editor.view_data_hex()
    qtbot.waitUntil(lambda: w.task is None)
    assert len(w.readonly.hex_viewers) == 1 and next(iter(w.readonly.hex_viewers.values())) is record
    assert len(list((w.session.workspace / "hex-viewer").iterdir())) == 1
    path = record.path
    record.viewer.close()
    qtbot.waitUntil(editor.tree.hasFocus)
    assert not path.exists() and not w.readonly.hex_viewers


def test_plist_data_hex_changed_values_create_independent_snapshots(window, qtbot):
    from planbak.plist_editor import PlistNode

    w = window
    plist = open_test_plist(w, qtbot)
    node = PlistNode.from_value(b"first", "blob")
    plist.editor.document.root.children[0] = node
    plist.editor.rebuild(node)
    plist.editor.view_data_hex()
    qtbot.waitUntil(lambda: w.task is None)
    first = next(iter(w.readonly.hex_viewers.values()))
    node.value = b"second"
    plist.editor.rebuild(node)
    plist.editor.view_data_hex()
    qtbot.waitUntil(lambda: w.task is None)
    assert len(w.readonly.hex_viewers) == 2
    assert {rec.path.read_bytes() for rec in w.readonly.hex_viewers.values()} == {b"first", b"second"}
    assert first.path.read_bytes() == b"first" and not w.session.dirty


def test_plist_data_hex_survives_source_close_and_exit_cancels_search(window, qtbot, monkeypatch):
    import time

    from planbak.codec import Cancelled
    from planbak.plist_editor import FixedMessageBox, PlistNode

    w = window
    plist = open_test_plist(w, qtbot)
    node = PlistNode.from_value(b"abc", "blob")
    plist.editor.document.root.children[0] = node
    plist.editor.rebuild(node)
    plist.editor.view_data_hex()
    qtbot.waitUntil(lambda: w.task is None)
    record = next(iter(w.readonly.hex_viewers.values()))
    monkeypatch.setattr(
        FixedMessageBox, "exec", lambda *a: FixedMessageBox.StandardButton.Ok
    )
    plist.editor.close()
    qtbot.waitUntil(lambda: not w.plist_editors)
    assert record.viewer.isVisible() and record.path.exists()
    assert not w.readonly.check_hex_viewers()

    def search(*a, cancel, **k):
        while not cancel():
            time.sleep(0.001)
        raise Cancelled()

    monkeypatch.setattr("planbak.hex_viewer.search_bytes", search)
    record.viewer.search_input.setText("FF")
    record.viewer.find_next()
    workspace = w.session.workspace
    w.close()
    qtbot.waitUntil(lambda: not w.isVisible(), timeout=5000)
    assert not workspace.exists() and not w.readonly.hex_viewers


@pytest.mark.parametrize("grid", [False, True])
def test_browser_unselected_current_item_has_no_residual_focus_rectangle(window, qtbot, grid):
    from PySide6.QtCore import Qt

    w = window
    w.set_view(grid)
    view = w.active_view()
    w.activateWindow()
    qtbot.waitUntil(w.isActiveWindow)
    w.panes.activate(view, focus=True)
    index = choose(w, "HomeDomain")
    view.setCurrentIndex(index)
    selected = view.viewport().grab().toImage()
    view.clearSelection()
    assert view.currentIndex().isValid() and not view.selectedIndexes()
    unselected_current = view.viewport().grab().toImage()
    assert selected != unselected_current
    view.selectionModel().clearCurrentIndex()
    assert unselected_current == view.viewport().grab().toImage()
    # Removing the paint state must not remove logical keyboard navigation.
    view.setCurrentIndex(index)
    qtbot.keyClick(view, Qt.Key.Key_Right if grid else Qt.Key.Key_Down)
    assert view.currentIndex().row() == index.row() + 1


@pytest.mark.parametrize("grid", [False, True])
@pytest.mark.parametrize("target", ["a.txt", "sub", None])
def test_browser_context_menu_is_specific_to_target(window, qtbot, monkeypatch, grid, target):
    from PySide6.QtCore import QPoint

    from planbak.app import QMenu
    from planbak.navigation import Location

    w = window
    w.navigate(Location("HomeDomain", "Documents"))
    w.set_view(grid)
    qtbot.wait(10)
    seen = []

    class Menu(QMenu):
        def exec(self, *args):
            seen.extend(a.text() for a in self.actions() if not a.isSeparator())

    monkeypatch.setattr("planbak.app.QMenu", Menu)
    view = w.active_view()
    choose(w, "a.txt")
    if target:
        row = next(i for i, item in enumerate(w.model.rows) if item.name == target)
        point = view.visualRect(w.model.index(row, 0)).center()
    else:
        point = QPoint(view.viewport().width() - 5, view.viewport().height() - 5)
        assert not view.indexAt(point).isValid()
    w.context_menu(view, point)
    additions = ["添加文件…", "添加文件夹…", "新建文件夹…"]
    if target is None:
        assert seen == additions
        assert not w.selected_tokens()
    else:
        assert not set(additions).intersection(seen)
        if target == "sub":
            assert seen == ["打开文件夹", "导出项目…", "重命名…", "删除"]
        else:
            assert seen == [
                "打开",
                "导出项目…",
                "以十六进制查看",
                "使用系统应用打开",
                "重命名…",
                "删除",
            ]


@pytest.mark.parametrize("column", range(5))
def test_browser_column_header_sorts_and_toggles_direction(window, qtbot, tmp_path, column):
    from PySide6.QtCore import QPoint, Qt

    from planbak.navigation import Location

    w = window
    file = tmp_path / "new.bin"
    file.write_bytes(b"x")
    w.session.import_paths([file], "HomeDomain", "Documents")
    w.navigate(Location("HomeDomain", "Documents"))
    w.directories_first.setChecked(False)
    header = w.table.horizontalHeader()
    # Start on another key so the first click must choose ascending order.
    w.sort_combo.setCurrentIndex((column + 1) % 5)
    w.descending.setChecked(True)
    point = QPoint(
        header.sectionViewportPosition(column) + header.sectionSize(column) // 2,
        header.height() // 2,
    )

    def values():
        rows = [r for r in w.model.rows if r.kind == "file"]
        if column == 0:
            return [r.name.casefold() for r in rows]
        if column == 1:
            return [Path(r.name).suffix for r in rows]
        if column == 2:
            return [r.entry.size for r in rows]
        if column == 3:
            return [r.entry.mtime for r in rows]
        return [
            bool(w.model.data(w.model.index(i, 4)))
            for i, r in enumerate(w.model.rows)
            if r.kind == "file"
        ]

    selected = choose(w, "a.txt")
    w.table.setCurrentIndex(selected)
    qtbot.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=point)
    qtbot.wait(10)
    assert w.sort_combo.currentIndex() == column and not w.descending.isChecked()
    assert header.sortIndicatorSection() == column
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    assert values() == sorted(values())
    assert w.model.rows[0].name == ".."
    assert [r.name for r in w.selected_rows()] == ["a.txt"]
    qtbot.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=point)
    qtbot.wait(10)
    assert w.descending.isChecked() and header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
    assert values() == sorted(values(), reverse=True)
    assert w.model.rows[0].name == ".."
    assert [r.name for r in w.selected_rows()] == ["a.txt"]
    w.set_view(True)
    assert values() == sorted(values(), reverse=True)
    w.sort_combo.setCurrentIndex((column + 1) % 5)
    assert header.sortIndicatorSection() == (column + 1) % 5
    w.descending.setChecked(False)
    assert header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder


def test_browser_type_sort_uses_visible_folder_type_for_virtual_nodes(window):
    from planbak.navigation import Location

    w = window
    seed_domains(w.session, ["AppDomain-com.example", "ZZDomain", "AADomain"])
    w.navigate(Location())
    w.sort_by_column(1)
    names = [r.name for r in w.model.rows]
    assert names == sorted(names, key=str.casefold)
    assert all(w.model.index(i, 1).data() == "文件夹" for i in range(len(names)))


def test_browser_type_sort_uses_stable_categories():
    from planbak.app import BrowserModel, BrowserRow

    def row(name, kind):
        return BrowserRow(name, "HomeDomain", name, kind)

    items = [row("folder", "directory"), row("plain", "file"), row("b.txt", "file"),
             row("a.PNG", "file"), row("link", "special"), row("domain", "domain")]
    assert [item.name for item in sorted(items, key=BrowserModel.type_sort_key)] == [
        "a.PNG", "b.txt", "plain", "folder", "domain", "link"
    ]


def test_independent_document_windows_close_with_session_and_allow_multiple_files(
    window, qtbot, tmp_path, monkeypatch
):
    from planbak.app import QMessageBox

    w = window
    plist = open_test_plist(w, qtbot)
    w.activateWindow()
    qtbot.waitUntil(w.isActiveWindow)
    database = open_test_database(w, qtbot, tmp_path)
    w.activateWindow()
    qtbot.waitUntil(w.isActiveWindow)
    hexdump = open_test_hex(w, qtbot)
    for document in (plist.editor, database.viewer, hexdump.viewer):
        assert document.isVisible() and document.windowHandle().transientParent() is None
    assert w.plist_editors and w.readonly.database_viewers and w.readonly.hex_viewers
    workspace = w.session.workspace
    monkeypatch.setattr(QMessageBox, "exec", lambda *a: QMessageBox.StandardButton.Discard)
    w.close()
    qtbot.waitUntil(lambda: not w.isVisible(), timeout=5000)
    assert not w.plist_editors and not w.readonly.database_viewers and not w.readonly.hex_viewers
    assert not workspace.exists()


def test_exit_cleans_session_off_gui_thread(window, qtbot, monkeypatch):
    import threading
    import time

    w = window
    session = w.session
    gui_thread = threading.get_ident()
    threads = []
    close = session.close

    def slow_close():
        threads.append(threading.get_ident())
        time.sleep(0.1)
        close()

    monkeypatch.setattr(session, "close", slow_close)
    w.force_close = True
    w.close()
    assert w.isVisible() and w.task is not None
    qtbot.waitUntil(lambda: not w.isVisible())
    assert threads and threads[0] != gui_thread


def test_drag_export_returns_when_task_cannot_start(window, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(window, "run_task", lambda *args, **kwargs: None)
        assert window.drag_export(["unused"]) == []


def test_external_check_error_is_nonmodal_and_delayed(window, qtbot, monkeypatch):
    import time

    w = window
    w.domain, w.path = "HomeDomain", "Documents"
    w.refresh()
    choose(w, "a.txt")
    monkeypatch.setattr("planbak.app.QDesktopServices.openUrl", lambda url: True)
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None)
    record = next(iter(w.external_edits.records.values()))
    record.path.write_bytes(b"changed")
    monkeypatch.setattr("planbak.external_edits.digest", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("locked")))
    w.poll_open_files()
    w.poll_open_files()
    qtbot.waitUntil(lambda: w.task is None)
    assert record.unreviewed and record.later_until > time.monotonic()
    assert "locked" in w.statusBar().currentMessage()
    w.poll_open_files()
    assert w.task is None


def test_failed_drag_export_removes_partial_files(window, monkeypatch):
    w = window
    def fail(_tokens, target, **_kwargs):
        target.mkdir(parents=True)
        (target / "partial").write_bytes(b"data")
        raise OSError("failed")
    monkeypatch.setattr(w.session, "export", fail)
    monkeypatch.setattr("planbak.app.message", lambda *_args, **_kwargs: None)
    assert w.drag_export(["unused"]) == []
    assert not list((w.session.workspace / "exports").iterdir())
