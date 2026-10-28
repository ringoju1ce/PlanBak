"""Application style: preserve focus everywhere except item-view cell painting."""

from PySide6.QtWidgets import QProxyStyle, QStyle, QStyleOptionViewItem


class ItemViewStyle(QProxyStyle):
    def drawControl(self, element, option, painter, widget=None):
        if element == QStyle.ControlElement.CE_ItemViewItem and isinstance(
            option, QStyleOptionViewItem
        ):
            option = QStyleOptionViewItem(option)
            # Keep the actual current index and selection state. Only suppress
            # the native pale current-cell rectangle, including in future views.
            option.state &= ~QStyle.StateFlag.State_HasFocus
        super().drawControl(element, option, painter, widget)
