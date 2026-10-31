"""Bounded page reads and cancellable, chunked byte search."""

from dataclasses import dataclass
from pathlib import Path

from .codec import BackupError, check_cancel

PAGE_SIZE = 4096
SEARCH_CHUNK = 1024 * 1024
MAX_PATTERN = 4096


def parse_offset(text, size):
    text = text.strip()
    try:
        value = int(text, 16 if text.lower().startswith("0x") else 10)
    except ValueError:
        raise BackupError("请输入十进制偏移或以 0x 开头的十六进制偏移。") from None
    if value < 0 or value >= max(1, size):
        raise BackupError("偏移超出文件范围。")
    return value


def parse_pattern(text, hex_mode=True):
    try:
        pattern = bytes.fromhex(text) if hex_mode else text.encode("utf-8")
    except (ValueError, UnicodeError):
        raise BackupError("请输入完整的十六进制字节，例如 53 51 4C 69。") from None
    if not pattern:
        raise BackupError("请输入搜索内容。")
    if len(pattern) > MAX_PATTERN:
        raise BackupError("搜索内容最多支持 4096 字节。")
    return pattern


def read_page(path, offset):
    with Path(path).open("rb") as stream:
        stream.seek(offset)
        return stream.read(PAGE_SIZE)


@dataclass
class SearchResult:
    offset: int | None
    wrapped: bool = False


def search_bytes(path, pattern, start=0, *, backwards=False, cancel=None):
    if not pattern or len(pattern) > MAX_PATTERN:
        raise BackupError("无效的搜索字节。")
    size = Path(path).stat().st_size
    if not size:
        return SearchResult(None)
    start = max(0, min(start, size)) if not backwards else max(-1, min(start, size - 1))

    def scan(stream, low, high):
        # [low, high) bounds candidate start offsets; overlapping bytes let a
        # match span chunks and pages without allocating the entire file.
        position = high if backwards else low
        while position > low if backwards else position < high:
            check_cancel(cancel)
            base = max(low, position - SEARCH_CHUNK) if backwards else position
            step = position - base if backwards else min(SEARCH_CHUNK, high - base)
            stream.seek(base)
            data = stream.read(step + len(pattern) - 1)
            found = data.rfind(pattern) if backwards else data.find(pattern)
            if 0 <= found < step:
                return base + found
            position = base if backwards else base + step
        return None

    ranges = ((0, start + 1), (start + 1, size)) if backwards else ((start, size), (0, start))
    with Path(path).open("rb") as stream:
        for wrapped, (low, high) in enumerate(ranges):
            found = scan(stream, low, high)
            if found is not None:
                return SearchResult(found, bool(wrapped))
    return SearchResult(None)
