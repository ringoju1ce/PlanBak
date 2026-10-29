"""按需读取应用PNG缩略图；限制输入、解码尺寸和会话LRU占用。"""

from collections import OrderedDict

from PySide6.QtCore import QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QIcon, QImageReader, QPixmap

ICON_EDGE = 128
ICON_CACHE_ENTRIES = 256  # 缩略图最多约16MiB；失败的查询也参与LRU。
MAX_ICON_BYTES = 4 * 1024**2
MAX_ICON_PIXELS = 4_000_000


class ApplicationIcons:
    def __init__(self):
        self.session_id = None
        self.applications = {}
        self.cache = OrderedDict()

    def set_session(self, session):
        session_id = session.session_id if session else None
        if session_id == self.session_id:
            return
        self.session_id = session_id
        applications = session.info.get("Applications", {}) if session else {}
        self.applications = applications if isinstance(applications, dict) else {}
        self.cache.clear()

    def for_directory(self, domain, path=""):
        if path or not domain or not domain.startswith("AppDomain-"):
            return None
        identifier = domain.removeprefix("AppDomain-")
        if identifier in self.cache:
            self.cache.move_to_end(identifier)
            return self.cache[identifier]
        icon = None
        metadata = self.applications.get(identifier)
        data = metadata.get("PlaceholderIcon") if isinstance(metadata, dict) else None
        if isinstance(data, bytes) and 0 < len(data) <= MAX_ICON_BYTES:
            buffer = QBuffer()
            buffer.setData(data)
            buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            reader = QImageReader(buffer, b"PNG")
            size = reader.size()
            if (
                size.width() > 0
                and size.height() > 0
                and size.width() * size.height() <= MAX_ICON_PIXELS
                and max(size.width(), size.height()) <= 8192
            ):
                if max(size.width(), size.height()) > ICON_EDGE:
                    reader.setScaledSize(
                        size.scaled(QSize(ICON_EDGE, ICON_EDGE), Qt.AspectRatioMode.KeepAspectRatio)
                    )
                image = reader.read()
                if not image.isNull():
                    icon = QIcon(QPixmap.fromImage(image))
        self.cache[identifier] = icon
        while len(self.cache) > ICON_CACHE_ENTRIES:
            self.cache.popitem(last=False)
        return icon
