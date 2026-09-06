"""Bounded, canonical context rows for one configured source object.

This module deliberately stops before MCP cursor serialization and response
budget admission.  Its streams retain excluded positions so the transport can
advance a continuation only after an excluded or delivered row.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import cast

from worktrace.candidates.builder import GENERATOR_VERSION
from worktrace.candidates.decisions import CREATION_ACTIONS, snapshot_member_ids
from worktrace.config import AppConfig
from worktrace.db.authority import (
    authoritative_availability_event_ctes,
    authoritative_current_observation_ctes,
    authoritative_current_reference_ctes,
    authoritative_run_sql,
)
from worktrace.db.repository import source_instance_id
from worktrace.errors import NotFound, ScopeViolation
from worktrace.packets.builder import PacketBuilder
from worktrace.packets.models import ContributionView
from worktrace.read_models.candidates import _active_generation

MAX_CONTEXT_SCAN_BUDGET = 200
type ScannedRow = tuple[dict[str, str], dict[str, object] | None, bool]
_JIRA_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-[1-9][0-9]*$")


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _source_instances(app: AppConfig, source: str) -> frozenset[str]:
    if source == "git":
        return frozenset(source_instance_id(app.id, "git", path) for path in app.repo_paths)
    if source == "gitlab":
        return frozenset(
            source_instance_id(app.id, "gitlab", project_id)
            for project_id in app.gitlab_project_ids
        )
    return frozenset()


_JIRA_ROOT_KINDS = frozenset({"jira_issue"})
_JIRA_SUBRESOURCE_KINDS = frozenset(
    {"issue_comment", "issue_changelog", "jira_comment", "jira_changelog"}
)


def _configured_jira_issue_key(app: AppConfig, value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(_JIRA_ISSUE_KEY.fullmatch(value.upper()))
        and app.allows_jira_key(value.upper())
    )


def _jira_root_in_scope(app: AppConfig, external_id: str, metadata: Mapping[str, object]) -> bool:
    """Validate a root issue from its own identity, never its parent metadata."""

    key = metadata.get("key")
    if key is not None:
        return _configured_jira_issue_key(app, key)
    issue_key = metadata.get("issue_key")
    if issue_key is not None:
        return _configured_jira_issue_key(app, issue_key)
    # Some older normalized root records kept only the textual Jira key as the
    # external identifier.  Numeric Jira IDs cannot establish project scope.
    return _configured_jira_issue_key(app, external_id)


def _jira_parent_binding_in_scope(
    connection: sqlite3.Connection,
    app: AppConfig,
    *,
    source_instance: str,
    issue_id: object,
) -> bool:
    if not isinstance(issue_id, str) or not issue_id:
        return False
    row = connection.execute(
        f"""
        WITH {authoritative_current_observation_ctes()}
        SELECT parent.external_id,
               {_scope_metadata_json("current", "parent")} AS metadata_json
        FROM source_objects parent
        LEFT JOIN authoritative_current_observations current
          ON current.source_object_id=parent.id
        WHERE parent.app_id=? AND parent.source='jira'
          AND parent.source_instance=? AND parent.kind='jira_issue'
          AND parent.external_id=?
        """,
        (app.id, source_instance, issue_id),
    ).fetchone()
    return row is not None and _jira_root_in_scope(
        app, str(row["external_id"]), _json_object(row["metadata_json"])
    )


def _object_in_scope(
    app: AppConfig,
    *,
    connection: sqlite3.Connection,
    source: str,
    source_instance: str,
    kind: str,
    external_id: str,
    metadata_json: object,
) -> bool:
    """Validate configured source scope without following source-controlled URLs."""

    metadata = _json_object(metadata_json)
    if source == "manual":
        return True
    if source == "git":
        return source_instance in _source_instances(app, source)
    if source == "gitlab":
        if source_instance not in _source_instances(app, source):
            return False
        project_id = metadata.get("project_id")
        if project_id is None:
            return True
        if isinstance(project_id, bool):
            return False
        if isinstance(project_id, int):
            return project_id in app.gitlab_project_ids
        if isinstance(project_id, str) and project_id.isascii() and project_id.isdecimal():
            return int(project_id) in app.gitlab_project_ids
        return False
    if source == "jira":
        if kind in _JIRA_ROOT_KINDS:
            return _jira_root_in_scope(app, external_id, metadata)
        if kind not in _JIRA_SUBRESOURCE_KINDS:
            return False
        owning_key = metadata.get("issue_key")
        if owning_key is not None:
            return _configured_jira_issue_key(app, owning_key)
        # A parent key is only a fallback for an actual Jira subresource with
        # no own issue identity and an exact stored parent-object binding.
        return _jira_parent_binding_in_scope(
            connection,
            app,
            source_instance=source_instance,
            issue_id=metadata.get("issue_id"),
        )
    return False


def _availability(row: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    event_id = row[f"{prefix}availability_event_id"]
    return {
        "state": str(row[f"{prefix}availability"]) if event_id is not None else "unknown",
        "current": event_id is not None,
        "evidence_id": str(event_id) if event_id is not None else None,
        "reason": (
            str(row[f"{prefix}availability_reason"])
            if event_id is not None and row[f"{prefix}availability_reason"] is not None
            else None
        ),
        "observed_at": (
            str(row[f"{prefix}availability_observed_at"])
            if event_id is not None and row[f"{prefix}availability_observed_at"] is not None
            else None
        ),
    }


def _scope_metadata_json(current_alias: str, object_alias: str) -> str:
    """Select only scope keys from authoritative-origin metadata, never a body."""

    fields = (
        "key",
        "issue_key",
        "project_key",
        "parent_key",
        "parent_issue_key",
        "parent_identifier",
        "issue_id",
        "project_id",
    )
    current_fields = ", ".join(
        f"'{field}', json_extract({current_alias}.data_json, '$.{field}')" for field in fields
    )
    historical_fields = ", ".join(
        f"'{field}', json_extract(historical.data_json, '$.{field}')" for field in fields
    )
    return f"""
        CASE WHEN {current_alias}.id IS NOT NULL
             THEN json_object({current_fields})
             ELSE (
                 SELECT json_object({historical_fields})
                 FROM observations historical
                 JOIN sync_runs historical_run ON historical_run.id=historical.sync_run_id
                 WHERE historical.source_object_id={object_alias}.id
                   AND historical_run.app_id={object_alias}.app_id
                   AND historical_run.source={object_alias}.source
                   AND historical_run.source_instance={object_alias}.source_instance
                   AND {authoritative_run_sql("historical_run")}
                 ORDER BY historical.fetched_at DESC, historical.id DESC
                 LIMIT 1
             )
        END
    """


def _object_metadata_row(
    builder: PacketBuilder,
    app_id: str,
    object_id: str,
) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        builder.connection.execute(
            f"""
        WITH {authoritative_current_observation_ctes()},
             {authoritative_availability_event_ctes()}
        SELECT object.id AS object_id, object.app_id, object.source,
               object.source_instance, object.kind, object.external_id,
               object.availability, object.availability_reason,
               object.availability_observed_at,
               current.id AS current_observation_id,
               event.id AS availability_event_id,
               {_scope_metadata_json("current", "object")} AS metadata_json
        FROM source_objects object
        LEFT JOIN authoritative_current_observations current
          ON current.source_object_id=object.id
        LEFT JOIN authoritative_current_availability_events event
          ON event.source_object_id=object.id
        WHERE object.id=? AND object.app_id=?
        """,
            (object_id, app_id),
        ).fetchone(),
    )


def describe_object(builder: PacketBuilder, app_id: str, object_id: str) -> dict[str, object]:
    """Return bounded identity/currentness metadata for one configured object.

    This intentionally requires a stored ``source_objects`` identifier.  An
    observation, reference, decision, participation, or candidate identifier
    cannot be translated into a source object here.
    """

    app = builder.config.app(app_id)
    if not isinstance(object_id, str) or not object_id.startswith("obj:"):
        raise ScopeViolation("object_id must be a source-object identifier")
    row = _object_metadata_row(builder, app_id, object_id)
    if row is None:
        foreign = builder.connection.execute(
            "SELECT 1 FROM source_objects WHERE id=?", (object_id,)
        ).fetchone()
        if foreign is not None:
            raise ScopeViolation("object belongs to another configured application")
        raise NotFound("source object not found")
    if not _object_in_scope(
        app,
        connection=builder.connection,
        source=str(row["source"]),
        source_instance=str(row["source_instance"]),
        kind=str(row["kind"]),
        external_id=str(row["external_id"]),
        metadata_json=row["metadata_json"],
    ):
        raise ScopeViolation("source object is outside current configured source scope")
    availability_row = {
        "availability": row["availability"],
        "availability_reason": row["availability_reason"],
        "availability_observed_at": row["availability_observed_at"],
        "availability_event_id": row["availability_event_id"],
    }
    return {
        "app_id": app_id,
        "object_id": str(row["object_id"]),
        "source": str(row["source"]),
        "kind": str(row["kind"]),
        "external_id": str(row["external_id"]),
        "current_observation_id": (
            str(row["current_observation_id"])
            if row["current_observation_id"] is not None
            else None
        ),
        "availability": _availability(availability_row),
        "limitations": (
            []
            if row["current_observation_id"] is not None
            else [
                "No authoritative current observation is available; historical metadata "
                "establishes scope only."
            ]
        ),
    }


def _legacy_role_snapshot_limitations(builder: PacketBuilder, app_id: str) -> list[str]:
    context = builder._decision_projection
    if context is None:
        # Building this projection is deliberate: contexts must use precisely
        # the canonical decision stream that contribution resolution uses.
        builder = builder.page_projection_builder(app_id)
        context = builder._decision_projection
    assert context is not None
    for decision in context.active_decisions:
        if context.decision_scopes.get(decision.id) != app_id:
            continue
        if decision.action in {"merge", "split", "merge_contributions", "split_contribution"} and (
            "context_members" not in decision.payload
        ):
            return [
                "A historical merge/split snapshot lacks context-role metadata; "
                "human re-review is required before treating roles as complete."
            ]
    return []


def context_readiness(builder: PacketBuilder, app_id: str) -> dict[str, object]:
    """Return context-specific derived-state limitations without claiming coverage."""

    builder.config.app(app_id)
    rows = list(
        builder.connection.execute(
            "SELECT DISTINCT generator_version FROM candidate_groups WHERE app_id=?",
            (app_id,),
        )
    )
    versions = {str(row["generator_version"]) for row in rows}
    requires_rebuild = bool(versions and versions != {GENERATOR_VERSION})
    limitations: list[str] = []
    if requires_rebuild:
        limitations.append(
            "Generated memberships use a legacy generator; run explicitly authorized "
            "`rebuild all` before treating them as current."
        )
    limitations.extend(_legacy_role_snapshot_limitations(builder, app_id))
    return {"requires_rebuild": requires_rebuild, "limitations": limitations}


@dataclass(slots=True)
class _LazyRows(Iterator[ScannedRow]):
    rows: tuple[sqlite3.Row, ...]
    has_more_after_last: bool
    project: object
    _index: int = 0

    def __next__(self) -> ScannedRow:
        if self._index >= len(self.rows):
            raise StopIteration
        row = self.rows[self._index]
        self._index += 1
        project = self.project
        assert callable(project)
        return (
            {"phase": "after", "key": str(row["scan_key"])},
            project(row),
            self._index < len(self.rows) or self.has_more_after_last,
        )


def _relation_rows(
    builder: PacketBuilder,
    app_id: str,
    object_id: str,
    after: str,
    limit: int,
) -> list[sqlite3.Row]:
    return list(
        builder.connection.execute(
            f"""
            WITH {authoritative_current_reference_ctes()},
                 {authoritative_availability_event_ctes()}
            SELECT reference.id AS scan_key, reference.id AS reference_id,
                   reference.from_object_id, reference.to_object_id,
                   reference.relationship_type, reference.extraction_method,
                   reference.exact_value, reference.supporting_observation_id,
                   from_object.source AS from_source,
                   from_object.source_instance AS from_source_instance,
                   from_object.kind AS from_kind,
                   from_object.external_id AS from_external_id,
                   from_object.availability AS from_availability,
                   from_object.availability_reason AS from_availability_reason,
                   from_object.availability_observed_at AS from_availability_observed_at,
                   from_current.id AS from_current_observation_id,
                   from_event.id AS from_availability_event_id,
                   {_scope_metadata_json("from_current", "from_object")} AS from_metadata_json,
                   to_object.source AS to_source,
                   to_object.source_instance AS to_source_instance,
                   to_object.kind AS to_kind,
                   to_object.external_id AS to_external_id,
                   to_object.availability AS to_availability,
                   to_object.availability_reason AS to_availability_reason,
                   to_object.availability_observed_at AS to_availability_observed_at,
                   to_current.id AS to_current_observation_id,
                   to_event.id AS to_availability_event_id,
                   {_scope_metadata_json("to_current", "to_object")} AS to_metadata_json
            FROM authoritative_current_references reference
            JOIN source_objects from_object ON from_object.id=reference.from_object_id
            JOIN source_objects to_object ON to_object.id=reference.to_object_id
            LEFT JOIN authoritative_current_observations from_current
              ON from_current.source_object_id=from_object.id
            LEFT JOIN authoritative_current_observations to_current
              ON to_current.source_object_id=to_object.id
            LEFT JOIN authoritative_current_availability_events from_event
              ON from_event.source_object_id=from_object.id
            LEFT JOIN authoritative_current_availability_events to_event
              ON to_event.source_object_id=to_object.id
            WHERE reference.app_id=?
              AND (reference.from_object_id=? OR reference.to_object_id=?)
              AND reference.id>?
            ORDER BY reference.id
            LIMIT ?
            """,
            (app_id, object_id, object_id, after, limit),
        )
    )


def _has_relation_after(builder: PacketBuilder, app_id: str, object_id: str, after: str) -> bool:
    return bool(_relation_rows(builder, app_id, object_id, after, 1))


def _endpoint(row: sqlite3.Row, prefix: str, object_id: str) -> dict[str, object]:
    availability_row = {
        "availability": row[f"{prefix}availability"],
        "availability_reason": row[f"{prefix}availability_reason"],
        "availability_observed_at": row[f"{prefix}availability_observed_at"],
        "availability_event_id": row[f"{prefix}availability_event_id"],
    }
    return {
        "object_id": object_id,
        "current_observation_id": (
            str(row[f"{prefix}current_observation_id"])
            if row[f"{prefix}current_observation_id"] is not None
            else None
        ),
        "availability": _availability(availability_row),
    }


def _reference_is_allowed(app: AppConfig, connection: sqlite3.Connection, row: sqlite3.Row) -> bool:
    if not _object_in_scope(
        app,
        connection=connection,
        source=str(row["from_source"]),
        source_instance=str(row["from_source_instance"]),
        kind=str(row["from_kind"]),
        external_id=str(row["from_external_id"]),
        metadata_json=row["from_metadata_json"],
    ) or not _object_in_scope(
        app,
        connection=connection,
        source=str(row["to_source"]),
        source_instance=str(row["to_source_instance"]),
        kind=str(row["to_kind"]),
        external_id=str(row["to_external_id"]),
        metadata_json=row["to_metadata_json"],
    ):
        return False
    relationship_type = str(row["relationship_type"])
    if relationship_type != "mapped_commit_sha":
        # Every GitLab-to-local-Git SHA family predating the explicit mapping
        # rule is historical derived state, not an admissible current link.
        return not (row["from_source"] == "gitlab" and row["to_source"] == "git")
    try:
        from worktrace.linking.mappings import reference_mapping_allowed
    except ImportError:
        return False
    return reference_mapping_allowed(
        app,
        dict(row),
        {
            "id": row["from_object_id"],
            "app_id": app.id,
            "source": row["from_source"],
            "source_instance": row["from_source_instance"],
            "kind": row["from_kind"],
            "external_id": row["from_external_id"],
        },
        {
            "id": row["to_object_id"],
            "app_id": app.id,
            "source": row["to_source"],
            "source_instance": row["to_source_instance"],
            "kind": row["to_kind"],
            "external_id": row["to_external_id"],
        },
    )


def _relationship_interpretation(relationship_type: str) -> str:
    if relationship_type == "mapped_commit_sha":
        return "explicitly_mapped_sha_reference"
    if relationship_type.startswith("mentions_") or relationship_type in {
        "contains_explicit_url",
        "jira_links_to_issue",
        "jira_hierarchy_context",
    }:
        return "textual_mention"
    if relationship_type in {
        "mr_contains_commit",
        "commit_introduced_by_mr",
        "mr_uses_source_branch",
        "jira_subtask_of",
        "git_reverts_commit",
        "git_cherry_picks_commit",
        "deployment_contains_sha",
        "tag_points_to_commit",
        "gitlab_mr_commit",
        "gitlab_mr_discussion",
        "gitlab_mr_changed_paths",
        "jira_comment_issue",
        "jira_changelog_issue",
    }:
        return "recorded_structural_relationship"
    return "unspecified_recorded_relationship"


def scan_relations(
    builder: PacketBuilder,
    app_id: str,
    object_id: str,
    *,
    after: str | None = None,
) -> Iterator[ScannedRow]:
    """Yield up to 200 authoritative incoming/outgoing reference positions."""

    describe_object(builder, app_id, object_id)
    app = builder.config.app(app_id)
    rows = _relation_rows(builder, app_id, object_id, after or "", MAX_CONTEXT_SCAN_BUDGET)
    has_more = (
        bool(rows)
        and len(rows) == MAX_CONTEXT_SCAN_BUDGET
        and _has_relation_after(builder, app_id, object_id, str(rows[-1]["reference_id"]))
    )

    def project(row: sqlite3.Row) -> dict[str, object] | None:
        if not _reference_is_allowed(app, builder.connection, row):
            return None
        from_id, to_id = str(row["from_object_id"]), str(row["to_object_id"])
        relationship_type = str(row["relationship_type"])
        return {
            "reference_id": str(row["reference_id"]),
            "direction": "outgoing" if from_id == object_id else "incoming",
            "from_object_id": from_id,
            "to_object_id": to_id,
            "relationship_type": relationship_type,
            "relationship_interpretation": _relationship_interpretation(relationship_type),
            "extraction_method": str(row["extraction_method"]),
            "exact_value": str(row["exact_value"]) if row["exact_value"] is not None else None,
            "supporting_observation_id": str(row["supporting_observation_id"]),
            "from_endpoint": _endpoint(row, "from_", from_id),
            "to_endpoint": _endpoint(row, "to_", to_id),
            "limitations": [
                "This relationship does not itself establish personal ownership, feature "
                "identity, deployment, or impact."
            ],
        }

    return _LazyRows(tuple(rows), has_more, project)


def _decision_mentions_object(action: str, payload: Mapping[str, object], object_id: str) -> bool:
    """Use only the canonical membership fields accepted for each action."""

    if action in CREATION_ACTIONS or action in {"confirm", "merge", "split"}:
        return object_id in snapshot_member_ids(payload)
    if action in {"add_member", "remove_member", "mark_context_only"}:
        return any(
            payload.get(key) == object_id
            for key in ("member_id", "source_object_id", "evidence_object_id")
        )
    return False


def _page_context_builder(builder: PacketBuilder, app_id: str) -> PacketBuilder:
    """Reuse an already scoped metadata-only page context without changing its contract."""

    builder.config.app(app_id)
    authority_context = builder._authority_context
    if (
        builder._decision_projection is not None
        and authority_context is not None
        and authority_context.metadata_only
        and authority_context.app_id == app_id
    ):
        return builder
    return builder.page_projection_builder(app_id)


def membership_locator_ids(builder: PacketBuilder, app_id: str, object_id: str) -> set[str]:
    """Find generated and decision-backed membership locators for one object.

    This intentionally does not resolve a contribution.  Callers which need
    several objects can deduplicate aliases before paying for any projection.
    """
    result = {
        str(row["candidate_id"])
        for row in builder.connection.execute(
            """
            SELECT member.candidate_id
            FROM candidate_members member
            JOIN candidate_groups candidate ON candidate.id=member.candidate_id
            WHERE candidate.app_id=? AND member.source_object_id=?
            """,
            (app_id, object_id),
        )
    }
    context_builder = _page_context_builder(builder, app_id)
    context = context_builder._decision_projection
    assert context is not None
    for decision in context.active_decisions:
        if context.decision_scopes.get(decision.id) == app_id and _decision_mentions_object(
            decision.action, decision.payload, object_id
        ):
            result.add(decision.target_id)
    return result


def lineage_identity(
    builder: PacketBuilder, app_id: str, identifier: str
) -> tuple[str, str | None, str, bool]:
    context_builder = _page_context_builder(builder, app_id)
    context = context_builder._decision_projection
    assert context is not None
    lineage = context.resolve_lineage(identifier, app_id=app_id)
    if lineage is None:
        confirmed = any(
            decision.target_id == identifier
            and decision.action in {"confirm", "confirm_candidate"}
            and context.decision_scopes.get(decision.id) == app_id
            for decision in context.active_decisions
        )
        return identifier, None, identifier, confirmed
    creation = next(
        (
            decision
            for decision in reversed(lineage.decisions)
            if decision.action in CREATION_ACTIONS
            and isinstance(decision.payload.get("contribution_id"), str)
            and bool(decision.payload["contribution_id"])
        ),
        None,
    )
    if creation is None:
        return lineage.canonical_candidate_id, None, lineage.canonical_candidate_id, False
    contribution_id = str(creation.payload["contribution_id"])
    return contribution_id, contribution_id, lineage.canonical_candidate_id, True


@dataclass(frozen=True, slots=True)
class CanonicalMembershipLocator:
    """One canonical group reachable from an object's membership locator."""

    key: str
    identifier: str
    contribution_id: str | None
    candidate_id: str
    confirmed: bool


def canonical_membership_locators(
    builder: PacketBuilder, app_id: str, object_id: str
) -> tuple[CanonicalMembershipLocator, ...]:
    """Return deterministic, alias-deduplicated canonical membership groups."""

    context_builder = _page_context_builder(builder, app_id)
    by_key: dict[str, CanonicalMembershipLocator] = {}
    for identifier in sorted(membership_locator_ids(context_builder, app_id, object_id)):
        try:
            key, contribution_id, candidate_id, confirmed = lineage_identity(
                context_builder, app_id, identifier
            )
        except ScopeViolation:
            continue
        locator = CanonicalMembershipLocator(
            key=key,
            identifier=identifier,
            contribution_id=contribution_id,
            candidate_id=candidate_id,
            confirmed=confirmed,
        )
        existing = by_key.get(key)
        if existing is None or (locator.confirmed and not existing.confirmed):
            by_key[key] = locator
    return tuple(by_key[key] for key in sorted(by_key))


def resolve_canonical_membership(
    builder: PacketBuilder,
    app_id: str,
    object_id: str,
    locator: CanonicalMembershipLocator,
    *,
    legacy_generated: bool,
) -> dict[str, object] | None:
    """Resolve one group to an effective membership, if it remains applicable.

    One invocation is one canonical projection attempt.  It deliberately
    returns ``None`` for a disappeared, ignored, unsupported, or nonmatching
    locator so callers can account for that work without inventing a link.
    """

    contribution = resolve_canonical_group(
        builder, app_id, locator, legacy_generated=legacy_generated
    )
    if contribution is None:
        return None
    return project_canonical_membership(builder, app_id, object_id, locator, contribution)


def resolve_canonical_group(
    builder: PacketBuilder,
    app_id: str,
    locator: CanonicalMembershipLocator,
    *,
    legacy_generated: bool,
) -> ContributionView | None:
    """Resolve one canonical group once, independent of the object being displayed."""

    if legacy_generated and not locator.confirmed:
        return None
    context_builder = _page_context_builder(builder, app_id)
    try:
        return context_builder.resolve_contribution(locator.identifier)
    except (NotFound, ScopeViolation):
        return None


def project_canonical_membership(
    builder: PacketBuilder,
    app_id: str,
    object_id: str,
    locator: CanonicalMembershipLocator,
    contribution: ContributionView,
) -> dict[str, object] | None:
    """Apply one already-resolved group to one evidence object without resolving again."""

    context_builder = _page_context_builder(builder, app_id)
    object_description = describe_object(context_builder, app_id, object_id)
    object_has_current = object_description["current_observation_id"] is not None
    role = (
        "material"
        if object_id in contribution.member_ids
        else "context"
        if object_id in contribution.context_ids
        else None
    )
    if role is None:
        return None
    citations = sorted(contribution.decision_evidence_ids) if locator.confirmed else []
    if not citations and object_has_current:
        citations = [str(object_description["current_observation_id"])]
    raw_limitations = object_description["limitations"]
    limitations = list(raw_limitations) if isinstance(raw_limitations, list) else []
    generation = _active_generation(builder.connection, app_id)
    legacy_generated = generation is not None and generation.generator_version != GENERATOR_VERSION
    if legacy_generated and locator.confirmed:
        limitations.append(
            "Generated locator coverage is legacy; this row persists because "
            "a human-confirmed decision lineage remains effective."
        )
    return {
        "object_id": object_id,
        "candidate_id": locator.candidate_id,
        "contribution_id": locator.contribution_id,
        "role": role,
        "basis": "confirmed" if locator.confirmed else "suggestion",
        "status": "confirmed" if locator.confirmed else "suggestion",
        "evidence_state": (
            "authoritative_current" if object_has_current else "current_evidence_unavailable"
        ),
        "citations": citations[:20],
        "citations_truncated": len(citations) > 20,
        "limitations": limitations,
    }


def scan_memberships(
    builder: PacketBuilder,
    app_id: str,
    object_id: str,
    *,
    after: str | None = None,
) -> tuple[str | None, Iterator[ScannedRow]]:
    """Yield canonical effective membership groups, deduplicated before paging."""

    describe_object(builder, app_id, object_id)
    generation = _active_generation(builder.connection, app_id)
    generation_token = generation.token if generation is not None else None
    legacy_generated = generation is not None and generation.generator_version != GENERATOR_VERSION
    context_builder = builder.page_projection_builder(app_id)
    locators = canonical_membership_locators(context_builder, app_id, object_id)
    entries = [locator for locator in locators if locator.key > (after or "")][
        :MAX_CONTEXT_SCAN_BUDGET
    ]
    has_more = len([locator for locator in locators if locator.key > (after or "")]) > len(entries)

    class _MembershipRows(Iterator[ScannedRow]):
        def __init__(self) -> None:
            self.index = 0

        def __next__(self) -> ScannedRow:
            if self.index >= len(entries):
                raise StopIteration
            locator = entries[self.index]
            self.index += 1
            item = resolve_canonical_membership(
                context_builder,
                app_id,
                object_id,
                locator,
                legacy_generated=legacy_generated,
            )
            return (
                {"phase": "after", "key": locator.key},
                item,
                self.index < len(entries) or has_more,
            )

    return generation_token, _MembershipRows()
