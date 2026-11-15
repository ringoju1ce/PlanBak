"""根据有界文件头选择内置查看器，不依赖文件名。"""

from pathlib import Path
from xml.parsers import expat

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImageReader

from .codec import BackupError
from .staging import checked_staged_path

HEADER_BYTES = 64 * 1024


def source_revision(session, token):
    """调用方持有会话锁；与提取时选择当前文件的规则一致。"""
    entry = session.get(token)
    path = checked_staged_path(entry) if entry.staged else session.cache_path(token)
    if not path.is_file():
        if not entry.original_id:
            raise BackupError("文件源已不可用。", english="The file source is no longer available.")
        path = session.payload_path(entry.original_id)
    stat = path.stat()
    return (
        str(session.root),
        str(path),
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_ino,
    )


def xml_root(header):
    class Found(Exception):
        pass

    root = None
    parser = expat.ParserCreate()

    def start(name, attrs):
        nonlocal root
        root = name
        raise Found

    def reject(*args):
        raise ValueError("不解析实体声明")

    parser.StartElementHandler = start
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = lambda *args: 0
    try:
        parser.Parse(header, False)
    except Found:
        return root
    except (expat.ExpatError, ValueError):
        pass
    return None


def plist_header(header):
    return header.startswith(b"bplist00") or xml_root(header) == "plist"


def image_header(header):
    # 常见格式直接检查签名，不要求 Qt 为每一种格式安装读取插件。
    if header.startswith(
        (
            b"\x89PNG\r\n\x1a\n",
            b"\xff\xd8\xff",
            b"GIF87a",
            b"GIF89a",
            b"II*\0",
            b"MM\0*",
            b"II+\0",
            b"MM\0+",
            b"BM",
            b"icns",
            b"\0\0\1\0",
            b"\0\0\2\0",
        )
    ):
        return True
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return True
    # Qt 的缓冲区读取器只接触已经读取的文件头，不能按扩展名猜测。
    buffer = QBuffer()
    buffer.setData(QByteArray(header))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    reader.setDecideFormatFromContent(True)
    if reader.canRead() and bytes(reader.format()).lower() != b"pdf":
        return True
    # HEIF/AVIF 由 Pillow/libheif 解码，不要求 Qt 装有对应插件。
    if len(header) >= 16 and header[4:8] == b"ftyp":
        length = int.from_bytes(header[:4], "big")
        if length < 16 or length > len(header):
            return False
        brands = [header[8:12]] + [header[i : i + 4] for i in range(16, length, 4)]
        return any(
            b in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"avif", b"avis"}
            for b in brands
        )
    return False


# 顺序即识别优先级。新增查看器在此登记识别器，并在主窗口登记分派函数。
# 纯文本编辑器尚未实现；未知内容保持交给系统应用。
HEADER_READERS = (
    ("sqlite", lambda header: header.startswith(b"SQLite format 3\0")),
    ("plist", plist_header),
    ("image", image_header),
)


def identify_file(path):
    with Path(path).open("rb") as stream:
        header = stream.read(HEADER_BYTES)
    return next((kind for kind, accepts in HEADER_READERS if accepts(header)), "external")
