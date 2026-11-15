import os

import pytest
from PySide6.QtCore import QObject, Qt, Signal

pytest.importorskip("PySide6.QtDBus")
from PySide6.QtDBus import QDBusConnection, QDBusMessage, QDBusVariant

import planbak.portal_appearance as module


class Hints:
    def __init__(self):
        self.scheme = Qt.ColorScheme.Light
        self.calls = []

    def colorScheme(self):
        return self.scheme

    def setColorScheme(self, scheme):
        self.calls.append(scheme)
        self.scheme = scheme

    def unsetColorScheme(self):
        self.calls.append(Qt.ColorScheme.Unknown)
        self.scheme = Qt.ColorScheme.Light


class Application(QObject):
    def __init__(self):
        super().__init__()
        self.hints = Hints()

    def styleHints(self):
        return self.hints


class Watcher(QObject):
    serviceOwnerChanged = Signal(str, str, str)

    class WatchModeFlag:
        WatchForOwnerChange = 1

    def __init__(self, *args):
        super().__init__(args[-1])


class Bus:
    def __init__(self, replies=(), connected=True):
        self.replies = list(replies)
        self.calls = []
        self.available = connected

    def isConnected(self):
        return self.available

    def connect(self, *args):
        self.subscription = args
        return True

    def call(self, message, mode, timeout):
        self.calls.append((message.member(), timeout))
        return self.replies.pop(0)


def reply(value):
    message = QDBusMessage.createMethodCall("org.example.Test", "/test", "org.example.Test", "Test")
    result = message.createReply()
    result.setArguments([value])
    return result


def error(name="org.freedesktop.DBus.Error.UnknownMethod"):
    message = QDBusMessage.createMethodCall("org.example.Test", "/test", "org.example.Test", "Test")
    return message.createErrorReply(name, "test only")


def controller(monkeypatch, bus):
    monkeypatch.setattr(module, "QDBusServiceWatcher", Watcher)
    app = Application()
    return app, module.PortalAppearance(app, bus=bus)


def test_portal_startup_and_live_dark_light_default(monkeypatch):
    app, c = controller(monkeypatch, Bus([reply(QDBusVariant(1))]))
    assert app.hints.scheme == Qt.ColorScheme.Dark and c.connected
    c.setting_changed(module.NAMESPACE, module.KEY, QDBusVariant(2))
    assert app.hints.scheme == Qt.ColorScheme.Light
    c.setting_changed(module.NAMESPACE, module.KEY, QDBusVariant(1))
    assert app.hints.scheme == Qt.ColorScheme.Dark
    c.setting_changed(module.NAMESPACE, module.KEY, QDBusVariant(0))
    assert app.hints.calls[-1] == Qt.ColorScheme.Unknown and not c.overridden


def test_legacy_read_and_nested_variants(monkeypatch):
    bus = Bus([error(), reply(QDBusVariant(QDBusVariant(1)))])
    app, c = controller(monkeypatch, bus)
    assert app.hints.scheme == Qt.ColorScheme.Dark
    assert bus.calls == [("ReadOne", 500), ("Read", 500)]


@pytest.mark.parametrize("value", [0, 3, "1", True, None])
def test_invalid_portal_values_do_not_force_dark(monkeypatch, value):
    app, c = controller(monkeypatch, Bus([reply(QDBusVariant(value))]))
    assert app.hints.scheme == Qt.ColorScheme.Light and not app.hints.calls


def test_missing_portal_and_owner_restart_preserve_native_fallback(monkeypatch):
    app, c = controller(monkeypatch, Bus(connected=False))
    assert not c.connected and not app.hints.calls
    c.bus = Bus([reply(QDBusVariant(1)), reply(QDBusVariant(2))])
    c.owner_changed(module.SERVICE, "", ":1.10")
    assert app.hints.scheme == Qt.ColorScheme.Dark
    c.owner_changed(module.SERVICE, ":1.10", "")
    assert not c.overridden and app.hints.calls[-1] == Qt.ColorScheme.Unknown
    c.owner_changed(module.SERVICE, "", ":1.11")
    assert app.hints.scheme == Qt.ColorScheme.Light


def test_portal_errors_and_unrelated_notifications(monkeypatch):
    bus = Bus([error("org.freedesktop.DBus.Error.NoReply")])
    app, c = controller(monkeypatch, bus)
    assert app.hints.scheme == Qt.ColorScheme.Light and not c.overridden
    assert bus.calls == [("ReadOne", 500)]
    c.setting_changed("org.example.Other", module.KEY, QDBusVariant(1))
    c.setting_changed(module.NAMESPACE, "accent-color", QDBusVariant(1))
    assert not app.hints.calls


def test_actual_dbus_signal_delivers_live_scheme_changes(qtbot, monkeypatch):
    service = f"org.planbak.AppearanceTest.p{os.getpid()}"
    bus = QDBusConnection.connectToBus(
        QDBusConnection.BusType.SessionBus, "planbak-appearance-test"
    )
    if not bus.isConnected():
        pytest.skip("No session bus available")
    monkeypatch.setattr(module, "SERVICE", service)
    monkeypatch.setattr(module.PortalAppearance, "refresh", lambda self: None)
    assert bus.registerService(service)
    app = Application()
    c = module.PortalAppearance(app, bus=bus)
    try:
        assert c.connected
        for value, expected in [
            (1, Qt.ColorScheme.Dark),
            (2, Qt.ColorScheme.Light),
            (1, Qt.ColorScheme.Dark),
            (0, Qt.ColorScheme.Light),
        ]:
            message = QDBusMessage.createSignal(module.PATH, module.INTERFACE, "SettingChanged")
            message.setArguments([module.NAMESPACE, module.KEY, QDBusVariant(value)])
            assert bus.send(message)
            qtbot.waitUntil(lambda: app.hints.scheme == expected)
    finally:
        bus.unregisterService(service)
        c.deleteLater()
        app.deleteLater()
        QDBusConnection.disconnectFromBus("planbak-appearance-test")
