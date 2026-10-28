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


class BackupError(Exception):
    """An actionable backup/operation error suitable for display."""


class Cancelled(BackupError):
    pass


def check_cancel(cancel: Callable[[], bool] | None = None):
    if cancel and cancel():
        raise Cancelled("操作已取消")


def file_id(domain: str, path: str) -> str:
    return hashlib.sha1(f"{domain}-{path}".encode("utf-8")).hexdigest()


def safe_path(path: str, *, root: bool = True) -> str:
    if not isinstance(path, str) or "\x00" in path:
        raise BackupError("路径包含不受支持的字符")
    if os.name == "nt" and ("\\" in path or ":" in path):
        raise BackupError("此 iOS 路径无法安全映射到 Windows 文件系统")
    if path.startswith("/") or any(p in (".", "..") for p in path.split("/")):
        raise BackupError("路径不能是绝对路径或包含 . / ..")
    if not root and not path:
        raise BackupError("名称不能为空")
    if path and any(not p for p in path.split("/")):
        raise BackupError("路径包含空的文件夹名称")
    return path


def crypto_file(
    source: Path,
    target: Path,
    key: bytes,
    *,
    encrypt: bool,
    cancel: Callable[[], bool] | None = None,
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
                raise BackupError("BackupKeyBag 格式损坏")
            tag = data[offset : offset + 4]
            length = struct.unpack_from(">I", data, offset + 4)[0]
            offset += 8
            if length > len(data) - offset:
                raise BackupError("BackupKeyBag 长度无效")
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
                raise BackupError("不支持此备份的密钥派生格式")
            count = struct.unpack(">I", raw)[0]
            if not 1 <= count <= maximum:
                raise BackupError("备份的密钥派生迭代次数超出安全范围")
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
            raise BackupError("备份密码不正确，或密钥包已损坏") from exc
        if not self.keys:
            raise BackupError("备份没有可用的密码保护密钥")

    def unwrap(self, wrapped: bytes) -> tuple[int, bytes]:
        if len(wrapped) < 28:
            raise BackupError("文件加密密钥无效")
        clas = struct.unpack_from("<I", wrapped)[0]
        if clas not in self.keys:
            raise BackupError(f"缺少保护类别 {clas} 的密钥")
        try:
            return clas, aes_key_unwrap(self.keys[clas], wrapped[4:])
        except InvalidUnwrap as exc:
            raise BackupError("文件密钥校验失败") from exc

    def new_key(self, clas: int) -> tuple[bytes, bytes]:
        if clas not in self.keys:
            raise BackupError(f"不能为保护类别 {clas} 创建文件")
        key = os.urandom(32)
        return key, struct.pack("<I", clas) + aes_key_wrap(self.keys[clas], key)


class Metadata:
    """Minimal MBFile keyed archive access without changing unrelated UID indices."""

    def __init__(self, blob: bytes):
        try:
            self.archive = plistlib.loads(blob)
            self.objects = self.archive["$objects"]
            self.root = self.objects[self.archive["$top"]["root"].data]
            if not isinstance(self.root, dict):
                raise ValueError("not MBFile")
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            raise BackupError("文件元数据不是支持的 MBFile 归档") from exc

    def get(self, key, default=None):
        value = self.root.get(key, default)
        if isinstance(value, plistlib.UID):
            value = self.objects[value.data]
            if isinstance(value, dict) and "NS.data" in value:
                return value["NS.data"]
        return value

    def set(self, key, value):
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
        return plistlib.dumps(self.archive, fmt=plistlib.FMT_BINARY, sort_keys=False)

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
