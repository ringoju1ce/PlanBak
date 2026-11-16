"""Track temporary copies opened in external applications."""

import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, Qt, QUrl
from PySide6.QtGui import QDesktopServices

from .backup import digest
from .codec import BackupError
from .dialogs import FixedMessageBox as QMessageBox
from .progress import ProgressPhase


def file_stamp(path):
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)
    except FileNotFoundError:
        return (None,)


@dataclass
class OpenedFile:
    token: str
    path: Path
    fingerprint: bytes
    stamp: tuple
    seen: tuple | None = None
    stable: int = 0
    later_until: float = 0
    unreviewed: bool = False


class ExternalEdits:
    def __init__(self, owner):
        self.owner = owner
        self.records = {}
        self.watcher = QFileSystemWatcher(owner)
        self.watcher.fileChanged.connect(self._file_event)
        self.watcher.directoryChanged.connect(self._directory_event)

    def register(self, token, path, fingerprint):
        self.records[token] = OpenedFile(token, path, fingerprint, file_stamp(path))
        self.watcher.addPath(str(path))
        self.watcher.addPath(str(path.parent))

    def reopen(self, token):
        record = self.records.get(token)
        if record and record.path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.path)))
            return True
        return False

    def clear(self):
        paths = self.watcher.files() + self.watcher.directories()
        if paths:
            self.watcher.removePaths(paths)
        self.records.clear()

    def forget(self, records):
        for record in records:
            self.records.pop(record.token, None)
            if str(record.path) in self.watcher.files():
                self.watcher.removePath(str(record.path))
            if not any(r.path.parent == record.path.parent for r in self.records.values()):
                if str(record.path.parent) in self.watcher.directories():
                    self.watcher.removePath(str(record.path.parent))

    def changed_records(self, session, tokens=None):
        result = []
        for token, record in self.records.items():
            if tokens is not None and token not in tokens:
                continue
            if record.unreviewed or file_stamp(record.path) != record.stamp:
                try:
                    session.get(token)
                except BackupError:
                    continue
                result.append(record)
        return result

    def records_for(self, tokens):
        return [record for token, record in self.records.items() if token in tokens]

    def has_changes(self, records):
        return any(
            record.unreviewed or file_stamp(record.path) != record.stamp for record in records
        )

    def has_unreviewed(self, records):
        return any(record.unreviewed for record in records)

    def stage(self, session, records, cancel):
        updates = []
        for record in records:
            stamp = file_stamp(record.path)
            if stamp == (None,):
                raise BackupError(
                    f"外部编辑文件已被移动或删除：{record.path.name}",
                    english="The externally edited file was moved or deleted: {name}",
                    values={"name": record.path.name},
                )
            session.stage_edit(record.token, record.path, cancel)
            staged = session.cached_file(record.token, cancel=cancel)
            updates.append((record, digest(staged, cancel=cancel), stamp))
        return updates

    def accept_staged(self, updates):
        for record, fingerprint, stamp in updates:
            record.fingerprint, record.stamp = fingerprint, stamp
            record.unreviewed = file_stamp(record.path) != stamp

    def _file_event(self, path):
        for record in self.records.values():
            if str(record.path) == path:
                record.stable = 0
                record.unreviewed = file_stamp(record.path) != record.stamp

    def _directory_event(self, path):
        for record in self.records.values():
            if str(record.path.parent) == path:
                self._file_event(str(record.path))

    def poll(self):
        for record in list(self.records.values()):
            if time.monotonic() < record.later_until:
                continue
            try:
                stamp = file_stamp(record.path)
            except OSError as exc:
                self.external_check_failed(record, exc)
                continue
            if stamp == record.stamp:
                continue
            record.unreviewed = True
            if record.seen != stamp:
                record.seen, record.stable = stamp, 0
                continue
            record.stable += 1
            if stamp == (None,):
                choice = QMessageBox.question(
                    self.owner,
                    self.owner.language_context.translate("Temporary File Deleted"),
                    self.owner.language_context.translate(
                        "Stop tracking this copy? The file in the backup will not be deleted."
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if choice == QMessageBox.StandardButton.Yes:
                    self.records.pop(record.token, None)
                else:
                    record.later_until = time.monotonic() + 30
                return
            if str(record.path) not in self.watcher.files():
                self.watcher.addPath(str(record.path))

            def checked(fingerprint, rec=record, observed=stamp):
                try:
                    current_stamp = file_stamp(rec.path)
                except OSError as exc:
                    self.external_check_failed(rec, exc)
                    return
                if current_stamp != observed:
                    rec.seen, rec.stable = None, 0
                    return
                if fingerprint == rec.fingerprint:
                    rec.stamp, rec.unreviewed = observed, False
                    return
                self.review_external(rec, fingerprint, observed)

            self.owner.run_task(
                "检查外部编辑",
                lambda progress, cancel, rec=record: digest(rec.path, cancel=cancel),
                checked,
                silent=True,
                on_error=lambda exc, rec=record: self.external_check_failed(rec, exc),
            )
            return

    def external_check_failed(self, record, error):
        record.unreviewed = True
        record.later_until = time.monotonic() + 30
        self.owner.statusBar().showMessage(
            self.owner.language_context.translate(
                "Could not check external edits for {name}: {error}",
                name=record.path.name,
                error=str(error),
            ),
            10000,
        )

    def review_external(self, record, fingerprint, stamp):
        box = QMessageBox(
            QMessageBox.Icon.Question,
            self.owner.language_context.translate("External Edit Detected"),
            "",
            parent=self.owner,
        )
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(
            self.owner.language_context.translate(
                "{name} has changed. Stage the new contents in this backup session?",
                name=record.path.name,
            )
        )
        stage = box.addButton(
            self.owner.language_context.translate("Stage Changes"),
            QMessageBox.ButtonRole.AcceptRole,
        )
        ignore = box.addButton(
            self.owner.language_context.translate("Ignore This Change"),
            QMessageBox.ButtonRole.DestructiveRole,
        )
        box.addButton(
            self.owner.language_context.translate("Decide Later"), QMessageBox.ButtonRole.RejectRole
        )
        box.exec()
        if box.clickedButton() == stage:

            def staged(_):
                record.fingerprint, record.stamp, record.unreviewed = fingerprint, stamp, False
                self.owner.refresh()

            self.owner.run_task(
                "保存外部编辑",
                lambda progress, cancel: self.owner.session.stage_edit(
                    record.token, record.path, cancel
                ),
                staged,
                status_phase=ProgressPhase.SAVE_EXTERNAL,
            )
        elif box.clickedButton() == ignore:
            record.fingerprint, record.stamp, record.unreviewed = fingerprint, stamp, False
        else:
            record.later_until = time.monotonic() + 30
