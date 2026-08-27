from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from worktrace.candidates.decisions import active_decisions
from worktrace.errors import NotFound


@dataclass(frozen=True)
class CandidateView:
    id: str
    app_id: str
    title: str
    contribution_type: str
    status: str
    members: tuple[dict[str, object], ...]
    decisions: tuple[dict[str, object], ...]


def project_candidate(connection: sqlite3.Connection, candidate_id: str) -> CandidateView:
    row = connection.execute(
        "SELECT * FROM candidate_groups WHERE id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"candidate not found: {candidate_id}")
    members: dict[str, dict[str, object]] = {
        str(member["source_object_id"]): dict(member)
        for member in connection.execute(
            """
            SELECT cm.*, so.kind, so.external_id, so.source
            FROM candidate_members cm JOIN source_objects so ON so.id=cm.source_object_id
            WHERE cm.candidate_id=? ORDER BY cm.source_object_id
            """,
            (candidate_id,),
        )
    }
    title = str(row["suggested_title"])
    contribution_type = str(row["suggested_type"])
    status = str(row["status"])
    decisions = active_decisions(connection, candidate_id)
    for decision in decisions:
        payload = decision.payload
        if decision.action in {"confirm", "confirm_candidate"}:
            status = "confirmed"
        elif decision.action in {"ignore", "ignore_candidate"}:
            status = "ignored"
        elif decision.action in {"rename", "rename_contribution"}:
            title = str(payload.get("title", title))
            contribution_type = str(payload.get("type", contribution_type))
        elif decision.action == "add_member":
            object_id = str(payload.get("source_object_id", ""))
            source = connection.execute(
                "SELECT id, source, kind, external_id FROM source_objects WHERE id=?", (object_id,)
            ).fetchone()
            if source:
                members[object_id] = {
                    "candidate_id": candidate_id,
                    "source_object_id": object_id,
                    "membership_reason": "human_added",
                    "context_only": 0,
                    "source": source["source"],
                    "kind": source["kind"],
                    "external_id": source["external_id"],
                }
        elif decision.action == "remove_member":
            members.pop(str(payload.get("source_object_id", "")), None)
        elif decision.action in {"merge", "merge_contributions"}:
            raw_ids = payload.get("candidate_ids", [])
            other_ids = raw_ids if isinstance(raw_ids, list) else []
            for other_id in other_ids:
                for member in connection.execute(
                    "SELECT source_object_id FROM candidate_members WHERE candidate_id=?",
                    (str(other_id),),
                ):
                    object_id = str(member[0])
                    if object_id not in members:
                        source = connection.execute(
                            "SELECT source, kind, external_id FROM source_objects WHERE id=?",
                            (object_id,),
                        ).fetchone()
                        if source:
                            members[object_id] = {
                                "candidate_id": candidate_id,
                                "source_object_id": object_id,
                                "membership_reason": "human_merge",
                                "context_only": 0,
                                "source": source["source"],
                                "kind": source["kind"],
                                "external_id": source["external_id"],
                            }
        elif decision.action in {"split", "split_contribution"}:
            keep = payload.get("keep_source_object_ids")
            if isinstance(keep, list):
                allowed = {str(value) for value in keep}
                members = {key: value for key, value in members.items() if key in allowed}
    return CandidateView(
        id=candidate_id,
        app_id=str(row["app_id"]),
        title=title,
        contribution_type=contribution_type,
        status=status,
        members=tuple(members[key] for key in sorted(members)),
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
        "SELECT id FROM candidate_groups WHERE app_id=? ORDER BY generated_at DESC, id LIMIT ?",
        (app_id, limit),
    )
    return [project_candidate(connection, str(row[0])) for row in rows]
