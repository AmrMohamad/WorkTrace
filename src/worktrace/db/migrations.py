from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from worktrace.errors import DatabaseError

MIGRATION_RE = re.compile(r"^(?P<version>[0-9]{3})_[a-z0-9_]+\.sql$")
BACKUP_TIMEOUT_SECONDS = 5.0
MIGRATION_BUSY_TIMEOUT_MS = 5_000


@dataclass(frozen=True)
class Migration:
    version: int
    path: Path
    sql: str


def migration_directory() -> Path:
    package_directory = Path(__file__).resolve().parents[1] / "migrations"
    repository_directory = Path(__file__).resolve().parents[3] / "migrations"
    for candidate in (package_directory, repository_directory):
        if candidate.is_dir():
            return candidate
    raise DatabaseError("migration directory not found in package or source tree")


def migrations() -> tuple[Migration, ...]:
    result: list[Migration] = []
    for path in sorted(migration_directory().glob("*.sql")):
        match = MIGRATION_RE.fullmatch(path.name)
        if not match:
            raise DatabaseError(f"invalid migration filename: {path.name}")
        result.append(
            Migration(int(match.group("version")), path, path.read_text(encoding="utf-8"))
        )
    versions = [migration.version for migration in result]
    if versions != list(range(1, len(versions) + 1)):
        raise DatabaseError("migrations must be contiguous starting at 001")
    return tuple(result)


def user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def backup_database(path: Path, destination: Path | None = None) -> Path | None:
    """Back up committed SQLite state, including WAL, without replacing a file."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    if destination is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = path.with_suffix(f".sqlite3.{stamp}.backup")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    deadline = time.monotonic() + BACKUP_TIMEOUT_SECONDS

    def check_deadline(status: int, remaining: int, total: int) -> None:
        if status != sqlite3.SQLITE_DONE and time.monotonic() >= deadline:
            raise DatabaseError("database backup timed out; stop other writers and retry")

    try:
        # A separate connection observes committed state and avoids backing up a
        # caller's autocommit=False transaction, which can retry forever on BUSY.
        with (
            closing(
                sqlite3.connect(
                    f"{path.resolve().as_uri()}?mode=ro",
                    uri=True,
                    autocommit=True,
                    timeout=0,
                )
            ) as source,
            closing(sqlite3.connect(destination, autocommit=True, timeout=0)) as target,
        ):
            source.execute("PRAGMA query_only = ON")
            source.backup(target, pages=128, progress=check_deadline, sleep=0.01)
    except sqlite3.Error as exc:
        destination.unlink(missing_ok=True)
        raise DatabaseError("database backup failed; original database was not changed") from exc
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def migrate(connection: sqlite3.Connection, database_path: Path) -> list[int]:
    available = migrations()
    supported = available[-1].version if available else 0
    applied: list[int] = []
    owns_transaction = False
    previous_autocommit = connection.autocommit
    previous_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    connection.execute(f"PRAGMA busy_timeout = {MIGRATION_BUSY_TIMEOUT_MS}")
    try:
        current = user_version(connection)
        if current > supported:
            raise DatabaseError("database schema is newer than this WorkTrace version supports")
        if current == supported:
            return []
        connection.autocommit = True
        # Reserve the writer before backup, retaining the reservation until all
        # migrations commit. The backup reads the pre-migration committed state.
        connection.execute("BEGIN IMMEDIATE")
        owns_transaction = True
        current = user_version(connection)
        if current > supported:
            raise DatabaseError("database schema is newer than this WorkTrace version supports")
        pending = [migration for migration in available if migration.version > current]
        if pending:
            backup_database(database_path)
        for migration in pending:
            try:
                statement = ""
                for line in migration.sql.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise sqlite3.OperationalError("incomplete SQL statement")
                connection.execute(f"PRAGMA user_version = {migration.version}")
            except sqlite3.Error as exc:
                raise DatabaseError(f"migration {migration.path.name} failed") from exc
            applied.append(migration.version)
        connection.execute("COMMIT")
        owns_transaction = False
    except sqlite3.Error as exc:
        if owns_transaction and connection.in_transaction:
            connection.execute("ROLLBACK")
        raise DatabaseError("database migration could not acquire or retain its lock") from exc
    except Exception:
        if owns_transaction and connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        if connection.autocommit != previous_autocommit:
            connection.autocommit = previous_autocommit
        connection.execute(f"PRAGMA busy_timeout = {previous_timeout}")
    return applied
