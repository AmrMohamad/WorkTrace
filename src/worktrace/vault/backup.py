from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from worktrace.db.migrations import backup_database, user_version
from worktrace.errors import RecoveryError, VaultIntegrityError
from worktrace.vault.format import read_vault_descriptor, verify_vault_object
from worktrace.vault.recovery import create_recovery_envelope, open_recovery_envelope


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _assert_no_symlink_chain(path: Path) -> None:
    path = _absolute(path)
    for item in [*list(reversed(path.parents)), path]:
        try:
            if item.is_symlink():
                raise RecoveryError(f"vault epoch path contains a symlink: {item}")
        except OSError as exc:
            raise RecoveryError("vault epoch path cannot be inspected safely") from exc


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _write_private(path: Path, data: bytes) -> None:
    _assert_no_symlink_chain(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("private file write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_private(source: Path, destination: Path) -> None:
    _assert_no_symlink_chain(source)
    _assert_no_symlink_chain(destination)
    if source.is_symlink() or not source.is_file():
        raise VaultIntegrityError(f"backup source is not a regular file: {source.name}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_no_symlink_chain(destination.parent)
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, source_flags)
    destination_fd: int | None = None
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            raise VaultIntegrityError(f"backup source is not a regular file: {source.name}")
        destination_fd = os.open(destination, destination_flags, 0o600)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("private file copy made no progress")
                view = view[written:]
        os.fsync(destination_fd)
    except BaseException:
        if destination_fd is not None:
            with suppress(FileNotFoundError):
                os.unlink(destination)
        raise
    finally:
        os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)


def _inventory(root: Path) -> dict[str, str]:
    root = _absolute(root)
    _assert_no_symlink_chain(root)
    if root.is_symlink() or not root.is_dir():
        raise VaultIntegrityError("vault root must be a real directory")
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise VaultIntegrityError("vault backup refuses symlinked objects")
        if path.is_file():
            result[str(path.relative_to(root))] = _hash(path)
    return result


@dataclass(frozen=True, slots=True)
class EpochBackup:
    epoch_id: str
    output: Path
    manifest: dict[str, object]


def _open_quiesced(database_path: Path) -> sqlite3.Connection:
    _assert_no_symlink_chain(database_path)
    connection = sqlite3.connect(database_path, autocommit=True, timeout=5)
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error:
        connection.close()
        raise RecoveryError("could not quiesce the SQLite writer") from None
    return connection


def _epoch_identity(
    connection: sqlite3.Connection,
    *,
    collection_id: str,
    revision_id: str,
    vault_id: str,
    key_versions: Mapping[int, bytes],
) -> dict[str, object]:
    archive_row = connection.execute(
        "SELECT c.site_id, c.id, c.vault_id, r.id, r.collection_id "
        "FROM jira_collections c JOIN jira_archive_revisions r "
        "ON r.collection_id=c.id WHERE c.id=? AND r.id=?",
        (collection_id, revision_id),
    ).fetchone()
    if (
        archive_row is None
        or str(archive_row[2]) != vault_id
        or str(archive_row[4]) != collection_id
    ):
        raise RecoveryError("SQLite archive identity does not match the requested epoch")
    if connection.execute(
        "SELECT COUNT(*) FROM jira_collection_runs WHERE collection_id=? AND status='running'",
        (collection_id,),
    ).fetchone()[0]:
        raise RecoveryError("cannot back up while an archive run is active")
    if connection.execute(
        "SELECT COUNT(*) FROM jira_resource_states WHERE collection_id=? AND state='fetching'",
        (collection_id,),
    ).fetchone()[0]:
        raise RecoveryError("cannot back up while an archive resource is fetching")
    if not key_versions or any(len(key) != 32 for key in key_versions.values()):
        raise RecoveryError("epoch key versions are missing or malformed")
    objects: list[dict[str, object]] = []
    rows = connection.execute(
        "SELECT raw_vault_object_path, raw_vault_object_id, raw_vault_ciphertext_sha256, "
        "raw_vault_key_version, kind FROM jira_resource_states WHERE collection_id=? "
        "AND revision_id=? "
        "UNION ALL SELECT vault_object_path, vault_object_id, ciphertext_sha256, "
        "vault_key_version, 'attachment_original' FROM jira_attachment_objects "
        "WHERE collection_id=? AND revision_id=?",
        (collection_id, revision_id, collection_id, revision_id),
    )
    for object_row in rows:
        relative_path, object_id, ciphertext_hash, key_version, kind = object_row
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or Path(relative_path).is_absolute()
            or ".." in Path(relative_path).parts
            or str(Path(relative_path)) != relative_path
            or not isinstance(object_id, str)
            or not object_id
            or not isinstance(ciphertext_hash, str)
            or len(ciphertext_hash) != 64
            or ciphertext_hash != ciphertext_hash.lower()
            or not isinstance(key_version, int)
            or key_version < 1
            or not isinstance(kind, str)
            or not kind
        ):
            raise RecoveryError("archive vault object manifest is incomplete or unsafe")
        objects.append(
            {
                "relative_path": relative_path,
                "object_id": object_id,
                "ciphertext_sha256": ciphertext_hash,
                "key_version": key_version,
                "kind": kind,
            }
        )
    if len({str(item["relative_path"]) for item in objects}) != len(objects):
        raise RecoveryError("archive vault object manifest has duplicate paths")
    objects.sort(key=lambda item: str(item["relative_path"]))
    return {
        "site_ids": [str(archive_row[0])],
        "collection_ids": [collection_id],
        "revision_ids": [revision_id],
        "vault_id": vault_id,
        "key_versions": sorted(key_versions),
        "ledger_schema_version": 7,
        "vault_objects": objects,
    }


def _verify_sqlite_identity(database_path: Path, *, identity: Mapping[str, object]) -> None:
    with sqlite3.connect(database_path) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RecoveryError("epoch SQLite integrity check failed")
        if user_version(connection) != 7:
            raise RecoveryError("epoch SQLite schema is not version 7")
        collection_ids = cast(list[object], identity["collection_ids"])
        revision_ids = cast(list[object], identity["revision_ids"])
        collection_id = str(collection_ids[0])
        revision_id = str(revision_ids[0])
        row = connection.execute(
            "SELECT c.site_id, c.vault_id, r.collection_id FROM jira_collections c "
            "JOIN jira_archive_revisions r ON r.collection_id=c.id "
            "WHERE c.id=? AND r.id=?",
            (collection_id, revision_id),
        ).fetchone()
        if (
            row is None
            or [str(row[0])] != identity["site_ids"]
            or str(row[1]) != identity["vault_id"]
        ):
            raise RecoveryError("epoch SQLite identity does not match its manifest")
        expected_objects = cast(list[dict[str, object]], identity["vault_objects"])
        actual_objects = _epoch_identity(
            connection,
            collection_id=collection_id,
            revision_id=revision_id,
            vault_id=str(identity["vault_id"]),
            key_versions={
                int(cast(int, version)): b"k" * 32
                for version in cast(list[object], identity["key_versions"])
            },
        )["vault_objects"]
        if actual_objects != expected_objects:
            raise RecoveryError("epoch SQLite vault object manifest does not match its identity")


def _verify_vault_inventory(
    root: Path, identity: Mapping[str, object], keys: Mapping[int, bytes]
) -> None:
    expected_objects = cast(list[dict[str, object]], identity["vault_objects"])
    for expected in expected_objects:
        relative = str(expected["relative_path"])
        expected_hash = str(expected["ciphertext_sha256"])
        path = root / relative
        _assert_no_symlink_chain(path)
        if _hash(path) != expected_hash:
            raise VaultIntegrityError(f"vault ciphertext hash mismatch: {relative}")
        with path.open("rb") as source:
            descriptor = read_vault_descriptor(source)
        key = keys.get(descriptor.key_version)
        if key is None:
            raise VaultIntegrityError(f"missing vault key version: {descriptor.key_version}")
        if (
            descriptor.site_id != str(cast(list[object], identity["site_ids"])[0])
            or descriptor.collection_id != str(cast(list[object], identity["collection_ids"])[0])
            or descriptor.revision_id != str(cast(list[object], identity["revision_ids"])[0])
            or descriptor.object_id != str(expected["object_id"])
            or descriptor.kind != str(expected["kind"])
            or descriptor.key_version != int(cast(int, expected["key_version"]))
        ):
            raise VaultIntegrityError(f"vault descriptor identity mismatch: {relative}")
        with path.open("rb") as source:
            verify_vault_object(source, key, expected_ciphertext_sha256=expected_hash)


def _scoped_inventory(root: Path, identity: Mapping[str, object]) -> dict[str, str]:
    all_inventory = _inventory(root)
    expected_objects = cast(list[dict[str, object]], identity["vault_objects"])
    expected = {str(item["relative_path"]) for item in expected_objects}
    missing = expected - set(all_inventory)
    if missing:
        raise VaultIntegrityError(f"referenced vault objects are missing: {sorted(missing)}")
    collection_id = str(cast(list[object], identity["collection_ids"])[0])
    revision_id = str(cast(list[object], identity["revision_ids"])[0])
    scoped_prefixes = (collection_id, revision_id)
    extras = {
        path
        for path in all_inventory
        if path not in expected and any(marker in path for marker in scoped_prefixes)
    }
    if extras:
        raise VaultIntegrityError(
            f"unreferenced vault objects are present for this revision: {sorted(extras)}"
        )
    return {path: all_inventory[path] for path in sorted(expected)}


def create_epoch_backup(
    *,
    database_path: Path,
    config_path: Path,
    hmac_key_path: Path,
    vault_root: Path,
    output: Path,
    collection_id: str,
    revision_id: str,
    installation_id: str,
    vault_id: str,
    key_versions: Mapping[int, bytes],
    passphrase: str,
) -> EpochBackup:
    """Create a portable epoch from one quiesced SQLite/archive snapshot."""
    database_path, config_path = _absolute(database_path), _absolute(config_path)
    hmac_key_path, vault_root, output = (
        _absolute(hmac_key_path),
        _absolute(vault_root),
        _absolute(output),
    )
    for path in (database_path, config_path, hmac_key_path, vault_root, output):
        _assert_no_symlink_chain(path)
    if output.exists():
        raise FileExistsError(output)
    guard = _open_quiesced(database_path)
    epoch_id = f"epoch:{uuid.uuid4()}"
    try:
        identity = _epoch_identity(
            guard,
            collection_id=collection_id,
            revision_id=revision_id,
            vault_id=vault_id,
            key_versions=key_versions,
        )
        source_inventory = _scoped_inventory(vault_root, identity)
        _verify_vault_inventory(vault_root, identity, key_versions)
        output.mkdir(mode=0o700, parents=True)
        sqlite_destination = output / "worktrace.sqlite3"
        backup_database(database_path, sqlite_destination)
        _copy_private(config_path, output / "config.toml")
        _copy_private(hmac_key_path, output / "email-hmac.key")
        copied_vault = output / "vault"
        copied_vault.mkdir(mode=0o700)
        for relative, digest in source_inventory.items():
            target = copied_vault / relative
            _copy_private(vault_root / relative, target)
            if _hash(target) != digest:
                raise VaultIntegrityError("vault object changed during backup")
        if _scoped_inventory(vault_root, identity) != source_inventory:
            raise VaultIntegrityError("vault manifest changed during the epoch backup")
        _verify_vault_inventory(copied_vault, identity, key_versions)
        if (
            _epoch_identity(
                guard,
                collection_id=collection_id,
                revision_id=revision_id,
                vault_id=vault_id,
                key_versions=key_versions,
            )
            != identity
        ):
            raise RecoveryError("SQLite archive identity changed during the epoch backup")
        vault_manifest_hash = hashlib.sha256(_canonical(source_inventory)).hexdigest()
        descriptor = {
            "format": "worktrace-jira-recovery",
            "version": 1,
            "installation_id": installation_id,
            "site_ids": identity["site_ids"],
            "collection_ids": identity["collection_ids"],
            "vault_id": identity["vault_id"],
            "key_versions": identity["key_versions"],
            "epoch_id": epoch_id,
            "schema_version": 1,
            "kdf": {"name": "argon2id", "opslimit": 3, "memlimit": 64 * 1024 * 1024, "dk_len": 32},
            "aead": {"name": "xchacha20-poly1305-ietf"},
        }
        _write_private(
            output / "recovery.wtrk", create_recovery_envelope(descriptor, key_versions, passphrase)
        )
        manifest: dict[str, object] = {
            "schema_version": 1,
            "epoch_id": epoch_id,
            "identity": identity,
            "identity_sha256": hashlib.sha256(_canonical(identity)).hexdigest(),
            "collection_id": collection_id,
            "revision_id": revision_id,
            "sqlite_sha256": _hash(sqlite_destination),
            "config_binding_sha256": _hash(output / "config.toml"),
            "hmac_binding_sha256": _hash(output / "email-hmac.key"),
            "vault_manifest_sha256": vault_manifest_hash,
            "vault_inventory": dict(source_inventory),
            "ciphertext_count": len(source_inventory),
            "key_versions": sorted(key_versions),
            "recovery_envelope_sha256": _hash(output / "recovery.wtrk"),
            "complete": True,
        }
        _write_private(output / "epoch.json", _canonical(manifest))
        return EpochBackup(epoch_id, output, manifest)
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    finally:
        guard.execute("ROLLBACK")
        guard.close()


def restore_epoch(*, epoch: Path, destination: Path, passphrase: str) -> dict[str, object]:
    """Verify every epoch binding/object, then restore only into a fresh destination."""
    epoch, destination = _absolute(epoch), _absolute(destination)
    _assert_no_symlink_chain(epoch)
    _assert_no_symlink_chain(destination)
    if destination.exists():
        raise RecoveryError("restore destination must not already exist")
    try:
        manifest = json.loads((epoch / "epoch.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError("epoch manifest is unreadable") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not manifest.get("complete")
        or not isinstance(manifest.get("identity"), dict)
    ):
        raise RecoveryError("epoch manifest is invalid")
    identity = manifest["identity"]
    if hashlib.sha256(_canonical(identity)).hexdigest() != manifest.get("identity_sha256"):
        raise RecoveryError("epoch identity hash does not match the manifest")
    recovery_path = epoch / "recovery.wtrk"
    recovery = open_recovery_envelope(recovery_path.read_bytes(), passphrase)
    if recovery.descriptor["epoch_id"] != manifest.get("epoch_id"):
        raise RecoveryError("recovery epoch identity does not match the manifest")
    if (
        manifest.get("collection_id") != identity["collection_ids"][0]
        or manifest.get("revision_id") != identity["revision_ids"][0]
    ):
        raise RecoveryError("epoch top-level identity does not match its manifest identity")
    recovery_identity = {
        key: recovery.descriptor[key]
        for key in ("site_ids", "collection_ids", "vault_id", "key_versions")
    }
    expected_recovery_identity = {
        key: identity[key] for key in ("site_ids", "collection_ids", "vault_id", "key_versions")
    }
    if recovery_identity != expected_recovery_identity:
        raise RecoveryError("recovery descriptor identity does not match the epoch")
    if _hash(recovery_path) != manifest.get("recovery_envelope_sha256"):
        raise RecoveryError("recovery envelope hash does not match the epoch")
    if sorted(recovery.keys) != manifest.get("key_versions"):
        raise RecoveryError("recovery key versions do not match the epoch")
    for filename, expected in (
        ("worktrace.sqlite3", "sqlite_sha256"),
        ("config.toml", "config_binding_sha256"),
        ("email-hmac.key", "hmac_binding_sha256"),
    ):
        path = epoch / filename
        _assert_no_symlink_chain(path)
        if _hash(path) != manifest.get(expected):
            raise RecoveryError(f"epoch binding mismatch: {filename}")
    _verify_sqlite_identity(epoch / "worktrace.sqlite3", identity=identity)
    vault = epoch / "vault"
    inventory = _inventory(vault)
    if inventory != manifest.get("vault_inventory"):
        raise RecoveryError("vault inventory does not match the epoch")
    if hashlib.sha256(_canonical(inventory)).hexdigest() != manifest.get("vault_manifest_sha256"):
        raise RecoveryError("vault manifest hash does not match the epoch")
    _verify_vault_inventory(vault, identity, recovery.keys)
    staging = destination.parent / f".{destination.name}.restore-{uuid.uuid4().hex}"
    _assert_no_symlink_chain(staging)
    staging.mkdir(mode=0o700, parents=False)
    try:
        for filename in (
            "worktrace.sqlite3",
            "config.toml",
            "email-hmac.key",
            "recovery.wtrk",
            "epoch.json",
        ):
            _copy_private(epoch / filename, staging / filename)
        restored_vault = staging / "vault"
        restored_vault.mkdir(mode=0o700)
        for relative in sorted(inventory):
            _copy_private(vault / relative, restored_vault / relative)
        _verify_sqlite_identity(staging / "worktrace.sqlite3", identity=identity)
        _verify_vault_inventory(restored_vault, identity, recovery.keys)
        os.rename(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "schema_version": 1,
        "epoch_id": manifest["epoch_id"],
        "destination": destination.name,
        "verified": True,
        "status": "restored",
    }
