"""Follow Qt's platform palette; derive custom colors from the effective palette."""

import sys

from PySide6.QtGui import QColor, QPalette

from .item_style import ItemViewStyle


def configure_system_appearance(application):
    # Keep Qt's native palette; Linux adds a portal scheme bridge below when
    # native hints miss desktop preferences. Explicit palette roles mask updates.
    application.setStyle(ItemViewStyle("Fusion"))
    application.styleHints().unsetColorScheme()
    if sys.platform.startswith("linux"):
        try:
            from .portal_appearance import PortalAppearance
        except ImportError:
            # QtDBus is optional on installations/platforms without D-Bus.
            return
        application._portal_appearance = PortalAppearance(application)


def sync_background(widget, source, role):
    """Refresh a custom Window background without pinning other palette roles."""
    palette = widget.palette()
    groups = (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
        QPalette.ColorGroup.Disabled,
    )
    if all(
        palette.color(group, QPalette.ColorRole.Window) == source.color(group, role)
        for group in groups
    ):
        return
    for group in groups:
        palette.setColor(group, QPalette.ColorRole.Window, source.color(group, role))
    widget.setPalette(palette)


def is_dark(palette):
    return palette.color(QPalette.ColorRole.Base).lightness() < 128


def xml_colors(palette):
    if is_dark(palette):
        return ("#80bfff", "#e6b673", "#83c794", "#a0b19f")
    return ("#175ca3", "#a04310", "#39733b", "#687468")


def marked_background(palette, *, error=False):
    base = palette.color(QPalette.ColorRole.Base)
    accent = QColor("#e34f55" if error else "#f0be1e")
    amount = 0.30 if is_dark(palette) else 0.20
    return QColor(
        *(
            round((1 - amount) * channel + amount * highlight)
            for channel, highlight in zip(base.getRgb()[:3], accent.getRgb()[:3])
        )
    )
