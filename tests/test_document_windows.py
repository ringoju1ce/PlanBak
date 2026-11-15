import plistlib

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QMainWindow
from test_sqlite_viewer import make_database

from planbak.app import PasswordDialog
from planbak.dialogs import FixedDialog
from planbak.hex_viewer import HexViewer
from planbak.plist_editor import PlistDocument, PlistEditor
from planbak.sqlite_database import DatabaseSnapshot
from planbak.sqlite_viewer import SQLiteViewer


@pytest.mark.parametrize("kind", ["plist", "sqlite", "hex"])
def test_document_window_has_no_transient_parent_and_main_can_activate(qtbot, tmp_path, kind):
    main = QMainWindow()
    qtbot.addWidget(main)
    main.show()
    if kind == "plist":
        document = PlistEditor(
            PlistDocument(plistlib.dumps({"key": "value"})), "file.plist", lambda _: True, main
        )
    elif kind == "sqlite":
        path = tmp_path / "data.db"
        make_database(path)
        document = SQLiteViewer(DatabaseSnapshot(tmp_path, path, b""), "data.db", main)
    else:
        path = tmp_path / "data.bin"
        path.write_bytes(b"test")
        document = HexViewer(path, "data.bin", main)
    qtbot.addWidget(document)
    document.show()
    if kind == "sqlite":
        qtbot.waitUntil(lambda: document.task is None)
    qtbot.waitExposed(document)
    assert document.parentWidget() is None
    assert document.windowType() == Qt.WindowType.Window
    assert document.windowHandle().transientParent() is None
    assert document.windowModality() == Qt.WindowModality.NonModal
    assert not document.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
    assert document.minimumSize() != document.maximumSize()
    size = document.size()
    document.resize(size + QSize(100, 100))
    assert document.size() == size + QSize(100, 100)
    assert document.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint
    document.activateWindow()
    qtbot.waitUntil(document.isActiveWindow)
    main.activateWindow()
    qtbot.waitUntil(main.isActiveWindow)
    assert document.isVisible()
    document.activateWindow()
    qtbot.waitUntil(document.isActiveWindow)
    document.close()
    main.close()


def test_confirmation_prompts_keep_parent_relationship_and_modality(qtbot):
    main = QMainWindow()
    qtbot.addWidget(main)
    main.show()
    password = PasswordDialog(main)
    qtbot.addWidget(password)
    password.setModal(True)
    password.show()
    qtbot.waitExposed(password)
    assert password.parentWidget() is main
    assert password.windowType() == Qt.WindowType.Dialog
    assert password.windowHandle().transientParent() is main.windowHandle()
    assert password.windowModality() == Qt.WindowModality.WindowModal
    child = FixedDialog(password)
    qtbot.addWidget(child)
    assert child.parentWidget() is password
    password.reject()
    main.close()
