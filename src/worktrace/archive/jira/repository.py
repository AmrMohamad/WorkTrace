from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from worktrace.archive.jira.models import ResourceCheckpoint
from worktrace.errors import ConfigurationError, DatabaseError

_SITE_HASH_PREFIX = "jira-site:"
_SITE_ORIGIN_RE = re.compile(r"^https://[^/?#]+(?:/[^?#]*)?$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def canonical_jira_origin(value: str) -> str:
    """Normalize an HTTPS Jira origin without accepting credentials or query state."""
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("Jira origin has an invalid port") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in (None, 443)
    ):
        raise ConfigurationError("Jira origin must be a credential-free HTTPS origin")
    host = parsed.hostname.casefold()
    path = parsed.path.rstrip("/") or ""
    origin = urlunsplit(("https", host, path, "", ""))
    if _SITE_ORIGIN_RE.fullmatch(origin) is None:
        raise ConfigurationError("Jira origin is invalid")
    return origin


def site_id_for_origin(origin: str) -> str:
    canonical = canonical_jira_origin(origin)
    return _SITE_HASH_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _uuid_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4()}"


class JiraArchiveRepository:
    """Write-owned foundation repository for archive metadata/checkpoints only."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_site(self, origin: str) -> str:
        canonical = canonical_jira_origin(origin)
        site_id = site_id_for_origin(canonical)
        with self.connection:
            self.connection.execute(
                "INSERT INTO jira_archive_sites(id, canonical_origin, hash_algorithm) "
                "VALUES (?, ?, 'sha256') ON CONFLICT(id) DO UPDATE SET "
                "canonical_origin=excluded.canonical_origin",
                (site_id, canonical),
            )
        return site_id

    def create_collection(
        self,
        *,
        site_id: str,
        scope: Mapping[str, object],
        scope_hash: str,
        approval_token_hash: str,
        policy_version: int,
        config_fingerprint: str,
        vault_id: str,
    ) -> str:
        collection_id = _uuid_id("jcol")
        with self.connection:
            self.connection.execute(
                "INSERT INTO jira_collections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    collection_id,
                    site_id,
                    _json(scope),
                    scope_hash,
                    approval_token_hash,
                    policy_version,
                    config_fingerprint,
                    vault_id,
                    _now(),
                ),
            )
        return collection_id

    def start_run(self, collection_id: str) -> str:
        run_id = _uuid_id("jrun")
        with self.connection:
            self.connection.execute(
                "INSERT INTO jira_collection_runs VALUES (?, ?, 'running', ?, NULL, '{}', NULL)",
                (run_id, collection_id, _now()),
            )
        return run_id

    def create_revision(self, collection_id: str, run_id: str, manifest_hash: str) -> str:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM jira_archive_revisions "
            "WHERE collection_id=?",
            (collection_id,),
        ).fetchone()
        number = int(row[0]) if row else 1
        revision_id = f"jrev:{collection_id}:{number}"
        with self.connection:
            self.connection.execute(
                "INSERT INTO jira_archive_revisions "
                "(id, collection_id, run_id, revision_number, status, manifest_hash) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (revision_id, collection_id, run_id, number, manifest_hash),
            )
        return revision_id

    def checkpoint_resource(
        self,
        *,
        resource_id: str,
        state: str,
        completeness: str,
        availability: str,
        seen_count: int,
        page_cursor: str | None,
        attempt: int,
        fetched_at: str | None = None,
        error: Mapping[str, object] | None = None,
        raw_vault_object_id: str | None = None,
        raw_vault_object_path: str | None = None,
    ) -> None:
        """Atomically publish one resource checkpoint and its progress boundary."""
        if seen_count < 0 or attempt < 0:
            raise ValueError("resource checkpoint counters must not be negative")
        with self.connection:
            updated = self.connection.execute(
                "UPDATE jira_resource_states SET state=?, completeness=?, availability=?, "
                "seen_count=?, page_cursor=?, attempt=?, fetched_at=?, error_json=?, "
                "raw_vault_object_id=?, raw_vault_object_path=? WHERE id=?",
                (
                    state,
                    completeness,
                    availability,
                    seen_count,
                    page_cursor,
                    attempt,
                    fetched_at,
                    _json(error) if error is not None else None,
                    raw_vault_object_id,
                    raw_vault_object_path,
                    resource_id,
                ),
            )
            if updated.rowcount != 1:
                raise DatabaseError("archive resource checkpoint not found")

    def create_resource_checkpoint(
        self,
        *,
        resource_id: str,
        collection_id: str,
        revision_id: str,
        run_id: str,
        issue_id: str,
        kind: str,
        locator: Mapping[str, object],
        role: str,
        redaction_version: str,
        raw_vault_object_path: str | None = None,
        state: str = "planned",
    ) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO jira_resource_states "
                "(id, collection_id, revision_id, run_id, archive_evidence_id, issue_id, kind, "
                "locator_json, role, state, completeness, availability, redaction_version, "
                "raw_vault_object_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown', "
                "'unknown', ?, ?)",
                (
                    resource_id,
                    collection_id,
                    revision_id,
                    run_id,
                    resource_id,
                    issue_id,
                    kind,
                    _json(locator),
                    role,
                    state,
                    redaction_version,
                    raw_vault_object_path,
                ),
            )

    def get_checkpoint(self, resource_id: str) -> ResourceCheckpoint:
        row = self.connection.execute(
            "SELECT id, collection_id, revision_id, run_id, issue_id, kind, locator_json, state, "
            "completeness, availability, expected_count, seen_count, page_cursor, attempt, "
            "raw_vault_object_id, raw_vault_object_path, redaction_version, "
            "source_updated_at, fetched_at, error_json "
            "FROM jira_resource_states WHERE id=?",
            (resource_id,),
        ).fetchone()
        if row is None:
            raise DatabaseError("archive resource checkpoint not found")
        return ResourceCheckpoint(*row)
