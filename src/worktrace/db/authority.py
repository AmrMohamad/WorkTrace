"""Canonical sync-run authority rules shared by every read surface."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

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


def authority_limitation(source: str, scope: Mapping[str, object]) -> str | None:
    if scope_is_authoritative(source, scope):
        return None
    return (
        "Unversioned or legacy project-wide discovery is historical only; "
        "a selection-policy-v2 reimport is required for current authority."
    )
