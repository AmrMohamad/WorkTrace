"""CLI-owned, run-scoped staging; adapters never receive a database connection."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, replace
from datetime import UTC, datetime

from worktrace.adapters.base import (
    ActorIdentity,
    JSONValue,
    NormalizedPage,
    NormalizedRecord,
    ObservationMetadata,
    Participation,
    ParticipationRole,
    Reference,
    ReferenceStrength,
    SourceObjectIdentity,
)
from worktrace.adapters.jira import JiraAdapter
from worktrace.db.repository import EvidenceRepository
from worktrace.errors import PermanentSourceError, ScopeViolation


def _reasons(value: object) -> set[str]:
    return {item for item in value if isinstance(item, str)} if isinstance(value, list) else set()


def _assignments(record: NormalizedRecord) -> list[dict[str, JSONValue]]:
    value = record.payload.get("transitions")
    return (
        [item for item in value if isinstance(item, dict) and item.get("field") == "assignee"]
        if isinstance(value, list)
        else []
    )


def _decode(text: str) -> NormalizedRecord:
    value = json.loads(text)
    return NormalizedRecord(
        identity=SourceObjectIdentity(**value["identity"]),
        observation=ObservationMetadata(**value["observation"]),
        payload=value["payload"],
        payload_hash=value["payload_hash"],
        participations=tuple(
            Participation(
                actor=ActorIdentity(**p["actor"]),
                role=ParticipationRole(p["role"]),
                effective_from=p["effective_from"],
                effective_to=p["effective_to"],
            )
            for p in value["participations"]
        ),
        references=tuple(
            Reference(**{**r, "strength": ReferenceStrength(r["strength"])})
            for r in value["references"]
        ),
        untrusted_text_fields=tuple(value["untrusted_text_fields"]),
    )


class JiraStage:
    def __init__(self, repository: EvidenceRepository, run_id: str) -> None:
        self.connection = repository.connection
        self.run_id = run_id
        self.run = self.connection.execute(
            "SELECT * FROM sync_runs WHERE id=?", (run_id,)
        ).fetchone()
        if self.run is None:
            raise ScopeViolation("Jira stage requires a source run")

    def put(self, kind: str, record: NormalizedRecord) -> bool:
        return self.put_page(kind, (record,))

    def put_page(self, kind: str, records: Iterable[NormalizedRecord]) -> bool:
        changed = False
        with self.connection:
            for record in records:
                changed = self._put(kind, record) or changed
        return changed

    def _put(self, kind: str, record: NormalizedRecord) -> bool:
        identity = record.identity
        if (
            identity.app_id != self.run["app_id"]
            or identity.source_kind != "jira"
            or identity.source_instance != self.run["source_instance"]
        ):
            raise ScopeViolation("Jira staged record escaped source scope")
        event_at = ""
        if kind.startswith("history:"):
            try:
                at = datetime.fromisoformat(
                    str(record.payload["created_at"]).replace("Z", "+00:00")
                )
                if at.tzinfo is None:
                    raise ValueError("timezone missing")
                event_at = at.astimezone(UTC).isoformat()
            except (KeyError, ValueError) as exc:
                raise PermanentSourceError("Jira history has invalid activity time") from exc
        changed = False
        previous = self.connection.execute(
            "SELECT record_json FROM jira_import_stage WHERE run_id=? AND kind=? AND external_id=?",
            (self.run_id, kind, identity.external_id),
        ).fetchone()
        if previous:
            old = _decode(str(previous[0]))
            reasons = sorted(
                _reasons(old.payload.get("selected_by"))
                | _reasons(record.payload.get("selected_by"))
            )
            changed = old.payload_hash != record.payload_hash
            record = replace(old, payload={**old.payload, "selected_by": list[JSONValue](reasons)})
        self.connection.execute(
            "INSERT INTO jira_import_stage VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id,kind,external_id) "
            "DO UPDATE SET record_json=excluded.record_json",
            (
                self.run_id,
                kind,
                identity.external_id,
                event_at,
                json.dumps(asdict(record), sort_keys=True),
            ),
        )
        return changed

    def records(self, kind: str, *, by_time: bool = False) -> Iterator[NormalizedRecord]:
        order = "event_at, external_id" if by_time else "external_id"
        count = "COUNT(*) OVER (PARTITION BY event_at)" if by_time else "1"
        cursor = self.connection.execute(
            f"SELECT record_json, {count} AS time_count "
            f"FROM jira_import_stage WHERE run_id=? AND kind=? ORDER BY {order}",
            (self.run_id, kind),
        )
        while rows := cursor.fetchmany(100):
            for row in rows:
                record = _decode(str(row[0]))
                if by_time and row[1] > 1:
                    record = replace(record, payload={**record.payload, "ambiguous_time": True})
                yield record

    def known_issue(self, issue_id: str) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM jira_import_stage WHERE run_id=? AND kind='issue' AND external_id=?",
                (self.run_id, issue_id),
            ).fetchone()
            is not None
        )


def _histories(
    adapter: JiraAdapter, stage: JiraStage, issue: NormalizedRecord
) -> Iterator[NormalizedRecord]:
    previous: NormalizedRecord | None = None
    predecessor: NormalizedRecord | None = None
    successor_seen = False
    start, end = adapter.work_window
    for record in stage.records("history:" + issue.identity.external_id, by_time=True):
        at = datetime.fromisoformat(str(record.payload["created_at"]).replace("Z", "+00:00"))
        transitions = _assignments(record)
        intervals: list[JSONValue] = []
        if transitions and previous:
            old_transitions = _assignments(previous)
            if (
                len(transitions) == len(old_transitions) == 1
                and not record.payload.get("ambiguous_time")
                and not previous.payload.get("ambiguous_time")
            ):
                old, new = old_transitions[0], transitions[0]
                before = datetime.fromisoformat(
                    str(previous.payload["created_at"]).replace("Z", "+00:00")
                )
                if old["to_id"] and old["to_id"] == new["from_id"] and before < at:
                    intervals.append(
                        {
                            "actor_id": old["to_id"],
                            "from": before.isoformat(),
                            "to": at.isoformat(),
                            "start_history_id": previous.identity.external_id,
                            "end_history_id": record.identity.external_id,
                        }
                    )
        if transitions:
            previous = record
        if at < start:
            if transitions:
                predecessor = record
            continue
        if predecessor:
            yield replace(predecessor, payload={**predecessor.payload, "boundary_context": True})
            predecessor = None
        if at >= end and (not transitions or successor_seen):
            continue
        if at >= end:
            successor_seen = True
        yield replace(
            record,
            payload={
                **record.payload,
                "boundary_context": at >= end,
                "assignment_intervals": intervals,
                "assignment_period_status": "known" if intervals else "unknown",
            },
        )
    if predecessor:
        yield replace(predecessor, payload={**predecessor.payload, "boundary_context": True})


def jira_pages(
    adapter: JiraAdapter, repository: EvidenceRepository, run_id: str
) -> Iterator[NormalizedPage]:
    stage = JiraStage(repository, run_id)
    for page in adapter.iter_discovery_pages():
        changed = stage.put_page("issue", page.records)
        yield replace(
            page,
            records=(),
            limitations=page.limitations
            + (
                ("Jira changed during discovery; the first issue version was retained.",)
                if changed
                else ()
            ),
        )
    last_time = ""
    for issue in stage.records("issue"):
        last_time = issue.observation.observed_at
        yield NormalizedPage(
            "jira",
            issue.identity.source_instance,
            "issue",
            issue.identity.external_id,
            None,
            True,
            (issue,),
        )
        for page in adapter.issue_context_pages(issue):
            if page.resource_type == "issue_changelog":
                changed = stage.put_page("history:" + issue.identity.external_id, page.records)
                yield replace(
                    page,
                    records=(),
                    limitations=page.limitations
                    + (
                        ("Jira history changed during paging; first version retained.",)
                        if changed
                        else ()
                    ),
                )
            else:
                yield page
        for record in _histories(adapter, stage, issue):
            yield NormalizedPage(
                "jira",
                issue.identity.source_instance,
                "issue_changelog",
                record.identity.external_id,
                None,
                True,
                (record,),
            )
    if last_time:
        yield from adapter.hierarchy_pages(stage.records("issue"), last_time, stage.known_issue)
