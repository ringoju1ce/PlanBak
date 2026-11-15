import threading
import time

import pytest
from PIL import Image
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QImage, QNativeGestureEvent, QPointingDevice, QWheelEvent
from PySide6.QtWidgets import QApplication
from test_ui import choose, window  # noqa: F401, F811

from planbak.codec import BackupError, Cancelled
from planbak.image_data import DecodedImage, decode_image, is_image
from planbak.image_viewer import ImageViewer


def make_image(path, size=(64, 48), color="red", **kwargs):
    Image.new("RGB", size, color).save(path, **kwargs)
    return path


@pytest.mark.parametrize("extension", ["png", "jpeg", "webp", "gif", "tiff", "heic", "heif"])
def test_decode_actual_formats_and_content_recognition(tmp_path, extension):
    path = tmp_path / f"image.{extension}"
    make_image(path, format="HEIF" if extension in ("heic", "heif") else extension)
    result = decode_image(path)
    assert (result.image.width(), result.image.height()) == (64, 48)
    assert result.image.pixelColor(10, 10).red() > 240
    anonymous = tmp_path / "no-extension"
    anonymous.write_bytes(path.read_bytes())
    assert is_image(anonymous)
    assert decode_image(anonymous).image.size() == result.image.size()


def test_decode_orientation_primary_heif_and_limits(tmp_path, monkeypatch):
    import planbak.image_data as module

    jpeg = tmp_path / "rotation.jpg"
    exif = Image.Exif()
    exif[274] = 6
    make_image(jpeg, (24, 16), exif=exif)
    image = decode_image(jpeg).image
    assert (image.width(), image.height()) == (16, 24)
    heif = tmp_path / "primary.heic"
    Image.new("RGB", (20, 10), "red").save(
        heif,
        format="HEIF",
        append_images=[Image.new("RGB", (30, 15), "blue")],
        primary_index=1,
        quality=-1,
    )
    image = decode_image(heif).image
    assert image.width() == 30 and image.pixelColor(5, 5).blue() > 240
    monkeypatch.setattr(module, "MAX_PIXELS", 10)
    with pytest.raises(BackupError, match="上限"):
        decode_image(jpeg)
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a picture")
    with pytest.raises(BackupError, match="无法识别"):
        decode_image(broken)
    with pytest.raises(Cancelled):
        decode_image(jpeg, lambda: True)


def standalone(qtbot, count=3, loader=None):
    image = QImage(1400, 1000, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.red)
    viewer = ImageViewer(
        [(str(i), f"{i}.png") for i in range(count)],
        "1" if count > 1 else "0",
        loader or (lambda token, cancel, **kwargs: DecodedImage(image, "PNG")),
        lambda *_: True,
    )
    qtbot.addWidget(viewer)
    viewer.show()
    viewer.activateWindow()
    qtbot.waitUntil(lambda: viewer.task is None and viewer.isActiveWindow())
    return viewer


def wheel(canvas, delta=120, *, inverted=False, control=False, pixels=0):
    point = QPointF(canvas.rect().center())
    event = QWheelEvent(
        point,
        QPointF(canvas.mapToGlobal(point.toPoint())),
        QPoint(0, pixels),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.ControlModifier if control else Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        inverted,
    )
    QApplication.sendEvent(canvas, event)


def test_touchpad_scroll_pans_without_navigating(qtbot):
    v = standalone(qtbot)
    wheel(v.canvas, 0, pixels=-100)
    assert v.index == 1 and v.canvas.pan == QPointF()
    v.canvas.zoom(2)
    wheel(v.canvas, 0, pixels=-100)
    assert v.index == 1 and v.canvas.pan.y() == -100
    for _ in range(30):
        wheel(v.canvas, 0, pixels=-100)
    assert v.index == 1
    v.close()


def test_window_chrome_fit_manual_resize_slider_and_navigation(qtbot):
    v = standalone(qtbot)
    assert v.parentWidget() is None
    for flag in (Qt.WindowType.WindowMinimizeButtonHint, Qt.WindowType.WindowMaximizeButtonHint):
        assert v.windowFlags() & flag
    assert v.minimumSize() != v.maximumSize()
    original = v.canvas.scale
    v.showMaximized()
    qtbot.waitUntil(v.isMaximized)
    assert v.canvas.fitting and v.canvas.scale >= original
    v.showNormal()
    v.canvas.zoom(2)
    v.resize(800, 600)
    QApplication.processEvents()
    assert v.canvas.scale == 2 and not v.canvas.fitting
    v.actual_button.click()
    assert v.canvas.scale == 1 and not v.canvas.fitting
    v.zoom_slider.setValue(100)
    assert v.canvas.scale == 2
    v.next_button.click()
    qtbot.waitUntil(lambda: v.task is None)
    assert v.index == 2 and v.canvas.fitting and not v.next_button.isEnabled()
    v.navigate(1)
    assert v.index == 2
    v.close()


def test_wheel_natural_direction_accumulation_ctrl_zoom_and_native_pinch(qtbot):
    v = standalone(qtbot)
    wheel(v.canvas, 120, inverted=False)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.index == 0
    wheel(v.canvas, -120, inverted=True)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.index == 1
    # The same logical up delta must stay up even if marked as system-inverted.
    wheel(v.canvas, 120, inverted=True)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.index == 0
    for _ in range(3):
        wheel(v.canvas, -30)
    assert v.index == 0
    wheel(v.canvas, -30)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.index == 1
    wheel(v.canvas, 240)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.index == 0
    wheel(v.canvas, 0)
    assert v.index == 0
    wheel(v.canvas, -120)
    qtbot.waitUntil(lambda: v.task is None)
    original = v.canvas.scale
    wheel(v.canvas, 120, control=True)
    assert v.index == 1 and v.canvas.scale > original and not v.canvas.fitting
    before = v.canvas.scale
    event = QNativeGestureEvent(
        Qt.NativeGestureType.ZoomNativeGesture,
        QPointingDevice.primaryPointingDevice(),
        2,
        QPointF(100, 100),
        QPointF(100, 100),
        QPointF(100, 100),
        0.25,
        QPointF(),
    )
    QApplication.sendEvent(v.canvas, event)
    assert v.canvas.scale == pytest.approx(before * 1.25)
    v.close()


def test_windows_only_active_input_and_pan(qtbot):
    first, second = standalone(qtbot), standalone(qtbot)
    first.activateWindow()
    first.canvas.setFocus()
    qtbot.waitUntil(first.isActiveWindow)
    second_index = second.index
    wheel(second.canvas, -120)
    assert second.index == second_index
    qtbot.keyClick(first.canvas, Qt.Key.Key_Right)
    qtbot.waitUntil(lambda: first.task is None)
    assert first.index == 2 and second.index == second_index
    first.canvas.zoom(2)
    before = QPointF(first.canvas.pan)
    qtbot.mousePress(first.canvas, Qt.MouseButton.LeftButton, pos=QPoint(150, 150))
    qtbot.mouseMove(first.canvas, QPoint(200, 180))
    qtbot.mouseRelease(first.canvas, Qt.MouseButton.LeftButton, pos=QPoint(200, 180))
    assert first.canvas.pan != before
    first.close()
    second.close()


def test_latest_request_wins_and_close_waits_for_worker(qtbot):
    started, release = threading.Event(), threading.Event()
    calls = []

    def load(token, cancel, **kwargs):
        calls.append(token)
        if len(calls) == 1:
            started.set()
            release.wait(2)
        result = QImage(2, 2, QImage.Format.Format_RGBA8888)
        result.fill(Qt.GlobalColor.red if token == "0" else Qt.GlobalColor.blue)
        return DecodedImage(result, "PNG")

    v = ImageViewer(
        [("0", "red.png"), ("1", "blue.png"), ("2", "end.png")], "0", load, lambda *_: True
    )
    qtbot.addWidget(v)
    v.show()
    qtbot.waitUntil(started.is_set)
    v.navigate(1)
    v.navigate(1)
    release.set()
    qtbot.waitUntil(lambda: v.task is None)
    assert calls == ["0", "2"] and v.canvas.image.pixelColor(0, 0).blue() == 255
    assert v.current_token == "2"

    def slow(token, cancel, **kwargs):
        while not cancel():
            time.sleep(0.001)
        raise Cancelled()

    v.loader = slow
    v.reload()
    before = time.monotonic()
    v.close()
    assert time.monotonic() - before < 0.2
    qtbot.waitUntil(lambda: v.task is None and not v.isVisible())


def add_pictures(w, tmp_path):
    paths = [
        make_image(tmp_path / f"{i}.png", color=color)
        for i, color in enumerate(("red", "blue", "green"))
    ]
    directory = tmp_path / "0-folder"
    directory.mkdir()
    text = tmp_path / "0.txt"
    text.write_text("not an image")
    w.session.import_paths(paths + [directory, text], "HomeDomain", "Documents")
    w.domain, w.path = "HomeDomain", "Documents"
    w.refresh()
    return paths


def test_main_open_sorted_sequence_current_image_reuse_and_independent_windows(
    request, qtbot, tmp_path
):
    w = request.getfixturevalue("window")
    add_pictures(w, tmp_path)
    baseline = list(w.session.changes)
    choose(w, "1.png")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.image_viewers))
    assert w.task is None and w.progress_dialog is None
    a = w.image_viewers[0]
    qtbot.waitUntil(lambda: a.task is None)
    assert [name for _, name in a.items] == ["0.png", "1.png", "2.png"]
    assert a.index == 1
    a.navigate(-1)
    qtbot.waitUntil(lambda: a.task is None)
    a.showMaximized()
    a.showMinimized()
    choose(w, "0.png")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.image_viewers))
    qtbot.waitUntil(lambda: not a.isMinimized())
    assert a.isMaximized()
    a.showNormal()
    assert w.image_viewers == [a]
    choose(w, "1.png")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.image_viewers))
    b = w.image_viewers[-1]
    qtbot.waitUntil(lambda: b.task is None)
    assert a is not b and a.index == 0 and b.index == 1
    b.navigate(-1)
    qtbot.waitUntil(lambda: b.task is None)
    a.activateWindow()
    qtbot.waitUntil(a.isActiveWindow)
    choose(w, "0.png")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.image_viewers))
    qtbot.waitUntil(a.isActiveWindow)
    assert len(w.image_viewers) == 2
    assert list(w.session.changes) == baseline
    assert w.images.cache.decoded
    assert any(w.images.cache.directory.iterdir())
    root = w.session.workspace
    w.force_close = True
    w.close()
    qtbot.waitUntil(lambda: not w.image_viewers and not w.isVisible())
    assert not root.exists()


def test_reload_reads_current_staged_bytes_and_bad_picture_keeps_navigation(
    request, qtbot, tmp_path
):
    w = request.getfixturevalue("window")
    paths = add_pictures(w, tmp_path)
    choose(w, "1.png")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.image_viewers))
    v = w.image_viewers[0]
    qtbot.waitUntil(lambda: v.task is None)
    make_image(paths[1], color="yellow")
    w.session.stage_edit(v.current_token, paths[1])
    v.reload()
    qtbot.waitUntil(lambda: v.task is None)
    assert v.canvas.image.pixelColor(1, 1).red() == 255
    assert v.canvas.image.pixelColor(1, 1).green() == 255
    token = v.items[2][0]
    paths[2].write_bytes(b"broken")
    w.session.stage_edit(token, paths[2])
    v.navigate(1)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.canvas.image.isNull() and v.language_context.translate(
        "Unable to load image: {error}", error=""
    ).strip() in v.status.text()
    v.navigate(-1)
    qtbot.waitUntil(lambda: v.task is None)
    assert not v.canvas.image.isNull()
    v.close()


def test_slider_wheel_uses_same_navigation_and_ctrl_zoom(qtbot):
    v = standalone(qtbot)
    scale = v.canvas.scale
    wheel(v.zoom_slider, -120)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.index == 2 and v.canvas.fitting
    wheel(v.zoom_slider, 120, control=True)
    assert v.index == 2 and v.canvas.scale > scale and not v.canvas.fitting
    v.close()


def test_backup_payload_decryption_and_switch_wait_for_image_worker(
    request, qtbot, tmp_path, backup_root, monkeypatch
):
    from conftest import PASSWORD, make_backup

    from planbak.backup import BackupSession

    w = request.getfixturevalue("window")
    def fail_message(*args, **kwargs):
        raise AssertionError(f"Unexpected dialog: {args[1:3]}")
    monkeypatch.setattr("planbak.app.message", fail_message)
    original = (backup_root / "Manifest.db").read_bytes()
    writer = BackupSession(backup_root, PASSWORD)
    path = make_image(tmp_path / "payload.heic", format="HEIF")
    writer.import_paths([path], "HomeDomain", "Documents")
    output = tmp_path / "saved"
    writer.save(output)
    writer.close()
    w.session.close()
    w.session = BackupSession(output, PASSWORD)
    w.domain, w.path = "HomeDomain", "Documents"
    w.refresh()
    choose(w, "payload.heic")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.image_viewers))
    v = w.image_viewers[0]
    qtbot.waitUntil(lambda: v.task is None)
    assert v.canvas.image.width() == 64 and not w.session.dirty
    assert (backup_root / "Manifest.db").read_bytes() == original
    import planbak.image_cache as module

    started = threading.Event()

    def slow(path, cancel):
        started.set()
        while not cancel():
            time.sleep(0.001)
        raise Cancelled()

    monkeypatch.setattr(module, "decode_image", slow)
    v.reload()
    qtbot.waitUntil(started.is_set)
    old_workspace = w.session.workspace
    target = make_backup(tmp_path / "next")
    w.confirm_leave(lambda: w.open_path(target))
    qtbot.waitUntil(lambda: not w.image_viewers and w.task is None and w.session.root == target)
    assert not old_workspace.exists()
    assert not list(w.session.workspace.glob("image-viewer/*"))


def test_extensionless_content_and_display_budget(request, qtbot, tmp_path, monkeypatch):
    import planbak.image_session as module

    w = request.getfixturevalue("window")
    image = make_image(tmp_path / "anonymous", format="PNG")
    w.session.import_paths([image], "HomeDomain", "Documents")
    w.domain, w.path = "HomeDomain", "Documents"
    w.refresh()
    choose(w, "anonymous")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.image_viewers))
    qtbot.waitUntil(lambda: w.task is None and len(w.image_viewers) == 1)
    v = w.image_viewers[0]
    qtbot.waitUntil(lambda: v.task is None)
    assert v.canvas.image.width() == 64
    monkeypatch.setattr(module, "DISPLAY_BUDGET", 1)
    v.reload()
    qtbot.waitUntil(lambda: v.task is None)
    assert v.canvas.image.isNull() and "内存" in v.status.text()
    v.close()


def test_navigation_and_closed_window_reuse_session_cache_until_exit(
    request, qtbot, tmp_path, monkeypatch
):
    import planbak.image_cache as module

    w = request.getfixturevalue("window")
    add_pictures(w, tmp_path)
    extracted, decoded = [], []
    original_extract, original_decode = w.session.extract_file, module.decode_image

    def extract(token, *args, **kwargs):
        extracted.append(token)
        return original_extract(token, *args, **kwargs)

    def decode(path, cancel):
        decoded.append(path)
        return original_decode(path, cancel)

    monkeypatch.setattr(w.session, "extract_file", extract)
    monkeypatch.setattr(module, "decode_image", decode)
    choose(w, "1.png")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.image_viewers))
    v = w.image_viewers[0]
    qtbot.waitUntil(lambda: v.task is None)
    v.navigate(-1)
    qtbot.waitUntil(lambda: v.task is None)
    v.navigate(1)
    qtbot.waitUntil(lambda: v.task is None)
    cache = w.images.cache
    token = v.current_token
    cached_path = cache.files[token].path
    assert token not in extracted and decoded.count(cached_path) == 1
    v.close()
    qtbot.waitUntil(lambda: not w.image_viewers)
    assert cache.decoded and cache.directory.exists()
    choose(w, "1.png")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.image_viewers))
    qtbot.waitUntil(lambda: w.image_viewers[0].task is None)
    assert token not in extracted and decoded.count(cached_path) == 1
    w.force_close = True
    w.close()
    qtbot.waitUntil(lambda: not w.isVisible() and not w.image_viewers)
    assert not cache.decoded and not cache.files and not cache.directory.exists()
    assert w.images.cache is None


def test_loop_toggle_wraps_buttons_keys_wheel_and_can_be_disabled(qtbot):
    v = standalone(qtbot)
    assert not v.loop_toggle.isChecked()
    assert v.actual_button.text() == v.language_context.translate("Actual Size")
    v.navigate(1)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.index == 2 and not v.next_button.isEnabled()
    v.loop_toggle.setChecked(True)
    assert v.previous_button.isEnabled() and v.next_button.isEnabled()
    v.next_button.click()
    qtbot.waitUntil(lambda: v.task is None)
    assert v.index == 0
    v.canvas.setFocus()
    qtbot.keyClick(v.canvas, Qt.Key.Key_Left)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.index == 2
    wheel(v.canvas, -120)
    qtbot.waitUntil(lambda: v.task is None)
    assert v.index == 0
    v.loop_toggle.setChecked(False)
    assert not v.previous_button.isEnabled()
    v.navigate(-1)
    assert v.index == 0
    v.close()


def test_loop_is_window_local_and_reopening_resets_it(qtbot):
    a, b = standalone(qtbot), standalone(qtbot)
    a.loop_toggle.setChecked(True)
    assert not b.loop_toggle.isChecked()
    a.close()
    c = standalone(qtbot, count=1)
    assert not c.loop_toggle.isChecked()
    c.loop_toggle.setChecked(True)
    assert not c.previous_button.isEnabled() and not c.next_button.isEnabled()
    c.navigate(1)
    assert c.index == 0
    b.close()
    c.close()


def test_prefetch_direction_and_cancel_on_close(qtbot):
    calls = []
    image = QImage(32, 32, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)

    def loader(token, cancel, **kwargs):
        calls.append(token)
        return DecodedImage(image, "PNG")

    v = ImageViewer([(str(i), f"{i}.png") for i in range(5)], "2", loader,
                    lambda *_: True, with_progress=True)
    qtbot.addWidget(v)
    v.show()
    qtbot.waitUntil(lambda: calls == ["2", "3", "1"] and v.prefetch_task is None)
    v.navigate(1)
    qtbot.waitUntil(lambda: calls == ["2", "3", "1", "3", "4"] and v.prefetch_task is None)
    v.close()


def test_cache_miss_notification_only_for_uncached_image(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from planbak.image_cache import ImageCache

    cache = ImageCache(SimpleNamespace(workspace=tmp_path))
    monkeypatch.setattr(cache, "source", lambda *args: (object(), ("revision",)))
    hit = object()
    monkeypatch.setattr(cache, "_hit", lambda *args: hit)
    notices = []
    assert cache.load("image", on_miss=lambda: notices.append(True)) is hit
    assert notices == []
    monkeypatch.setattr(cache, "_hit", lambda *args: None)
    monkeypatch.setattr(cache, "_load", lambda *args: hit)
    assert cache.load("image", on_miss=lambda: notices.append(True)) is hit
    assert notices == [True]
