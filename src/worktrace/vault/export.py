"""Explicit, private, fail-closed Jira attachment export."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from worktrace.errors import RecoveryError, VaultIntegrityError
from worktrace.vault.format import decrypt_vault_object, read_vault_descriptor


def _check_chain(path: Path) -> None:
    absolute = Path(os.path.abspath(os.path.expanduser(str(path))))
    for item in [*reversed(absolute.parents), absolute]:
        if item.is_symlink():
            raise RecoveryError("export path contains a symlink")


def export_attachment(
    *,
    source: Path,
    destination: Path,
    vault_root: Path,
    key: bytes,
    expected_ciphertext_sha256: str | None = None,
    collection_id: str | None = None,
    revision_id: str | None = None,
    object_id: str | None = None,
) -> Path:
    """Verify and publish one decrypted original to a private no-overwrite path."""
    if str(destination) in {"-", "stdout", "/dev/stdout"}:
        raise RecoveryError("attachment export refuses stdout")
    source = source.expanduser().absolute()
    destination = destination.expanduser().absolute()
    vault_root = vault_root.expanduser().absolute()
    _check_chain(source)
    _check_chain(destination)
    if not source.is_file() or not vault_root.is_dir():
        raise RecoveryError("attachment export source or vault root is unavailable")
    try:
        destination.relative_to(vault_root)
    except ValueError:
        pass
    else:
        raise RecoveryError("attachment export destination may not be inside the vault")
    if destination.exists():
        raise RecoveryError("attachment export refuses overwrite")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_chain(destination.parent)
    with source.open("rb") as stream:
        descriptor = read_vault_descriptor(stream)
    if collection_id is not None and descriptor.collection_id != collection_id:
        raise VaultIntegrityError("attachment belongs to another collection")
    if revision_id is not None and descriptor.revision_id != revision_id:
        raise VaultIntegrityError("attachment belongs to another revision")
    if object_id is not None and descriptor.object_id != object_id:
        raise VaultIntegrityError("attachment object identity mismatch")
    fd, temporary_name = tempfile.mkstemp(prefix=".worktrace-export-", dir=destination.parent)
    temporary = Path(temporary_name)
    os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w+b") as sink, source.open("rb") as stream:
            decrypt_vault_object(
                stream,
                sink,
                key,
                expected_ciphertext_sha256=expected_ciphertext_sha256,
            )
            sink.flush()
            os.fsync(sink.fileno())
        os.link(temporary, destination, follow_symlinks=False)
        temporary.unlink(missing_ok=True)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination
