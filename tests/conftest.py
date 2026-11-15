import hashlib
import os
import plistlib
import sqlite3
import struct
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.keywrap import aes_key_wrap

PASSWORD = "test-password-only"


def encrypt_bytes(data, key):
    padder = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).encryptor()
    return enc.update(padder.update(data) + padder.finalize()) + enc.finalize()


def archive(*, data, directory=False, encrypted=False, key=None, class_key=None, special=False):
    objects = [
        "$null",
        {},
        {"$classname": "MBFile", "$classes": ["MBFile", "NSObject"]},
        {"$classname": "NSData", "$classes": ["NSData", "NSObject"]},
    ]
    root = {
        "$class": plistlib.UID(2),
        "Size": len(data),
        "Birth": 1700000000,
        "LastModified": 1700000000,
        "LastStatusChange": 1700000000,
        "Mode": 0o120777 if special else (0o40755 if directory else 0o100644),
        "UserID": 501,
        "GroupID": 501,
        "ProtectionClass": 3,
        "Flags": 0,
        "InodeNumber": 12345,
    }
    if not directory:
        stored = encrypt_bytes(data, key) if encrypted else data
        objects.append({"NS.data": hashlib.sha1(stored).digest(), "$class": plistlib.UID(3)})
        root["Digest"] = plistlib.UID(len(objects) - 1)
    if encrypted and not directory:
        objects.append(
            {
                "NS.data": struct.pack("<I", 3) + aes_key_wrap(class_key, key),
                "$class": plistlib.UID(3),
            }
        )
        root["EncryptionKey"] = plistlib.UID(len(objects) - 1)
    objects[1] = root
    return plistlib.dumps(
        {
            "$archiver": "NSKeyedArchiver",
            "$version": 100000,
            "$objects": objects,
            "$top": {"root": plistlib.UID(1)},
        },
        fmt=plistlib.FMT_BINARY,
    )


def make_backup(root: Path, encrypted=False):
    root.mkdir()
    manifest = {"Version": "10.0", "IsEncrypted": encrypted}
    class_key = os.urandom(32)
    manifest_key = os.urandom(32)
    if encrypted:
        salt, dpsl = os.urandom(20), os.urandom(20)
        derived = hashlib.pbkdf2_hmac("sha256", PASSWORD.encode(), dpsl, 10, 32)
        derived = hashlib.pbkdf2_hmac("sha1", derived, salt, 10, 32)

        def tlv(tag, value):
            if isinstance(value, int):
                value = struct.pack(">I", value)
            return tag + struct.pack(">I", len(value)) + value

        manifest["BackupKeyBag"] = b"".join(
            tlv(k, v)
            for k, v in [
                (b"VERS", 5),
                (b"TYPE", 1),
                (b"UUID", uuid.uuid4().bytes),
                (b"WRAP", 2),
                (b"SALT", salt),
                (b"ITER", 10),
                (b"DPSL", dpsl),
                (b"DPIC", 10),
                (b"UUID", uuid.uuid4().bytes),
                (b"CLAS", 3),
                (b"WRAP", 2),
                (b"WPKY", aes_key_wrap(derived, class_key)),
            ]
        )
        manifest["ManifestKey"] = struct.pack("<I", 3) + aes_key_wrap(class_key, manifest_key)
    db_path = root / "Manifest.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE Files(fileID TEXT PRIMARY KEY,domain TEXT,relativePath TEXT,flags INTEGER,file BLOB)"
        )
        db.execute("CREATE TABLE Properties(key TEXT PRIMARY KEY,value BLOB)")
        entries = [
            ("HomeDomain", "", b"", 2),
            ("HomeDomain", "Documents", b"", 2),
            ("HomeDomain", "Documents/a.txt", b"hello world", 1),
            ("HomeDomain", "Documents/empty.bin", b"", 1),
            ("HomeDomain", "Documents/sub", b"", 2),
            ("HomeDomain", "Documents/sub/b.txt", b"nested", 1),
            ("HomeDomain", "implicit/deep.txt", b"implied directory", 1),
            ("SystemPreferencesDomain", "config.plist", plistlib.dumps({"sample": True}), 1),
        ]
        for domain, path, data, flags in entries:
            fid = hashlib.sha1(f"{domain}-{path}".encode()).hexdigest()
            key = os.urandom(32)
            blob = archive(
                data=data, directory=flags == 2, encrypted=encrypted, key=key, class_key=class_key
            )
            db.execute("INSERT INTO Files VALUES(?,?,?,?,?)", (fid, domain, path, flags, blob))
            if flags == 1:
                file = root / fid[:2] / fid
                file.parent.mkdir(exist_ok=True)
                file.write_bytes(encrypt_bytes(data, key) if encrypted else data)
    if encrypted:
        db_path.write_bytes(encrypt_bytes(db_path.read_bytes(), manifest_key))
    for name, data in [
        ("Manifest.plist", manifest),
        ("Info.plist", {"Device Name": "Fixture", "Product Version": "test"}),
        ("Status.plist", {"SnapshotState": "finished", "BackupState": "new", "Version": "3.3"}),
    ]:
        (root / name).write_bytes(plistlib.dumps(data))
    return root


@pytest.fixture(params=[False, True], ids=["plain", "encrypted"])
def backup_root(request, tmp_path):
    return make_backup(tmp_path / "source", request.param)


@pytest.fixture(scope="session", autouse=True)
def application_item_style(qapp):
    from planbak.appearance import configure_system_appearance

    configure_system_appearance(qapp)
