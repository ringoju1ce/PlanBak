"""SQLite and hexadecimal viewer lifecycle and snapshot management."""

import hashlib
import shutil
import uuid
import weakref
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .backup import digest
from .codec import CHUNK, BackupError, check_cancel
from .dialogs import FixedMessageBox as QMessageBox
from .hex_viewer import HexViewer
from .sqlite_database import DatabaseSnapshot, companions, prepare_snapshot
from .sqlite_viewer import SQLiteViewer


@dataclass
class OpenedDatabase:
    token: str
    viewer: SQLiteViewer
    snapshot: DatabaseSnapshot
    revision: tuple


@dataclass
class OpenedHex:
    token: str
    viewer: HexViewer
    path: Path
    source_digest: bytes
    observed_digest: bytes
    revision: tuple
    detached: bool = False


class ReadOnlyWindows:
    def __init__(self, owner):
        self.owner = owner
        self.database_viewers = {}
        self.hex_viewers = {}

    def viewers(self):
        return [
            record.viewer
            for record in (*self.database_viewers.values(), *self.hex_viewers.values())
        ]

    def has_open(self):
        return bool(self.database_viewers or self.hex_viewers)

    def request_close_all(self, on_finished):
        for viewer in self.viewers():
            viewer._skip_prompt = True
            viewer.finished.connect(on_finished)
            viewer.reject()

    def reuse_database(self, token):
        record = self.database_viewers.get(token)
        if not record:
            return False
        viewer = record.viewer
        viewer.showNormal()
        viewer.raise_()
        viewer.activateWindow()
        viewer.table.setFocus()
        return True

    def database_revision(self, token):
        return tuple(
            (entry.token, self.owner.plist_revision(entry.token))
            for entry in companions(self.owner.session, token)
        )

    def show_database(self, token, snapshot):
        if (
            self.owner._transitioning
            or self.owner._outgoing_transfers
            or self.owner._readonly_closing
        ):
            shutil.rmtree(snapshot.directory, ignore_errors=True)
            return
        session = self.owner.session
        viewer = SQLiteViewer(snapshot, session.get(token).name, self.owner)
        record = OpenedDatabase(token, viewer, snapshot, self.database_revision(token))
        self.database_viewers[token] = record
        viewer.reload_requested.connect(lambda: self.reload_database(record))

        def closed(_):
            self.database_viewers.pop(token, None)
            shutil.rmtree(record.snapshot.directory, ignore_errors=True)
            if not viewer._skip_prompt:
                self.owner.restore_browser_after_editor(session, viewer)
            viewer.deleteLater()

        viewer.finished.connect(closed)
        self.owner.panes.activate(self.owner.active_view(), focus=True)
        viewer.show()

    def open_hex(self):
        if (
            self.owner._transitioning
            or self.owner._outgoing_transfers
            or self.owner._readonly_closing
            or self.owner.task
            or not self.owner.session
            or self.owner.sidebar_active()
        ):
            return
        rows = self.owner.selected_rows()
        if len(rows) != 1 or rows[0].kind != "file":
            return
        token = rows[0].entry.token
        if token in self.hex_viewers:
            viewer = self.hex_viewers[token].viewer
            viewer.showNormal()
            viewer.raise_()
            viewer.activateWindow()
            viewer.table.setFocus()
            return
        session = self.owner.session
        path = session.workspace / "hex-viewer" / uuid.uuid4().hex / "snapshot.bin"

        def prepare(progress, cancel):
            session.extract_file(token, path, cancel=cancel)
            return digest(path, cancel=cancel)

        def opened(fingerprint):
            viewer = HexViewer(path, session.get(token).name, self.owner)
            record = OpenedHex(
                token, viewer, path, fingerprint, fingerprint, self.owner.plist_revision(token)
            )
            self.hex_viewers[token] = record
            viewer.reload_requested.connect(lambda: self.reload_hex(record))

            def closed(_):
                self.hex_viewers.pop(token, None)
                shutil.rmtree(record.path.parent, ignore_errors=True)
                if not viewer._skip_prompt:
                    self.owner.restore_browser_after_editor(session, viewer)
                viewer.deleteLater()

            viewer.finished.connect(closed)
            self.owner.panes.activate(self.owner.active_view(), focus=True)
            viewer.show()

        def cleanup():
            if token not in self.hex_viewers:
                shutil.rmtree(path.parent, ignore_errors=True)

        self.owner.run_task("打开文件", prepare, opened, on_done=cleanup)

    def reload_hex(self, record, *, automatic=False):
        if record.detached:
            return
        if (
            self.owner.task
            or not self.owner.session
            or record.viewer.task
            or record.viewer._closing
        ):
            return
        session = self.owner.session
        try:
            revision = self.owner.plist_revision(record.token)
        except BackupError:
            record.viewer.set_source_state(available=False)
            return
        path = session.workspace / "hex-viewer" / uuid.uuid4().hex / "snapshot.bin"

        def prepare(progress, cancel):
            session.extract_file(record.token, path, cancel=cancel)
            return digest(path, cancel=cancel)

        def loaded(fingerprint):
            if self.owner._focus_owned_modal(focus=False):
                return
            if self.hex_viewers.get(record.token) is not record:
                return
            record.revision = revision
            changed = fingerprint != record.source_digest
            prompt = changed and fingerprint != record.observed_digest
            record.observed_digest = fingerprint
            record.viewer.set_source_state(stale=changed)
            accept = not automatic
            if automatic and prompt:
                accept = (
                    QMessageBox.question(
                        record.viewer,
                        self.owner.language_context.translate("Source File Changed"),
                        self.owner.language_context.translate("Reload the updated version?"),
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    == QMessageBox.StandardButton.Yes
                )
            if accept and not record.viewer.task and not record.viewer._closing:
                previous = record.path
                record.path, record.source_digest = path, fingerprint
                record.viewer.replace_snapshot(path, session.get(record.token).name)
                shutil.rmtree(previous.parent, ignore_errors=True)

        def failed(exc):
            record.revision = revision
            if self.hex_viewers.get(record.token) is record:
                record.viewer.set_source_state(stale=True)
                record.viewer.set_status("Unable to reload file: {error}", error=exc)

        def cleanup():
            if self.hex_viewers.get(record.token) is not record or record.path != path:
                shutil.rmtree(path.parent, ignore_errors=True)

        self.owner.run_task(
            "重新加载文件", prepare, loaded, silent=automatic, on_error=failed, on_done=cleanup
        )

    def check_hex_viewers(self):
        if (
            self.owner._outgoing_transfers
            or self.owner._transitioning
            or self.owner._readonly_closing
        ):
            return False
        if self.owner._focus_owned_modal(focus=False):
            return False
        for record in list(self.hex_viewers.values()):
            if record.detached or record.viewer.task or record.viewer._closing:
                continue
            try:
                entry = self.owner.session.get(record.token)
                revision = self.owner.plist_revision(record.token)
            except BackupError:
                record.viewer.set_source_state(available=False)
                continue
            if record.viewer.name != entry.name:
                record.viewer.name = entry.name
                record.viewer.update_title()
            if not record.viewer.available:
                record.viewer.set_source_state(stale=record.viewer.stale)
            if revision != record.revision:
                self.reload_hex(record, automatic=True)
                return True
        return False

    def open_data_hex(self, data, identity, label, *, owner=None):
        if (
            self.owner._transitioning
            or self.owner._outgoing_transfers
            or self.owner._readonly_closing
            or self.owner.task
            or not self.owner.session
        ):
            self.owner.statusBar().setToolTip(
                self.owner.language_context.translate(
                    "A backup operation is in progress; try viewing Data later."
                )
            )
            return
        session = self.owner.session
        path = session.workspace / "hex-viewer" / uuid.uuid4().hex / "data.bin"
        retained = False
        owner_ref = weakref.ref(owner) if owner is not None else lambda: None

        def prepare(progress, cancel):
            path.parent.mkdir(parents=True, exist_ok=True)
            fingerprint = hashlib.sha256()
            with path.open("wb") as stream:
                view = memoryview(data)
                for offset in range(0, len(view), CHUNK):
                    check_cancel(cancel)
                    block = view[offset : offset + CHUNK]
                    fingerprint.update(block)
                    stream.write(block)
            check_cancel(cancel)
            return fingerprint.digest()

        def opened(fingerprint):
            nonlocal retained
            token = f"plist-data:{identity}:{fingerprint.hex()}"
            if token in self.hex_viewers:
                viewer = self.hex_viewers[token].viewer
                viewer.name = label
                viewer.update_title()
                viewer.showNormal()
                viewer.raise_()
                viewer.activateWindow()
                viewer.table.setFocus()
                return
            viewer = HexViewer(path, label, self.owner)
            viewer.reload_button.defaultAction().setVisible(False)
            viewer.set_status()
            record = OpenedHex(token, viewer, path, fingerprint, fingerprint, (), detached=True)
            self.hex_viewers[token] = record
            retained = True

            def closed(_):
                self.hex_viewers.pop(token, None)
                shutil.rmtree(path.parent, ignore_errors=True)
                if not viewer._skip_prompt:
                    target = owner_ref()
                    if target is not None and any(
                        r.editor is target for r in self.owner.plist_editors.values()
                    ):

                        def restore():
                            if (
                                any(r.editor is target for r in self.owner.plist_editors.values())
                                and target.isVisible()
                                and not target.isMinimized()
                                and not self.owner._focus_owned_modal(focus=False)
                                and QApplication.activeWindow()
                                in (None, self.owner, viewer, target)
                            ):
                                target.activateWindow()
                                (
                                    target.tree
                                    if target.pages.currentIndex() == 0
                                    else target.xml.code
                                ).setFocus()

                        QTimer.singleShot(0, restore)
                    else:
                        self.owner.restore_browser_after_editor(session, viewer)
                viewer.deleteLater()

            viewer.finished.connect(closed)
            viewer.show()

        def cleanup():
            if not retained:
                shutil.rmtree(path.parent, ignore_errors=True)

        self.owner.run_task("打开项目", prepare, opened, on_done=cleanup)

    def reload_database(self, record, *, automatic=False):
        if (
            self.owner.task
            or not self.owner.session
            or record.viewer.task
            or record.viewer._closing
        ):
            return
        session = self.owner.session
        try:
            revision = self.database_revision(record.token)
        except BackupError:
            record.viewer.set_source_state(available=False)
            return
        directory = session.workspace / "sqlite-viewer" / uuid.uuid4().hex

        def loaded(snapshot):
            if self.owner._focus_owned_modal(focus=False):
                return
            if record.token not in self.database_viewers:
                shutil.rmtree(snapshot.directory, ignore_errors=True)
                return
            record.revision = revision
            changed = snapshot.fingerprint != record.snapshot.fingerprint
            record.viewer.set_source_state(stale=changed)
            accept = not automatic
            if automatic and changed:
                accept = (
                    QMessageBox.question(
                        record.viewer,
                        self.owner.language_context.translate("Database Changed"),
                        self.owner.language_context.translate("Reload the updated version?"),
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    == QMessageBox.StandardButton.Yes
                )
            if not accept or record.viewer.task or record.viewer._closing:
                shutil.rmtree(snapshot.directory, ignore_errors=True)
                return
            previous = record.snapshot
            record.snapshot = snapshot
            record.viewer.replace_snapshot(snapshot, session.get(record.token).name)
            shutil.rmtree(previous.directory, ignore_errors=True)

        def failed(exc):
            record.revision = revision
            if record.token in self.database_viewers:
                record.viewer.set_source_state(stale=True)
                record.viewer.set_status("Unable to reload database: {error}", error=exc)

        def cleanup():
            if record.snapshot.directory != directory:
                shutil.rmtree(directory, ignore_errors=True)

        self.owner.run_task(
            "重新加载文件",
            lambda progress, cancel: prepare_snapshot(
                session, record.token, directory, cancel=cancel
            ),
            loaded,
            silent=automatic,
            on_error=failed,
            on_done=cleanup,
        )

    def check_database_viewers(self):
        if (
            self.owner._outgoing_transfers
            or self.owner._transitioning
            or self.owner._readonly_closing
        ):
            return False
        if self.owner._focus_owned_modal(focus=False):
            return False
        for record in list(self.database_viewers.values()):
            if record.viewer.task or record.viewer._closing:
                continue
            try:
                entry = self.owner.session.get(record.token)
                revision = self.database_revision(record.token)
            except BackupError:
                record.viewer.set_source_state(available=False)
                continue
            if record.viewer.name != entry.name:
                record.viewer.name = entry.name
                record.viewer.update_title()
            if not record.viewer.available:
                record.viewer.set_source_state(stale=record.viewer.stale)
                record.viewer.set_status("The source file is available again.")
            if revision != record.revision:
                self.reload_database(record, automatic=True)
                return True
        return False
