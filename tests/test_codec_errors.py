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


@pytest.mark.parametrize("truncate", [True, False])
def test_corrupt_encrypted_payload_reports_backup_error_and_cleans_output(tmp_path, truncate):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from planbak.codec import crypto_file

    key = bytes(32)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(bytes(16))).encryptor()
    ciphertext = encryptor.update(bytes(16)) + encryptor.finalize()
    source, target = tmp_path / "ciphertext", tmp_path / "output"
    source.write_bytes(ciphertext[:-1] if truncate else ciphertext)
    with pytest.raises(BackupError, match="损坏或不完整"):
        crypto_file(source, target, key, encrypt=False)
    assert source.exists() and not target.exists()


def test_decryption_progress_errors_are_not_misreported_as_corrupt_data(tmp_path):
    from planbak.codec import crypto_file

    source, target = tmp_path / "input", tmp_path / "output"
    source.write_bytes(bytes(16))

    def fail_progress(_amount):
        raise ValueError("progress callback failed")

    with pytest.raises(ValueError, match="progress callback failed"):
        crypto_file(source, target, bytes(32), encrypt=False, progress=fail_progress)
    assert not target.exists()
