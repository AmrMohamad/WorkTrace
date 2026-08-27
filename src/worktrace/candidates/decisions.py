from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from worktrace.errors import NotFound
from worktrace.normalize.redaction import Redactor


@dataclass(frozen=True)
class Decision:
    id: str
    action: str
    target_id: str
    payload: dict[str, object]
    created_at: str
    undo_target_id: str | None = None


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
    if (
        undo_target_id is not None
        and connection.execute(
            "SELECT 1 FROM human_decisions WHERE id=?", (undo_target_id,)
        ).fetchone()
        is None
    ):
        raise NotFound(f"decision not found: {undo_target_id}")
    stored_payload: dict[str, object] = dict(payload or {})
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
    with connection:
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
    return decision_id


def undo_decision(connection: sqlite3.Connection, decision_id: str) -> str:
    row = connection.execute(
        "SELECT target_id FROM human_decisions WHERE id=?", (decision_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"decision not found: {decision_id}")
    return append_decision(
        connection,
        "undo_decision",
        str(row["target_id"]),
        {"compensates": decision_id},
        undo_target_id=decision_id,
    )


def active_decisions(connection: sqlite3.Connection, target_id: str) -> list[Decision]:
    rows = connection.execute(
        """
        SELECT d.* FROM human_decisions d
        WHERE d.target_id=?
          AND NOT EXISTS (
            SELECT 1 FROM human_decisions u
            WHERE u.action IN ('undo', 'undo_decision') AND u.undo_target_id=d.id
          )
        ORDER BY d.created_at, d.id
        """,
        (target_id,),
    )
    return [
        Decision(
            id=str(row["id"]),
            action=str(row["action"]),
            target_id=str(row["target_id"]),
            payload=json.loads(str(row["payload_json"])),
            created_at=str(row["created_at"]),
            undo_target_id=(str(row["undo_target_id"]) if row["undo_target_id"] else None),
        )
        for row in rows
    ]
