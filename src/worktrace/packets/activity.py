"""Dated activity projection; source freshness is never a work-date fallback."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from worktrace.packets.models import EvidenceRecord


def timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else None
    except ValueError:
        return None


@dataclass(frozen=True)
class ActivityPeriod:
    start: datetime | None
    end: datetime | None  # exclusive, including one microsecond for point events
    status: str
    evidence_ids: tuple[str, ...]

    def fields(self) -> dict[str, object]:
        return {
            "date_from": self.start.isoformat() if self.start else None,
            "date_to": (self.end - timedelta(microseconds=1)).isoformat() if self.end else None,
            "period_status": self.status,
        }

    def matches(self, first: str | None, last: str | None, zone: str) -> bool:
        if not (first or last):
            return True
        if self.start is None or self.end is None:
            return False
        tz = ZoneInfo(zone)
        lower = datetime.combine(date.fromisoformat(first), time.min, tzinfo=tz) if first else None
        upper = (
            datetime.combine(date.fromisoformat(last) + timedelta(days=1), time.min, tzinfo=tz)
            if last
            else None
        )
        return (lower is None or self.end > lower) and (upper is None or self.start < upper)


def activity_period(
    records: Iterable[EvidenceRecord],
    *,
    zone: str,
    first: date,
    last: date,
    children: Callable[[EvidenceRecord], Iterable[EvidenceRecord]],
) -> ActivityPeriod:
    lower = datetime.combine(first, time.min, tzinfo=ZoneInfo(zone)).astimezone(UTC)
    upper = datetime.combine(last + timedelta(days=1), time.min, tzinfo=ZoneInfo(zone)).astimezone(
        UTC
    )
    earliest: datetime | None = None
    latest: datetime | None = None
    evidence: set[str] = set()
    unknown = False

    def include(record: EvidenceRecord, start: datetime | None, end: datetime | None) -> bool:
        nonlocal earliest, latest
        if start is None or end is None or start >= end or start >= upper or end <= lower:
            return False
        start, end = max(start, lower), min(end, upper)
        earliest = min(earliest, start) if earliest else start
        latest = max(latest, end) if latest else end
        evidence.add(record.evidence_id)
        return True

    def points(record: EvidenceRecord, boundary_support: set[str] | None = None) -> bool:
        data = record.data
        fields = {
            "git_commit": ("authored_at", "committed_at"),
            "gitlab_merge_request_commit": ("authored_at", "committed_at"),
            "jira_issue": ("created_at",),
            "jira_issue_comment": ("created_at", "updated_at"),
            "jira_issue_changelog": ("created_at",),
            "gitlab_mr": ("created_at", "merged_at", "closed_at"),
            "merge_request": ("created_at", "merged_at", "closed_at"),
            "gitlab_release": ("created_at", "released_at"),
            "release": ("created_at", "released_at"),
            "gitlab_deployment": ("created_at", "finished_at"),
            "git_deployment": ("created_at", "finished_at"),
            "deployment": ("created_at", "finished_at"),
            "gitlab_merge_request_reviewer_state": ("assigned_at",),
            "gitlab_merge_request_discussion_note": ("created_at", "updated_at"),
        }.get(record.kind, ("occurred_at", "event_at", "created_at"))
        found = False
        if data.get("boundary_context") is not True:
            for field in fields:
                at = timestamp(data.get(field))
                if at:
                    found = include(record, at, at + timedelta(microseconds=1)) or found
        intervals = data.get("assignment_intervals", [])
        if isinstance(intervals, list):
            for interval in intervals:
                if isinstance(interval, dict):
                    included = include(
                        record, timestamp(interval.get("from")), timestamp(interval.get("to"))
                    )
                    found = included or found
                    if included and boundary_support is not None:
                        start_id = interval.get("start_history_id")
                        if isinstance(start_id, str):
                            boundary_support.add(start_id)
        return found

    material = list(records)
    contextual = {record.object_id for record in material if record.context_only}
    for record in material:
        if record.context_only or record.data.get("boundary_context") is True:
            continue
        found = points(record)
        if record.kind == "jira_issue":
            boundary_support: set[str] = set()
            issue_children = list(children(record))
            for child in issue_children:
                if (child.context_only or child.object_id in contextual) and not child.data.get(
                    "boundary_context"
                ):
                    continue
                found = points(child, boundary_support) or found
            if boundary_support:
                for child in issue_children:
                    if child.external_id in boundary_support:
                        evidence.add(child.evidence_id)
        unknown = unknown or not found
    status = "unknown" if earliest is None else ("partially_known" if unknown else "known")
    return ActivityPeriod(earliest, latest, status, tuple(sorted(evidence)))
