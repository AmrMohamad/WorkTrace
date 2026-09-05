from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from worktrace.db.read_state import mark_read_states_changed
from worktrace.errors import NotFound, ScopeViolation
from worktrace.normalize.redaction import Redactor


@dataclass(frozen=True)
class Decision:
    id: str
    action: str
    target_id: str
    payload: dict[str, object]
    created_at: str
    undo_target_id: str | None = None


@dataclass(frozen=True)
class DecisionLineage:
    """One app-owned connected component in the immutable decision graph."""

    app_id: str
    candidate_ids: frozenset[str]
    contribution_ids: frozenset[str]
    canonical_candidate_id: str
    decisions: tuple[Decision, ...]


@dataclass(frozen=True, slots=True)
class _DecisionProjectionContext:
    """One read-snapshot projection shared by every candidate on a bounded page."""

    active_decisions: tuple[Decision, ...]
    decisions_by_target: Mapping[str, tuple[Decision, ...]]
    decision_scopes: Mapping[str, str | None]
    lineages: tuple[DecisionLineage, ...]
    lineages_by_identifier: Mapping[str, tuple[DecisionLineage, ...]]

    def for_target(self, target_id: str) -> tuple[Decision, ...]:
        return self.decisions_by_target.get(target_id, ())

    def resolve_lineage(
        self,
        identifier: str,
        *,
        app_id: str | None = None,
    ) -> DecisionLineage | None:
        matches = [
            lineage
            for lineage in self.lineages_by_identifier.get(identifier, ())
            if app_id is None or lineage.app_id == app_id
        ]
        matched_apps = {lineage.app_id for lineage in matches}
        if len(matched_apps) > 1:
            raise ScopeViolation("contribution identifier belongs to more than one configured app")
        if not matches:
            return None
        if len(matches) > 1:
            raise ScopeViolation("contribution identifier has ambiguous decision lineage")
        return matches[0]


VALID_ACTIONS = {
    "confirm",
    "merge",
    "split",
    "ignore",
    "rename",
    "add_member",
    "remove_member",
    "attest",
    "manual_evidence",
    "undo",
    "confirm_candidate",
    "merge_contributions",
    "split_contribution",
    "ignore_candidate",
    "rename_contribution",
    "set_contribution_type",
    "attest_claim",
    "undo_decision",
}

CREATION_ACTIONS = frozenset({"confirm_candidate", "merge_contributions", "split_contribution"})
UNDO_ACTIONS = frozenset({"undo", "undo_decision"})


def snapshot_member_ids(payload: Mapping[str, object]) -> set[str]:
    """Return every source-object identifier carried by a decision snapshot."""

    result: set[str] = set()
    for key in ("members", "context_members", "keep_source_object_ids"):
        values = payload.get(key)
        if isinstance(values, list):
            result.update(str(value) for value in values if isinstance(value, str) and value)
    return result


def _valid_compensation_map(rows: list[sqlite3.Row]) -> dict[str, str]:
    """Map each compensated decision to the first structurally valid undo event."""

    rows_by_id = {str(row["id"]): row for row in rows}
    result: dict[str, str] = {}
    for row in rows:
        if str(row["action"]) not in UNDO_ACTIONS or not row["undo_target_id"]:
            continue
        target_decision_id = str(row["undo_target_id"])
        target = rows_by_id.get(target_decision_id)
        if target is None or str(target["action"]) in UNDO_ACTIONS:
            continue
        if str(row["target_id"]) != str(target["target_id"]):
            continue
        try:
            parsed = json.loads(str(row["payload_json"]))
        except (TypeError, json.JSONDecodeError):
            parsed = {}
        compensates = parsed.get("compensates") if isinstance(parsed, dict) else None
        if compensates is not None and compensates != target_decision_id:
            continue
        result.setdefault(target_decision_id, str(row["id"]))
    return result


def compensating_decision_ids(
    connection: sqlite3.Connection,
    decision_id: str,
) -> tuple[str, ...]:
    """Return the valid compensation event for one decision, if present."""

    rows = list(connection.execute("SELECT * FROM human_decisions ORDER BY created_at, id"))
    compensation_id = _valid_compensation_map(rows).get(decision_id)
    return (compensation_id,) if compensation_id is not None else ()


def creation_decision_scope_app(
    connection: sqlite3.Connection,
    target_id: str,
    payload: Mapping[str, object],
) -> str | None:
    """Resolve a creation snapshot to one app without trusting its payload alone."""

    candidate = connection.execute(
        "SELECT app_id FROM candidate_groups WHERE id=?", (target_id,)
    ).fetchone()
    candidate_app = str(candidate[0]) if candidate is not None else None
    member_ids = sorted(snapshot_member_ids(payload))
    related_apps: set[str] = set()
    if member_ids:
        placeholders = ",".join("?" for _ in member_ids)
        related_apps.update(
            str(row[0])
            for row in connection.execute(
                f"SELECT DISTINCT app_id FROM source_objects WHERE id IN ({placeholders})",
                member_ids,
            )
        )
    raw_candidate_ids = payload.get("candidate_ids")
    candidate_ids = (
        sorted(str(value) for value in raw_candidate_ids if isinstance(value, str) and value)
        if isinstance(raw_candidate_ids, list)
        else []
    )
    if candidate_ids:
        placeholders = ",".join("?" for _ in candidate_ids)
        related_apps.update(
            str(row[0])
            for row in connection.execute(
                f"SELECT DISTINCT app_id FROM candidate_groups WHERE id IN ({placeholders})",
                candidate_ids,
            )
        )
    payload_app = payload.get("app_id")
    declared_app = payload_app if isinstance(payload_app, str) and payload_app else None
    if candidate_app is not None:
        expected_app = candidate_app
    elif len(related_apps) == 1:
        expected_app = next(iter(related_apps))
    else:
        return None
    if declared_app is not None and declared_app != expected_app:
        return None
    if any(app_id != expected_app for app_id in related_apps):
        return None
    return expected_app


def append_decision(
    connection: sqlite3.Connection,
    action: str,
    target_id: str,
    payload: Mapping[str, object] | None = None,
    *,
    actor_label: str = "local-user",
    undo_target_id: str | None = None,
    redactor: Redactor | None = None,
) -> str:
    if action not in VALID_ACTIONS:
        raise ValueError(f"unsupported decision action: {action}")
    is_undo = action in UNDO_ACTIONS
    if is_undo and undo_target_id is None:
        raise ScopeViolation("undo decisions require an undo target")
    if not is_undo and undo_target_id is not None:
        raise ScopeViolation("only undo decisions may carry an undo target")
    if undo_target_id is not None:
        undo_target = connection.execute(
            "SELECT action, target_id FROM human_decisions WHERE id=?", (undo_target_id,)
        ).fetchone()
        if undo_target is None:
            raise NotFound(f"decision not found: {undo_target_id}")
        if str(undo_target["action"]) in UNDO_ACTIONS:
            raise ScopeViolation("undo decisions cannot themselves be undone")
        if str(undo_target["target_id"]) != target_id:
            raise ScopeViolation("undo decision target does not match the compensated decision")
        decision_rows = list(
            connection.execute("SELECT * FROM human_decisions ORDER BY created_at, id")
        )
        if undo_target_id in _valid_compensation_map(decision_rows):
            raise ScopeViolation("decision has already been undone")
        if decision_scope_map(connection).get(undo_target_id) is None:
            raise ScopeViolation("decision has no unambiguous configured application scope")
    stored_payload: dict[str, object] = dict(payload or {})
    if is_undo:
        compensates = stored_payload.get("compensates")
        if compensates is not None and compensates != undo_target_id:
            raise ScopeViolation("undo payload does not match the compensated decision")
    if redactor is not None:
        redacted = redactor.redact_payload(stored_payload)
        if not isinstance(redacted, dict):
            raise ValueError("decision payload must be an object")
        stored_payload = dict(redacted)
    else:
        validation_redactor = Redactor(b"worktrace-validation-only")
        validated = validation_redactor.redact_payload(stored_payload)
        if validated != stored_payload:
            raise ValueError("redaction is required before decision persistence")
    decision_id = f"decision:{uuid.uuid4()}"
    # WorkTrace's normal writer connection uses autocommit=False, which starts
    # its next transaction immediately after a commit. An explicit BEGIN on an
    # autocommit=True connection is therefore the only caller-owned form here.
    owns_transaction = connection.autocommit is False or not connection.in_transaction
    if owns_transaction and connection.autocommit is True:
        connection.execute("BEGIN")
    try:
        connection.execute(
            """
            INSERT INTO human_decisions(
                id, action, target_id, payload_json, actor_label, created_at, undo_target_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                action,
                target_id,
                json.dumps(stored_payload, sort_keys=True),
                actor_label,
                datetime.now(UTC).isoformat(),
                undo_target_id,
            ),
        )
        affected_app = decision_scope_map(connection).get(decision_id)
        if affected_app is not None:
            mark_read_states_changed(connection, [affected_app])
    except BaseException:
        if owns_transaction and connection.in_transaction:
            connection.rollback()
        raise
    else:
        if owns_transaction and connection.in_transaction:
            connection.commit()
    return decision_id


def undo_decision(connection: sqlite3.Connection, decision_id: str) -> str:
    row = connection.execute(
        "SELECT action, target_id FROM human_decisions WHERE id=?", (decision_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"decision not found: {decision_id}")
    if str(row["action"]) in UNDO_ACTIONS:
        raise ScopeViolation("undo decisions cannot themselves be undone")
    return append_decision(
        connection,
        "undo_decision",
        str(row["target_id"]),
        {"compensates": decision_id},
        undo_target_id=decision_id,
    )


def active_decisions(
    connection: sqlite3.Connection,
    target_id: str,
    *,
    context: _DecisionProjectionContext | None = None,
) -> list[Decision]:
    if context is not None:
        return list(context.for_target(target_id))
    return [
        decision
        for decision in decision_stream(connection, active_only=True)
        if decision.target_id == target_id
    ]


def decision_stream(
    connection: sqlite3.Connection,
    *,
    active_only: bool = False,
) -> list[Decision]:
    """Return immutable decisions in projection order, optionally without compensated rows."""

    rows = list(connection.execute("SELECT * FROM human_decisions ORDER BY created_at, id"))
    canceled = set(_valid_compensation_map(rows))
    result: list[Decision] = []
    for row in rows:
        action = str(row["action"])
        decision_id = str(row["id"])
        if active_only and (decision_id in canceled or action in UNDO_ACTIONS):
            continue
        try:
            parsed = json.loads(str(row["payload_json"]))
        except (TypeError, json.JSONDecodeError):
            parsed = {}
        result.append(
            Decision(
                id=decision_id,
                action=action,
                target_id=str(row["target_id"]),
                payload=dict(parsed) if isinstance(parsed, dict) else {},
                created_at=str(row["created_at"]),
                undo_target_id=(str(row["undo_target_id"]) if row["undo_target_id"] else None),
            )
        )
    return result


def _declared_app(payload: Mapping[str, object]) -> str | None:
    value = payload.get("app_id")
    return value if isinstance(value, str) and value else None


def _undo_ancestry_target(
    decision: Decision,
    decisions_by_id: Mapping[str, Decision],
) -> Decision | None:
    """Return a structurally valid historical undo edge, without activating it."""

    target_id = decision.undo_target_id
    target = decisions_by_id.get(target_id) if target_id is not None else None
    if target is None or target.target_id != decision.target_id:
        return None
    compensates = decision.payload.get("compensates")
    if compensates is not None and compensates != target_id:
        return None
    return target


def _target_apps(
    connection: sqlite3.Connection,
    target_id: str,
    node_apps: Mapping[str, set[str]],
) -> set[str]:
    result = set(node_apps.get(target_id, set()))
    result.update(
        str(row[0])
        for row in connection.execute(
            """
            SELECT app_id FROM candidate_groups WHERE id=?
            UNION SELECT app_id FROM source_objects WHERE id=?
            UNION SELECT object.app_id FROM observations observation
                  JOIN source_objects object ON object.id=observation.source_object_id
                  WHERE observation.id=?
            """,
            (target_id, target_id, target_id),
        )
    )
    return result


def scoped_decision_app(
    connection: sqlite3.Connection,
    decision: Decision,
    *,
    node_apps: Mapping[str, set[str]] | None = None,
) -> str | None:
    """Resolve a decision to exactly one app without trusting a bare target string."""

    if decision.action in CREATION_ACTIONS or decision.action in {"confirm", "merge", "split"}:
        return creation_decision_scope_app(
            connection,
            decision.target_id,
            decision.payload,
        )
    target_apps = _target_apps(connection, decision.target_id, node_apps or {})
    declared = _declared_app(decision.payload)
    if declared is not None:
        if declared not in target_apps:
            return None
        target_apps = {declared}
    if decision.action in {"add_member", "remove_member", "mark_context_only"}:
        source_object_id = decision.payload.get("source_object_id")
        if not isinstance(source_object_id, str) or not source_object_id:
            return None
        member = connection.execute(
            "SELECT app_id FROM source_objects WHERE id=?", (source_object_id,)
        ).fetchone()
        if member is None:
            return None
        member_app = str(member[0])
        target_apps &= {member_app}
    return next(iter(target_apps)) if len(target_apps) == 1 else None


def decision_node_apps(
    connection: sqlite3.Connection,
    *,
    active_only: bool = False,
) -> dict[str, set[str]]:
    """Map every declared candidate/contribution lineage identifier to owning apps."""

    return _decision_node_apps(
        connection,
        decision_stream(connection, active_only=active_only),
    )


def _decision_node_apps(
    connection: sqlite3.Connection,
    decisions: Sequence[Decision],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for decision in decisions:
        if decision.action not in CREATION_ACTIONS:
            continue
        app_id = creation_decision_scope_app(
            connection,
            decision.target_id,
            decision.payload,
        )
        contribution_id = decision.payload.get("contribution_id")
        if app_id is None or not isinstance(contribution_id, str) or not contribution_id:
            continue
        identifiers = {decision.target_id, contribution_id}
        raw_candidates = decision.payload.get("candidate_ids")
        if isinstance(raw_candidates, list):
            identifiers.update(
                str(value) for value in raw_candidates if isinstance(value, str) and value
            )
        for identifier in identifiers:
            result.setdefault(identifier, set()).add(app_id)
    return result


def decision_scope_map(
    connection: sqlite3.Connection,
    *,
    active_only: bool = False,
) -> dict[str, str | None]:
    """Resolve every decision to one owning app, including its undo ancestry.

    Historical ledgers may contain undo-of-undo rows that current write paths reject. Their
    timestamps are not a trustworthy graph order, so resolve those rows by stable decision ID
    ancestry while keeping malformed, cyclic, or cross-target chains unscoped.
    """

    decisions = decision_stream(connection, active_only=active_only)
    node_apps = _decision_node_apps(connection, decisions)
    return _decision_scope_map(connection, decisions, node_apps)


def _decision_scope_map(
    connection: sqlite3.Connection,
    decisions: Sequence[Decision],
    node_apps: Mapping[str, set[str]],
) -> dict[str, str | None]:
    decisions_by_id = {decision.id: decision for decision in decisions}
    result: dict[str, str | None] = {}
    for decision in decisions:
        if decision.action in UNDO_ACTIONS:
            continue
        result[decision.id] = scoped_decision_app(
            connection,
            decision,
            node_apps=node_apps,
        )
    for decision in decisions:
        if decision.action not in UNDO_ACTIONS or decision.id in result:
            continue
        current_id = decision.id
        trail: list[Decision] = []
        seen: set[str] = set()
        while current_id not in result:
            if current_id in seen:
                break
            seen.add(current_id)
            current = decisions_by_id.get(current_id)
            if current is None or current.action not in UNDO_ACTIONS:
                break
            trail.append(current)
            target = _undo_ancestry_target(current, decisions_by_id)
            if target is None:
                break
            current_id = target.id
        else:
            resolved_app = result[current_id]
            for item in reversed(trail):
                declared_app = _declared_app(item.payload)
                if declared_app is not None and declared_app != resolved_app:
                    resolved_app = None
                result[item.id] = resolved_app
            continue
        for item in trail:
            result[item.id] = None
    return result


def decision_lineages(
    connection: sqlite3.Connection,
    *,
    active_only: bool = True,
) -> tuple[DecisionLineage, ...]:
    """Build app-scoped lineage components for the selected decision projection."""

    decisions = decision_stream(connection, active_only=active_only)
    node_apps = _decision_node_apps(connection, decisions)
    decision_scopes = _decision_scope_map(connection, decisions, node_apps)
    return _decision_lineages(connection, decisions, decision_scopes)


def _decision_lineages(
    connection: sqlite3.Connection,
    decisions: Sequence[Decision],
    decision_scopes: Mapping[str, str | None],
) -> tuple[DecisionLineage, ...]:
    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = {}
    node_kind: dict[tuple[str, str], str] = {}
    for decision in decisions:
        if decision.action not in CREATION_ACTIONS:
            continue
        app_id = creation_decision_scope_app(
            connection,
            decision.target_id,
            decision.payload,
        )
        contribution_id = decision.payload.get("contribution_id")
        if app_id is None or not isinstance(contribution_id, str) or not contribution_id:
            continue
        candidates = {decision.target_id}
        raw_candidates = decision.payload.get("candidate_ids")
        if isinstance(raw_candidates, list):
            candidates.update(
                str(value) for value in raw_candidates if isinstance(value, str) and value
            )
        contribution_node = (app_id, contribution_id)
        node_kind[contribution_node] = "contribution"
        adjacency.setdefault(contribution_node, set())
        for candidate_id in candidates:
            candidate_node = (app_id, candidate_id)
            node_kind[candidate_node] = "candidate"
            adjacency.setdefault(candidate_node, set()).add(contribution_node)
            adjacency[contribution_node].add(candidate_node)
        candidate_nodes = [(app_id, candidate_id) for candidate_id in candidates]
        for candidate_node in candidate_nodes:
            adjacency[candidate_node].update(
                other for other in candidate_nodes if other != candidate_node
            )

    result: list[DecisionLineage] = []
    visited: set[tuple[str, str]] = set()
    for start in sorted(adjacency):
        if start in visited:
            continue
        stack = [start]
        component: set[tuple[str, str]] = set()
        while stack:
            node = stack.pop()
            if node in component:
                continue
            component.add(node)
            stack.extend(adjacency.get(node, ()))
        visited.update(component)
        app_id = start[0]
        component_ids = {node_id for _, node_id in component}
        component_candidate_ids = frozenset(
            node_id for node, node_id in component if node_kind[node, node_id] == "candidate"
        )
        component_contribution_ids = frozenset(component_ids - set(component_candidate_ids))
        scoped: list[Decision] = []
        creations: list[Decision] = []
        for decision in decisions:
            if decision.target_id not in component_ids:
                continue
            if decision_scopes.get(decision.id) != app_id:
                continue
            scoped.append(decision)
            if decision.action in CREATION_ACTIONS:
                creations.append(decision)
        if not creations:
            continue
        result.append(
            DecisionLineage(
                app_id=app_id,
                candidate_ids=component_candidate_ids,
                contribution_ids=component_contribution_ids,
                canonical_candidate_id=creations[-1].target_id,
                decisions=tuple(scoped),
            )
        )
    return tuple(result)


def _build_decision_projection_context(
    connection: sqlite3.Connection,
) -> _DecisionProjectionContext:
    decisions = tuple(decision_stream(connection, active_only=True))
    by_target: dict[str, list[Decision]] = {}
    for decision in decisions:
        by_target.setdefault(decision.target_id, []).append(decision)
    node_apps = _decision_node_apps(connection, decisions)
    scopes = _decision_scope_map(connection, decisions, node_apps)
    lineages = _decision_lineages(connection, decisions, scopes)
    by_identifier: dict[str, list[DecisionLineage]] = {}
    for lineage in lineages:
        for identifier in lineage.candidate_ids | lineage.contribution_ids:
            by_identifier.setdefault(identifier, []).append(lineage)
    return _DecisionProjectionContext(
        active_decisions=decisions,
        decisions_by_target=MappingProxyType(
            {key: tuple(value) for key, value in by_target.items()}
        ),
        decision_scopes=MappingProxyType(dict(scopes)),
        lineages=lineages,
        lineages_by_identifier=MappingProxyType(
            {key: tuple(value) for key, value in by_identifier.items()}
        ),
    )


def resolve_decision_lineage(
    connection: sqlite3.Connection,
    identifier: str,
    *,
    app_id: str | None = None,
) -> DecisionLineage | None:
    matches = [
        lineage
        for lineage in decision_lineages(connection)
        if identifier in lineage.candidate_ids or identifier in lineage.contribution_ids
        if app_id is None or lineage.app_id == app_id
    ]
    matched_apps = {lineage.app_id for lineage in matches}
    if len(matched_apps) > 1:
        raise ScopeViolation("contribution identifier belongs to more than one configured app")
    if not matches:
        return None
    if len(matches) > 1:
        raise ScopeViolation("contribution identifier has ambiguous decision lineage")
    return matches[0]
