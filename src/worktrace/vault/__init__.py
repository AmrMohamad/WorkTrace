"""Encrypted Jira vault foundation primitives."""

from worktrace.vault.format import (
    VaultDescriptor,
    VaultObjectResult,
    decrypt_vault_object,
    read_vault_object,
    write_vault_object,
)

__all__ = [
    "VaultDescriptor",
    "VaultObjectResult",
    "decrypt_vault_object",
    "read_vault_object",
    "write_vault_object",
]
