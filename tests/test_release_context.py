from __future__ import annotations

from dataclasses import replace

from worktrace.packets.models import EvidenceRecord, HumanAttestation
from worktrace.packets.release import build_release_ladder


def _record(
    index: int, kind: str, data: dict[str, object], *, source: str = "gitlab"
) -> EvidenceRecord:
    return EvidenceRecord(
        observation_id=f"obs:context-{index}",
        object_id=f"obj:context-{index}",
        app_id="sample",
        source=source,
        source_instance="fixture",
        kind=kind,
        external_id=str(index),
        title="Context only",
        body_text=None,
        data=data,
        completeness="complete_for_scope",
        fetched_at="2026-01-01T00:00:00+00:00",
        source_updated_at=None,
        availability="visible",
        availability_evidence_id="availability:fixture",
        availability_reason="observed",
        availability_observed_at="2026-01-01T00:00:00+00:00",
        is_current=True,
        context_only=True,
    )


def test_context_only_records_cannot_advance_release_or_outcome_rungs() -> None:
    records = [
        _record(1, "gitlab_mr", {"state": "merged"}),
        _record(2, "jira_issue", {"fix_versions": [{"name": "1.0"}]}, source="jira"),
        _record(
            3,
            "git_deployment",
            {"status": "success", "environment_name": "production"},
        ),
        _record(4, "manual_evidence", {"kind": "measured_outcome"}, source="manual"),
    ]

    ladder = build_release_ladder(
        records,
        {"self_participations": []},
        (),
        ("production",),
        ("v*",),
    )

    assert ladder["merged"]["status"] == "unknown"
    assert ladder["release_associated"]["status"] == "unknown"
    assert ladder["deployed"]["status"] == "unknown"
    assert ladder["measurably_successful"]["status"] == "unknown"


def test_repository_implementation_evidence_is_not_hidden_by_attestation() -> None:
    commit = replace(
        _record(10, "git_commit", {"sha": "a" * 40}, source="git"),
        context_only=False,
    )

    ladder = build_release_ladder(
        [commit],
        {
            "self_participations": [
                {
                    "object_id": commit.object_id,
                    "categories": ["implemented"],
                    "claim_supporting_evidence_ids": [commit.evidence_id],
                }
            ]
        },
        (HumanAttestation("decision:implemented", "implementation", "I implemented it."),),
        (),
        (),
    )

    assert ladder["implemented"]["status"] == "supported"
    assert ladder["implemented"]["supporting_evidence_ids"] == [commit.evidence_id]


def test_implementation_attestation_is_a_fallback_when_authorship_is_missing() -> None:
    attestation = HumanAttestation("decision:implemented", "implementation", "I implemented it.")

    ladder = build_release_ladder([], {"self_participations": []}, (attestation,), (), ())

    assert ladder["implemented"]["status"] == "human_attested"
    assert ladder["implemented"]["supporting_evidence_ids"] == [attestation.decision_id]
    assert ladder["implemented"]["limitations"] == ["This statement is a local-user attestation."]
