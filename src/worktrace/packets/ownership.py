from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence

from worktrace.db.authority import authoritative_current_participation_ctes
from worktrace.packets.authority import find_attestation
from worktrace.packets.models import EvidenceRecord, HumanAttestation
from worktrace.participation import (
    ParticipationCategory,
    canonical_role,
    categories_for_evidence,
    is_implementation_evidence,
    is_implementation_role,
)


def _placeholders(values: Sequence[str]) -> str:
    return ",".join("?" for _ in values)


def build_participation_summary(
    connection: sqlite3.Connection,
    records: Sequence[EvidenceRecord],
    attestations: Sequence[HumanAttestation],
    *,
    current_participation_rows: Mapping[str, tuple[sqlite3.Row, ...]] | None = None,
) -> dict[str, object]:
    """Return role observations without converting them into ownership labels."""

    observation_ids = [record.observation_id for record in records]
    rows: list[sqlite3.Row] = []
    if observation_ids:
        if current_participation_rows is not None:
            rows = [
                row
                for observation_id in observation_ids
                for row in current_participation_rows.get(observation_id, ())
            ]
            rows.sort(
                key=lambda row: (
                    row["effective_from"] is not None,
                    str(row["effective_from"] or ""),
                    str(row["id"]),
                )
            )
        else:
            rows = list(
                connection.execute(
                    f"""
                    WITH {authoritative_current_participation_ctes()}
                    SELECT p.id, p.source_object_id, p.observation_id, p.role,
                        p.effective_from, p.effective_to, a.id AS actor_id,
                        a.display_name, a.is_self, so.source, so.kind, so.external_id
                    FROM authoritative_current_participations p
                    JOIN actors a ON a.id=p.actor_id
                    JOIN source_objects so ON so.id=p.source_object_id
                    WHERE p.observation_id IN ({_placeholders(observation_ids)})
                    ORDER BY p.effective_from, p.id
                    """,
                    observation_ids,
                )
            )

    self_rows = [row for row in rows if bool(row["is_self"])]
    other_rows = [row for row in rows if not bool(row["is_self"])]
    data_by_object = {record.object_id: record.data for record in records}
    self_by_object: dict[str, set[str]] = {}
    for row in self_rows:
        self_by_object.setdefault(str(row["source_object_id"]), set()).add(
            canonical_role(str(row["source"]), str(row["kind"]), str(row["role"]))
        )

    self_participations = [
        {
            "participation_evidence_id": str(row["id"]),
            "observation_evidence_id": str(row["observation_id"]),
            "object_id": str(row["source_object_id"]),
            "source": str(row["source"]),
            "kind": str(row["kind"]),
            "external_id": str(row["external_id"]),
            "role": canonical_role(str(row["source"]), str(row["kind"]), str(row["role"])),
            "categories": sorted(
                category.value
                for category in (
                    ParticipationCategory.IMPLEMENTED,
                    ParticipationCategory.REVIEWED,
                    ParticipationCategory.ASSIGNED,
                    ParticipationCategory.MERGED,
                    ParticipationCategory.DEPLOYED,
                    ParticipationCategory.RELEASE_ASSOCIATED,
                    ParticipationCategory.CONTEXT,
                )
                if category
                in categories_for_evidence(
                    str(row["source"]),
                    str(row["kind"]),
                    str(row["role"]),
                    data_by_object.get(str(row["source_object_id"]), {}),
                )
            ),
            "claim_supporting_evidence_ids": [str(row["observation_id"])],
            "effective_from": row["effective_from"],
            "effective_to": row["effective_to"],
        }
        for row in self_rows
    ]
    committer_only = [
        item
        for item in self_participations
        if item["role"] in {"git_committer", "gitlab_commit_committer"}
        and not any(
            is_implementation_role(str(item["source"]), str(item["kind"]), role)
            for role in self_by_object.get(str(item["object_id"]), set())
        )
    ]
    other_implementation_authors = [
        {
            "participation_evidence_id": str(row["id"]),
            "observation_evidence_id": str(row["observation_id"]),
            "object_id": str(row["source_object_id"]),
            "actor_id": str(row["actor_id"]),
            "display_name": str(row["display_name"]),
            "role": canonical_role(str(row["source"]), str(row["kind"]), str(row["role"])),
        }
        for row in other_rows
        if is_implementation_evidence(
            str(row["source"]),
            str(row["kind"]),
            str(row["role"]),
            data_by_object.get(str(row["source_object_id"]), {}),
        )
    ]

    ownership_attestation = find_attestation(
        attestations, {"ownership", "ownership_statement", "main_owner", "sole_owner"}
    )
    if ownership_attestation is None:
        ownership_statement: dict[str, object] = {
            "status": "requires_human_confirmation",
            "statement": None,
            "supporting_evidence_ids": [],
        }
    else:
        ownership_statement = {
            "status": "human_attested",
            "statement": ownership_attestation.statement,
            "supporting_evidence_ids": [ownership_attestation.decision_id],
        }

    return {
        "self_participations": self_participations,
        "committer_only": committer_only,
        "other_implementation_authors": other_implementation_authors,
        "ownership_statement": ownership_statement,
        "limitations": [
            "Participation records are role observations, not ownership proof.",
            "Counts and productivity scores are intentionally omitted.",
        ],
    }
