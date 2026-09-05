"""Auditable exact-key roots and conservative selector replacement proposals."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from worktrace.candidates.builder import _self_roles
from worktrace.candidates.decisions import decision_lineages
from worktrace.candidates.projector import project_candidate
from worktrace.config import AppConfig, WorkTraceConfig
from worktrace.db.authority import parse_scope, run_is_authoritative
from worktrace.db.repository import EvidenceRepository
from worktrace.domain.models import JsonValue
from worktrace.errors import NotFound, ScopeViolation
from worktrace.linking.extractors import extract_jira_keys
from worktrace.packets.builder import PacketBuilder

JIRA_SELECTOR_VERSION = 3
_PERSONAL_ROLES = frozenset(
    {
        "git_author",
        "git_coauthor",
        "git_committer",
        "git_reviewer",
        "git_tag_author",
        "mr_author",
        "mr_assignee",
        "mr_reviewer",
        "mr_merger",
        "gitlab_commit_author",
        "gitlab_commit_coauthor",
        "gitlab_commit_committer",
        "gitlab_commit_reviewer",
        "gitlab_discussion_author",
        "gitlab_deployer",
        "gitlab_release_author",
    }
)
_CONTEXT_RELATIONS = frozenset(
    {
        "gitlab_mr_commit",
        "mr_contains_commit",
        "commit_introduced_by_mr",
        "gitlab_mr_discussion",
        "gitlab_mr_changed_paths",
        "mr_uses_source_branch",
        "git_ref_target",
        "tag_points_to_commit",
        "deployment_contains_sha",
    }
)


@dataclass(frozen=True)
class JiraKeySeed:
    key: str
    supporting_observation_ids: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class JiraSeedSelection:
    seeds: tuple[JiraKeySeed, ...]
    discovered_count: int
    policy: str = "personal_roots_v3_with_bounded_structural_context"

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(seed.key for seed in self.seeds)

    def as_dict(self) -> dict[str, JsonValue]:
        return {
            "policy": self.policy,
            "policy_version": JIRA_SELECTOR_VERSION,
            "discovered_count": self.discovered_count,
            "selected_count": len(self.seeds),
            "omitted_count": self.discovered_count - len(self.seeds),
            "omission_reason": "outside_personal_roots"
            if self.discovered_count > len(self.seeds)
            else None,
            "exhaustive_for_selected_roots": True,
            "seeds": [
                {
                    "key": seed.key,
                    "supporting_observation_ids": list(seed.supporting_observation_ids),
                    "reasons": list(seed.reasons),
                }
                for seed in self.seeds
            ],
            "limitations": ["Provider search may omit deleted or inaccessible issues."],
        }


def confirmed_memberships(
    connection: sqlite3.Connection, configuration: WorkTraceConfig, app_id: str
) -> dict[str, set[str]]:
    """Use canonical decisions, including lineages whose suggestion disappeared."""
    result: dict[str, set[str]] = {}
    builder = PacketBuilder(connection, configuration)
    for row in connection.execute("SELECT id FROM candidate_groups WHERE app_id=?", (app_id,)):
        try:
            view = project_candidate(connection, str(row[0]))
        except NotFound:
            continue
        if view.status == "confirmed":
            resolved = builder._resolve_contribution(view.id)
            result[view.id] = resolved.member_ids | resolved.context_ids
    for lineage in decision_lineages(connection):
        if lineage.app_id != app_id:
            continue
        try:
            resolved = builder._resolve_contribution(lineage.canonical_candidate_id)
        except NotFound:
            continue
        result[lineage.canonical_candidate_id] = resolved.member_ids | resolved.context_ids
    return result


def select_jira_seeds(
    repository: EvidenceRepository,
    app: AppConfig,
    *,
    configuration: WorkTraceConfig,
    explicit_keys: tuple[str, ...] = (),
) -> JiraSeedSelection:
    explicit = {key.strip().upper() for key in explicit_keys}
    if any(
        re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key) is None or not app.allows_jira_key(key)
        for key in explicit
    ):
        raise ScopeViolation("Explicit Jira keys must be exact keys in configured projects")
    rows = {str(row["source_object_id"]): row for row in repository.current_observations(app.id)}
    confirmed = confirmed_memberships(repository.connection, configuration, app.id)
    historical: set[str] = set()
    for members in confirmed.values():
        for member in members - rows.keys():
            for row in repository.connection.execute(
                "SELECT o.*, so.source, so.source_instance, so.kind, so.external_id, "
                "r.status AS run_status, r.completeness AS run_completeness, r.scope_json "
                "FROM observations o JOIN source_objects so ON so.id=o.source_object_id "
                "JOIN sync_runs r ON r.id=o.sync_run_id WHERE so.id=? AND so.app_id=? "
                "ORDER BY o.fetched_at DESC, o.id DESC",
                (member, app.id),
            ):
                if run_is_authoritative(
                    str(row["source"]),
                    str(row["run_status"]),
                    str(row["run_completeness"]),
                    parse_scope(row["scope_json"]),
                ):
                    rows[member] = row
                    historical.add(member)
                    break
    data = {identifier: json.loads(str(row["data_json"])) for identifier, row in rows.items()}
    roots: dict[str, set[str]] = {}
    for identifier, roles in _self_roles(repository.connection, app.id).items():
        accepted = roles & _PERSONAL_ROLES
        if accepted and identifier in rows:
            roots[identifier] = {"self_role:" + role for role in accepted}
    for group, members in confirmed.items():
        for member in members & rows.keys():
            roots.setdefault(member, set()).add("confirmed_contribution:" + group)
            if member in historical:
                roots[member].add("historical_confirmed_observation_not_current")

    # Two hops reach a review's MR and its collaborator records. Git parents are
    # deliberately absent: sharing repository ancestry is not personal scope.
    by_external: dict[tuple[str, str], set[str]] = defaultdict(set)
    for identifier, row in rows.items():
        by_external[str(row["source"]), str(row["external_id"])].add(identifier)
    edges: dict[str, set[str]] = defaultdict(set)
    pending_by_object: dict[str, list[dict[str, object]]] = {}
    for identifier, payload in data.items():
        pending = payload.get("_pending_references", []) if isinstance(payload, dict) else []
        pending_by_object[identifier] = (
            [p for p in pending if isinstance(p, dict)] if isinstance(pending, list) else []
        )
        for ref in pending_by_object[identifier]:
            if ref.get("relationship_type") not in _CONTEXT_RELATIONS:
                continue
            for target in by_external.get(
                (str(ref.get("target_source")), str(ref.get("target_external_id"))), ()
            ):
                # Same-source structural references cannot cross source instances.
                if (
                    rows[target]["source"] == rows[identifier]["source"]
                    and rows[target]["source_instance"] != rows[identifier]["source_instance"]
                ):
                    continue
                edges[identifier].add(target)
                edges[target].add(identifier)
    selected = {identifier: set(reasons) for identifier, reasons in roots.items()}
    frontier = set(roots)
    for _ in range(2):
        following: set[str] = set()
        for identifier in frontier:
            for target in edges.get(identifier, ()):
                if target not in selected:
                    following.add(target)
                    selected[target] = {"related_collaborator_context"}
        frontier = following
    all_keys = set(explicit)
    support: dict[str, set[str]] = defaultdict(set)
    reasons: dict[str, set[str]] = defaultdict(set)
    for key in explicit:
        reasons[key].add("explicit_user_key")
    for identifier, row in rows.items():
        keys = set(extract_jira_keys(f"{row['title'] or ''}\n{row['body_text'] or ''}", app))
        for ref in pending_by_object[identifier]:
            key = str(ref.get("target_external_id", "")).upper()
            if (
                ref.get("target_source") == "jira"
                and re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key)
                and app.allows_jira_key(key)
            ):
                keys.add(key)
        all_keys.update(keys)
        if identifier not in selected:
            continue
        for key in keys:
            support[key].add(str(row["id"]))
            reasons[key].update(selected[identifier])
            if row["kind"] == "git_branch":
                reasons[key].add("branch_reference_context_not_ownership")
    return JiraSeedSelection(
        tuple(
            JiraKeySeed(key, tuple(sorted(support[key])), tuple(sorted(why)))
            for key, why in sorted(reasons.items())
        ),
        len(all_keys),
    )


def selector_replacement_proposal(
    repository: EvidenceRepository,
    configuration: WorkTraceConfig,
    app_id: str,
    source_instance: str,
    run_id: str,
) -> dict[str, JsonValue] | None:
    current = {
        str(row["source_object_id"]): str(row["id"])
        for row in repository.current_observations(app_id)
        if row["source"] == "jira" and row["source_instance"] == source_instance
    }
    imported = {
        str(row[0])
        for row in repository.connection.execute(
            "SELECT source_object_id FROM observations WHERE sync_run_id=?", (run_id,)
        )
    }
    removed = sorted(current.keys() - imported)
    if not removed:
        return None
    confirmed = confirmed_memberships(repository.connection, configuration, app_id)
    affected = sorted(
        group for group, members in confirmed.items() if members.intersection(removed)
    )
    material = {
        "app_id": app_id,
        "source_instance": source_instance,
        "policy_version": JIRA_SELECTOR_VERSION,
        "previous_observations": current,
        "new_object_ids": sorted(imported),
        "removed_object_ids": list[JsonValue](removed),
        "confirmed_memberships": {
            group: sorted(members) for group, members in sorted(confirmed.items())
        },
        "date_from": configuration.employment_from.isoformat(),
        "date_to": configuration.employment_to.isoformat(),
        "work_timezone": configuration.employment_timezone,
    }
    token = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    return {
        "proposal_token": token,
        "removed_object_ids": list[JsonValue](removed),
        "removed_count": len(removed),
        "affected_confirmed_contributions": list[JsonValue](affected),
        "policy_version": JIRA_SELECTOR_VERSION,
        "requires_approval": True,
        "notice": "Previous authority retained. Review removals and repeat the full-range "
        "import with this proposal token.",
    }
