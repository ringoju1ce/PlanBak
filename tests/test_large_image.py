import threading

import pytest
import pyvips
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication
from test_ui import choose, window  # noqa: F401

from planbak.codec import BackupError, Cancelled
from planbak.image_data import decode_image


def long_png(path, width=1200, height=30000):
    # Construct and save lazily: fixtures must not allocate the full raster either.
    mask = (pyvips.Image.xyz(width, height)[1] % 600) > 300
    mask.ifthenelse([30, 90, 150], [230, 230, 230]).cast("uchar").pngsave(str(path))
    return path


def test_long_image_over_32mp_preview_and_exact_viewport(tmp_path):
    path = long_png(tmp_path / "long.png", height=60000)  # 72MP
    result = decode_image(path)
    assert result.large and result.width == 1200 and result.height == 60000
    assert result.image.width() * result.image.height() <= 1600**2
    image = result.large.render((0, 55000, 500, 800), (500, 800), lambda: False)
    assert image.width() == 500 and image.height() == 800
    assert image.pixelColor(10, 10).name() == "#1e5a96"
    assert result.large.tiles.exists()
    assert result.large.disk_size() < 10 * 1024**2
    tile_stamp = result.large.tiles.stat().st_mtime_ns
    again = result.large.render((100, 200, 300, 400), (300, 400), lambda: False)
    assert again.width() == 300 and result.large.tiles.stat().st_mtime_ns == tile_stamp
    result.large.close()
    assert not result.large.tiles.exists()


def test_region_cancel_and_tile_budget_leave_no_partial_file(tmp_path, monkeypatch):
    import planbak.large_image as module

    path = long_png(tmp_path / "budget.png", height=10000)
    result = decode_image(path)
    with pytest.raises(Cancelled):
        result.large.render((0, 0, 100, 100), (100, 100), lambda: True)
    monkeypatch.setattr(module, "MAX_TILE_BYTES", 1)
    with pytest.raises(BackupError, match="缓存"):
        result.large.render((0, 0, 100, 100), (100, 100), lambda: False)
    assert not result.large.tiles.exists()
    result.large.close()


def test_large_view_pan_zoom_cache_reload_and_exit_cleanup(request, qtbot, tmp_path):
    w = request.getfixturevalue("window")
    path = long_png(tmp_path / "screen.png", height=10000)
    w.session.import_paths([path], "HomeDomain", "Documents")
    w.domain, w.path = "HomeDomain", "Documents"
    w.refresh()
    choose(w, "screen.png")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.image_viewers))
    v = w.image_viewers[0]
    qtbot.waitUntil(lambda: v.task is None)
    cache = w.images.cache
    assert v.canvas.large and v.canvas.full_height == 10000
    assert v.canvas.fitting and v.canvas.region.isNull()
    provider = v.canvas.large
    v.canvas.zoom(1)
    qtbot.waitUntil(lambda: v.region_task is None and not v.canvas.region.isNull(), timeout=10000)
    assert v.canvas.region.width() <= v.canvas.width() + 2
    assert v.canvas.region.height() <= v.canvas.height() + 2
    assert provider.tiles.exists()
    v.fit_button.click()
    assert v.canvas.region.isNull()
    v.actual_button.click()
    qtbot.waitUntil(lambda: v.region_task is None and not v.canvas.region.isNull(), timeout=10000)
    v.canvas.pan += QPointF(0, -500)
    v.canvas.clamp_pan()
    v.canvas.view_changed.emit()
    qtbot.waitUntil(lambda: v.region_task is None and not v._region_timer.isActive())
    # Memory LRU eviction may remove the preview, but an active provider's
    # shared file/lock identity must survive a subsequent cache miss.
    cache.decoded.clear()
    result = cache.load(v.current_token)
    assert result.large is provider
    result.lease = None
    v.reload()
    qtbot.waitUntil(lambda: v.task is None)
    assert v.canvas.large is not provider and provider.closed
    assert not provider.tiles.exists()
    directory = cache.directory
    w.force_close = True
    w.close()
    qtbot.waitUntil(lambda: not w.image_viewers and not w.isVisible())
    assert not directory.exists() and not cache.decoded


def test_close_waits_for_region_worker_without_blocking_gui(request, qtbot, tmp_path, monkeypatch):
    from planbak.large_image import LargeImage

    w = request.getfixturevalue("window")
    path = long_png(tmp_path / "close.png", height=10000)
    w.session.import_paths([path], "HomeDomain", "Documents")
    w.domain, w.path = "HomeDomain", "Documents"
    w.refresh()
    choose(w, "close.png")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.image_viewers))
    v = w.image_viewers[0]
    qtbot.waitUntil(lambda: v.task is None)
    started = threading.Event()

    def slow(self, rect, output, cancel):
        started.set()
        while not cancel():
            threading.Event().wait(0.001)
        raise Cancelled()

    monkeypatch.setattr(LargeImage, "render", slow)
    v.canvas.zoom(1)
    qtbot.waitUntil(started.is_set)
    root = w.session.workspace
    w.force_close = True
    w.close()
    QApplication.processEvents()
    qtbot.waitUntil(lambda: not w.isVisible() and not w.image_viewers)
    assert not root.exists()


@pytest.mark.parametrize("format_name", ["JPEG", "HEIF"])
def test_large_photo_decode_and_detail_use_backend(format_name, tmp_path):
    from PIL import Image, ImageCms

    path = tmp_path / ("photo.heic" if format_name == "HEIF" else "photo.jpg")
    exif = Image.Exif()
    exif[274] = 6
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", (3200, 2600), "blue").save(
        path, format=format_name, exif=exif, icc_profile=profile
    )
    result = decode_image(path)
    expected = (3200, 2600) if format_name == "HEIF" else (2600, 3200)
    assert result.large and (result.width, result.height) == expected
    region = result.large.render((50, 60, 200, 300), (200, 300), lambda: False)
    assert region.pixelColor(20, 20).blue() > 240
    result.large.close()


def test_progress_callback_does_not_keep_native_image_alive():
    import sys

    from planbak.large_image import monitor

    image = pyvips.Image.black(2, 2)
    # pyvips.Image does not support weakrefs. An image captured by its own
    # callback adds a Python reference and survives reference-count teardown.
    baseline = sys.getrefcount(image)
    monitor(image, lambda: False)
    assert sys.getrefcount(image) == baseline
