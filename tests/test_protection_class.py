import pytest
from conftest import PASSWORD

from planbak.backup import BackupSession
from planbak.codec import BackupError


def set_class(session, entry, value):
    meta = session.metadata(entry)
    meta.set("ProtectionClass", value)
    session.db.execute("UPDATE Files SET file=? WHERE fileID=?", (meta.dump(), entry.original_id))
    session.db.commit()


def test_new_file_in_class_zero_directory(backup_root, tmp_path):
    session = BackupSession(backup_root, PASSWORD)
    try:
        set_class(session, session.at("HomeDomain", "Documents"), 0)
        session.new_directory("HomeDomain", "Documents", "new")
        session.new_directory("HomeDomain", "Documents/new", "deep")
        source = tmp_path / "added.txt"
        source.write_bytes(b"new data")
        session.import_paths([source], "HomeDomain", "Documents/new/deep")
        output = session.save(tmp_path / "output")
        reopened = BackupSession(output, PASSWORD)
        try:
            entry = reopened.at("HomeDomain", "Documents/new/deep/added.txt")
            assert reopened.metadata(entry).get("ProtectionClass") == 3
            assert (
                reopened.metadata(reopened.at("HomeDomain", "Documents/new")).get("ProtectionClass")
                == 0
            )
            extracted = tmp_path / "extracted"
            reopened.extract_file(entry.token, extracted)
            assert extracted.read_bytes() == b"new data"
        finally:
            reopened.close()
    finally:
        session.close()


def test_new_file_uses_class_three_not_neighbor_class(backup_root, tmp_path):
    session = BackupSession(backup_root, PASSWORD)
    try:
        for item in session.children("HomeDomain", "Documents"):
            if item.kind == "file":
                set_class(session, item, 4)
        source = tmp_path / "added.txt"
        source.write_bytes(b"new")
        session.import_paths([source], "HomeDomain", "Documents")
        out = session.save(tmp_path / "output")
        reopened = BackupSession(out, PASSWORD)
        try:
            assert (
                reopened.metadata(reopened.at("HomeDomain", "Documents/added.txt")).get(
                    "ProtectionClass"
                )
                == 3
            )
        finally:
            reopened.close()
    finally:
        session.close()


@pytest.mark.parametrize("remove_key", [False, True])
def test_existing_empty_file_keeps_class_when_edited(backup_root, tmp_path, remove_key):
    session = BackupSession(backup_root, PASSWORD)
    try:
        entry = session.at("HomeDomain", "Documents/empty.bin")
        assert entry.size == 0
        if remove_key:
            meta = session.metadata(entry)
            meta.root.pop("EncryptionKey", None)
            if session.encrypted:
                session.payload_path(entry.original_id).write_bytes(b"")
            session.db.execute(
                "UPDATE Files SET file=? WHERE fileID=?", (meta.dump(), entry.original_id)
            )
            session.db.commit()
        session.stage_bytes(entry.token, b"no longer empty")
        output = session.save(tmp_path / "output")
        reopened = BackupSession(output, PASSWORD)
        try:
            saved = reopened.at("HomeDomain", "Documents/empty.bin")
            assert reopened.metadata(saved).get("ProtectionClass") == 3
            result = tmp_path / "readback"
            reopened.extract_file(saved.token, result)
            assert result.read_bytes() == b"no longer empty"
        finally:
            reopened.close()
    finally:
        session.close()


def test_existing_nondefault_class_preserved_in_plain_backup(backup_root, tmp_path):
    session = BackupSession(backup_root, PASSWORD)
    try:
        if session.encrypted:
            pytest.skip("fixture keybag has only class 3")
        entry = session.at("HomeDomain", "Documents/a.txt")
        set_class(session, entry, 4)
        session.stage_bytes(entry.token, b"modified")
        output = session.save(tmp_path / "output")
        reopened = BackupSession(output, PASSWORD)
        try:
            saved = reopened.at("HomeDomain", "Documents/a.txt")
            assert reopened.metadata(saved).get("ProtectionClass") == 4
        finally:
            reopened.close()
    finally:
        session.close()


@pytest.mark.parametrize("mode", ["edit", "delete_reimport"])
def test_existing_class_zero_reports_missing_key(backup_root, tmp_path, mode):
    session = BackupSession(backup_root, PASSWORD)
    try:
        if not session.encrypted:
            pytest.skip("keybag applies only to encrypted backups")
        entry = session.at("HomeDomain", "Documents/a.txt")
        set_class(session, entry, 0)
        if mode == "edit":
            session.stage_bytes(entry.token, b"changed")
        else:
            session.delete([entry.token])
            source = tmp_path / "a.txt"
            source.write_bytes(b"readded")
            session.import_paths([source], "HomeDomain", "Documents")
            assert session.at("HomeDomain", "Documents/a.txt").original_id == entry.original_id
        with pytest.raises(BackupError, match=r"保护类别 0.*密钥包.*没有对应密钥"):
            session.save(tmp_path / "output")
        assert not (tmp_path / "output").exists()
    finally:
        session.close()


def test_new_file_missing_default_key_reports_error(backup_root, tmp_path):
    session = BackupSession(backup_root, PASSWORD)
    try:
        if not session.encrypted:
            pytest.skip("keybag applies only to encrypted backups")
        source = tmp_path / "added.txt"
        source.write_bytes(b"new")
        session.import_paths([source], "HomeDomain", "Documents")
        session.keybag.keys.pop(3)
        with pytest.raises(BackupError, match=r"保护类别 3.*密钥包.*没有对应密钥"):
            session.save(tmp_path / "output")
    finally:
        session.close()


def test_new_file_without_existing_file_candidates(backup_root, tmp_path):
    session = BackupSession(backup_root, PASSWORD)
    try:
        tokens = [
            row[0]
            for row in session.db.execute("SELECT token FROM EditorEntries WHERE kind='file'")
        ]
        session.delete(tokens)
        source = tmp_path / "added.txt"
        source.write_bytes(b"new")
        session.import_paths([source], "HomeDomain", "Documents")
        output = session.save(tmp_path / "output")
        reopened = BackupSession(output, PASSWORD)
        try:
            entry = reopened.at("HomeDomain", "Documents/added.txt")
            assert reopened.metadata(entry).get("ProtectionClass") == 3
        finally:
            reopened.close()
    finally:
        session.close()
