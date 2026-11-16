import struct
import zlib

import pytest
import pyvips
from PIL import ExifTags, Image, TiffImagePlugin
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QPlainTextEdit
from test_ui import choose, window  # noqa: F401

from planbak.image_data import decode_image
from planbak.image_metadata import information_text, read_metadata


def camera_exif():
    exif = Image.Exif()
    exif[271] = "Test Maker"
    exif[272] = "<b>Test Camera</b>"
    exif[ExifTags.IFD.Exif] = {
        36867: "2026:10:29 12:00:00",
        33434: TiffImagePlugin.IFDRational(1, 125),
        34855: 200,
    }
    exif[ExifTags.IFD.GPSInfo] = {
        1: "N",
        2: (
            TiffImagePlugin.IFDRational(31),
            TiffImagePlugin.IFDRational(12),
            TiffImagePlugin.IFDRational(0),
        ),
    }
    return exif


def test_metadata_generated_details_translate_without_translating_exif_values():
    from types import SimpleNamespace

    from planbak.i18n import ENGLISH, SIMPLIFIED_CHINESE, LanguageContext
    from planbak.image_metadata import ImageMetadata, MetadataIssue, value_text

    metadata = ImageMetadata(
        100,
        mode="L",
        exif=(("Make", "灰度"), ("Data", value_text(b"x" * 65))),
        error=(MetadataIssue("Unable to read {group} data", (("group", "GPS"),)),),
    )
    image = SimpleNamespace(metadata=metadata, name="sample", backup_path="", format="PNG", width=1, height=1)
    chinese = information_text(image, LanguageContext(SIMPLIFIED_CHINESE).translate)
    english = information_text(image, LanguageContext(ENGLISH).translate)
    assert "Make: 灰度" in chinese and "Make: 灰度" in english
    assert "共 65 字节" in chinese and "65 bytes" in english
    assert "GPS 数据无法读取" in chinese and "Unable to read GPS data" in english
    assert "颜色模式：灰度" in chinese and "Color mode: Grayscale" in english


@pytest.mark.parametrize("format_name", ["JPEG", "HEIF"])
def test_jpeg_exif_nested_and_missing_data(tmp_path, format_name):
    jpeg = tmp_path / ("photo.jpg" if format_name == "JPEG" else "photo.heic")
    Image.new("RGB", (32, 24), "blue").save(jpeg, format=format_name, exif=camera_exif())
    result = decode_image(jpeg)
    result.name = jpeg.name
    info = information_text(result)
    assert "32 × 24" in info and "Test Maker" in info
    assert "<b>Test Camera</b>" in info and "2026:10:29" in info and "1/125" in info
    assert "GPS.GPSLatitude" in info and not result.metadata.error
    plain = tmp_path / "plain.png"
    Image.new("RGB", (2, 2)).save(plain)
    assert "无 EXIF 信息" in information_text(decode_image(plain))


def test_long_png_exif_after_pixels_is_read_without_decoding(tmp_path, monkeypatch):
    path = tmp_path / "long.png"
    pyvips.Image.black(1000, 10000).pngsave(str(path))
    data = path.read_bytes()
    raw = camera_exif().tobytes()[6:]
    chunk = (
        struct.pack(">I", len(raw)) + b"eXIf" + raw + struct.pack(">I", zlib.crc32(b"eXIf" + raw))
    )
    path.write_bytes(data[:-12] + chunk + data[-12:])

    def forbidden(*args, **kwargs):
        raise AssertionError("Metadata must not decode the full raster")

    monkeypatch.setattr(Image.Image, "load", forbidden)
    metadata = read_metadata(path)
    assert ("Make", "Test Maker") in metadata.exif and not metadata.error
    assert metadata.bit_depth == "8"


def test_image_information_dialog_and_cached_metadata_survive_preview_eviction(
    request, qtbot, tmp_path
):
    w = request.getfixturevalue("window")
    w.language_context.set_language("zh_CN")
    path = tmp_path / "camera.jpg"
    Image.new("RGB", (3200, 2600), "blue").save(path, exif=camera_exif())
    w.session.import_paths([path], "HomeDomain", "Documents")
    w.domain, w.path = "HomeDomain", "Documents"
    w.refresh()
    choose(w, "camera.jpg")
    w.open_file()
    qtbot.waitUntil(lambda: w.task is None and bool(w.image_viewers))
    v = w.image_viewers[0]
    qtbot.waitUntil(lambda: v.task is None)
    assert v.status.details_button.text() == "图片信息"
    assert "只读图片快照" not in v.image_information()
    assert "HomeDomain/Documents/camera.jpg" in v.image_information()
    assert "2026:10:29" in v.image_information()
    checked = []

    def inspect():
        dialog = QApplication.activeModalWidget()
        assert dialog.windowTitle() == "图片信息"
        text = dialog.findChild(QPlainTextEdit)
        assert text.isReadOnly() and "<b>Test Camera</b>" in text.toPlainText()
        text.selectAll()
        text.copy()
        assert QApplication.clipboard().text() == text.toPlainText()
        checked.append(True)
        dialog.reject()

    QTimer.singleShot(0, inspect)
    v.status.details_button.click()
    assert checked
    w.images.cache.decoded.clear()
    v.request()
    qtbot.waitUntil(lambda: v.task is None)
    assert "2026:10:29" in v.image_information()
    v.close()
