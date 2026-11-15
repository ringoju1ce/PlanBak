import threading

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QApplication

from planbak.codec import BackupError, Cancelled
from planbak.hex_data import (
    PAGE_SIZE,
    SEARCH_CHUNK,
    parse_offset,
    parse_pattern,
    read_page,
    search_bytes,
)
from planbak.hex_viewer import HexViewer


def test_hex_parsing_and_invalid_input():
    assert parse_offset("0xFF", 300) == 255
    assert parse_offset("255", 300) == 255
    assert parse_offset("0", 0) == 0
    for text in ("-1", "300", "1.2", "junk"):
        with pytest.raises(BackupError):
            parse_offset(text, 300)
    assert parse_pattern("00 fF 12") == b"\x00\xff\x12"
    assert parse_pattern("中文", False) == "中文".encode()
    for text in ("", "FF0", "GG", "00" * 4097):
        with pytest.raises(BackupError):
            parse_pattern(text)


@pytest.mark.parametrize("backwards", [False, True])
def test_hex_streaming_search_crosses_chunks_and_wraps(tmp_path, backwards):
    path = tmp_path / "binary"
    needle = b"\x00\xff\x01\xfe"
    data = bytearray(b"a" * (SEARCH_CHUNK * 2 + 10))
    positions = [5, PAGE_SIZE - 2, SEARCH_CHUNK - 2, SEARCH_CHUNK * 2 - 1]
    for position in positions:
        data[position : position + len(needle)] = needle
    path.write_bytes(data)
    for position in positions:
        result = search_bytes(path, needle, position, backwards=backwards)
        assert result.offset == position and not result.wrapped
    result = search_bytes(path, needle, -1 if backwards else len(data), backwards=backwards)
    assert result.offset == (positions[-1] if backwards else positions[0]) and result.wrapped
    assert search_bytes(path, b"not-present", backwards=backwards).offset is None
    event = threading.Event()
    event.set()
    with pytest.raises(Cancelled):
        search_bytes(path, needle, backwards=backwards, cancel=event.is_set)


def test_hex_sparse_large_file_reads_single_page_and_handles_empty(qtbot, tmp_path):
    path = tmp_path / "large"
    with path.open("wb") as f:
        f.seek(5 * 1024**3)
        f.write(b"\xff")
    assert len(read_page(path, 0)) == PAGE_SIZE
    viewer = HexViewer(path, "large")
    qtbot.addWidget(viewer)
    viewer.show()
    assert viewer.model.rowCount() == 256
    viewer.address.setText("0x140000000")
    viewer.jump()
    assert viewer.model.rowCount() == 1 and viewer.selected_bytes() == b"\xff"
    assert viewer.model.index(0, 0).data() == "140000000"
    assert not viewer.next_button.isEnabled()
    viewer.close()
    empty = tmp_path / "empty"
    empty.touch()
    viewer = HexViewer(empty, "empty")
    qtbot.addWidget(viewer)
    assert viewer.model.rowCount() == 0
    assert not viewer.next_button.isEnabled() and not viewer.previous_button.isEnabled()
    viewer.close()


def test_hex_selection_copy_search_and_shortcuts(qtbot, tmp_path):
    path = tmp_path / "file"
    data = b"0123456789abcdef" * 400 + b"UTF-8:" + "中文".encode()
    path.write_bytes(data)
    viewer = HexViewer(path, "file")
    qtbot.addWidget(viewer)
    viewer.show()
    viewer.table.setCurrentIndex(viewer.model.index(0, 1))
    viewer.table.selectionModel().select(
        viewer.model.index(0, 2), QItemSelectionModel.SelectionFlag.Select
    )
    viewer.copy_hex()
    assert QApplication.clipboard().text() == "30 31"
    viewer.copy_bytes()
    assert bytes(QApplication.clipboard().mimeData().data("application/octet-stream")) == b"01"
    viewer.copy_offset()
    assert QApplication.clipboard().text() == "0x0"
    viewer.activateWindow()
    qtbot.waitUntil(viewer.isActiveWindow)
    qtbot.keyClick(viewer, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert viewer.search_input.hasFocus()
    viewer.search_mode.setCurrentIndex(1)
    viewer.search_input.setText("中文")
    viewer.find_next()
    qtbot.waitUntil(lambda: viewer.task is None)
    assert viewer.match == (6406, 6)
    assert viewer.selected_bytes() == "中文".encode()
    assert (
        viewer.model.data(viewer.table.currentIndex(), Qt.ItemDataRole.BackgroundRole) is not None
    )
    viewer.go(14, select=True)
    viewer.search_input.setText("ef0123")  # crosses a row
    viewer.find_next()
    qtbot.waitUntil(lambda: viewer.task is None)
    assert viewer.match == (14, 6) and viewer.selected_bytes() == b"ef0123"
    viewer.find_next(backwards=True)
    qtbot.waitUntil(lambda: viewer.task is None)
    assert viewer.match[0] > 14
    assert viewer.status.text() == viewer.language_context.translate(
        "Match at 0x{offset:X} · {count} bytes · Search wrapped",
        offset=viewer.match[0], count=viewer.match[1],
    )
    viewer.table.setFocus()
    qtbot.keyClick(viewer.table, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert viewer.selected_bytes() == viewer.model.data_bytes
    viewer.close()


def test_hex_close_cancels_background_search(qtbot, tmp_path, monkeypatch):
    import time

    import planbak.hex_viewer as module

    path = tmp_path / "file"
    path.write_bytes(b"abc")
    viewer = HexViewer(path, "file")
    qtbot.addWidget(viewer)
    viewer.show()

    def search(*args, cancel, **kwargs):
        while not cancel():
            time.sleep(0.001)
        raise Cancelled()

    monkeypatch.setattr(module, "search_bytes", search)
    viewer.search_input.setText("00")
    viewer.find_next()
    viewer.close()
    qtbot.waitUntil(lambda: viewer.task is None and not viewer.isVisible(), timeout=2000)


def test_hex_search_match_spanning_chunk_boundary(tmp_path, monkeypatch):
    import planbak.hex_data as module

    monkeypatch.setattr(module, "SEARCH_CHUNK", 32)
    path = tmp_path / "boundary"
    path.write_bytes(b"a" * 30 + b"NEEDLE" + b"z" * 30)
    assert search_bytes(path, b"NEEDLE", 0).offset == 30
    assert search_bytes(path, b"NEEDLE", 65, backwards=True).offset == 30
