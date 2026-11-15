import struct

import pytest

from planbak.codec import BackupError, Keybag


def field(tag, value):
    return tag + struct.pack(">I", len(value)) + value


@pytest.mark.parametrize(
    "class_id,wrap", [(b"\0", b"\0\0\0\2"), (None, b"\0\0\0\2"), (b"\0\0\0\3", b"\2")]
)
def test_malformed_keybag_class_fields_raise_backup_error(class_id, wrap):
    data = b"".join(
        [
            field(b"SALT", b"synthetic salt"),
            field(b"ITER", struct.pack(">I", 1)),
            field(b"UUID", bytes(16)),
            field(b"UUID", bytes(16)),
            field(b"WRAP", wrap),
            field(b"WPKY", bytes(40)),
        ]
    )
    if class_id is not None:
        data += field(b"CLAS", class_id)
    with pytest.raises(BackupError, match="密钥包已损坏"):
        Keybag(data, "synthetic-password")


def test_keybag_missing_salt_raises_backup_error():
    with pytest.raises(BackupError, match="派生盐值"):
        Keybag(field(b"ITER", struct.pack(">I", 1)), "synthetic-password")
