from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from typing import cast

from worktrace.candidates.projector import CandidateView, project_candidate
from worktrace.config import AppConfig, WorkTraceConfig
from worktrace.constants import DEFAULT_EXCERPT_CHARS, STALE_AFTER_DAYS
from worktrace.db.authority import (
    authoritative_availability_event_ctes,
    authoritative_current_availability_ctes,
    authoritative_current_observation_ctes,
    authoritative_current_participation_ctes,
    authoritative_current_reference_ctes,
    completeness_is_full_scope,
    parse_scope,
    run_authority_limitation,
    run_is_authoritative,
    selection_policy_version,
)
from worktrace.domain.enums import ClaimStatus, ObservationType
from worktrace.errors import NotFound, ScopeViolation
from worktrace.packets.authority import (
    attested_answer,
    find_attestation,
    is_merge_request_kind,
    supported_answer,
    unknown_answer,
)
from worktrace.packets.gaps import build_gap_report
from worktrace.packets.models import (
    ContributionView,
    EvidenceRecord,
    HumanAttestation,
    QuestionAnswer,
)
from worktrace.packets.ownership import build_participation_summary
from worktrace.packets.release import build_release_ladder

PHASE4_QUESTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "contribution_identity": (
        ("identity.what", "What was the contribution?"),
        ("identity.app_flow", "Which application and flow did it affect?"),
        ("identity.when", "When did the work occur?"),
        ("identity.origin", "Was it assigned, proposed, or inherited?"),
        ("identity.ownership", "Was the engineer the sole, main, or a contributing owner?"),
    ),
    "problem_context": (
        ("problem.what", "What problem existed?"),
        ("problem.before", "What happened before the change?"),
        ("problem.severity", "How serious was the problem?"),
        ("problem.affected", "Who or which flow was affected?"),
        ("problem.blocked", "What did the problem block?"),
        ("problem.constraints", "What constraints shaped the work?"),
        ("problem.requirement_clarity", "Was the requirement unclear or changing?"),
    ),
    "action": (
        ("action.implemented", "What did the engineer implement?"),
        ("action.decisions", "Which technical decisions were made?"),
        ("action.tools", "Which tools or frameworks were involved?"),
        ("action.reuse", "Was a reusable component produced?"),
        ("action.architecture", "How did architecture or data flow change?"),
        ("action.coordination", "What coordination or review occurred?"),
        ("action.quality", "Were tests, documentation, or monitoring added?"),
    ),
    "result": (
        ("result.changed", "What changed as a result?"),
        ("result.measured", "Is there a measurable before-and-after result?"),
        ("result.scope", "What scope was affected?"),
        ("result.efficiency", "Were errors or time reduced?"),
        ("result.business", "Was conversion, stability, or another outcome improved?"),
        ("result.released", "What release state is supported?"),
        ("result.current_use", "Is the contribution still used or enabled?"),
        ("result.reused", "Was the work reused later?"),
        ("result.feedback", "Is there client or stakeholder feedback?"),
        ("result.interview_defensible", "Which parts are defensible in an interview?"),
    ),
}


def _parse_json_object(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}
    return cast(dict[str, object], parsed) if isinstance(parsed, dict) else {}


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _calendar_date(value: str | None) -> str | None:
    parsed = _parse_timestamp(value)
    if parsed is not None:
        return parsed.date().isoformat()
    return value[:10] if value and len(value) >= 10 else value


def _member_values(payload: dict[str, object]) -> set[str]:
    for key in ("members", "member_ids", "source_object_ids"):
        value = payload.get(key)
        if isinstance(value, list):
            return {str(item) for item in value if isinstance(item, str)}
    return set()


def _single_member(payload: dict[str, object]) -> str | None:
    for key in ("member_id", "source_object_id", "evidence_object_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


class PacketBuilder:
    """Read model over the SQLite ledger; this class never mutates state."""

    def __init__(self, connection: sqlite3.Connection, config: WorkTraceConfig) -> None:
        self.connection = connection
        self.config = config

    def _app(self, app_id: str) -> AppConfig:
        return self.config.app(app_id)

    def _active_decisions(self) -> list[tuple[str, str, str, dict[str, object], str | None]]:
        rows = list(
            self.connection.execute(
                """
                SELECT id, action, target_id, payload_json, undo_target_id
                FROM human_decisions ORDER BY created_at, id
                """
            )
        )
        canceled = {
            str(row["undo_target_id"])
            for row in rows
            if str(row["action"]) == "undo_decision" and row["undo_target_id"]
        }
        return [
            (
                str(row["id"]),
                str(row["action"]),
                str(row["target_id"]),
                _parse_json_object(row["payload_json"]),
                str(row["undo_target_id"]) if row["undo_target_id"] else None,
            )
            for row in rows
            if str(row["id"]) not in canceled and str(row["action"]) != "undo_decision"
        ]

    def _candidate(self, candidate_id: str) -> ContributionView | None:
        try:
            projected = project_candidate(self.connection, candidate_id)
        except NotFound:
            return None
        if projected.status == "ignored":
            raise NotFound(f"candidate was ignored by a human decision: {candidate_id}")
        members = {
            str(member["source_object_id"])
            for member in projected.members
            if not bool(member.get("context_only"))
        }
        context = {
            str(member["source_object_id"])
            for member in projected.members
            if bool(member.get("context_only"))
        }
        return ContributionView(
            id=candidate_id,
            candidate_id=candidate_id,
            app_id=projected.app_id,
            title=projected.title,
            contribution_type=projected.contribution_type,
            member_ids=members,
            context_ids=context,
        )

    def _resolve_contribution(self, identifier: str) -> ContributionView:
        candidate = self._candidate(identifier)
        decisions = self._active_decisions()
        if candidate is not None:
            state = candidate
        else:
            state = ContributionView(
                id=identifier,
                app_id="",
                title=identifier,
                contribution_type="unknown",
                member_ids=set(),
            )

        base_found = candidate is not None
        for decision_id, action, target_id, payload, _ in decisions:
            contribution_id = payload.get("contribution_id")
            creates_identifier = (
                isinstance(contribution_id, str)
                and contribution_id == identifier
                and action in {"confirm_candidate", "merge_contributions", "split_contribution"}
            )
            if creates_identifier:
                base_found = True
                state.id = identifier
                if action == "confirm_candidate":
                    state.candidate_id = target_id
                state.decision_evidence_ids.add(decision_id)
                state.member_ids = _member_values(payload) or state.member_ids
                context = payload.get("context_members")
                if isinstance(context, list):
                    state.context_ids = {str(item) for item in context if isinstance(item, str)}
                if isinstance(payload.get("title"), str):
                    state.title = str(payload["title"])
                if isinstance(payload.get("type"), str):
                    state.contribution_type = str(payload["type"])
                if isinstance(payload.get("app_id"), str):
                    state.app_id = str(payload["app_id"])

            applies = target_id == identifier or creates_identifier
            if not applies:
                continue
            state.decision_evidence_ids.add(decision_id)
            if action == "rename_contribution" and isinstance(payload.get("title"), str):
                state.title = str(payload["title"])
            elif action == "set_contribution_type" and isinstance(payload.get("type"), str):
                state.contribution_type = str(payload["type"])
            elif action == "add_member":
                member = _single_member(payload)
                if member:
                    state.member_ids.add(member)
                    state.context_ids.discard(member)
            elif action == "remove_member":
                member = _single_member(payload)
                if member:
                    state.member_ids.discard(member)
                    state.context_ids.discard(member)
            elif action == "mark_context_only":
                member = _single_member(payload)
                if member:
                    state.member_ids.discard(member)
                    state.context_ids.add(member)
            elif action == "attest_claim":
                claim = payload.get("claim")
                statement = payload.get("statement")
                if isinstance(claim, str) and isinstance(statement, str) and statement:
                    source_note = payload.get("source_note")
                    state.attestations.append(
                        HumanAttestation(
                            decision_id=decision_id,
                            claim=claim,
                            statement=statement,
                            source_note=str(source_note) if isinstance(source_note, str) else None,
                        )
                    )

        if not base_found:
            raise NotFound(f"contribution not found: {identifier}")
        all_members = state.member_ids | state.context_ids
        if not state.app_id and all_members:
            placeholders = ",".join("?" for _ in all_members)
            rows = list(
                self.connection.execute(
                    f"SELECT DISTINCT app_id FROM source_objects WHERE id IN ({placeholders})",
                    sorted(all_members),
                )
            )
            if len(rows) != 1:
                raise ScopeViolation(
                    "contribution members must belong to exactly one configured app"
                )
            state.app_id = str(rows[0]["app_id"])
        self._app(state.app_id)
        return state

    def _record_for_object(self, object_id: str, context_only: bool) -> EvidenceRecord | None:
        row = self.connection.execute(
            f"""
            WITH {authoritative_current_observation_ctes()},
                 {authoritative_availability_event_ctes()}
            SELECT current.*, so.app_id, so.source, so.source_instance, so.kind, so.external_id,
                so.availability, so.availability_reason, so.availability_observed_at,
                (
                    SELECT event.id
                    FROM authoritative_current_availability_events event
                    WHERE event.source_object_id=so.id
                    LIMIT 1
                ) AS availability_evidence_id
            FROM authoritative_current_observations current
            JOIN source_objects so ON so.id=current.source_object_id
            WHERE current.source_object_id=?
            """,
            (object_id,),
        ).fetchone()
        if row is None:
            return None
        return EvidenceRecord(
            observation_id=str(row["id"]),
            object_id=str(row["source_object_id"]),
            app_id=str(row["app_id"]),
            source=str(row["source"]),
            source_instance=str(row["source_instance"]),
            kind=str(row["kind"]),
            external_id=str(row["external_id"]),
            title=str(row["title"]) if row["title"] is not None else None,
            body_text=str(row["body_text"]) if row["body_text"] is not None else None,
            data=_parse_json_object(row["data_json"]),
            completeness=str(row["completeness"]),
            fetched_at=str(row["fetched_at"]),
            source_updated_at=(
                str(row["source_updated_at"]) if row["source_updated_at"] is not None else None
            ),
            availability=str(row["availability"]),
            availability_evidence_id=(
                str(row["availability_evidence_id"])
                if row["availability_evidence_id"] is not None
                else None
            ),
            availability_reason=(
                str(row["availability_reason"]) if row["availability_reason"] is not None else None
            ),
            availability_observed_at=(
                str(row["availability_observed_at"])
                if row["availability_observed_at"] is not None
                else None
            ),
            is_current=True,
            context_only=context_only,
        )

    def _records(self, contribution: ContributionView) -> list[EvidenceRecord]:
        records: list[EvidenceRecord] = []
        for object_id in sorted(contribution.member_ids | contribution.context_ids):
            record = self._record_for_object(
                object_id, context_only=object_id in contribution.context_ids
            )
            if record is None:
                continue
            if record.app_id != contribution.app_id:
                raise ScopeViolation("contribution contains an object from another app")
            records.append(record)
        return records

    def _unsupported_member_ids(
        self, contribution: ContributionView, records: Sequence[EvidenceRecord]
    ) -> list[str]:
        supported = {record.object_id for record in records}
        return sorted((contribution.member_ids | contribution.context_ids) - supported)

    def source_status(self, app_id: str) -> dict[str, object]:
        app = self._app(app_id)
        rows = list(
            self.connection.execute(
                """
                SELECT * FROM (
                    SELECT sr.*, ROW_NUMBER() OVER (
                        PARTITION BY source, source_instance
                        ORDER BY COALESCE(completed_at, started_at) DESC, id DESC
                    ) AS position
                    FROM sync_runs sr WHERE app_id=?
                ) WHERE position=1 ORDER BY source, source_instance
                """,
                (app_id,),
            )
        )
        expected: set[str] = set()
        if app.repo_paths:
            expected.add("git")
        if app.jira_project_keys:
            expected.add("jira")
        if app.gitlab_project_ids:
            expected.add("gitlab")
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["source"]), []).append(row)
        result: dict[str, object] = {}
        now = datetime.now(UTC)
        for source in sorted(expected | set(grouped)):
            source_rows = grouped.get(source, [])
            instances: list[dict[str, object]] = []
            for row in source_rows:
                completed = _parse_timestamp(row["completed_at"])
                stale = completed is None or now - completed > timedelta(days=STALE_AFTER_DAYS)
                scope = parse_scope(row["scope_json"])
                selection_version = selection_policy_version(scope)
                authoritative = run_is_authoritative(
                    source,
                    str(row["status"]),
                    str(row["completeness"]),
                    scope,
                )
                progress = _parse_json_object(row["progress_json"])
                progress_limitations = progress.get("limitations", [])
                limitations = (
                    [value for value in progress_limitations if isinstance(value, str) and value]
                    if isinstance(progress_limitations, list)
                    else []
                )
                limitation = run_authority_limitation(
                    source,
                    str(row["status"]),
                    str(row["completeness"]),
                    scope,
                )
                if limitation and limitation not in limitations:
                    limitations.append(limitation)
                raw_selection_events = progress.get("selection_events", [])
                selection_events = (
                    [value for value in raw_selection_events if isinstance(value, dict)]
                    if isinstance(raw_selection_events, list)
                    else []
                )
                complete = authoritative and completeness_is_full_scope(str(row["completeness"]))
                instances.append(
                    {
                        "source_instance": str(row["source_instance"]),
                        "run_id": str(row["id"]),
                        "status": str(row["status"]),
                        "completeness": str(row["completeness"]),
                        "completed_at": row["completed_at"],
                        "complete": complete,
                        "stale": stale,
                        "error_summary": row["error_summary"],
                        "selection_policy_version": selection_version,
                        "authoritative_current": authoritative,
                        "limitations": limitations,
                        "selection_events": selection_events,
                    }
                )
            result[source] = {
                "complete": bool(instances) and all(bool(item["complete"]) for item in instances),
                "stale": not instances or any(bool(item["stale"]) for item in instances),
                "instances": instances,
                "warning": None
                if instances and all(bool(item["complete"]) for item in instances)
                else f"{source} evidence is missing or incomplete for the configured scope.",
            }
        return result

    def _contradictions(
        self, contribution: ContributionView, records: Sequence[EvidenceRecord]
    ) -> list[dict[str, object]]:
        member_ids = sorted(contribution.member_ids | contribution.context_ids)
        contradictions: list[dict[str, object]] = []
        if member_ids:
            placeholders = ",".join("?" for _ in member_ids)
            rows = self.connection.execute(
                f"""
                WITH {authoritative_current_reference_ctes()}
                SELECT id, relationship_type, supporting_observation_id
                FROM authoritative_current_references
                WHERE app_id=?
                    AND from_object_id IN ({placeholders})
                    AND to_object_id IN ({placeholders})
                    AND lower(relationship_type) LIKE '%revert%'
                ORDER BY id
                """,
                [contribution.app_id, *member_ids, *member_ids],
            )
            for row in rows:
                evidence = [str(row["id"])]
                if row["supporting_observation_id"]:
                    evidence.append(str(row["supporting_observation_id"]))
                contradictions.append(
                    {
                        "kind": "recorded_revert",
                        "statement": "A member record has a typed revert relationship.",
                        "evidence_ids": evidence,
                    }
                )
        for record in records:
            state = str(record.data.get("state", "")).casefold()
            if (
                record.source.casefold() == "gitlab"
                and is_merge_request_kind(record.kind)
                and state == "closed"
            ):
                contradictions.append(
                    {
                        "kind": "closed_without_merge",
                        "statement": "GitLab recorded a closed merge request, not a merged one.",
                        "evidence_ids": [record.evidence_id],
                    }
                )
            if record.availability != "visible":
                contradictions.append(
                    {
                        "kind": "source_unavailable",
                        "statement": (
                            "A previously observed source object is no longer visible: "
                            f"{record.availability_reason or 'reason unknown'}."
                        ),
                        "evidence_ids": [record.availability_evidence_id or record.evidence_id],
                        "observed_at": record.availability_observed_at,
                    }
                )
        recorded_object_ids = {record.object_id for record in records}
        unavailable_member_ids = [
            object_id for object_id in member_ids if object_id not in recorded_object_ids
        ]
        if unavailable_member_ids:
            placeholders = ",".join("?" for _ in unavailable_member_ids)
            unavailable_rows = self.connection.execute(
                f"""
                WITH {authoritative_current_availability_ctes()}
                SELECT event.id, event.source_object_id, event.reason, event.observed_at
                FROM authoritative_current_availability_events event
                WHERE event.source_object_id IN ({placeholders})
                  AND event.state='unavailable'
                ORDER BY event.source_object_id
                """,
                unavailable_member_ids,
            )
            for row in unavailable_rows:
                contradictions.append(
                    {
                        "kind": "source_unavailable",
                        "statement": (
                            "A previously observed source object is no longer visible: "
                            f"{str(row['reason']) or 'reason unknown'}."
                        ),
                        "evidence_ids": [str(row["id"])],
                        "observed_at": row["observed_at"],
                    }
                )
        return contradictions

    @staticmethod
    def _date_range(records: Sequence[EvidenceRecord]) -> tuple[str | None, str | None]:
        values = sorted(
            value
            for record in records
            for value in (record.source_updated_at or record.fetched_at,)
            if value
        )
        return (values[0], values[-1]) if values else (None, None)

    @staticmethod
    def _path_is_ignored(path: str, app: AppConfig) -> bool:
        for raw_pattern in app.ignored_paths:
            pattern = raw_pattern.replace("\\", "/").removeprefix("./")
            if pattern.endswith("/") and path.startswith(pattern):
                return True
            if fnmatchcase(path, pattern):
                return True
        return False

    @staticmethod
    def _changed_paths(record: EvidenceRecord) -> list[tuple[str, bool]]:
        generated_paths = record.data.get("generated_paths", [])
        generated: set[str] = (
            {
                value.replace("\\", "/").removeprefix("./")
                for value in generated_paths
                if isinstance(value, str)
            }
            if isinstance(generated_paths, list)
            else set()
        )
        values: object = None
        for key in ("changed_paths", "changes", "paths"):
            candidate = record.data.get(key)
            if isinstance(candidate, list):
                values = candidate
                break
        if not isinstance(values, list):
            return []
        result: list[tuple[str, bool]] = []
        for raw in values:
            raw_path: object
            is_generated = False
            if isinstance(raw, dict):
                raw_path = raw.get("new_path") or raw.get("path") or raw.get("old_path")
                is_generated = raw.get("generated") is True or raw.get("is_generated") is True
            else:
                raw_path = raw
            if not isinstance(raw_path, str):
                continue
            path = raw_path.replace("\\", "/").removeprefix("./")
            parts = path.split("/")
            if not path or path.startswith("/") or ".." in parts:
                continue
            result.append((path, is_generated or path in generated))
        return result

    @staticmethod
    def _changed_path_scope_is_complete(record: EvidenceRecord) -> bool:
        return (
            record.completeness in {"complete", "complete_for_scope"}
            and record.data.get("overflow") is not True
            and record.data.get("scope_complete") is not False
        )

    @classmethod
    def _changed_path_scope_is_limited(cls, record: EvidenceRecord) -> bool:
        return (
            record.data.get("overflow") is True
            or record.data.get("scope_complete") is False
            or (
                bool(cls._changed_paths(record))
                and record.completeness not in {"complete", "complete_for_scope"}
            )
        )

    def _modules(self, records: Sequence[EvidenceRecord]) -> tuple[list[str], list[EvidenceRecord]]:
        modules: set[str] = set()
        evidence: list[EvidenceRecord] = []
        for record in records:
            if not self._changed_path_scope_is_complete(record):
                continue
            app = self._app(record.app_id)
            record_modules: set[str] = set()
            for path, is_generated in self._changed_paths(record):
                if is_generated or self._path_is_ignored(path, app):
                    continue
                if app.module_rules:
                    for rule in app.module_rules:
                        if fnmatchcase(path, rule.pattern):
                            record_modules.add(rule.module)
                            break
                else:
                    record_modules.add(path.split("/", 1)[0])
            if record_modules:
                modules.update(record_modules)
                evidence.append(record)
        return sorted(modules)[:50], evidence

    def contribution_summary(self, identifier: str) -> dict[str, object]:
        contribution = self._resolve_contribution(identifier)
        records = self._records(contribution)
        participation = build_participation_summary(
            self.connection, records, contribution.attestations
        )
        release_ladder = build_release_ladder(
            records,
            participation,
            contribution.attestations,
            self._app(contribution.app_id).production_environments,
            self._app(contribution.app_id).release_tag_patterns,
        )
        contradictions = self._contradictions(contribution, records)
        date_from, date_to = self._date_range(records)
        as_of = max((record.fetched_at for record in records), default=None)
        modules, module_evidence = self._modules(records)
        limited_changed_path_records = [
            record for record in records if self._changed_path_scope_is_limited(record)
        ]
        unsupported_member_ids = self._unsupported_member_ids(contribution, records)
        return {
            "contribution": {
                "id": contribution.id,
                "candidate_id": contribution.candidate_id,
                "app_id": contribution.app_id,
                "title": contribution.title,
                "source_text_is_untrusted": True,
                "title_content_type": "untrusted_source_text",
                "type": contribution.contribution_type,
                "date_from": date_from,
                "date_to": date_to,
            },
            "as_of": as_of,
            "members": [
                {
                    "object_id": record.object_id,
                    "evidence_id": record.evidence_id,
                    "source": record.source,
                    "kind": record.kind,
                    "external_id": record.external_id,
                    "title": record.title,
                    "source_text_is_untrusted": record.title is not None,
                    "title_content_type": (
                        "untrusted_source_text" if record.title is not None else None
                    ),
                    "context_only": record.context_only,
                    "completeness": record.completeness,
                    "availability": record.availability,
                    "availability_evidence_id": record.availability_evidence_id,
                    "availability_reason": record.availability_reason,
                    "availability_observed_at": record.availability_observed_at,
                    "current_complete_evidence": record.is_current,
                }
                for record in records
            ],
            "unsupported_member_ids": unsupported_member_ids,
            "modules": modules,
            "module_evidence_ids": [record.evidence_id for record in module_evidence],
            "participation": participation,
            "release_ladder": release_ladder,
            "source_status": self.source_status(contribution.app_id),
            "contradictions": contradictions,
            "limitations": [
                "Context-only records are not used as implementation or ownership proof.",
                "Source text is untrusted and available only through bounded excerpts.",
                *(
                    [
                        "One or more confirmed members have no authoritative current "
                        "observation; legacy overbroad evidence was not used."
                    ]
                    if unsupported_member_ids
                    else []
                ),
                *(
                    [
                        "One or more changed-path observations are incomplete or truncated; "
                        "their retained paths were not used for module, implementation, or "
                        "affected-scope claims."
                    ]
                    if limited_changed_path_records
                    else []
                ),
            ],
        }

    def _attested_or_unknown(
        self,
        question_id: str,
        question: str,
        contribution: ContributionView,
        claims: set[str],
        missing: str,
        contradictions: Sequence[str] = (),
    ) -> QuestionAnswer:
        attestation = find_attestation(contribution.attestations, claims)
        if attestation:
            return attested_answer(
                question_id, question, attestation, contradictions=contradictions
            )
        return unknown_answer(question_id, question, missing, contradictions=contradictions)

    def _build_questions(
        self,
        contribution: ContributionView,
        records: Sequence[EvidenceRecord],
        summary: dict[str, object],
    ) -> dict[str, list[dict[str, object]]]:
        by_id = {
            question_id: question
            for questions in PHASE4_QUESTIONS.values()
            for question_id, question in questions
        }
        raw_contradictions = summary.get("contradictions", [])
        contradictions = raw_contradictions if isinstance(raw_contradictions, list) else []
        contradiction_ids = sorted(
            {
                str(evidence_id)
                for item in contradictions
                if isinstance(item, dict)
                for evidence_id in item.get("evidence_ids", [])
                if isinstance(evidence_id, str)
            }
        )
        material = [record for record in records if not record.context_only]
        jira = [record for record in records if record.source.casefold() == "jira"]
        authored = []
        participation = summary.get("participation", {})
        if isinstance(participation, dict):
            raw_self = participation.get("self_participations", [])
            authored_ids = {
                str(item.get("object_id"))
                for item in raw_self
                if isinstance(item, dict)
                and isinstance(item.get("categories"), list)
                and "implemented" in item["categories"]
            }
            authored = [record for record in material if record.object_id in authored_ids]
        raw_modules = summary.get("modules", [])
        modules = raw_modules if isinstance(raw_modules, list) else []
        raw_module_evidence = summary.get("module_evidence_ids", [])
        module_evidence_ids = (
            {str(value) for value in raw_module_evidence if isinstance(value, str)}
            if isinstance(raw_module_evidence, list)
            else set()
        )
        module_records = [
            record for record in material if record.evidence_id in module_evidence_ids
        ]
        authored_module_records = [
            record for record in authored if record.evidence_id in module_evidence_ids
        ]
        action_modules, _ = self._modules(authored_module_records)
        date_from, date_to = self._date_range(records)

        answers: dict[str, QuestionAnswer] = {}
        if material:
            answers["identity.what"] = supported_answer(
                "identity.what",
                by_id["identity.what"],
                f"Evidence is grouped under the reviewed title: {contribution.title}.",
                material,
                status=(
                    ClaimStatus.HUMAN_ATTESTED
                    if contribution.decision_evidence_ids
                    else ClaimStatus.PARTIALLY_SUPPORTED
                ),
                observation_types=(ObservationType.DERIVED,),
                limitations=(
                    "The title is a candidate suggestion unless confirmed by a decision.",
                ),
            )
            app = self._app(contribution.app_id)
            answers["identity.app_flow"] = supported_answer(
                "identity.app_flow",
                by_id["identity.app_flow"],
                f"The evidence is scoped to the configured application {app.name}.",
                material,
                observation_types=(ObservationType.DERIVED,),
                limitations=("Application scope comes from explicit local configuration.",),
            )
        else:
            answers["identity.what"] = unknown_answer(
                "identity.what",
                by_id["identity.what"],
                "Import or add evidence for this contribution.",
            )
            answers["identity.app_flow"] = unknown_answer(
                "identity.app_flow", by_id["identity.app_flow"], "Add scoped contribution evidence."
            )
        if date_from and date_to and records:
            answers["identity.when"] = supported_answer(
                "identity.when",
                by_id["identity.when"],
                f"Observed evidence spans {date_from} through {date_to}.",
                records,
                observation_types=(ObservationType.DERIVED,),
                limitations=("This is the evidence period, not necessarily the full work period.",),
            )
        else:
            answers["identity.when"] = unknown_answer(
                "identity.when", by_id["identity.when"], "Add dated evidence."
            )
        answers["identity.origin"] = self._attested_or_unknown(
            "identity.origin",
            by_id["identity.origin"],
            contribution,
            {"origin", "work_origin", "assigned_or_proposed"},
            "Confirm whether the work was assigned, proposed, or inherited.",
        )
        answers["identity.ownership"] = self._attested_or_unknown(
            "identity.ownership",
            by_id["identity.ownership"],
            contribution,
            {"ownership", "ownership_statement", "main_owner", "sole_owner"},
            "Review all contributors and add a narrowly worded ownership attestation.",
            contradiction_ids,
        )

        if jira:
            issue = jira[0]
            source_title = issue.title or issue.external_id
            answers["problem.what"] = supported_answer(
                "problem.what",
                by_id["problem.what"],
                f"Jira recorded the work item titled: {source_title}.",
                [issue],
                observation_types=(ObservationType.SOURCE_ASSERTED,),
                limitations=(
                    "This reports Jira's assertion; it does not establish objective severity.",
                ),
            )
        else:
            answers["problem.what"] = unknown_answer(
                "problem.what", by_id["problem.what"], "Add Jira or manual problem evidence."
            )
        for question_id, claims, missing in (
            (
                "problem.before",
                {"problem_before", "prior_behavior"},
                "Document the prior behavior.",
            ),
            ("problem.blocked", {"blocked_flow", "blocked"}, "Confirm what was actually blocked."),
            ("problem.constraints", {"constraints"}, "Document verified constraints."),
            (
                "problem.requirement_clarity",
                {"requirement_clarity", "changing_requirements"},
                "Document requirement changes or ambiguity.",
            ),
        ):
            answers[question_id] = self._attested_or_unknown(
                question_id, by_id[question_id], contribution, claims, missing
            )
        priority_records = [
            record for record in jira if any(key in record.data for key in ("priority", "severity"))
        ]
        if priority_records:
            values = sorted(
                {
                    str(record.data[key])
                    for record in priority_records
                    for key in ("priority", "severity")
                    if key in record.data and record.data[key] not in (None, "")
                }
            )
            answers["problem.severity"] = supported_answer(
                "problem.severity",
                by_id["problem.severity"],
                f"Jira recorded priority or severity values: {', '.join(values)}.",
                priority_records,
                limitations=("Provider labels do not prove objective customer impact.",),
            )
        else:
            answers["problem.severity"] = unknown_answer(
                "problem.severity", by_id["problem.severity"], "Add explicit severity evidence."
            )
        if modules and module_records:
            answers["problem.affected"] = supported_answer(
                "problem.affected",
                by_id["problem.affected"],
                "Changed-path metadata identifies these top-level modules: "
                f"{', '.join(map(str, modules))}.",
                module_records,
                observation_types=(ObservationType.REPOSITORY_OBSERVED,),
                limitations=(
                    "Changed paths do not identify every affected customer or business flow.",
                ),
            )
        else:
            answers["problem.affected"] = unknown_answer(
                "problem.affected",
                by_id["problem.affected"],
                "Add flow or affected-scope evidence.",
            )

        if authored_module_records:
            answers["action.implemented"] = supported_answer(
                "action.implemented",
                by_id["action.implemented"],
                "Repository participation and changed-path evidence identify self-authored "
                f"work in configured modules: {', '.join(action_modules)}.",
                authored_module_records,
                observation_types=(ObservationType.REPOSITORY_OBSERVED,),
                contradictions=contradiction_ids,
                limitations=(
                    "Authorship of these records does not establish ownership of the whole "
                    "feature.",
                ),
            )
        else:
            answers["action.implemented"] = unknown_answer(
                "action.implemented",
                by_id["action.implemented"],
                "Add self-authored implementation evidence with non-ignored changed-path metadata.",
                contradictions=contradiction_ids,
            )
        for question_id, claims, missing in (
            (
                "action.decisions",
                {"technical_decision", "decisions"},
                "Document a specific decision and its evidence.",
            ),
            ("action.tools", {"tools", "frameworks"}, "Confirm tools or frameworks used."),
            ("action.reuse", {"reusable_component", "reuse"}, "Add reuse evidence."),
            (
                "action.architecture",
                {"architecture", "data_flow"},
                "Document an architecture or data-flow change.",
            ),
            (
                "action.quality",
                {"tests_docs_monitoring", "quality_work"},
                "Add test, documentation, or monitoring evidence.",
            ),
        ):
            answers[question_id] = self._attested_or_unknown(
                question_id, by_id[question_id], contribution, claims, missing
            )
        self_participations = []
        if isinstance(participation, dict):
            raw_self = participation.get("self_participations", [])
            if isinstance(raw_self, list):
                self_participations = [item for item in raw_self if isinstance(item, dict)]
        coordination_ids = tuple(
            sorted(
                str(item["participation_evidence_id"])
                for item in self_participations
                if (
                    isinstance(item.get("categories"), list)
                    and any(
                        category in item["categories"]
                        for category in ("reviewed", "assigned", "merged")
                    )
                )
                or item.get("role") == "jira_reporter"
            )
        )
        if coordination_ids:
            answers["action.coordination"] = QuestionAnswer(
                question_id="action.coordination",
                question=by_id["action.coordination"],
                answer_draft=(
                    "Participation evidence records review, assignment, reporting, "
                    "approval, or merge roles."
                ),
                status=ClaimStatus.SUPPORTED,
                observation_types=(ObservationType.SOURCE_ASSERTED,),
                supporting_evidence_ids=coordination_ids,
                limitations=("These roles do not prove implementation ownership.",),
            )
        else:
            answers["action.coordination"] = unknown_answer(
                "action.coordination",
                by_id["action.coordination"],
                "Add coordination or review evidence.",
            )

        ladder = summary.get("release_ladder", {})
        merged_rung = ladder.get("merged", {}) if isinstance(ladder, dict) else {}
        if isinstance(merged_rung, dict) and merged_rung.get("status") != "unknown":
            answers["result.changed"] = QuestionAnswer(
                question_id="result.changed",
                question=by_id["result.changed"],
                answer_draft=str(merged_rung.get("statement")),
                status=(ClaimStatus.CONTRADICTED if contradiction_ids else ClaimStatus.SUPPORTED),
                observation_types=(ObservationType.SOURCE_ASSERTED,),
                supporting_evidence_ids=tuple(
                    str(value) for value in merged_rung.get("supporting_evidence_ids", [])
                ),
                contradicting_evidence_ids=tuple(contradiction_ids),
                limitations=tuple(str(value) for value in merged_rung.get("limitations", [])),
            )
        else:
            answers["result.changed"] = unknown_answer(
                "result.changed",
                by_id["result.changed"],
                "Add merged or otherwise accepted outcome evidence.",
                contradictions=contradiction_ids,
            )
        measured_rung = ladder.get("measurably_successful", {}) if isinstance(ladder, dict) else {}
        if isinstance(measured_rung, dict) and measured_rung.get("status") != "unknown":
            answers["result.measured"] = QuestionAnswer(
                question_id="result.measured",
                question=by_id["result.measured"],
                answer_draft=str(measured_rung.get("statement")),
                status=ClaimStatus(str(measured_rung.get("status"))),
                observation_types=(ObservationType.HUMAN_ATTESTED,),
                supporting_evidence_ids=tuple(
                    str(value) for value in measured_rung.get("supporting_evidence_ids", [])
                ),
                limitations=tuple(str(value) for value in measured_rung.get("limitations", [])),
            )
        else:
            answers["result.measured"] = unknown_answer(
                "result.measured",
                by_id["result.measured"],
                "Add a sourced before-and-after metric.",
            )
        if modules and module_records:
            answers["result.scope"] = supported_answer(
                "result.scope",
                by_id["result.scope"],
                "Changed-path metadata covers these top-level modules: "
                f"{', '.join(map(str, modules))}.",
                module_records,
                observation_types=(ObservationType.REPOSITORY_OBSERVED,),
            )
        else:
            answers["result.scope"] = unknown_answer(
                "result.scope", by_id["result.scope"], "Add changed-path or flow-scope evidence."
            )
        for question_id, claims, missing in (
            (
                "result.efficiency",
                {"efficiency", "error_reduction", "time_reduction"},
                "Add a sourced error or time comparison.",
            ),
            (
                "result.business",
                {"business_outcome", "conversion", "stability"},
                "Add a sourced business or stability result.",
            ),
            ("result.reused", {"reused_later", "reuse"}, "Add a later-reference or reuse record."),
        ):
            answers[question_id] = self._attested_or_unknown(
                question_id, by_id[question_id], contribution, claims, missing
            )
        release_evidence = [
            str(value)
            for value in (
                ladder.get("deployed", {}).get("supporting_evidence_ids", [])
                if isinstance(ladder, dict) and isinstance(ladder.get("deployed"), dict)
                else []
            )
        ]
        if release_evidence:
            answers["result.released"] = QuestionAnswer(
                question_id="result.released",
                question=by_id["result.released"],
                answer_draft=(
                    "Evidence reaches the deployment-observed rung; later user-release "
                    "states remain separate."
                ),
                status=ClaimStatus.PARTIALLY_SUPPORTED,
                observation_types=(ObservationType.SOURCE_ASSERTED,),
                supporting_evidence_ids=tuple(release_evidence),
                limitations=("Deployment does not prove release to mobile users.",),
            )
        else:
            answers["result.released"] = unknown_answer(
                "result.released",
                by_id["result.released"],
                "Add explicit deployment or user-release evidence.",
            )
        current_attestation = find_attestation(
            contribution.attestations, {"currently_enabled", "currently_used", "current_use"}
        )
        answers["result.current_use"] = (
            attested_answer("result.current_use", by_id["result.current_use"], current_attestation)
            if current_attestation
            else unknown_answer(
                "result.current_use",
                by_id["result.current_use"],
                "Add current-use or feature-enable evidence.",
            )
        )
        feedback_records = [
            record
            for record in records
            if record.source.casefold() == "manual"
            and str(record.data.get("kind", record.kind)).casefold()
            in {"client_feedback", "stakeholder_feedback"}
        ]
        if feedback_records:
            answers["result.feedback"] = supported_answer(
                "result.feedback",
                by_id["result.feedback"],
                "Manual evidence records client or stakeholder feedback.",
                feedback_records,
                status=ClaimStatus.HUMAN_ATTESTED,
                observation_types=(ObservationType.HUMAN_ATTESTED,),
                limitations=("The underlying communication was not imported as a primary source.",),
            )
        else:
            answers["result.feedback"] = unknown_answer(
                "result.feedback",
                by_id["result.feedback"],
                "Add a named feedback source or attestation.",
            )
        answers["result.interview_defensible"] = unknown_answer(
            "result.interview_defensible",
            by_id["result.interview_defensible"],
            "Use the defensibility breakdown; WorkTrace does not collapse it to a boolean.",
        )

        return {
            section: [answers[question_id].as_dict() for question_id, _ in questions]
            for section, questions in PHASE4_QUESTIONS.items()
        }

    def build_packet(self, identifier: str) -> dict[str, object]:
        contribution = self._resolve_contribution(identifier)
        records = self._records(contribution)
        summary = self.contribution_summary(identifier)
        sections = self._build_questions(contribution, records, summary)
        statuses = [
            (str(question["question_id"]), str(question["status"]))
            for questions in sections.values()
            for question in questions
        ]
        packet: dict[str, object] = {
            "contribution": summary["contribution"],
            "as_of": summary["as_of"],
            "source_status": summary["source_status"],
            "evidence_summary": {
                "members": summary["members"],
                "unsupported_member_ids": summary["unsupported_member_ids"],
                "modules": summary["modules"],
                "module_evidence_ids": summary["module_evidence_ids"],
                "contradictions": summary["contradictions"],
            },
            "sections": sections,
            "participation": summary["participation"],
            "release_ladder": summary["release_ladder"],
            "contradictions": summary["contradictions"],
            "defensibility": {
                "well_supported_question_ids": [
                    question_id
                    for question_id, status in statuses
                    if status in {"supported", "human_attested"}
                ],
                "partially_supported_question_ids": [
                    question_id
                    for question_id, status in statuses
                    if status == "partially_supported"
                ],
                "missing_or_unresolved_question_ids": [
                    question_id
                    for question_id, status in statuses
                    if status in {"unknown", "unresolved", "contradicted"}
                ],
                "boolean_verdict": None,
            },
            "limitations": summary["limitations"],
        }
        return packet

    def evidence_gaps(self, identifier: str) -> dict[str, object]:
        return build_gap_report(self.build_packet(identifier))

    def list_candidates(
        self,
        app_id: str,
        *,
        date_from: str | None,
        date_to: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, object]:
        self._app(app_id)
        rows = list(
            self.connection.execute(
                """
                SELECT * FROM candidate_groups WHERE app_id=?
                ORDER BY generated_at DESC, id
                """,
                (app_id,),
            )
        )
        visible_items: list[dict[str, object]] = []
        for row in rows:
            try:
                projected: CandidateView = project_candidate(self.connection, str(row["id"]))
                candidate = self._candidate(str(row["id"]))
            except NotFound:
                continue
            if candidate is None or projected.status == "ignored":
                continue
            records = self._records(candidate)
            if not records:
                continue
            period_from, period_to = self._date_range(records)
            period_from_date = _calendar_date(period_from)
            period_to_date = _calendar_date(period_to)
            if date_from and period_to_date and period_to_date < date_from:
                continue
            if date_to and period_from_date and period_from_date > date_to:
                continue
            coverage = sorted({record.source for record in records})
            raw_roles = build_participation_summary(self.connection, records, []).get(
                "self_participations", []
            )
            roles = raw_roles if isinstance(raw_roles, list) else []
            indicators = sorted(
                {
                    str(item.get("role"))
                    for item in roles
                    if isinstance(item, dict) and item.get("role")
                }
            )
            confirmed = next(
                (
                    str(payload["contribution_id"])
                    for _, action, target, payload, _ in self._active_decisions()
                    if action == "confirm_candidate"
                    and target == candidate.id
                    and isinstance(payload.get("contribution_id"), str)
                ),
                None,
            )
            visible_items.append(
                {
                    "candidate_id": candidate.id,
                    "confirmed_contribution_id": confirmed,
                    "title": candidate.title,
                    "source_text_is_untrusted": True,
                    "title_content_type": "untrusted_source_text",
                    "period_from": period_from,
                    "period_to": period_to,
                    "suggested_type": candidate.contribution_type,
                    "status": projected.status,
                    "source_coverage": coverage,
                    "participation_indicators": indicators,
                    "warnings": [
                        "Candidate grouping is deterministic derived state, not contribution truth."
                    ],
                }
            )
        items = visible_items[offset : offset + limit]
        as_of_row = self.connection.execute(
            f"""
            WITH {authoritative_current_observation_ctes()}
            SELECT MAX(current.fetched_at)
            FROM authoritative_current_observations current
            JOIN source_objects object ON object.id=current.source_object_id
            WHERE object.app_id=?
            """,
            (app_id,),
        ).fetchone()
        as_of = str(as_of_row[0]) if as_of_row is not None and as_of_row[0] else None
        return {
            "app_id": app_id,
            "as_of": as_of,
            "source_status": self.source_status(app_id),
            "candidates": items,
            "next_offset": offset + limit if len(visible_items) > offset + limit else None,
        }

    def search_evidence(
        self,
        query: str,
        app_id: str,
        *,
        source_types: Sequence[str],
        actor_id: str | None,
        module: str | None,
        date_from: str | None,
        date_to: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, object]:
        self._app(app_id)
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses = [
            "latest.position=1",
            "so.app_id=?",
            "(lower(COALESCE(latest.title,'')) LIKE lower(?) ESCAPE '\\' "
            "OR lower(COALESCE(latest.body_text,'')) LIKE lower(?) ESCAPE '\\' "
            "OR lower(latest.data_json) LIKE lower(?) ESCAPE '\\')",
        ]
        parameters: list[object] = [app_id, f"%{escaped}%", f"%{escaped}%", f"%{escaped}%"]
        if source_types:
            clauses.append(f"so.source IN ({','.join('?' for _ in source_types)})")
            parameters.extend(source_types)
        if actor_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM authoritative_current_participations p "
                "WHERE p.observation_id=latest.id AND p.actor_id=?)"
            )
            parameters.append(actor_id)
        if module:
            escaped_module = module.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("lower(latest.data_json) LIKE lower(?) ESCAPE '\\'")
            parameters.append(f"%{escaped_module}%")
        if date_from:
            clauses.append("date(COALESCE(latest.source_updated_at, latest.fetched_at)) >= date(?)")
            parameters.append(date_from)
        if date_to:
            clauses.append("date(COALESCE(latest.source_updated_at, latest.fetched_at)) <= date(?)")
            parameters.append(date_to)
        parameters.extend((limit + 1, offset))
        rows = list(
            self.connection.execute(
                f"""
                WITH {authoritative_current_participation_ctes()}
                SELECT latest.*, so.source, so.source_instance, so.kind, so.external_id
                FROM authoritative_current_observations latest
                JOIN source_objects so ON so.id=latest.source_object_id
                WHERE {" AND ".join(clauses)}
                ORDER BY COALESCE(latest.source_updated_at, latest.fetched_at) DESC, latest.id
                LIMIT ? OFFSET ?
                """,
                parameters,
            )
        )
        results = []
        for row in rows[:limit]:
            text = str(row["body_text"] or row["title"] or "")[:DEFAULT_EXCERPT_CHARS]
            results.append(
                {
                    "evidence_id": str(row["id"]),
                    "object_id": str(row["source_object_id"]),
                    "source": str(row["source"]),
                    "source_instance": str(row["source_instance"]),
                    "kind": str(row["kind"]),
                    "external_id": str(row["external_id"]),
                    "title": row["title"],
                    "content_type": "untrusted_source_excerpt",
                    "source_text_is_untrusted": True,
                    "text": text,
                    "completeness": str(row["completeness"]),
                    "observed_at": str(row["fetched_at"]),
                }
            )
        return {
            "app_id": app_id,
            "as_of": max((str(row["fetched_at"]) for row in rows), default=None),
            "source_status": self.source_status(app_id),
            "results": results,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def evidence_excerpt(self, evidence_id: str, max_chars: int) -> dict[str, object]:
        row = self.connection.execute(
            f"""
            WITH {authoritative_current_observation_ctes()}
            SELECT o.*, so.app_id, so.source, so.source_instance, so.kind, so.external_id,
                   sr.status AS run_status, sr.completeness AS run_completeness
            FROM authoritative_current_observations o
            JOIN source_objects so ON so.id=o.source_object_id
            JOIN sync_runs sr ON sr.id=o.sync_run_id
            WHERE o.id=? OR so.id=?
            ORDER BY CASE WHEN o.id=? THEN 0 ELSE 1 END, o.fetched_at DESC, o.id DESC
            LIMIT 1
            """,
            (evidence_id, evidence_id, evidence_id),
        ).fetchone()
        if row is not None:
            app_id = str(row["app_id"])
            self._app(app_id)
            text = str(row["body_text"] or row["title"] or "")
            if not text:
                text = json.dumps(_parse_json_object(row["data_json"]), ensure_ascii=False)
            return {
                "evidence_id": str(row["id"]),
                "object_id": str(row["source_object_id"]),
                "app_id": app_id,
                "source": str(row["source"]),
                "source_instance": str(row["source_instance"]),
                "kind": str(row["kind"]),
                "external_id": str(row["external_id"]),
                "content_type": "untrusted_source_excerpt",
                "source_text_is_untrusted": True,
                "text": text[:max_chars],
                "truncated": len(text) > max_chars,
                "as_of": str(row["fetched_at"]),
                "completeness": str(row["completeness"]),
                "run_status": str(row["run_status"]),
                "run_completeness": str(row["run_completeness"]),
                "authoritative_current": True,
                "source_status": self.source_status(app_id),
            }
        participation = self.connection.execute(
            f"""
            WITH {authoritative_current_participation_ctes()}
            SELECT p.*, a.display_name, a.is_self, so.app_id, so.source, so.kind,
                   so.external_id, sr.status AS run_status,
                   sr.completeness AS run_completeness
            FROM authoritative_current_participations p
            JOIN actors a ON a.id=p.actor_id
            JOIN source_objects so ON so.id=p.source_object_id
            JOIN authoritative_current_observations o ON o.id=p.observation_id
            JOIN sync_runs sr ON sr.id=o.sync_run_id
            WHERE p.id=?
            """,
            (evidence_id,),
        ).fetchone()
        if participation is not None:
            app_id = str(participation["app_id"])
            self._app(app_id)
            return {
                "evidence_id": evidence_id,
                "app_id": app_id,
                "content_type": "participation_evidence",
                "source": str(participation["source"]),
                "kind": str(participation["kind"]),
                "external_id": str(participation["external_id"]),
                "role": str(participation["role"]),
                "actor_display_name": str(participation["display_name"]),
                "is_self": bool(participation["is_self"]),
                "effective_from": participation["effective_from"],
                "effective_to": participation["effective_to"],
                "run_status": str(participation["run_status"]),
                "run_completeness": str(participation["run_completeness"]),
                "authoritative_current": True,
                "source_status": self.source_status(app_id),
            }
        availability = self.connection.execute(
            f"""
            WITH {authoritative_current_availability_ctes()}
            SELECT event.*, object.app_id, object.source, object.source_instance,
                   object.kind, object.external_id, run.status AS run_status,
                   run.completeness AS run_completeness
            FROM authoritative_current_availability_events event
            JOIN source_objects object ON object.id=event.source_object_id
            JOIN sync_runs run ON run.id=event.sync_run_id
            WHERE event.id=?
            """,
            (evidence_id,),
        ).fetchone()
        if availability is not None:
            app_id = str(availability["app_id"])
            self._app(app_id)
            return {
                "evidence_id": evidence_id,
                "object_id": str(availability["source_object_id"]),
                "app_id": app_id,
                "content_type": "availability_evidence",
                "source": str(availability["source"]),
                "source_instance": str(availability["source_instance"]),
                "kind": str(availability["kind"]),
                "external_id": str(availability["external_id"]),
                "state": str(availability["state"]),
                "reason": str(availability["reason"]),
                "observed_at": str(availability["observed_at"]),
                "as_of": str(availability["observed_at"]),
                "run_status": str(availability["run_status"]),
                "run_completeness": str(availability["run_completeness"]),
                "authoritative_current": True,
                "source_status": self.source_status(app_id),
            }
        decision = self.connection.execute(
            "SELECT * FROM human_decisions WHERE id=?", (evidence_id,)
        ).fetchone()
        if decision is not None:
            target_id = str(decision["target_id"])
            try:
                app_id = self._resolve_contribution(target_id).app_id
            except NotFound:
                scoped_target = self.connection.execute(
                    """
                    SELECT app_id FROM source_objects WHERE id=?
                    UNION
                    SELECT so.app_id FROM observations o
                    JOIN source_objects so ON so.id=o.source_object_id
                    WHERE o.id=?
                    """,
                    (target_id, target_id),
                ).fetchone()
                if scoped_target is None:
                    raise ScopeViolation(
                        "manual evidence has no configured application scope"
                    ) from None
                app_id = str(scoped_target["app_id"])
                self._app(app_id)
            payload = _parse_json_object(decision["payload_json"])
            statement = payload.get("statement", payload.get("reason", ""))
            text = str(statement)[:max_chars]
            return {
                "evidence_id": evidence_id,
                "app_id": app_id,
                "content_type": "untrusted_source_excerpt",
                "source_text_is_untrusted": True,
                "source": "manual",
                "kind": str(decision["action"]),
                "text": text,
                "truncated": len(str(statement)) > max_chars,
                "as_of": str(decision["created_at"]),
                "completeness": "human_attested",
                "source_status": self.source_status(app_id),
            }
        reference = self.connection.execute(
            f"""
            WITH {authoritative_current_reference_ctes()}
            SELECT * FROM authoritative_current_references WHERE id=?
            """,
            (evidence_id,),
        ).fetchone()
        if reference is not None:
            app_id = str(reference["app_id"])
            self._app(app_id)
            return {
                "evidence_id": evidence_id,
                "app_id": app_id,
                "content_type": "typed_reference_evidence",
                "relationship_type": str(reference["relationship_type"]),
                "from_object_id": str(reference["from_object_id"]),
                "to_object_id": str(reference["to_object_id"]),
                "extraction_method": str(reference["extraction_method"]),
                "supporting_observation_id": reference["supporting_observation_id"],
                "authoritative_current": True,
                "source_status": self.source_status(app_id),
            }
        raise NotFound(f"evidence not found: {evidence_id}")


def build_phase4_packet(
    connection: sqlite3.Connection,
    contribution_id: str,
    config: WorkTraceConfig,
) -> dict[str, object]:
    """Build a Phase 4 packet through the same read model used by MCP."""

    return PacketBuilder(connection, config).build_packet(contribution_id)
