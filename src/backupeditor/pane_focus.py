"""Explicit browser-pane ownership, independent of transient menu/dialog focus."""

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import QApplication


class BrowserPaneFocus(QObject):
    changed = Signal()

    def __init__(self, tree, files, parent=None):
        super().__init__(parent)
        self.tree, self.files = tree, tuple(files)
        self.owner = "files"
        self.preferred_view = self.files[0]
        self.generation = 0
        self.stopped = False
        self.watched = {}
        for view in (tree, *self.files):
            for widget in (view, view.viewport()):
                self.watched[widget] = view
                widget.installEventFilter(self)
            # Keep each selection for navigation, but show only the active pane
            # with the theme's selected highlight, including while a menu is open.
            view.setStyleSheet(
                view.styleSheet()
                + """
                QAbstractItemView[activeBrowserPane="false"]::item:selected {
                    background-color: palette(midlight);
                    color: palette(text);
                }
            """
            )
        QApplication.instance().focusChanged.connect(self.focus_changed)
        self.update_visuals()

    @property
    def sidebar_active(self):
        return self.owner == "tree"

    def update_visuals(self):
        for view in (self.tree, *self.files):
            active = self.sidebar_active if view is self.tree else not self.sidebar_active
            view.setProperty("activeBrowserPane", active)
            view.style().unpolish(view)
            view.style().polish(view)
            view.viewport().update()

    def activate(self, view, *, focus=False, explicit=True):
        if self.stopped:
            return
        if explicit:
            self.preferred_view = view
        owner = "tree" if view is self.tree else "files"
        if self.owner != owner:
            self.owner = owner
            self.generation += 1
            self.update_visuals()
            self.changed.emit()
        if focus:
            view.setFocus(
                Qt.FocusReason.MouseFocusReason if explicit else Qt.FocusReason.OtherFocusReason
            )

    def focus_changed(self, old, new):
        self.generation += 1
        if self.stopped:
            return
        view = self.view_for_widget(new)
        if view is not None:
            self.activate(view, explicit=False)

    def view_for_widget(self, widget):
        while widget is not None:
            if widget is self.tree or widget in self.files:
                return widget
            widget = widget.parentWidget()
        return None

    def eventFilter(self, watched, event):
        if not self.stopped and watched in self.watched:
            if event.type() == QEvent.Type.MouseButtonPress:
                self.generation += 1
                # Some platform focus policies allow a row to be selected without
                # giving its view keyboard focus. A click must choose both.
                self.activate(self.watched[watched], focus=True)
            elif event.type() == QEvent.Type.FocusIn:
                explicit = event.reason() in (
                    Qt.FocusReason.MouseFocusReason,
                    Qt.FocusReason.TabFocusReason,
                    Qt.FocusReason.BacktabFocusReason,
                    Qt.FocusReason.ShortcutFocusReason,
                )
                self.activate(self.watched[watched], explicit=explicit)
        return super().eventFilter(watched, event)
