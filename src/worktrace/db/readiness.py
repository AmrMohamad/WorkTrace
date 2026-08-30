from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from worktrace.db.migrations import migrations, user_version


class DatabaseReadinessStatus(StrEnum):
    READY = "ready"
    UPGRADE_REQUIRED = "upgrade_required"
    UNSUPPORTED_NEWER = "unsupported_newer"


@dataclass(frozen=True, slots=True)
class DatabaseReadiness:
    status: DatabaseReadinessStatus
    current_version: int
    supported_version: int


def database_readiness(connection: sqlite3.Connection) -> DatabaseReadiness:
    """Compare the ledger schema with the latest packaged migration without writing."""

    packaged = migrations()
    supported_version = packaged[-1].version if packaged else 0
    current_version = user_version(connection)
    if current_version < supported_version:
        status = DatabaseReadinessStatus.UPGRADE_REQUIRED
    elif current_version > supported_version:
        status = DatabaseReadinessStatus.UNSUPPORTED_NEWER
    else:
        status = DatabaseReadinessStatus.READY
    return DatabaseReadiness(
        status=status,
        current_version=current_version,
        supported_version=supported_version,
    )
