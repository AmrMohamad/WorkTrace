from __future__ import annotations

import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from worktrace.errors import DatabaseError

MIGRATION_RE = re.compile(r"^(?P<version>[0-9]{3})_[a-z0-9_]+\.sql$")


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
    if not path.exists() or path.stat().st_size == 0:
        return None
    if destination is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        destination = path.with_suffix(f".sqlite3.{stamp}.backup")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with path.open("rb") as source, os.fdopen(descriptor, "wb") as target:
            shutil.copyfileobj(source, target)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def migrate(connection: sqlite3.Connection, database_path: Path) -> list[int]:
    current = user_version(connection)
    pending = [migration for migration in migrations() if migration.version > current]
    if not pending:
        return []
    backup_database(database_path)
    applied: list[int] = []
    previous_autocommit = connection.autocommit
    connection.autocommit = True
    try:
        for migration in pending:
            try:
                connection.execute("BEGIN IMMEDIATE")
                statement = ""
                for line in migration.sql.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                if statement.strip():
                    raise sqlite3.OperationalError("incomplete SQL statement")
                connection.execute(f"PRAGMA user_version = {migration.version}")
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise DatabaseError(f"migration {migration.path.name} failed") from exc
            applied.append(migration.version)
    finally:
        connection.autocommit = previous_autocommit
    return applied
