from PySide6.QtCore import QObject

from planbak.transfers import OutgoingTransfers


def test_leases_release_once_and_defer_close_until_last_transfer(qtbot):
    class Session:
        def __init__(self):
            self.tokens = set()

        def begin_transfer(self):
            token = object()
            self.tokens.add(token)
            return token

        def end_transfer(self, token):
            self.tokens.remove(token)

    session = Session()
    owner = QObject()
    manager = OutgoingTransfers(owner, lambda: session, lambda: True)
    events = []
    manager.status_started.connect(lambda token: events.append(("start", token)))
    manager.status_finished.connect(lambda token: events.append(("finish", token)))
    manager.close_ready.connect(lambda: events.append(("close", None)))
    first, second = manager.acquire(), manager.acquire()
    first.show_status()
    first.show_status()
    manager.defer_close()
    first.release()
    first.release()
    assert manager.active == 1 and session.tokens == {second.token}
    assert events == [("start", first.token), ("finish", first.token)]
    second.release()
    assert manager.active == 0 and not session.tokens
    assert events[-1] == ("close", None)
