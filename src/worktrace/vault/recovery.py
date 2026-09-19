from __future__ import annotations

import base64
import json
import secrets
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from worktrace.errors import RecoveryError
from worktrace.vault.format import canonical_json

MAGIC = b"WTRK"
VERSION = 1
ENDIAN_MARKER = 0x4245
MAX_DESCRIPTOR_BYTES = 8_192
MAX_CIPHERTEXT_BYTES = 4_096
MIN_PASSPHRASE_SCALARS = 12
MAX_KEY_VERSIONS = 64
_HEADER = struct.Struct(">4sBBHI")
_U32 = struct.Struct(">I")


def _bindings() -> Any:
    try:
        from nacl import bindings  # type: ignore[import-not-found, unused-ignore]
    except ImportError as exc:  # pragma: no cover - optional dependency is absent
        raise RecoveryError("install the jira-vault extra for recovery envelopes") from exc
    return bindings


def _pwhash() -> Any:
    try:
        from nacl import pwhash  # type: ignore[import-not-found, unused-ignore]
    except ImportError as exc:  # pragma: no cover - optional dependency is absent
        raise RecoveryError("install the jira-vault extra for recovery envelopes") from exc
    return pwhash


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise RecoveryError("recovery descriptor contains duplicate fields")
        value[key] = item
    return value


def _parse_descriptor(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecoveryError) as exc:
        raise RecoveryError("recovery descriptor is invalid JSON") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise RecoveryError("recovery descriptor is not canonical")
    return value


def _passphrase(passphrase: str) -> bytes:
    if not isinstance(passphrase, str) or any(0xD800 <= ord(char) <= 0xDFFF for char in passphrase):
        raise RecoveryError("recovery passphrase is invalid")
    if len(passphrase) < MIN_PASSPHRASE_SCALARS:
        raise RecoveryError("recovery passphrase is too short")
    return passphrase.encode("utf-8")


def _descriptor(value: Mapping[str, object]) -> tuple[bytes, list[int], int, int]:
    expected = {
        "aead",
        "collection_ids",
        "epoch_id",
        "format",
        "installation_id",
        "kdf",
        "key_versions",
        "schema_version",
        "site_ids",
        "vault_id",
        "version",
    }
    if set(value) != expected:
        raise RecoveryError("recovery descriptor fields are not exact")
    if value.get("format") != "worktrace-jira-recovery" or value.get("version") != 1:
        raise RecoveryError("unsupported recovery envelope format")
    if value.get("schema_version") != 1 or value.get("aead") != {"name": "xchacha20-poly1305-ietf"}:
        raise RecoveryError("unsupported recovery envelope schema or AEAD")
    kdf = value.get("kdf")
    if not isinstance(kdf, dict) or set(kdf) != {"dk_len", "memlimit", "name", "opslimit"}:
        raise RecoveryError("recovery KDF descriptor is not exact")
    if kdf.get("name") != "argon2id" or kdf.get("dk_len") != 32:
        raise RecoveryError("unsupported recovery KDF")
    opslimit, memlimit = kdf.get("opslimit"), kdf.get("memlimit")
    if (
        isinstance(opslimit, bool)
        or not isinstance(opslimit, int)
        or not 1 <= opslimit <= 10
        or isinstance(memlimit, bool)
        or not isinstance(memlimit, int)
        or not 8 * 1024 * 1024 <= memlimit <= 1024 * 1024 * 1024
    ):
        raise RecoveryError("recovery KDF cost is outside the approved bounds")
    versions = value.get("key_versions")
    if (
        not isinstance(versions, list)
        or not 1 <= len(versions) <= MAX_KEY_VERSIONS
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in versions)
        or len(set(versions)) != len(versions)
    ):
        raise RecoveryError("recovery key versions are invalid")
    for field in ("installation_id", "vault_id", "epoch_id"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise RecoveryError(f"recovery descriptor field {field} is invalid")
    for field in ("site_ids", "collection_ids"):
        items = value.get(field)
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item for item in items
        ):
            raise RecoveryError(f"recovery descriptor field {field} is invalid")
    return canonical_json(value), versions, opslimit, memlimit


@dataclass(frozen=True, slots=True)
class RecoveryEnvelope:
    descriptor: dict[str, object]
    keys: dict[int, bytes]


def create_recovery_envelope(
    descriptor: Mapping[str, object],
    keys: Mapping[int, bytes],
    passphrase: str,
    *,
    salt: bytes | None = None,
    nonce: bytes | None = None,
) -> bytes:
    descriptor_bytes, versions, opslimit, memlimit = _descriptor(descriptor)
    _passphrase(passphrase)
    if set(keys) != set(versions):
        raise RecoveryError("recovery key versions do not match the descriptor")
    if any(not isinstance(key, bytes) or len(key) != 32 for key in keys.values()):
        raise RecoveryError("recovery keys must be exactly 32 bytes")
    salt = secrets.token_bytes(16) if salt is None else salt
    nonce = secrets.token_bytes(24) if nonce is None else nonce
    if len(salt) != 16 or len(nonce) != 24:
        raise RecoveryError("recovery salt or nonce has the wrong length")
    pwhash = _pwhash()
    bindings = _bindings()
    derived = pwhash.argon2id.kdf(32, _passphrase(passphrase), salt, opslimit, memlimit)
    plaintext = b"WRAP" + _U32.pack(len(versions)) + b"".join(keys[version] for version in versions)
    ciphertext = bytes(
        bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
            plaintext, descriptor_bytes, nonce, derived
        )
    )
    if len(descriptor_bytes) > MAX_DESCRIPTOR_BYTES or len(ciphertext) > MAX_CIPHERTEXT_BYTES:
        raise RecoveryError("recovery envelope exceeds its size limit")
    return (
        _HEADER.pack(MAGIC, VERSION, 0, ENDIAN_MARKER, len(descriptor_bytes))
        + descriptor_bytes
        + salt
        + nonce
        + _U32.pack(len(ciphertext))
        + ciphertext
    )


def open_recovery_envelope(data: bytes, passphrase: str) -> RecoveryEnvelope:
    if len(data) < _HEADER.size:
        raise RecoveryError("recovery envelope is truncated")
    magic, version, flags, endian, descriptor_length = _HEADER.unpack_from(data)
    if magic != MAGIC or version != VERSION or flags != 0 or endian != ENDIAN_MARKER:
        raise RecoveryError("unsupported recovery envelope header")
    if descriptor_length > MAX_DESCRIPTOR_BYTES:
        raise RecoveryError("recovery descriptor exceeds its size limit")
    offset = _HEADER.size
    end_descriptor = offset + descriptor_length
    descriptor_bytes = data[offset:end_descriptor]
    if len(descriptor_bytes) != descriptor_length:
        raise RecoveryError("recovery envelope descriptor is truncated")
    descriptor = _parse_descriptor(descriptor_bytes)
    canonical_descriptor, versions, opslimit, memlimit = _descriptor(descriptor)
    if canonical_descriptor != descriptor_bytes:
        raise RecoveryError("recovery descriptor encoding changed")
    offset = end_descriptor
    salt = data[offset : offset + 16]
    nonce = data[offset + 16 : offset + 40]
    if len(salt) != 16 or len(nonce) != 24:
        raise RecoveryError("recovery envelope salt or nonce is truncated")
    offset += 40
    if len(data) < offset + 4:
        raise RecoveryError("recovery envelope ciphertext length is truncated")
    ciphertext_length = _U32.unpack_from(data, offset)[0]
    offset += 4
    if ciphertext_length > MAX_CIPHERTEXT_BYTES or len(data) != offset + ciphertext_length:
        raise RecoveryError("recovery envelope ciphertext length is invalid")
    ciphertext = data[offset:]
    pwhash = _pwhash()
    bindings = _bindings()
    derived = pwhash.argon2id.kdf(32, _passphrase(passphrase), salt, opslimit, memlimit)
    try:
        plaintext = bytes(
            bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
                ciphertext, descriptor_bytes, nonce, derived
            )
        )
    except Exception as exc:
        raise RecoveryError("recovery envelope authentication failed") from exc
    if len(plaintext) < 8 or plaintext[:4] != b"WRAP":
        raise RecoveryError("recovery key wrapping record is invalid")
    count = _U32.unpack_from(plaintext, 4)[0]
    if count != len(versions) or len(plaintext) != 8 + count * 32:
        raise RecoveryError("recovery key wrapping record has invalid length")
    keys = {
        version: plaintext[8 + index * 32 : 8 + (index + 1) * 32]
        for index, version in enumerate(versions)
    }
    return RecoveryEnvelope(descriptor, keys)


def diagnostic_base64(value: bytes) -> str:
    """Return the only permitted diagnostic representation for binary envelope fields."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
