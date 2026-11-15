# 架构概览

PlanBak 是离线桌面应用，界面使用 PySide6。应用不需要连接设备；备份内容按需在本机读取和处理。

## 主要模块

| 模块 | 职责 |
| --- | --- |
| `app.py`、`backup_windows.py` | 主窗口、浏览与操作、后台任务、备份窗口及占用管理。 |
| `backup.py`、`backup_copy.py`、`codec.py` | 备份清单、数据提取、加密、备份副本和保存。 |
| `staging.py`、`pending_view.py` | 会话中的暂挂变更、回退和更改列表。 |
| `external_edits.py` | 外部编辑临时副本的登记、文件监视、变更检查和跟踪清理。 |
| `plist_editor.py`、`plist_xml.py` | plist 树和 XML 编辑。 |
| `sqlite_viewer.py`、`sqlite_database.py` | 只读 SQLite 查看和隔离快照。 |
| `hex_viewer.py`、`hex_data.py` | 十六进制查看和搜索。 |
| `image_session.py`、`image_viewer.py`、`image_cache.py`、`image_data.py`、`large_image.py` | 图片窗口、解码、共享缓存及大图区域读取。 |
| `i18n.py`、`translations/`、`appearance.py` | 语言和外观。 |

## 会话与后台工作

打开备份后，应用使用临时会话管理清单、缓存及尚未应用的更改；创建副本或应用更改时才写入持久备份。不同主窗口拥有各自会话，备份占用锁防止本应用的多个窗口或进程同时打开同一实际备份。保存边界见[备份与保存](backup-and-saving.md)。

读取、解码、导出和清理等操作使用后台任务。取消采用协作式检查，原生解码等步骤可能无法立即中断。关闭备份或退出时等待相关任务结束并清理会话工作区；异常退出后的失效工作区由后续启动清理。

外部编辑使用临时副本并监视变更；`ExternalEdits` 管理跟踪、轮询和变更暂挂，主窗口协调后台任务、退出确认及其他查看器的检查。检查失败时状态栏提示并延迟重试。拖出成功后的临时文件保留到关闭备份，以供接收程序读取。加密备份的临时工作区可能包含解密内容。

自动测试和手动验证的边界见[测试说明](testing.md)与[发布检查清单](release-checklist.md)。
