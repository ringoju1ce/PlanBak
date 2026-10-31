"""Linux desktop-portal fallback when Qt's native scheme misses OS preferences."""

from PySide6.QtCore import SLOT, QObject, Qt, Slot
from PySide6.QtDBus import (
    QDBus,
    QDBusConnection,
    QDBusMessage,
    QDBusServiceWatcher,
    QDBusVariant,
)

SERVICE = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
INTERFACE = "org.freedesktop.portal.Settings"
NAMESPACE = "org.freedesktop.appearance"
KEY = "color-scheme"


def portal_scheme(value):
    # Legacy Read has two variant layers; ReadOne and notifications have one.
    for _ in range(4):
        if not isinstance(value, QDBusVariant):
            break
        value = value.variant()
    if type(value) is int:
        return {1: Qt.ColorScheme.Dark, 2: Qt.ColorScheme.Light}.get(value, Qt.ColorScheme.Unknown)
    return Qt.ColorScheme.Unknown


class PortalAppearance(QObject):
    def __init__(self, application, *, bus=None):
        super().__init__(application)
        self.hints = application.styleHints()
        self.bus = bus if bus is not None else QDBusConnection.sessionBus()
        self.overridden = False
        self.connected = (
            self.bus.connect(
                SERVICE,
                PATH,
                INTERFACE,
                "SettingChanged",
                self,
                SLOT("setting_changed(QString,QString,QDBusVariant)"),
            )
            if self.bus.isConnected()
            else False
        )
        self.watcher = QDBusServiceWatcher(
            SERVICE,
            self.bus,
            QDBusServiceWatcher.WatchModeFlag.WatchForOwnerChange,
            self,
        )
        self.watcher.serviceOwnerChanged.connect(self.owner_changed)
        self.refresh()

    def apply(self, scheme):
        if scheme == Qt.ColorScheme.Unknown:
            if self.overridden:
                self.overridden = False
                self.hints.unsetColorScheme()
        elif self.hints.colorScheme() != scheme:
            # This is application-local, never a write to desktop preferences.
            # Keep listening to the portal because an explicit Qt override no
            # longer follows native scheme notifications by itself.
            self.overridden = True
            self.hints.setColorScheme(scheme)

    def refresh(self):
        if not self.bus.isConnected():
            self.apply(Qt.ColorScheme.Unknown)
            return
        for method in ("ReadOne", "Read"):
            message = QDBusMessage.createMethodCall(SERVICE, PATH, INTERFACE, method)
            message.setArguments([NAMESPACE, KEY])
            reply = self.bus.call(message, QDBus.CallMode.Block, 500)
            if reply.type() == QDBusMessage.MessageType.ReplyMessage and reply.arguments():
                self.apply(portal_scheme(reply.arguments()[0]))
                return
            if reply.errorName() != "org.freedesktop.DBus.Error.UnknownMethod":
                break
        self.apply(Qt.ColorScheme.Unknown)

    @Slot(str, str, QDBusVariant)
    def setting_changed(self, namespace, key, value):
        if namespace == NAMESPACE and key == KEY:
            self.apply(portal_scheme(value))

    @Slot(str, str, str)
    def owner_changed(self, service, old_owner, new_owner):
        if new_owner:
            self.refresh()
        else:
            self.apply(Qt.ColorScheme.Unknown)
