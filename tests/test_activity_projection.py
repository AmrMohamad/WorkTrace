from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from worktrace.packets.activity import ActivityPeriod, activity_period
from worktrace.packets.models import EvidenceRecord


def record(kind: str, data: dict[str, object], name: str = "1") -> EvidenceRecord:
    return EvidenceRecord(
        observation_id=f"obs:{name}",
        object_id=f"obj:{name}",
        app_id="sample",
        source="jira" if kind.startswith("jira") else "gitlab",
        source_instance="fixture",
        kind=kind,
        external_id=name,
        title=None,
        body_text=None,
        data=data,
        completeness="complete",
        fetched_at="2024-01-31T00:00:00Z",
        source_updated_at="2024-01-31T00:00:00Z",
        availability="visible",
        availability_evidence_id=None,
        availability_reason=None,
        availability_observed_at=None,
        is_current=True,
    )


def project(*records: EvidenceRecord, children: tuple[EvidenceRecord, ...] = ()) -> ActivityPeriod:
    return activity_period(
        records,
        zone="UTC",
        first=date(2024, 1, 1),
        last=date(2024, 1, 31),
        children=lambda _: children,
    )


@pytest.mark.parametrize(
    ("kind", "field"),
    [
        ("gitlab_mr", "merged_at"),
        ("gitlab_mr", "closed_at"),
        ("merge_request", "merged_at"),
        ("gitlab_release", "released_at"),
        ("release", "released_at"),
        ("gitlab_deployment", "finished_at"),
        ("git_deployment", "finished_at"),
        ("deployment", "finished_at"),
        ("gitlab_merge_request_reviewer_state", "assigned_at"),
        ("gitlab_merge_request_discussion_note", "created_at"),
        ("gitlab_merge_request_discussion_note", "updated_at"),
    ],
)
def test_existing_source_event_fields_remain_activity_dates(kind: str, field: str) -> None:
    period = project(record(kind, {field: "2024-01-10T12:00:00Z"}))
    assert period.fields() == {
        "date_from": "2024-01-10T12:00:00+00:00",
        "date_to": "2024-01-10T12:00:00+00:00",
        "period_status": "known",
    }


@pytest.mark.parametrize("kind", ["git_commit", "gitlab_merge_request_commit"])
def test_author_and_committer_dates_are_separate_from_freshness(kind: str) -> None:
    period = project(
        record(
            kind, {"authored_at": "2024-01-03T00:00:00Z", "committed_at": "2024-01-05T00:00:00Z"}
        )
    )
    assert period.fields()["date_from"] == "2024-01-03T00:00:00+00:00"
    assert period.fields()["date_to"] == "2024-01-05T00:00:00+00:00"
    assert project(record(kind, {})).status == "unknown"


def test_context_child_never_expands_material_issue_period() -> None:
    issue = record("jira_issue", {"created_at": "2024-01-03T00:00:00Z"})
    comment = replace(
        record("jira_issue_comment", {"created_at": "2024-01-20T00:00:00Z"}, "comment"),
        context_only=True,
    )
    period = project(issue, children=(comment,))
    assert period.fields()["date_to"] == "2024-01-03T00:00:00+00:00"
    assert period.evidence_ids == ("obs:1",)
    # Candidate context membership also overrides the independently loaded child.
    assert project(issue, comment, children=(replace(comment, context_only=False),)) == period


def test_assignment_boundaries_support_issue_overlap_without_boundary_events() -> None:
    issue = record("jira_issue", {})
    before = record(
        "jira_issue_changelog",
        {"created_at": "2023-12-01T00:00:00Z", "boundary_context": True},
        "before",
    )
    after = record(
        "jira_issue_changelog",
        {
            "created_at": "2024-02-01T00:00:00Z",
            "boundary_context": True,
            "assignment_intervals": [
                {
                    "from": "2023-12-01T00:00:00Z",
                    "to": "2024-02-01T00:00:00Z",
                    "start_history_id": "before",
                }
            ],
        },
        "after",
    )
    period = project(issue, children=(before, after))
    assert period.fields() == {
        "date_from": "2024-01-01T00:00:00+00:00",
        "date_to": "2024-01-31T23:59:59.999999+00:00",
        "period_status": "known",
    }
    assert period.evidence_ids == ("obs:after", "obs:before")
    assert project(after).status == "unknown"
    assert not project(after).matches("2024-01-01", "2024-01-31", "UTC")


def test_nonoverlapping_interval_does_not_add_irrelevant_endpoint_citation() -> None:
    issue = record("jira_issue", {})
    before = record("jira_issue_changelog", {"boundary_context": True}, "before")
    event = record(
        "jira_issue_changelog",
        {
            "created_at": "2024-01-10T00:00:00Z",
            "assignment_intervals": [
                {
                    "from": "2023-11-01T00:00:00Z",
                    "to": "2023-12-01T00:00:00Z",
                    "start_history_id": "before",
                }
            ],
        },
        "event",
    )
    assert project(issue, children=(before, event)).evidence_ids == ("obs:event",)


def test_unknown_material_dates_are_explicit_and_filtering_uses_local_day() -> None:
    dated = record("jira_issue_comment", {"created_at": "2024-01-02T01:00:00Z"})
    undated = record("jira_issue_comment", {"created_at": "invalid"}, "unknown")
    period = project(dated, undated)
    assert period.status == "partially_known"
    assert period.matches("2024-01-01", "2024-01-01", "America/New_York")
    assert not period.matches("2024-01-02", "2024-01-02", "America/New_York")
    assert project(undated).fields()["date_from"] is None
    assert project(undated).matches(None, None, "UTC")
    assert not project(undated).matches("2024-01-01", None, "UTC")
