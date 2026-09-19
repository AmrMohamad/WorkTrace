from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import tempfile
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast

from worktrace.errors import VaultFormatError, VaultIntegrityError

MAGIC = b"WTVA"
FORMAT_VERSION = 1
HEADER_BYTES = 24
SECRETSTREAM_OVERHEAD = 17
DEFAULT_CHUNK_SIZE = 1024 * 1024
MAX_DESCRIPTOR_BYTES = 65_536
MAX_RECORD_BYTES = DEFAULT_CHUNK_SIZE + SECRETSTREAM_OVERHEAD
_RECORD_HEADER = struct.Struct(">I")


def _bindings() -> Any:
    try:
        from nacl import bindings
    except ImportError as exc:  # pragma: no cover - exercised without the optional extra
        raise VaultFormatError("install the jira-vault extra for encrypted objects") from exc
    return bindings


def canonical_json(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VaultFormatError("vault descriptor is not canonical JSON") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VaultFormatError("vault descriptor contains duplicate keys")
        result[key] = value
    return result


def _parse_canonical_json(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, VaultFormatError) as exc:
        raise VaultFormatError("vault descriptor is invalid JSON") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise VaultFormatError("vault descriptor is not canonically encoded")
    return value


@dataclass(frozen=True, slots=True)
class VaultDescriptor:
    site_id: str
    collection_id: str
    revision_id: str
    object_id: str
    kind: str
    content_length: int
    content_sha256: str
    key_version: int
    chunk_size: int = DEFAULT_CHUNK_SIZE
    format: str = "worktrace-jira-vault-object"
    format_version: int = FORMAT_VERSION

    def to_mapping(self) -> dict[str, object]:
        return {
            "collection_id": self.collection_id,
            "content_length": self.content_length,
            "content_sha256": self.content_sha256,
            "format": self.format,
            "format_version": self.format_version,
            "key_version": self.key_version,
            "kind": self.kind,
            "object_id": self.object_id,
            "revision_id": self.revision_id,
            "site_id": self.site_id,
            "chunk_size": self.chunk_size,
        }

    def encoded(self) -> bytes:
        _validate_descriptor(self)
        encoded = canonical_json(self.to_mapping())
        if len(encoded) > MAX_DESCRIPTOR_BYTES:
            raise VaultFormatError("vault descriptor exceeds the size limit")
        return encoded

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> VaultDescriptor:
        expected = {
            "collection_id",
            "content_length",
            "content_sha256",
            "format",
            "format_version",
            "key_version",
            "kind",
            "object_id",
            "revision_id",
            "site_id",
            "chunk_size",
        }
        if set(value) != expected:
            raise VaultFormatError("vault descriptor fields are not exact")
        try:
            descriptor = cls(
                site_id=_string(value["site_id"], "site_id"),
                collection_id=_string(value["collection_id"], "collection_id"),
                revision_id=_string(value["revision_id"], "revision_id"),
                object_id=_string(value["object_id"], "object_id"),
                kind=_string(value["kind"], "kind"),
                content_length=_integer(value["content_length"], "content_length"),
                content_sha256=_string(value["content_sha256"], "content_sha256"),
                key_version=_integer(value["key_version"], "key_version"),
                chunk_size=_integer(value["chunk_size"], "chunk_size"),
                format=_string(value["format"], "format"),
                format_version=_integer(value["format_version"], "format_version"),
            )
        except (TypeError, ValueError) as exc:
            raise VaultFormatError("vault descriptor has invalid field types") from exc
        _validate_descriptor(descriptor)
        return descriptor


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise VaultFormatError(f"vault descriptor field {name} must be a non-empty string")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VaultFormatError(f"vault descriptor field {name} must be an integer")
    return value


def _validate_descriptor(descriptor: VaultDescriptor) -> None:
    if descriptor.format != "worktrace-jira-vault-object" or descriptor.format_version != 1:
        raise VaultFormatError("unsupported vault object format")
    if descriptor.content_length < 0 or descriptor.key_version < 1:
        raise VaultFormatError("vault descriptor contains an invalid size or key version")
    if descriptor.chunk_size != DEFAULT_CHUNK_SIZE:
        raise VaultFormatError("unsupported vault chunk size")
    if (
        len(descriptor.content_sha256) != 64
        or descriptor.content_sha256 != descriptor.content_sha256.lower()
    ):
        raise VaultFormatError("vault descriptor content hash is invalid")
    try:
        int(descriptor.content_sha256, 16)
    except ValueError as exc:
        raise VaultFormatError("vault descriptor content hash is invalid") from exc


@dataclass(frozen=True, slots=True)
class VaultObjectResult:
    descriptor: VaultDescriptor
    ciphertext_sha256: str
    plaintext_length: int


@dataclass(frozen=True, slots=True)
class VaultCheckpoint:
    plaintext_length: int
    record_count: int
    ciphertext_sha256: str


CheckpointCallback = Callable[[VaultCheckpoint], None]


def _key(key: bytes) -> bytes:
    if len(key) != 32:
        raise VaultFormatError("vault keys must be exactly 32 bytes")
    return key


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _assert_no_symlink_chain(path: Path) -> None:
    path = _absolute(path)
    for item in [*list(reversed(path.parents)), path]:
        try:
            if item.is_symlink():
                raise VaultFormatError(f"vault path contains a symlink: {item}")
        except OSError as exc:
            raise VaultFormatError("vault path cannot be inspected safely") from exc


def _write_record(
    output: BinaryIO,
    ciphertext: bytes,
    file_hash: hashlib._Hash,
) -> None:
    if not SECRETSTREAM_OVERHEAD <= len(ciphertext) <= MAX_RECORD_BYTES:
        raise VaultFormatError("encrypted record length is outside the allowed range")
    prefix = _RECORD_HEADER.pack(len(ciphertext))
    output.write(prefix)
    output.write(ciphertext)
    file_hash.update(prefix)
    file_hash.update(ciphertext)


def write_vault_object(
    source: BinaryIO,
    destination: Path,
    descriptor: VaultDescriptor,
    key: bytes,
    *,
    vault_root: Path,
    checkpoint: CheckpointCallback | None = None,
) -> VaultObjectResult:
    """Encrypt one stream and publish it atomically without replacing a destination."""
    descriptor_bytes = descriptor.encoded()
    bindings = _bindings()
    state = bindings.crypto_secretstream_xchacha20poly1305_state()
    header = bindings.crypto_secretstream_xchacha20poly1305_init_push(state, _key(key))
    if len(header) != HEADER_BYTES:
        raise VaultFormatError("secretstream returned an invalid header")
    vault_root = _absolute(vault_root)
    destination = _absolute(destination)
    _assert_no_symlink_chain(vault_root)
    if vault_root.is_symlink() or not vault_root.is_dir():
        raise VaultFormatError("vault root must be a real directory")
    _assert_no_symlink_chain(destination)
    try:
        destination.relative_to(vault_root)
    except ValueError as exc:
        raise VaultFormatError("vault object destination escapes the vault root") from exc
    if destination == vault_root:
        raise VaultFormatError("vault object destination must be below the vault root")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_no_symlink_chain(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    temporary: Path | None = None
    file_hash = hashlib.sha256()
    plain_hash = hashlib.sha256()
    plain_length = 0
    record_count = 0
    try:
        descriptor_prefix = (
            MAGIC + bytes([FORMAT_VERSION]) + struct.pack(">I", len(descriptor_bytes))
        )
        fd, temporary_name = tempfile.mkstemp(prefix=".wtva-", dir=destination.parent)
        temporary = Path(temporary_name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(descriptor_prefix)
            output.write(descriptor_bytes)
            output.write(header)
            file_hash.update(descriptor_prefix)
            file_hash.update(descriptor_bytes)
            file_hash.update(header)

            current = source.read(descriptor.chunk_size)
            if not isinstance(current, bytes):
                raise VaultFormatError("vault source did not return bytes")
            while True:
                following = source.read(descriptor.chunk_size) if current else b""
                if not isinstance(following, bytes):
                    raise VaultFormatError("vault source did not return bytes")
                final = not following
                tag = (
                    bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL
                    if final
                    else bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE
                )
                encrypted = bindings.crypto_secretstream_xchacha20poly1305_push(
                    state, current, descriptor_bytes, tag
                )
                _write_record(output, encrypted, file_hash)
                plain_hash.update(current)
                plain_length += len(current)
                record_count += 1
                if checkpoint is not None:
                    checkpoint(VaultCheckpoint(plain_length, record_count, file_hash.hexdigest()))
                if final:
                    break
                current = following
            output.flush()
            os.fsync(output.fileno())
        if (
            plain_length != descriptor.content_length
            or plain_hash.hexdigest() != descriptor.content_sha256
        ):
            raise VaultIntegrityError(
                "vault plaintext length or hash does not match its descriptor"
            )
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            raise
        finally:
            temporary.unlink(missing_ok=True)
            temporary = None
        return VaultObjectResult(descriptor, file_hash.hexdigest(), plain_length)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _read_exact(source: BinaryIO, length: int, what: str) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        data = source.read(remaining)
        if not isinstance(data, bytes) or not data:
            raise VaultIntegrityError(f"vault object is truncated while reading {what}")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def decrypt_vault_object(
    source: BinaryIO,
    sink: BinaryIO,
    key: bytes,
    *,
    expected_ciphertext_sha256: str | None = None,
) -> VaultObjectResult:
    return _process_vault_object(
        source,
        key,
        sink,
        expected_ciphertext_sha256=expected_ciphertext_sha256,
        stage_plaintext=True,
    )


def verify_vault_object(
    source: BinaryIO,
    key: bytes,
    *,
    expected_ciphertext_sha256: str | None = None,
) -> VaultObjectResult:
    """Authenticate and hash an object without materializing plaintext."""
    return _process_vault_object(
        source,
        key,
        None,
        expected_ciphertext_sha256=expected_ciphertext_sha256,
        stage_plaintext=False,
    )


def _process_vault_object(
    source: BinaryIO,
    key: bytes,
    sink: BinaryIO | None,
    *,
    expected_ciphertext_sha256: str | None,
    stage_plaintext: bool,
) -> VaultObjectResult:
    bindings = _bindings()
    file_hash = hashlib.sha256()
    prefix = _read_exact(source, 9, "header")
    file_hash.update(prefix)
    if prefix[:4] != MAGIC or prefix[4] != FORMAT_VERSION:
        raise VaultFormatError("unsupported vault object header")
    descriptor_length = struct.unpack(">I", prefix[5:])[0]
    if descriptor_length > MAX_DESCRIPTOR_BYTES:
        raise VaultFormatError("vault descriptor exceeds the size limit")
    descriptor_bytes = _read_exact(source, descriptor_length, "descriptor")
    file_hash.update(descriptor_bytes)
    descriptor = VaultDescriptor.from_mapping(_parse_canonical_json(descriptor_bytes))
    header = _read_exact(source, HEADER_BYTES, "secretstream header")
    file_hash.update(header)
    state = bindings.crypto_secretstream_xchacha20poly1305_state()
    try:
        bindings.crypto_secretstream_xchacha20poly1305_init_pull(state, header, _key(key))
    except Exception as exc:
        raise VaultIntegrityError("vault key or secretstream header is invalid") from exc

    plain_hash = hashlib.sha256()
    plain_length = 0
    saw_final = False
    with (
        tempfile.TemporaryFile(mode="w+b") if stage_plaintext else nullcontext()
    ) as verified_plaintext:
        while True:
            length_bytes = source.read(_RECORD_HEADER.size)
            if length_bytes == b"":
                if not saw_final:
                    raise VaultIntegrityError("vault object is missing its final record")
                break
            if saw_final:
                raise VaultIntegrityError("vault object contains a record after its final tag")
            if not isinstance(length_bytes, bytes) or len(length_bytes) != _RECORD_HEADER.size:
                raise VaultIntegrityError("vault object has a truncated record length")
            file_hash.update(length_bytes)
            record_length = _RECORD_HEADER.unpack(length_bytes)[0]
            if not SECRETSTREAM_OVERHEAD <= record_length <= MAX_RECORD_BYTES:
                raise VaultFormatError("encrypted record length is outside the allowed range")
            ciphertext = _read_exact(source, record_length, "encrypted record")
            file_hash.update(ciphertext)
            try:
                plaintext, tag = bindings.crypto_secretstream_xchacha20poly1305_pull(
                    state, ciphertext, descriptor_bytes
                )
            except Exception as exc:
                raise VaultIntegrityError("vault record authentication failed") from exc
            if tag == bindings.crypto_secretstream_xchacha20poly1305_TAG_FINAL:
                saw_final = True
            elif tag != bindings.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE:
                raise VaultIntegrityError("vault record has an invalid tag")
            if verified_plaintext is not None:
                verified_plaintext.write(plaintext)
            plain_hash.update(plaintext)
            plain_length += len(plaintext)
        if source.read(1) != b"":
            raise VaultIntegrityError("vault object has trailing bytes")
        if (
            plain_length != descriptor.content_length
            or plain_hash.hexdigest() != descriptor.content_sha256
        ):
            raise VaultIntegrityError(
                "vault plaintext length or hash does not match its descriptor"
            )
        ciphertext_hash = file_hash.hexdigest()
        if expected_ciphertext_sha256 is not None and ciphertext_hash != expected_ciphertext_sha256:
            raise VaultIntegrityError("vault ciphertext hash does not match its manifest")
        if verified_plaintext is not None:
            if sink is None:
                raise VaultFormatError("verified plaintext sink is missing")
            verified_plaintext.seek(0)
            shutil.copyfileobj(verified_plaintext, sink)
        return VaultObjectResult(descriptor, ciphertext_hash, plain_length)


def read_vault_descriptor(source: BinaryIO) -> VaultDescriptor:
    """Read only the authenticated object's canonical descriptor and key version."""
    prefix = _read_exact(source, 9, "header")
    if prefix[:4] != MAGIC or prefix[4] != FORMAT_VERSION:
        raise VaultFormatError("unsupported vault object header")
    descriptor_length = struct.unpack(">I", prefix[5:])[0]
    if descriptor_length > MAX_DESCRIPTOR_BYTES:
        raise VaultFormatError("vault descriptor exceeds the size limit")
    descriptor_bytes = _read_exact(source, descriptor_length, "descriptor")
    return VaultDescriptor.from_mapping(_parse_canonical_json(descriptor_bytes))


def read_vault_object(
    path: Path, key: bytes, *, expected_ciphertext_sha256: str | None = None
) -> bytes:
    from io import BytesIO

    output = BytesIO()
    with path.open("rb") as source:
        decrypt_vault_object(
            source,
            output,
            key,
            expected_ciphertext_sha256=expected_ciphertext_sha256,
        )
    return output.getvalue()


def _parse_canonical_descriptor(raw: bytes) -> Mapping[str, object]:
    return cast(Mapping[str, object], _parse_canonical_json(raw))
