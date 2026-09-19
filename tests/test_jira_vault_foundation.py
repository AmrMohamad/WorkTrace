from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import threading
from io import BytesIO
from pathlib import Path

import pytest

from worktrace.archive.jira.repository import JiraArchiveRepository
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate, migrations, user_version
from worktrace.errors import (
    DatabaseError,
    KeychainError,
    RecoveryError,
    VaultFormatError,
    VaultIntegrityError,
)
from worktrace.vault.backup import create_epoch_backup, restore_epoch
from worktrace.vault.format import (
    DEFAULT_CHUNK_SIZE,
    VaultDescriptor,
    VaultObjectResult,
    decrypt_vault_object,
    read_vault_object,
    verify_vault_object,
    write_vault_object,
)
from worktrace.vault.keychain import KEYCHAIN_SERVICE, MacOSKeychain
from worktrace.vault.recovery import create_recovery_envelope, open_recovery_envelope


def _descriptor(
    data: bytes,
    *,
    object_id: str = "jatt:test",
    site_id: str = "jira-site:test",
    collection_id: str = "jcol:test",
    revision_id: str = "jrev:jcol:test:1",
) -> VaultDescriptor:
    return VaultDescriptor(
        site_id=site_id,
        collection_id=collection_id,
        revision_id=revision_id,
        object_id=object_id,
        kind="attachment_original",
        content_length=len(data),
        content_sha256=hashlib.sha256(data).hexdigest(),
        key_version=1,
    )


def _record_ranges(raw: bytes) -> list[tuple[int, int]]:
    descriptor_length = struct.unpack(">I", raw[5:9])[0]
    offset = 9 + descriptor_length + 24
    result: list[tuple[int, int]] = []
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset : offset + 4])[0]
        end = offset + 4 + length
        result.append((offset, end))
        offset = end
    return result


def test_populated_schema_six_migrates_additively_to_seven(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    connection = connect(database)
    try:
        for migration in migrations()[:6]:
            connection.executescript(migration.sql)
            connection.execute(f"PRAGMA user_version={migration.version}")
        connection.execute("INSERT INTO apps(id, name) VALUES ('sample', 'Sample')")
        connection.execute(
            "INSERT INTO human_decisions(id, action, target_id, payload_json, created_at) "
            "VALUES ('decision:1', 'confirm', 'candidate:1', '{}', '2026-01-01T00:00:00+00:00')"
        )
        connection.commit()
        assert migrate(connection, database) == [7]
        assert user_version(connection) == 7
        assert (
            connection.execute("SELECT name FROM apps WHERE id='sample'").fetchone()[0] == "Sample"
        )
        assert connection.execute("SELECT id FROM human_decisions").fetchone()[0] == "decision:1"
        assert connection.execute("SELECT COUNT(*) FROM jira_resource_states").fetchone()[0] == 0
    finally:
        connection.close()


def test_resource_checkpoint_is_atomic_and_resumable(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    connection = connect(database)
    try:
        migrate(connection, database)
        repository = JiraArchiveRepository(connection)
        site_id = repository.ensure_site("https://jira.example.test/")
        collection_id = repository.create_collection(
            site_id=site_id,
            scope={"roots": ["10001"]},
            scope_hash="scope",
            approval_token_hash="token",
            policy_version=1,
            config_fingerprint="config",
            vault_id="vault:test",
        )
        run_id = repository.start_run(collection_id)
        revision_id = repository.create_revision(collection_id, run_id, "manifest")
        repository.create_resource_checkpoint(
            resource_id="jres:test",
            collection_id=collection_id,
            revision_id=revision_id,
            run_id=run_id,
            issue_id="10001",
            kind="comments",
            locator={"page": 1},
            role="root",
            redaction_version="1",
        )
        repository.checkpoint_resource(
            resource_id="jres:test",
            state="fetching",
            completeness="partial",
            availability="available",
            seen_count=2,
            page_cursor="next-page",
            attempt=1,
        )
        checkpoint = repository.get_checkpoint("jres:test")
        assert checkpoint.page_cursor == "next-page"
        assert checkpoint.seen_count == 2
        with pytest.raises(DatabaseError):
            repository.checkpoint_resource(
                resource_id="missing",
                state="complete",
                completeness="complete",
                availability="available",
                seen_count=2,
                page_cursor=None,
                attempt=1,
            )
        assert repository.get_checkpoint("jres:test").state == "fetching"
    finally:
        connection.close()


def test_vault_stream_roundtrip_checkpoint_and_empty_object(tmp_path: Path) -> None:
    key = b"k" * 32
    data = b"vault data" * 100
    path = tmp_path / "object.wtva"
    checkpoints = []
    result = write_vault_object(
        BytesIO(data),
        path,
        _descriptor(data),
        key,
        vault_root=tmp_path,
        checkpoint=checkpoints.append,
    )
    assert result.plaintext_length == len(data)
    assert checkpoints
    assert read_vault_object(path, key) == data

    empty = b""
    empty_path = tmp_path / "empty.wtva"
    write_vault_object(
        BytesIO(empty),
        empty_path,
        _descriptor(empty, object_id="jatt:empty"),
        key,
        vault_root=tmp_path,
    )
    assert read_vault_object(empty_path, key) == b""


def test_vault_rejects_wrong_key_corruption_truncation_reorder_and_trailing_bytes(
    tmp_path: Path,
) -> None:
    key = b"k" * 32
    data = b"a" * (DEFAULT_CHUNK_SIZE * 2 + 17)
    path = tmp_path / "object.wtva"
    write_vault_object(BytesIO(data), path, _descriptor(data), key, vault_root=tmp_path)
    raw = path.read_bytes()

    with pytest.raises(VaultIntegrityError):
        read_vault_object(path, b"w" * 32)
    path.write_bytes(raw[:-1])
    with pytest.raises(VaultIntegrityError):
        read_vault_object(path, key)
    path.write_bytes(raw + b"trailing")
    with pytest.raises(VaultIntegrityError):
        read_vault_object(path, key)
    ranges = _record_ranges(raw)
    assert len(ranges) >= 2
    reordered = raw[: ranges[0][0]] + b"".join(raw[start:end] for start, end in reversed(ranges))
    path.write_bytes(reordered)
    with pytest.raises(VaultIntegrityError):
        read_vault_object(path, key)

    descriptor_length = struct.unpack(">I", raw[5:9])[0]
    descriptor_start = 9
    descriptor_end = descriptor_start + descriptor_length
    tampered = bytearray(raw)
    marker = b'"content_sha256":"'
    index = raw.find(marker, descriptor_start, descriptor_end) + len(marker)
    tampered[index] = ord("0") if tampered[index] != ord("0") else ord("1")
    path.write_bytes(tampered)
    with pytest.raises(VaultIntegrityError):
        read_vault_object(path, key)


def test_vault_interrupted_writer_does_not_publish_partial_object(tmp_path: Path) -> None:
    class FailingSource(BytesIO):
        def __init__(self, value: bytes) -> None:
            super().__init__(value)
            self.calls = 0

        def read(self, size: int = -1) -> bytes:
            self.calls += 1
            if self.calls > 1:
                raise OSError("synthetic interruption")
            return super().read(size)

    data = b"x" * (DEFAULT_CHUNK_SIZE + 1)
    destination = tmp_path / "object.wtva"
    with pytest.raises(OSError):
        write_vault_object(
            FailingSource(data), destination, _descriptor(data), b"k" * 32, vault_root=tmp_path
        )
    assert not destination.exists()
    assert not list(tmp_path.glob(".wtva-*"))

    write_vault_object(
        BytesIO(b"x"), destination, _descriptor(b"x"), b"k" * 32, vault_root=tmp_path
    )
    with pytest.raises(FileExistsError):
        write_vault_object(
            BytesIO(b"x"), destination, _descriptor(b"x"), b"k" * 32, vault_root=tmp_path
        )


def test_vault_publication_requires_a_real_root_and_parent_chain(tmp_path: Path) -> None:
    data = b"boundary"
    real_root = tmp_path / "real-vault"
    real_root.mkdir()
    root_link = tmp_path / "root-link"
    root_link.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(VaultFormatError):
        write_vault_object(
            BytesIO(data),
            root_link / "object.wtva",
            _descriptor(data),
            b"k" * 32,
            vault_root=root_link,
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    parent_link = real_root / "nested"
    parent_link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(VaultFormatError):
        write_vault_object(
            BytesIO(data),
            parent_link / "object.wtva",
            _descriptor(data),
            b"k" * 32,
            vault_root=real_root,
        )
    with pytest.raises(VaultFormatError):
        write_vault_object(
            BytesIO(data),
            outside / "escape.wtva",
            _descriptor(data),
            b"k" * 32,
            vault_root=real_root,
        )


def test_backup_copy_streams_without_path_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import worktrace.vault.backup as backup_module

    source = tmp_path / "large-source.bin"
    destination = tmp_path / "copy" / "large-destination.bin"
    payload = b"streamed" * (1024 * 1024 // 8 + 1)
    source.write_bytes(payload)

    def fail_read_bytes(self: Path) -> bytes:
        raise AssertionError("ciphertext copy must not use Path.read_bytes")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    backup_module._copy_private(source, destination)
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.stat().st_size == len(payload)
    monkeypatch.undo()
    assert destination.read_bytes() == payload


def test_backup_verification_does_not_allocate_plaintext_tempfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import worktrace.vault.format as format_module

    data = b"verification-only" * 100
    path = tmp_path / "object.wtva"
    result = write_vault_object(
        BytesIO(data), path, _descriptor(data), b"k" * 32, vault_root=tmp_path
    )

    def fail_temporary_file(*args: object, **kwargs: object) -> object:
        raise AssertionError("verification must not allocate a plaintext TemporaryFile")

    monkeypatch.setattr(format_module.tempfile, "TemporaryFile", fail_temporary_file)
    with path.open("rb") as source:
        verified = verify_vault_object(
            source, b"k" * 32, expected_ciphertext_sha256=result.ciphertext_sha256
        )
    assert verified.ciphertext_sha256 == result.ciphertext_sha256


def test_corrupt_vault_never_publishes_partial_plaintext(tmp_path: Path) -> None:
    data = b"verified only after final tag"
    path = tmp_path / "object.wtva"
    result = write_vault_object(
        BytesIO(data), path, _descriptor(data), b"k" * 32, vault_root=tmp_path
    )
    corrupt = bytearray(path.read_bytes())
    corrupt[-1] ^= 1
    path.write_bytes(corrupt)
    sink = BytesIO(b"unchanged")
    with path.open("rb") as source, pytest.raises(VaultIntegrityError):
        decrypt_vault_object(
            source,
            sink,
            b"k" * 32,
            expected_ciphertext_sha256=result.ciphertext_sha256,
        )
    assert sink.getvalue() == b"unchanged"


def _recovery_descriptor() -> dict[str, object]:
    return {
        "format": "worktrace-jira-recovery",
        "version": 1,
        "installation_id": "install:test",
        "site_ids": ["jira-site:test"],
        "collection_ids": ["jcol:test"],
        "vault_id": "vault:test",
        "epoch_id": "epoch:test",
        "key_versions": [1, 2],
        "schema_version": 1,
        "kdf": {"name": "argon2id", "opslimit": 1, "memlimit": 8 * 1024 * 1024, "dk_len": 32},
        "aead": {"name": "xchacha20-poly1305-ietf"},
    }


def test_recovery_envelope_known_answer_and_rejects_tampering() -> None:
    descriptor = _recovery_descriptor()
    envelope = create_recovery_envelope(
        descriptor,
        {1: b"a" * 32, 2: b"b" * 32},
        "passphrase-12",
        salt=b"s" * 16,
        nonce=b"n" * 24,
    )
    assert hashlib.sha256(envelope).hexdigest() == (
        "a1d0ea5631ab3dc121b8183210126f5d2f747859f9f219532d44a889148cfbaa"
    )
    opened = open_recovery_envelope(envelope, "passphrase-12")
    assert opened.keys == {1: b"a" * 32, 2: b"b" * 32}
    with pytest.raises(RecoveryError):
        open_recovery_envelope(envelope, "wrong-passphrase")
    with pytest.raises(RecoveryError):
        open_recovery_envelope(envelope[:-1], "passphrase-12")
    with pytest.raises(RecoveryError):
        open_recovery_envelope(envelope + b"extra", "passphrase-12")


class _FakeKeychain:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self.values[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


def test_keychain_is_explicit_versioned_and_fails_closed() -> None:
    fake = _FakeKeychain()
    store = MacOSKeychain.open("install:test", backend=fake, platform_name="Darwin")
    first = store.ensure()
    second = store.rotate(2)
    assert len(first) == len(second) == 32
    assert store.get(1) == first
    assert store.get(2) == second
    assert (KEYCHAIN_SERVICE, "install:test:1") in fake.values
    with pytest.raises(KeychainError):
        MacOSKeychain.open("install:test", backend=fake, platform_name="Linux")
    with pytest.raises(KeychainError):
        store.rotate(2)
    assert not hasattr(store, "retire")


def test_epoch_backup_restore_and_tamper_refusal(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    connection = connect(database)
    try:
        migrate(connection, database)
        repository = JiraArchiveRepository(connection)
        site_id = repository.ensure_site("https://jira.example.test")
        collection_id = repository.create_collection(
            site_id=site_id,
            scope={"roots": ["10001"]},
            scope_hash="scope",
            approval_token_hash="token",
            policy_version=1,
            config_fingerprint="config",
            vault_id="vault:test",
        )
        run_id = repository.start_run(collection_id)
        connection.execute(
            "UPDATE jira_collection_runs SET status='complete', "
            "completed_at='2026-01-01T00:00:00+00:00' WHERE id=?",
            (run_id,),
        )
        connection.commit()
        revision_id = repository.create_revision(collection_id, run_id, "manifest")
    finally:
        connection.close()
    config = tmp_path / "config.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")
    hmac_key = tmp_path / "email-hmac.key"
    hmac_key.write_bytes(b"h" * 64)
    vault = tmp_path / "vault"
    vault.mkdir()
    data = b"original"
    object_result = write_vault_object(
        BytesIO(data),
        vault / "object.wtva",
        _descriptor(data, site_id=site_id, collection_id=collection_id, revision_id=revision_id),
        b"k" * 32,
        vault_root=vault,
    )
    connection = connect(database)
    try:
        connection.execute(
            "INSERT INTO jira_attachment_objects "
            "(id, collection_id, revision_id, issue_id, attachment_id, archive_evidence_id, "
            "filename, manifest_sha256, original_state, vault_object_id, vault_object_path, "
            "vault_key_version, ciphertext_sha256, extracted_state, source_locator) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "jatt:test",
                collection_id,
                revision_id,
                "10001",
                "1",
                "jare:test",
                "object.bin",
                "manifest",
                "complete",
                "jatt:test",
                "object.wtva",
                1,
                object_result.ciphertext_sha256,
                "not_requested",
                "object.wtva",
            ),
        )
        unavailable_repository = JiraArchiveRepository(connection)
        unavailable_repository.create_resource_checkpoint(
            resource_id="jres:unavailable",
            collection_id=collection_id,
            revision_id=revision_id,
            run_id=run_id,
            issue_id="10001",
            kind="comments",
            locator={"page": 1},
            role="root",
            redaction_version="1",
            state="unavailable",
        )
        unavailable_repository.checkpoint_resource(
            resource_id="jres:unavailable",
            state="unavailable",
            completeness="unavailable",
            availability="unavailable",
            seen_count=0,
            page_cursor=None,
            attempt=1,
        )
        connection.commit()
    finally:
        connection.close()
    epoch = tmp_path / "epoch"
    backup = create_epoch_backup(
        database_path=database,
        config_path=config,
        hmac_key_path=hmac_key,
        vault_root=vault,
        output=epoch,
        collection_id=collection_id,
        revision_id=revision_id,
        installation_id="install:test",
        vault_id="vault:test",
        key_versions={1: b"k" * 32},
        passphrase="passphrase-12",
    )
    restored = tmp_path / "restored"
    result = restore_epoch(epoch=epoch, destination=restored, passphrase="passphrase-12")
    assert result["verified"] is True
    assert user_version(connect(restored / "worktrace.sqlite3")) == 7
    assert read_vault_object(restored / "vault/object.wtva", b"k" * 32) == data
    assert backup.manifest["complete"] is True
    restored_connection = connect(restored / "worktrace.sqlite3")
    try:
        assert tuple(
            restored_connection.execute(
                "SELECT state, availability, raw_vault_object_path, raw_vault_object_id "
                "FROM jira_resource_states WHERE id='jres:unavailable'"
            ).fetchone()
        ) == ("unavailable", "unavailable", None, None)
    finally:
        restored_connection.close()

    (epoch / "config.toml").write_text("tampered", encoding="utf-8")
    with pytest.raises(RecoveryError):
        restore_epoch(epoch=epoch, destination=tmp_path / "bad", passphrase="passphrase-12")
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "existing").write_text("x", encoding="utf-8")
    with pytest.raises(RecoveryError):
        restore_epoch(epoch=epoch, destination=nonempty, passphrase="passphrase-12")

    (epoch / "config.toml").write_text("schema_version = 1\n", encoding="utf-8")
    manifest = json.loads((epoch / "epoch.json").read_text(encoding="utf-8"))
    manifest["identity"]["vault_id"] = "vault:evil"
    (epoch / "epoch.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RecoveryError):
        restore_epoch(
            epoch=epoch, destination=tmp_path / "identity-mismatch", passphrase="passphrase-12"
        )

    epoch_link = tmp_path / "epoch-link"
    epoch_link.symlink_to(epoch, target_is_directory=True)
    with pytest.raises(RecoveryError):
        restore_epoch(
            epoch=epoch_link, destination=tmp_path / "symlink-restore", passphrase="passphrase-12"
        )

    vault_link = tmp_path / "vault-link"
    vault_link.symlink_to(vault, target_is_directory=True)
    with pytest.raises(RecoveryError):
        create_epoch_backup(
            database_path=database,
            config_path=config,
            hmac_key_path=hmac_key,
            vault_root=vault_link,
            output=tmp_path / "symlink-backup",
            collection_id=collection_id,
            revision_id=revision_id,
            installation_id="install:test",
            vault_id="vault:test",
            key_versions={1: b"k" * 32},
            passphrase="passphrase-12",
        )

    with pytest.raises(VaultIntegrityError):
        create_epoch_backup(
            database_path=database,
            config_path=config,
            hmac_key_path=hmac_key,
            vault_root=vault,
            output=tmp_path / "missing-key-backup",
            collection_id=collection_id,
            revision_id=revision_id,
            installation_id="install:test",
            vault_id="vault:test",
            key_versions={2: b"z" * 32},
            passphrase="passphrase-12",
        )
    source_object = vault / "object.wtva"
    source_object.write_bytes(source_object.read_bytes()[:-1])
    with pytest.raises(VaultIntegrityError):
        create_epoch_backup(
            database_path=database,
            config_path=config,
            hmac_key_path=hmac_key,
            vault_root=vault,
            output=tmp_path / "corrupt-backup",
            collection_id=collection_id,
            revision_id=revision_id,
            installation_id="install:test",
            vault_id="vault:test",
            key_versions={1: b"k" * 32},
            passphrase="passphrase-12",
        )
    assert not (tmp_path / "corrupt-backup").exists()


def test_epoch_backup_rejects_partial_vault_tuple(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    connection = connect(database)
    try:
        migrate(connection, database)
        repository = JiraArchiveRepository(connection)
        site_id = repository.ensure_site("https://jira.example.test")
        collection_id = repository.create_collection(
            site_id=site_id,
            scope={"roots": ["10001"]},
            scope_hash="scope",
            approval_token_hash="token",
            policy_version=1,
            config_fingerprint="config",
            vault_id="vault:test",
        )
        run_id = repository.start_run(collection_id)
        connection.execute(
            "UPDATE jira_collection_runs SET status='complete', "
            "completed_at='2026-01-01T00:00:00+00:00' WHERE id=?",
            (run_id,),
        )
        connection.commit()
        revision_id = repository.create_revision(collection_id, run_id, "manifest")
        connection.execute(
            "INSERT INTO jira_attachment_objects "
            "(id, collection_id, revision_id, issue_id, attachment_id, archive_evidence_id, "
            "filename, manifest_sha256, original_state, extracted_state, source_locator, "
            "vault_object_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "jatt:partial",
                collection_id,
                revision_id,
                "10001",
                "1",
                "jare:partial",
                "object.bin",
                "manifest",
                "unavailable",
                "not_requested",
                "object.wtva",
                "object.wtva",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    config = tmp_path / "config.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")
    hmac_key = tmp_path / "email-hmac.key"
    hmac_key.write_bytes(b"h" * 64)
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(RecoveryError, match="partial vault tuple"):
        create_epoch_backup(
            database_path=database,
            config_path=config,
            hmac_key_path=hmac_key,
            vault_root=vault,
            output=tmp_path / "partial-epoch",
            collection_id=collection_id,
            revision_id=revision_id,
            installation_id="install:test",
            vault_id="vault:test",
            key_versions={1: b"k" * 32},
            passphrase="passphrase-12",
        )
    assert not (tmp_path / "partial-epoch").exists()


def test_epoch_backup_holds_single_writer_quiescence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "ledger.sqlite3"
    connection = connect(database)
    try:
        migrate(connection, database)
        repository = JiraArchiveRepository(connection)
        site_id = repository.ensure_site("https://jira.example.test")
        collection_id = repository.create_collection(
            site_id=site_id,
            scope={"roots": ["10001"]},
            scope_hash="scope",
            approval_token_hash="token",
            policy_version=1,
            config_fingerprint="config",
            vault_id="vault:test",
        )
        run_id = repository.start_run(collection_id)
        connection.execute(
            "UPDATE jira_collection_runs SET status='complete', "
            "completed_at='2026-01-01T00:00:00+00:00' WHERE id=?",
            (run_id,),
        )
        connection.commit()
        revision_id = repository.create_revision(collection_id, run_id, "manifest")
    finally:
        connection.close()
    config = tmp_path / "config.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")
    hmac_key = tmp_path / "email-hmac.key"
    hmac_key.write_bytes(b"h" * 64)
    vault = tmp_path / "vault"
    vault.mkdir()
    data = b"quiesced"
    object_result = write_vault_object(
        BytesIO(data),
        vault / "object.wtva",
        _descriptor(data, site_id=site_id, collection_id=collection_id, revision_id=revision_id),
        b"k" * 32,
        vault_root=vault,
    )
    connection = connect(database)
    try:
        connection.execute(
            "INSERT INTO jira_attachment_objects "
            "(id, collection_id, revision_id, issue_id, attachment_id, archive_evidence_id, "
            "filename, manifest_sha256, original_state, vault_object_id, vault_object_path, "
            "vault_key_version, ciphertext_sha256, extracted_state, source_locator) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "jatt:test",
                collection_id,
                revision_id,
                "10001",
                "1",
                "jare:test",
                "object.bin",
                "manifest",
                "complete",
                "jatt:test",
                "object.wtva",
                1,
                object_result.ciphertext_sha256,
                "not_requested",
                "object.wtva",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    import worktrace.vault.backup as backup_module

    original_verify = backup_module._verify_vault_inventory
    reached = threading.Event()
    release = threading.Event()
    calls = 0

    def blocking_verify(root: Path, identity: dict[str, object], keys: dict[int, bytes]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            reached.set()
            assert release.wait(3)
        original_verify(root, identity, keys)

    monkeypatch.setattr(backup_module, "_verify_vault_inventory", blocking_verify)
    errors: list[BaseException] = []

    def run_backup() -> None:
        try:
            create_epoch_backup(
                database_path=database,
                config_path=config,
                hmac_key_path=hmac_key,
                vault_root=vault,
                output=tmp_path / "epoch",
                collection_id=collection_id,
                revision_id=revision_id,
                installation_id="install:test",
                vault_id="vault:test",
                key_versions={1: b"k" * 32},
                passphrase="passphrase-12",
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run_backup)
    worker.start()
    assert reached.wait(3)
    writer = sqlite3.connect(database, autocommit=True, timeout=0.05)
    try:
        with pytest.raises(sqlite3.OperationalError):
            writer.execute("BEGIN IMMEDIATE")
    finally:
        writer.close()
        release.set()
        worker.join(timeout=5)
    assert not errors


def test_epoch_backup_isolates_referenced_objects_by_collection_and_revision(
    tmp_path: Path,
) -> None:
    database = tmp_path / "ledger.sqlite3"
    connection = connect(database)
    try:
        migrate(connection, database)
        repository = JiraArchiveRepository(connection)
        site_id = repository.ensure_site("https://jira.example.test")
        collections: list[tuple[str, str]] = []
        for index in (1, 2):
            collection = repository.create_collection(
                site_id=site_id,
                scope={"roots": [str(index)]},
                scope_hash=f"scope-{index}",
                approval_token_hash=f"token-{index}",
                policy_version=1,
                config_fingerprint=f"config-{index}",
                vault_id=f"vault:{index}",
            )
            run = repository.start_run(collection)
            connection.execute(
                "UPDATE jira_collection_runs SET status='complete', "
                "completed_at='2026-01-01T00:00:00+00:00' WHERE id=?",
                (run,),
            )
            revision = repository.create_revision(collection, run, f"manifest-{index}")
            collections.append((collection, revision))
            connection.execute(
                "INSERT INTO jira_attachment_objects "
                "(id, collection_id, revision_id, issue_id, attachment_id, archive_evidence_id, "
                "filename, manifest_sha256, original_state, vault_object_id, extracted_state, "
                "source_locator, vault_object_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"jatt:{index}",
                    collection,
                    revision,
                    str(index),
                    str(index),
                    f"jare:{index}",
                    "object.bin",
                    "manifest",
                    "complete",
                    f"jatt:{index}",
                    "not_requested",
                    f"{collection}/object.wtva",
                    f"{collection}/object.wtva",
                ),
            )
        connection.commit()
    finally:
        connection.close()
    config = tmp_path / "config.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")
    hmac_key = tmp_path / "email-hmac.key"
    hmac_key.write_bytes(b"h" * 64)
    vault = tmp_path / "vault"
    vault.mkdir()
    object_results: dict[str, VaultObjectResult] = {}
    for index, (collection, _) in enumerate(collections, start=1):
        data = f"object-{index}".encode()
        object_results[collection] = write_vault_object(
            BytesIO(data),
            vault / collection / "object.wtva",
            _descriptor(
                data,
                object_id=f"jatt:{index}",
                site_id=site_id,
                collection_id=collection,
                revision_id=collections[index - 1][1],
            ),
            b"k" * 32,
            vault_root=vault,
        )
    connection = connect(database)
    try:
        for _index, (collection, _) in enumerate(collections, start=1):
            result = object_results[collection]
            connection.execute(
                "UPDATE jira_attachment_objects SET ciphertext_sha256=?, vault_key_version=? "
                "WHERE collection_id=?",
                (result.ciphertext_sha256, 1, collection),
            )
        connection.commit()
    finally:
        connection.close()
    epoch = tmp_path / "epoch"
    create_epoch_backup(
        database_path=database,
        config_path=config,
        hmac_key_path=hmac_key,
        vault_root=vault,
        output=epoch,
        collection_id=collections[0][0],
        revision_id=collections[0][1],
        installation_id="install:test",
        vault_id="vault:1",
        key_versions={1: b"k" * 32},
        passphrase="passphrase-12",
    )
    manifest = json.loads((epoch / "epoch.json").read_text(encoding="utf-8"))
    assert set(manifest["vault_inventory"]) == {f"{collections[0][0]}/object.wtva"}

    first_path = vault / collections[0][0] / "object.wtva"
    second_path = vault / collections[1][0] / "object.wtva"
    first_bytes = first_path.read_bytes()
    second_bytes = second_path.read_bytes()
    first_path.write_bytes(second_bytes)
    second_path.write_bytes(first_bytes)
    connection = connect(database)
    try:
        first_hash = object_results[collections[0][0]].ciphertext_sha256
        second_hash = object_results[collections[1][0]].ciphertext_sha256
        connection.execute(
            "UPDATE jira_attachment_objects SET ciphertext_sha256=? WHERE collection_id=?",
            (second_hash, collections[0][0]),
        )
        connection.execute(
            "UPDATE jira_attachment_objects SET ciphertext_sha256=? WHERE collection_id=?",
            (first_hash, collections[1][0]),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(VaultIntegrityError, match="descriptor identity mismatch"):
        create_epoch_backup(
            database_path=database,
            config_path=config,
            hmac_key_path=hmac_key,
            vault_root=vault,
            output=tmp_path / "swapped-epoch",
            collection_id=collections[0][0],
            revision_id=collections[0][1],
            installation_id="install:test",
            vault_id="vault:1",
            key_versions={1: b"k" * 32},
            passphrase="passphrase-12",
        )
