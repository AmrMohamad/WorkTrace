from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from worktrace.adapters.base import ActorIdentity, NormalizedRecord, SnapshotAdapter
from worktrace.adapters.jira import JiraAdapter
from worktrace.config import AppConfig
from worktrace.db.repository import EvidenceRepository
from worktrace.domain.enums import Completeness
from worktrace.domain.models import (
    ActorObservation,
    AvailabilityObservation,
    JsonValue,
    NormalizedObject,
    ParticipationObservation,
    PendingReference,
    SourceIdentity,
)
from worktrace.errors import SourceError
from worktrace.importers.jira_staging import jira_pages
from worktrace.participation import canonical_role


@dataclass(frozen=True)
class ImportResult:
    session_id: str
    run_id: str
    status: str
    pages: int
    records: int
    error: str | None = None
    completeness: str = Completeness.COMPLETE.value
    limitations: tuple[str, ...] = ()
    selection_events: tuple[dict[str, JsonValue], ...] = field(default_factory=tuple)


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _role(source: str, object_type: str, value: str) -> str:
    return canonical_role(source, _kind(source, object_type), value)


def _kind(source: str, object_type: str) -> str:
    aliases = {
        ("git", "commit"): "git_commit",
        ("git", "ref"): "git_tag",
        ("jira", "issue"): "jira_issue",
        ("gitlab", "merge_request"): "gitlab_mr",
        ("gitlab", "deployment"): "git_deployment",
        ("gitlab", "release"): "gitlab_release",
        ("gitlab", "discussion"): "gitlab_discussion",
    }
    return aliases.get((source, object_type), f"{source}_{object_type}")


def _is_self_actor(
    source: str, object_type: str, actor: ActorIdentity, self_actor_ids: set[str]
) -> bool:
    if source == "git":
        return actor.email_hash is not None and actor.email_hash in self_actor_ids
    if source == "gitlab":
        if actor.source_actor_id.isascii() and actor.source_actor_id.isdecimal():
            return int(actor.source_actor_id) > 0 and actor.source_actor_id in self_actor_ids
        # Only email-identified commit actors may use configured/verified aliases.
        # A different provider user ID must never be overridden by its email.
        return (
            object_type == "merge_request_commit"
            and actor.email_hash is not None
            and actor.source_actor_id == actor.email_hash
            and actor.email_hash in self_actor_ids
        )
    return actor.source_actor_id in self_actor_ids


def record_to_object(
    record: NormalizedRecord,
    self_actor_ids: set[str],
    self_display_names: set[str] | None = None,
    *,
    completeness: Completeness = Completeness.COMPLETE,
) -> NormalizedObject:
    # Kept for existing adapter callers; names are display data, never identity evidence.
    del self_display_names
    identity = record.identity
    payload = dict(record.payload)
    payload["_untrusted_text_fields"] = list(record.untrusted_text_fields)
    normalized_kind = _kind(identity.source_kind, identity.object_type)
    if identity.source_kind == "git" and identity.object_type == "ref":
        normalized_kind = "git_tag" if payload.get("ref_kind") == "tag" else "git_branch"
    title = next(
        (
            str(payload[key])
            for key in ("title", "summary", "subject", "name", "ref_name")
            if isinstance(payload.get(key), str)
        ),
        None,
    )
    body = next(
        (
            str(payload[key])
            for key in ("description", "body", "comment", "notes")
            if isinstance(payload.get(key), str)
        ),
        None,
    )
    actors: dict[str, ActorObservation] = {}
    participations: list[ParticipationObservation] = []
    for item in record.participations:
        actor = item.actor
        actors.setdefault(
            actor.source_actor_id,
            ActorObservation(
                source=identity.source_kind,
                source_instance=identity.source_instance,
                external_actor_id=actor.source_actor_id,
                display_name=actor.display_name or actor.username or "Unknown source actor",
                email_hash=actor.email_hash,
                is_self=_is_self_actor(
                    identity.source_kind, identity.object_type, actor, self_actor_ids
                ),
            ),
        )
        participations.append(
            ParticipationObservation(
                actor_external_id=actor.source_actor_id,
                role=_role(identity.source_kind, identity.object_type, item.role.value),
                effective_from=_timestamp(item.effective_from),
                effective_to=_timestamp(item.effective_to),
            )
        )
    references = tuple(
        PendingReference(
            target_source=reference.target_source_kind or identity.source_kind,
            target_kind=_kind(
                reference.target_source_kind or identity.source_kind,
                reference.target_object_type or "unknown",
            ),
            target_external_id=reference.target_external_id,
            relationship_type={
                "jira_key_mention": "mentions_jira_key",
                "git_ref_target": "tag_points_to_commit",
                "git_parent": "git_parent_of",
            }.get(reference.reference_type, reference.reference_type),
            extraction_method=reference.strength.value,
            exact_value=reference.target_external_id,
        )
        for reference in record.references
    )
    return NormalizedObject(
        identity=SourceIdentity(
            source=identity.source_kind,
            source_instance=identity.source_instance,
            kind=normalized_kind,
            external_id=identity.external_id,
            canonical_url=(
                str(payload["web_url"]) if isinstance(payload.get("web_url"), str) else None
            ),
        ),
        app_id=identity.app_id,
        title=title,
        body_text=body,
        source_updated_at=_timestamp(record.observation.source_updated_at),
        actors=tuple(actors.values()),
        participations=tuple(participations),
        pending_references=references,
        data=payload,
        completeness=completeness,
    )


def import_snapshot(
    app: AppConfig,
    adapter: SnapshotAdapter | JiraAdapter,
    repository: EvidenceRepository,
    *,
    source: str,
    source_instance: str,
    date_from: date,
    date_to: date,
    self_actor_ids: set[str] | None = None,
    self_display_names: set[str] | None = None,
    import_session_id: str | None = None,
    finish_session: bool = True,
    scope_details: dict[str, JsonValue] | None = None,
) -> ImportResult:
    """Persist a full snapshot one page per transaction.

    A run only becomes current after every adapter page completes. Failed runs
    remain inspectable while the preceding complete run stays queryable.
    """
    if date_from > date_to:
        raise ValueError("date_from must not be after date_to")
    session_id = import_session_id or repository.create_import_session(app, date_from, date_to)
    scope: dict[str, JsonValue] = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "selection_policy_version": 2,
    }
    if scope_details:
        scope.update(scope_details)
    if isinstance(adapter, JiraAdapter):
        scope.update(adapter.import_scope())
    run_id = repository.start_sync_run(
        app.id,
        source,
        source_instance,
        scope,
        session_id,
    )
    pages = 0
    records = 0
    selection_biased = scope.get("selection_biased") is True
    raw_limitations = scope.get("limitations")
    limitations = (
        [value for value in raw_limitations if isinstance(value, str) and value]
        if isinstance(raw_limitations, list)
        else []
    )
    raw_selection_events = scope.get("selection_events")
    selection_events = (
        [dict(value) for value in raw_selection_events if isinstance(value, dict)]
        if isinstance(raw_selection_events, list)
        else []
    )
    if limitations or selection_events:
        selection_biased = True
    try:
        pages_iterator = (
            jira_pages(adapter, repository, run_id)
            if isinstance(adapter, JiraAdapter)
            else adapter.iter_pages()
        )
        for page in pages_iterator:
            if page.source_kind != source or page.source_instance != source_instance:
                raise SourceError("adapter page escaped its configured source scope")
            if any(
                record.identity.source_kind != source
                or record.identity.source_instance != source_instance
                for record in page.records
            ):
                raise SourceError("adapter record escaped its configured source scope")
            if any(record.identity.app_id != app.id for record in page.records):
                raise SourceError("adapter record escaped its configured application scope")
            for limitation in page.limitations:
                if limitation and limitation not in limitations:
                    limitations.append(limitation)
            for event in page.selection_events:
                normalized_event = dict(event)
                if normalized_event not in selection_events:
                    selection_events.append(normalized_event)
            if page.limitations or page.selection_events:
                selection_biased = True
            record_completeness = (
                Completeness.SELECTION_BIASED
                if page.records_selection_biased
                else Completeness.COMPLETE
            )
            normalized = [
                record_to_object(
                    record,
                    self_actor_ids or set(),
                    self_display_names,
                    completeness=record_completeness,
                )
                for record in page.records
            ]
            progress: dict[str, JsonValue] = {
                "pages": pages + 1,
                "records": records + len(normalized),
                "resource_type": page.resource_type,
                "cursor": page.next_cursor,
            }
            if selection_biased:
                progress["selection_biased"] = True
            if limitations:
                progress["limitations"] = list(limitations)
            if selection_events:
                progress["selection_events"] = list(selection_events)
            repository.store_page(
                run_id,
                normalized,
                progress,
                unavailable_objects=(
                    AvailabilityObservation(
                        source=page.source_kind,
                        source_instance=page.source_instance,
                        kind=descriptor.kind,
                        external_id=descriptor.external_id,
                        reason=descriptor.reason,
                    )
                    for descriptor in page.unavailable_objects
                ),
            )
            pages += 1
            records += len(normalized)
    except Exception as exc:
        message = str(exc)[:500] or type(exc).__name__
        repository.finish_sync_run(run_id, "failed", Completeness.PARTIAL.value, message)
        if finish_session:
            repository.finish_import_session(
                session_id,
                "partial",
                {"source": source, "pages": pages, "records": records, "error": message},
            )
        return ImportResult(
            session_id,
            run_id,
            "partial",
            pages,
            records,
            message,
            completeness=Completeness.PARTIAL.value,
            limitations=tuple(limitations),
            selection_events=tuple(selection_events),
        )
    completeness = (
        Completeness.SELECTION_BIASED.value if selection_biased else Completeness.COMPLETE.value
    )
    repository.finish_sync_run(run_id, "complete", completeness)
    if isinstance(adapter, JiraAdapter):
        with repository.connection:
            repository.connection.execute("DELETE FROM jira_import_stage WHERE run_id=?", (run_id,))
    if finish_session:
        repository.finish_import_session(
            session_id,
            "complete",
            {"source": source, "pages": pages, "records": records},
        )
    return ImportResult(
        session_id,
        run_id,
        "complete",
        pages,
        records,
        completeness=completeness,
        limitations=tuple(limitations),
        selection_events=tuple(selection_events),
    )
