"""Modern iOS backup keybag, streamed payload crypto, and MBFile metadata.

Only cryptography's maintained AES, key-wrap, and padding primitives are used.
Unknown archive fields and object indices are retained when editing metadata.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import struct
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap, aes_key_wrap

CHUNK = 1024 * 1024
# 应用资源预算，并非 iOS 规格上限。较大的 NSData 不等于大量解析对象。
MAX_METADATA_BYTES = 8 * 1024**2
MAX_METADATA_OBJECTS = 100_000
MAX_METADATA_ITEMS = 200_000
MAX_METADATA_DEPTH = 128


class BackupError(Exception):
    """An actionable backup/operation error suitable for display."""

    def __init__(self, message, *, english=None):
        super().__init__(message)
        self.translation_source = english


class Cancelled(BackupError):
    pass


def check_cancel(cancel: Callable[[], bool] | None = None):
    if cancel and cancel():
        raise Cancelled("操作已取消")


def file_id(domain: str, path: str) -> str:
    return hashlib.sha1(f"{domain}-{path}".encode("utf-8")).hexdigest()


def safe_path(path: str, *, root: bool = True) -> str:
    if not isinstance(path, str) or "\x00" in path:
        raise BackupError(
            "路径包含不受支持的字符", english="The path contains unsupported characters"
        )
    if os.name == "nt" and ("\\" in path or ":" in path):
        raise BackupError(
            "此 iOS 路径无法安全映射到 Windows 文件系统",
            english="This iOS path cannot be safely mapped to a Windows filesystem",
        )
    if path.startswith("/") or any(p in (".", "..") for p in path.split("/")):
        raise BackupError(
            "路径不能是绝对路径或包含 . / ..",
            english="The path cannot be absolute or contain . / ..",
        )
    if not root and not path:
        raise BackupError("名称不能为空", english="The name cannot be empty")
    if path and any(not p for p in path.split("/")):
        raise BackupError(
            "路径包含空的文件夹名称", english="The path contains an empty folder name"
        )
    return path


def crypto_file(
    source: Path,
    target: Path,
    key: bytes,
    *,
    encrypt: bool,
    cancel: Callable[[], bool] | None = None,
    progress=None,
):
    """Stream AES-CBC/zero IV with PKCS#7 padding to a new target file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    cipher = Cipher(algorithms.AES(key), modes.CBC(bytes(16)))
    transform = cipher.encryptor() if encrypt else cipher.decryptor()
    pad = padding.PKCS7(128).padder() if encrypt else padding.PKCS7(128).unpadder()
    try:
        with source.open("rb") as src, target.open("wb") as dst:
            while block := src.read(CHUNK):
                check_cancel(cancel)
                if encrypt:
                    dst.write(transform.update(pad.update(block)))
                else:
                    dst.write(pad.update(transform.update(block)))
                if progress:
                    progress(len(block))
            if encrypt:
                dst.write(transform.update(pad.finalize()) + transform.finalize())
            else:
                dst.write(pad.update(transform.finalize()) + pad.finalize())
    except Exception:
        target.unlink(missing_ok=True)
        raise


class Keybag:
    """Unlock password-wrapped backup protection classes; never persists secrets."""

    def __init__(self, data: bytes, password: str):
        attrs, classes, current = {}, [], None
        seen_uuid = False
        offset = 0
        while offset < len(data):
            if offset + 8 > len(data):
                raise BackupError(
                    "BackupKeyBag 格式损坏", english="The BackupKeyBag format is damaged"
                )
            tag = data[offset : offset + 4]
            length = struct.unpack_from(">I", data, offset + 4)[0]
            offset += 8
            if length > len(data) - offset:
                raise BackupError("BackupKeyBag 长度无效", english="Invalid BackupKeyBag length")
            value = data[offset : offset + length]
            offset += length
            if tag == b"UUID":
                if seen_uuid:
                    current = {}
                    classes.append(current)
                seen_uuid = True
            elif tag in (b"CLAS", b"WRAP", b"WPKY", b"KTYP", b"PBKY") and current is not None:
                current[tag] = value
            else:
                attrs[tag] = value

        def number(tag, maximum):
            raw = attrs.get(tag)
            if not isinstance(raw, bytes) or len(raw) != 4:
                raise BackupError(
                    "不支持此备份的密钥派生格式",
                    english="The backup key derivation format is not supported",
                )
            count = struct.unpack(">I", raw)[0]
            if not 1 <= count <= maximum:
                raise BackupError(
                    "备份的密钥派生迭代次数超出安全范围",
                    english="The backup key derivation iteration count is outside the safe range",
                )
            return count

        secret = password.encode("utf-8")
        if b"DPSL" in attrs:
            secret = hashlib.pbkdf2_hmac(
                "sha256", secret, attrs[b"DPSL"], number(b"DPIC", 50_000_000), 32
            )
        derived = hashlib.pbkdf2_hmac(
            "sha1", secret, attrs[b"SALT"], number(b"ITER", 10_000_000), 32
        )
        self.keys: dict[int, bytes] = {}
        try:
            for item in classes:
                if b"WPKY" not in item:
                    continue
                wrap = struct.unpack(">I", item.get(b"WRAP", bytes(4)))[0]
                if wrap & 2:
                    clas = struct.unpack(">I", item[b"CLAS"])[0]
                    self.keys[clas] = aes_key_unwrap(derived, item[b"WPKY"])
        except (InvalidUnwrap, ValueError) as exc:
            raise BackupError(
                "备份密码不正确，或密钥包已损坏",
                english="The backup password is incorrect or the keybag is damaged",
            ) from exc
        if not self.keys:
            raise BackupError(
                "备份没有可用的密码保护密钥",
                english="The backup has no usable password-protected key",
            )

    def unwrap(self, wrapped: bytes) -> tuple[int, bytes]:
        if len(wrapped) < 28:
            raise BackupError("文件加密密钥无效", english="Invalid file encryption key")
        clas = struct.unpack_from("<I", wrapped)[0]
        if clas not in self.keys:
            raise BackupError(f"缺少保护类别 {clas} 的密钥")
        try:
            return clas, aes_key_unwrap(self.keys[clas], wrapped[4:])
        except InvalidUnwrap as exc:
            raise BackupError("文件密钥校验失败", english="File key verification failed") from exc

    def new_key(self, clas: int) -> tuple[bytes, bytes]:
        if clas not in self.keys:
            raise BackupError(f"不能为保护类别 {clas} 创建文件")
        key = os.urandom(32)
        return key, struct.pack("<I", clas) + aes_key_wrap(self.keys[clas], key)


class Metadata:
    """Minimal MBFile keyed archive access without changing unrelated UID indices."""

    def __init__(self, blob: bytes):
        if len(blob) > MAX_METADATA_BYTES:
            raise BackupError(
                "文件元数据超过程序的 8 MiB 资源上限",
                english="File metadata exceeds the application’s 8 MiB limit",
            )
        if blob.startswith(b"bplist00") and len(blob) >= 32:
            # 二进制 plist 尾部已有对象数量；在 plistlib 分配对象前拒绝异常表。
            count = int.from_bytes(blob[-24:-16], "big")
            if count > MAX_METADATA_OBJECTS:
                raise BackupError(
                    "文件元数据的对象数量超过资源上限",
                    english="File metadata contains too many objects",
                )
        try:
            self.archive = plistlib.loads(blob)
            self.validate_structure()
            self.objects = self.archive["$objects"]
            if not isinstance(self.objects, list):
                raise ValueError("not an object array")
            self.root = self.objects[self.archive["$top"]["root"].data]
            if not isinstance(self.root, dict):
                raise ValueError("not MBFile")
        except (ValueError, KeyError, TypeError, IndexError, AttributeError, RecursionError) as exc:
            raise BackupError(
                "文件元数据不是支持的 MBFile 归档",
                english="File metadata is not a supported MBFile archive",
            ) from exc

    def validate_structure(self):
        pending = [(self.archive, 1)]
        visited = set()
        items = 0
        while pending:
            value, depth = pending.pop()
            if depth > MAX_METADATA_DEPTH:
                raise BackupError(
                    "文件元数据的嵌套深度超过资源上限",
                    english="File metadata exceeds the nesting limit",
                )
            if not isinstance(value, (list, dict)) or id(value) in visited:
                continue
            visited.add(id(value))
            items += len(value) * (2 if isinstance(value, dict) else 1)
            if items > MAX_METADATA_ITEMS:
                raise BackupError(
                    "文件元数据的容器条目数量超过资源上限",
                    english="File metadata exceeds the container entry limit",
                )
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)

    def get(self, key, default=None):
        value = self.root.get(key, default)
        if isinstance(value, plistlib.UID):
            value = self.objects[value.data]
            if isinstance(value, dict) and "NS.data" in value:
                return value["NS.data"]
        return value

    def set(self, key, value):
        if key in self.root and self.get(key) == value:
            return
        if isinstance(value, bytes):
            # Append rather than editing a possibly shared archived NSData object.
            data_class = next(
                (
                    i
                    for i, obj in enumerate(self.objects)
                    if isinstance(obj, dict) and obj.get("$classname") == "NSData"
                ),
                None,
            )
            if data_class is None:
                self.objects.append({"$classname": "NSData", "$classes": ["NSData", "NSObject"]})
                data_class = len(self.objects) - 1
            self.objects.append({"NS.data": value, "$class": plistlib.UID(data_class)})
            self.root[key] = plistlib.UID(len(self.objects) - 1)
        elif isinstance(value, str) and (
            isinstance(self.root.get(key), plistlib.UID) or key in ("RelativePath", "Target")
        ):
            self.objects.append(value)
            self.root[key] = plistlib.UID(len(self.objects) - 1)
        else:
            self.root[key] = value

    def dump(self):
        self.validate_structure()
        blob = plistlib.dumps(self.archive, fmt=plistlib.FMT_BINARY, sort_keys=False)
        # 同一约束用于输入和发布，不能生成程序无法重新打开的归档。
        if len(blob) > MAX_METADATA_BYTES:
            raise BackupError(
                "修改后的文件元数据超过程序的 8 MiB 资源上限，未应用更改",
                english="Modified file metadata exceeds the 8 MiB limit; changes were not applied",
            )
        if int.from_bytes(blob[-24:-16], "big") > MAX_METADATA_OBJECTS:
            raise BackupError(
                "修改后的文件元数据对象数量超过资源上限，未应用更改",
                english="Modified file metadata contains too many objects; changes were not applied",
            )
        return blob

    @classmethod
    def new(cls, *, directory: bool, timestamp: int, uid: int, gid: int, clas: int):
        root = {
            "$class": plistlib.UID(2),
            "Birth": timestamp,
            "LastModified": timestamp,
            "LastStatusChange": timestamp,
            "Mode": 0o40755 if directory else 0o100644,
            "Size": 0,
            "UserID": uid,
            "GroupID": gid,
            "ProtectionClass": clas,
            "InodeNumber": int.from_bytes(os.urandom(7), "little"),
            "Flags": 0,
        }
        archive = {
            "$archiver": "NSKeyedArchiver",
            "$version": 100000,
            "$objects": [
                "$null",
                root,
                {"$classname": "MBFile", "$classes": ["MBFile", "NSObject"]},
            ],
            "$top": {"root": plistlib.UID(1)},
        }
        return cls(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY))
