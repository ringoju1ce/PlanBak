"""Source-side leases for copying entries between backup windows."""

import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .codec import BackupError
from .progress import ProgressMessage, ProgressPhase


class TransferLease:
    def __init__(self, manager, session, token):
        self.manager = manager
        self.session = session
        self.token = token
        self.started = False
        self.released = False
        self._status_visible = False

    def show_status(self):
        if not self.released and not self._status_visible:
            self._status_visible = True
            self.manager.status_started.emit(self.token)
            self.manager.changed.emit()

    def copy_to(self, destination, tokens, domain, parent, policy, progress, cancel):
        def copy_progress(_text, done, total):
            progress(ProgressMessage(ProgressPhase.COPY_TO), done, total)

        with tempfile.TemporaryDirectory(prefix=".transfer-", dir=destination.workspace) as folder:
            paths = self.session.export(tokens, Path(folder), progress=copy_progress, cancel=cancel)
            destination.import_paths(
                paths, domain, parent, collision=policy, progress=copy_progress, cancel=cancel
            )

    def release(self):
        if self.released:
            return
        self.released = True
        self.session.end_transfer(self.token)
        self.manager._released(self)


class OutgoingTransfers(QObject):
    changed = Signal()
    status_started = Signal(object)
    status_finished = Signal(object)
    close_ready = Signal()

    def __init__(self, owner, get_session, can_start):
        super().__init__(owner)
        self._get_session = get_session
        self._can_start = can_start
        self._leases = set()
        self._close_pending = False

    @property
    def active(self):
        return len(self._leases)

    def acquire(self):
        session = self._get_session()
        if session is None or not self._can_start():
            raise BackupError(
                english="The source backup is busy; try copying again later",
            )
        token = session.begin_transfer()
        lease = TransferLease(self, session, token)
        self._leases.add(lease)
        return lease

    def defer_close(self):
        self._close_pending = True

    def _released(self, lease):
        self._leases.discard(lease)
        if lease._status_visible:
            self.status_finished.emit(lease.token)
        self.changed.emit()
        if not self._leases and self._close_pending:
            self._close_pending = False
            self.close_ready.emit()
