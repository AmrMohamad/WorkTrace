from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from worktrace.candidates.builder import SEED_PRIORITY, suggest_contribution_type
from worktrace.candidates.decisions import active_decisions
from worktrace.db.authority import authoritative_current_observations
from worktrace.errors import NotFound


@dataclass(frozen=True)
class CandidateView:
    id: str
    app_id: str
    seed_object_id: str | None
    title: str
    contribution_type: str
    status: str
    members: tuple[dict[str, object], ...]
    unsupported_member_ids: tuple[str, ...]
    decisions: tuple[dict[str, object], ...]


def project_candidate(connection: sqlite3.Connection, candidate_id: str) -> CandidateView:
    row = connection.execute(
        "SELECT * FROM candidate_groups WHERE id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"candidate not found: {candidate_id}")
    app_id = str(row["app_id"])
    members: dict[str, dict[str, object]] = {
        str(member["source_object_id"]): dict(member)
        for member in connection.execute(
            """
            SELECT cm.*
            FROM candidate_members cm JOIN source_objects so ON so.id=cm.source_object_id
            WHERE cm.candidate_id=? AND so.app_id=? ORDER BY cm.source_object_id
            """,
            (candidate_id, app_id),
        )
    }
    title = ""
    contribution_type = "unknown"
    status = str(row["status"])
    human_title = False
    human_type = False
    decisions = active_decisions(connection, candidate_id)
    for decision in decisions:
        payload = decision.payload
        if decision.action in {"confirm", "confirm_candidate"}:
            status = "confirmed"
            raw_members = payload.get("members")
            raw_context = payload.get("context_members")
            if isinstance(raw_members, list):
                member_ids = {str(value) for value in raw_members if isinstance(value, str)}
                context_ids = (
                    {str(value) for value in raw_context if isinstance(value, str)}
                    if isinstance(raw_context, list)
                    else set()
                )
                requested_ids = sorted(member_ids | context_ids)
                valid_ids: set[str] = set()
                if requested_ids:
                    placeholders = ",".join("?" for _ in requested_ids)
                    valid_ids = {
                        str(source[0])
                        for source in connection.execute(
                            f"SELECT id FROM source_objects WHERE app_id=? "
                            f"AND id IN ({placeholders})",
                            [app_id, *requested_ids],
                        )
                    }
                members = {
                    object_id: {
                        "candidate_id": candidate_id,
                        "source_object_id": object_id,
                        "membership_reason": "human_confirmation_snapshot",
                        "context_only": int(object_id in context_ids),
                    }
                    for object_id in sorted(valid_ids)
                }
            if isinstance(payload.get("title"), str) and payload["title"]:
                title = str(payload["title"])
                human_title = True
            if isinstance(payload.get("type"), str) and payload["type"]:
                contribution_type = str(payload["type"])
                human_type = True
        elif decision.action in {"ignore", "ignore_candidate"}:
            status = "ignored"
        elif decision.action in {"rename", "rename_contribution"}:
            if isinstance(payload.get("title"), str) and payload["title"]:
                title = str(payload["title"])
                human_title = True
            if isinstance(payload.get("type"), str) and payload["type"]:
                contribution_type = str(payload["type"])
                human_type = True
        elif decision.action == "set_contribution_type":
            if isinstance(payload.get("type"), str) and payload["type"]:
                contribution_type = str(payload["type"])
                human_type = True
        elif decision.action == "add_member":
            object_id = str(payload.get("source_object_id", ""))
            source = connection.execute(
                "SELECT id FROM source_objects WHERE id=? AND app_id=?", (object_id, app_id)
            ).fetchone()
            if source:
                members[object_id] = {
                    "candidate_id": candidate_id,
                    "source_object_id": object_id,
                    "membership_reason": "human_added",
                    "context_only": 0,
                }
        elif decision.action == "remove_member":
            members.pop(str(payload.get("source_object_id", "")), None)
        elif decision.action in {"merge", "merge_contributions"}:
            raw_ids = payload.get("candidate_ids", [])
            other_ids = raw_ids if isinstance(raw_ids, list) else []
            for other_id in other_ids:
                for member in connection.execute(
                    """
                    SELECT member.source_object_id
                    FROM candidate_members member
                    JOIN candidate_groups candidate ON candidate.id=member.candidate_id
                    JOIN source_objects object ON object.id=member.source_object_id
                    WHERE member.candidate_id=? AND candidate.app_id=? AND object.app_id=?
                    """,
                    (str(other_id), app_id, app_id),
                ):
                    object_id = str(member[0])
                    if object_id not in members:
                        members[object_id] = {
                            "candidate_id": candidate_id,
                            "source_object_id": object_id,
                            "membership_reason": "human_merge",
                            "context_only": 0,
                        }
        elif decision.action in {"split", "split_contribution"}:
            keep = payload.get("keep_source_object_ids")
            if isinstance(keep, list):
                allowed = {str(value) for value in keep}
                members = {key: value for key, value in members.items() if key in allowed}
    current = {
        str(observation["source_object_id"]): observation
        for observation in authoritative_current_observations(connection, app_id)
    }
    eligible_member_ids = sorted(set(members) & set(current))
    has_authoritative_support = bool(eligible_member_ids)
    if not has_authoritative_support and status != "confirmed":
        raise NotFound(f"candidate has no authoritative current evidence: {candidate_id}")
    seed_object_id: str | None = None
    if has_authoritative_support:
        original_seed = str(row["seed_object_id"])
        if original_seed in eligible_member_ids:
            seed_object_id = original_seed
        else:
            seed_object_id = min(
                eligible_member_ids,
                key=lambda object_id: (
                    bool(members[object_id].get("context_only")),
                    SEED_PRIORITY.get(str(current[object_id]["kind"]), 99),
                    str(current[object_id]["external_id"]),
                    object_id,
                ),
            )
        seed = current[seed_object_id]
        if not human_title:
            title = str(seed["title"] or seed["external_id"])
        if not human_type:
            contribution_type = suggest_contribution_type(str(seed["kind"]), title)
    else:
        if not human_title:
            title = "Confirmed contribution history (current evidence unavailable)"
        if not human_type:
            contribution_type = "unknown"

    projected_members = tuple(
        {
            **members[object_id],
            "source": str(current[object_id]["source"]),
            "kind": str(current[object_id]["kind"]),
            "external_id": str(current[object_id]["external_id"]),
            "evidence_status": "authoritative_current",
        }
        for object_id in eligible_member_ids
    )
    unsupported_member_ids = (
        tuple(sorted(set(members) - set(eligible_member_ids))) if status == "confirmed" else ()
    )
    return CandidateView(
        id=candidate_id,
        app_id=app_id,
        seed_object_id=seed_object_id,
        title=title,
        contribution_type=contribution_type,
        status=status,
        members=projected_members,
        unsupported_member_ids=unsupported_member_ids,
        decisions=tuple(
            {
                "id": decision.id,
                "action": decision.action,
                "payload": json.loads(json.dumps(decision.payload)),
                "created_at": decision.created_at,
            }
            for decision in decisions
        ),
    )


def list_candidates(
    connection: sqlite3.Connection, app_id: str, *, limit: int = 20
) -> list[CandidateView]:
    rows = connection.execute(
        "SELECT id FROM candidate_groups WHERE app_id=? ORDER BY generated_at DESC, id",
        (app_id,),
    )
    result: list[CandidateView] = []
    for row in rows:
        try:
            result.append(project_candidate(connection, str(row[0])))
        except NotFound:
            continue
        if len(result) >= limit:
            break
    return result
