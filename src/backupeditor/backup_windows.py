"""主窗口登记和跨进程备份占用；锁放运行目录，不写入来源备份。"""

from __future__ import annotations

import hashlib
import os
import tempfile
import weakref
from pathlib import Path

from PySide6.QtCore import QLockFile, QStandardPaths
from PySide6.QtWidgets import QApplication

from .codec import BackupError


def backup_keys(path):
    root = Path(path).expanduser().resolve()
    keys = {"path:" + os.path.normcase(str(root))}
    try:
        info = root.stat()
    except FileNotFoundError:
        pass
    else:
        if info.st_ino:
            keys.add(f"inode:{info.st_dev}:{info.st_ino}")
    return root, keys


class BackupClaim:
    def __init__(self, manager, path, owner, *, shared=None):
        self.manager = manager
        self.path, keys = backup_keys(path)
        self.owner = weakref.ref(owner)
        self.locks = {}
        self.released = False
        if shared is not None and (
            shared.manager is not manager or shared.owner() is not owner or shared.released
        ):
            raise BackupError("不能共享其他会话的备份占用")
        self._acquire(keys, shared=shared)

    def _acquire(self, keys, *, shared=None):
        acquired = {}
        created = []
        try:
            for key in sorted(keys - self.locks.keys()):
                if shared is not None and key in shared.locks:
                    acquired[key] = shared.locks[key]
                    continue
                name = hashlib.sha256(key.encode()).hexdigest() + ".lock"
                lock = QLockFile(str(self.manager.lock_directory / name))
                # 长任务不能仅因锁年龄被误判失效；QLockFile仍检查死亡进程。
                lock.setStaleLockTime(0)
                if not lock.tryLock(0):
                    raise BackupError("此备份已在另一个窗口或程序实例中打开，请先关闭它。")
                acquired[key] = lock
                created.append(lock)
        except BaseException:
            for lock in created:
                lock.unlock()
            raise
        self.locks.update(acquired)

    def promote(self):
        """新输出发布后补充磁盘身份锁，路径锁从创建前一直持有。"""
        if self.released:
            raise BackupError("备份占用已释放")
        _, keys = backup_keys(self.path)
        self._acquire(keys)

    def release(self):
        if self.released:
            return
        self.released = True
        for key, lock in self.locks.items():
            if not any(
                claim is not self and claim.locks.get(key) is lock for claim in self.manager.claims
            ):
                lock.unlock()
        self.locks.clear()
        if self in self.manager.claims:
            self.manager.claims.remove(self)


class BackupWindows:
    def __init__(self, lock_directory=None):
        if lock_directory is None:
            runtime = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.RuntimeLocation
            )
            home_key = hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]
            lock_directory = Path(runtime or tempfile.gettempdir()) / (
                "backupeditor-locks-" + home_key
            )
        self.lock_directory = Path(lock_directory)
        self.lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.lock_directory.is_symlink() or (
            hasattr(os, "getuid") and self.lock_directory.stat().st_uid != os.getuid()
        ):
            raise BackupError("备份占用锁目录不安全")
        self.windows = set()
        self.claims = []

    @classmethod
    def current(cls):
        app = QApplication.instance()
        manager = getattr(app, "_backup_windows", None)
        if manager is None:
            manager = cls()
            app._backup_windows = manager
        return manager

    def reserve(self, path, owner, *, shared=None):
        claim = BackupClaim(self, path, owner, shared=shared)
        self.claims.append(claim)
        return claim

    def owner_for(self, path):
        _, keys = backup_keys(path)
        for claim in self.claims:
            if keys.intersection(claim.locks):
                return claim.owner()
        return None

    def window_for_session(self, session_id):
        return next(
            (w for w in self.windows if w.session and w.session.session_id == session_id), None
        )

    def remove(self, window):
        for claim in list(self.claims):
            if claim.owner() is window:
                claim.release()
        self.windows.discard(window)
