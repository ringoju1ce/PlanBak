"""受保护的来源备份、会话暂挂及可编辑副本保存。"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .backup_copy import (
    backup_stamp,
    check_backup_overlap,
    check_persistent_location,
    copy_backup,
    output_location,
    read_marker,
    write_marker,
)
from .codec import (
    CHUNK,
    MAX_METADATA_BYTES,
    BackupError,
    Keybag,
    Metadata,
    check_cancel,
    crypto_file,
    file_id,
    safe_path,
)
from .staging import Entry, SessionOverlay, checked_staged_path

Progress = Callable[[str, int, int], None]


def process_alive(pid):
    if os.name == "nt":
        # Windows 的 os.kill(pid, 0) 不能当作无副作用的 POSIX 存活探测。
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # 无权限/未知错误时保守保留。
        try:
            code = wintypes.DWORD()
            return not api.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def stale_workspaces():
    """Only return private, marked sessions owned by this user with no live process."""
    result = []
    for path in Path(tempfile.gettempdir()).glob("planbak-*"):
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
                continue
            marker = path / ".planbak-session.json"
            if marker.is_symlink() or marker.stat().st_size > 2048:
                continue
            data = json.loads(marker.read_text())
            if not isinstance(data, dict) or data.get("application") != "PlanBak":
                continue
            pid = int(data["pid"])
            if pid < 1:
                continue
            if not process_alive(pid):
                result.append(path)
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            continue
    return result


def detect_backups(root: Path) -> list[Path]:
    root = root.expanduser().resolve()

    def valid(path):
        return all((path / p).is_file() for p in ("Info.plist", "Manifest.plist"))

    if valid(root):
        return [root]
    if not root.is_dir():
        raise BackupError("请选择存在的备份文件夹", english="Choose an existing backup folder")
    result = sorted(
        p
        for p in root.iterdir()
        if p.is_dir() and not p.is_symlink() and valid(p) and (p / "Manifest.db").is_file()
    )
    if not result:
        raise BackupError(
            "文件夹内没有 iOS 备份（需要 Info.plist、Manifest.plist 和 Manifest.db）",
            english="The folder does not contain an iOS backup (Info.plist, Manifest.plist, and Manifest.db are required)",
        )
    return result


def load_plist(path: Path):
    if path.is_symlink() or path.stat().st_size > 64 * 1024 * 1024:
        raise BackupError(
            f"元数据文件无效或过大：{path.name}",
            english="Metadata file is invalid or too large: {name}",
            values={"name": path.name},
        )
    try:
        value = plistlib.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("expected dictionary")
        return value
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise BackupError(
            f"无法读取 {path.name}", english="Unable to read {name}", values={"name": path.name}
        ) from exc


def copy_file(src: Path, dst: Path, cancel=None, *, progress=None):
    if src.is_symlink() or not src.is_file():
        raise BackupError(
            f"拒绝读取链接或特殊文件：{src.name}",
            english="Refusing to read link or special file: {name}",
            values={"name": src.name},
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    original = src.stat()
    try:
        with src.open("rb") as fi, dst.open("wb") as fo:
            while chunk := fi.read(CHUNK):
                check_cancel(cancel)
                fo.write(chunk)
                if progress:
                    progress(len(chunk))
        current = src.stat()
        if (original.st_size, original.st_mtime_ns, original.st_ino) != (
            current.st_size,
            current.st_mtime_ns,
            current.st_ino,
        ):
            raise BackupError(
                f"复制期间源文件发生变化，请稍后重试：{src.name}",
                english="Source file changed while copying; try again: {name}",
                values={"name": src.name},
            )
        shutil.copystat(src, dst)
    except Exception:
        dst.unlink(missing_ok=True)
        raise


def digest(path: Path, algorithm="sha256", cancel=None):
    h = hashlib.new(algorithm)
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            check_cancel(cancel)
            h.update(chunk)
    return h.digest()


class BackupSession(SessionOverlay):
    def __init__(
        self, root: Path, password: str = "", *, progress: Progress | None = None, cancel=None
    ):
        self.root = Path(root).expanduser().resolve()
        self._protected_roots = {self.root}
        self.workspace = Path(tempfile.mkdtemp(prefix="planbak-"))
        (self.workspace / ".planbak-session.json").write_text(
            json.dumps(
                {
                    "application": "PlanBak",
                    "pid": os.getpid(),
                    "created": time.time(),
                }
            )
        )
        self.lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._applying = None
        self._transfers = set()
        self._copying = False
        self.db = None
        self.keybag = None
        self.manifest_key = None
        self._init_overlay()
        self.session_id = uuid.uuid4().hex
        self._progress = progress or (lambda *a: None)
        try:
            self.manifest = load_plist(self.root / "Manifest.plist")
            self.info = load_plist(self.root / "Info.plist")
            if not (self.root / "Manifest.db").is_file():
                raise BackupError(
                    "此版本仅支持 Manifest.db 备份；暂不支持 Manifest.mbdb",
                    english="This version supports Manifest.db backups only; Manifest.mbdb is not supported",
                )
            status_path = self.root / "Status.plist"
            if status_path.exists():
                status = load_plist(status_path)
                if status.get("SnapshotState") not in (None, "finished"):
                    raise BackupError(
                        "备份尚未完成，不能安全打开",
                        english="The backup is incomplete and cannot be opened safely",
                    )
            self.encrypted = bool(self.manifest.get("IsEncrypted"))
            self.source_stamp = self._stamp()
            self.manifest_path = self.workspace / "Manifest.db"
            self._progress("正在读取备份…", 0, 0)
            if self.encrypted:
                if not password:
                    raise BackupError(
                        "需要备份加密密码", english="The encrypted backup requires a password"
                    )
                self.keybag = Keybag(self.manifest["BackupKeyBag"], password)
                _, self.manifest_key = self.keybag.unwrap(self.manifest["ManifestKey"])
                crypto_file(
                    self.root / "Manifest.db",
                    self.manifest_path,
                    self.manifest_key,
                    encrypt=False,
                    cancel=cancel,
                )
            else:
                copy_file(self.root / "Manifest.db", self.manifest_path, cancel)
            check_cancel(cancel)
            self.db = sqlite3.connect(self.manifest_path, check_same_thread=False)
            self.db.row_factory = sqlite3.Row
            columns = {r[1] for r in self.db.execute("PRAGMA table_info(Files)")}
            if not {"fileID", "domain", "relativePath", "flags", "file"} <= columns:
                raise BackupError(
                    "Manifest.db 的 Files 表结构不受支持",
                    english="The Manifest.db Files table structure is not supported",
                )
            self._index(cancel)
        except Exception:
            self.close()
            raise

    def _stamp(self):
        return backup_stamp(self.root)

    @property
    def editable(self):
        return read_marker(self.root) is not None

    def check_changes_allowed(self):
        with self._state_lock:
            if self._transfers:
                raise BackupError(
                    "正在向其他备份复制项目，请稍后再修改",
                    english="Items are being copied to another backup; try editing again later",
                )
            if self._applying is not None:
                raise BackupError(
                    "正在应用更改，不能新增或放弃暂挂更改",
                    english="Changes are being applied; changes cannot be added or discarded now",
                )

    def begin_apply(self):
        with self._state_lock:
            if self._applying is not None or self._copying or self._transfers:
                raise BackupError(
                    "正在应用更改或创建副本，请稍后重试",
                    english="Changes are being applied or a copy is being created; try again later",
                )
            token = object()
            self._applying = token
            return token

    def begin_transfer(self):
        with self._state_lock:
            if self._applying is not None or self._copying:
                raise BackupError(
                    "来源备份正在应用更改或创建副本，请稍后复制",
                    english="The source backup is applying changes or creating a copy; try again later",
                )
            token = object()
            self._transfers.add(token)
            return token

    def end_transfer(self, token):
        with self._state_lock:
            self._transfers.discard(token)

    def end_apply(self, token):
        with self._state_lock:
            if self._applying is token:
                self._applying = None

    def create_copy(self, destination, *, progress=None, cancel=None, container=False):
        with self._state_lock:
            if self._applying is not None or self._copying or self._transfers:
                raise BackupError(
                    "已有保存或副本任务进行中",
                    english="A save or copy operation is already in progress",
                )
            self._copying = True
        try:
            # 不持有会话锁进行整份复制，允许期间继续浏览、编辑和暂挂。
            with self.lock:
                source, stamp = self.root, self.source_stamp
                protected_roots = tuple(self._protected_roots)
            destination = output_location(
                source, destination, workspace=self.workspace, protected_roots=protected_roots
            )
            return copy_backup(
                source,
                destination,
                stamp,
                progress or (lambda *a: None),
                cancel,
                container=container,
            )
        finally:
            with self._state_lock:
                self._copying = False

    def use_copy(self, copied):
        self.check_changes_allowed()
        with self.lock:
            self.check_changes_allowed()
            check_persistent_location(copied.path, self.workspace)
            check_backup_overlap(copied.path, self._protected_roots)
            if self.root != copied.source or self._stamp() != copied.stamp:
                raise BackupError(
                    "当前备份已改变，未切换到新副本",
                    english="The current backup changed; the new copy was not selected",
                )
            if not read_marker(copied.path) or backup_stamp(copied.path) != copied.stamp:
                raise BackupError(
                    "副本标记或清单已改变，未切换保存位置",
                    english="The copy marker or manifest changed; the save location was not switched",
                )
            self.root = copied.path
            self._protected_roots.add(self.root)
            self.source_stamp = copied.stamp
            self._revision += 1

    def _index(self, cancel):
        self.create_entry_table(self.db)
        total = self.db.execute("SELECT COUNT(*) FROM Files").fetchone()[0]
        self._progress("正在建立备份索引…", 0, total)
        parents = set()
        batch = []
        cursor = self.db.execute("SELECT fileID,domain,relativePath,flags,file FROM Files")
        for count, row in enumerate(cursor, 1):
            if count % 500 == 0:
                check_cancel(cancel)
                self._progress("正在建立备份索引…", count, total)
            fid, domain, path, flags, blob = row
            safe_path(path)
            if not domain or "/" in domain or "\\" in domain or "\x00" in domain:
                raise BackupError(
                    "清单包含无效 domain", english="The manifest contains an invalid domain"
                )
            if (
                not isinstance(fid, str)
                or len(fid) != 40
                or any(c not in "0123456789abcdef" for c in fid)
            ):
                raise BackupError(
                    "清单包含无效文件 ID", english="The manifest contains an invalid file ID"
                )
            if blob and len(blob) > MAX_METADATA_BYTES:
                raise BackupError(
                    "清单中的文件元数据过大，拒绝解析",
                    english="File metadata in the manifest is too large to parse",
                )
            meta = Metadata(blob) if blob else None
            mode = meta.get("Mode", 0) if meta else 0
            kind = (
                "directory"
                if flags == 2 or stat.S_ISDIR(mode)
                else ("file" if flags == 1 and (not mode or stat.S_ISREG(mode)) else "special")
            )
            size = int(meta.get("Size", 0)) if meta else 0
            mtime = int(meta.get("LastModified", 0)) if meta else 0
            parent, _, name = path.rpartition("/")
            batch.append((fid, fid, domain, path, parent, name, kind, size, mtime, None, 0, 1))
            parts = path.split("/") if path else []
            for n in range(len(parts)):
                parents.add((domain, "/".join(parts[:n])))
            parents.add((domain, ""))
            if len(batch) >= 1000:
                self.db.executemany(
                    "INSERT INTO EditorEntries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", batch
                )
                batch.clear()
        if batch:
            self.db.executemany("INSERT INTO EditorEntries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        for domain, path in sorted(parents):
            parent, _, name = path.rpartition("/")
            self.db.execute(
                "INSERT OR IGNORE INTO EditorEntries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, None, domain, path, parent, name, "directory", 0, 0, None, 0, 0),
            )
        self.db.commit()
        self.create_baseline(self.db)
        self.db.commit()
        self._progress("正在建立备份索引…", total, total)

    def domains(self):
        with self.lock:
            return [
                r[0]
                for r in self.db.execute(
                    "SELECT DISTINCT domain FROM EditorEntries ORDER BY domain"
                )
            ]

    def get(self, token) -> Entry:
        row = self.db.execute("SELECT * FROM EditorEntries WHERE token=?", (token,)).fetchone()
        if row is None:
            raise BackupError(
                "此文件已经被删除或不再存在", english="This file was deleted or no longer exists"
            )
        return Entry.row(row)

    def at(self, domain, path):
        row = self.db.execute(
            "SELECT * FROM EditorEntries WHERE domain=? AND path=?", (domain, path)
        ).fetchone()
        return Entry.row(row) if row else None

    def children(self, domain, path):
        with self.lock:
            return [
                Entry.row(r)
                for r in self.db.execute(
                    "SELECT * FROM EditorEntries WHERE domain=? AND parent=? AND path<>?",
                    (domain, path, path),
                )
            ]

    def directory_children(self, domain, path):
        with self.lock:
            return [
                Entry.row(row)
                for row in self.db.execute(
                    "SELECT * FROM EditorEntries WHERE domain=? AND parent=? AND path<>? AND kind='directory'",
                    (domain, path, path),
                )
            ]

    def has_directory_children(self, domain, path):
        with self.lock:
            return (
                self.db.execute(
                    "SELECT 1 FROM EditorEntries WHERE domain=? AND parent=? AND path<>? AND kind='directory' LIMIT 1",
                    (domain, path, path),
                ).fetchone()
                is not None
            )

    def subtree(self, entry):
        return [
            Entry.row(r)
            for r in self.db.execute(
                "SELECT * FROM EditorEntries WHERE domain=? AND (path=? OR substr(path,1,?)=?)",
                (entry.domain, entry.path, len(entry.path) + 1, entry.path + "/"),
            )
        ]

    def roots(self, tokens):
        entries = sorted((self.get(t) for t in set(tokens)), key=lambda e: len(e.path))
        result = []
        for entry in entries:
            if not entry.path:
                raise BackupError(
                    "不能修改此备份的顶层文件夹",
                    english="The backup’s top-level folders cannot be modified",
                )
            if not any(
                e.domain == entry.domain and entry.path.startswith(e.path + "/") for e in result
            ):
                result.append(entry)
        return result

    def payload_path(self, fid):
        path = self.root / fid[:2] / fid
        if not path.is_file():
            path = self.root / fid
        if path.is_symlink() or not path.is_file():
            raise BackupError(
                f"缺少或无效的备份数据文件：{fid}",
                english="Backup data file is missing or invalid: {file_id}",
                values={"file_id": fid},
            )
        if path.resolve().parent not in (self.root, self.root / fid[:2]):
            raise BackupError(
                "备份数据路径包含文件夹链接", english="The backup data path contains a folder link"
            )
        return path

    def metadata(self, entry):
        if entry.original_id:
            row = self.db.execute(
                "SELECT file FROM Files WHERE fileID=?", (entry.original_id,)
            ).fetchone()
            if row and row[0]:
                return Metadata(row[0])
        return None

    def extract_file(self, token, target: Path, *, cancel=None, progress=None):
        with self.lock:
            return self._extract_entry(self.get(token), target, cancel=cancel, progress=progress)

    def _extract_entry(self, entry, target: Path, *, cancel=None, use_cache=True, progress=None):
        if entry.kind != "file":
            raise BackupError(
                "只能导出普通文件；链接和特殊条目仅供浏览",
                english="Only regular files can be exported; links and special entries are view-only",
            )
        if target.is_symlink():
            raise BackupError(
                "导出目标不能是链接", english="The export destination cannot be a link"
            )
        cached = (
            checked_staged_path(entry)
            if use_cache and entry.staged
            else self.cache_path(entry.token)
        )
        if use_cache and cached.is_file():
            if cached.resolve() != target.resolve():
                copy_file(cached, target, cancel, progress=progress)
        else:
            src = self.payload_path(entry.original_id)
            if self.encrypted:
                metadata = self.metadata(entry)
                wrapped = metadata.get("EncryptionKey")
                if not wrapped:
                    if entry.size == 0 and src.stat().st_size == 0:
                        copy_file(src, target, cancel, progress=progress)
                        return target
                    raise BackupError(
                        "文件缺少加密密钥", english="The file encryption key is missing"
                    )
                _, key = self.keybag.unwrap(wrapped)
                crypto_file(src, target, key, encrypt=False, cancel=cancel, progress=progress)
            else:
                copy_file(src, target, cancel, progress=progress)
        if entry.mtime:
            os.utime(target, (entry.mtime, entry.mtime))
        return target

    def export(self, tokens, target: Path, *, collision="error", progress=None, cancel=None):
        with self.lock:
            target = Path(target).resolve()

            def protect(path):
                resolved = path.resolve()
                if any(resolved.is_relative_to(root) for root in self._protected_roots):
                    raise BackupError(
                        "导出位置不能与来源备份或当前备份重叠",
                        english="The export location cannot overlap the source or current backup",
                    )

            def validate_destination(dest):
                protect(dest)
                if dest.is_symlink() or any(p.is_symlink() for p in dest.parents if p != target):
                    raise BackupError(
                        "导出目标路径包含链接",
                        english="The export destination path contains a link",
                    )

            protect(target)
            roots = self.roots(tokens)
            exported = []
            plans = []
            reserved = set()
            progress = progress or (lambda *a: None)
            for root in roots:
                output = target / root.name
                if output.exists() or output in reserved:
                    if collision == "skip":
                        continue
                    if collision == "keep_both":
                        n = 2
                        while output.exists() or output in reserved:
                            output = target / f"{root.name} ({n})"
                            n += 1
                    elif collision != "replace":
                        raise BackupError(
                            f"目标已存在：{output.name}（请选择保留两者或跳过）",
                            english="Destination already exists: {name} (choose Keep Both or Skip)",
                            values={"name": output.name},
                        )
                entries = sorted(self.subtree(root), key=lambda e: len(e.path))
                destinations = []
                # 先检查整个批次，再创建文件夹或替换文件，防止前半批已写入。
                for entry in entries:
                    check_cancel(cancel)
                    suffix = entry.path[len(root.path) :].lstrip("/")
                    dest = output / suffix if suffix else output
                    validate_destination(dest)
                    if entry.kind not in ("file", "directory"):
                        raise BackupError(
                            f"不能导出链接或特殊条目：{entry.name}",
                            english="Cannot export link or special item: {name}",
                            values={"name": entry.name},
                        )
                    destinations.append((entry, dest))
                reserved.add(output)
                plans.append((output, destinations))
            target.mkdir(parents=True, exist_ok=True)
            for output, destinations in plans:
                for i, (entry, dest) in enumerate(destinations, 1):
                    check_cancel(cancel)
                    progress(f"正在导出：{entry.name}", i, len(destinations))
                    validate_destination(dest)
                    if entry.kind == "directory":
                        if dest.exists() and not dest.is_dir():
                            raise BackupError(
                                f"目标不是文件夹：{dest.name}",
                                english="Destination is not a folder: {name}",
                                values={"name": dest.name},
                            )
                        dest.mkdir(parents=True, exist_ok=True)
                    elif entry.kind == "file":
                        if dest.exists() and not dest.is_file():
                            raise BackupError(
                                f"不能用文件替换文件夹：{dest.name}",
                                english="Cannot replace a folder with a file: {name}",
                                values={"name": dest.name},
                            )
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        temporary = dest.parent / (".planbak-extract-" + uuid.uuid4().hex)
                        try:
                            self.extract_file(entry.token, temporary, cancel=cancel)
                            os.replace(temporary, dest)
                        finally:
                            temporary.unlink(missing_ok=True)
                    else:
                        raise BackupError(
                            f"不能导出链接或特殊条目：{entry.name}",
                            english="Cannot export link or special item: {name}",
                            values={"name": entry.name},
                        )
                exported.append(output)
            return exported

    def _new_metadata(self, entry):
        parent = self.at(entry.domain, entry.parent)
        template = self.metadata(parent) if parent else None
        owner = (
            0
            if entry.domain in ("SystemPreferencesDomain", "RootDomain", "ManagedPreferencesDomain")
            else 501
        )
        clas = template.get("ProtectionClass", 3) if template else 3
        return Metadata.new(
            directory=entry.kind == "directory",
            timestamp=entry.mtime,
            uid=template.get("UserID", owner) if template else owner,
            gid=template.get("GroupID", owner) if template else owner,
            clas=clas,
        )

    def estimate_save_bytes(self):
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def save(
        self,
        destination: Path | None = None,
        *,
        progress=None,
        cancel=None,
        _token=None,
        container=False,
    ):
        token = _token if _token is not None else self.begin_apply()
        try:
            with self._state_lock:
                if token is not self._applying:
                    raise BackupError("无效的保存状态", english="Invalid save state")
            return self._save(destination, progress=progress, cancel=cancel, container=container)
        finally:
            self.end_apply(token)

    def _save(self, destination, *, progress=None, cancel=None, container=False):
        """Prepare and validate output, then publish a copy or update a marked backup."""
        with self.lock:
            progress = progress or (lambda *a: None)
            direct = destination is None
            marker = read_marker(self.root)
            if direct:
                if not marker:
                    raise BackupError(
                        "没有有效副本标记，必须保存为新的备份副本",
                        english="No valid copy marker was found; save to a new backup copy",
                    )
                destination = self.root
            else:
                destination = output_location(
                    self.root, destination, workspace=self.workspace,
                    protected_roots=self._protected_roots,
                )
            check_persistent_location(destination, self.workspace)
            if self._stamp() != self.source_stamp:
                raise BackupError(
                    "原备份在打开后已改变，请重新打开备份",
                    english="The original backup changed after opening; reopen the backup",
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            progress("估算备份大小…", 0, 0)
            required = (
                sum(
                    r[0]
                    for r in self.db.execute(
                        "SELECT size FROM EditorEntries WHERE changed=1 AND kind='file'"
                    )
                )
                if direct
                else self.estimate_save_bytes()
            )
            extra = self.manifest_path.stat().st_size * 2
            if shutil.disk_usage(destination.parent).free < required + extra:
                raise BackupError(
                    "目标磁盘空间不足以创建独立备份副本",
                    english="There is not enough disk space to create an independent backup copy",
                )
            temporary = Path(tempfile.mkdtemp(prefix=".planbak-save-", dir=destination.parent))
            work = temporary / self.root.name if container and not direct else temporary
            out_db_path = self.workspace / (uuid.uuid4().hex + ".db")
            saved_db = None
            try:
                if work != temporary:
                    work.mkdir()
                files = (
                    []
                    if direct
                    else [
                        p
                        for p in self.root.rglob("*")
                        if (p.is_file() or p.is_symlink()) and p.name != ".planbak.json"
                    ]
                )
                for n, source in enumerate(files, 1):
                    check_cancel(cancel)
                    progress("复制备份到独立输出文件夹…", n, len(files))
                    copy_file(source, work / source.relative_to(self.root), cancel)
                with contextlib.closing(sqlite3.connect(out_db_path)) as out_db, out_db:
                    self.db.backup(out_db)
                    # Delete original rows that are removed or replaced/moved in the overlay.
                    obsolete = [
                        r[0]
                        for r in out_db.execute(
                            "SELECT fileID FROM Files WHERE fileID NOT IN "
                            "(SELECT original_id FROM EditorEntries WHERE original_id IS NOT NULL AND changed=0)"
                        )
                    ]
                    out_db.executemany(
                        "DELETE FROM Files WHERE fileID=?", ((fid,) for fid in obsolete)
                    )
                    changed = [
                        Entry.row(r)
                        for r in self.db.execute(
                            "SELECT * FROM EditorEntries WHERE changed=1 AND explicit=1"
                        )
                    ]
                    verification = []
                    for i, entry in enumerate(changed, 1):
                        check_cancel(cancel)
                        progress(f"写入：{entry.name}", i, len(changed))
                        meta = self.metadata(entry) or self._new_metadata(entry)
                        fid = file_id(entry.domain, entry.path)
                        meta.set("RelativePath", entry.path)
                        if entry.kind == "file":
                            output = work / fid[:2] / fid
                            if entry.staged:
                                staged = checked_staged_path(entry)
                                try:
                                    staged_size = staged.stat().st_size
                                except FileNotFoundError as exc:
                                    raise BackupError(
                                        f"暂挂的编辑内容不可用：{entry.name}，请重新应用更改。",
                                        english="Staged edited content is unavailable: {name}; apply the change again.",
                                        values={"name": entry.name},
                                    ) from exc
                                meta.set("Size", staged_size)
                                meta.set("LastModified", entry.mtime)
                                meta.set("LastStatusChange", int(time.time()))
                                sha1 = digest(staged, "sha1", cancel)
                                if self.encrypted:
                                    clas = int(meta.get("ProtectionClass", 3))
                                    key, wrapped = self.keybag.new_key(clas)
                                    meta.set("EncryptionKey", wrapped)
                                    crypto_file(staged, output, key, encrypt=True, cancel=cancel)
                                else:
                                    meta.root.pop("EncryptionKey", None)
                                    copy_file(staged, output, cancel)
                                # MBFile的Digest校验备份中的存储字节；加密备份
                                # 必须在加密后计算。sha1单独用于明文往返验证。
                                meta.set(
                                    "Digest",
                                    digest(output, "sha1", cancel) if self.encrypted else sha1,
                                )
                                verification.append((fid, sha1, meta))
                            elif entry.original_id != fid:
                                copy_file(self.payload_path(entry.original_id), output, cancel)
                            # Modern saves use the sharded payload path, including when
                            # the source had a legacy flat payload layout.
                            if output.exists():
                                (work / fid).unlink(missing_ok=True)
                        elif entry.kind not in ("directory", "special"):
                            raise BackupError(
                                "无法保存未知条目类型", english="Cannot save an unknown item type"
                            )
                        flags = {"file": 1, "directory": 2, "special": 4}[entry.kind]
                        if entry.kind == "special" and entry.original_id:
                            flags = self.db.execute(
                                "SELECT flags FROM Files WHERE fileID=?", (entry.original_id,)
                            ).fetchone()[0]
                        blob = meta.dump()
                        if len(blob) > MAX_METADATA_BYTES:
                            raise BackupError(
                                "修改后的文件元数据超过程序的 8 MiB 资源上限；"
                                "未应用更改，备份与暂挂内容均保留",
                                english="Modified file metadata exceeds the application’s 8 MiB limit; changes were not applied and the backup and staged content were preserved",
                            )
                        out_db.execute(
                            "INSERT INTO Files(fileID,domain,relativePath,flags,file) VALUES(?,?,?,?,?)",
                            (fid, entry.domain, entry.path, flags, blob),
                        )
                    removed = []
                    for fid in obsolete:
                        if not out_db.execute(
                            "SELECT 1 FROM Files WHERE fileID=?", (fid,)
                        ).fetchone():
                            removed.append(fid)
                            for candidate in (work / fid[:2] / fid, work / fid):
                                candidate.unlink(missing_ok=True)
                    expected_count = out_db.execute(
                        "SELECT COUNT(*) FROM EditorEntries WHERE explicit=1"
                    ).fetchone()[0]
                    if out_db.execute("SELECT COUNT(*) FROM Files").fetchone()[0] != expected_count:
                        raise BackupError(
                            "输出清单条目数量不一致",
                            english="The output manifest entry count does not match",
                        )
                    out_db.execute("DROP TABLE EditorEntries")
                    out_db.execute("DROP TABLE EditorBaseline")
                    out_db.commit()
                    result = out_db.execute("PRAGMA integrity_check").fetchone()[0]
                    if result != "ok":
                        raise BackupError(
                            "输出清单完整性检查失败",
                            english="The output manifest integrity check failed",
                        )
                    payload_ids = out_db.execute("SELECT fileID FROM Files WHERE flags=1")
                    for (fid,) in payload_ids:
                        check_cancel(cancel)
                        locations = [work / fid[:2] / fid, work / fid]
                        if direct:
                            locations += [self.root / fid[:2] / fid, self.root / fid]
                        if not any(p.is_file() and not p.is_symlink() for p in locations):
                            raise BackupError(
                                f"输出备份缺少文件数据：{fid}",
                                english="Output backup is missing file data: {file_id}",
                                values={"file_id": fid},
                            )
                progress("重新封装并验证清单…", 0, 0)
                if self.encrypted:
                    crypto_file(
                        out_db_path,
                        work / "Manifest.db",
                        self.manifest_key,
                        encrypt=True,
                        cancel=cancel,
                    )
                    verify_db = self.workspace / (uuid.uuid4().hex + ".db")
                    crypto_file(
                        work / "Manifest.db",
                        verify_db,
                        self.manifest_key,
                        encrypt=False,
                        cancel=cancel,
                    )
                    if digest(verify_db, cancel=cancel) != digest(out_db_path, cancel=cancel):
                        raise BackupError(
                            "输出清单加密往返验证失败",
                            english="Encrypted output manifest verification failed",
                        )
                    verify_db.unlink()
                else:
                    copy_file(out_db_path, work / "Manifest.db", cancel)
                # Verify changed payloads independently from their staged plaintext.
                for fid, expected, meta in verification:
                    check_cancel(cancel)
                    payload = work / fid[:2] / fid
                    plain = self.workspace / (uuid.uuid4().hex + ".verify")
                    try:
                        if self.encrypted:
                            _, key = self.keybag.unwrap(meta.get("EncryptionKey"))
                            crypto_file(payload, plain, key, encrypt=False, cancel=cancel)
                        else:
                            copy_file(payload, plain, cancel)
                        if digest(plain, "sha1", cancel) != expected:
                            raise BackupError(
                                "输出文件校验失败", english="Output file verification failed"
                            )
                    finally:
                        plain.unlink(missing_ok=True)
                check_cancel(cancel)
                if self._stamp() != self.source_stamp:
                    raise BackupError(
                        "保存期间原备份发生变化，输出未发布",
                        english="The original backup changed during saving; the output was not published",
                    )
                if not direct and destination.exists():
                    raise BackupError(
                        "保存目标被其他程序创建，输出未覆盖",
                        english="Another application created the destination; it was not overwritten",
                    )
                progress("建立新的保存基线…", 0, 0)
                saved_db = self.prepare_saved_index(out_db_path, cancel)
                check_cancel(cancel)
                if self._stamp() != self.source_stamp:
                    raise BackupError(
                        "保存期间原备份发生变化，输出未发布",
                        english="The original backup changed during saving; the output was not published",
                    )
                if not direct and destination.exists():
                    raise BackupError(
                        "保存目标被其他程序创建，输出未覆盖",
                        english="Another application created the destination; it was not overwritten",
                    )
                if direct:
                    if read_marker(self.root) != marker:
                        raise BackupError(
                            "副本标记在保存期间发生变化",
                            english="The copy marker changed during saving",
                        )
                    # 最后一次取消检查在写入前。开始安装后完成本次写入，不提供日志/回滚。
                    progress("正在应用更改…", 0, 0)
                    payloads = [
                        p for p in work.rglob("*") if p.is_file() and p.name != "Manifest.db"
                    ]
                    installs = [(p, destination / p.relative_to(work)) for p in payloads]
                    deletes = [
                        p
                        for fid in removed
                        for p in (destination / fid[:2] / fid, destination / fid)
                    ]
                    deletes += [destination / p.name for p in payloads]
                    for path in [target for _, target in installs] + deletes:
                        if path.is_symlink() or path.parent.is_symlink():
                            raise BackupError(
                                "副本输出位置不能是链接",
                                english="The copy destination cannot be a link",
                            )
                    try:
                        for payload, target in installs:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            os.replace(payload, target)
                        for path in deletes:
                            path.unlink(missing_ok=True)
                        os.replace(work / "Manifest.db", destination / "Manifest.db")
                    except OSError as exc:
                        raise BackupError(
                            "应用更改未完成，当前副本可能已部分写入；请从原备份重新创建副本。",
                            english="Applying changes did not complete and the current copy may be partially written; create a new copy from the original backup.",
                        ) from exc
                else:
                    write_marker(work)
                    temporary.rename(destination)
                    if container:
                        destination = destination / self.root.name
                new_stamp = backup_stamp(destination)
                old_db, old_path = self.db, self.manifest_path
                self.db, saved_db = saved_db, None
                self.manifest_path = out_db_path
                self.root = destination
                self._protected_roots.add(self.root)
                self.source_stamp = new_stamp
                self._pending.clear()
                self._baseline_hashes.clear()
                self._revision += 1
                old_db.close()
                old_path.unlink(missing_ok=True)
                return destination
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
                if saved_db is not None:
                    saved_db.close()
                if self.manifest_path != out_db_path:
                    out_db_path.unlink(missing_ok=True)

    def close(self):
        with self._state_lock:
            if self._transfers:
                raise BackupError(
                    "正在向其他备份复制项目，暂不能清理来源会话",
                    english="Items are being copied to another backup; the source session cannot be cleared yet",
                )
        with contextlib.suppress(Exception):
            if self.db is not None:
                self.db.close()
        self.db = None
        if self.keybag:
            self.keybag.keys.clear()
        self.keybag = self.manifest_key = None
        shutil.rmtree(self.workspace, ignore_errors=True)
