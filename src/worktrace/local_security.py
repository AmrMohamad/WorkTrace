from __future__ import annotations

import os
from pathlib import Path

from worktrace.paths import ensure_private_directory


def email_hmac_key(data_directory: Path, *, create: bool) -> bytes:
    configured = os.environ.get("WORKTRACE_EMAIL_HMAC_KEY")
    if configured:
        return configured.encode("utf-8")
    key_path = data_directory / "email-hmac.key"
    if not key_path.exists():
        if not create:
            raise FileNotFoundError("local email hash key is missing; run worktrace init")
        ensure_private_directory(data_directory)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(key_path, flags, 0o600)
        try:
            os.write(descriptor, os.urandom(32).hex().encode("ascii"))
        finally:
            os.close(descriptor)
    key_path.chmod(0o600)
    return key_path.read_bytes()
