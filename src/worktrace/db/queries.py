from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from worktrace.constants import MAX_RECORDS, STALE_AFTER_DAYS
from worktrace.db.authority import (
    authoritative_current_observation_ctes,
    completeness_is_full_scope,
    parse_scope,
    run_authority_limitation,
    run_is_authoritative,
    selection_policy_version,
)
from worktrace.errors import NotFound, ScopeViolation


def source_status(connection: sqlite3.Connection, app_id: str) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT * FROM (
            SELECT source, source_instance, status, completeness, started_at, completed_at,
                   error_summary, progress_json, scope_json,
                   ROW_NUMBER() OVER (
                     PARTITION BY source, source_instance ORDER BY started_at DESC, id DESC
                   ) AS position
            FROM sync_runs WHERE app_id=?
        ) WHERE position=1 ORDER BY source, source_instance
        """,
        (app_id,),
    )
    cutoff = datetime.now(UTC) - timedelta(days=STALE_AFTER_DAYS)
    result: list[dict[str, object]] = []
    for row in rows:
        completed = str(row["completed_at"] or row["started_at"])
        try:
            stale = datetime.fromisoformat(completed.replace("Z", "+00:00")) < cutoff
        except ValueError:
            stale = True
        scope = parse_scope(row["scope_json"])
        selection_version = selection_policy_version(scope)
        authoritative = run_is_authoritative(
            str(row["source"]),
            str(row["status"]),
            str(row["completeness"]),
            scope,
        )
        try:
            raw_progress = json.loads(str(row["progress_json"]))
        except (TypeError, json.JSONDecodeError):
            raw_progress = {}
        progress = raw_progress if isinstance(raw_progress, dict) else {}
        raw_limitations = progress.get("limitations", [])
        limitations = (
            [value for value in raw_limitations if isinstance(value, str) and value]
            if isinstance(raw_limitations, list)
            else []
        )
        limitation = run_authority_limitation(
            str(row["source"]),
            str(row["status"]),
            str(row["completeness"]),
            scope,
        )
        if limitation and limitation not in limitations:
            limitations.append(limitation)
        complete = authoritative and completeness_is_full_scope(str(row["completeness"]))
        result.append(
            {
                "source": str(row["source"]),
                "source_instance": str(row["source_instance"]),
                "status": str(row["status"]),
                "completeness": str(row["completeness"]),
                "completed_at": row["completed_at"],
                "stale": stale,
                "error": row["error_summary"],
                "progress": progress,
                "complete": complete,
                "selection_policy_version": selection_version,
                "authoritative_current": authoritative,
                "limitations": limitations,
                "selection_events": progress.get("selection_events", []),
            }
        )
    return result


def search_evidence(
    connection: sqlite3.Connection,
    app_id: str,
    query: str,
    *,
    kinds: tuple[str, ...] = (),
    limit: int = MAX_RECORDS,
) -> list[dict[str, object]]:
    if limit < 1 or limit > MAX_RECORDS:
        raise ScopeViolation(f"limit must be between 1 and {MAX_RECORDS}")
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    parameters: list[object] = [app_id, f"%{escaped}%", f"%{escaped}%"]
    kind_filter = ""
    if kinds:
        kind_filter = f" AND so.kind IN ({','.join('?' for _ in kinds)})"
        parameters.extend(kinds)
    parameters.append(limit)
    rows = connection.execute(
        f"""
        WITH {authoritative_current_observation_ctes()}
        SELECT o.id AS evidence_id, so.id AS object_id, so.kind, so.source,
               so.external_id, o.title, o.body_text, o.completeness, o.fetched_at
        FROM authoritative_current_observations o
        JOIN source_objects so ON so.id=o.source_object_id
        WHERE so.app_id=? AND (COALESCE(o.title, '') LIKE ? ESCAPE '\\'
              OR COALESCE(o.body_text, '') LIKE ? ESCAPE '\\')
              {kind_filter}
        ORDER BY o.fetched_at DESC, o.id LIMIT ?
        """,
        parameters,
    )
    return [dict(row) for row in rows]


def evidence_excerpt(
    connection: sqlite3.Connection,
    evidence_id: str,
    *,
    chars: int,
) -> dict[str, object]:
    row = connection.execute(
        f"""
        WITH {authoritative_current_observation_ctes()}
        SELECT o.id AS evidence_id, so.app_id, so.source, so.kind, so.external_id,
               o.title, o.body_text, o.data_json, o.completeness, o.fetched_at,
               sr.status AS run_status, sr.completeness AS run_completeness
        FROM authoritative_current_observations o
        JOIN source_objects so ON so.id=o.source_object_id
        JOIN sync_runs sr ON sr.id=o.sync_run_id
        WHERE o.id=?
        """,
        (evidence_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"evidence not found: {evidence_id}")
    text = "\n".join(value for value in (row["title"], row["body_text"]) if value)
    return {
        "evidence_id": str(row["evidence_id"]),
        "app_id": str(row["app_id"]),
        "source": str(row["source"]),
        "kind": str(row["kind"]),
        "external_id": str(row["external_id"]),
        "excerpt": text[:chars],
        "truncated": len(text) > chars,
        "source_text_is_untrusted": True,
        "completeness": str(row["completeness"]),
        "run_status": str(row["run_status"]),
        "run_completeness": str(row["run_completeness"]),
        "authoritative_current": True,
        "as_of": str(row["fetched_at"]),
    }
