"""Canonical sync-run authority rules shared by every read surface."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Collection, Mapping

REMOTE_POLICY_SOURCES = frozenset({"jira", "gitlab"})
CURRENT_SELECTION_POLICY_VERSION = 2
_SQL_ALIAS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_scope(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return {str(key): item for key, item in parsed.items()} if isinstance(parsed, dict) else {}


def selection_policy_version(scope: Mapping[str, object]) -> int | None:
    value = scope.get("selection_policy_version")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def scope_is_authoritative(source: str, scope: Mapping[str, object]) -> bool:
    """Remote project evidence is current only under the approved v2 selector."""

    if source.casefold() not in REMOTE_POLICY_SOURCES:
        return True
    version = selection_policy_version(scope)
    return version is not None and version >= CURRENT_SELECTION_POLICY_VERSION


def run_is_authoritative(source: str, status: str, scope: Mapping[str, object]) -> bool:
    return status == "complete" and scope_is_authoritative(source, scope)


def authoritative_run_sql(alias: str) -> str:
    """Return the canonical SQLite predicate for an internally supplied table alias."""

    if not _SQL_ALIAS.fullmatch(alias):
        raise ValueError("invalid SQL alias")
    return f"""
        {alias}.status='complete'
        AND (
            {alias}.source NOT IN ('jira', 'gitlab')
            OR (
                json_type(
                    {alias}.scope_json,
                    '$.selection_policy_version'
                )='integer'
                AND json_extract(
                    {alias}.scope_json,
                    '$.selection_policy_version'
                ) >= {CURRENT_SELECTION_POLICY_VERSION}
            )
        )
    """


def authoritative_current_observations(
    connection: sqlite3.Connection,
    app_id: str,
) -> list[sqlite3.Row]:
    """Return the one current citable observation per object for an application."""

    return list(
        connection.execute(
            f"""
            WITH current_runs AS (
                SELECT id FROM (
                    SELECT id, source,
                        ROW_NUMBER() OVER (
                            PARTITION BY app_id, source, source_instance
                            ORDER BY completed_at DESC, id DESC
                        ) AS position
                    FROM sync_runs sr
                    WHERE app_id=? AND {authoritative_run_sql("sr")}
                ) WHERE position=1 OR source='manual'
            ), latest AS (
                SELECT o.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY o.source_object_id ORDER BY o.fetched_at DESC, o.id DESC
                    ) AS position
                FROM observations o JOIN current_runs r ON r.id=o.sync_run_id
            )
            SELECT latest.*, so.app_id, so.source, so.source_instance, so.kind,
                so.external_id, so.canonical_url, so.availability,
                so.availability_reason, so.availability_observed_at
            FROM latest JOIN source_objects so ON so.id=latest.source_object_id
            WHERE latest.position=1
            ORDER BY so.source, so.kind, so.external_id
            """,
            (app_id,),
        )
    )


def authoritative_current_observation_ids(
    connection: sqlite3.Connection,
    app_id: str,
) -> frozenset[str]:
    return frozenset(
        str(row["id"]) for row in authoritative_current_observations(connection, app_id)
    )


def supporting_observation_is_authoritative(
    supporting_observation_id: object,
    current_observation_ids: Collection[str],
) -> bool:
    """Fail closed unless a typed fact is backed by a current citable observation."""

    return (
        isinstance(supporting_observation_id, str)
        and supporting_observation_id in current_observation_ids
    )


def authority_limitation(source: str, scope: Mapping[str, object]) -> str | None:
    if scope_is_authoritative(source, scope):
        return None
    return (
        "Unversioned or legacy project-wide discovery is historical only; "
        "a selection-policy-v2 reimport is required for current authority."
    )
