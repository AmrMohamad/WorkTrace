from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import cast

from worktrace.config import AppConfig, WorkTraceConfig
from worktrace.constants import ADAPTER_VERSION, NORMALIZATION_VERSION, REDACTION_VERSION
from worktrace.domain.models import (
    ActorObservation,
    AvailabilityObservation,
    JsonValue,
    NormalizedObject,
)
from worktrace.errors import DatabaseError, NotFound
from worktrace.normalize.redaction import Redactor


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("naive datetime is not permitted")
        return value.astimezone(UTC).isoformat()
    return value.isoformat()


def stable_id(prefix: str, *parts: object) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    return f"{prefix}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]}"


def payload_hash(title: str | None, body: str | None, data: dict[str, JsonValue]) -> str:
    payload = json.dumps(
        {"title": title, "body": body, "data": data},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EvidenceRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        redactor: Redactor | None = None,
    ) -> None:
        self.connection = connection
        self.redactor = redactor

    def ensure_apps(self, config: WorkTraceConfig) -> None:
        with self.connection:
            for app in config.apps:
                self.connection.execute(
                    """
                    INSERT INTO apps(id, name, market, business_type)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        market=excluded.market,
                        business_type=excluded.business_type
                    """,
                    (app.id, app.name, app.market, app.business_type),
                )

    def create_import_session(self, app: AppConfig, date_from: date, date_to: date) -> str:
        session_id = f"import:{uuid.uuid4()}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO import_sessions(id, app_id, status, started_at, date_from, date_to)
                VALUES (?, ?, 'running', ?, ?, ?)
                """,
                (session_id, app.id, iso(utc_now()), iso(date_from), iso(date_to)),
            )
        return session_id

    def finish_import_session(
        self, session_id: str, status: str, summary: dict[str, JsonValue]
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE import_sessions SET status=?, completed_at=?, summary_json=? WHERE id=?",
                (status, iso(utc_now()), json.dumps(summary, sort_keys=True), session_id),
            )

    def start_sync_run(
        self,
        app_id: str,
        source: str,
        source_instance: str,
        scope: dict[str, JsonValue],
        import_session_id: str | None = None,
    ) -> str:
        run_id = f"sync:{uuid.uuid4()}"
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sync_runs(
                    id, import_session_id, app_id, source, source_instance, status,
                    started_at, adapter_version, scope_json
                ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    run_id,
                    import_session_id,
                    app_id,
                    source,
                    source_instance,
                    iso(utc_now()),
                    ADAPTER_VERSION,
                    json.dumps(scope, sort_keys=True),
                ),
            )
        return run_id

    def update_run_progress(self, run_id: str, progress: dict[str, JsonValue]) -> None:
        self.connection.execute(
            "UPDATE sync_runs SET progress_json=? WHERE id=?",
            (json.dumps(progress, sort_keys=True), run_id),
        )

    def finish_sync_run(
        self,
        run_id: str,
        status: str,
        completeness: str,
        error_summary: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE sync_runs
                SET status=?, completeness=?, completed_at=?, error_summary=?
                WHERE id=?
                """,
                (status, completeness, iso(utc_now()), error_summary, run_id),
            )
            if status == "complete":
                self.connection.execute(
                    """
                    UPDATE source_objects
                    SET availability = (
                            SELECT event.state
                            FROM source_object_availability_events event
                            WHERE event.source_object_id=source_objects.id
                              AND event.sync_run_id=?
                            ORDER BY event.observed_at DESC, event.id DESC LIMIT 1
                        ),
                        availability_reason = (
                            SELECT event.reason
                            FROM source_object_availability_events event
                            WHERE event.source_object_id=source_objects.id
                              AND event.sync_run_id=?
                            ORDER BY event.observed_at DESC, event.id DESC LIMIT 1
                        ),
                        availability_observed_at = (
                            SELECT event.observed_at
                            FROM source_object_availability_events event
                            WHERE event.source_object_id=source_objects.id
                              AND event.sync_run_id=?
                            ORDER BY event.observed_at DESC, event.id DESC LIMIT 1
                        )
                    WHERE EXISTS (
                        SELECT 1 FROM source_object_availability_events event
                        WHERE event.source_object_id=source_objects.id
                          AND event.sync_run_id=?
                    )
                    """,
                    (run_id, run_id, run_id, run_id),
                )

    def mark_stale_runs_failed(self, older_than: timedelta = timedelta(hours=6)) -> int:
        cutoff = iso(utc_now() - older_than)
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE sync_runs SET status='running_stale', completed_at=?,
                    error_summary='process ended before run completion', completeness='partial'
                WHERE status='running' AND started_at < ?
                """,
                (iso(utc_now()), cutoff),
            )
        return cursor.rowcount

    def store_page(
        self,
        run_id: str,
        objects: Iterable[NormalizedObject],
        progress: dict[str, JsonValue] | None = None,
        unavailable_objects: Iterable[AvailabilityObservation] = (),
    ) -> list[str]:
        observation_ids: list[str] = []
        try:
            with self.connection:
                run = self.connection.execute(
                    "SELECT app_id, status FROM sync_runs WHERE id=?", (run_id,)
                ).fetchone()
                if run is None or str(run["status"]) != "running":
                    raise DatabaseError("page requires a running sync run")
                for item in objects:
                    if item.app_id != str(run["app_id"]):
                        raise DatabaseError("source page escaped its configured application scope")
                    observation_ids.append(self._store_object(run_id, item))
                for unavailable in unavailable_objects:
                    self._record_object_unavailable(run_id, unavailable)
                if progress is not None:
                    self.update_run_progress(run_id, progress)
        except sqlite3.Error as exc:
            raise DatabaseError("failed to persist source page") from exc
        return observation_ids

    def _store_actor(self, actor: ActorObservation) -> str:
        redactor = self.redactor or Redactor(b"worktrace-validation-only")
        external_actor_id = (
            redactor.hash_email(actor.external_actor_id)
            if "@" in actor.external_actor_id
            else actor.external_actor_id
        )
        display_name = redactor.redact_text(actor.display_name)
        email_hash = actor.email_hash
        if email_hash and not email_hash.startswith("email_hmac_sha256:"):
            email_hash = (
                redactor.hash_email(email_hash)
                if "@" in email_hash
                else redactor.redact_text(email_hash)
            )
        if self.redactor is None and (
            external_actor_id != actor.external_actor_id
            or display_name != actor.display_name
            or email_hash != actor.email_hash
        ):
            raise DatabaseError("redaction is required before actor persistence")
        actor_id = stable_id("actor", actor.source, actor.source_instance, external_actor_id)
        self.connection.execute(
            """
            INSERT INTO actors(
                id, source, source_instance, external_actor_id, display_name, email_hash, is_self
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, source_instance, external_actor_id) DO UPDATE SET
                display_name=excluded.display_name,
                email_hash=COALESCE(excluded.email_hash, actors.email_hash),
                is_self=MAX(actors.is_self, excluded.is_self)
            """,
            (
                actor_id,
                actor.source,
                actor.source_instance,
                external_actor_id,
                display_name,
                email_hash,
                int(actor.is_self),
            ),
        )
        row = self.connection.execute(
            "SELECT id FROM actors WHERE source=? AND source_instance=? AND external_actor_id=?",
            (actor.source, actor.source_instance, external_actor_id),
        ).fetchone()
        assert row is not None
        return str(row["id"])

    def _store_object(self, run_id: str, item: NormalizedObject) -> str:
        identity = item.identity
        object_id = stable_id(
            "obj",
            item.app_id,
            identity.source,
            identity.source_instance,
            identity.kind,
            identity.external_id,
        )
        self.connection.execute(
            """
            INSERT INTO source_objects(
                id, app_id, source, source_instance, kind, external_id,
                canonical_url, first_seen_run_id, last_seen_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(app_id, source, source_instance, kind, external_id) DO UPDATE SET
                canonical_url=COALESCE(excluded.canonical_url, source_objects.canonical_url),
                last_seen_run_id=excluded.last_seen_run_id
            """,
            (
                object_id,
                item.app_id,
                identity.source,
                identity.source_instance,
                identity.kind,
                identity.external_id,
                identity.canonical_url,
                run_id,
                run_id,
            ),
        )
        row = self.connection.execute(
            """
            SELECT id FROM source_objects
            WHERE app_id=? AND source=? AND source_instance=? AND kind=? AND external_id=?
            """,
            (
                item.app_id,
                identity.source,
                identity.source_instance,
                identity.kind,
                identity.external_id,
            ),
        ).fetchone()
        assert row is not None
        object_id = str(row["id"])
        previous = self.connection.execute(
            "SELECT availability FROM source_objects WHERE id=?", (object_id,)
        ).fetchone()
        self._append_availability_event(
            run_id,
            object_id,
            "visible",
            "reappeared"
            if previous and str(previous["availability"]) == "unavailable"
            else "observed",
        )

        validation_redactor = self.redactor or Redactor(b"worktrace-validation-only")
        if self.redactor is None:
            validated_title = (
                validation_redactor.redact_text(item.title) if item.title else item.title
            )
            validated_body = (
                validation_redactor.redact_text(item.body_text)
                if item.body_text
                else item.body_text
            )
            validated_data = validation_redactor.redact_payload(item.data)
            if (
                validated_title != item.title
                or validated_body != item.body_text
                or validated_data != item.data
            ):
                raise DatabaseError("redaction is required before evidence persistence")
        title = (
            self.redactor.redact_text(item.title) if self.redactor and item.title else item.title
        )
        body_text = (
            self.redactor.redact_text(item.body_text)
            if self.redactor and item.body_text
            else item.body_text
        )
        raw_data = self.redactor.redact_payload(item.data) if self.redactor else dict(item.data)
        if not isinstance(raw_data, dict):
            raise DatabaseError("evidence payload must be an object")
        persisted_data = raw_data
        persisted_data["_pending_references"] = [
            {
                "target_source": reference.target_source,
                "target_kind": reference.target_kind,
                "target_external_id": reference.target_external_id,
                "relationship_type": reference.relationship_type,
                "extraction_method": reference.extraction_method,
                "exact_value": reference.exact_value,
            }
            for reference in item.pending_references
        ]
        digest = payload_hash(title, body_text, persisted_data)
        observation_id = stable_id("obs", object_id, run_id, digest)
        self.connection.execute(
            """
            INSERT OR IGNORE INTO observations(
                id, source_object_id, sync_run_id, source_updated_at, fetched_at,
                payload_hash, title, body_text, data_json, completeness,
                adapter_version, normalization_version, redaction_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                object_id,
                run_id,
                iso(item.source_updated_at),
                iso(utc_now()),
                digest,
                title,
                body_text,
                json.dumps(persisted_data, ensure_ascii=False, sort_keys=True),
                item.completeness.value,
                ADAPTER_VERSION,
                NORMALIZATION_VERSION,
                REDACTION_VERSION,
            ),
        )

        actors = {actor.external_actor_id: self._store_actor(actor) for actor in item.actors}
        for participation in item.participations:
            actor_id = actors.get(participation.actor_external_id)
            if actor_id is None:
                raise DatabaseError(
                    f"participation references unknown actor: {participation.actor_external_id}"
                )
            participation_id = stable_id(
                "part",
                object_id,
                observation_id,
                actor_id,
                participation.role,
                iso(participation.effective_from),
                iso(participation.effective_to),
            )
            self.connection.execute(
                """
                INSERT OR IGNORE INTO participations(
                    id, source_object_id, observation_id, actor_id, role,
                    effective_from, effective_to, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    participation_id,
                    object_id,
                    observation_id,
                    actor_id,
                    participation.role,
                    iso(participation.effective_from),
                    iso(participation.effective_to),
                    json.dumps(participation.details, sort_keys=True),
                ),
            )
        return observation_id

    def _append_availability_event(
        self,
        run_id: str,
        object_id: str,
        state: str,
        reason: str,
    ) -> str:
        if state not in {"visible", "unavailable"}:
            raise DatabaseError("invalid availability state")
        observed_at = iso(utc_now())
        assert observed_at is not None
        event_id = stable_id("availability", object_id, run_id, state, reason)
        self.connection.execute(
            """
            INSERT OR IGNORE INTO source_object_availability_events(
                id, source_object_id, sync_run_id, state, reason, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (event_id, object_id, run_id, state, reason, observed_at),
        )
        return event_id

    def record_object_unavailable(
        self,
        run_id: str,
        *,
        source: str,
        source_instance: str,
        kind: str,
        external_id: str,
        reason: str = "not_found",
    ) -> str:
        """Stage an exact-object availability transition for a running scoped run."""

        with self.connection:
            return self._record_object_unavailable(
                run_id,
                AvailabilityObservation(
                    source=source,
                    source_instance=source_instance,
                    kind=kind,
                    external_id=external_id,
                    reason=reason,
                ),
            )

    def _record_object_unavailable(self, run_id: str, unavailable: AvailabilityObservation) -> str:
        run = self.connection.execute(
            "SELECT app_id, source, source_instance, status FROM sync_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run is None or str(run["status"]) != "running":
            raise DatabaseError("availability event requires a running sync run")
        if (
            str(run["source"]) != unavailable.source
            or str(run["source_instance"]) != unavailable.source_instance
        ):
            raise DatabaseError("availability event escaped its source scope")
        row = self.connection.execute(
            """
            SELECT id FROM source_objects
            WHERE app_id=? AND source=? AND source_instance=? AND kind=? AND external_id=?
            """,
            (
                str(run["app_id"]),
                unavailable.source,
                unavailable.source_instance,
                unavailable.kind,
                unavailable.external_id,
            ),
        ).fetchone()
        if row is None:
            object_id = stable_id(
                "obj",
                str(run["app_id"]),
                unavailable.source,
                unavailable.source_instance,
                unavailable.kind,
                unavailable.external_id,
            )
            self.connection.execute(
                """
                INSERT INTO source_objects(
                    id, app_id, source, source_instance, kind, external_id,
                    canonical_url, first_seen_run_id, last_seen_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    object_id,
                    str(run["app_id"]),
                    unavailable.source,
                    unavailable.source_instance,
                    unavailable.kind,
                    unavailable.external_id,
                    run_id,
                    run_id,
                ),
            )
        else:
            object_id = str(row["id"])
        return self._append_availability_event(run_id, object_id, "unavailable", unavailable.reason)

    def current_observations(self, app_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                WITH current_runs AS (
                    SELECT id FROM (
                        SELECT id, source,
                            ROW_NUMBER() OVER (
                                PARTITION BY app_id, source, source_instance
                                ORDER BY completed_at DESC, id DESC
                            ) AS position
                        FROM sync_runs
                        WHERE app_id=? AND status='complete'
                          AND (
                            source NOT IN ('jira', 'gitlab')
                            OR CAST(
                                COALESCE(
                                    json_extract(scope_json, '$.selection_policy_version'), 0
                                ) AS INTEGER
                            ) >= 2
                            OR json_extract(scope_json, '$.date_from') IS NULL
                          )
                    ) WHERE position=1 OR source='manual'
                ), latest AS (
                    SELECT o.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY o.source_object_id ORDER BY o.fetched_at DESC, o.id DESC
                        ) AS position
                    FROM observations o JOIN current_runs r ON r.id=o.sync_run_id
                )
                SELECT latest.*, so.app_id, so.source, so.source_instance, so.kind,
                    so.external_id, so.canonical_url, so.availability,
                    so.availability_reason, so.availability_observed_at
                FROM latest JOIN source_objects so ON so.id=latest.source_object_id
                WHERE latest.position=1
                ORDER BY so.source, so.kind, so.external_id
                """,
                (app_id,),
            )
        )

    def object_row(self, object_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM source_objects WHERE id=?", (object_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"evidence object not found: {object_id}")
        return cast(sqlite3.Row, row)
