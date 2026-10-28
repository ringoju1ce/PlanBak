"""Cancellable worker shared by read-only file viewers."""

import threading

from PySide6.QtCore import QThread


class ViewerTask(QThread):
    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function
        self.cancelled = threading.Event()
        self.result = None
        self.error = None

    def run(self):
        try:
            self.result = self.function(self.cancelled.is_set)
        except Exception as exc:
            self.error = exc
        finally:
            self.function = None
