"""对抗性缺陷修复的必要回归；原始审计脚本和失败证据仍保留在 docs/qa。"""
import json
import plistlib
import sqlite3

import pytest
from conftest import PASSWORD, archive, make_backup
from test_ui import window as window

from planbak.app import OpenedFile, QFileDialog, QMessageBox, Task
from planbak.backup import BackupSession, digest, stale_workspaces
from planbak.codec import BackupError, file_id
from planbak.plist_editor import PlistNode, ValueDialog


def tree_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_progress_accepts_backup_sizes_above_two_gib(qapp):
    seen = []
    total = 3 * 1024**3
    task = Task(lambda notify, cancel: notify('创建副本…', total, total))
    task.progress.connect(lambda text, done, size: seen.append((done, size)))
    task.run()
    print('progress error=', repr(task.error), 'delivered=', seen)
    assert task.error is None
    assert seen == [(total, total)]


@pytest.mark.parametrize('value', [[], None, 'marker', 1, True])
def test_malformed_session_marker_cannot_abort_startup(tmp_path, monkeypatch, value):
    folder = tmp_path / 'planbak-broken-marker'
    folder.mkdir()
    (folder / '.planbak-session.json').write_text(json.dumps(value))
    monkeypatch.setattr('planbak.backup.tempfile.gettempdir', lambda: str(tmp_path))
    assert stale_workspaces() == []


def test_export_cannot_merge_into_original_via_ancestor(tmp_path):
    root = make_backup(tmp_path / 'Documents')
    s = BackupSession(root)
    try:
        imported = tmp_path / 'Manifest.db'
        imported.write_bytes(b'QA synthetic data, not a database')
        s.import_paths([imported], 'HomeDomain', 'Documents')
        before = tree_bytes(root)
        folder = s.at('HomeDomain', 'Documents')
        try:
            s.export([folder.token], root.parent, collision='replace')
        except BackupError:
            pass
        after = tree_bytes(root)
        print('source changes=', sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)))
        assert bool(after == before), '只读导出覆盖了受保护原备份的 Manifest.db'
    finally:
        s.close()


@pytest.mark.parametrize('text', ['a\r\nb', 'a\rb', 'a\u2028b', 'a\u2029b'])
def test_string_edit_accept_without_changes_preserves_value(qtbot, text):
    node = PlistNode('String', 'key', text)
    d = ValueDialog(node, 'Dictionary', [node], True)
    qtbot.addWidget(d)
    d.accept()
    print('before=', repr(text), 'after=', repr(d.result_node.value))
    assert d.result_node.value == text


def test_save_with_near_limit_metadata_reopens(tmp_path):
    root = make_backup(tmp_path / 'metadata-source')
    fid = file_id('HomeDomain', 'Documents/a.txt')
    blob = plistlib.loads(archive(data=b'hello world'))
    # 未知字段保留是现有设计，构造在读取上限内的合法未知 archive 对象。
    blob['$objects'].append(b'x' * 130400)
    encoded = plistlib.dumps(blob, fmt=plistlib.FMT_BINARY, sort_keys=False)
    print('source metadata bytes=', len(encoded))
    assert len(encoded) <= 128 * 1024
    with sqlite3.connect(root / 'Manifest.db') as db:
        db.execute('UPDATE Files SET file=? WHERE fileID=?', (encoded, fid))
    s = BackupSession(root)
    try:
        token = s.at('HomeDomain', 'Documents/a.txt').token
        s.stage_bytes(token, b'edited')
        output = s.save(tmp_path / 'metadata-output')
        with sqlite3.connect(output / 'Manifest.db') as db:
            size = db.execute('SELECT length(file) FROM Files WHERE fileID=?', (fid,)).fetchone()[0]
        print('saved metadata bytes=', size)
        reopened = BackupSession(output)
        reopened.close()
    finally:
        s.close()


def test_noncanonical_fileid_does_not_break_live_session_after_save(tmp_path):
    root = make_backup(tmp_path / 'noncanonical-source')
    canonical = file_id('HomeDomain', 'Documents/a.txt')
    alternative = 'f' * 40
    (root / alternative[:2]).mkdir(exist_ok=True)
    (root / canonical[:2] / canonical).rename(root / alternative[:2] / alternative)
    with sqlite3.connect(root / 'Manifest.db') as db:
        db.execute('UPDATE Files SET fileID=? WHERE fileID=?', (alternative, canonical))
    try:
        s = BackupSession(root)
    except BackupError:
        return  # 显式拒绝不受支持的输入也是合理行为。
    try:
        token = s.at('HomeDomain', 'Documents/a.txt').token
        s.new_directory('HomeDomain', '', 'unrelated')
        output = s.save(tmp_path / 'noncanonical-output')
        reopened = BackupSession(output)
        try:
            reopened.extract_file(reopened.at('HomeDomain', 'Documents/a.txt').token, tmp_path / 'reopened')
            assert (tmp_path / 'reopened').read_bytes() == b'hello world'
        finally:
            reopened.close()
        s.extract_file(token, tmp_path / 'live')
        assert (tmp_path / 'live').read_bytes() == b'hello world'
    finally:
        s.close()


def test_external_reversion_to_baseline_during_switch(window, qtbot, monkeypatch, tmp_path):
    w = window
    w.poll_timer.stop()
    token = w.session.at('HomeDomain', 'Documents/a.txt').token
    w.session.stage_bytes(token, b'first change')
    path = w.session.workspace / 'external-edit.txt'
    path.write_bytes(b'first change')
    record = OpenedFile(token, path, digest(path), w.file_stamp(path))
    w.opened[token] = record
    path.write_bytes(b'hello world')
    record.unreviewed = True
    errors, continued = [], []
    monkeypatch.setattr(QMessageBox, 'exec', lambda self: QMessageBox.StandardButton.Save)
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (str(tmp_path / 'switch-copy'), ''))
    monkeypatch.setattr('planbak.app.message', lambda *a, **kw: errors.append(a))
    w.confirm_leave(lambda: continued.append(True))
    qtbot.waitUntil(lambda: w.task is None)
    print('errors=', errors, 'continued=', continued, 'dirty=', w.session.dirty)
    assert not errors
    assert continued == [True]


def test_async_readonly_close_does_not_lose_new_window_continuation(window, qtbot, monkeypatch):
    import threading

    from test_ui import choose, open_test_hex

    from planbak.codec import Cancelled

    w = window
    w.poll_timer.stop()
    first = open_test_hex(w, qtbot)
    started, release = threading.Event(), threading.Event()

    def delayed(*a, cancel, **kw):
        started.set()
        assert release.wait(5)
        raise Cancelled()

    monkeypatch.setattr('planbak.hex_viewer.search_bytes', delayed)
    first.viewer.search_input.setText('FF')
    first.viewer.find_next()
    qtbot.waitUntil(started.is_set)
    continued = []
    try:
        w.close_readonly_viewers(lambda: continued.append(True))
        choose(w, 'empty.bin')
        w.open_hex()
        qtbot.waitUntil(lambda: w.task is None)
        second = next((r for r in w.hex_viewers.values() if r is not first), None)
    finally:
        release.set()
    qtbot.waitUntil(lambda: first.viewer.task is None)
    if second:
        second.viewer.close()
    qtbot.waitUntil(lambda: not w.hex_viewers)
    qtbot.wait(20)
    print('continuation=', continued, 'closing flag=', w._readonly_closing)
    assert continued == [True] and not w._readonly_closing


def test_cancel_exit_does_not_orphan_late_reload_snapshot(window, qtbot, tmp_path, monkeypatch):
    import threading
    import time

    from test_ui import open_test_database

    import planbak.app as module

    w = window
    w.poll_timer.stop()
    record = open_test_database(w, qtbot, tmp_path)
    started = threading.Event()
    original = module.prepare_snapshot
    snapshots = []

    def late(*a, **kw):
        result = original(*a, **kw)
        snapshots.append(result.directory)
        started.set()
        deadline = time.monotonic() + 5
        while not kw['cancel']() and time.monotonic() < deadline:
            time.sleep(0.002)
        return result

    monkeypatch.setattr(module, 'prepare_snapshot', late)
    monkeypatch.setattr(QMessageBox, 'exec', lambda self: QMessageBox.StandardButton.Cancel)
    w.reload_database(record)
    qtbot.waitUntil(started.is_set)
    w.close()
    qtbot.waitUntil(lambda: w.task is None and not w.database_viewers)
    qtbot.wait(30)
    assert w.isVisible() and w.session is not None
    remaining = [str(p.relative_to(w.session.workspace)) for p in snapshots if p.exists()]
    print('orphan reload dirs=', remaining)
    assert not remaining


def test_sidebar_rename_replacing_current_folder_refreshes_view(window, qtbot, monkeypatch):
    from planbak.app import QInputDialog
    from planbak.navigation import Location

    w = window
    w.poll_timer.stop()
    source = w.session.new_directory('HomeDomain', 'Documents', 'other')
    w.session.new_directory('HomeDomain', 'Documents/other', 'unique-child')
    w.navigate(Location('HomeDomain', 'Documents/sub'))
    errors = []
    monkeypatch.setattr(QInputDialog, 'getText', lambda *a, **kw: ('sub', True))
    monkeypatch.setattr(w, 'collision_policy', lambda: 'replace')
    monkeypatch.setattr('planbak.app.message', lambda *a, **kw: errors.append(a))
    w.rename_entry(source, focus_browser=False)
    names = [r.name for r in w.model.rows if r.kind != 'parent']
    actual = [e.name for e in w.session.children('HomeDomain', 'Documents/sub')]
    print('rename errors=', errors, 'visible=', names, 'actual=', actual)
    assert not errors and sorted(names) == sorted(actual)


@pytest.mark.parametrize('operation', ['save','create_copy'])
def test_published_backup_never_disappears_with_session_cleanup(tmp_path, operation):
    s = BackupSession(make_backup(tmp_path/'source'))
    output = s.workspace/'user-chosen-output'
    rejected = False
    try:
        s.new_directory('HomeDomain','','qa')
        try:
            if operation == 'save':
                s.save(output)
            else:
                s.use_copy(s.create_copy(output))
        except BackupError:
            rejected = True
        if not rejected:
            assert (output/'Manifest.db').is_file()
    finally:
        s.close()
    print('operation=',operation,'rejected=',rejected,'published_output_exists_after_close=',output.exists())
    assert rejected or output.exists(), '报告创建成功的备份在关闭会话时被删除'


@pytest.mark.parametrize('encrypted', [False,True])
def test_renamed_implicit_directory_survives_save_when_last_child_deleted(tmp_path, encrypted):
    s=BackupSession(make_backup(tmp_path/'source',encrypted),PASSWORD)
    try:
        folder=s.at('HomeDomain','implicit')
        assert not folder.explicit
        s.move([folder.token],'HomeDomain','',name='kept-empty-folder')
        s.delete([s.at('HomeDomain','kept-empty-folder/deep.txt').token])
        assert s.at('HomeDomain','kept-empty-folder')
        output=s.save(tmp_path/'saved')
        reader=BackupSession(output,PASSWORD)
        try:
            assert reader.at('HomeDomain','kept-empty-folder'), '保存成功后用户明确重命名的空文件夹消失'
        finally:
            reader.close()
    finally:
        s.close()


def test_export_keeps_original_protected_after_switch_to_copy(tmp_path):
    root=make_backup(tmp_path/'Documents')
    s=BackupSession(root)
    try:
        s.use_copy(s.create_copy(tmp_path/'copy'))
        before=tree_bytes(root)
        with pytest.raises(BackupError):
            s.export([s.at('HomeDomain','Documents').token],tmp_path,collision='replace')
        assert bool(tree_bytes(root)==before)
    finally:
        s.close()


def test_output_cannot_enter_another_live_session_workspace(tmp_path):
    first=BackupSession(make_backup(tmp_path/'first'))
    second=BackupSession(make_backup(tmp_path/'second'))
    try:
        first.new_directory('HomeDomain','','pending')
        for operation in (first.save,first.create_copy):
            with pytest.raises(BackupError,match='临时'):
                operation(second.workspace/'output')
        assert not (second.workspace/'output').exists()
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize('depth',[16,24,100])
def test_plist_shared_graph_is_rejected_before_expansion(depth):
    from planbak.plist_editor import PlistDocument
    value=[]
    for _ in range(depth):
        value=[value,value]
    data=plistlib.dumps(value,fmt=plistlib.FMT_BINARY)
    assert len(data)<2048
    with pytest.raises(ValueError,match='节点'):
        PlistDocument(data)


def test_plist_cycles_rejected_and_small_shared_graph_edits_independent():
    from planbak.plist_editor import PlistDocument
    value=[]
    value.append(value)
    with pytest.raises(ValueError,match='循环'):
        PlistNode.from_value(value)
    shared=['before']
    document=PlistDocument(plistlib.dumps([shared,shared],fmt=plistlib.FMT_BINARY))
    document.root.children[0].children[0].value='after'
    assert plistlib.loads(document.serialize())==[['after'],['before']]


def test_plist_depth_limit_rejects_deep_nonshared_tree():
    from planbak.plist_editor import MAX_PLIST_DEPTH
    value='leaf'
    for _ in range(MAX_PLIST_DEPTH+1):
        value=[value]
    with pytest.raises(ValueError,match='层'):
        PlistNode.from_value(value)


def test_icon_thumbnails_and_lru_bound_all_cached_results(qapp,monkeypatch):
    import io
    from types import SimpleNamespace

    from PIL import Image

    import planbak.icons as module
    monkeypatch.setattr(module,'ICON_CACHE_ENTRIES',3)
    data=io.BytesIO()
    Image.new('RGBA',(1024,1024),'red').save(data,format='PNG')
    icons=module.ApplicationIcons()
    icons.set_session(SimpleNamespace(session_id='qa',info={'Applications':{str(i):{'PlaceholderIcon':data.getvalue()} for i in range(6)}}))
    first=icons.for_directory('AppDomain-0')
    assert max(s.width() for s in first.availableSizes())<=module.ICON_EDGE
    icons.for_directory('AppDomain-1')
    icons.for_directory('AppDomain-2')
    assert icons.for_directory('AppDomain-0') is first
    icons.for_directory('AppDomain-3')
    assert list(icons.cache)==['2','0','3']
    for i in range(10):
        icons.for_directory('AppDomain-missing-'+str(i))
    assert len(icons.cache)==3
    icons.set_session(None)
    assert not icons.cache


def test_oversize_icon_rejected_before_reading_raster(qapp,monkeypatch):
    import io
    from types import SimpleNamespace

    from PIL import Image

    import planbak.icons as module
    data=io.BytesIO()
    Image.new('RGB',(2200,2200),'red').save(data,format='PNG')
    icons=module.ApplicationIcons()
    icons.set_session(SimpleNamespace(session_id='qa',info={'Applications':{'too-large':{'PlaceholderIcon':data.getvalue()}}}))
    monkeypatch.setattr(module.QImageReader,'read',lambda self:pytest.fail('must not decode oversize header'))
    assert icons.for_directory('AppDomain-too-large') is None


@pytest.mark.parametrize('direct',[False,True])
def test_metadata_crossing_resource_budget_is_not_published(tmp_path,direct):
    import hashlib

    from planbak.codec import MAX_METADATA_BYTES
    root=make_backup(tmp_path/'source')
    fid=file_id('HomeDomain','Documents/a.txt')
    blob=plistlib.loads(archive(data=b'hello world'))
    blob['$objects'].append(b'x'*(MAX_METADATA_BYTES-650))
    encoded=plistlib.dumps(blob,fmt=plistlib.FMT_BINARY,sort_keys=False)
    assert MAX_METADATA_BYTES-150<len(encoded)<=MAX_METADATA_BYTES
    with sqlite3.connect(root/'Manifest.db') as db:
        db.execute('UPDATE Files SET file=? WHERE fileID=?',(encoded,fid))
    s=BackupSession(root)
    try:
        if direct:
            s.use_copy(s.create_copy(tmp_path/'copy'))
        before=hashlib.sha256((s.root/'Manifest.db').read_bytes()).digest()
        s.stage_bytes(s.at('HomeDomain','Documents/a.txt').token,b'edit')
        with pytest.raises(BackupError,match='元数据'):
            s.save(None if direct else tmp_path/'rejected-output')
        assert s.dirty
        assert hashlib.sha256((s.root/'Manifest.db').read_bytes()).digest()==before
        assert not (tmp_path/'rejected-output').exists()
        assert not list(tmp_path.glob('.planbak-save-*'))
    finally:
        s.close()


def test_metadata_object_count_rejected_before_plistlib_allocates(monkeypatch):
    import planbak.codec as module
    blob=bytearray(archive(data=b'qa'))
    blob[-24:-16]=(module.MAX_METADATA_OBJECTS+1).to_bytes(8,'big')
    monkeypatch.setattr(module.plistlib,'loads',lambda *a:pytest.fail('object table must be rejected before parsing'))
    with pytest.raises(BackupError,match='对象数量'):
        module.Metadata(bytes(blob))


def test_metadata_container_and_depth_budgets():
    import planbak.codec as module
    for value in [[None]*(module.MAX_METADATA_ITEMS+1),None]:
        if value is None:
            value=[]
            for _ in range(module.MAX_METADATA_DEPTH+1):
                value=[value]
        blob=plistlib.loads(archive(data=b'qa'))
        blob['$objects'].append(value)
        with pytest.raises(BackupError,match='元数据'):
            module.Metadata(plistlib.dumps(blob,fmt=plistlib.FMT_BINARY))
