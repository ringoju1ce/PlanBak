import hashlib
from pathlib import Path

import pytest
from conftest import PASSWORD

from planbak.backup import BackupSession, detect_backups
from planbak.codec import BackupError, Cancelled, Metadata, file_id, safe_path


def hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_browse_extract_edit_save(backup_root, tmp_path):
    original = hashes(backup_root)
    with_session = BackupSession(backup_root, PASSWORD)
    s = with_session
    try:
        assert s.domains() == ["HomeDomain", "SystemPreferencesDomain"]
        assert s.at("HomeDomain", "implicit").kind == "directory"
        source = s.at("HomeDomain", "Documents/a.txt")
        extracted = tmp_path / "opened.txt"
        s.extract_file(source.token, extracted)
        assert extracted.read_bytes() == b"hello world"
        extracted.write_bytes(b"changed external editor")
        s.stage_edit(source.token, extracted)
        s.move([source.token], "HomeDomain", "Documents", name="renamed.txt")
        new = tmp_path / "new.dat"
        new.write_bytes(b"new file content")
        s.import_paths([new], "HomeDomain", "Documents/sub")
        s.delete([s.at("HomeDomain", "Documents/empty.bin").token])
        s.new_directory("HomeDomain", "Documents", "empty-directory")
        assert s.dirty
        output = s.save(tmp_path / "output")
        assert not s.dirty
        assert hashes(backup_root) == original
        read = BackupSession(output, PASSWORD)
        try:
            assert read.at("HomeDomain", "Documents/a.txt") is None
            assert read.at("HomeDomain", "Documents/empty.bin") is None
            target = tmp_path / "verify.txt"
            read.extract_file(read.at("HomeDomain", "Documents/renamed.txt").token, target)
            assert target.read_bytes() == b"changed external editor"
            read.extract_file(read.at("HomeDomain", "Documents/sub/new.dat").token, target)
            assert target.read_bytes() == b"new file content"
            assert read.at("HomeDomain", "Documents/empty-directory").kind == "directory"
            added = read.at("HomeDomain", "Documents/sub/new.dat")
            assert read.metadata(added).get("RelativePath") == added.path
        finally:
            read.close()
        # An independent project's decoder can read our encrypted output.
        if s.encrypted:
            from iphone_backup_decrypt import EncryptedBackup

            other = EncryptedBackup(backup_directory=str(output), passphrase=PASSWORD)
            try:
                assert (
                    other.extract_file_as_bytes("Documents/renamed.txt", domain_like="HomeDomain")
                    == b"changed external editor"
                )
                assert (
                    other.extract_file_as_bytes("Documents/sub/new.dat", domain_like="HomeDomain")
                    == b"new file content"
                )
            finally:
                other._cleanup()
                other._temporary_folder = None
        # Discard returns to the last successful save, not the initial source.
        s.delete([source.token])
        s.discard()
        assert s.at("HomeDomain", "Documents/renamed.txt") is not None
        assert not s.dirty
    finally:
        temp = s.workspace
        s.close()
        assert not temp.exists()


def test_atomic_import_and_single_revert(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    try:
        source = s.at("HomeDomain", "Documents/sub")
        s.move([source.token], "HomeDomain", "", name="moved")
        assert s.at("HomeDomain", "moved/b.txt")
        s.revert(s.plan_revert(source.token))
        assert s.at("HomeDomain", "Documents/sub/b.txt")
        s.move([source.token], "HomeDomain", "", name="moved")
        assert s.at("HomeDomain", "moved/b.txt")
        with pytest.raises(BackupError):
            s.move([source.token], "HomeDomain", "moved")
        folder = tmp_path / "import"
        folder.mkdir()
        (folder / "x").write_bytes(b"x")
        (folder / "bad-link").symlink_to("/etc/passwd")
        before = len(s.children("HomeDomain", ""))
        with pytest.raises(BackupError):
            s.import_paths([folder], "HomeDomain", "")
        assert len(s.children("HomeDomain", "")) == before
        assert s.at("HomeDomain", "import") is None
    finally:
        s.close()


def test_cancel_save_keeps_source_and_pending(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    try:
        original = hashes(backup_root)
        s.delete([s.at("HomeDomain", "Documents/a.txt").token])
        with pytest.raises(Cancelled):
            s.save(tmp_path / "cancelled", cancel=lambda: True)
        assert not (tmp_path / "cancelled").exists()
        assert s.dirty and hashes(backup_root) == original
        assert not list(tmp_path.glob(".planbak-save-*"))
    finally:
        s.close()


def test_wrong_password_and_detection(tmp_path):
    from conftest import make_backup

    root = make_backup(tmp_path / "encrypted", True)
    assert detect_backups(tmp_path) == [root]
    with pytest.raises(BackupError, match="密码"):
        BackupSession(root, "wrong")
    for path in ["../outside", "a/../../x", "/absolute", "a//b", "nul\x00"]:
        with pytest.raises(BackupError):
            safe_path(path)


def test_conflict_and_replace_metadata(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    try:
        entry = s.at("HomeDomain", "Documents/a.txt")
        replacement = tmp_path / "a.txt"
        replacement.write_bytes(b"replaced")
        with pytest.raises(BackupError):
            s.import_paths([replacement], "HomeDomain", "Documents")
        s.import_paths([replacement], "HomeDomain", "Documents", collision="replace")
        assert s.at("HomeDomain", "Documents/a.txt").original_id == entry.original_id
        s.revert(s.plan_revert(entry.token))
        p = tmp_path / "content"
        s.extract_file(entry.token, p)
        assert p.read_bytes() == b"hello world"
        s.import_paths([replacement], "HomeDomain", "Documents", collision="keep_both")
        assert s.at("HomeDomain", "Documents/a (2).txt")
        with pytest.raises(BackupError):
            s.save(backup_root / "inside")
    finally:
        s.close()


def test_metadata_archive_class_and_references():
    m = Metadata.new(directory=False, timestamp=1, uid=501, gid=501, clas=3)
    m.set("Digest", b"abc")
    m.set("EncryptionKey", b"xyz")
    result = Metadata(m.dump())
    assert result.get("Digest") == b"abc"
    assert (
        result.objects[result.root["Digest"].data]["$class"]
        == result.objects[result.root["EncryptionKey"].data]["$class"]
    )
    assert (
        file_id("HomeDomain", "Documents/a.txt")
        == hashlib.sha1(b"HomeDomain-Documents/a.txt").hexdigest()
    )


def test_export_replace_cancel_and_symlink_safety(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    try:
        entry = s.at("HomeDomain", "Documents/a.txt")
        output = tmp_path / "extracted"
        output.mkdir()
        target = output / "a.txt"
        target.write_bytes(b"preserve until success")
        with pytest.raises(Cancelled):
            s.export([entry.token], output, collision="replace", cancel=lambda: True)
        assert target.read_bytes() == b"preserve until success"
        s.export([entry.token], output, collision="replace")
        assert target.read_bytes() == b"hello world"
        target.unlink()
        outside = tmp_path / "outside"
        outside.write_bytes(b"private")
        target.symlink_to(outside)
        with pytest.raises(BackupError):
            s.export([entry.token], output, collision="replace")
        assert outside.read_bytes() == b"private"
    finally:
        s.close()


def test_streaming_binary_and_repeated_save(backup_root, tmp_path):
    import os

    s = BackupSession(backup_root, PASSWORD)
    try:
        data = os.urandom(2 * 1024 * 1024 + 19)
        source = tmp_path / "large.bin"
        source.write_bytes(data)
        s.import_paths([source], "HomeDomain", "Documents")
        first = s.save(tmp_path / "first")
        s.move(
            [s.at("HomeDomain", "Documents/large.bin").token], "HomeDomain", "", name="moved.bin"
        )
        second = s.save(tmp_path / "second")
        for root, path in ((first, "Documents/large.bin"), (second, "moved.bin")):
            reader = BackupSession(root, PASSWORD)
            try:
                output = tmp_path / "out.bin"
                reader.extract_file(reader.at("HomeDomain", path).token, output)
                assert output.read_bytes() == data
            finally:
                reader.close()
    finally:
        s.close()


def test_missing_payload_refuses_save(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    try:
        missing = s.at("HomeDomain", "Documents/a.txt")
        s.payload_path(missing.original_id).unlink()
        s.new_directory("HomeDomain", "", "pending")
        with pytest.raises(BackupError, match="缺少文件数据"):
            s.save(tmp_path / "invalid")
        assert s.dirty
        assert not (tmp_path / "invalid").exists()
    finally:
        s.close()


def test_literal_backslash_filename_on_linux(tmp_path):
    import os

    if os.name == "nt":
        pytest.skip("POSIX filename semantics")
    from conftest import make_backup

    s = BackupSession(make_backup(tmp_path / "backup"))
    try:
        source = tmp_path / "literal\\name.txt"
        source.write_bytes(b"not a directory separator")
        s.import_paths([source], "HomeDomain", "")
        entry = s.at("HomeDomain", source.name)
        target = tmp_path / "export"
        s.export([entry.token], target)
        assert (target / source.name).read_bytes() == source.read_bytes()
    finally:
        s.close()


def test_move_replacement_and_same_name_import(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    try:
        a = s.at("HomeDomain", "Documents/a.txt")
        b = s.at("HomeDomain", "Documents/sub/b.txt")
        s.move([a.token], "HomeDomain", "Documents/sub", name="b.txt", collision="replace")
        assert s.at("HomeDomain", "Documents/sub/b.txt").token == a.token
        s.discard()
        assert s.at("HomeDomain", "Documents/sub/b.txt").token == b.token
        assert s.at("HomeDomain", "Documents/a.txt")
        one, two = tmp_path / "one", tmp_path / "two"
        one.mkdir()
        two.mkdir()
        (one / "same.txt").write_bytes(b"one")
        (two / "same.txt").write_bytes(b"two")
        s.import_paths([one / "same.txt", two / "same.txt"], "HomeDomain", "", collision="replace")
        entry = s.at("HomeDomain", "same.txt")
        assert Path(entry.staged).read_bytes() == b"two"
        s.discard()
        assert s.at("HomeDomain", "same.txt") is None
    finally:
        s.close()


def editor_rows(session, table="EditorEntries"):
    return [tuple(row) for row in session.db.execute(f"SELECT * FROM {table} ORDER BY token")]


def test_discard_restores_mixed_changes_and_does_not_rewrite_unchanged_rows(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    try:
        baseline = editor_rows(s)
        a = s.at("HomeDomain", "Documents/a.txt")
        replacement = tmp_path / "a.txt"
        replacement.write_bytes(b"replacement")
        s.import_paths([replacement], "HomeDomain", "Documents", collision="replace")
        s.move([a.token], "HomeDomain", "", name="renamed.txt")
        folder = s.new_directory("HomeDomain", "", "created")
        del folder
        s.import_paths([replacement], "HomeDomain", "created")
        s.delete([s.at("HomeDomain", "Documents/sub").token])
        s.discard()
        assert editor_rows(s) == baseline
        assert not s.dirty and not s.changes
        s.stage_edit(a.token, replacement)
        changes = s.db.total_changes
        s.discard()
        # Work is proportional to the edited file, rather than every baseline row.
        assert s.db.total_changes - changes == 2
        assert editor_rows(s) == baseline
        added = s.new_directory("HomeDomain", "", "undone")
        s.revert(s.plan_revert(added.token))
        changes = s.db.total_changes
        s.discard()
        assert s.db.total_changes == changes and not s.changes
    finally:
        s.close()


def test_discard_cancellation_rolls_back_index_and_keeps_changes(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    try:
        source = tmp_path / "new.txt"
        source.write_bytes(b"new")
        s.import_paths([source], "HomeDomain", "")
        s.delete([s.at("HomeDomain", "Documents").token])
        pending = editor_rows(s)
        history = s.changes
        cancelled = False

        def progress(text, done, total):
            nonlocal cancelled
            if done:
                cancelled = True

        with pytest.raises(Cancelled):
            s.discard(progress=progress, cancel=lambda: cancelled)
        assert editor_rows(s) == pending and s.changes == history and s.dirty
        s.discard()
        assert editor_rows(s) == editor_rows(s, "EditorBaseline") and not s.dirty
    finally:
        s.close()


def test_discard_failure_keeps_index_and_history(backup_root, tmp_path):
    import sqlite3

    s = BackupSession(backup_root, PASSWORD)
    try:
        path = tmp_path / "changed.txt"
        path.write_bytes(b"changed")
        s.stage_edit(s.at("HomeDomain", "Documents/a.txt").token, path)
        pending = editor_rows(s)
        history = s.changes

        def authorize(action, table, *args):
            return (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_INSERT and table == "EditorEntries"
                else sqlite3.SQLITE_OK
            )

        s.db.set_authorizer(authorize)
        with pytest.raises(sqlite3.DatabaseError):
            s.discard()
        s.db.set_authorizer(None)
        assert editor_rows(s) == pending and s.changes == history and s.dirty
    finally:
        s.close()


def test_discard_uses_last_saved_state(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    try:
        token = s.at("HomeDomain", "Documents/a.txt").token
        path = tmp_path / "changed.txt"
        path.write_bytes(b"saved version")
        s.stage_edit(token, path)
        s.save(tmp_path / "saved")
        baseline = editor_rows(s)
        path.write_bytes(b"unsaved version")
        s.stage_edit(token, path)
        s.delete([token])
        s.discard()
        assert editor_rows(s) == baseline and not s.dirty
        s.extract_file(token, tmp_path / "restored.txt")
        assert (tmp_path / "restored.txt").read_bytes() == b"saved version"
    finally:
        s.close()


def test_missing_staged_content_never_falls_back_to_original(backup_root, tmp_path):
    from planbak.file_opening import source_revision

    session = BackupSession(backup_root, PASSWORD)
    try:
        token = session.at("HomeDomain", "Documents/a.txt").token
        session.stage_bytes(token, b"edited contents")
        staged = Path(session.get(token).staged)
        staged.unlink()
        operations = [
            lambda: session.extract_file(token, tmp_path / "extracted"),
            lambda: session.cached_file(token),
            lambda: source_revision(session, token),
            lambda: session.export([token], tmp_path / "exported"),
            lambda: session.save(tmp_path / "output"),
        ]
        for operation in operations:
            with pytest.raises(BackupError, match="暂挂的编辑内容不可用"):
                operation()
            assert session.dirty and not staged.exists()
        assert not (tmp_path / "output").exists()
    finally:
        session.close()


def test_missing_required_payload_fails_before_copy(backup_root, tmp_path, monkeypatch):
    import planbak.backup as module

    session = BackupSession(backup_root, PASSWORD)
    try:
        missing = session.at("HomeDomain", "Documents/a.txt")
        session.payload_path(missing.original_id).unlink()
        session.new_directory("HomeDomain", "Documents", "pending")
        copies = []
        original = module.copy_file

        def track_copy(*args, **kwargs):
            copies.append(args)
            return original(*args, **kwargs)

        monkeypatch.setattr(module, "copy_file", track_copy)
        with pytest.raises(BackupError, match="缺少文件数据"):
            session.save(tmp_path / "output")
        assert not copies and session.dirty and not (tmp_path / "output").exists()
    finally:
        session.close()


@pytest.mark.parametrize("resolution", ["delete", "replace"])
def test_preflight_does_not_require_deleted_or_replaced_payload(backup_root, tmp_path, resolution):
    session = BackupSession(backup_root, PASSWORD)
    try:
        entry = session.at("HomeDomain", "Documents/a.txt")
        if resolution == "replace":
            session.stage_bytes(entry.token, b"replacement")
        else:
            session.delete([entry.token])
        session.payload_path(entry.original_id).unlink()
        output = session.save(tmp_path / "output")
        reader = BackupSession(output, PASSWORD)
        try:
            current = reader.at("HomeDomain", "Documents/a.txt")
            if resolution == "delete":
                assert current is None
            else:
                assert reader.cached_file(current.token).read_bytes() == b"replacement"
        finally:
            reader.close()
    finally:
        session.close()


@pytest.mark.parametrize("operation", ["extract", "revision"])
def test_staged_file_disappearing_during_check_never_uses_original(
    backup_root, tmp_path, monkeypatch, operation
):
    import planbak.backup as backup_module
    import planbak.file_opening as opening_module

    session = BackupSession(backup_root, PASSWORD)
    try:
        token = session.at("HomeDomain", "Documents/a.txt").token
        session.stage_bytes(token, b"edited contents")
        module = backup_module if operation == "extract" else opening_module
        original_check = module.checked_staged_path

        def disappear(entry):
            path = original_check(entry)
            path.unlink()
            return path

        monkeypatch.setattr(module, "checked_staged_path", disappear)
        with pytest.raises(BackupError, match="暂挂的编辑内容不可用"):
            if operation == "extract":
                session.extract_file(token, tmp_path / "output")
            else:
                opening_module.source_revision(session, token)
        assert session.dirty and not (tmp_path / "output").exists()
    finally:
        session.close()


def test_staged_content_disappearing_during_save_reports_error(backup_root, tmp_path, monkeypatch):
    import planbak.backup as module

    session = BackupSession(backup_root, PASSWORD)
    try:
        token = session.at("HomeDomain", "Documents/a.txt").token
        session.stage_bytes(token, b"edited")
        staged = Path(session.get(token).staged)
        original = module.digest

        def disappear(path, *args, **kwargs):
            if Path(path) == staged:
                staged.unlink()
            return original(path, *args, **kwargs)

        monkeypatch.setattr(module, "digest", disappear)
        with pytest.raises(BackupError, match="暂挂的编辑内容不可用"):
            session.save(tmp_path / "output")
        assert session.dirty and not (tmp_path / "output").exists()
    finally:
        session.close()


def test_save_cancellation_is_not_misreported_if_staged_file_disappears(
    backup_root, tmp_path, monkeypatch
):
    import planbak.backup as module

    session = BackupSession(backup_root, PASSWORD)
    try:
        token = session.at("HomeDomain", "Documents/a.txt").token
        session.stage_bytes(token, b"edited")
        staged = Path(session.get(token).staged)
        original = module.digest

        def cancel(path, *args, **kwargs):
            if Path(path) == staged:
                staged.unlink()
                raise Cancelled("cancelled")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(module, "digest", cancel)
        with pytest.raises(Cancelled):
            session.save(tmp_path / "output")
        assert session.dirty and not (tmp_path / "output").exists()
    finally:
        session.close()
