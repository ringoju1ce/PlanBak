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
from .file_opening import source_revision
from .image_data import MAX_FILE_BYTES, DecodedImage, decode_image
from .progress import ProgressMessage, ProgressPhase

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
                raise BackupError(
                    english="This item is no longer a readable image file.",
                )
            if entry.size > MAX_FILE_BYTES:
                raise BackupError(
                    english="The image exceeds the 128 MiB preview limit.",
                )
            return entry, source_revision(self.session, token)

    @contextmanager
    def prepared_source(self, token, revision):
        """为文件头识别短暂固定已有快照，避免重复提取已浏览过的图片。"""
        with self._lock:
            cached = self.files.get(token)
            if cached and cached.revision == revision and cached.path.is_file():
                cached.users += 1
            else:
                cached = None
        try:
            yield cached.path if cached else None
        finally:
            if cached:
                with self._lock:
                    cached.users -= 1
                    self._trim()

    def adopt(self, token, path, revision, cancel=None):
        """接收统一打开流程提取的快照；转移所有权而不是再次解密。"""
        # 与正在导航/解码同一图片的任务串行，避免覆盖它刚登记的缓存。
        with self.token_lock(token, cancel), self.session_lock(cancel):
            _, current = self.source(token, cancel)
            if current != revision:
                raise BackupError(
                    english="The source image changed while being read; open it again.",
                )
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise BackupError(
                    english="The image exceeds the 128 MiB preview limit.",
                )
            with self._lock:
                previous = self.files.get(token)
                if previous and previous.revision == revision and previous.path.is_file():
                    return
                self.directory.mkdir(parents=True, exist_ok=True)
                target = self.directory / uuid.uuid4().hex
                path.replace(target)
                cached = CachedFile(revision, target, size, users=1)
                if previous:
                    self._retired.append(previous)
                self.decoded.pop(token, None)
                self.files[token] = cached
                try:
                    if not self._trim():
                        self.files.pop(token)
                        target.unlink(missing_ok=True)
                        raise BackupError(
                            english="The image file cache is full; close some image windows.",
                        )
                finally:
                    cached.users -= 1

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

    def load(self, token, cancel=None, *, force=False, progress=None, on_miss=None):
        check_cancel(cancel)
        entry, revision = self.source(token, cancel)
        if not force and (hit := self._hit(token, revision, entry)):
            return hit
        with self.token_lock(token, cancel):
            entry, revision = self.source(token, cancel)
            if not force and (hit := self._hit(token, revision, entry)):
                return hit
            if on_miss:
                on_miss()
            while not self._work.acquire(timeout=0.05):
                check_cancel(cancel)
            try:
                check_cancel(cancel)
                return self._load(token, revision, cancel, force, progress)
            finally:
                self._work.release()

    @contextmanager
    def token_lock(self, token, cancel):
        with self._lock:
            gate, users = self._loads.get(token, (threading.Lock(), 0))
            self._loads[token] = (gate, users + 1)
        acquired = False
        try:
            while not gate.acquire(timeout=0.05):
                check_cancel(cancel)
            acquired = True
            check_cancel(cancel)
            yield
        finally:
            if acquired:
                gate.release()
            with self._lock:
                _, users = self._loads[token]
                if users == 1:
                    self._loads.pop(token)
                else:
                    self._loads[token] = (gate, users - 1)

    def _load(self, token, revision, cancel, force, progress=None):
        entry, _ = self.source(token, cancel)
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
                label = ProgressMessage(ProgressPhase.LOAD_IMAGE, name=entry.name)
                copied = 0

                def advanced(amount):
                    nonlocal copied
                    copied += amount
                    if progress:
                        progress(label, copied, max(1, entry.size))

                if progress:
                    progress(label, 0, max(1, entry.size))
                with self.session_lock(cancel):
                    check_cancel(cancel)
                    self.session.extract_file(
                        token, path, cancel=cancel, progress=advanced if progress else None
                    )
                check_cancel(cancel)
                cached = CachedFile(revision, path, path.stat().st_size, users=1)
                if cached.size > MAX_FILE_BYTES:
                    raise BackupError(
                        english="The image exceeds the 128 MiB preview limit.",
                    )
                with self._lock:
                    self.files[token] = cached
                    if not self._trim():
                        raise BackupError(
                            english="The image file cache is full; close some image windows.",
                        )
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
                raise BackupError(
                    english="The source image changed while being read; reload it.",
                )
            result.name = current.name
            result.backup_path = f"/{current.domain}/{current.path}"
            with self._lock:
                self.decoded[token] = (revision, result)
                lease = result.large.lease() if result.large else None
                if not self._trim():
                    if lease:
                        lease.release()
                    raise BackupError(
                        english="The image tile cache is full; close some image windows.",
                    )
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
