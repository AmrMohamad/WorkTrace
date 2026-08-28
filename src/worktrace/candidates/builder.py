from __future__ import annotations

import json
import sqlite3
from collections import deque
from datetime import UTC, datetime

from worktrace.db.authority import (
    authoritative_current_participation_ctes,
    authoritative_current_reference_ctes,
)
from worktrace.db.repository import EvidenceRepository, stable_id
from worktrace.participation import (
    ParticipationCategory,
    canonical_role,
    categories_for_evidence,
    supports_category,
)

GENERATOR_VERSION = "2"
MAX_DEPTH = 3
MAX_MEMBERS = 200
MAX_CONTEXT = 50

STRUCTURAL = {
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
}
CONTEXT_ONLY = {"mentions_jira_key", "contains_explicit_url", "mentions_commit_sha", "mentions_mr"}
CONTEXT_ONLY.update({"jira_links_to_issue", "jira_hierarchy_context"})
SEED_PRIORITY = {"jira_issue": 0, "gitlab_mr": 1, "git_commit": 2, "manual_evidence": 3}


def _self_roles(
    connection: sqlite3.Connection,
    app_id: str,
) -> dict[str, set[str]]:
    rows = connection.execute(
        f"""
        WITH {authoritative_current_participation_ctes()}
        SELECT p.source_object_id, p.observation_id, p.role, so.source, so.kind
        FROM authoritative_current_participations p
        JOIN actors a ON a.id=p.actor_id
        JOIN source_objects so ON so.id=p.source_object_id
        WHERE so.app_id=? AND a.is_self=1
        """,
        (app_id,),
    )
    result: dict[str, set[str]] = {}
    for row in rows:
        result.setdefault(str(row["source_object_id"]), set()).add(
            canonical_role(str(row["source"]), str(row["kind"]), str(row["role"]))
        )
    return result


def suggest_contribution_type(kind: str, title: str) -> str:
    lowered = title.lower()
    if "bug" in lowered or "fix" in lowered or "crash" in lowered:
        return "bug_fix"
    if "hotfix" in lowered or "production" in lowered:
        return "hotfix"
    if "migrat" in lowered or "upgrade" in lowered:
        return "migration"
    if "metric" in lowered or "observ" in lowered or "logging" in lowered:
        return "observability"
    if kind == "manual_evidence":
        return "unknown"
    if kind == "jira_issue" or kind == "gitlab_mr":
        return "feature"
    return "unknown"


def _has_complete_changed_paths(row: sqlite3.Row) -> bool:
    if str(row["completeness"]) not in {"complete", "complete_for_scope"}:
        return False
    raw_data = json.loads(str(row["data_json"]))
    data = raw_data if isinstance(raw_data, dict) else {}
    paths = data.get("changed_paths")
    return (
        isinstance(paths, list)
        and bool(paths)
        and data.get("overflow") is not True
        and data.get("scope_complete") is not False
    )


def rebuild_candidates(app_id: str, repository: EvidenceRepository) -> int:
    connection = repository.connection
    current = repository.current_observations(app_id)
    objects = {str(row["source_object_id"]): row for row in current}
    references = list(
        connection.execute(
            f"""
            WITH {authoritative_current_reference_ctes()}
            SELECT * FROM authoritative_current_references
            WHERE app_id=? ORDER BY id
            """,
            (app_id,),
        )
    )
    mr_with_changed_paths: set[str] = set()
    for relation in references:
        if str(relation["relationship_type"]) != "gitlab_mr_changed_paths":
            continue
        left = str(relation["from_object_id"])
        right = str(relation["to_object_id"])
        left_kind = str(objects[left]["kind"]) if left in objects else ""
        right_kind = str(objects[right]["kind"]) if right in objects else ""
        if (
            left_kind == "gitlab_mr"
            and "changed_path" in right_kind
            and _has_complete_changed_paths(objects[right])
        ):
            mr_with_changed_paths.add(left)
        elif (
            right_kind == "gitlab_mr"
            and "changed_path" in left_kind
            and _has_complete_changed_paths(objects[left])
        ):
            mr_with_changed_paths.add(right)
    roles = _self_roles(connection, app_id)
    linked_ids = {
        object_id
        for row in references
        for object_id in (str(row["from_object_id"]), str(row["to_object_id"]))
    }
    seeds = []
    for object_id, row in objects.items():
        kind = str(row["kind"])
        raw_data = json.loads(str(row["data_json"]))
        data = raw_data if isinstance(raw_data, dict) else {}
        if object_id in mr_with_changed_paths:
            data = {**data, "changed_paths": True}
        self_roles = roles.get(object_id, set())
        qualifies = (
            (kind == "jira_issue" and (self_roles or object_id in linked_ids))
            or (
                kind == "gitlab_mr"
                and any(
                    category in categories_for_evidence("gitlab", kind, role, data)
                    for role in self_roles
                    for category in (
                        ParticipationCategory.IMPLEMENTED,
                        ParticipationCategory.REVIEWED,
                        ParticipationCategory.ASSIGNED,
                    )
                )
            )
            or (
                kind == "git_commit"
                and any(
                    supports_category("git", kind, role, ParticipationCategory.IMPLEMENTED)
                    for role in self_roles
                )
            )
            or kind == "manual_evidence"
        )
        if qualifies:
            seeds.append(object_id)
    seeds.sort(
        key=lambda object_id: (
            SEED_PRIORITY.get(str(objects[object_id]["kind"]), 99),
            str(objects[object_id]["external_id"]),
        )
    )

    edges: dict[str, list[tuple[str, str, bool]]] = {}
    for row in references:
        left = str(row["from_object_id"])
        right = str(row["to_object_id"])
        relationship = str(row["relationship_type"])
        edges.setdefault(left, []).append((right, relationship, True))
        edges.setdefault(right, []).append((left, relationship, False))

    used_structural: set[str] = set()
    generated = datetime.now(UTC).isoformat()
    with connection:
        connection.execute(
            """
            DELETE FROM candidate_members
            WHERE candidate_id IN (SELECT id FROM candidate_groups WHERE app_id=?)
            """,
            (app_id,),
        )
        connection.execute("DELETE FROM candidate_groups WHERE app_id=?", (app_id,))
        for seed in seeds:
            if seed in used_structural:
                continue
            members = {seed}
            context: set[str] = set()
            queue: deque[tuple[str, int]] = deque([(seed, 0)])
            overflow = False
            while queue:
                current_id, depth = queue.popleft()
                if depth >= MAX_DEPTH:
                    continue
                for target, relationship, outbound in sorted(edges.get(current_id, [])):
                    if target not in objects:
                        continue
                    if relationship == "jira_parent_of":
                        continue
                    if relationship == "jira_subtask_of" and not outbound:
                        continue
                    if relationship in STRUCTURAL:
                        if target not in members:
                            members.add(target)
                            queue.append((target, depth + 1))
                            if len(members) > MAX_MEMBERS:
                                overflow = True
                                break
                    elif relationship in CONTEXT_ONLY and target not in members:
                        context.add(target)
                        if len(context) > MAX_CONTEXT:
                            overflow = True
                    if overflow:
                        break
                if overflow:
                    break
            retained_members = [seed, *sorted(members - {seed})][:MAX_MEMBERS]
            members = set(retained_members)
            used_structural.update(members)
            context = set(sorted(context - members)[:MAX_CONTEXT])
            row = objects[seed]
            title = str(row["title"] or row["external_id"])
            candidate_id = stable_id("candidate", app_id, seed)
            connection.execute(
                """
                INSERT INTO candidate_groups(
                    id, app_id, seed_object_id, generator_version, suggested_title,
                    suggested_type, status, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    app_id,
                    seed,
                    GENERATOR_VERSION,
                    title,
                    suggest_contribution_type(str(row["kind"]), title),
                    "needs_manual_narrowing" if overflow else "candidate",
                    generated,
                ),
            )
            for member in sorted(members):
                reason = "seed" if member == seed else "structural_reference"
                connection.execute(
                    "INSERT INTO candidate_members VALUES (?, ?, ?, 0)",
                    (candidate_id, member, reason),
                )
            for member in sorted(context):
                connection.execute(
                    "INSERT INTO candidate_members VALUES (?, ?, 'textual_reference', 1)",
                    (candidate_id, member),
                )
    row = connection.execute(
        "SELECT COUNT(*) AS count FROM candidate_groups WHERE app_id=?", (app_id,)
    ).fetchone()
    return int(row["count"] if row else 0)
