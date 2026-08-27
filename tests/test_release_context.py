from __future__ import annotations

from worktrace.packets.models import EvidenceRecord
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
