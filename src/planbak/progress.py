"""Stable progress phases with a temporary string-compatible callback value."""

from enum import Enum
from types import MappingProxyType


class ProgressPhase(Enum):
    READ = ("正在读取备份…", "Reading backup…")
    INDEX = ("正在建立备份索引…", "Building backup index…")
    COPY_BACKUP = ("正在创建备份副本…", "Creating backup copy…")
    EXPORT = ("正在导出：{name}", "Exporting: {name}")
    ESTIMATE = ("估算备份大小…", "Estimating backup size…")
    COPY_OUTPUT = ("复制备份到独立输出文件夹…", "Copying backup to output folder…")
    WRITE = ("写入：{name}", "Writing: {name}")
    VERIFY = ("重新封装并验证清单…", "Repacking and verifying manifest…")
    BASELINE = ("建立新的保存基线…", "Establishing the new save baseline…")
    APPLY = ("正在应用更改…", "Applying changes…")
    UPDATE_CHANGES = ("正在更新更改列表…", "Updating changes…")
    ADD = ("正在添加：{name}", "Adding: {name}")
    LOAD_IMAGE = ("正在加载文件：{name}", "Loading file: {name}")
    OPEN_FILE = ("正在打开文件…", "Opening file…")
    CLEANUP = ("正在清理旧会话缓存…", "Cleaning old session cache…")
    COPY_TO = ("正在复制项目到当前备份…", "Copying items to this backup…")


class ProgressMessage(str):
    """Carry semantic progress while preserving legacy three-argument callbacks."""

    def __new__(cls, phase, **values):
        obj = super().__new__(cls, phase.value[0].format(**values))
        obj.phase = phase
        obj.values = MappingProxyType(values)
        return obj

    def translate(self, language_context):
        return language_context.translate(self.phase.value[1], **self.values)
