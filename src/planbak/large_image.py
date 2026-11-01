"""Streaming previews and disk-tiled, viewport-sized reads for large rasters."""

import math
import threading
from contextlib import contextmanager

import pyvips
from PySide6.QtGui import QImage

from .codec import BackupError, check_cancel

pyvips.cache_set_max(0)  # Do not retain file handles outside session ownership.
pyvips.cache_set_max_mem(32 * 1024**2)
pyvips.vips_lib.vips_concurrency_set(2)

PREVIEW_EDGE = 1600
MAX_PIXELS = 1_000_000_000
MAX_EDGE = 1_000_000
MAX_REGION_PIXELS = 8_000_000
MAX_TILE_BYTES = 256 * 1024**2


def to_qimage(image):
    if image.get_typeof("icc-profile-data"):
        image = image.icc_transform("srgb", embedded=True)
    image = image.colourspace("srgb").cast("uchar", shift=True)
    if image.bands == 3:
        image = image.bandjoin(255)
    if image.bands != 4:
        raise BackupError(
            "无法转换此图片的颜色通道。", english="Unable to convert this image’s color channels."
        )
    data = image.write_to_memory()
    return QImage(
        data, image.width, image.height, image.width * 4, QImage.Format.Format_RGBA8888
    ).copy()


def monitor(image, cancel, capacity=None):
    stopped = [False]
    image.set_progress(True)

    def progress(sender, *_):
        if (cancel and cancel()) or (capacity is not None and not capacity()):
            stopped[0] = True
            # The image owns its callback; capturing it here would form a
            # reference cycle and delay releasing native buffers/file handles.
            sender.set_kill(True)

    image.signal_connect("eval", progress)
    return stopped


class LargeLease:
    def __init__(self, source):
        self.source = None
        with source.state_lock:
            if source.closed:
                raise BackupError(
                    "大图缓存已回收，请重新加载。",
                    english="The large image cache was released; reload the image.",
                )
            source.users += 1
            self.source = source

    def release(self):
        source = getattr(self, "source", None)
        if source is not None:
            with source.state_lock:
                source.users -= 1
            self.source = None

    def __del__(self):
        self.release()


class LargeImage:
    def __init__(self, path, width, height):
        self.path = path
        self.tiles = path.with_name(path.name + ".tiles.tif")
        self.width, self.height = width, height
        self.users = 0
        self.closed = False
        self.lock = threading.RLock()
        self.state_lock = threading.Lock()
        self.capacity = lambda: True
        self.format = "图片"

    def lease(self):
        return LargeLease(self)

    @contextmanager
    def locked(self, cancel):
        while not self.lock.acquire(timeout=0.05):
            check_cancel(cancel)
        try:
            check_cancel(cancel)
            if self.closed:
                raise BackupError(
                    "大图缓存已回收，请重新加载。",
                    english="The large image cache was released; reload the image.",
                )
            yield
        finally:
            self.lock.release()

    def disk_size(self):
        try:
            return self.tiles.stat().st_size
        except FileNotFoundError:
            return 0

    def prepare(self, cancel):
        if self.tiles.exists():
            return
        image = pyvips.Image.new_from_file(str(self.path), access="sequential")
        # Preserve the orientation tag while streaming the raw scan order.
        # Apply rotation only when reading the random-access tiled TIFF.
        self.write_tiles(image, cancel)

    def write_tiles(self, image, cancel):
        stopped = monitor(
            image, cancel, lambda: self.disk_size() <= MAX_TILE_BYTES and self.capacity()
        )
        try:
            image.tiffsave(
                str(self.tiles),
                tile=True,
                tile_width=256,
                tile_height=256,
                compression="deflate",
                bigtiff=True,
            )
            check_cancel(cancel)
            if self.disk_size() > MAX_TILE_BYTES or not self.capacity():
                raise BackupError(
                    "大图分块缓存超过空间预算，请关闭部分图片窗口后重试。",
                    english="The large image tile cache exceeds its space budget; close some image windows and try again.",
                )
        except Exception as exc:
            self.tiles.unlink(missing_ok=True)
            check_cancel(cancel)
            if stopped[0]:
                raise BackupError(
                    "大图分块缓存超过空间预算。",
                    english="The large image tile cache exceeds its space budget.",
                ) from exc
            raise

    def preview(self, cancel):
        from .image_data import DECODE_SLOTS

        while not DECODE_SLOTS.acquire(timeout=0.05):
            check_cancel(cancel)
        try:
            with self.locked(cancel):
                path = self.tiles if self.tiles.exists() else self.path
                image = pyvips.Image.thumbnail(
                    str(path), PREVIEW_EDGE, height=PREVIEW_EDGE, size="down"
                )
                monitor(image, cancel)
                result = to_qimage(image)
                check_cancel(cancel)
                return result
        finally:
            DECODE_SLOTS.release()

    def render(self, rect, output, cancel):
        lease = self.lease()
        try:
            return self._render(rect, output, cancel)
        finally:
            lease.release()

    def _render(self, rect, output, cancel):
        from .image_data import DECODE_SLOTS

        while not DECODE_SLOTS.acquire(timeout=0.05):
            check_cancel(cancel)
        try:
            with self.locked(cancel):
                self.prepare(cancel)
                x, y, width, height = rect
                target_w, target_h = output
                factor = min(1.0, math.sqrt(MAX_REGION_PIXELS / max(1, target_w * target_h)))
                target_w, target_h = (
                    max(1, round(target_w * factor)),
                    max(1, round(target_h * factor)),
                )
                image = pyvips.Image.new_from_file(str(self.tiles), access="random").autorot()
                image = image.crop(x, y, width, height).resize(
                    target_w / width, vscale=target_h / height
                )
                monitor(image, cancel)
                result = to_qimage(image)
                check_cancel(cancel)
                return result
        finally:
            DECODE_SLOTS.release()

    def close(self):
        with self.state_lock:
            if self.users:
                return False
            self.closed = True
        self.tiles.unlink(missing_ok=True)
        return True


def decode_large(path, cancel, format_name=""):
    if format_name.upper() in ("HEIF", "HEIC"):
        return decode_heif(path, cancel)
    # Sequential access keeps long PNG screenshots streaming, rather than
    # decompressing a full random-access raster into a large memory buffer.
    image = pyvips.Image.new_from_file(str(path), access="sequential").autorot()
    width, height = image.width, image.height
    loader = image.get("vips-loader") if image.get_typeof("vips-loader") else ""
    if any(kind in loader for kind in ("heif", "gif", "webp")) and width * height > 64_000_000:
        raise BackupError(
            "此图片编码需要整帧缓冲，超过 6400 万像素的解码内存保护范围。",
            english="This image encoding requires a full-frame buffer and exceeds the 64-million-pixel decoding memory limit.",
        )
    if width * height > MAX_PIXELS or max(width, height) > MAX_EDGE:
        raise BackupError(
            "图片尺寸超出大图读取范围（10 亿像素或单边 100 万像素）。",
            english="The image exceeds the large-image limit (one billion pixels or one million pixels per side).",
        )
    preview = pyvips.Image.thumbnail(str(path), PREVIEW_EDGE, height=PREVIEW_EDGE, size="down")
    monitor(preview, cancel)
    result = to_qimage(preview)
    check_cancel(cancel)
    return result, LargeImage(path, width, height)


def decode_heif(path, cancel):
    # The bundled libvips has a HEIF reader but not necessarily a HEVC decoder.
    # Use pillow-heif's bundled decoder and a zero-copy memoryview bridge.
    import pillow_heif

    source = pillow_heif.open_heif(path, convert_hdr_to_8bit=True, remove_stride=True)
    width, height = source.size
    if width * height > 64_000_000:
        raise BackupError(
            "HEIC/HEIF 需要整帧缓冲，超过 6400 万像素的解码内存保护范围。",
            english="HEIC/HEIF requires a full-frame buffer and exceeds the 64-million-pixel decoding memory limit.",
        )
    check_cancel(cancel)
    data = source.data
    check_cancel(cancel)
    width, height = source.size
    if width * height > 64_000_000:
        raise BackupError(
            "HEIC/HEIF 解码后的尺寸超过整帧内存保护范围。",
            english="The decoded HEIC/HEIF image exceeds the full-frame memory limit.",
        )
    bands = 4 if source.has_alpha else 3
    image = pyvips.Image.new_from_memory(data, width, height, bands, "uchar").copy(
        interpretation="srgb"
    )
    profile = source.info.get("icc_profile")
    if profile:
        image.set_type(pyvips.GValue.blob_type, "icc-profile-data", profile)
    # HEIF EXIF orientation is descriptive; libheif applies normative irot/imir.
    # Do not autorotate again based on the EXIF tag.
    large = LargeImage(path, width, height)
    try:
        preview = image.thumbnail_image(PREVIEW_EDGE, height=PREVIEW_EDGE, size="down")
        monitor(preview, cancel)
        result = to_qimage(preview)
        large.write_tiles(image, cancel)
        check_cancel(cancel)
        return result, large
    except Exception:
        large.close()
        raise
