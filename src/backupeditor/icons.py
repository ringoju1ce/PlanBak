"""Session-scoped application icons from backup metadata, decoded on demand."""

from PySide6.QtGui import QIcon, QPixmap


class ApplicationIcons:
    def __init__(self):
        self.session_id = None
        self.applications = {}
        self.cache = {}

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
        if identifier not in self.cache:
            icon = None
            metadata = self.applications.get(identifier)
            data = metadata.get("PlaceholderIcon") if isinstance(metadata, dict) else None
            if isinstance(data, bytes) and data:
                pixmap = QPixmap()
                if pixmap.loadFromData(data, "PNG"):
                    icon = QIcon(pixmap)
            self.cache[identifier] = icon
        return self.cache[identifier]
