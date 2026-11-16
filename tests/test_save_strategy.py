import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import PASSWORD
from test_backup import hashes
from test_ui import window as window

from planbak.backup import BackupSession, stale_workspaces
from planbak.backup_copy import MARKER, read_marker, write_marker
from planbak.codec import BackupError, Cancelled


def test_container_copy_and_apply_keep_backup_in_named_child(backup_root, tmp_path):
    original = hashes(backup_root)
    session = BackupSession(backup_root, PASSWORD)
    try:
        token = session.at('HomeDomain', 'Documents/a.txt').token
        session.stage_bytes(token, b'edited')
        copied = session.create_copy(tmp_path / 'copy-container', container=True)
        assert copied.path == tmp_path / 'copy-container' / backup_root.name
        assert read_marker(copied.path)
        assert len(list(copied.path.parent.iterdir())) == 1
        session.use_copy(copied)
        assert session.save() == copied.path
        reopened = BackupSession(copied.path, PASSWORD)
        try:
            output = tmp_path / 'read-copy'
            reopened.extract_file(reopened.at('HomeDomain', 'Documents/a.txt').token, output)
            assert output.read_bytes() == b'edited'
        finally:
            reopened.close()
        assert hashes(backup_root) == original
    finally:
        session.close()


def test_container_save_creates_named_child_and_preserves_original(backup_root, tmp_path):
    original = hashes(backup_root)
    session = BackupSession(backup_root, PASSWORD)
    try:
        token = session.at('HomeDomain', 'Documents/a.txt').token
        session.stage_bytes(token, b'edited')
        destination = session.save(tmp_path / 'output-container', container=True)
        assert destination == tmp_path / 'output-container' / backup_root.name
        assert session.root == destination and read_marker(destination)
        reopened = BackupSession(destination, PASSWORD)
        try:
            output = tmp_path / 'read-output'
            reopened.extract_file(reopened.at('HomeDomain', 'Documents/a.txt').token, output)
            assert output.read_bytes() == b'edited'
        finally:
            reopened.close()
        assert hashes(backup_root) == original
    finally:
        session.close()


def test_copy_preserves_pending_and_direct_save_roundtrip(backup_root, tmp_path):
    before = hashes(backup_root)
    s = BackupSession(backup_root, PASSWORD)
    try:
        token = s.at('HomeDomain', 'Documents/a.txt').token
        s.stage_bytes(token, b'first change')
        copied = s.create_copy(tmp_path / 'copy')
        assert read_marker(copied.path) and not read_marker(backup_root)
        baseline = BackupSession(copied.path, PASSWORD)
        try:
            path = tmp_path / 'extracted'
            baseline.extract_file(baseline.at('HomeDomain', 'Documents/a.txt').token, path)
            assert path.read_bytes() == b'hello world'
        finally:
            baseline.close()
        s.use_copy(copied)
        assert s.dirty and s.root == copied.path
        s.move([token], 'HomeDomain', 'Documents', name='renamed.txt')
        deleted = s.at('HomeDomain', 'Documents/empty.bin').token
        s.delete([deleted])
        s.new_directory('HomeDomain', 'Documents', 'new-folder')
        new = tmp_path / 'added.txt'
        new.write_bytes(b'new content')
        s.import_paths([new], 'HomeDomain', 'Documents/new-folder')
        marker = read_marker(s.root)
        untouched = s.payload_path(s.at('SystemPreferencesDomain', 'config.plist').original_id)
        unchanged_inode = untouched.stat().st_ino
        unchanged_bytes = untouched.read_bytes()
        assert s.save() == copied.path and not s.dirty
        assert read_marker(s.root) == marker
        directory = s.at('HomeDomain', 'Documents/new-folder')
        s.move([directory.token], 'HomeDomain', 'Documents', name='renamed-folder')
        s.stage_bytes(token, b'second change')
        assert s.save() == copied.path and not s.dirty
        read = BackupSession(copied.path, PASSWORD)
        try:
            assert read.at('HomeDomain', 'Documents/a.txt') is None
            assert read.at('HomeDomain', 'Documents/empty.bin') is None
            path = tmp_path / 'verified'
            read.extract_file(read.at('HomeDomain', 'Documents/renamed.txt').token, path)
            assert path.read_bytes() == b'second change'
            read.extract_file(read.at('HomeDomain', 'Documents/renamed-folder/added.txt').token, path)
            assert path.read_bytes() == b'new content'
        finally:
            read.close()
        assert untouched.stat().st_ino == unchanged_inode and untouched.read_bytes() == unchanged_bytes
        assert hashes(backup_root) == before
        assert not list(tmp_path.glob('.planbak-save-*'))
    finally:
        s.close()


def test_fallback_copy_is_marked_and_invalid_marker_protects_source(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    try:
        token = s.at('HomeDomain', 'Documents/a.txt').token
        s.stage_bytes(token, b'edited')
        before = hashes(backup_root)
        with pytest.raises(BackupError, match='marker'):
            s.save()
        assert hashes(backup_root) == before
        out = s.save(tmp_path / 'edited')
        assert s.editable and read_marker(out)
        (out / MARKER).write_text('{broken')
        s.stage_bytes(token, b'later')
        saved = hashes(out)
        with pytest.raises(BackupError, match='marker'):
            s.save()
        assert hashes(out) == saved and s.dirty
    finally:
        s.close()


def test_copy_allows_edits_but_prevents_concurrent_apply(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    ready, release = threading.Event(), threading.Event()
    def progress(*args):
        ready.set()
        assert release.wait(3)
    try:
        token = s.at('HomeDomain', 'Documents/a.txt').token
        with ThreadPoolExecutor(1) as pool:
            job = pool.submit(s.create_copy, tmp_path / 'copy', progress=progress)
            assert ready.wait(3)
            try:
                s.stage_bytes(token, b'during copy')
                with pytest.raises(BackupError, match='copy is being created'):
                    s.save(tmp_path / 'forbidden')
            finally:
                release.set()
            copied = job.result(timeout=3)
        s.use_copy(copied)
        assert s.dirty and s.cached_file(token).read_bytes() == b'during copy'
    finally:
        s.close()


def test_apply_gate_rejects_mutations_immediately(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    try:
        token = s.at('HomeDomain', 'Documents/a.txt').token
        s.stage_bytes(token, b'accepted')
        permit = s.begin_apply()
        with s.lock, ThreadPoolExecutor(1) as pool:
            job = pool.submit(s.stage_bytes, token, b'must not wait or queue')
            with pytest.raises(BackupError, match='being applied'):
                job.result(timeout=1)
            discard = pool.submit(s.discard)
            with pytest.raises(BackupError, match='being applied'):
                discard.result(timeout=1)
        for operation in (
            lambda: s.new_directory('HomeDomain', 'Documents', 'blocked'),
            lambda: s.delete([token]),
            lambda: s.move([token], 'HomeDomain', 'Documents', name='blocked'),
            lambda: s.discard(),
        ):
            with pytest.raises(BackupError, match='being applied'):
                operation()
        s.save(tmp_path / 'copy', _token=permit)
        s.stage_bytes(token, b'allowed again')
        assert s.dirty
    finally:
        s.close()


@pytest.mark.parametrize('field,value', [('version', 2), ('version', True), ('copy_id', 'bad'),
                                        ('created_utc', '2020-01-01'), ('application', 'other')])
def test_marker_validation(tmp_path, field, value):
    marker = write_marker(tmp_path)
    marker[field] = value
    (tmp_path / MARKER).write_text(json.dumps(marker))
    assert read_marker(tmp_path) is None


def test_cancel_copy_cleans_unpublished_output(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    try:
        with pytest.raises(Cancelled):
            s.create_copy(tmp_path / 'cancelled', cancel=lambda: True)
        assert not (tmp_path / 'cancelled').exists()
        assert not list(tmp_path.glob('.planbak-save-*'))
        s.stage_bytes(s.at('HomeDomain', 'Documents/a.txt').token, b'ok')
    finally:
        s.close()


def test_ui_copy_is_background_and_keeps_pending(window, qtbot, tmp_path, monkeypatch):
    from planbak.app import QFileDialog
    w = window
    w.poll_timer.stop()
    original = w.session.create_copy
    ready, release = threading.Event(), threading.Event()
    target = tmp_path / 'ui-copy'
    def slow(*args, progress=None, **kwargs):
        def notify(*values):
            progress(*values)
            ready.set()
            assert release.wait(3)
        return original(*args, progress=notify, **kwargs)
    monkeypatch.setattr(w.session, 'create_copy', slow)
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (str(target), ''))
    w.create_backup_copy()
    qtbot.waitUntil(ready.is_set)
    try:
        assert w.task is None and w.stack.isEnabled() and w.progress_dialog is None
        assert w.work_progress.isVisible() and '创建备份副本' in w.work_label.text()
        token = w.session.at('HomeDomain', 'Documents/a.txt').token
        w.session.stage_bytes(token, b'while copying')
        w.refresh()
        assert w.work_progress.isVisible()
    finally:
        release.set()
    qtbot.waitUntil(lambda: w.copy_task is None and w.task is None)
    assert w.session.root == target and w.session.dirty
    assert w.session.cached_file(token).read_bytes() == b'while copying'
    assert w.work_label.text() == '就绪' and not w.work_progress.isVisible()
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: pytest.fail('direct save asked for location'))
    monkeypatch.setattr('planbak.app.message', lambda *a, **k: None)
    w.save()
    assert w.progress_dialog is not None
    with pytest.raises(BackupError, match='being applied'):
        w.session.stage_bytes(token, b'late')
    qtbot.waitUntil(lambda: w.task is None)
    assert not w.session.dirty and w.session.root == target


def test_startup_auto_cleans_stale_cache_without_question(window, qtbot, tmp_path, monkeypatch):
    dead = tmp_path / 'dead-cache'
    dead.mkdir()
    (dead / 'secret').write_bytes(b'private')
    monkeypatch.setattr('planbak.app.stale_workspaces', lambda: [dead] if dead.exists() else [])
    monkeypatch.setattr('planbak.app.QMessageBox.question', lambda *a: pytest.fail('cleanup asked permission'))
    called = []
    window.offer_temp_cleanup(lambda: called.append(True))
    qtbot.waitUntil(lambda: window.task is None)
    assert called == [True] and not dead.exists()
    assert window.work_label.text() == '就绪'


def test_read_backup_uses_status_bar(window, qtbot, tmp_path):
    from conftest import make_backup
    target = make_backup(tmp_path / 'next-backup')
    window.open_path(target)
    assert window.progress_dialog is None and window.work_progress.isVisible()
    qtbot.waitUntil(lambda: window.task is None)
    assert window.session.root == target
    assert window.work_label.text() == '就绪'


def test_stale_scan_excludes_live_and_unmarked_sessions(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr('planbak.backup.tempfile.gettempdir', lambda: str(tmp_path))
    marked = {}
    for name, pid in [('dead', 123456789), ('live', os.getpid())]:
        path = tmp_path / ('planbak-' + name)
        path.mkdir()
        (path / '.planbak-session.json').write_text(json.dumps({'application': 'PlanBak', 'pid': pid}))
        marked[name] = path
    (tmp_path / 'planbak-unmarked').mkdir()
    monkeypatch.setattr('planbak.backup.process_alive', lambda pid: pid == os.getpid())
    assert stale_workspaces() == [marked['dead']]


def test_ui_exit_cancels_background_copy_and_waits(window, qtbot, tmp_path, monkeypatch):
    import time

    from planbak.app import QFileDialog
    ready = threading.Event()
    target = tmp_path / 'cancelled-ui-copy'
    original = window.session.create_copy
    def slow(*args, progress=None, cancel=None, **kwargs):
        def notify(*values):
            progress(*values)
            ready.set()
            while not cancel():
                time.sleep(0.002)
        return original(*args, progress=notify, cancel=cancel, **kwargs)
    monkeypatch.setattr(window.session, 'create_copy', slow)
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (str(target), ''))
    root = window.session.workspace
    window.create_backup_copy()
    qtbot.waitUntil(ready.is_set)
    window.close()
    qtbot.waitUntil(lambda: not window.isVisible())
    assert window.copy_task is None and not root.exists() and not target.exists()
    assert not list(tmp_path.glob('.planbak-save-*'))


def test_cancel_direct_save_before_install_keeps_copy_and_pending(backup_root, tmp_path):
    s = BackupSession(backup_root, PASSWORD)
    try:
        copied = s.create_copy(tmp_path / 'copy')
        s.use_copy(copied)
        before = hashes(s.root)
        token = s.at('HomeDomain', 'Documents/a.txt').token
        s.stage_bytes(token, b'pending')
        with pytest.raises(Cancelled):
            s.save(cancel=lambda: True)
        assert hashes(s.root) == before and s.dirty
        s.stage_bytes(token, b'still editable')
    finally:
        s.close()


def test_copy_completion_waits_for_foreground_task(window, qtbot, tmp_path, monkeypatch):
    from planbak.app import QFileDialog
    copy_ready, foreground_ready, release_copy, release_task = [threading.Event() for _ in range(4)]
    original = window.session.create_copy
    def slow(*args, progress=None, **kwargs):
        def notify(*values):
            progress(*values)
            copy_ready.set()
            assert release_copy.wait(3)
        return original(*args, progress=notify, **kwargs)
    target = tmp_path / 'deferred-copy'
    source = window.session.root
    monkeypatch.setattr(window.session, 'create_copy', slow)
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (str(target), ''))
    window.create_backup_copy()
    qtbot.waitUntil(copy_ready.is_set)
    def foreground(progress, cancel):
        foreground_ready.set()
        assert release_task.wait(3)
    window.run_task('读取文件', foreground, silent=True)
    qtbot.waitUntil(foreground_ready.is_set)
    release_copy.set()
    qtbot.waitUntil(lambda: window.copy_task is None)
    assert window._copy_pending is not None and window.session.root == source
    release_task.set()
    qtbot.waitUntil(lambda: window.task is None and window.session.root == target)


def test_direct_install_failure_has_no_recovery_and_does_not_touch_original(backup_root, tmp_path, monkeypatch):
    import os
    s = BackupSession(backup_root, PASSWORD)
    original_hashes = hashes(backup_root)
    try:
        copied = s.create_copy(tmp_path / 'copy')
        s.use_copy(copied)
        token = s.at('HomeDomain', 'Documents/a.txt').token
        s.stage_bytes(token, b'new bytes')
        original_replace = os.replace
        def fail_manifest(source, target):
            if target == s.root / 'Manifest.db':
                raise OSError('simulated install failure')
            return original_replace(source, target)
        monkeypatch.setattr('planbak.backup.os.replace', fail_manifest)
        with pytest.raises(BackupError, match='partially written'):
            s.save()
        assert s.dirty and hashes(backup_root) == original_hashes
        assert not list(s.root.glob('*journal*'))
        assert not list(tmp_path.glob('.planbak-save-*'))
        s.check_changes_allowed()
    finally:
        s.close()



def test_progress_phase_changes_are_not_throttled(qapp, monkeypatch):
    from planbak.app import Task
    from planbak.progress import ProgressMessage, ProgressPhase
    monkeypatch.setattr('planbak.app.time.monotonic', lambda: 100)
    def work(progress, cancel):
        progress(ProgressMessage(ProgressPhase.READ), 0, 0)
        progress(ProgressMessage(ProgressPhase.WRITE, name='a'), 1, 3)
        progress(ProgressMessage(ProgressPhase.WRITE, name='b'), 2, 3)
        progress(ProgressMessage(ProgressPhase.APPLY), 0, 0)
    task = Task(work)
    messages = []
    task.progress.connect(lambda text, *args: messages.append(text))
    task.run()
    assert [message.phase for message in messages] == [ProgressPhase.READ, ProgressPhase.WRITE, ProgressPhase.APPLY]


@pytest.mark.parametrize('width', [1160, 1788])
def test_status_keeps_backup_summary_and_places_work_near_three_quarters(window, qtbot, width):
    from PySide6.QtCore import QPoint

    from planbak.progress import ProgressMessage, ProgressPhase
    w = window
    w.resize(width, 760)
    qtbot.waitUntil(lambda: w.width() == width)
    assert w.backup_status.isVisible()
    assert '原始备份' in w.backup_status.text() and '个项目' in w.backup_status.text()
    assert '未加密' in w.backup_status.text()
    job = object()
    w._status_jobs[job] = (ProgressMessage(ProgressPhase.COPY_BACKUP), 1, 2)
    w.update_work_status()
    qtbot.waitUntil(lambda: w.work_progress.isVisible())
    qtbot.waitUntil(lambda: abs(w.work_panel.x() - w.status_panel.width() * 0.75) <= 1)
    line = w.work_panel.mapTo(w, QPoint()).x()
    assert w.work_progress.mapTo(w, QPoint()).x() == line
    label_right = w.work_label.mapTo(w, QPoint(w.work_label.width(), 0)).x()
    assert line - label_right == 8
    assert w.summary_panel.width() == w.work_panel.x()
    initial_line = line
    w._status_jobs[job] = (ProgressMessage(ProgressPhase.READ), 1, 2)
    w.update_work_status()
    qtbot.waitUntil(lambda: w.work_progress.mapTo(w, QPoint()).x() == initial_line)
    assert w.work_label.mapTo(w, QPoint(w.work_label.width(), 0)).x() == initial_line - 8
    w._status_jobs[job] = (ProgressMessage(ProgressPhase.COPY_BACKUP), 1, 2)
    w.update_work_status()
    before = w.backup_status.text()
    w.session.new_directory('HomeDomain', '', 'status-update')
    w.refresh()
    assert w.backup_status.text() != before
    assert '1 个已更改项目' in w.backup_status.text()
    assert w.work_label.text() == '正在创建备份副本…' and w.work_progress.value() == 500
    w._status_jobs.pop(job)
    w.update_work_status()
    assert w.work_label.text() == '就绪' and not w.work_progress.isVisible()
    assert '1 个已更改项目' in w.backup_status.text()


def test_prefetch_priority_is_explicit_and_localized(window):
    from planbak.i18n import ENGLISH, SIMPLIFIED_CHINESE
    from planbak.progress import ProgressMessage, ProgressPhase

    w = window
    w.language_context.set_language(SIMPLIFIED_CHINESE)
    prefetch, foreground = object(), object()
    w.track_status_job(prefetch, None, connect_progress=False, prefetch_name="image.heic")
    assert w.work_label.text() == "预加载文件：image.heic"
    assert not w.work_progress.isVisible()
    w.track_status_job(foreground, ProgressMessage(ProgressPhase.LOAD_IMAGE, name="预加载文件：ordinary"), connect_progress=False)
    assert w.work_label.text() == "正在加载文件：预加载文件：ordinary"
    assert w.work_progress.isVisible()
    w.untrack_status_job(foreground)
    w.language_context.set_language(ENGLISH)
    assert w.work_label.text() == "Preloading file: image.heic"
    assert not w.work_progress.isVisible()
    w.untrack_status_job(prefetch)
    assert prefetch not in w._prefetch_jobs


def test_add_only_save_preserves_all_existing_payloads(backup_root, tmp_path):
    import hashlib
    import plistlib
    s = BackupSession(backup_root, PASSWORD)
    try:
        copied = s.create_copy(tmp_path / 'copy')
        s.use_copy(copied)
        before = {p.relative_to(copied.path): hashlib.sha256(p.read_bytes()).digest()
                  for p in copied.path.rglob('*') if p.is_file() and len(p.name) == 40}
        count_before = sum(p.is_file() for p in copied.path.rglob('*'))
        imports = tmp_path / 'imports'
        imports.mkdir()
        added = []
        for n in range(15):
            path = imports / f'new-{n}.plist'
            path.write_bytes(plistlib.dumps({'index': n}))
            added.append(path)
        s.import_paths(added, 'HomeDomain', 'Documents')
        s.save()
        assert sum(p.is_file() for p in copied.path.rglob('*')) == count_before + 15
        for rel, fingerprint in before.items():
            assert hashlib.sha256((copied.path / rel).read_bytes()).digest() == fingerprint
        # 同名替换不会把其他已保存项目当作删除项，也不应继续增加文件数。
        for path in added[:2]:
            path.write_bytes(plistlib.dumps({'replacement': True}))
        s.import_paths(added[:2], 'HomeDomain', 'Documents', collision='replace')
        s.save()
        assert sum(p.is_file() for p in copied.path.rglob('*')) == count_before + 15
        reopened = BackupSession(copied.path, PASSWORD)
        try:
            for n in range(15):
                entry = reopened.at('HomeDomain', f'Documents/new-{n}.plist')
                data = reopened.cached_file(entry.token).read_bytes()
                assert plistlib.loads(data) == ({'replacement': True} if n < 2 else {'index': n})
            for (fid,) in reopened.db.execute('SELECT fileID FROM Files WHERE flags=1'):
                assert reopened.payload_path(fid).is_file()
        finally:
            reopened.close()
    finally:
        s.close()


@pytest.mark.parametrize('direct', [False, True], ids=['new-copy', 'direct'])
def test_saved_mbfile_digest_matches_stored_payload(backup_root, tmp_path, direct):
    import hashlib
    import plistlib
    s = BackupSession(backup_root, PASSWORD)
    try:
        if direct:
            s.use_copy(s.create_copy(tmp_path / 'existing-copy'))
        token = s.at('HomeDomain', 'Documents/a.txt').token
        s.stage_bytes(token, b'changed content for restore verification')
        imported = tmp_path / 'new.plist'
        imported.write_bytes(plistlib.dumps({'new': 'restore check'}))
        s.import_paths([imported], 'HomeDomain', 'Documents')
        output = s.save(None if direct else tmp_path / 'new-copy')
        # 直接检查归档引用与磁盘字节，不使用保存端的digest/Metadata.get。
        for path, plain in [('Documents/a.txt', b'changed content for restore verification'),
                            ('Documents/new.plist', imported.read_bytes())]:
            fid, blob = s.db.execute('SELECT fileID,file FROM Files WHERE domain=? AND relativePath=?',
                                    ('HomeDomain', path)).fetchone()
            archive = plistlib.loads(blob)
            record = archive['$objects'][archive['$top']['root'].data]
            archived_digest = record['Digest']
            if isinstance(archived_digest, plistlib.UID):
                archived_digest = archive['$objects'][archived_digest.data]
            if isinstance(archived_digest, dict):
                archived_digest = archived_digest['NS.data']
            stored = (output / fid[:2] / fid).read_bytes()
            assert archived_digest == hashlib.sha1(stored).digest()
            if s.encrypted:
                assert archived_digest != hashlib.sha1(plain).digest()
            assert s.cached_file(s.at('HomeDomain', path).token).read_bytes() == plain
        reopened = BackupSession(output, PASSWORD)
        try:
            assert reopened.cached_file(reopened.at('HomeDomain', 'Documents/a.txt').token).read_bytes() == b'changed content for restore verification'
            assert reopened.cached_file(reopened.at('HomeDomain', 'Documents/new.plist').token).read_bytes() == imported.read_bytes()
        finally:
            reopened.close()
    finally:
        s.close()


def test_previous_backup_roots_remain_protected_after_switch(backup_root, tmp_path):
    from dataclasses import replace

    session = BackupSession(backup_root, PASSWORD)
    before = hashes(backup_root)
    try:
        copied = session.create_copy(tmp_path / "editable")
        session.use_copy(copied)
        token = session.at("HomeDomain", "Documents/a.txt").token
        session.stage_bytes(token, b"changed")
        with pytest.raises(BackupError, match="overlap a protected backup"):
            session.create_copy(backup_root / "nested-copy")
        with pytest.raises(BackupError, match="overlap a protected backup"):
            session.save(backup_root / "nested-save")
        alias = tmp_path / "source-alias"
        try:
            alias.symlink_to(backup_root, target_is_directory=True)
        except OSError:
            pass  # Creating symlinks may require special permissions on Windows.
        else:
            with pytest.raises(BackupError, match="overlap a protected backup"):
                session.create_copy(alias / "nested-alias")
        with pytest.raises(BackupError, match="overlap a protected backup"):
            session.use_copy(replace(copied, source=session.root, path=backup_root))
        assert session.root == copied.path and session.dirty
        assert hashes(backup_root) == before
        assert not (backup_root / "nested-copy").exists()
        assert not (backup_root / "nested-save").exists()
    finally:
        session.close()


def test_close_rejects_active_apply(backup_root):
    session = BackupSession(backup_root, PASSWORD)
    token = session.begin_apply()
    try:
        with pytest.raises(BackupError, match="cannot close yet"):
            session.close()
        assert session.workspace.exists() and session.db is not None
    finally:
        session.end_apply(token)
        session.close()
    assert not session.workspace.exists()


def test_close_rejects_active_copy(backup_root, tmp_path, monkeypatch):
    import planbak.backup as module

    session = BackupSession(backup_root, PASSWORD)
    started, release = threading.Event(), threading.Event()
    original = module.copy_backup

    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "copy_backup", blocked)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(session.create_copy, tmp_path / "copy")
            assert started.wait(5)
            try:
                with pytest.raises(BackupError, match="cannot close yet"):
                    session.close()
                assert session.workspace.exists() and session.db is not None
            finally:
                release.set()
            assert future.result(timeout=10).path.is_dir()
    finally:
        session.close()
    assert not session.workspace.exists()


def test_close_gate_blocks_new_operations_before_cleanup(backup_root, tmp_path):
    import time

    session = BackupSession(backup_root, PASSWORD)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with session.lock:
            future = pool.submit(session.close)
            deadline = time.monotonic() + 5
            while not session._closing and time.monotonic() < deadline:
                time.sleep(0.001)
            assert session._closing
            # A second close while holding the session lock must not deadlock
            # the first close, which is waiting for that lock.
            with pytest.raises(BackupError, match="session is closing"):
                session.close()
            for operation in (
                session.begin_apply,
                session.begin_transfer,
                lambda: session.create_copy(tmp_path / "copy"),
                lambda: session.new_directory("HomeDomain", "Documents", "new"),
            ):
                with pytest.raises(BackupError, match="session is closing"):
                    operation()
        future.result(timeout=10)
    session.close()  # Closing an already closed session remains harmless.
    assert not session.workspace.exists()
