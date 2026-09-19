from __future__ import annotations

import base64
import platform
import secrets
from dataclasses import dataclass
from typing import Protocol

from worktrace.errors import KeychainError

KEYCHAIN_SERVICE = "WorkTrace Jira Vault"
VAULT_KEY_BYTES = 32


class _KeyringBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


def _encoded(key: bytes) -> str:
    if len(key) != VAULT_KEY_BYTES:
        raise KeychainError("vault keys must be exactly 32 bytes")
    return base64.b64encode(key).decode("ascii")


def _decoded(value: str) -> bytes:
    try:
        key = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise KeychainError("Keychain vault key is malformed") from exc
    if len(key) != VAULT_KEY_BYTES:
        raise KeychainError("Keychain vault key has the wrong length")
    return key


@dataclass(frozen=True, slots=True)
class MacOSKeychain:
    """Dedicated Keychain owner; no file, environment, or SQLite fallback exists."""

    backend: _KeyringBackend
    installation_id: str

    @classmethod
    def open(
        cls,
        installation_id: str,
        *,
        backend: _KeyringBackend | None = None,
        platform_name: str | None = None,
    ) -> MacOSKeychain:
        if (platform_name or platform.system()) != "Darwin":
            raise KeychainError("WorkTrace Jira Vault requires the macOS Keychain backend")
        if not installation_id:
            raise KeychainError("installation_id is required for the vault Keychain")
        if backend is None:
            try:
                import keyring
            except ImportError as exc:  # pragma: no cover - optional dependency is absent
                raise KeychainError("install the jira-vault extra for Keychain access") from exc
            backend = keyring.get_keyring()
            module = type(backend).__module__
            name = type(backend).__name__
            if module != "keyring.backends.macOS" or name != "Keyring":
                raise KeychainError("configured keyring backend is not macOS Keychain")
        for method in ("get_password", "set_password", "delete_password"):
            if not callable(getattr(backend, method, None)):
                raise KeychainError("configured Keychain backend is incomplete")
        return cls(backend, installation_id)

    def account(self, version: int) -> str:
        if version < 1:
            raise KeychainError("vault key versions start at one")
        return f"{self.installation_id}:{version}"

    def get(self, version: int) -> bytes | None:
        try:
            value = self.backend.get_password(KEYCHAIN_SERVICE, self.account(version))
        except Exception as exc:
            raise KeychainError("Keychain vault key lookup failed") from exc
        return None if value is None else _decoded(value)

    def put(self, version: int, key: bytes) -> None:
        try:
            self.backend.set_password(KEYCHAIN_SERVICE, self.account(version), _encoded(key))
        except Exception as exc:
            raise KeychainError("Keychain vault key write failed") from exc

    def ensure(self, version: int = 1) -> bytes:
        existing = self.get(version)
        if existing is not None:
            return existing
        key = secrets.token_bytes(VAULT_KEY_BYTES)
        self.put(version, key)
        return key

    def rotate(self, new_version: int) -> bytes:
        if self.get(new_version) is not None:
            raise KeychainError("requested vault key version already exists")
        key = secrets.token_bytes(VAULT_KEY_BYTES)
        self.put(new_version, key)
        return key

    def retire(self, version: int) -> None:
        try:
            self.backend.delete_password(KEYCHAIN_SERVICE, self.account(version))
        except Exception as exc:
            raise KeychainError("Keychain vault key retirement failed") from exc
