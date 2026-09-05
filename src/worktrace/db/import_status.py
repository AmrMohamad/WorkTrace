"""SQLite-only preparation audit and readiness, independent of snapshot authority."""

from __future__ import annotations

import json
import sqlite3

from worktrace.db.authority import parse_scope, run_is_authoritative
from worktrace.db.repository import stable_id


def legacy_preflight_ids(connection: sqlite3.Connection, app_id: str) -> set[str]:
    """Recognize only the exact old credential-placeholder writer's empty failed runs."""
    return {
        str(row[0])
        for row in connection.execute(
            """SELECT r.id FROM sync_runs r WHERE r.app_id=? AND r.source='jira'
            AND r.source_instance=? AND r.status='failed'
            AND r.completeness='source_unavailable'
            AND r.error_summary='Jira credentials are not configured'
            AND NOT EXISTS (SELECT 1 FROM observations o WHERE o.sync_run_id=r.id)""",
            (app_id, stable_id("source", app_id, "jira", "configured")),
        )
    }


def source_readiness(connection: sqlite3.Connection, app_id: str) -> dict[str, dict[str, object]]:
    """Read latest preparation per configured target and retained authoritative snapshots."""
    result: dict[str, dict[str, object]] = {}
    seen: set[tuple[str, str]] = set()
    for row in connection.execute(
        "SELECT id, started_at, summary_json FROM import_sessions WHERE app_id=? "
        "ORDER BY started_at DESC, id DESC",
        (app_id,),
    ):
        try:
            summary = json.loads(str(row["summary_json"]))
        except (ValueError, TypeError):
            continue
        sources = summary.get("sources", []) if isinstance(summary, dict) else []
        if not isinstance(sources, list):
            continue
        for item in sources:
            if not isinstance(item, dict) or not isinstance(item.get("source"), str):
                continue
            source = item["source"]
            target = str(item.get("target", source))
            preflight = item.get("preflight")
            if not isinstance(preflight, dict) or (source, target) in seen:
                continue
            seen.add((source, target))
            entry = result.setdefault(source, {"preflight": [], "last_authoritative_snapshots": []})
            attempts = entry["preflight"]
            assert isinstance(attempts, list)
            attempts.append(
                {
                    **preflight,
                    "target": target,
                    "session_id": row["id"],
                    "attempted_at": row["started_at"],
                    "source_instance": item.get("source_instance"),
                }
            )
    snapshots: set[tuple[str, str]] = set()
    legacy = legacy_preflight_ids(connection, app_id)
    for row in connection.execute(
        "SELECT * FROM sync_runs WHERE app_id=? ORDER BY started_at DESC, id DESC", (app_id,)
    ):
        source, instance = str(row["source"]), str(row["source_instance"])
        entry = result.setdefault(source, {"preflight": [], "last_authoritative_snapshots": []})
        if str(row["id"]) in legacy:
            audit = entry.setdefault("legacy_preflight_audit", [])
            assert isinstance(audit, list)
            audit.append({"run_id": row["id"], "reason": "credentials_missing"})
            continue
        scope = parse_scope(row["scope_json"])
        if (source, instance) in snapshots or not run_is_authoritative(
            source, str(row["status"]), str(row["completeness"]), scope
        ):
            continue
        snapshots.add((source, instance))
        authoritative = entry["last_authoritative_snapshots"]
        assert isinstance(authoritative, list)
        authoritative.append(
            {
                "run_id": row["id"],
                "source_instance": instance,
                "completed_at": row["completed_at"],
                "completeness": row["completeness"],
                "selection_policy_version": scope.get("selection_policy_version"),
                "jira_seed_selection": scope.get("jira_seed_selection")
                if source == "jira"
                else None,
                "seed_input_authority": scope.get("seed_input_authority"),
                "selector_policy": "legacy"
                if source == "jira" and scope.get("selection_policy_version") != 3
                else "current",
                "coverage": "unknown"
                if source == "jira" and scope.get("selection_policy_version") != 3
                else "no-known-omissions"
                if row["completeness"] == "complete_for_scope"
                else "limited",
                "requested_scope": {
                    name: scope[name]
                    for name in ("date_from", "date_to", "work_timezone")
                    if name in scope
                },
            }
        )
    return result


def readiness_contract(
    connection: sqlite3.Connection,
    app_id: str,
    source: str,
    status: str,
    completeness: str,
    *,
    derived_current: bool = False,
    source_instance: str | None = None,
) -> dict[str, object]:
    known = (
        source_readiness(connection, app_id).get(source, {}).get("last_authoritative_snapshots", [])
    )
    snapshots = (
        [
            item
            for item in known
            if isinstance(item, dict)
            and source_instance is not None
            and item.get("source_instance") == source_instance
        ]
        if isinstance(known, list)
        else []
    )
    activated = status == "complete"
    coverage = (
        "no-known-omissions"
        if activated and completeness == "complete_for_scope"
        else "limited"
        if activated or snapshots
        else "unknown"
    )
    return {
        "execution": "complete" if activated else "failed" if status == "not_started" else status,
        "coverage": coverage,
        "snapshot_state": "activated"
        if activated
        else "previous_retained"
        if snapshots
        else "unavailable",
        "derived_data": "current" if derived_current else "requires_rebuild",
        "agent_review": "available"
        if coverage == "no-known-omissions" and derived_current
        else "available-with-gaps"
        if snapshots
        else "blocked",
    }
