from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path


def default_config_path() -> Path:
    configured = os.environ.get("WORKTRACE_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path("~/.config/worktrace/config.toml").expanduser()


def default_data_directory() -> Path:
    return Path("~/Library/Application Support/WorkTrace").expanduser()


def ensure_private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with suppress(OSError):
        path.chmod(0o700)
    # Networked or policy-managed filesystems may reject chmod; doctor reports it.
    return path
