"""Redacted SQLite-only Jira archive indexing and bounded search."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from worktrace.errors import KeychainError
from worktrace.normalize.redaction import Redactor

if TYPE_CHECKING:
    from worktrace.vault.extract import ExtractionLimits

SEARCH_SCHEMA_VERSION = 1
MAX_SEARCH_LIMIT = 20
CHUNK_CHARS = 10_000


def _encode(value: dict[str, object]) -> str:
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(value: str) -> dict[str, object]:
    try:
        payload = base64.urlsafe_b64decode(value + "=")
        decoded = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Jira search cursor") from exc
    if not isinstance(decoded, dict):
        raise ValueError("invalid Jira search cursor")
    return decoded


def collection_view_token(connection: sqlite3.Connection, collection_id: str) -> tuple[str, str]:
    row = connection.execute(
        "SELECT id FROM jira_archive_revisions WHERE collection_id=? AND status='active' "
        "ORDER BY revision_number DESC LIMIT 1",
        (collection_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Jira collection was not found")
    revision_id = str(row[0])
    digest = hashlib.sha256()
    for item in connection.execute(
        "SELECT id, kind, state, completeness, locator_json FROM jira_resource_states "
        "WHERE revision_id=? ORDER BY id",
        (revision_id,),
    ):
        digest.update(json.dumps(tuple(item), sort_keys=True).encode())
    for item in connection.execute(
        "SELECT attachment_id, original_state, extracted_state, ciphertext_sha256 "
        "FROM jira_attachment_objects WHERE revision_id=? ORDER BY attachment_id",
        (revision_id,),
    ):
        digest.update(json.dumps(tuple(item), sort_keys=True).encode())
    for item in connection.execute(
        "SELECT id, attachment_id, ordinal, chars, extraction_version "
        "FROM jira_search_chunks WHERE revision_id=? ORDER BY id",
        (revision_id,),
    ):
        digest.update(json.dumps(tuple(item), sort_keys=True).encode())
    return revision_id, "jira-view:" + digest.hexdigest()


def search_collection(
    connection: sqlite3.Connection,
    collection_id: str,
    query: str,
    *,
    limit: int = MAX_SEARCH_LIMIT,
    cursor: str | None = None,
    expected_view_token: str | None = None,
) -> dict[str, object]:
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ValueError("limit must be between 1 and 20")
    revision_id, view_token = collection_view_token(connection, collection_id)
    if expected_view_token is not None and expected_view_token != view_token:
        raise ValueError("Jira search view changed")
    last: tuple[str, str, int] | None = None
    if cursor:
        decoded = _decode(cursor)
        if set(decoded) != {
            "schema_version",
            "collection_id",
            "revision_id",
            "view_token",
            "query",
            "last",
        }:
            raise ValueError("Jira search cursor fields are invalid")
        if decoded.get("schema_version") != SEARCH_SCHEMA_VERSION:
            raise ValueError("Jira search cursor schema is unsupported")
        if (
            decoded.get("collection_id") != collection_id
            or decoded.get("revision_id") != revision_id
        ):
            raise ValueError("Jira search cursor scope mismatch")
        if decoded.get("view_token") != view_token or decoded.get("query") != query:
            raise ValueError("Jira search cursor is stale")
        raw_last = decoded.get("last")
        if (
            not isinstance(raw_last, list)
            or len(raw_last) != 3
            or not isinstance(raw_last[0], str)
            or not isinstance(raw_last[1], str)
            or not isinstance(raw_last[2], int)
        ):
            raise ValueError("Jira search cursor key is invalid")
        last = (raw_last[0], raw_last[1], raw_last[2])
    if last is None:
        rows = connection.execute(
            "SELECT issue_id, attachment_id, ordinal, locator_json, text_redacted "
            "FROM jira_search_chunks WHERE collection_id=? AND revision_id=? "
            "AND instr(lower(text_redacted), lower(?)) > 0 "
            "ORDER BY issue_id, attachment_id, ordinal LIMIT ?",
            (collection_id, revision_id, query, limit + 1),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT issue_id, attachment_id, ordinal, locator_json, text_redacted "
            "FROM jira_search_chunks WHERE collection_id=? AND revision_id=? "
            "AND instr(lower(text_redacted), lower(?)) > 0 "
            "AND (issue_id>? OR (issue_id=? AND attachment_id>?) OR "
            "(issue_id=? AND attachment_id=? AND ordinal>?)) "
            "ORDER BY issue_id, attachment_id, ordinal LIMIT ?",
            (
                collection_id,
                revision_id,
                query,
                last[0],
                last[0],
                last[1],
                last[0],
                last[1],
                last[2],
                limit + 1,
            ),
        ).fetchall()
    delivered = rows[:limit]
    hits = [
        {
            "issue_id": str(row[0]),
            "attachment_id": str(row[1]),
            "ordinal": int(row[2]),
            "locator": json.loads(str(row[3])),
            "text": str(row[4]),
        }
        for row in delivered
    ]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = _encode(
            {
                "schema_version": SEARCH_SCHEMA_VERSION,
                "collection_id": collection_id,
                "revision_id": revision_id,
                "view_token": view_token,
                "query": query,
                "last": [str(delivered[-1][0]), str(delivered[-1][1]), int(delivered[-1][2])],
            }
        )
    return {
        "schema_version": SEARCH_SCHEMA_VERSION,
        "collection_id": collection_id,
        "revision_id": revision_id,
        "view_token": view_token,
        "query": query,
        "hits": hits,
        "next_cursor": next_cursor,
        "limitations": ["search reads redacted extracted chunks only"],
    }


def show_issue(
    connection: sqlite3.Connection,
    collection_id: str,
    issue_id: str,
    *,
    limit: int = MAX_SEARCH_LIMIT,
    cursor: str | None = None,
    expected_view_token: str | None = None,
) -> dict[str, object]:
    if not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise ValueError("limit must be between 1 and 20")
    revision_id, view_token = collection_view_token(connection, collection_id)
    if expected_view_token is not None and expected_view_token != view_token:
        raise ValueError("Jira issue view changed")
    last_ordinal: int | None = None
    if cursor:
        decoded = _decode(cursor)
        if set(decoded) != {
            "schema_version",
            "collection_id",
            "revision_id",
            "view_token",
            "issue_id",
            "last",
        }:
            raise ValueError("Jira issue cursor fields are invalid")
        if decoded.get("schema_version") != SEARCH_SCHEMA_VERSION:
            raise ValueError("Jira issue cursor schema is unsupported")
        if (
            decoded.get("collection_id") != collection_id
            or decoded.get("revision_id") != revision_id
            or decoded.get("view_token") != view_token
            or decoded.get("issue_id") != issue_id
        ):
            raise ValueError("Jira issue cursor is stale")
        raw_last = decoded.get("last")
        if (
            not isinstance(raw_last, list)
            or len(raw_last) != 2
            or not isinstance(raw_last[0], int)
            or raw_last[0] < 0
            or not isinstance(raw_last[1], str)
        ):
            raise ValueError("Jira issue cursor key is invalid")
        last_ordinal = raw_last[0]
        last_attachment_id = raw_last[1]
    else:
        last_attachment_id = ""
    issue = connection.execute(
        "SELECT issue_id, issue_key, role, assignment_status, boundary_status "
        "FROM jira_collection_issues WHERE collection_id=? AND revision_id=? AND issue_id=?",
        (collection_id, revision_id, issue_id),
    ).fetchone()
    if issue is None:
        raise ValueError("Jira issue is outside the collection")
    resources = [
        {
            "kind": str(row[0]),
            "state": str(row[1]),
            "completeness": str(row[2]),
            "availability": str(row[3]),
            "locator": json.loads(str(row[4])),
        }
        for row in connection.execute(
            "SELECT kind, state, completeness, availability, locator_json "
            "FROM jira_resource_states WHERE collection_id=? AND revision_id=? AND issue_id=? "
            "ORDER BY kind, locator_json",
            (collection_id, revision_id, issue_id),
        )
    ]
    attachments = [
        {
            "attachment_id": str(row[0]),
            "mime_type": str(row[1]),
            "declared_size": row[2],
            "original_state": str(row[3]),
            "extracted_state": str(row[4]),
        }
        for row in connection.execute(
            "SELECT attachment_id, mime_type, declared_size, original_state, extracted_state "
            "FROM jira_attachment_objects WHERE collection_id=? AND revision_id=? AND issue_id=? "
            "ORDER BY attachment_id",
            (collection_id, revision_id, issue_id),
        )
    ]
    chunk_query = (
        "SELECT attachment_id, ordinal, locator_json, text_redacted, chars, extraction_version "
        "FROM jira_search_chunks WHERE collection_id=? AND revision_id=? AND issue_id=? "
    )
    chunk_args: tuple[object, ...] = (collection_id, revision_id, issue_id)
    if last_ordinal is not None:
        chunk_query += "AND (ordinal>? OR (ordinal=? AND attachment_id>?)) "
        chunk_args += (last_ordinal, last_ordinal, last_attachment_id)
    chunk_query += "ORDER BY ordinal, attachment_id LIMIT ?"
    chunk_args += (limit + 1,)
    chunk_rows = connection.execute(chunk_query, chunk_args).fetchall()
    chunks = [
        {
            "attachment_id": str(row[0]),
            "ordinal": int(row[1]),
            "locator": json.loads(str(row[2])),
            "text": str(row[3]),
            "chars": int(row[4]),
            "extraction_version": str(row[5]),
        }
        for row in chunk_rows[:limit]
    ]
    next_cursor = None
    if len(chunk_rows) > limit:
        next_cursor = _encode(
            {
                "schema_version": SEARCH_SCHEMA_VERSION,
                "collection_id": collection_id,
                "revision_id": revision_id,
                "view_token": view_token,
                "issue_id": issue_id,
                "last": [int(chunks[-1]["ordinal"]), str(chunks[-1]["attachment_id"])],
            }
        )
    return {
        "schema_version": 1,
        "collection_id": collection_id,
        "revision_id": revision_id,
        "view_token": view_token,
        "issue": {
            "issue_id": str(issue[0]),
            "issue_key": str(issue[1]),
            "role": str(issue[2]),
            "assignment_status": str(issue[3]),
            "boundary_status": str(issue[4]),
        },
        "resources": resources,
        "attachments": attachments,
        "chunks": chunks,
        "next_cursor": next_cursor,
        "limitations": ["raw Jira payloads and vault paths are never returned"],
    }


def show_attachment(
    connection: sqlite3.Connection,
    collection_id: str,
    attachment_id: str,
    *,
    expected_view_token: str | None = None,
) -> dict[str, object]:
    """Return one active-revision attachment and its redacted indexed chunks."""
    revision_id, view_token = collection_view_token(connection, collection_id)
    if expected_view_token is not None and expected_view_token != view_token:
        raise ValueError("Jira attachment view changed")
    row = connection.execute(
        "SELECT issue_id, mime_type, declared_size, original_state, extracted_state "
        "FROM jira_attachment_objects WHERE collection_id=? AND revision_id=? "
        "AND attachment_id=?",
        (collection_id, revision_id, attachment_id),
    ).fetchone()
    if row is None:
        raise ValueError("Jira attachment is outside the collection")
    chunks = [
        {
            "ordinal": int(chunk[0]),
            "locator": json.loads(str(chunk[1])),
            "text": str(chunk[2]),
            "chars": int(chunk[3]),
            "extraction_version": str(chunk[4]),
        }
        for chunk in connection.execute(
            "SELECT ordinal, locator_json, text_redacted, chars, extraction_version "
            "FROM jira_search_chunks WHERE collection_id=? AND revision_id=? "
            "AND attachment_id=? ORDER BY ordinal LIMIT ?",
            (collection_id, revision_id, attachment_id, MAX_SEARCH_LIMIT),
        )
    ]
    return {
        "schema_version": SEARCH_SCHEMA_VERSION,
        "collection_id": collection_id,
        "revision_id": revision_id,
        "view_token": view_token,
        "attachment": {
            "attachment_id": attachment_id,
            "issue_id": str(row[0]),
            "mime_type": str(row[1]),
            "declared_size": row[2],
            "original_state": str(row[3]),
            "extracted_state": str(row[4]),
        },
        "chunks": chunks,
        "limitations": ["raw Jira payloads, vault paths, and original bytes are unavailable"],
    }


def redact_chunks(text: str, redactor: Redactor) -> list[tuple[int, str, dict[str, object]]]:
    redacted = redactor.redact_text(text)
    return [
        (offset // CHUNK_CHARS, piece, {"char_start": offset, "char_end": offset + len(piece)})
        for offset in range(0, len(redacted), CHUNK_CHARS)
        for piece in (redacted[offset : offset + CHUNK_CHARS],)
    ]


def index_collection_attachments(
    connection: sqlite3.Connection,
    *,
    collection_id: str,
    vault_root: str,
    key_for_version: Callable[[int], bytes | None],
    redactor: Redactor,
    worker: Callable[..., Any] | None = None,
    limits: ExtractionLimits | None = None,
) -> dict[str, int]:
    if worker is None:
        from worktrace.vault.extract import run_vault_worker

        worker = run_vault_worker
    if limits is None:
        from worktrace.vault.extract import DEFAULT_LIMITS

        limits = DEFAULT_LIMITS
    revision_id, _ = collection_view_token(connection, collection_id)
    counts = {"complete": 0, "unsupported": 0, "failed": 0, "extraction_unavailable": 0}
    rows = connection.execute(
        "SELECT id, attachment_id, mime_type, filename, vault_object_path, "
        "vault_key_version, ciphertext_sha256, declared_size FROM jira_attachment_objects "
        "WHERE collection_id=? AND revision_id=? AND original_state='complete'",
        (collection_id, revision_id),
    ).fetchall()
    for row in rows:
        if row["vault_object_path"] is None or row["vault_key_version"] is None:
            continue
        source = Path(vault_root) / str(row["vault_object_path"])
        try:
            key = key_for_version(int(row["vault_key_version"]))
            if key is None:
                result = None
                status = "extraction_unavailable"
            else:
                with source.open("rb") as stream:
                    result = worker(
                        stream,
                        key,
                        expected_ciphertext_sha256=row["ciphertext_sha256"],
                        attachment_id=str(row["attachment_id"]),
                        declared_length=row["declared_size"],
                        mime_type=row["mime_type"],
                        limits=limits,
                    )
                status = result.status
        except KeychainError:
            result = None
            status = "extraction_unavailable"
        except OSError:
            result = None
            status = "failed"
        counts[status] = counts.get(status, 0) + 1
        with connection:
            connection.execute(
                "UPDATE jira_attachment_objects SET extracted_state=? WHERE id=?",
                (status, row["id"]),
            )
            connection.execute(
                "DELETE FROM jira_search_chunks WHERE collection_id=? AND revision_id=? "
                "AND attachment_id=?",
                (collection_id, revision_id, row["attachment_id"]),
            )
            if status == "complete" and result is not None:
                for ordinal, text, locator in redact_chunks(result.text, redactor):
                    connection.execute(
                        "INSERT INTO jira_search_chunks "
                        "(id, collection_id, revision_id, issue_id, attachment_id, ordinal, "
                        "locator_json, text_redacted, chars, extraction_version) "
                        "SELECT ?, ?, ?, issue_id, ?, ?, ?, ?, ?, ? "
                        "FROM jira_attachment_objects WHERE id=?",
                        (
                            f"jchunk:{row['id']}:{ordinal}",
                            collection_id,
                            revision_id,
                            row["attachment_id"],
                            ordinal,
                            json.dumps(locator, sort_keys=True, separators=(",", ":")),
                            text,
                            len(text),
                            result.parser_version,
                            row["id"],
                        ),
                    )
    return counts
