"""Bounded still-image decoding. Workers return QImage, never GUI pixmaps."""

from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore

from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener
from PySide6.QtGui import QColorSpace, QImage, QImageReader

from .codec import BackupError, check_cancel
from .image_metadata import read_metadata

register_heif_opener(thumbnails=False)
Image.init()

MAX_FILE_BYTES = 128 * 1024**2
# A fallback guard if a decoder expands beyond its header dimensions. Normal
# rasters enter the streaming path much earlier; this is not the support limit.
MAX_PIXELS = 32_000_000
LARGE_PIXEL_THRESHOLD = 8_000_000
LARGE_EDGE_THRESHOLD = 8192
DISPLAY_BUDGET = 256 * 1024**2
DECODE_SLOTS = BoundedSemaphore(2)
IMAGE_SUFFIXES = frozenset(
    "." + suffix
    for suffix in (
        "bmp cur gif icns ico jfif jpeg jpg pbm pgm png ppm svg svgz tga tif tiff "
        "wbmp webp xbm xpm heic heif hif avif"
    ).split()
)


def image_candidate(name):
    return Path(name).suffix.lower() in IMAGE_SUFFIXES


def is_image(path):
    """Content recognition for generically opened files, without decoding pixels."""
    try:
        with Image.open(path):
            return True
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        reader = QImageReader(str(path))
        return reader.canRead() and bytes(reader.format()).lower() != b"pdf"


@dataclass
class DecodedImage:
    image: QImage
    format: str
    name: str = ""
    large: object = None
    lease: object = None
    metadata: object = None
    backup_path: str = ""

    @property
    def width(self):
        return self.large.width if self.large else self.image.width()

    @property
    def height(self):
        return self.large.height if self.large else self.image.height()


def check_dimensions(width, height):
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise BackupError(
            "图片尺寸无效或超过普通解码的内存上限；可导出后使用系统应用查看。",
            english="The image dimensions are invalid or exceed the normal decoding memory limit; export it to view in a system application.",
        )


def decode_image(path, cancel=None):
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise BackupError(
            "图片文件超过 128 MiB 的预览上限；可导出后使用系统应用查看。",
            english="The image exceeds the 128 MiB preview limit; export it to view in a system application.",
        )
    while not DECODE_SLOTS.acquire(timeout=0.05):
        check_cancel(cancel)
    try:
        check_cancel(cancel)
        # Read metadata before large decode, which may create a tiled file that
        # must be handed back to the cache even if cancellation follows.
        metadata = read_metadata(path, cancel)
        check_cancel(cancel)
        result = _decode(path, cancel)
        result.metadata = metadata
        return result
    finally:
        DECODE_SLOTS.release()


def large_result(path, cancel, format_name):
    from .large_image import decode_large

    image, large = decode_large(path, cancel, format_name)
    return DecodedImage(image, format_name, large=large)


def _decode(path, cancel):
    # Read dimensions without Pillow's whole-raster bomb threshold preventing
    # legitimate very long PNG/JPEG/TIFF screenshots from reaching streaming.
    probe = QImageReader(str(path))
    size = probe.size()
    if (
        size.isValid()
        and (
            size.width() * size.height() > LARGE_PIXEL_THRESHOLD
            or max(size.width(), size.height()) > LARGE_EDGE_THRESHOLD
        )
        and bytes(probe.format()).lower() != b"pdf"
    ):
        return large_result(path, cancel, bytes(probe.format()).decode("ascii", "replace").upper())
    try:
        source = Image.open(path)
    except UnidentifiedImageError:
        reader = QImageReader(str(path))
        reader.setDecideFormatFromContent(True)
        reader.setAutoTransform(True)
        if not reader.canRead() or bytes(reader.format()).lower() == b"pdf":
            raise BackupError(
                "无法识别图片：文件可能损坏或格式不受支持。",
                english="Unable to recognize the image; the file may be damaged or its format unsupported.",
            ) from None
        size = reader.size()
        check_dimensions(size.width(), size.height())
        image = reader.read()
        check_cancel(cancel)
        if image.isNull():
            raise BackupError("无法解码图片：" + reader.errorString())
        check_dimensions(image.width(), image.height())
        return DecodedImage(image, bytes(reader.format()).decode("ascii", "replace").upper())
    except Image.DecompressionBombError:
        return large_result(path, cancel, "图片")
    except (OSError, ValueError) as exc:
        raise BackupError(f"无法读取图片：{exc}") from exc
    with source:
        if (
            source.width * source.height > LARGE_PIXEL_THRESHOLD
            or max(source.size) > LARGE_EDGE_THRESHOLD
        ):
            return large_result(path, cancel, source.format or "图片")
        check_dimensions(*source.size)
        format_name = source.format or "图片"
        # Pillow starts on the first frame; pillow-heif starts on the primary
        # image, which may differ from container frame zero.
        source.load()
        check_cancel(cancel)
        check_dimensions(*source.size)
        ImageOps.exif_transpose(source, in_place=True)
        with source.convert("RGBA") as rgba:
            data = rgba.tobytes()
            image = QImage(
                data, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888
            ).copy()
        profile = source.info.get("icc_profile")
        if profile:
            space = QColorSpace.fromIccProfile(profile)
            if space.isValid():
                image.setColorSpace(space)
                image = image.convertedToColorSpace(QColorSpace.NamedColorSpace.SRgb)
        check_cancel(cancel)
        if image.isNull():
            raise BackupError(
                "图片解码失败或内存不足。",
                english="Image decoding failed or memory is insufficient.",
            )
        return DecodedImage(image, format_name)
