"""Bounded, header-only image information and readable EXIF snapshots."""

import struct
from dataclasses import dataclass
from pathlib import Path

from PIL import ExifTags, Image

from .codec import check_cancel

MAX_EXIF_BYTES = 256 * 1024
MAX_FIELDS = 512
MAX_VALUE = 512


@dataclass(frozen=True)
class ImageMetadata:
    file_size: int
    mode: str = ""
    bit_depth: str = ""
    exif: tuple = ()
    error: str = ""


def value_text(value, depth=0):
    if isinstance(value, bytes):
        prefix = value[:64]
        if value.startswith(b"ASCII\x00\x00\x00"):
            return value[8 : 8 + MAX_VALUE].decode("ascii", "replace").rstrip("\x00")
        if prefix and all(32 <= b < 127 or b == 0 for b in prefix):
            text = prefix.decode("ascii").rstrip("\x00")
        else:
            text = prefix.hex(" ")
        return text + (f"…（共 {len(value)} 字节）" if len(value) > 64 else "")
    if isinstance(value, (tuple, list)) and depth < 2:
        values = [value_text(v, depth + 1) for v in value[:16]]
        return ", ".join(values) + (f"…（共 {len(value)} 项）" if len(value) > 16 else "")
    numerator, denominator = getattr(value, "numerator", None), getattr(value, "denominator", None)
    if numerator is not None and denominator not in (None, 1):
        return f"{numerator}/{denominator}"
    text = str(value).replace("\x00", "\\0").replace("\r", "\\r").replace("\n", "\\n")
    return text[:MAX_VALUE] + ("…" if len(text) > MAX_VALUE else "")


def exif_fields(exif):
    fields = []
    pointers = {int(ExifTags.IFD.Exif), int(ExifTags.IFD.GPSInfo), int(ExifTags.IFD.Interop)}
    # TIFF strip/tile offset arrays describe pixel storage, not camera EXIF.
    # Avoid expanding potentially huge arrays just to show image information.
    storage = {273, 279, 324, 325, 330}
    base = {tag: exif[tag] for tag in exif if tag not in storage and tag not in pointers}
    groups = [("", base, ExifTags.TAGS)]
    errors = []
    used = 0
    nested_exif = None
    for label, kind in (
        ("Exif", ExifTags.IFD.Exif),
        ("GPS", ExifTags.IFD.GPSInfo),
        ("Interop", ExifTags.IFD.Interop),
    ):
        try:
            if kind == ExifTags.IFD.Interop:
                if not nested_exif or int(kind) not in nested_exif:
                    continue
            elif int(kind) not in exif:
                continue
            nested = exif.get_ifd(kind)
            if kind == ExifTags.IFD.Exif:
                nested_exif = nested
            if nested:
                groups.append(
                    (label, nested, ExifTags.GPSTAGS if label == "GPS" else ExifTags.TAGS)
                )
        except Exception:
            errors.append(f"{label} 数据无法读取")
    for group, values, names in groups:
        for tag, value in sorted(values.items()):
            if tag in pointers:
                continue
            label = names.get(tag, f"Tag 0x{tag:04X}")
            if group:
                label = f"{group}.{label}"
            try:
                fields.append((label, value_text(value)))
            except Exception:
                fields.append((label, "无法显示此值"))
            used += sum(len(value) for value in fields[-1])
            if len(fields) >= MAX_FIELDS or used >= 64 * 1024:
                fields.append(("提示", "EXIF 内容较多，显示已截断"))
                return tuple(fields), "；".join(errors)
    return tuple(fields), "；".join(errors)


def png_metadata(path, cancel):
    # PNG EXIF can follow IDAT. Pillow.getexif() may load pixels to reach it;
    # skip chunks by seeking instead, including for very long screenshots.
    mode, depth, raw = "", "", None
    size = path.stat().st_size
    with path.open("rb") as stream:
        stream.seek(8)
        for _ in range(4096):
            check_cancel(cancel)
            header = stream.read(8)
            if len(header) != 8:
                break
            length, kind = struct.unpack(">I4s", header)
            if stream.tell() + length + 4 > size:
                break
            if kind == b"IHDR" and length == 13:
                data = stream.read(13)
                depth = str(data[8])
                mode = {0: "灰度", 2: "RGB", 3: "索引色", 4: "灰度+Alpha", 6: "RGBA"}.get(
                    data[9], ""
                )
            elif kind == b"eXIf":
                if length > MAX_EXIF_BYTES:
                    return ImageMetadata(size, mode, depth, error="EXIF 数据过大，未展开")
                raw = stream.read(length)
            else:
                stream.seek(length, 1)
            stream.seek(4, 1)
            if kind == b"IEND":
                break
        else:
            return ImageMetadata(size, mode, depth, error="PNG 数据块过多，未读取全部元数据")
    if not raw:
        return ImageMetadata(size, mode, depth)
    exif = Image.Exif()
    exif.load(raw)
    fields, error = exif_fields(exif)
    return ImageMetadata(size, mode, depth, fields, error)


def read_metadata(path, cancel=None):
    path = Path(path)
    size = path.stat().st_size
    check_cancel(cancel)
    try:
        with path.open("rb") as stream:
            png = stream.read(8) == b"\x89PNG\r\n\x1a\n"
        if png:
            return png_metadata(path, cancel)
        with Image.open(path) as image:
            mode = image.mode
            depth = str(image.info.get("bit_depth") or getattr(image, "bits", "") or "")
            raw = image.info.get("exif")
            if raw and len(raw) > MAX_EXIF_BYTES:
                return ImageMetadata(size, mode, depth, error="EXIF 数据过大，未展开")
            if image.format == "TIFF":
                exif = image.getexif()  # TIFF IFD metadata; no pixel load.
            elif raw:
                exif = Image.Exif()
                exif.load(raw)
                orientation = image.info.get("original_orientation")
                if orientation is not None:
                    exif[274] = orientation
            else:
                return ImageMetadata(size, mode, depth)
            check_cancel(cancel)
            fields, error = exif_fields(exif)
            return ImageMetadata(size, mode, depth, fields, error)
    except Exception as exc:
        check_cancel(cancel)
        return ImageMetadata(size, error=f"元数据无法读取：{type(exc).__name__}")


def information_text(result):
    metadata = result.metadata
    lines = [f"文件名称：{result.name or '未命名图片'}"]
    if result.backup_path:
        lines.append(f"备份路径：{result.backup_path}")
    lines.extend((f"格式：{result.format}", f"图片尺寸：{result.width} × {result.height} 像素"))
    if metadata:
        size = metadata.file_size
        unit = f"（{size / 1024**2:.2f} MiB）" if size >= 1024**2 else ""
        lines.append(f"文件大小：{size:,} 字节{unit}")
        if metadata.mode:
            lines.append(f"颜色模式：{metadata.mode}")
        if metadata.bit_depth:
            lines.append(f"位深：{metadata.bit_depth} bit")
    lines.extend(("", "EXIF"))
    if metadata and metadata.exif:
        lines.extend(f"{name}：{value}" for name, value in metadata.exif)
    elif not metadata or not metadata.error:
        lines.append("无 EXIF 信息")
    if metadata and metadata.error:
        lines.append(metadata.error)
    return "\n".join(lines)
