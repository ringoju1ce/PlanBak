from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QModelIndex, QTimer, QUrl
from PySide6.QtWidgets import QApplication
from test_ui import window as window

from planbak.app import QMessageBox
from planbak.navigation import Location


def sources(tmp_path):
    paths = [tmp_path / name for name in ('a.txt', 'empty.bin', 'new.txt')]
    for i, path in enumerate(paths):
        path.write_bytes(f'replacement-{i}'.encode())
    return paths


def test_multiple_drop_two_existing_real_replace_dialog(window, qtbot, tmp_path):
    w = window
    w.poll_timer.stop()
    w.navigate(Location('HomeDomain', 'Documents'))
    paths = sources(tmp_path)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    clicked = []
    def choose_replace():
        box = QApplication.activeModalWidget()
        assert isinstance(box, QMessageBox)
        button = next(b for b in box.buttons() if b.text() == '替换')
        clicked.append(True)
        button.click()
    QTimer.singleShot(0, choose_replace)
    assert w.handle_drop(mime, QModelIndex())
    qtbot.waitUntil(lambda: w.task is None)
    assert clicked and len(w.session.changes) == 3
    for path in paths:
        entry = w.session.at('HomeDomain', f'Documents/{path.name}')
        assert Path(entry.staged).read_bytes() == path.read_bytes()
    assert w.isVisible()


@pytest.mark.parametrize("grid", [False, True])
def test_native_drop_returns_before_replace_prompt(window, qtbot, tmp_path, monkeypatch, grid):
    import shiboken6
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QDropEvent
    w = window
    w.poll_timer.stop()
    w.navigate(Location('HomeDomain', 'Documents'))
    paths = sources(tmp_path)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    w.set_view(grid)
    view = w.active_view()
    returned = []
    prompted = []
    def policy(**kwargs):
        assert returned, 'Modal collision prompt ran inside native drop handling'
        prompted.append(True)
        shiboken6.delete(mime)  # The native drag owner may release its MIME after delivery.
        return 'replace'
    monkeypatch.setattr(w, 'collision_policy', policy)
    event = QDropEvent(QPointF(5, view.viewport().height() - 5), Qt.DropAction.CopyAction,
                       mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view.dropEvent(event)
    returned.append(True)
    assert event.isAccepted()
    qtbot.waitUntil(lambda: bool(prompted) and w.task is None)
    for path in paths:
        entry = w.session.at('HomeDomain', f'Documents/{path.name}')
        assert Path(entry.staged).read_bytes() == path.read_bytes()


@pytest.mark.parametrize('policy', ['skip', 'keep_both', None])
def test_deferred_drop_other_collision_choices(window, qtbot, tmp_path, monkeypatch, policy):
    w = window
    w.poll_timer.stop()
    w.navigate(Location('HomeDomain', 'Documents'))
    paths = sources(tmp_path)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    choices = []
    def choose(**kwargs):
        choices.append(True)
        return policy
    monkeypatch.setattr(w, 'collision_policy', choose)
    assert w.queue_drop(mime, QModelIndex()) is not None
    assert not choices and w.task is None
    qtbot.waitUntil(lambda: bool(choices) and w.task is None)
    assert w.session.at('HomeDomain', 'Documents/a.txt').staged is None
    assert w.session.at('HomeDomain', 'Documents/empty.bin').staged is None
    if policy:
        assert w.session.at('HomeDomain', 'Documents/new.txt')
        assert len(w.session.changes) == (3 if policy == 'keep_both' else 1)
    else:
        assert not w.session.dirty


def test_internal_queue_uses_copied_tokens_and_original_target(window, qtbot, monkeypatch):
    import json

    import shiboken6

    from planbak.app import INTERNAL_MIME
    w = window
    w.poll_timer.stop()
    w.navigate(Location('HomeDomain', 'Documents'))
    token = w.session.at('HomeDomain', 'Documents/a.txt').token
    class InternalMime(QMimeData):
        def urls(self):
            pytest.fail('Internal move must not request lazy export URLs')
    mime = InternalMime()
    mime.setData(INTERNAL_MIME, json.dumps({'session': w.session.session_id, 'tokens': [token]}).encode())
    row = next(i for i, r in enumerate(w.model.rows) if r.name == 'sub')
    assert w.queue_drop(mime, w.model.index(row, 0)) is not None
    assert w.queue_drop(mime, QModelIndex()) is None
    shiboken6.delete(mime)
    w.navigate(Location('SystemPreferencesDomain'))
    qtbot.waitUntil(lambda: not w._drop_queued and w.task is None)
    assert w.session.at('HomeDomain', 'Documents/sub/a.txt').token == token
    assert w.session.at('HomeDomain', 'Documents/a.txt') is None


def test_queued_drop_is_discarded_after_session_change(window, qtbot, tmp_path):
    from conftest import make_backup

    from planbak.backup import BackupSession
    w = window
    w.poll_timer.stop()
    w.navigate(Location('HomeDomain', 'Documents'))
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in sources(tmp_path)])
    assert w.queue_drop(mime, QModelIndex()) is not None
    old = w.session
    w.session = BackupSession(make_backup(tmp_path / 'another-backup'))
    old.close()
    qtbot.waitUntil(lambda: not w._drop_queued)
    assert not w.session.dirty and w.task is None
