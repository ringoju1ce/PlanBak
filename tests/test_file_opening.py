import plistlib
import struct

import pytest
from PIL import Image
from test_ui import choose, open_test_plist
from test_ui import window as window

from planbak.file_opening import identify_file, xml_root
from planbak.navigation import Location


@pytest.mark.parametrize('data,expected', [
    (plistlib.dumps({'a': True}), 'plist'),
    (plistlib.dumps({'a': 42}, fmt=plistlib.FMT_BINARY), 'plist'),
    ('<?xml version="1.0" encoding="UTF-16"?><plist><dict/></plist>'.encode('utf-16'), 'plist'),
    (b'<!-- hi --><plist><dict/></plist>', 'plist'),
    (b'<?xml version="1.0"?><unrelated/>', 'external'),
    (b'SQLite format 3\0' + b'\0' * 84, 'sqlite'),
    (b'plain text', 'external'),
    (struct.pack('>I',24)+b'ftypheic'+b'\0'*4+b'mif1heic', 'image'),
    (struct.pack('>I',24)+b'ftypmp42'+b'\0'*4+b'isommp42', 'external'),
])
def test_header_only(tmp_path, data, expected):
    path = tmp_path / 'misleading.plist'
    path.write_bytes(data)
    assert identify_file(path) == expected


def test_xml_entity_declaration_rejected():
    assert xml_root(b'<!DOCTYPE plist [<!ENTITY a "x">]><plist/>') is None


def test_misnamed_plist_and_explicit_external(window, qtbot, tmp_path, monkeypatch):
    w = window
    w.poll_timer.stop()
    path = tmp_path / 'preferences.png'
    path.write_bytes(plistlib.dumps({'test': True}))
    w.session.import_paths([path], 'HomeDomain', 'Documents')
    w.navigate(Location('HomeDomain', 'Documents'))
    choose(w, path.name)
    calls = []
    monkeypatch.setattr('planbak.app.QDesktopServices.openUrl', lambda u: calls.append(u) or True)
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.plist_editors))
    token = next(iter(w.plist_editors))
    assert not w.image_viewers and not calls
    assert not list((w.session.workspace / 'opened').glob('*'))
    w.open_file(external=True)
    qtbot.waitUntil(lambda: w.task is None)
    assert calls and token in w.external_edits.records and token not in w.plist_editors
    assert w.external_edits.records[token].path.read_bytes() == path.read_bytes()


def test_explicit_external_respects_unsaved_cancel(window, qtbot, monkeypatch):
    from planbak.dialogs import FixedMessageBox
    record = open_test_plist(window, qtbot)
    record.editor.document.root.children[0].value = False
    record.editor.rebuild()
    assert record.editor.document.dirty
    monkeypatch.setattr(FixedMessageBox, 'exec', lambda *a: FixedMessageBox.StandardButton.Cancel)
    monkeypatch.setattr('planbak.app.QDesktopServices.openUrl',
                        lambda *a: pytest.fail('must not launch while editing is cancelled'))
    window.open_file(external=True)
    assert record.token in window.plist_editors and window.task is None


def test_misnamed_image_single_extract_and_reopen_cache(window, qtbot, tmp_path, monkeypatch):
    w = window
    w.poll_timer.stop()
    path = tmp_path / 'picture.plist'
    Image.new('RGB', (20, 12), 'red').save(path, format='PNG')
    w.session.import_paths([path], 'HomeDomain', 'Documents')
    w.navigate(Location('HomeDomain', 'Documents'))
    choose(w, path.name)
    extracted = []
    original = w.session.extract_file
    def extract(token, *a, **kw):
        extracted.append(token)
        return original(token, *a, **kw)
    monkeypatch.setattr(w.session, 'extract_file', extract)
    for i in range(2):
        w.open_file()
        qtbot.waitUntil(lambda: w.task is None and bool(w.image_viewers))
        v = w.image_viewers[0]
        qtbot.waitUntil(lambda: v.task is None)
        assert v.canvas.image.width() == 20 and len(extracted) == 0
        assert not w.plist_editors
        assert not list((w.session.workspace / 'opened').glob('*'))
        v.close()
        qtbot.waitUntil(lambda: not w.image_viewers)


def test_exit_drops_prepared_database_snapshot(window, qtbot, tmp_path, monkeypatch):
    import sqlite3
    import threading
    import time

    import planbak.app as module

    w = window
    w.poll_timer.stop()
    path = tmp_path / 'database.plist'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sample(value)')
    w.session.import_paths([path], 'HomeDomain', 'Documents')
    w.navigate(Location('HomeDomain', 'Documents'))
    choose(w, path.name)
    ready = threading.Event()
    snapshots = []
    original = module.prepare_snapshot
    def delayed(*args, **kwargs):
        snapshot = original(*args, **kwargs)
        snapshots.append(snapshot.directory)
        ready.set()
        while not kwargs['cancel']():
            time.sleep(0.002)
        return snapshot
    monkeypatch.setattr(module, 'prepare_snapshot', delayed)
    monkeypatch.setattr(w, 'confirm_leave', lambda continuation, **kw: continuation())
    root = w.session.workspace
    w.open_file()
    qtbot.waitUntil(ready.is_set)
    w.close()
    qtbot.waitUntil(lambda: not w.isVisible())
    assert not root.exists() and not w.database_viewers
    assert snapshots and not snapshots[0].exists()
