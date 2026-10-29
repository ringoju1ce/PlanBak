"""Backup-owned image windows; no extraction work runs on the GUI thread."""

import shutil

from PySide6.QtCore import Qt

from .image_cache import ImageCache
from .image_data import DISPLAY_BUDGET, image_candidate
from .image_viewer import ImageViewer


class ImageWindows:
    def __init__(self, owner):
        self.owner = owner
        self.viewers = []
        self.cache = None

    def clear_cache(self):
        if self.cache is not None:
            self.cache.clear()
            self.cache = None

    def open(self, token, prepared=None):
        owner, session = self.owner, self.owner.session
        matches = [v for v in self.viewers if v.current_token == token and not v._closing]
        if matches:
            viewer = max(matches, key=lambda v: v.last_active)
            if viewer.isMinimized():
                viewer.setWindowState(viewer.windowState() & ~Qt.WindowState.WindowMinimized)
            viewer.show()
            viewer.raise_()
            viewer.activateWindow()
            viewer.canvas.setFocus()
            if prepared:
                shutil.rmtree(prepared.parent, ignore_errors=True)
            return viewer
        items = [
            (row.entry.token, row.name)
            for row in owner.model.rows
            if row.kind == "file" and (row.entry.token == token or image_candidate(row.name))
        ]
        if not any(item[0] == token for item in items):
            # A generic content detector can finish after selection changes.
            # Never infer a different folder's sequence in that case.
            items = [(token, session.get(token).name)]
        if self.cache is None or self.cache.session is not session:
            self.clear_cache()
            self.cache = ImageCache(session)
        cache = self.cache

        def load(current, cancel, *, force=False):
            try:
                return cache.load(current, cancel, force=force)
            finally:
                # Generic content detection may already have extracted a file.
                # Re-read the session for a version-checked cache entry rather
                # than associating an older snapshot with a newer source stamp.
                if prepared:
                    shutil.rmtree(prepared.parent, ignore_errors=True)

        def accept(viewer, image, region=False):
            allocations = {image.cacheKey(): image.sizeInBytes()}
            for other in self.viewers:
                fields = []
                if other is not viewer or region:
                    fields.append(other.canvas.image)
                if other is not viewer:
                    fields.append(other.canvas.region)
                for current in fields:
                    if not current.isNull():
                        allocations[current.cacheKey()] = current.sizeInBytes()
            return sum(allocations.values()) <= DISPLAY_BUDGET

        viewer = ImageViewer(items, token, load, accept, owner)
        self.viewers.append(viewer)

        def closed(_):
            self.viewers.remove(viewer)
            if prepared:
                shutil.rmtree(prepared.parent, ignore_errors=True)
            if not viewer._skip_prompt:
                owner.restore_browser_after_editor(session, viewer)
            viewer.deleteLater()

        viewer.finished.connect(closed)
        viewer.show()
        viewer.canvas.setFocus()
        return viewer
