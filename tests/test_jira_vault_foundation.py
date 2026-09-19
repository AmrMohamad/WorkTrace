from __future__ import annotations

import hashlib
import struct
from io import BytesIO
from pathlib import Path

import pytest

from worktrace.archive.jira.repository import JiraArchiveRepository
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate, migrations, user_version
from worktrace.errors import DatabaseError, KeychainError, RecoveryError, VaultIntegrityError
from worktrace.vault.backup import create_epoch_backup, restore_epoch
from worktrace.vault.format import (
    DEFAULT_CHUNK_SIZE,
    VaultDescriptor,
    read_vault_object,
    write_vault_object,
)
from worktrace.vault.keychain import KEYCHAIN_SERVICE, MacOSKeychain
from worktrace.vault.recovery import create_recovery_envelope, open_recovery_envelope


def _descriptor(data: bytes, *, object_id: str = "jatt:test") -> VaultDescriptor:
    return VaultDescriptor(
        site_id="jira-site:test",
        collection_id="jcol:test",
        revision_id="jrev:jcol:test:1",
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
        BytesIO(data), path, _descriptor(data), key, checkpoint=checkpoints.append
    )
    assert result.plaintext_length == len(data)
    assert checkpoints
    assert read_vault_object(path, key) == data

    empty = b""
    empty_path = tmp_path / "empty.wtva"
    write_vault_object(BytesIO(empty), empty_path, _descriptor(empty, object_id="jatt:empty"), key)
    assert read_vault_object(empty_path, key) == b""


def test_vault_rejects_wrong_key_corruption_truncation_reorder_and_trailing_bytes(
    tmp_path: Path,
) -> None:
    key = b"k" * 32
    data = b"a" * (DEFAULT_CHUNK_SIZE * 2 + 17)
    path = tmp_path / "object.wtva"
    write_vault_object(BytesIO(data), path, _descriptor(data), key)
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
        write_vault_object(FailingSource(data), destination, _descriptor(data), b"k" * 32)
    assert not destination.exists()
    assert not list(tmp_path.glob(".wtva-*"))

    write_vault_object(BytesIO(b"x"), destination, _descriptor(b"x"), b"k" * 32)
    with pytest.raises(FileExistsError):
        write_vault_object(BytesIO(b"x"), destination, _descriptor(b"x"), b"k" * 32)


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


def test_epoch_backup_restore_and_tamper_refusal(tmp_path: Path) -> None:
    database = tmp_path / "ledger.sqlite3"
    connection = connect(database)
    try:
        migrate(connection, database)
    finally:
        connection.close()
    config = tmp_path / "config.toml"
    config.write_text("schema_version = 1\n", encoding="utf-8")
    hmac_key = tmp_path / "email-hmac.key"
    hmac_key.write_bytes(b"h" * 64)
    vault = tmp_path / "vault"
    vault.mkdir()
    data = b"original"
    write_vault_object(
        BytesIO(data),
        vault / "object.wtva",
        _descriptor(data),
        b"k" * 32,
    )
    epoch = tmp_path / "epoch"
    backup = create_epoch_backup(
        database_path=database,
        config_path=config,
        hmac_key_path=hmac_key,
        vault_root=vault,
        output=epoch,
        collection_id="jcol:test",
        revision_id="jrev:jcol:test:1",
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

    (epoch / "config.toml").write_text("tampered", encoding="utf-8")
    with pytest.raises(RecoveryError):
        restore_epoch(epoch=epoch, destination=tmp_path / "bad", passphrase="passphrase-12")
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "existing").write_text("x", encoding="utf-8")
    with pytest.raises(RecoveryError):
        restore_epoch(epoch=epoch, destination=nonempty, passphrase="passphrase-12")
