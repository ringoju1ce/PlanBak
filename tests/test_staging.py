from pathlib import Path

import pytest
from conftest import PASSWORD

from planbak.backup import BackupSession
from planbak.codec import BackupError, Cancelled


@pytest.fixture
def session(backup_root):
    s = BackupSession(backup_root, PASSWORD)
    yield s
    s.close()


def contents(session, token, tmp_path):
    path = tmp_path / "extracted"
    session.extract_file(token, path)
    return path.read_bytes()


def test_unique_cache_final_difference_and_content_reset(session, tmp_path):
    token = session.at("HomeDomain", "Documents/a.txt").token
    path = session.cached_file(token)
    assert path.read_bytes() == b"hello world" and not session.dirty
    session.stage_bytes(token, b"first")
    session.stage_bytes(token, b"second")
    assert session.cached_file(token) == path
    assert path.read_bytes() == b"second"
    assert list((session.workspace / "pending").iterdir()) == [path]
    assert len(session.changes) == 1 and session.changes[0].labels == ("修改",)
    session.stage_bytes(token, b"hello world")
    assert not session.dirty and not session.changes
    assert session.get(token).staged is None
    assert contents(session, token, tmp_path) == b"hello world"
    assert not hasattr(session, "undo_stack") and not hasattr(session, "undo")


def test_add_edit_delete_and_modified_delete_merge(session, tmp_path):
    path = tmp_path / "added.txt"
    path.write_bytes(b"one")
    session.import_paths([path], "HomeDomain", "")
    added = session.at("HomeDomain", "added.txt")
    session.stage_bytes(added.token, b"two")
    assert session.changes[0].labels == ("添加",)
    session.delete([added.token])
    assert not session.dirty and not session.cache_path(added.token).exists()
    token = session.at("HomeDomain", "Documents/a.txt").token
    session.stage_bytes(token, b"modified")
    session.delete([token])
    assert len(session.changes) == 1 and session.changes[0].labels == ("删除",)
    session.revert(session.plan_revert(token))
    assert contents(session, token, tmp_path) == b"hello world" and not session.dirty


def test_revert_new_directory_restores_moved_baseline_files_and_removes_added_descendants(
    session, tmp_path
):
    directory = session.new_directory("HomeDomain", "", "new")
    child_dir = session.new_directory("HomeDomain", "new", "nested")
    source = tmp_path / "added.txt"
    source.write_bytes(b"new")
    session.import_paths([source], "HomeDomain", child_dir.path)
    original = session.at("HomeDomain", "Documents/a.txt")
    session.move([original.token], "HomeDomain", "new")
    session.stage_bytes(original.token, b"changed")
    session.revert(session.plan_revert(directory.token))
    assert session.at("HomeDomain", "new") is None
    assert session.at("HomeDomain", original.path).token == original.token
    assert contents(session, original.token, tmp_path) == b"hello world"
    assert not session.dirty


def test_new_directory_revert_also_restores_baseline_file_deleted_inside_it(session, tmp_path):
    directory = session.new_directory("HomeDomain", "", "new")
    original = session.at("HomeDomain", "Documents/a.txt")
    session.move([original.token], "HomeDomain", "new")
    session.delete([original.token])
    session.revert(session.plan_revert(directory.token))
    assert session.at("HomeDomain", original.path).token == original.token
    assert contents(session, original.token, tmp_path) == b"hello world" and not session.dirty


def test_directory_rename_revert_preserves_independent_content_changes(session, tmp_path):
    directory = session.at("HomeDomain", "Documents")
    child = session.at("HomeDomain", "Documents/a.txt")
    session.move([directory.token], "HomeDomain", "", name="Renamed")
    assert len(session.changes) == 1 and session.changes[0].labels == ("重命名文件夹",)
    session.stage_bytes(child.token, b"modified")
    assert {change.labels for change in session.changes} == {("重命名文件夹",), ("修改",)}
    session.revert(session.plan_revert(directory.token))
    assert session.get(child.token).path == "Documents/a.txt"
    assert contents(session, child.token, tmp_path) == b"modified"
    assert len(session.changes) == 1 and session.changes[0].labels == ("修改",)
    session.revert(session.plan_revert(child.token))
    assert not session.dirty


def test_revert_child_content_under_renamed_parent_keeps_parent_rename(session, tmp_path):
    directory = session.at("HomeDomain", "Documents")
    child = session.at("HomeDomain", "Documents/a.txt")
    session.move([directory.token], "HomeDomain", "", name="Renamed")
    session.stage_bytes(child.token, b"modified")
    session.revert(session.plan_revert(child.token))
    assert session.get(child.token).path == "Renamed/a.txt"
    assert contents(session, child.token, tmp_path) == b"hello world"
    assert len(session.changes) == 1 and session.changes[0].token == directory.token


def test_deleted_directory_restore_and_path_conflict_atomicity(session, tmp_path):
    directory = session.at("HomeDomain", "Documents")
    session.delete([directory.token])
    session.revert(session.plan_revert(directory.token))
    assert session.at("HomeDomain", "Documents/sub/b.txt") and not session.dirty
    file = session.at("HomeDomain", "Documents/a.txt")
    session.move([file.token], "HomeDomain", "", name="renamed.txt")
    source = tmp_path / "a.txt"
    source.write_bytes(b"occupant")
    session.import_paths([source], "HomeDomain", "Documents")
    snapshot = session.changes
    with pytest.raises(BackupError, match="占用"):
        session.plan_revert(file.token)
    assert session.changes == snapshot
    assert (
        contents(session, session.at("HomeDomain", "Documents/a.txt").token, tmp_path)
        == b"occupant"
    )


def test_revert_deleted_child_restores_missing_ancestors(session):
    directory = session.at("HomeDomain", "Documents")
    child = session.at("HomeDomain", "Documents/sub/b.txt")
    session.delete([directory.token])
    session.revert(session.plan_revert(child.token))
    assert session.at("HomeDomain", "Documents/sub/b.txt")
    assert session.at("HomeDomain", "Documents/sub")
    assert session.at("HomeDomain", "Documents")
    assert session.at("HomeDomain", "Documents/a.txt") is None


def test_single_cache_transaction_failure_and_cancel_preserve_current_contents(session):
    token = session.at("HomeDomain", "Documents/a.txt").token
    session.stage_bytes(token, b"first")
    path = Path(session.get(token).staged)
    snapshot = session.changes
    with pytest.raises(Cancelled):
        session.stage_bytes(token, b"second", cancel=lambda: True)
    assert path.read_bytes() == b"first" and session.changes == snapshot
    cancelled = False

    def progress(*args):
        nonlocal cancelled
        cancelled = True

    plan = session.plan_revert(token)
    with pytest.raises(Cancelled):
        session.revert(plan, progress=progress, cancel=lambda: cancelled)
    assert path.read_bytes() == b"first" and session.changes == snapshot
    assert not list(session.workspace.glob(".change-*"))


def test_save_rebases_immutable_baseline_and_reuses_cache(session, tmp_path):
    token = session.at("HomeDomain", "Documents/a.txt").token
    original_root = session.root
    session.stage_bytes(token, b"saved")
    cache = session.cached_file(token)
    output = tmp_path / "saved"
    session.save(output)
    assert session.root == output and not session.dirty
    assert session.cached_file(token) == cache
    session.stage_bytes(token, b"later")
    session.revert(session.plan_revert(token))
    assert contents(session, token, tmp_path) == b"saved"
    assert not session.dirty
    opened = BackupSession(output, PASSWORD)
    try:
        restored = opened.at("HomeDomain", "Documents/a.txt").token
        assert contents(opened, restored, tmp_path) == b"saved"
    finally:
        opened.close()
    session.stage_bytes(token, b"second save")
    session.save(tmp_path / "saved-again")
    assert session.root != original_root and not session.dirty
    assert contents(session, token, tmp_path) == b"second save"


def test_tooltip_rename_contains_only_original_full_path(session):
    token = session.at("HomeDomain", "Documents/a.txt").token
    session.move([token], "HomeDomain", "", name="renamed.txt")
    change = session.changes[0]
    assert change.labels == ("重命名",)
    assert change.tooltip == "/HomeDomain/Documents/a.txt"
    assert "renamed.txt" not in change.tooltip


def test_new_directory_revert_restores_moved_baseline_directory_tree(session, tmp_path):
    directory = session.new_directory("HomeDomain", "", "new")
    old = session.at("HomeDomain", "Documents/sub")
    child = session.at("HomeDomain", "Documents/sub/b.txt")
    session.move([old.token], "HomeDomain", "new")
    session.revert(session.plan_revert(directory.token))
    assert session.at("HomeDomain", "new") is None
    assert session.get(child.token).path == "Documents/sub/b.txt"
    assert contents(session, child.token, tmp_path) == b"nested" and not session.dirty


def test_failure_after_cache_install_rolls_back_both_file_and_index(session):
    from dataclasses import replace

    token = session.at("HomeDomain", "Documents/a.txt").token
    session.stage_bytes(token, b"current")
    snapshot, revision = session.changes, session.signature
    path = session.cached_file(token)

    def fail(*args):
        raise OSError("injected failure after installing cache")

    with pytest.raises(OSError):
        with session.mutation(progress=fail) as work:
            staged, size = work.file(token, data=b"new but rolled back")
            work.put(replace(session.get(token), staged=staged, size=size))
    assert path.read_bytes() == b"current"
    assert session.changes == snapshot and session.signature == revision
    assert not list(session.workspace.glob(".change-*"))


def test_failed_rebase_does_not_publish_or_change_baseline(session, tmp_path, monkeypatch):
    root = session.root
    token = session.at("HomeDomain", "Documents/a.txt").token
    session.stage_bytes(token, b"current")
    snapshot = session.changes

    def fail(*args):
        raise BackupError("cannot prepare next baseline")

    monkeypatch.setattr(session, "prepare_saved_index", fail)
    output = tmp_path / "failed-save"
    with pytest.raises(BackupError):
        session.save(output)
    assert not output.exists() and not list(tmp_path.glob(".planbak-save-*"))
    assert session.root == root and session.changes == snapshot and session.dirty
    assert session.cached_file(token).read_bytes() == b"current"


@pytest.mark.parametrize("collision", ["error", "replace", "skip", "keep_both"])
def test_multi_move_with_duplicate_source_names(backup_root, tmp_path, collision):
    session = BackupSession(backup_root, PASSWORD)
    try:
        session.new_directory("HomeDomain", "Documents", "destination")
        duplicate = tmp_path / "a.txt"
        duplicate.write_bytes(b"second source")
        session.import_paths([duplicate], "HomeDomain", "Documents/sub")
        original = session.at("HomeDomain", "Documents/a.txt")
        second = session.at("HomeDomain", "Documents/sub/a.txt")
        tokens = [original.token, second.token]
        if collision in ("error", "replace"):
            before = session.changes
            with pytest.raises(BackupError, match="目标"):
                session.move(tokens, "HomeDomain", "Documents/destination", collision=collision)
            assert session.changes == before
            assert session.at("HomeDomain", "Documents/a.txt")
            assert session.at("HomeDomain", "Documents/sub/a.txt")
            assert not session.at("HomeDomain", "Documents/destination/a.txt")
        else:
            session.move(tokens, "HomeDomain", "Documents/destination", collision=collision)
            moved = session.at("HomeDomain", "Documents/destination/a.txt")
            assert moved
            alternate = session.at("HomeDomain", "Documents/destination/a (2).txt")
            if collision == "keep_both":
                assert alternate
                assert {session.cached_file(moved.token).read_bytes(),
                        session.cached_file(alternate.token).read_bytes()} == {
                    b"hello world", b"second source"
                }
            else:
                assert alternate is None
                assert sum(session.at("HomeDomain", path) is not None for path in (
                    "Documents/a.txt", "Documents/sub/a.txt"
                )) == 1
    finally:
        session.close()
