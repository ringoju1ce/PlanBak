"""可编辑副本标记与独立复制；不复制会话明文或暂挂内容。"""

import json
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .codec import BackupError, check_cancel

MARKER = ".backupeditor.json"


def read_marker(root):
    path = Path(root) / MARKER
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return None
        if value.get("application") != "BackupEditor" or type(value.get("version")) is not int:
            return None
        if value["version"] != 1 or str(uuid.UUID(value["copy_id"])) != value["copy_id"]:
            return None
        created = datetime.fromisoformat(value["created_utc"])
        if created.utcoffset() is None:
            return None
        if not isinstance(value["creator_version"], str) or not value["creator_version"]:
            return None
        return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def write_marker(root):
    value = {
        "application": "BackupEditor",
        "version": 1,
        "copy_id": str(uuid.uuid4()),
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "creator_version": __version__,
    }
    path = Path(root) / MARKER
    path.unlink(missing_ok=True)  # Never follow a copied marker symlink.
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return value


def backup_stamp(root):
    return tuple(
        (name, (root / name).stat().st_size, (root / name).stat().st_mtime_ns)
        for name in ("Manifest.db", "Manifest.plist", "Info.plist")
    )


def output_location(source, destination):
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise BackupError("保存目标已存在，请选择一个新的文件夹名称")
    destination = destination.parent.resolve() / destination.name
    if destination == source or source in destination.parents or destination in source.parents:
        raise BackupError("保存位置不能与原备份重叠")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


@dataclass(frozen=True)
class CopiedBackup:
    source: Path
    stamp: tuple
    path: Path


def copy_backup(source, destination, stamp, progress, cancel=None):
    from .backup import copy_file

    destination = output_location(source, destination)
    if backup_stamp(source) != stamp:
        raise BackupError("原备份在打开后已改变，请重新打开备份")
    progress("正在创建备份副本…", 0, 0)
    files, total = [], 0
    for path in source.rglob("*"):
        check_cancel(cancel)
        if (path.is_file() or path.is_symlink()) and path.name != MARKER:
            files.append(path)
            total += path.stat().st_size
    if shutil.disk_usage(destination.parent).free < total:
        raise BackupError("目标磁盘空间不足以创建备份副本")
    work = Path(tempfile.mkdtemp(prefix=".backupeditor-save-", dir=destination.parent))
    done = 0

    def advanced(amount):
        nonlocal done
        done += amount
        progress("正在创建备份副本…", done, max(1, total))

    try:
        progress("正在创建备份副本…", 0, max(1, total))
        for path in files:
            check_cancel(cancel)
            copy_file(path, work / path.relative_to(source), cancel, progress=advanced)
        check_cancel(cancel)
        if backup_stamp(source) != stamp or backup_stamp(work) != stamp:
            raise BackupError("复制期间原备份发生变化，副本未发布")
        write_marker(work)
        if destination.exists() or destination.is_symlink():
            raise BackupError("保存目标已被创建，未覆盖现有文件夹")
        work.rename(destination)
        return CopiedBackup(source, stamp, destination)
    finally:
        if work.exists():
            shutil.rmtree(work)
