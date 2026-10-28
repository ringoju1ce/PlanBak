"""Isolated SQLite snapshots and bounded, read-only queries. No backup writes."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .codec import BackupError, check_cancel

SQLITE_HEADER = b"SQLite format 3\0"
PAGE_SIZE = 200


def is_sqlite(path):
    with Path(path).open("rb") as stream:
        return stream.read(16) == SQLITE_HEADER


def companions(session, token):
    entry = session.get(token)
    return [entry] + [
        other
        for suffix in ("-wal", "-journal")
        if (other := session.at(entry.domain, entry.path + suffix)) is not None
        and other.kind == "file"
    ]


@dataclass
class DatabaseSnapshot:
    directory: Path
    path: Path
    fingerprint: bytes
    warning: str = ""


def prepare_snapshot(session, token, directory, *, cancel=None, source=None):
    """Copy current main/WAL into a private folder, then merge via SQLite backup.

    SHM is transient coordination state and is regenerated locally. Never use
    immutable on the source: doing so could silently omit WAL transactions.
    """
    from .backup import copy_file, digest

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    incoming = directory / "input"
    incoming.mkdir()
    main = incoming / "database.sqlite"
    fingerprint = hashlib.sha256()
    warning = ""
    try:
        entry = session.get(token)
        if source is not None:
            copy_file(source, main, cancel)
        else:
            session.extract_file(token, main, cancel=cancel)
        if not is_sqlite(main):
            raise BackupError("此文件不是标准 SQLite，可能使用其他格式或应用自身的加密。")
        files = companions(session, token)
        for other in files[1:]:
            suffix = other.path[len(entry.path) :]
            if suffix == "-journal" and other.size:
                raise BackupError("数据库含非空回滚日志，暂不支持此恢复状态；请导出项目后处理。")
            session.extract_file(
                other.token, incoming / ("database.sqlite" + suffix), cancel=cancel
            )
        for suffix in ("", "-wal", "-journal"):
            path = incoming / ("database.sqlite" + suffix)
            # Empty sidecars do not affect the database contents.
            if path.exists() and path.stat().st_size:
                fingerprint.update(suffix.encode())
                fingerprint.update(digest(path, cancel=cancel))
        with main.open("rb") as stream:
            header = stream.read(20)
        if header[18:20] == b"\x02\x02" and not any(e.path.endswith("-wal") for e in files):
            warning = "此数据库使用 WAL 模式，备份中未包含配套日志；只能显示现有主文件中的内容。"
        output = directory / "snapshot.sqlite"
        deadline = time.monotonic() + 30

        def progress(status, remaining, total):
            check_cancel(cancel)
            if time.monotonic() > deadline:
                raise BackupError("数据库快照读取超时，可能存在不完整的日志。")

        # The input is an isolated copy, so recovery/SHM cannot affect the backup.
        with closing(sqlite3.connect(main.as_uri() + "?mode=ro", uri=True, timeout=1)) as src:
            with closing(sqlite3.connect(output)) as dst:
                src.backup(dst, pages=256, progress=progress, sleep=0.01)
        return DatabaseSnapshot(directory, output, fingerprint.digest(), warning)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(incoming, ignore_errors=True)


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def connect_readonly(path, cancel):
    connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 4 * 1024 * 1024)
    connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 1024)
    connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
    deadline = time.monotonic() + 30
    connection.set_progress_handler(
        lambda: int(bool(cancel and cancel()) or time.monotonic() > deadline), 1000
    )
    return connection


def schema(path, cancel=None):
    connection = connect_readonly(path, cancel)
    try:
        return connection.execute(
            "SELECT name,type,sql,tbl_name FROM sqlite_schema "
            "WHERE type IN ('table','view','index') ORDER BY type,name"
        ).fetchall()
    finally:
        connection.close()


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    more: bool


def readonly_authorizer(action, arg1, arg2, database, origin):
    # FTS5 initializes its read-only connection with this harmless pragma.
    if action == sqlite3.SQLITE_PRAGMA and arg1 == "data_version" and arg2 is None:
        return sqlite3.SQLITE_OK
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
    if action not in allowed:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in {
        "load_extension",
        "readfile",
        "writefile",
        "fts3_tokenizer",
    }:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def query(path, sql, *, offset=0, cancel=None):
    if offset < 0:
        raise BackupError("无效的分页位置")
    connection = connect_readonly(path, cancel)
    try:
        connection.set_authorizer(readonly_authorizer)
        cursor = connection.execute(sql)
        if cursor.description is None:
            raise BackupError("只支持返回结果的只读查询")
        # Preserve arbitrary SELECT/CTE syntax without rewriting user SQL.
        remaining = offset
        while remaining:
            check_cancel(cancel)
            skipped = cursor.fetchmany(min(remaining, PAGE_SIZE))
            if not skipped:
                break
            remaining -= len(skipped)
        rows = []
        size = 0
        for _ in range(PAGE_SIZE + 1):
            check_cancel(cancel)
            row = cursor.fetchone()
            if row is None:
                break
            rows.append(row)
            size += sum(len(value) if isinstance(value, (str, bytes)) else 8 for value in row)
            if size > 16 * 1024 * 1024:
                raise BackupError("此页数据过大；请缩小查询范围或选择较少的列。")
        return QueryResult(
            [item[0] for item in cursor.description], rows[:PAGE_SIZE], len(rows) > PAGE_SIZE
        )
    finally:
        connection.close()
