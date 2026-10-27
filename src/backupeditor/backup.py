"""Read-only source backups, transactional session overlay, and separate-output saves."""

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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .codec import (
    CHUNK,
    BackupError,
    Keybag,
    Metadata,
    check_cancel,
    crypto_file,
    file_id,
    safe_path,
)

Progress = Callable[[str, int, int], None]
COLUMNS = (
    "token",
    "original_id",
    "domain",
    "path",
    "parent",
    "name",
    "kind",
    "size",
    "mtime",
    "staged",
    "changed",
    "explicit",
)


def stale_workspaces():
    """Only return private, marked sessions owned by this user with no live process."""
    result = []
    for path in Path(tempfile.gettempdir()).glob("backupeditor-*"):
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
                continue
            marker = path / ".backupeditor-session.json"
            if marker.is_symlink() or marker.stat().st_size > 2048:
                continue
            data = json.loads(marker.read_text())
            if data.get("application") != "BackupEditor":
                continue
            pid = int(data["pid"])
            if pid < 1:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                result.append(path)
            except PermissionError:
                pass
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def detect_backups(root: Path) -> list[Path]:
    root = root.expanduser().resolve()

    def valid(path):
        return all((path / p).is_file() for p in ("Info.plist", "Manifest.plist"))

    if valid(root):
        return [root]
    if not root.is_dir():
        raise BackupError("请选择存在的备份目录")
    result = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and valid(p))
    if not result:
        raise BackupError("目录内没有 iOS 备份（需要 Info.plist 和 Manifest.plist）")
    return result


def load_plist(path: Path):
    if path.is_symlink() or path.stat().st_size > 64 * 1024 * 1024:
        raise BackupError(f"元数据文件无效或过大：{path.name}")
    try:
        value = plistlib.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("expected dictionary")
        return value
    except (ValueError, plistlib.InvalidFileException) as exc:
        raise BackupError(f"无法读取 {path.name}") from exc


def copy_file(src: Path, dst: Path, cancel=None):
    if src.is_symlink() or not src.is_file():
        raise BackupError(f"拒绝读取链接或特殊文件：{src.name}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    original = src.stat()
    try:
        with src.open("rb") as fi, dst.open("wb") as fo:
            while chunk := fi.read(CHUNK):
                check_cancel(cancel)
                fo.write(chunk)
        current = src.stat()
        if (original.st_size, original.st_mtime_ns, original.st_ino) != (
            current.st_size,
            current.st_mtime_ns,
            current.st_ino,
        ):
            raise BackupError(f"复制期间源文件发生变化，请稍后重试：{src.name}")
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


@dataclass(frozen=True)
class Entry:
    token: str
    original_id: str | None
    domain: str
    path: str
    parent: str
    name: str
    kind: str
    size: int
    mtime: int
    staged: str | None
    changed: int
    explicit: int

    @classmethod
    def row(cls, row):
        return cls(*(row[c] for c in COLUMNS))


class BackupSession:
    def __init__(
        self, root: Path, password: str = "", *, progress: Progress | None = None, cancel=None
    ):
        self.root = Path(root).expanduser().resolve()
        self.workspace = Path(tempfile.mkdtemp(prefix="backupeditor-"))
        (self.workspace / ".backupeditor-session.json").write_text(
            json.dumps(
                {
                    "application": "BackupEditor",
                    "pid": os.getpid(),
                    "created": time.time(),
                }
            )
        )
        self.lock = threading.RLock()
        self.db = None
        self.keybag = None
        self.manifest_key = None
        self.undo_stack = []
        self.redo_stack = []
        self.saved_signature = ()
        self.session_id = uuid.uuid4().hex
        self._progress = progress or (lambda *a: None)
        try:
            self.manifest = load_plist(self.root / "Manifest.plist")
            self.info = load_plist(self.root / "Info.plist")
            if not (self.root / "Manifest.db").is_file():
                raise BackupError("此版本仅支持 Manifest.db 备份；暂不支持 Manifest.mbdb")
            status_path = self.root / "Status.plist"
            if status_path.exists():
                status = load_plist(status_path)
                if status.get("SnapshotState") not in (None, "finished"):
                    raise BackupError("备份尚未完成，不能安全打开")
            self.encrypted = bool(self.manifest.get("IsEncrypted"))
            self.source_stamp = self._stamp()
            self.manifest_path = self.workspace / "Manifest.db"
            self._progress("解锁并读取备份清单…", 0, 0)
            if self.encrypted:
                if not password:
                    raise BackupError("需要备份加密密码")
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
                raise BackupError("Manifest.db 的 Files 表结构不受支持")
            self._index(cancel)
        except Exception:
            self.close()
            raise

    def _stamp(self):
        return tuple(
            (p, (self.root / p).stat().st_size, (self.root / p).stat().st_mtime_ns)
            for p in ("Manifest.db", "Manifest.plist", "Info.plist")
        )

    def _index(self, cancel):
        self.db.execute("""CREATE TABLE EditorEntries(
            token TEXT PRIMARY KEY, original_id TEXT, domain TEXT NOT NULL,
            path TEXT NOT NULL, parent TEXT NOT NULL, name TEXT NOT NULL,
            kind TEXT NOT NULL, size INTEGER NOT NULL, mtime INTEGER NOT NULL,
            staged TEXT, changed INTEGER NOT NULL, explicit INTEGER NOT NULL,
            UNIQUE(domain,path))""")
        self.db.execute("CREATE INDEX EditorParent ON EditorEntries(domain,parent)")
        self.db.execute("CREATE INDEX EditorOriginal ON EditorEntries(original_id)")
        total = self.db.execute("SELECT COUNT(*) FROM Files").fetchone()[0]
        parents = set()
        batch = []
        cursor = self.db.execute("SELECT fileID,domain,relativePath,flags,file FROM Files")
        for count, row in enumerate(cursor, 1):
            if count % 500 == 0:
                check_cancel(cancel)
                self._progress("建立目录索引…", count, total)
            fid, domain, path, flags, blob = row
            safe_path(path)
            if not domain or "/" in domain or "\\" in domain or "\x00" in domain:
                raise BackupError("清单包含无效 domain")
            if (
                not isinstance(fid, str)
                or len(fid) != 40
                or any(c not in "0123456789abcdef" for c in fid)
            ):
                raise BackupError("清单包含无效文件 ID")
            if blob and len(blob) > 128 * 1024:
                raise BackupError("清单中的文件元数据过大，拒绝解析")
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
        self.db.execute("CREATE TABLE EditorBaseline AS SELECT * FROM EditorEntries")
        self.db.commit()
        self._progress("清单已就绪", total, total)

    @property
    def dirty(self):
        return self.signature != self.saved_signature

    @property
    def signature(self):
        return tuple(c[0] for c in self.undo_stack)

    @property
    def changes(self):
        return [c[1] for c in self.undo_stack]

    @property
    def pending_tokens(self):
        return {row["token"] for command in self.undo_stack for row in command[2] + command[3]}

    @property
    def change_details(self):
        result = []
        for _, title, before, after in self.undo_stack:
            old = {row["token"]: row for row in before}
            new = {row["token"]: row for row in after}
            details = []
            for token in list(dict.fromkeys(list(old) + list(new)))[:50]:
                a, b = old.get(token), new.get(token)
                if a is None:
                    details.append(f"添加 {b['domain']}/{b['path']}")
                elif b is None:
                    details.append(f"删除 {a['domain']}/{a['path']}")
                elif a["path"] != b["path"]:
                    details.append(f"{a['domain']}/{a['path']} → {b['path']}")
                elif a["staged"] != b["staged"]:
                    details.append(f"编辑 {b['domain']}/{b['path']}")
            result.append((title, "\n".join(details)))
        return result

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
            raise BackupError("此文件已经被删除或不再存在")
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
                raise BackupError("不能修改 domain 根目录")
            if not any(
                e.domain == entry.domain and entry.path.startswith(e.path + "/") for e in result
            ):
                result.append(entry)
        return result

    def _record(self, title, before, after):
        self.undo_stack.append((uuid.uuid4().hex, title, before, after))
        self.redo_stack.clear()

    def _restore(self, command, index):
        tokens = {r["token"] for r in command[2] + command[3]}
        with self.db:
            self.db.executemany("DELETE FROM EditorEntries WHERE token=?", ((t,) for t in tokens))
            self.db.executemany(
                "INSERT INTO EditorEntries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (tuple(r[c] for c in COLUMNS) for r in command[index]),
            )

    def undo(self):
        with self.lock:
            if self.undo_stack:
                command = self.undo_stack.pop()
                self._restore(command, 2)
                self.redo_stack.append(command)

    def redo(self):
        with self.lock:
            if self.redo_stack:
                command = self.redo_stack.pop()
                self._restore(command, 3)
                self.undo_stack.append(command)

    def discard(self):
        with self.lock, self.db:
            self.db.execute("DELETE FROM EditorEntries")
            self.db.execute("INSERT INTO EditorEntries SELECT * FROM EditorBaseline")
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.saved_signature = ()

    def new_directory(self, domain, parent, name):
        safe_path(name, root=False)
        if "/" in name:
            raise BackupError("名称不能包含目录分隔符")
        with self.lock, self.db:
            directory = self.at(domain, parent)
            path = f"{parent}/{name}".lstrip("/")
            if not directory or directory.kind != "directory":
                raise BackupError("目标目录不存在")
            if self.at(domain, path):
                raise BackupError("该名称已存在")
            token = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO EditorEntries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    token,
                    None,
                    domain,
                    path,
                    parent,
                    name,
                    "directory",
                    0,
                    int(time.time()),
                    None,
                    1,
                    1,
                ),
            )
            self._record(f"新建目录 {name}", [], [self.get(token).__dict__])

    def delete(self, tokens):
        with self.lock:
            entries = self.roots(tokens)
            affected = [e for root in entries for e in self.subtree(root)]
            before = [e.__dict__ for e in affected]
            with self.db:
                self.db.executemany(
                    "DELETE FROM EditorEntries WHERE token=?", ((e.token,) for e in affected)
                )
            self._record(f"删除 {len(entries)} 个项目", before, [])

    def move(self, tokens, domain, parent, *, name=None, collision="error"):
        safe_path(parent)
        with self.lock:
            destination = self.at(domain, parent)
            if not destination or destination.kind != "directory":
                raise BackupError("目标目录不存在")
            roots = self.roots(tokens)
            if name is not None and len(roots) != 1:
                raise BackupError("重命名只能选中一个项目")
            plans, all_entries, replaced = [], [], []
            for root in roots:
                if root.domain != domain:
                    raise BackupError("目前只支持同一 domain 内移动")
                if root.kind == "directory" and (
                    parent == root.path or parent.startswith(root.path + "/")
                ):
                    raise BackupError("不能移动到自身或子目录")
                basename = safe_path(name, root=False) if name is not None else root.name
                if "/" in basename:
                    raise BackupError("名称不能包含目录分隔符")
                target = f"{parent}/{basename}".lstrip("/")
                if target == root.path:
                    continue
                existing = self.at(domain, target)
                if existing:
                    if collision == "skip":
                        continue
                    if collision == "keep_both":
                        base, n = target, 2
                        while self.at(domain, target):
                            stem, ext = os.path.splitext(base)
                            target = f"{stem} ({n}){ext}"
                            n += 1
                    elif collision == "replace":
                        if root.path.startswith(target + "/"):
                            raise BackupError("不能覆盖包含源项目的上层目录")
                        replaced.extend(self.subtree(existing))
                    else:
                        raise BackupError(f"目标已存在：{basename}")
                for entry in self.subtree(root):
                    new = target + entry.path[len(root.path) :]
                    plans.append((entry.token, new))
                    all_entries.append(entry)
            before = [e.__dict__ for e in all_entries + replaced]
            with self.db:
                self.db.executemany(
                    "DELETE FROM EditorEntries WHERE token=?", ((e.token,) for e in replaced)
                )
                for token, path in plans:
                    par, _, base = path.rpartition("/")
                    self.db.execute(
                        "UPDATE EditorEntries SET path=?,parent=?,name=?,changed=1 WHERE token=?",
                        (path, par, base, token),
                    )
            if plans:
                self._record(
                    f"重命名 {roots[0].name} → {name}"
                    if name is not None
                    else f"移动 {len(roots)} 个项目 → {domain}/{parent}",
                    before,
                    [self.get(t).__dict__ for t, _ in plans],
                )

    def _stage_copy(self, path, cancel=None):
        target = self.workspace / "pending" / uuid.uuid4().hex
        copy_file(path, target, cancel)
        return target

    def stage_edit(self, token, path: Path, cancel=None):
        with self.lock:
            entry = self.get(token)
            if entry.kind != "file":
                raise BackupError("只能编辑普通文件")
            staged = self._stage_copy(path, cancel)
            with self.db:
                self.db.execute(
                    "UPDATE EditorEntries SET staged=?,size=?,mtime=?,changed=1 WHERE token=?",
                    (str(staged), staged.stat().st_size, int(time.time()), token),
                )
            self._record(f"编辑 {entry.name}", [entry.__dict__], [self.get(token).__dict__])

    def import_paths(self, paths, domain, parent, *, collision="error", cancel=None, progress=None):
        """Stage an atomic recursive import. External links/special files are rejected."""
        with self.lock:
            target = self.at(domain, parent)
            if not target or target.kind != "directory":
                raise BackupError("请先进入一个 domain 目录")
            before, added = [], []
            progress = progress or (lambda *a: None)
            with self.db:
                for source in map(Path, paths):
                    check_cancel(cancel)
                    if source.is_symlink() or not (source.is_file() or source.is_dir()):
                        raise BackupError(f"不支持导入此文件类型：{source.name}")
                    safe_path(source.name, root=False)
                    dest = f"{parent}/{source.name}".lstrip("/")
                    existing = self.at(domain, dest)
                    replacement = None
                    if existing:
                        if collision == "skip":
                            continue
                        if collision == "keep_both":
                            n, base = 2, dest
                            while self.at(domain, dest):
                                stem, ext = os.path.splitext(base)
                                dest = f"{stem} ({n}){ext}"
                                n += 1
                        elif collision == "replace":
                            if existing.kind == "file" and source.is_file():
                                replacement = existing
                            removed = self.subtree(existing)
                            before.extend(e.__dict__ for e in removed if e.token not in added)
                            removed_tokens = {e.token for e in removed}
                            added = [token for token in added if token not in removed_tokens]
                            self.db.executemany(
                                "DELETE FROM EditorEntries WHERE token=?",
                                ((e.token,) for e in removed),
                            )
                        else:
                            raise BackupError(f"目标已存在：{source.name}")
                    todo = [(source, dest)]
                    while todo:
                        check_cancel(cancel)
                        src, virtual = todo.pop()
                        if src.is_symlink() or not (src.is_dir() or src.is_file()):
                            raise BackupError(f"不支持链接或特殊文件：{src.name}")
                        progress(f"暂挂添加：{src.name}", 0, 0)
                        is_dir = src.is_dir()
                        staged = None if is_dir else str(self._stage_copy(src, cancel))
                        par, _, basename = virtual.rpartition("/")
                        token = (
                            replacement.token if replacement and src == source else uuid.uuid4().hex
                        )
                        self.db.execute(
                            "INSERT INTO EditorEntries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                token,
                                replacement.original_id if replacement and src == source else None,
                                domain,
                                safe_path(virtual),
                                par,
                                basename,
                                "directory" if is_dir else "file",
                                0 if is_dir else src.stat().st_size,
                                int(src.stat().st_mtime),
                                staged,
                                1,
                                1,
                            ),
                        )
                        added.append(token)
                        if is_dir:
                            todo.extend(
                                (p, virtual + "/" + p.name)
                                for p in sorted(src.iterdir(), reverse=True)
                            )
            if added or before:
                self._record(
                    f"添加 {len(added)} 个文件/目录", before, [self.get(t).__dict__ for t in added]
                )

    def payload_path(self, fid):
        path = self.root / fid[:2] / fid
        if not path.is_file():
            path = self.root / fid
        if path.is_symlink() or not path.is_file():
            raise BackupError(f"缺少或无效的备份数据文件：{fid}")
        if path.resolve().parent not in (self.root, self.root / fid[:2]):
            raise BackupError("备份数据路径包含目录链接")
        return path

    def metadata(self, entry):
        if entry.original_id:
            row = self.db.execute(
                "SELECT file FROM Files WHERE fileID=?", (entry.original_id,)
            ).fetchone()
            if row and row[0]:
                return Metadata(row[0])
        return None

    def extract_file(self, token, target: Path, *, cancel=None):
        with self.lock:
            entry = self.get(token)
            if entry.kind != "file":
                raise BackupError("只能提取普通文件；链接和特殊条目仅供浏览")
            if target.is_symlink():
                raise BackupError("提取目标不能是链接")
            if entry.staged:
                copy_file(Path(entry.staged), target, cancel)
            else:
                src = self.payload_path(entry.original_id)
                if self.encrypted:
                    metadata = self.metadata(entry)
                    wrapped = metadata.get("EncryptionKey")
                    if not wrapped:
                        if entry.size == 0 and src.stat().st_size == 0:
                            copy_file(src, target, cancel)
                            return target
                        raise BackupError("文件缺少加密密钥")
                    _, key = self.keybag.unwrap(wrapped)
                    crypto_file(src, target, key, encrypt=False, cancel=cancel)
                else:
                    copy_file(src, target, cancel)
            if entry.mtime:
                os.utime(target, (entry.mtime, entry.mtime))
            return target

    def export(self, tokens, target: Path, *, collision="error", progress=None, cancel=None):
        with self.lock:
            target = Path(target).resolve()
            if target == self.root or self.root in target.parents:
                raise BackupError("不能提取到原备份目录内部")
            target.mkdir(parents=True, exist_ok=True)
            roots = self.roots(tokens)
            exported = []
            progress = progress or (lambda *a: None)
            for root in roots:
                output = target / root.name
                if output.exists():
                    if collision == "skip":
                        continue
                    if collision == "keep_both":
                        n = 2
                        while output.exists():
                            output = target / f"{root.name} ({n})"
                            n += 1
                    elif collision != "replace":
                        raise BackupError(f"目标已存在：{output.name}（请选择保留两者或跳过）")
                entries = self.subtree(root)
                for i, entry in enumerate(sorted(entries, key=lambda e: len(e.path)), 1):
                    check_cancel(cancel)
                    progress(f"提取：{entry.name}", i, len(entries))
                    suffix = entry.path[len(root.path) :].lstrip("/")
                    dest = output / suffix if suffix else output
                    if dest.is_symlink() or any(
                        p.is_symlink() for p in dest.parents if p != target
                    ):
                        raise BackupError("提取目标路径包含链接")
                    if entry.kind == "directory":
                        if dest.exists() and not dest.is_dir():
                            raise BackupError(f"目标不是目录：{dest.name}")
                        dest.mkdir(parents=True, exist_ok=True)
                    elif entry.kind == "file":
                        if dest.exists() and not dest.is_file():
                            raise BackupError(f"不能用文件替换目录：{dest.name}")
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        temporary = dest.parent / (".backupeditor-extract-" + uuid.uuid4().hex)
                        try:
                            self.extract_file(entry.token, temporary, cancel=cancel)
                            os.replace(temporary, dest)
                        finally:
                            temporary.unlink(missing_ok=True)
                    else:
                        raise BackupError(f"不能提取链接或特殊条目：{entry.name}")
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

    def save(self, destination: Path, *, progress=None, cancel=None):
        """Publish a complete, validated, separately copied backup; source is never written."""
        with self.lock:
            progress = progress or (lambda *a: None)
            destination = destination.expanduser().absolute()
            if destination.exists():
                raise BackupError("保存目标已存在，请选择一个新的目录名称")
            destination = destination.parent.resolve() / destination.name
            if (
                destination == self.root
                or self.root in destination.parents
                or destination in self.root.parents
            ):
                raise BackupError("保存位置不能与原备份重叠")
            if self._stamp() != self.source_stamp:
                raise BackupError("原备份在打开后已改变，请重新打开备份")
            destination.parent.mkdir(parents=True, exist_ok=True)
            progress("估算备份大小…", 0, 0)
            required = self.estimate_save_bytes()
            extra = self.manifest_path.stat().st_size * 2
            if shutil.disk_usage(destination.parent).free < required + extra:
                raise BackupError("目标磁盘空间不足以创建独立备份副本")
            work = Path(tempfile.mkdtemp(prefix=".backupeditor-save-", dir=destination.parent))
            out_db_path = self.workspace / (uuid.uuid4().hex + ".db")
            try:
                files = [p for p in self.root.rglob("*") if p.is_file() or p.is_symlink()]
                for n, source in enumerate(files, 1):
                    check_cancel(cancel)
                    progress("复制备份到独立输出目录…", n, len(files))
                    copy_file(source, work / source.relative_to(self.root), cancel)
                with sqlite3.connect(out_db_path) as out_db:
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
                                staged = Path(entry.staged)
                                meta.set("Size", staged.stat().st_size)
                                meta.set("LastModified", entry.mtime)
                                meta.set("LastStatusChange", int(time.time()))
                                sha1 = digest(staged, "sha1", cancel)
                                meta.set("Digest", sha1)
                                if self.encrypted:
                                    clas = int(meta.get("ProtectionClass", 3))
                                    key, wrapped = self.keybag.new_key(clas)
                                    meta.set("EncryptionKey", wrapped)
                                    crypto_file(staged, output, key, encrypt=True, cancel=cancel)
                                else:
                                    meta.root.pop("EncryptionKey", None)
                                    copy_file(staged, output, cancel)
                                verification.append((fid, sha1, meta))
                            elif entry.original_id != fid:
                                copy_file(self.payload_path(entry.original_id), output, cancel)
                            # Modern saves use the sharded payload path, including when
                            # the source had a legacy flat payload layout.
                            if output.exists():
                                (work / fid).unlink(missing_ok=True)
                        elif entry.kind not in ("directory", "special"):
                            raise BackupError("无法保存未知条目类型")
                        flags = {"file": 1, "directory": 2, "special": 4}[entry.kind]
                        if entry.kind == "special" and entry.original_id:
                            flags = self.db.execute(
                                "SELECT flags FROM Files WHERE fileID=?", (entry.original_id,)
                            ).fetchone()[0]
                        out_db.execute(
                            "INSERT INTO Files(fileID,domain,relativePath,flags,file) VALUES(?,?,?,?,?)",
                            (fid, entry.domain, entry.path, flags, meta.dump()),
                        )
                    for fid in obsolete:
                        if not out_db.execute(
                            "SELECT 1 FROM Files WHERE fileID=?", (fid,)
                        ).fetchone():
                            for candidate in (work / fid[:2] / fid, work / fid):
                                candidate.unlink(missing_ok=True)
                    expected_count = out_db.execute(
                        "SELECT COUNT(*) FROM EditorEntries WHERE explicit=1"
                    ).fetchone()[0]
                    if out_db.execute("SELECT COUNT(*) FROM Files").fetchone()[0] != expected_count:
                        raise BackupError("输出清单条目数量不一致")
                    out_db.execute("DROP TABLE EditorEntries")
                    out_db.execute("DROP TABLE EditorBaseline")
                    out_db.commit()
                    result = out_db.execute("PRAGMA integrity_check").fetchone()[0]
                    if result != "ok":
                        raise BackupError("输出清单完整性检查失败")
                    payload_ids = out_db.execute("SELECT fileID FROM Files WHERE flags=1")
                    for (fid,) in payload_ids:
                        check_cancel(cancel)
                        if not (work / fid[:2] / fid).is_file() and not (work / fid).is_file():
                            raise BackupError(f"输出备份缺少文件数据：{fid}")
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
                        raise BackupError("输出清单加密往返验证失败")
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
                            raise BackupError("输出文件校验失败")
                    finally:
                        plain.unlink(missing_ok=True)
                check_cancel(cancel)
                if self._stamp() != self.source_stamp:
                    raise BackupError("保存期间原备份发生变化，输出未发布")
                if destination.exists():
                    raise BackupError("保存目标被其他程序创建，输出未覆盖")
                work.rename(destination)
                with self.db:
                    self.db.execute("DELETE FROM EditorBaseline")
                    self.db.execute("INSERT INTO EditorBaseline SELECT * FROM EditorEntries")
                self.undo_stack.clear()
                self.redo_stack.clear()
                self.saved_signature = ()
                return destination
            finally:
                if work.exists():
                    shutil.rmtree(work)
                out_db_path.unlink(missing_ok=True)

    def close(self):
        with contextlib.suppress(Exception):
            if self.db is not None:
                self.db.close()
        self.db = None
        if self.keybag:
            self.keybag.keys.clear()
        self.keybag = self.manifest_key = None
        shutil.rmtree(self.workspace, ignore_errors=True)
