"""Canonical sync-run authority rules shared by every read surface."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping

REMOTE_POLICY_SOURCES = frozenset({"jira", "gitlab"})
CURRENT_SELECTION_POLICY_VERSION = 2
AUTHORITATIVE_COMPLETENESS = frozenset({"complete", "complete_for_scope", "selection_biased"})
FULL_SCOPE_COMPLETENESS = frozenset({"complete", "complete_for_scope"})
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


def completeness_is_authoritative(completeness: str) -> bool:
    return completeness in AUTHORITATIVE_COMPLETENESS


def completeness_is_full_scope(completeness: str) -> bool:
    return completeness in FULL_SCOPE_COMPLETENESS


def run_is_authoritative(
    source: str,
    status: str,
    completeness: str,
    scope: Mapping[str, object],
) -> bool:
    return (
        status == "complete"
        and completeness_is_authoritative(completeness)
        and scope_is_authoritative(source, scope)
    )


def authoritative_run_sql(alias: str) -> str:
    """Return the canonical SQLite predicate for an internally supplied table alias."""

    if not _SQL_ALIAS.fullmatch(alias):
        raise ValueError("invalid SQL alias")
    return f"""
        {alias}.status='complete'
        AND {alias}.completeness IN ('complete', 'complete_for_scope', 'selection_biased')
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


def authoritative_current_run_ctes() -> str:
    """Return shared CTEs selecting current eligible runs for every app/source instance."""

    return f"""
        ranked_authoritative_runs AS (
            SELECT sr.id, sr.app_id, sr.source, sr.source_instance, sr.completed_at,
                ROW_NUMBER() OVER (
                    PARTITION BY sr.app_id, sr.source, sr.source_instance
                    ORDER BY sr.completed_at DESC, sr.id DESC
                ) AS position
            FROM sync_runs sr
            WHERE {authoritative_run_sql("sr")}
        ),
        authoritative_current_runs AS (
            SELECT id, app_id, source, source_instance
            FROM ranked_authoritative_runs
            WHERE position=1 OR source='manual'
        )
    """


def authoritative_current_observation_ctes() -> str:
    """Return the canonical one-current-observation projection for every object."""

    return f"""
        {authoritative_current_run_ctes()},
        ranked_authoritative_observations AS (
            SELECT o.*,
                ROW_NUMBER() OVER (
                    PARTITION BY o.source_object_id
                    ORDER BY o.fetched_at DESC, o.id DESC
                ) AS position
            FROM observations o
            JOIN source_objects authority_object
              ON authority_object.id=o.source_object_id
            JOIN authoritative_current_runs current_run
              ON current_run.id=o.sync_run_id
             AND current_run.app_id=authority_object.app_id
             AND current_run.source=authority_object.source
             AND current_run.source_instance=authority_object.source_instance
        ),
        authoritative_current_observations AS (
            SELECT * FROM ranked_authoritative_observations WHERE position=1
        )
    """


def authoritative_current_participation_ctes() -> str:
    """Return current participations whose complete origin chain is coherent."""

    return f"""
        {authoritative_current_observation_ctes()},
        authoritative_current_participations AS (
            SELECT participation.*
            FROM participations participation
            JOIN authoritative_current_observations observation
              ON observation.id=participation.observation_id
             AND observation.source_object_id=participation.source_object_id
            JOIN source_objects authority_object
              ON authority_object.id=participation.source_object_id
            JOIN actors authority_actor
              ON authority_actor.id=participation.actor_id
             AND authority_actor.source=authority_object.source
             AND authority_actor.source_instance=authority_object.source_instance
        )
    """


def authoritative_current_reference_ctes() -> str:
    """Return current typed references backed by their declaring source object."""

    return f"""
        {authoritative_current_observation_ctes()},
        authoritative_current_references AS (
            SELECT reference.*
            FROM "references" reference
            JOIN source_objects from_object
              ON from_object.id=reference.from_object_id
             AND from_object.app_id=reference.app_id
            JOIN source_objects to_object
              ON to_object.id=reference.to_object_id
             AND to_object.app_id=reference.app_id
            JOIN authoritative_current_observations supporting_observation
              ON supporting_observation.id=reference.supporting_observation_id
             AND supporting_observation.source_object_id=reference.from_object_id
        )
    """


def authoritative_availability_event_ctes() -> str:
    """Return current availability CTEs after a ranked_authoritative_runs CTE.

    The event and its source object must share the producing run's complete origin tuple.
    The projected object fields are the final guard that the cited event is still current.
    """

    return """
        ranked_authoritative_availability_events AS (
            SELECT event.*,
                ROW_NUMBER() OVER (
                    PARTITION BY event.source_object_id
                    ORDER BY eligible_run.completed_at DESC,
                             event.observed_at DESC, event.id DESC
                ) AS position
            FROM source_object_availability_events event
            JOIN ranked_authoritative_runs eligible_run
              ON eligible_run.id=event.sync_run_id
            JOIN source_objects authority_object
              ON authority_object.id=event.source_object_id
             AND authority_object.app_id=eligible_run.app_id
             AND authority_object.source=eligible_run.source
             AND authority_object.source_instance=eligible_run.source_instance
        ),
        authoritative_current_availability_events AS (
            SELECT event.*
            FROM ranked_authoritative_availability_events event
            JOIN source_objects projected_object
              ON projected_object.id=event.source_object_id
            WHERE event.position=1
              AND projected_object.availability=event.state
              AND projected_object.availability_reason=event.reason
              AND projected_object.availability_observed_at=event.observed_at
        )
    """


def authoritative_current_availability_ctes() -> str:
    """Return self-contained CTEs for current citable availability events."""

    return f"""
        {authoritative_current_run_ctes()},
        {authoritative_availability_event_ctes()}
    """


def authoritative_current_observations(
    connection: sqlite3.Connection,
    app_id: str,
) -> list[sqlite3.Row]:
    """Return the one current citable observation per object for an application."""

    return list(
        connection.execute(
            f"""
            WITH {authoritative_current_observation_ctes()}
            SELECT current.*, so.app_id, so.source, so.source_instance, so.kind,
                so.external_id, so.canonical_url, so.availability,
                so.availability_reason, so.availability_observed_at
            FROM authoritative_current_observations current
            JOIN source_objects so ON so.id=current.source_object_id
            WHERE so.app_id=?
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


def authoritative_current_object_ids(
    connection: sqlite3.Connection,
    app_id: str,
) -> frozenset[str]:
    return frozenset(
        str(row["source_object_id"])
        for row in authoritative_current_observations(connection, app_id)
    )


def authority_limitation(source: str, scope: Mapping[str, object]) -> str | None:
    if scope_is_authoritative(source, scope):
        return None
    return (
        "Unversioned or legacy project-wide discovery is historical only; "
        "a selection-policy-v2 reimport is required for current authority."
    )


def run_authority_limitation(
    source: str,
    status: str,
    completeness: str,
    scope: Mapping[str, object],
) -> str | None:
    if status != "complete":
        return "The run did not complete successfully and is historical only."
    if not completeness_is_authoritative(completeness):
        return (
            "Partial or unknown-completeness runs are historical only; "
            "a complete reimport is required for current authority."
        )
    return authority_limitation(source, scope)
