"""File-level final differences, one current cache per token, and atomic unstage."""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from .codec import BackupError, check_cancel

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
        return cls(*(row[column] for column in COLUMNS))


def relocated(entry, path, domain=None):
    parent, _, name = path.rpartition("/")
    return replace(entry, domain=domain or entry.domain, path=path, parent=parent, name=name)


def beneath(path, root):
    return path == root or path.startswith(root + "/")


def full_path(entry):
    return f"/{entry.domain}" + (f"/{entry.path}" if entry.path else "")


@dataclass(frozen=True)
class PendingChange:
    token: str
    before: Entry | None
    after: Entry | None
    labels: tuple[str, ...]
    location: Entry

    @property
    def title(self):
        text = full_path(self.after or self.before)
        if any(label.startswith("重命名") for label in self.labels):
            text = f"{full_path(self.before)} → {full_path(self.after)}"
        return f"{'、'.join(self.labels)}　{text}"

    @property
    def tooltip(self):
        # Renames intentionally expose only the original path on hover.
        renamed = any(label.startswith("重命名") for label in self.labels)
        return full_path(self.before if renamed else self.after or self.before)


@dataclass
class RevertPlan:
    revision: int
    updates: dict[str, Entry | None]
    reset_contents: set[str]
    locations: dict[str, Entry]


class Mutation:
    def __init__(self, session, folder, cancel):
        self.session, self.folder, self.cancel = session, folder, cancel
        self.before = {}
        self.before_paths = {}
        self.incoming = {}
        self.installed = []
        self.locations = {}

    def touch(self, token):
        if token not in self.before:
            self.before[token] = self.session.find(token)
            entry = self.before[token]
            if entry:
                self.before_paths[(entry.domain, entry.path)] = entry

    def remove(self, token):
        self.touch(token)
        self.session.db.execute("DELETE FROM EditorEntries WHERE token=?", (token,))

    def put(self, entry):
        self.touch(entry.token)
        self.session.db.execute(
            "INSERT INTO EditorEntries VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(token) DO UPDATE SET "
            + ",".join(f"{column}=excluded.{column}" for column in COLUMNS if column != "token"),
            tuple(getattr(entry, c) for c in COLUMNS),
        )

    def file(self, token, source=None, data=None):
        from .backup import copy_file

        check_cancel(self.cancel)
        spool = self.folder / (uuid.uuid4().hex + ".incoming")
        if source is not None:
            copy_file(Path(source), spool, self.cancel)
        else:
            spool.write_bytes(data)
        check_cancel(self.cancel)
        previous = self.incoming.get(token)
        if previous:
            previous.unlink(missing_ok=True)
        self.incoming[token] = spool
        return str(self.session.cache_path(token)), spool.stat().st_size

    def install(self, token, source):
        target = self.session.cache_path(token)
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = self.folder / (uuid.uuid4().hex + ".rollback") if target.exists() else None
        if backup:
            os.replace(target, backup)
        self.installed.append((target, backup))
        if source is not None:
            os.replace(source, target)

    def rollback_files(self):
        for target, backup in reversed(self.installed):
            target.unlink(missing_ok=True)
            if backup:
                os.replace(backup, target)

    def finish(self):
        from .backup import digest

        session = self.session
        pending = dict(session._pending)
        total = len(self.before)
        for number, (token, previous) in enumerate(self.before.items(), 1):
            check_cancel(self.cancel)
            base, current = session.baseline(token), session.find(token)
            stored = current
            incoming = self.incoming.get(token)
            if current and current.kind == "file":
                candidate = incoming or (Path(current.staged) if current.staged else None)
                modified = False
                if candidate:
                    modified = (
                        base is None
                        or base.kind != "file"
                        or digest(candidate, cancel=self.cancel)
                        != session.baseline_digest(base, self.cancel)
                    )
                    current = replace(
                        current,
                        staged=str(session.cache_path(token)) if modified else None,
                        size=candidate.stat().st_size,
                        mtime=current.mtime if modified else base.mtime,
                    )
                if incoming:
                    target = session.cache_path(token)
                    if not target.exists() or digest(incoming, cancel=self.cancel) != digest(
                        target, cancel=self.cancel
                    ):
                        self.install(token, incoming)
                elif not current.staged and session.cache_path(token).exists():
                    self.install(token, None)
            elif session.cache_path(token).exists():
                self.install(token, None)
            labels = []
            if base is None and current:
                labels = ["建立文件夹" if current.kind == "directory" else "添加"]
            elif base and current is None:
                labels = ["删除文件夹" if base.kind == "directory" else "删除"]
            elif base and current:
                if (base.domain, base.path) != (current.domain, current.path):
                    labels.append("重命名文件夹" if current.kind == "directory" else "重命名")
                if current.staged:
                    labels.append("修改")
            if current:
                current = replace(current, changed=int(bool(labels)))
                if not labels and base:
                    current = base
                if current != stored:
                    session.db.execute(
                        "UPDATE EditorEntries SET staged=?,size=?,mtime=?,changed=?,explicit=? WHERE token=?",
                        (
                            current.staged,
                            current.size,
                            current.mtime,
                            current.changed,
                            current.explicit,
                            token,
                        ),
                    )
            if labels:
                old = pending.get(token)
                location = (
                    self.locations.get(token)
                    or current
                    or previous
                    or (old.location if old else base)
                )
                pending[token] = PendingChange(token, base, current, tuple(labels), location)
            else:
                pending.pop(token, None)
            session._change_progress("正在更新更改列表…", number, total)
        check_cancel(self.cancel)
        return pending


class SessionOverlay:
    def _init_overlay(self):
        self._pending = {}
        self._revision = 0
        self._baseline_hashes = {}
        self._visible_revision = -1
        self._visible_changes = ()

    @staticmethod
    def create_entry_table(db):
        db.execute("""CREATE TABLE EditorEntries(
            token TEXT PRIMARY KEY, original_id TEXT, domain TEXT NOT NULL,
            path TEXT NOT NULL, parent TEXT NOT NULL, name TEXT NOT NULL,
            kind TEXT NOT NULL, size INTEGER NOT NULL, mtime INTEGER NOT NULL,
            staged TEXT, changed INTEGER NOT NULL, explicit INTEGER NOT NULL,
            UNIQUE(domain,path))""")
        db.execute("CREATE INDEX EditorParent ON EditorEntries(domain,parent)")
        db.execute(
            "CREATE INDEX EditorDirectories ON EditorEntries(domain,parent) WHERE kind='directory'"
        )
        db.execute("CREATE INDEX EditorOriginal ON EditorEntries(original_id)")

    @staticmethod
    def create_baseline(db):
        db.execute("CREATE TABLE EditorBaseline AS SELECT * FROM EditorEntries")
        db.execute("CREATE UNIQUE INDEX EditorBaselineToken ON EditorBaseline(token)")
        db.execute("CREATE UNIQUE INDEX EditorBaselinePath ON EditorBaseline(domain,path)")

    @property
    def dirty(self):
        return bool(self._pending)

    @property
    def signature(self):
        return self._revision

    @property
    def pending_tokens(self):
        return set(self._pending)

    @property
    def changes(self):
        if self._visible_revision != self._revision:
            directories = self.directory_mappings()
            visible = []
            for change in self._pending.values():
                labels = list(change.labels)
                if change.before and change.after:
                    path = change.before.parent
                    while path:
                        directory = directories.get((change.before.domain, path))
                        if (
                            directory
                            and change.after.path
                            == directory.after.path + change.before.path[len(path) :]
                        ):
                            labels = [label for label in labels if not label.startswith("重命名")]
                            break
                        path = path.rpartition("/")[0]
                if labels:
                    visible.append(replace(change, labels=tuple(labels)))
            self._visible_changes = tuple(
                sorted(
                    visible,
                    key=lambda c: (c.location.domain.casefold(), c.location.path.casefold()),
                )
            )
            self._visible_revision = self._revision
        return self._visible_changes

    def find(self, token):
        row = self.db.execute("SELECT * FROM EditorEntries WHERE token=?", (token,)).fetchone()
        return Entry.row(row) if row else None

    def baseline(self, token):
        row = self.db.execute("SELECT * FROM EditorBaseline WHERE token=?", (token,)).fetchone()
        return Entry.row(row) if row else None

    def baseline_at(self, domain, path):
        row = self.db.execute(
            "SELECT * FROM EditorBaseline WHERE domain=? AND path=?", (domain, path)
        ).fetchone()
        return Entry.row(row) if row else None

    def cache_path(self, token):
        return self.workspace / "pending" / token

    def cached_file(self, token, *, cancel=None):
        from .backup import digest

        with self.lock:
            entry = self.get(token)
            if entry.kind != "file":
                raise BackupError("只能打开普通文件", english="Only regular files can be opened")
            target = self.cache_path(token)
            if not target.exists():
                temporary = self.workspace / (uuid.uuid4().hex + ".extract")
                try:
                    self.extract_file(token, temporary, cancel=cancel)
                    target.parent.mkdir(exist_ok=True)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            if not entry.staged and token not in self._baseline_hashes and self.baseline(token):
                self._baseline_hashes[token] = digest(target, cancel=cancel)
            return target

    def baseline_digest(self, entry, cancel=None):
        from .backup import digest

        if entry.token not in self._baseline_hashes:
            path = self.workspace / (uuid.uuid4().hex + ".baseline")
            try:
                self._extract_entry(entry, path, cancel=cancel, use_cache=False)
                self._baseline_hashes[entry.token] = digest(path, cancel=cancel)
            finally:
                path.unlink(missing_ok=True)
        return self._baseline_hashes[entry.token]

    @contextlib.contextmanager
    def mutation(self, *, progress=None, cancel=None):
        self.check_changes_allowed()
        with (
            self.lock,
            tempfile.TemporaryDirectory(prefix=".change-", dir=self.workspace) as directory,
        ):
            self.check_changes_allowed()
            work = Mutation(self, Path(directory), cancel)
            self._change_progress = progress or (lambda *args: None)
            try:
                with self.db:
                    yield work
                    pending = work.finish()
            except BaseException:
                work.rollback_files()
                raise
            else:
                self._pending = pending
                if work.before:
                    self._revision += 1
                    # Bulk mutations normally run in a worker. Prepare ordering
                    # and inherited-rename filtering there, rather than on repaint.
                    self.changes
            finally:
                self._change_progress = lambda *args: None

    def new_directory(self, domain, parent, name):
        from .codec import safe_path

        safe_path(name, root=False)
        if "/" in name:
            raise BackupError(
                "名称不能包含文件夹分隔符", english="The name cannot contain folder separators"
            )
        with self.mutation() as work:
            directory = self.at(domain, parent)
            path = f"{parent}/{name}".lstrip("/")
            if not directory or directory.kind != "directory":
                raise BackupError(
                    "目标文件夹不存在", english="The destination folder does not exist"
                )
            if self.at(domain, path):
                raise BackupError("该名称已存在", english="An item with that name already exists")
            token = uuid.uuid4().hex
            work.put(
                Entry(
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
                )
            )
        return self.get(token)

    def delete(self, tokens):
        with self.mutation() as work:
            for root in self.roots(tokens):
                for entry in self.subtree(root):
                    work.remove(entry.token)

    def move(self, tokens, domain, parent, *, name=None, collision="error"):
        from .codec import safe_path

        safe_path(parent)
        with self.mutation() as work:
            destination = self.at(domain, parent)
            if not destination or destination.kind != "directory":
                raise BackupError(
                    "目标文件夹不存在", english="The destination folder does not exist"
                )
            roots = self.roots(tokens)
            if name is not None and len(roots) != 1:
                raise BackupError(
                    "重命名只能选中一个项目", english="Select exactly one item to rename"
                )
            plans = []
            for root in roots:
                if root.domain != domain:
                    raise BackupError(
                        "目前只支持在同一个备份顶层文件夹内移动",
                        english="Moving is only supported within the same top-level backup folder",
                    )
                if root.kind == "directory" and beneath(parent, root.path):
                    raise BackupError(
                        "不能移动到自身或子文件夹",
                        english="Cannot move an item into itself or its child folder",
                    )
                basename = safe_path(name, root=False) if name is not None else root.name
                if "/" in basename:
                    raise BackupError(
                        "名称不能包含文件夹分隔符",
                        english="The name cannot contain folder separators",
                    )
                target = f"{parent}/{basename}".lstrip("/")
                if target == root.path:
                    continue
                existing = self.at(domain, target)
                if existing:
                    if collision == "skip":
                        continue
                    if collision == "keep_both":
                        base, number = target, 2
                        while self.at(domain, target):
                            stem, ext = os.path.splitext(base)
                            target = f"{stem} ({number}){ext}"
                            number += 1
                    elif collision == "replace":
                        if root.path.startswith(target + "/"):
                            raise BackupError(
                                "不能覆盖包含源项目的上层文件夹",
                                english="Cannot overwrite a parent folder containing the source item",
                            )
                        for entry in self.subtree(existing):
                            work.remove(entry.token)
                    else:
                        raise BackupError(
                            f"目标已存在：{basename}",
                            english="Destination already exists: {name}",
                            values={"name": basename},
                        )
                for entry in self.subtree(root):
                    moved = relocated(entry, target + entry.path[len(root.path) :])
                    # 明确移动/重命名的合成文件夹必须能独立保存为空文件夹。
                    if moved.kind == "directory":
                        moved = replace(moved, explicit=1)
                    plans.append(moved)
            for entry in plans:
                work.remove(entry.token)
            for entry in plans:
                work.put(entry)

    def stage_edit(self, token, path, cancel=None):
        with self.mutation(cancel=cancel) as work:
            entry = self.get(token)
            if entry.kind != "file":
                raise BackupError("只能编辑普通文件", english="Only regular files can be edited")
            staged, size = work.file(token, source=path)
            work.put(replace(entry, staged=staged, size=size, mtime=int(time.time())))

    def stage_bytes(self, token, data, *, cancel=None):
        with self.mutation(cancel=cancel) as work:
            entry = self.get(token)
            if entry.kind != "file":
                raise BackupError("只能编辑普通文件", english="Only regular files can be edited")
            staged, size = work.file(token, data=data)
            work.put(replace(entry, staged=staged, size=size, mtime=int(time.time())))

    def import_paths(self, paths, domain, parent, *, collision="error", cancel=None, progress=None):
        from .codec import safe_path

        with self.mutation(progress=progress, cancel=cancel) as work:
            target = self.at(domain, parent)
            if not target or target.kind != "directory":
                raise BackupError(
                    "请先进入可添加项目的文件夹",
                    english="Open a folder where items can be added first",
                )
            for source in map(Path, paths):
                check_cancel(cancel)
                if source.is_symlink() or not (source.is_file() or source.is_dir()):
                    raise BackupError(
                        f"不支持导入此文件类型：{source.name}",
                        english="Importing this file type is not supported: {name}",
                        values={"name": source.name},
                    )
                safe_path(source.name, root=False)
                dest = f"{parent}/{source.name}".lstrip("/")
                existing = self.at(domain, dest)
                if existing:
                    if collision == "skip":
                        continue
                    if collision == "keep_both":
                        number, base = 2, dest
                        while self.at(domain, dest):
                            stem, ext = os.path.splitext(base)
                            dest = f"{stem} ({number}){ext}"
                            number += 1
                    elif collision == "replace":
                        for entry in self.subtree(existing):
                            work.remove(entry.token)
                    else:
                        raise BackupError(
                            f"目标已存在：{source.name}",
                            english="Destination already exists: {name}",
                            values={"name": source.name},
                        )
                todo = [(source, dest)]
                while todo:
                    check_cancel(cancel)
                    src, virtual = todo.pop()
                    if src.is_symlink() or not (src.is_dir() or src.is_file()):
                        raise BackupError(
                            f"不支持链接或特殊文件：{src.name}",
                            english="Links or special files are not supported: {name}",
                            values={"name": src.name},
                        )
                    safe_path(virtual)
                    is_dir = src.is_dir()
                    kind = "directory" if is_dir else "file"
                    baseline = self.baseline_at(domain, virtual)
                    previous = work.before_paths.get((domain, virtual))
                    if previous and previous.kind != kind:
                        previous = None
                    reuse = previous or (
                        baseline
                        if baseline and baseline.kind == kind and self.find(baseline.token) is None
                        else None
                    )
                    token = reuse.token if reuse else uuid.uuid4().hex
                    staged, size = (None, 0) if is_dir else work.file(token, source=src)
                    par, _, basename = virtual.rpartition("/")
                    work.put(
                        Entry(
                            token,
                            reuse.original_id if reuse else None,
                            domain,
                            virtual,
                            par,
                            basename,
                            kind,
                            size,
                            int(src.stat().st_mtime),
                            staged,
                            1,
                            1,
                        )
                    )
                    if progress:
                        progress(f"正在添加：{src.name}", 0, 0)
                    if is_dir:
                        todo.extend(
                            (child, virtual + "/" + child.name)
                            for child in sorted(src.iterdir(), reverse=True)
                        )

    def directory_mappings(self):
        return {
            (change.before.domain, change.before.path): change
            for change in self._pending.values()
            if change.before
            and change.after
            and change.before.kind == "directory"
            and change.before.path != change.after.path
        }

    def _mapped_baseline(self, base, excluded=(), mappings=None):
        mappings = self.directory_mappings() if mappings is None else mappings
        parent = base.parent
        while parent:
            owner = mappings.get((base.domain, parent))
            if owner and owner.token not in excluded:
                return relocated(base, owner.after.path + base.path[len(parent) :])
            parent = parent.rpartition("/")[0]
        return base

    def plan_revert(self, token=None, *, cancel=None):
        with self.lock:
            if token is None:
                updates = {key: change.before for key, change in self._pending.items()}
                reset = {
                    key for key, c in self._pending.items() if (c.after or c.before).kind == "file"
                }
                return RevertPlan(self.signature, updates, reset, {})
            change = self._pending.get(token)
            if change is None:
                return RevertPlan(self.signature, {}, set(), {})
            before, after = change.before, change.after
            mappings = self.directory_mappings()
            updates, reset, locations = {}, set(), {}
            if before is None and after.kind == "directory":
                affected = {entry.token for entry in self.subtree(after)}
                affected.update(
                    c.token
                    for c in self._pending.values()
                    if c.location.domain == after.domain and beneath(c.location.path, after.path)
                )
                for key in affected:
                    check_cancel(cancel)
                    base = self.baseline(key)
                    updates[key] = (
                        self._mapped_baseline(base, excluded=affected, mappings=mappings)
                        if base
                        else None
                    )
                    entry = self.find(key) or base
                    if entry and entry.kind == "file":
                        reset.add(key)
            elif before and after and before.kind == "directory" and before.path != after.path:
                restored = self._mapped_baseline(before, {token}, mappings)
                updates[token] = restored
                for entry in self.subtree(after):
                    if entry.token != token:
                        updates[entry.token] = relocated(
                            entry, restored.path + entry.path[len(after.path) :]
                        )
                for c in self._pending.values():
                    if (
                        c.after is None
                        and c.location.domain == after.domain
                        and beneath(c.location.path, after.path)
                    ):
                        updates[c.token] = None
                        locations[c.token] = relocated(
                            c.location, restored.path + c.location.path[len(after.path) :]
                        )
            elif before and after is None and before.kind == "directory":
                rows = self.db.execute(
                    "SELECT * FROM EditorBaseline WHERE domain=? AND (path=? OR substr(path,1,?)=?)",
                    (before.domain, before.path, len(before.path) + 1, before.path + "/"),
                )
                for row in rows:
                    check_cancel(cancel)
                    entry = Entry.row(row)
                    if self.find(entry.token) is None:
                        updates[entry.token] = self._mapped_baseline(entry, mappings=mappings)
                        if entry.kind == "file":
                            reset.add(entry.token)
            else:
                updates[token] = (
                    self._mapped_baseline(before, mappings=mappings) if before else None
                )
                if (after or before).kind == "file":
                    reset.add(token)
            # Restore missing baseline ancestors rather than leaving orphaned files.
            queue = list(updates.values())
            planned_paths = {(entry.domain, entry.path): entry for entry in queue if entry}
            while queue:
                check_cancel(cancel)
                entry = queue.pop()
                if entry is None or not entry.path:
                    continue
                parent = planned_paths.get((entry.domain, entry.parent))
                existing = self.at(entry.domain, entry.parent)
                if parent or (existing and existing.token not in updates):
                    if (parent or existing).kind != "directory":
                        raise BackupError(
                            "还原目标的父路径不是文件夹",
                            english="The parent of the restore destination is not a folder",
                        )
                    continue
                base_parent = self.baseline_at(entry.domain, entry.parent)
                if not base_parent or self.find(base_parent.token):
                    raise BackupError(
                        f"无法还原父文件夹：{entry.parent}",
                        english="Unable to restore parent folder: {path}",
                        values={"path": entry.parent},
                    )
                updates[base_parent.token] = base_parent
                planned_paths[(base_parent.domain, base_parent.path)] = base_parent
                queue.append(base_parent)
            for key, entry in updates.items():
                if entry:
                    occupant = self.at(entry.domain, entry.path)
                    if occupant and occupant.token != key and occupant.token not in updates:
                        raise BackupError(
                            f"还原路径已被占用：{full_path(entry)}；请先处理该路径上的更改"
                        )
            return RevertPlan(self.signature, updates, reset, locations)

    def revert(self, plan, *, progress=None, cancel=None):
        with self.mutation(progress=progress, cancel=cancel) as work:
            if plan.revision != self.signature:
                raise BackupError(
                    "更改列表已变化，请重新选择要放弃更改的项目",
                    english="The change list has changed; select the change to discard again",
                )
            for key in plan.updates:
                check_cancel(cancel)
                work.remove(key)
            for key, entry in plan.updates.items():
                check_cancel(cancel)
                if entry:
                    work.put(entry)
                elif key in plan.locations:
                    work.locations[key] = plan.locations[key]

    def discard(self, *, progress=None, cancel=None):
        self.check_changes_allowed()
        self.revert(self.plan_revert(cancel=cancel), progress=progress, cancel=cancel)

    def prepare_saved_index(self, path, cancel=None):
        import sqlite3

        from .codec import file_id

        db = sqlite3.connect(path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        try:
            self.create_entry_table(db)
            with db:
                cursor = self.db.execute("SELECT * FROM EditorEntries")
                while rows := cursor.fetchmany(1000):
                    check_cancel(cancel)
                    entries = [
                        replace(
                            Entry.row(row),
                            original_id=(
                                file_id(row["domain"], row["path"])
                                if row["changed"]
                                else row["original_id"]
                            )
                            if row["explicit"]
                            else None,
                            staged=None,
                            changed=0,
                        )
                        for row in rows
                    ]
                    db.executemany(
                        "INSERT INTO EditorEntries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (tuple(getattr(entry, column) for column in COLUMNS) for entry in entries),
                    )
                self.create_baseline(db)
            return db
        except BaseException:
            db.close()
            raise
