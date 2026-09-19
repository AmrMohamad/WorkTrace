from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from worktrace.db.migrations import backup_database, user_version
from worktrace.errors import RecoveryError, VaultIntegrityError
from worktrace.vault.recovery import create_recovery_envelope, open_recovery_envelope


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_private(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file():
        raise VaultIntegrityError(f"backup source is not a regular file: {source.name}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private(destination, source.read_bytes())


def _inventory(root: Path) -> dict[str, str]:
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
    """Create a portable, hash-bound epoch without touching the source installation."""
    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(mode=0o700, parents=True)
    epoch_id = f"epoch:{uuid.uuid4()}"
    try:
        sqlite_destination = output / "worktrace.sqlite3"
        backup_database(database_path, sqlite_destination)
        _copy_private(config_path, output / "config.toml")
        _copy_private(hmac_key_path, output / "email-hmac.key")
        copied_vault = output / "vault"
        copied_vault.mkdir(mode=0o700)
        for relative, digest in _inventory(vault_root).items():
            source = vault_root / relative
            target = copied_vault / relative
            _copy_private(source, target)
            if _hash(target) != digest:
                raise VaultIntegrityError("vault object changed during backup")
        vault_inventory = _inventory(copied_vault)
        vault_manifest_hash = hashlib.sha256(_canonical(vault_inventory)).hexdigest()
        descriptor = {
            "format": "worktrace-jira-recovery",
            "version": 1,
            "installation_id": installation_id,
            "site_ids": [],
            "collection_ids": [collection_id],
            "vault_id": vault_id,
            "epoch_id": epoch_id,
            "key_versions": sorted(key_versions),
            "schema_version": 1,
            "kdf": {"name": "argon2id", "opslimit": 3, "memlimit": 64 * 1024 * 1024, "dk_len": 32},
            "aead": {"name": "xchacha20-poly1305-ietf"},
        }
        recovery = create_recovery_envelope(descriptor, key_versions, passphrase)
        _write_private(output / "recovery.wtrk", recovery)
        manifest: dict[str, object] = {
            "schema_version": 1,
            "epoch_id": epoch_id,
            "collection_id": collection_id,
            "revision_id": revision_id,
            "sqlite_sha256": _hash(sqlite_destination),
            "config_binding_sha256": _hash(output / "config.toml"),
            "hmac_binding_sha256": _hash(output / "email-hmac.key"),
            "vault_manifest_sha256": vault_manifest_hash,
            "vault_inventory": vault_inventory,
            "ciphertext_count": len(vault_inventory),
            "key_versions": sorted(key_versions),
            "recovery_envelope_sha256": _hash(output / "recovery.wtrk"),
            "complete": True,
        }
        _write_private(output / "epoch.json", _canonical(manifest))
        return EpochBackup(epoch_id, output, manifest)
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


def restore_epoch(
    *,
    epoch: Path,
    destination: Path,
    passphrase: str,
) -> dict[str, object]:
    """Verify every epoch binding, then restore only into a fresh destination."""
    epoch = epoch.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if destination.exists() and any(destination.iterdir()):
        raise RecoveryError("restore destination must be fresh and empty")
    manifest_path = epoch / "epoch.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError("epoch manifest is unreadable") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not manifest.get("complete")
    ):
        raise RecoveryError("epoch manifest is invalid")
    recovery_path = epoch / "recovery.wtrk"
    recovery = open_recovery_envelope(recovery_path.read_bytes(), passphrase)
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
        if _hash(path) != manifest.get(expected):
            raise RecoveryError(f"epoch binding mismatch: {filename}")
    vault = epoch / "vault"
    inventory = _inventory(vault)
    if inventory != manifest.get("vault_inventory"):
        raise RecoveryError("vault inventory does not match the epoch")
    if hashlib.sha256(_canonical(inventory)).hexdigest() != manifest.get("vault_manifest_sha256"):
        raise RecoveryError("vault manifest hash does not match the epoch")
    if destination.exists():
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    else:
        destination.mkdir(mode=0o700, parents=True)
    try:
        for filename in (
            "worktrace.sqlite3",
            "config.toml",
            "email-hmac.key",
            "recovery.wtrk",
            "epoch.json",
        ):
            _copy_private(epoch / filename, destination / filename)
        restored_vault = destination / "vault"
        restored_vault.mkdir(mode=0o700)
        for relative in sorted(inventory):
            _copy_private(vault / relative, restored_vault / relative)
        with sqlite3.connect(destination / "worktrace.sqlite3") as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RecoveryError("restored SQLite integrity check failed")
            if user_version(connection) != 7:
                raise RecoveryError("restored SQLite schema is not version 7")
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return {
        "schema_version": 1,
        "epoch_id": manifest["epoch_id"],
        "destination": destination.name,
        "verified": True,
        "status": "restored",
    }
