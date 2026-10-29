"""Session-scoped LRU caches for extracted files and decoded still images."""

import shutil
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtGui import QImage

from .codec import BackupError, check_cancel
from .image_data import MAX_FILE_BYTES, DecodedImage, decode_image

MEMORY_BUDGET = 128 * 1024**2
DISK_BUDGET = 512 * 1024**2
MEMORY_ENTRIES = 16
DISK_ENTRIES = 128


@dataclass
class CachedFile:
    revision: tuple
    path: Path
    size: int
    users: int = 0
    provider: object = None
    metadata: object = None

    @property
    def disk_bytes(self):
        return self.size + (self.provider.disk_size() if self.provider else 0)


class ImageCache:
    def __init__(self, session):
        self.session = session
        self.directory = session.workspace / "image-cache"
        self.decoded = OrderedDict()
        self.files = OrderedDict()
        self._lock = threading.RLock()
        self._loads = {}
        self._retired = []
        self._work = threading.BoundedSemaphore(2)

    @contextmanager
    def session_lock(self, cancel):
        while not self.session.lock.acquire(timeout=0.05):
            check_cancel(cancel)
        try:
            check_cancel(cancel)
            yield
        finally:
            self.session.lock.release()

    def source(self, token, cancel=None):
        # Match BackupSession._extract_entry's preference for the session's
        # current plain file. A new baseline, staged version or replacement
        # changes this signature; a rename alone need not decode again.
        with self.session_lock(cancel):
            entry = self.session.get(token)
            if entry.kind != "file":
                raise BackupError("此项目已不再是可读取的图片文件。")
            if entry.size > MAX_FILE_BYTES:
                raise BackupError("图片文件超过 128 MiB 的预览上限。")
            path = Path(entry.staged) if entry.staged else self.session.cache_path(token)
            if not path.is_file():
                if not entry.original_id:
                    raise BackupError("图片源文件已不可用。")
                path = self.session.payload_path(entry.original_id)
            stat = path.stat()
            revision = (
                str(self.session.root),
                str(path),
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_ino,
            )
            return entry, revision

    def _hit(self, token, revision, entry):
        with self._lock:
            cached = self.decoded.get(token)
            if (
                cached
                and cached[0] == revision
                and not (cached[1].large and cached[1].large.closed)
            ):
                self.decoded.move_to_end(token)
                if token in self.files:
                    self.files.move_to_end(token)
                result = cached[1]
                return replace(
                    result,
                    image=QImage(result.image),
                    name=entry.name,
                    backup_path=f"/{entry.domain}/{entry.path}",
                    lease=result.large.lease() if result.large else None,
                )
        return None

    def load(self, token, cancel=None, *, force=False):
        check_cancel(cancel)
        entry, revision = self.source(token, cancel)
        if not force and (hit := self._hit(token, revision, entry)):
            return hit
        with self._lock:
            gate, users = self._loads.get(token, (threading.Lock(), 0))
            self._loads[token] = (gate, users + 1)
        acquired = False
        try:
            while not gate.acquire(timeout=0.05):
                check_cancel(cancel)
            acquired = True
            check_cancel(cancel)
            entry, revision = self.source(token, cancel)
            if not force and (hit := self._hit(token, revision, entry)):
                return hit
            while not self._work.acquire(timeout=0.05):
                check_cancel(cancel)
            try:
                check_cancel(cancel)
                return self._load(token, revision, cancel, force)
            finally:
                self._work.release()
        finally:
            if acquired:
                gate.release()
            with self._lock:
                _, users = self._loads[token]
                if users == 1:
                    self._loads.pop(token)
                else:
                    self._loads[token] = (gate, users - 1)

    def _load(self, token, revision, cancel, force):
        with self._lock:
            cached = self.files.get(token)
            if cached and (force or cached.revision != revision or not cached.path.is_file()):
                self.files.pop(token)
                self._retired.append(cached)
                self._trim()
                cached = None
            self.decoded.pop(token, None)
            if cached:
                cached.users += 1
                self.files.move_to_end(token)
        if not cached:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / uuid.uuid4().hex
            try:
                with self.session_lock(cancel):
                    check_cancel(cancel)
                    self.session.extract_file(token, path, cancel=cancel)
                check_cancel(cancel)
                cached = CachedFile(revision, path, path.stat().st_size, users=1)
                if cached.size > MAX_FILE_BYTES:
                    raise BackupError("图片文件超过 128 MiB 的预览上限。")
                with self._lock:
                    self.files[token] = cached
                    if not self._trim():
                        raise BackupError("图片文件缓存空间不足，请关闭部分图片窗口。")
            except BaseException:
                with self._lock:
                    if cached is not None and self.files.get(token) is cached:
                        self.files.pop(token)
                path.unlink(missing_ok=True)
                raise
        try:
            if cached.provider and not cached.provider.closed:
                result = DecodedImage(
                    cached.provider.preview(cancel),
                    cached.provider.format,
                    large=cached.provider,
                    metadata=cached.metadata,
                )
            else:
                result = decode_image(cached.path, cancel)
            cached.metadata = result.metadata
            # HEIF may already have produced a tiled file. Account for it even
            # when cancellation or a changed source prevents publishing pixels.
            if result.large:
                with self._lock:
                    if cached.provider and not cached.provider.closed:
                        result.large = cached.provider
                    cached.provider = result.large
                    result.large.format = result.format
                    result.large.capacity = self.trim_capacity
            check_cancel(cancel)
            current, after = self.source(token, cancel)
            if after != revision:
                raise BackupError("图片源文件在读取时发生变化，请重新加载。")
            result.name = current.name
            result.backup_path = f"/{current.domain}/{current.path}"
            with self._lock:
                self.decoded[token] = (revision, result)
                lease = result.large.lease() if result.large else None
                if not self._trim():
                    if lease:
                        lease.release()
                    raise BackupError("图片分块缓存空间不足，请关闭部分图片窗口。")
            return replace(result, image=QImage(result.image), lease=lease)
        finally:
            with self._lock:
                cached.users -= 1
                self._trim()

    def trim_capacity(self):
        with self._lock:
            return self._trim()

    @staticmethod
    def discard_file(cached):
        if cached.users or (cached.provider and not cached.provider.close()):
            return False
        cached.path.unlink(missing_ok=True)
        return True

    def _trim(self):
        size = sum(item[1].image.sizeInBytes() for item in self.decoded.values())
        while self.decoded and (size > MEMORY_BUDGET or len(self.decoded) > MEMORY_ENTRIES):
            _, (_, result) = self.decoded.popitem(last=False)
            size -= result.image.sizeInBytes()
        for cached in list(self._retired):
            if self.discard_file(cached):
                self._retired.remove(cached)
        size = sum(item.disk_bytes for item in list(self.files.values()) + self._retired)
        for token, cached in list(self.files.items()):
            if size <= DISK_BUDGET and len(self.files) <= DISK_ENTRIES:
                break
            amount = cached.disk_bytes
            if self.discard_file(cached):
                self.files.pop(token)
                self.decoded.pop(token, None)
                size -= amount
        return size <= DISK_BUDGET

    def clear(self):
        # The main window calls this only after all image and region workers end.
        with self._lock:
            self.decoded.clear()
            for cached in list(self.files.values()) + self._retired:
                if cached.provider:
                    cached.provider.close()
            self.files.clear()
            self._retired.clear()
            self._loads.clear()
        shutil.rmtree(self.directory, ignore_errors=True)
