import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import make_backup
from PySide6.QtCore import QMimeData, QModelIndex, QSettings, Qt

import planbak.app as module
from planbak.app import INTERNAL_MIME, MainWindow, QMessageBox
from planbak.backup import BackupSession
from planbak.backup_windows import BackupWindows
from planbak.codec import BackupError, Cancelled
from planbak.navigation import Location
from planbak.staging import ChangeKind


def test_parent_choice_and_background_copy_keep_container_layout(
    factory, tmp_path, qtbot, monkeypatch
):
    import plistlib
    from datetime import datetime

    parent = tmp_path / "backups"
    parent.mkdir()
    first = make_backup(parent / "device-1")
    second = make_backup(parent / "device-2")
    invalid = parent / "not-a-backup"
    invalid.mkdir()
    (invalid / "Info.plist").write_bytes((first / "Info.plist").read_bytes())
    (invalid / "Manifest.plist").write_bytes((first / "Manifest.plist").read_bytes())
    for root, name in ((first, "First"), (second, "Second")):
        info = plistlib.loads((root / "Info.plist").read_bytes())
        info["Device Name"] = name
        info["Last Backup Date"] = datetime(2025, 1, 2, 3, 4, 5)
        (root / "Info.plist").write_bytes(plistlib.dumps(info))

    def choose(dialog):
        assert dialog.table.rowCount() == 2
        assert [dialog.table.item(1, col).text() for col in range(3)] == [
            "Second",
            "2025-01-02 03:04:05",
            "device-2",
        ]
        dialog.table.setCurrentCell(1, 0)
        return module.QDialog.DialogCode.Accepted

    monkeypatch.setattr(module.BackupChoiceDialog, "exec", choose)
    w = factory()
    w.open_path(parent)
    ready(w, qtbot)
    assert w.session.root == second and w._backup_container == parent
    target = tmp_path / "backups-copy"

    def destination(*args):
        assert args[2] == str(target)
        return str(target), ""

    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", destination)
    w.create_backup_copy()
    qtbot.waitUntil(lambda: w.copy_task is None and w.task is None and not w._copy_pending)
    assert w.session.root == target / second.name
    assert w._backup_container == target and list(target.iterdir()) == [target / second.name]
    direct = factory(second)
    assert direct._backup_container is None
    direct.open_path(parent)
    assert direct._backup_container == parent


def test_parent_open_apply_creates_container_copy(factory, tmp_path, qtbot, monkeypatch):
    parent = tmp_path / "backups"
    parent.mkdir()
    source = make_backup(parent / "device-1")
    w = factory(parent)
    assert w._backup_container == parent
    token = w.session.at("HomeDomain", "Documents/a.txt").token
    w.session.stage_bytes(token, b"edited")
    target = tmp_path / "backups-copy"
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *args: (str(target), ""))
    w.save(quiet=True)
    qtbot.waitUntil(lambda: w.task is None)
    assert w.session.root == target / source.name and w._backup_container == target
    assert not w.session.dirty and (w.session.root / ".planbak.json").is_file()


def test_parent_open_in_new_window_preserves_container_context(factory, tmp_path, qtbot):
    parent = tmp_path / "backups"
    parent.mkdir()
    source = make_backup(parent / "device-1")
    original = factory()
    original.open_path(parent, new_window=True)
    other = next(window for window in factory.manager.windows if window is not original)
    ready(other, qtbot)
    assert other.session.root == source and other._backup_container == parent


@pytest.fixture
def factory(qtbot, tmp_path, monkeypatch):
    manager = BackupWindows(tmp_path / "locks")
    monkeypatch.setattr(
        module,
        "QSettings",
        lambda *a: QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat),
    )
    errors = []
    monkeypatch.setattr(module, "message", lambda *a, **kw: errors.append(a))

    def create(root=None, *, track=True):
        w = MainWindow(manager, language="zh_CN")
        w.poll_timer.stop()
        w.show()
        if root:
            w.open_path(root)
            qtbot.waitUntil(lambda: w.task is None)
            assert w.session is not None, errors
        return w

    create.manager = manager
    create.errors = errors
    yield create
    from shiboken6 import isValid

    for w in list(manager.windows):
        if w.task:
            w.task.cancelled.set()
            qtbot.waitUntil(lambda: w.task is None)
        if w.copy_task:
            w.copy_task.cancelled.set()
            qtbot.waitUntil(lambda: w.copy_task is None)
        w.force_close = True
        w.close()
        qtbot.waitUntil(lambda: not isValid(w) or not w.isVisible())
    assert not manager.claims


def ready(w, qtbot):
    qtbot.waitUntil(lambda: w.task is None and not w._transitioning)


def drag(source, path="Documents/a.txt"):
    mime = QMimeData()
    token = source.session.at("HomeDomain", path).token
    mime.setData(
        INTERNAL_MIME,
        json.dumps({"session": source.session.session_id, "tokens": [token]}).encode(),
    )
    return mime


def test_file_menu_and_close_reset_release_everything(factory, tmp_path, qtbot):
    w = factory()
    assert w.open_backup_action.text() == "打开备份…"
    assert not w.open_new_window_action.isVisible()
    assert not w.close_backup_action.isEnabled()
    w.open_path(make_backup(tmp_path / "backup"))
    ready(w, qtbot)
    assert w.open_new_window_action.isVisible()
    actions = w.menuBar().actions()[0].menu().actions()
    close = actions.index(w.close_backup_action)
    assert actions[close - 1].isSeparator() and actions[close + 1].text() == "退出"
    root, workspace = w.session.root, w.session.workspace
    w.navigate(Location("HomeDomain", "Documents"))
    w.close_backup()
    ready(w, qtbot)
    assert w.isVisible() and w.session is None and not workspace.exists()
    assert not w.model.rows and w.changes_model.rowCount() == 0
    assert w.directory_tree.directory_model.root is None
    assert not w._view_states and not w.external_edits.records and w.images.cache is None
    assert not w.external_edits.watcher.files() and not w.external_edits.watcher.directories()
    assert w.windowTitle() == "PlanBak" and not w.changes_dock.isVisible()
    assert w.location.text() == "/" and w.work_label.text() == "就绪"
    assert factory.manager.owner_for(root) is None
    w.open_path(root)
    ready(w, qtbot)
    assert w.session and w.close_backup_action.isEnabled()


@pytest.mark.parametrize(
    "answer",
    [
        QMessageBox.StandardButton.Cancel,
        QMessageBox.StandardButton.Discard,
        QMessageBox.StandardButton.Save,
    ],
)
def test_dirty_close_apply_discard_cancel(factory, tmp_path, qtbot, monkeypatch, answer):
    w = factory(make_backup(tmp_path / "backup"))
    root, workspace = w.session.root, w.session.workspace
    w.session.new_directory("HomeDomain", "", "pending")
    target = tmp_path / "applied"
    monkeypatch.setattr(QMessageBox, "exec", lambda self: answer)
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *a: (str(target), ""))
    w.close_backup()
    ready(w, qtbot)
    if answer == QMessageBox.StandardButton.Cancel:
        assert w.session and w.session.dirty and workspace.exists()
        assert factory.manager.owner_for(root) is w
    else:
        assert w.session is None and not workspace.exists() and w.isVisible()
        if answer == QMessageBox.StandardButton.Save:
            read = BackupSession(target)
            try:
                assert read.at("HomeDomain", "pending")
            finally:
                read.close()
        else:
            assert not target.exists()


def test_switch_cleans_before_password_cancel_and_leaves_empty(
    factory, tmp_path, qtbot, monkeypatch
):
    w = factory(make_backup(tmp_path / "old"))
    old, workspace = w.session.root, w.session.workspace
    encrypted = make_backup(tmp_path / "encrypted", True)

    def password(dialog):
        assert w.session is None and not workspace.exists()
        assert factory.manager.owner_for(old) is None
        return module.QDialog.DialogCode.Rejected

    monkeypatch.setattr(module.PasswordDialog, "exec", password)
    w.open_path(encrypted)
    ready(w, qtbot)
    assert w.session is None
    assert factory.manager.owner_for(encrypted) is None


def test_failed_new_backup_load_does_not_restore_old_session(factory, tmp_path, qtbot):
    w = factory(make_backup(tmp_path / "old"))
    workspace = w.session.workspace
    invalid = make_backup(tmp_path / "invalid")
    (invalid / "Manifest.db").write_bytes(b"not sqlite")
    w.open_path(invalid)
    ready(w, qtbot)
    assert w.session is None and not workspace.exists()
    assert factory.errors and factory.manager.owner_for(invalid) is None


def test_switch_cancel_and_save_destination_cancel_preserve_old(
    factory, tmp_path, qtbot, monkeypatch
):
    old = make_backup(tmp_path / "old")
    other = make_backup(tmp_path / "other")
    w = factory(old)
    w.session.new_directory("HomeDomain", "", "pending")
    workspace = w.session.workspace
    for answer in (QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Save):
        monkeypatch.setattr(QMessageBox, "exec", lambda self: answer)
        monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *a: ("", ""))
        w.open_path(other)
        ready(w, qtbot)
        assert w.session.root == old and w.session.dirty and workspace.exists()
        assert factory.manager.owner_for(other) is None


def test_duplicate_alias_blocked_but_copy_and_new_window_independent(factory, tmp_path, qtbot):
    original = make_backup(tmp_path / "original")
    first = factory(original)
    alias = tmp_path / "alias"
    alias.symlink_to(original, target_is_directory=True)
    blank = factory()
    blank.open_path(alias)
    assert blank.session is None and first.session.root == original
    assert factory.errors
    copied = first.session.create_copy(tmp_path / "copy")
    first.open_path(copied.path, new_window=True)
    second = next(w for w in factory.manager.windows if w not in (first, blank))
    ready(second, qtbot)
    assert first.session.root == original and second.session.root == copied.path
    assert first.session.workspace != second.session.workspace
    first.session.new_directory("HomeDomain", "", "first-only")
    assert second.session.at("HomeDomain", "first-only") is None
    first.force_close = True
    first.close()
    assert second.isVisible() and second.session.workspace.exists()


def test_new_window_does_not_prompt_for_current_unsaved_changes(
    factory, tmp_path, qtbot, monkeypatch
):
    first = factory(make_backup(tmp_path / "first"))
    first.session.new_directory("HomeDomain", "", "unsaved")
    monkeypatch.setattr(
        QMessageBox, "exec", lambda self: pytest.fail("new window must not prompt for old edits")
    )
    first.open_path(make_backup(tmp_path / "second"), new_window=True)
    second = next(w for w in factory.manager.windows if w is not first)
    ready(second, qtbot)
    assert first.session.dirty and not second.session.dirty


@pytest.mark.parametrize("policy", ["replace", "keep_both", "skip", None])
def test_cross_window_copy_uses_current_bytes_and_source_changes_unchanged(
    factory, tmp_path, qtbot, monkeypatch, policy
):
    source = factory(make_backup(tmp_path / "source"))
    target = factory(make_backup(tmp_path / "target"))
    original = source.session.at("HomeDomain", "Documents/a.txt")
    source.session.stage_bytes(original.token, b"current staged source")
    before = source.session.changes
    target.navigate(Location("HomeDomain", "Documents"))
    monkeypatch.setattr(target, "collision_policy", lambda: policy)
    assert target.queue_drop(drag(source), QModelIndex()) == Qt.DropAction.CopyAction
    qtbot.waitUntil(lambda: not target._drop_queued and target.task is None)
    assert source.session.changes == before and not source.transfers.active
    if policy == "replace":
        assert (
            target.session.cached_file(
                target.session.at("HomeDomain", "Documents/a.txt").token
            ).read_bytes()
            == b"current staged source"
        )
        assert len(target.session.changes) == 1
    elif policy == "keep_both":
        entry = target.session.at("HomeDomain", "Documents/a (2).txt")
        assert entry and Path(entry.staged).read_bytes() == b"current staged source"
    else:
        assert not target.session.dirty
    assert not list(target.session.workspace.glob(".transfer-*"))


def test_cross_window_copy_directory_is_addition_and_preserves_empty_children(
    factory, tmp_path, qtbot
):
    source = factory(make_backup(tmp_path / "source"))
    target = factory(make_backup(tmp_path / "target"))
    source.session.new_directory("HomeDomain", "Documents/sub", "empty-child")
    before = source.session.changes
    target.navigate(Location("HomeDomain"))
    assert target.handle_drop(drag(source, "Documents/sub"), QModelIndex())
    qtbot.waitUntil(lambda: target.task is None)
    assert target.session.at("HomeDomain", "sub/b.txt")
    assert target.session.at("HomeDomain", "sub/empty-child")
    assert source.session.changes == before
    assert any(c.kinds == (ChangeKind.ADD,) for c in target.session.changes)


def test_cancel_transfer_releases_source_and_rolls_back_target(
    factory, tmp_path, qtbot, monkeypatch
):
    source = factory(make_backup(tmp_path / "source"))
    target = factory(make_backup(tmp_path / "target"))
    target.navigate(Location("HomeDomain"))
    started = threading.Event()

    def slow(*a, cancel, **kw):
        started.set()
        while not cancel():
            time.sleep(0.002)
        raise Cancelled()

    monkeypatch.setattr(source.session, "export", slow)
    assert target.handle_drop(drag(source), QModelIndex())
    qtbot.waitUntil(started.is_set)
    assert "复制" in source.work_label.text()
    assert not source.open_backup_action.isEnabled()
    with pytest.raises(BackupError):
        source.session.stage_bytes(
            source.session.at("HomeDomain", "Documents/a.txt").token, b"blocked"
        )
    with pytest.raises(BackupError):
        source.session.close()
    target.task.cancelled.set()
    qtbot.waitUntil(lambda: target.task is None)
    assert not target.session.dirty and not source.session.dirty
    assert not source.transfers.active and not list(target.session.workspace.glob(".transfer-*"))
    assert source.work_label.text() == "就绪" and source.open_backup_action.isEnabled()


def test_copy_before_queued_event_owns_source_until_completion(factory, tmp_path, qtbot):
    source = factory(make_backup(tmp_path / "source"))
    target = factory(make_backup(tmp_path / "target"))
    workspace = source.session.workspace
    target.navigate(Location("HomeDomain"))
    assert target.queue_drop(drag(source), QModelIndex()) == Qt.DropAction.CopyAction
    source.close()
    assert source.isVisible() and workspace.exists()
    qtbot.waitUntil(
        lambda: target.task is None and not target._drop_queued and not source.isVisible()
    )
    assert not workspace.exists() and target.session.at("HomeDomain", "a.txt")


def test_save_copy_claim_transfer_and_release(factory, tmp_path, qtbot, monkeypatch):
    root = make_backup(tmp_path / "source")
    w = factory(root)
    w.session.new_directory("HomeDomain", "", "new")
    output = tmp_path / "output"
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *a: (str(output), ""))
    w.save(quiet=True)
    qtbot.waitUntil(lambda: w.task is None)
    assert factory.manager.owner_for(root) is None
    assert factory.manager.owner_for(output) is w
    w.close_backup()
    ready(w, qtbot)
    assert factory.manager.owner_for(output) is None


def test_process_claim_is_exclusive_and_dead_process_is_recoverable(tmp_path, qapp):
    root = make_backup(tmp_path / "root")
    locks = tmp_path / "locks"
    code = """
import sys
from PySide6.QtCore import QCoreApplication
from planbak.backup_windows import BackupWindows
app=QCoreApplication([])
class Owner: pass
owner=Owner()
manager=BackupWindows(sys.argv[2])
claim=manager.reserve(sys.argv[1],owner)
print('locked',flush=True)
sys.stdin.read()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(root), str(locks)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )

    class Owner:
        pass

    owner = Owner()
    manager = BackupWindows(locks)
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(BackupError):
            manager.reserve(root, owner)
    finally:
        process.kill()
        process.communicate(timeout=5)
    claim = manager.reserve(root, owner)
    claim.release()


def test_progress_and_confirmation_are_window_modal(factory, tmp_path, qtbot):
    first = factory(make_backup(tmp_path / "first"))
    second = factory(make_backup(tmp_path / "second"))
    release = threading.Event()
    first.run_task("test", lambda progress, cancel: release.wait(3), dialog_progress=True)
    try:
        assert first.progress_dialog.windowModality() == Qt.WindowModality.WindowModal
        assert first.progress_dialog.parentWidget() is first
        assert second.open_backup_action.isEnabled()
        dialog = module.PasswordDialog(first)
        assert dialog.windowModality() == Qt.WindowModality.WindowModal
        dialog.deleteLater()
    finally:
        release.set()
        qtbot.waitUntil(lambda: first.task is None)


def test_close_with_saved_plist_buffer_applies_then_cleans(factory, tmp_path, qtbot, monkeypatch):
    import plistlib

    from test_ui import choose

    w = factory(make_backup(tmp_path / "source"))
    w.navigate(Location("SystemPreferencesDomain"))
    choose(w, "config.plist")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.plist_editors))
    rec = next(iter(w.plist_editors.values()))
    rec.editor.document.root.children[0].value = False
    rec.editor.rebuild()
    assert rec.editor.save_document()
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Save)
    out = tmp_path / "saved"
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *a: (str(out), ""))
    workspace = w.session.workspace
    w.close_backup()
    ready(w, qtbot)
    assert w.session is None and not workspace.exists() and not w.plist_editors
    reader = BackupSession(out)
    try:
        path = reader.cached_file(reader.at("SystemPreferencesDomain", "config.plist").token)
        assert plistlib.loads(path.read_bytes()) == {"sample": False}
    finally:
        reader.close()


def test_copy_creation_holds_then_transfers_claim(factory, tmp_path, qtbot, monkeypatch):
    w = factory(make_backup(tmp_path / "source"))
    source = w.session.root
    out = tmp_path / "copy"
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *a: (str(out), ""))
    w.create_backup_copy()
    assert factory.manager.owner_for(out) is w
    qtbot.waitUntil(lambda: w.copy_task is None and w.task is None)
    assert w.session.root == out and factory.manager.owner_for(out) is w
    assert factory.manager.owner_for(source) is None


def test_pending_open_claim_blocks_another_window(factory, tmp_path, qtbot, monkeypatch):
    original = module.BackupSession
    release, started = threading.Event(), threading.Event()

    class SlowSession(original):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            started.set()
            assert release.wait(4)

    monkeypatch.setattr(module, "BackupSession", SlowSession)
    root = make_backup(tmp_path / "source")
    first, second = factory(), factory()
    try:
        first.open_path(root)
        qtbot.waitUntil(started.is_set)
        second.open_path(root)
        assert second.session is None and second.task is None
        assert factory.manager.owner_for(root) is first
    finally:
        release.set()
        ready(first, qtbot)


def test_target_switch_before_queued_copy_releases_source_lease(factory, tmp_path, qtbot):
    source = factory(make_backup(tmp_path / "source"))
    target = factory(make_backup(tmp_path / "target"))
    target.navigate(Location("HomeDomain"))
    assert target.queue_drop(drag(source), QModelIndex()) == Qt.DropAction.CopyAction
    # 在队列消费前请求关闭，不能让租约泄漏或把文件加到下一会话。
    target.close_backup()
    ready(target, qtbot)
    qtbot.waitUntil(lambda: not target._drop_queued)
    assert target.session is None and not source.transfers.active


def test_cancelled_save_releases_reserved_output(factory, tmp_path, qtbot, monkeypatch):
    w = factory(make_backup(tmp_path / "source"))
    w.session.new_directory("HomeDomain", "", "pending")
    output = tmp_path / "cancelled"
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *a: (str(output), ""))

    def cancel(*a, **kw):
        raise Cancelled()

    monkeypatch.setattr(w.session, "save", cancel)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Save)
    w.close_backup()
    ready(w, qtbot)
    assert w.session is not None and w.session.dirty
    assert factory.manager.owner_for(output) is None and not output.exists()


@pytest.mark.parametrize("source_encrypted", [False, True])
@pytest.mark.parametrize("target_encrypted", [False, True])
def test_copy_between_encryption_modes_can_be_applied(
    factory, tmp_path, qtbot, monkeypatch, source_encrypted, target_encrypted
):
    from conftest import PASSWORD

    def password(dialog):
        dialog.password.setText(PASSWORD)
        return module.QDialog.DialogCode.Accepted

    monkeypatch.setattr(module.PasswordDialog, "exec", password)
    source = factory(make_backup(tmp_path / "source", source_encrypted))
    target = factory(make_backup(tmp_path / "target", target_encrypted))
    target.navigate(Location("HomeDomain"))
    assert target.handle_drop(drag(source), QModelIndex())
    qtbot.waitUntil(lambda: target.task is None)
    assert not source.session.dirty
    output = tmp_path / "applied"
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *a: (str(output), ""))
    target.save(quiet=True)
    qtbot.waitUntil(lambda: target.task is None)
    assert not target.session.dirty
    read = BackupSession(output, PASSWORD)
    try:
        assert read.cached_file(read.at("HomeDomain", "a.txt").token).read_bytes() == b"hello world"
        assert read.encrypted == target_encrypted
    finally:
        read.close()


def test_close_backup_cleans_its_viewers_without_affecting_other_window(
    factory, tmp_path, qtbot, monkeypatch
):
    from PIL import Image
    from test_ui import choose, open_test_database, open_test_hex

    first = factory(make_backup(tmp_path / "first"))
    second = factory(make_backup(tmp_path / "second"))
    open_test_database(first, qtbot, tmp_path)
    open_test_hex(first, qtbot)
    image = tmp_path / "picture.png"
    Image.new("RGB", (16, 12), "red").save(image)
    first.session.import_paths([image], "HomeDomain", "Documents")
    first.navigate(Location("HomeDomain", "Documents"))
    choose(first, "picture.png")
    first.open_file()
    qtbot.waitUntil(lambda: first.task is None and bool(first.image_viewers))
    workspace = first.session.workspace
    other_workspace = second.session.workspace
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Discard)
    first.close_backup()
    ready(first, qtbot)
    assert not workspace.exists() and first.session is None
    assert not first.readonly.database_viewers and not first.readonly.hex_viewers and not first.image_viewers
    assert other_workspace.exists() and second.session is not None and second.isVisible()


def test_transfer_import_failure_rolls_back_target_and_releases_lease(
    factory, tmp_path, qtbot, monkeypatch
):
    source = factory(make_backup(tmp_path / "source"))
    target = factory(make_backup(tmp_path / "target"))
    target.navigate(Location("HomeDomain"))
    original = target.session.import_paths

    def fail(*args, progress=None, **kwargs):
        def notify(text, done, total):
            if text.phase.name == "UPDATE_CHANGES" and done:
                raise OSError("injected failure after cache install")

        return original(*args, progress=notify, **kwargs)

    monkeypatch.setattr(target.session, "import_paths", fail)
    assert target.handle_drop(drag(source), QModelIndex())
    qtbot.waitUntil(lambda: target.task is None)
    assert not target.session.dirty and target.session.at("HomeDomain", "a.txt") is None
    assert not source.session.dirty and not source.transfers.active
    assert not list(target.session.workspace.glob(".transfer-*"))
    assert not list(target.session.workspace.glob(".change-*"))
    assert factory.errors


def test_open_during_slow_viewer_close_cannot_leave_reserved_target(
    factory, tmp_path, qtbot, monkeypatch
):
    from test_ui import open_test_hex

    w = factory(make_backup(tmp_path / "source"))
    w.session.new_directory("HomeDomain", "", "pending")
    record = open_test_hex(w, qtbot)
    started, release = threading.Event(), threading.Event()

    def slow(*a, cancel, **kw):
        started.set()
        assert release.wait(3)
        raise Cancelled()

    monkeypatch.setattr("planbak.hex_viewer.search_bytes", slow)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Cancel)
    record.viewer.search_input.setText("FF")
    record.viewer.find_next()
    qtbot.waitUntil(started.is_set)
    other = make_backup(tmp_path / "other")
    try:
        w.close()
        assert w._readonly_closing and not w.open_backup_action.isEnabled()
        w.open_path(other)
        assert factory.manager.owner_for(other) is None
    finally:
        release.set()
    qtbot.waitUntil(lambda: not w._readonly_closing and not w.readonly.hex_viewers)
    qtbot.wait(10)
    assert w.isVisible() and w.session is not None and not w._transitioning


@pytest.mark.parametrize("operation", ["close_backup", "switch", "exit"])
def test_owned_editor_modal_prevents_parent_cleanup_but_not_other_backup(
    factory, tmp_path, qtbot, operation
):
    from test_ui import choose

    from planbak.plist_editor import ValueDialog

    w = factory(make_backup(tmp_path / "source"))
    other = factory(make_backup(tmp_path / "other-window"))
    replacement = make_backup(tmp_path / "replacement")
    w.navigate(Location("SystemPreferencesDomain"))
    choose(w, "config.plist")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.plist_editors))
    rec = next(iter(w.plist_editors.values()))
    node = rec.editor.document.root.children[0]
    dialog = ValueDialog(node, "Dictionary", rec.editor.document.root.children, True, rec.editor)
    qtbot.addWidget(dialog)
    dialog.show()
    workspace = w.session.workspace
    try:
        w.open_file(external=True)
        w.revert_pending(rec.token)
        assert w.task is None and rec.token in w.plist_editors and dialog.isVisible()
        if operation == "close_backup":
            w.close_backup()
        elif operation == "switch":
            w.open_path(replacement)
        else:
            w.close()
        assert w.session is not None and workspace.exists() and dialog.isVisible()
        assert not w._transitioning and factory.manager.owner_for(replacement) is None
        other.close_backup()
        ready(other, qtbot)
        assert other.session is None and dialog.isVisible() and w.session is not None
    finally:
        dialog.reject()


def test_deleting_target_before_drop_consumption_releases_source(factory, tmp_path, qtbot):
    from shiboken6 import isValid

    source = factory(make_backup(tmp_path / "source"))
    # 工厂已负责清理；此处让Qt真正删除窗口，不再交qtbot重复close已销毁的C++对象。
    target = factory(make_backup(tmp_path / "target"), track=False)
    target.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    target.navigate(Location("HomeDomain"))
    assert target.queue_drop(drag(source), QModelIndex()) == Qt.DropAction.CopyAction
    assert source.transfers.active == 1
    target.force_close = True
    target.close()
    assert source.transfers.active == 0
    qtbot.waitUntil(lambda: not isValid(target))
    source.session.stage_bytes(
        source.session.at("HomeDomain", "Documents/a.txt").token, b"still editable"
    )
    assert source.session.dirty


def test_search_stays_modeless_and_can_save_to_session(factory, tmp_path, qtbot):
    from test_ui import choose

    w = factory(make_backup(tmp_path / "source"))
    w.navigate(Location("SystemPreferencesDomain"))
    choose(w, "config.plist")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.plist_editors))
    editor = next(iter(w.plist_editors.values())).editor
    editor.switch_mode(1)
    editor.xml.code.setPlainText(editor.xml.code.toPlainText().replace("<true/>", "<false/>"))
    editor.xml.show_search()
    assert editor.xml.search_dialog.windowModality() == Qt.WindowModality.NonModal
    assert editor.save_document() and w.session.dirty
    editor.xml.hide_search()


def test_late_plist_check_waits_for_value_dialog(factory, tmp_path, qtbot, monkeypatch):
    import plistlib

    from test_ui import choose

    from planbak.plist_editor import ValueDialog

    w = factory(make_backup(tmp_path / "source"))
    w.navigate(Location("SystemPreferencesDomain"))
    choose(w, "config.plist")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.plist_editors))
    rec = next(iter(w.plist_editors.values()))
    w.session.stage_bytes(rec.token, plistlib.dumps({"sample": False}))
    release, started = threading.Event(), threading.Event()
    original = w.session.extract_file

    def read(*args, **kw):
        started.set()
        assert release.wait(3)
        return original(*args, **kw)

    monkeypatch.setattr(w.session, "extract_file", read)
    w.check_plist_editors()
    qtbot.waitUntil(started.is_set)
    dialog = ValueDialog(
        rec.editor.document.root.children[0],
        "Dictionary",
        rec.editor.document.root.children,
        True,
        rec.editor,
    )
    qtbot.addWidget(dialog)
    dialog.show()
    try:
        release.set()
        qtbot.waitUntil(lambda: w.task is None)
        assert rec.editor.document.root.to_value() == {"sample": True} and dialog.isVisible()
    finally:
        dialog.reject()
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    w.check_plist_editors()
    qtbot.waitUntil(lambda: w.task is None)
    assert rec.editor.document.root.to_value() == {"sample": False}


def test_another_backups_modal_does_not_stop_version_checks(factory, tmp_path, qtbot, monkeypatch):
    import plistlib

    from test_ui import choose

    w = factory(make_backup(tmp_path / "first"))
    other = factory(make_backup(tmp_path / "second"))
    w.navigate(Location("SystemPreferencesDomain"))
    choose(w, "config.plist")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.plist_editors))
    rec = next(iter(w.plist_editors.values()))
    w.session.stage_bytes(rec.token, plistlib.dumps({"sample": False}))
    dialog = module.PasswordDialog(other)
    qtbot.addWidget(dialog)
    dialog.show()
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    try:
        w.check_plist_editors()
        assert w.task is not None
        qtbot.waitUntil(lambda: w.task is None)
        assert rec.editor.document.root.to_value() == {"sample": False} and dialog.isVisible()
    finally:
        dialog.reject()


def test_another_backups_modal_does_not_block_browser_focus_restore(factory, tmp_path, qtbot):
    from PySide6.QtCore import QTimer
    from test_ui import open_test_plist

    w = factory(make_backup(tmp_path / "first"))
    other = factory(make_backup(tmp_path / "second"))
    rec = open_test_plist(w, qtbot)
    dialog = module.PasswordDialog(other)
    qtbot.addWidget(dialog)
    dialog.show()
    try:
        rec.editor.activateWindow()
        rec.editor.tree.setFocus()
        qtbot.waitUntil(rec.editor.isActiveWindow)
        QTimer.singleShot(
            0, lambda: w.directory_tree.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        )
        rec.editor.close()
        qtbot.waitUntil(w.active_view().hasFocus)
        assert not w.sidebar_active() and dialog.isVisible()
    finally:
        dialog.reject()


@pytest.mark.parametrize("minimized", [False, True])
def test_reopening_current_backup_preserves_maximized_state(factory, tmp_path, qtbot, minimized):
    root = make_backup(tmp_path / "backup")
    w = factory(root)
    original = w.session
    workspace = original.workspace
    w.showMaximized()
    qtbot.waitUntil(w.isMaximized)
    if minimized:
        w.showMinimized()
        qtbot.waitUntil(w.isMinimized)
    w.open_path(root)
    qtbot.waitUntil(lambda: not w.isMinimized())
    assert w.isMaximized()
    assert w.session is original and w.session.workspace == workspace
    assert w.task is None and not factory.errors


def test_switch_from_original_to_existing_encrypted_copy_reloads(
    factory, tmp_path, qtbot, monkeypatch
):
    from conftest import PASSWORD

    calls = []

    def password(dialog):
        calls.append(True)
        dialog.password.setText(PASSWORD)
        return module.QDialog.DialogCode.Accepted

    monkeypatch.setattr(module.PasswordDialog, "exec", password)
    source = make_backup(tmp_path / "original", True)
    w = factory(source)
    copied = w.session.create_copy(tmp_path / "edited")
    old = w.session
    workspace = old.workspace
    w.showMaximized()
    qtbot.waitUntil(w.isMaximized)
    w.open_path(copied.path)
    ready(w, qtbot)
    assert len(calls) == 2
    assert w.session is not old and w.session.root == copied.path
    assert w.session.workspace != workspace and not workspace.exists()
    assert w.session.editable and w.isMaximized()
    assert factory.manager.owner_for(source) is None
    assert factory.manager.owner_for(copied.path) is w
    assert not factory.errors


@pytest.mark.parametrize("outcome", ["open", "password-cancel", "wrong-password", "dirty-cancel"])
def test_reopen_copy_moved_and_renamed_outside_app(factory, tmp_path, qtbot, monkeypatch, outcome):
    from conftest import PASSWORD

    prompts = []

    def initial_password(dialog):
        dialog.password.setText(PASSWORD)
        return module.QDialog.DialogCode.Accepted

    monkeypatch.setattr(module.PasswordDialog, "exec", initial_password)
    original = make_backup(tmp_path / "UDID", True)
    w = factory(original)
    copy = tmp_path / "UDID-copy"
    monkeypatch.setattr(module.QFileDialog, "getSaveFileName", lambda *a: (str(copy), ""))
    w.create_backup_copy()
    qtbot.waitUntil(lambda: w.copy_task is None and w.task is None and w._copy_pending is None)
    assert w.session.root == copy
    old, old_claim = w.session, w._backup_claim
    workspace = old.workspace
    parent = tmp_path / "testbackup-edited"
    parent.mkdir()
    moved = parent / "UDID"
    copy.rename(moved)
    assert factory.manager.owner_for(moved) is w
    competitor = BackupWindows(factory.manager.lock_directory)
    w.showMaximized()
    qtbot.waitUntil(w.isMaximized)
    if outcome == "dirty-cancel":
        old.new_directory("HomeDomain", "", "unsaved")
        monkeypatch.setattr(QMessageBox, "exec", lambda *a: QMessageBox.StandardButton.Cancel)

    def password(dialog):
        prompts.append(True)
        assert w.session is None and not workspace.exists()
        # 清理旧会话后、密码提示期间，身份锁仍不允许其他进程/窗口占用。
        with pytest.raises(BackupError):
            competitor.reserve(moved, w)
        if outcome == "password-cancel":
            return module.QDialog.DialogCode.Rejected
        dialog.password.setText("incorrect" if outcome == "wrong-password" else PASSWORD)
        return module.QDialog.DialogCode.Accepted

    monkeypatch.setattr(module.PasswordDialog, "exec", password)
    w.open_path(parent)
    ready(w, qtbot)
    assert w.isMaximized()
    if outcome == "dirty-cancel":
        assert not prompts and w.session is old and workspace.exists() and old.dirty
        assert w._backup_claim is old_claim
        assert factory.manager.owner_for(moved) is w
        with pytest.raises(BackupError):
            competitor.reserve(moved, w)
    else:
        assert len(prompts) == 1 and not workspace.exists()
        assert old_claim.released
        if outcome == "open":
            assert w.session is not old and w.session.root == moved and w.session.editable
            assert w.session.workspace != workspace
            with pytest.raises(BackupError):
                competitor.reserve(moved, w)
        else:
            assert w.session is None and factory.manager.owner_for(moved) is None
            released = competitor.reserve(moved, w)
            released.release()
    assert not factory.errors or outcome == "wrong-password"


def test_failed_relocated_reservation_does_not_unlock_old_identity(factory, tmp_path):
    root = make_backup(tmp_path / "old")

    class Owner:
        pass

    owner = Owner()
    original = factory.manager.reserve(root, owner)
    competitor = BackupWindows(factory.manager.lock_directory)
    target = tmp_path / "new"
    blocking = competitor.reserve(target, owner)
    root.rename(target)
    try:
        with pytest.raises(BackupError):
            factory.manager.reserve(target, owner, shared=original)
        blocking.release()
        with pytest.raises(BackupError):
            competitor.reserve(target, owner)
    finally:
        blocking.release()
        original.release()
    unlocked = competitor.reserve(target, owner)
    unlocked.release()
