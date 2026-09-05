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
from worktrace.db.authority import (
    authoritative_current_observations,
    authoritative_current_run_ctes,
)
from worktrace.db.read_state import mark_read_states_changed
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
        from worktrace.identity import IDENTITY_POLICY_VERSION, identity_fingerprint

        with self.connection:
            for app in config.apps:
                existing = self.connection.execute(
                    "SELECT name, market, business_type FROM apps WHERE id=?", (app.id,)
                ).fetchone()
                is_new = existing is None
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
                if existing is not None and (
                    str(existing["name"]) != app.name
                    or str(existing["market"]) != app.market
                    or str(existing["business_type"]) != app.business_type
                ):
                    mark_read_states_changed(self.connection, [app.id])
                if is_new:
                    self.connection.execute(
                        "INSERT INTO app_identity_policy(app_id, version, fingerprint) "
                        "VALUES (?, ?, ?)",
                        (app.id, IDENTITY_POLICY_VERSION, identity_fingerprint(config)),
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
            mark_read_states_changed(self.connection, [app.id])
        return session_id

    def update_import_session_progress(
        self, session_id: str, summary: dict[str, JsonValue]
    ) -> None:
        """Persist visible import-session progress with its app revision atomically."""
        owns_transaction = self.connection.autocommit is False or not self.connection.in_transaction
        if owns_transaction and self.connection.autocommit is True:
            self.connection.execute("BEGIN")
        try:
            row = self.connection.execute(
                "SELECT app_id FROM import_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise DatabaseError("import session not found")
            self.connection.execute(
                "UPDATE import_sessions SET summary_json=? WHERE id=?",
                (json.dumps(summary, sort_keys=True), session_id),
            )
            mark_read_states_changed(self.connection, [str(row["app_id"])])
        except BaseException:
            if owns_transaction and self.connection.in_transaction:
                self.connection.rollback()
            raise
        else:
            if owns_transaction and self.connection.in_transaction:
                self.connection.commit()

    def finish_import_session(
        self, session_id: str, status: str, summary: dict[str, JsonValue]
    ) -> None:
        owns_transaction = self.connection.autocommit is False or not self.connection.in_transaction
        if owns_transaction and self.connection.autocommit is True:
            self.connection.execute("BEGIN")
        try:
            row = self.connection.execute(
                "SELECT app_id FROM import_sessions WHERE id=?", (session_id,)
            ).fetchone()
            self.connection.execute(
                "UPDATE import_sessions SET status=?, completed_at=?, summary_json=? WHERE id=?",
                (status, iso(utc_now()), json.dumps(summary, sort_keys=True), session_id),
            )
            if row is not None:
                mark_read_states_changed(self.connection, [str(row["app_id"])])
        except BaseException:
            if owns_transaction and self.connection.in_transaction:
                self.connection.rollback()
            raise
        else:
            if owns_transaction and self.connection.in_transaction:
                self.connection.commit()

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
            mark_read_states_changed(self.connection, [app_id])
        return run_id

    def update_run_progress(self, run_id: str, progress: dict[str, JsonValue]) -> None:
        self.connection.execute(
            "UPDATE sync_runs SET progress_json=? WHERE id=?",
            (json.dumps(progress, sort_keys=True), run_id),
        )
        mark_read_states_changed(
            self.connection,
            [
                str(row["app_id"])
                for row in self.connection.execute(
                    "SELECT app_id FROM sync_runs WHERE id=?", (run_id,)
                )
            ],
        )

    def finish_sync_run(
        self,
        run_id: str,
        status: str,
        completeness: str,
        error_summary: str | None = None,
    ) -> None:
        owns_transaction = self.connection.autocommit is False or not self.connection.in_transaction
        if owns_transaction and self.connection.autocommit is True:
            self.connection.execute("BEGIN")
        try:
            row = self.connection.execute(
                "SELECT app_id FROM sync_runs WHERE id=?", (run_id,)
            ).fetchone()
            self.connection.execute(
                """
                UPDATE sync_runs
                SET status=?, completeness=?, completed_at=?, error_summary=?
                WHERE id=?
                """,
                (status, completeness, iso(utc_now()), error_summary, run_id),
            )
            self._project_authoritative_availability(run_id)
            if row is not None:
                mark_read_states_changed(self.connection, [str(row["app_id"])])
        except BaseException:
            if owns_transaction and self.connection.in_transaction:
                self.connection.rollback()
            raise
        else:
            if owns_transaction and self.connection.in_transaction:
                self.connection.commit()

    def _project_authoritative_availability(self, run_id: str) -> None:
        """Reconcile affected objects from eligible events only.

        Running this for every finalization also restores the prior eligible projection when
        a failed, partial, or legacy remote run staged an availability event.
        """

        self.connection.execute(
            f"""
            WITH {authoritative_current_run_ctes()},
            affected_objects AS (
                SELECT DISTINCT source_object_id
                FROM source_object_availability_events
                WHERE sync_run_id=?
            ), ranked_events AS (
                SELECT event.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY event.source_object_id
                        ORDER BY eligible_run.completed_at DESC,
                                 event.observed_at DESC, event.id DESC
                    ) AS position
                FROM source_object_availability_events event
                JOIN ranked_authoritative_runs eligible_run
                  ON eligible_run.id=event.sync_run_id
                JOIN source_objects authority_object
                  ON authority_object.id=event.source_object_id
                 AND authority_object.app_id=eligible_run.app_id
                 AND authority_object.source=eligible_run.source
                 AND authority_object.source_instance=eligible_run.source_instance
                JOIN affected_objects affected
                  ON affected.source_object_id=event.source_object_id
            ), current_events AS (
                SELECT * FROM ranked_events WHERE position=1
            )
            UPDATE source_objects
            SET availability = (
                    SELECT event.state FROM current_events event
                    WHERE event.source_object_id=source_objects.id
                ),
                availability_reason = (
                    SELECT event.reason FROM current_events event
                    WHERE event.source_object_id=source_objects.id
                ),
                availability_observed_at = (
                    SELECT event.observed_at FROM current_events event
                    WHERE event.source_object_id=source_objects.id
                )
            WHERE id IN (SELECT source_object_id FROM affected_objects)
              AND EXISTS (
                  SELECT 1 FROM current_events event
                  WHERE event.source_object_id=source_objects.id
              )
            """,
            (run_id,),
        )

    def mark_stale_runs_failed(self, older_than: timedelta = timedelta(hours=6)) -> int:
        cutoff = iso(utc_now() - older_than)
        with self.connection:
            affected_apps = [
                str(row["app_id"])
                for row in self.connection.execute(
                    "SELECT DISTINCT app_id FROM sync_runs "
                    "WHERE status='running' AND started_at < ?",
                    (cutoff,),
                )
            ]
            cursor = self.connection.execute(
                """
                UPDATE sync_runs SET status='running_stale', completed_at=?,
                    error_summary='process ended before run completion', completeness='partial'
                WHERE status='running' AND started_at < ?
                """,
                (iso(utc_now()), cutoff),
            )
            mark_read_states_changed(self.connection, affected_apps)
        return cursor.rowcount

    def store_page(
        self,
        run_id: str,
        objects: Iterable[NormalizedObject],
        progress: dict[str, JsonValue] | None = None,
        unavailable_objects: Iterable[AvailabilityObservation] = (),
    ) -> list[str]:
        observation_ids: list[str] = []
        page_objects = tuple(objects)
        page_unavailable = tuple(unavailable_objects)
        try:
            with self.connection:
                run = self.connection.execute(
                    "SELECT app_id, source, source_instance, status FROM sync_runs WHERE id=?",
                    (run_id,),
                ).fetchone()
                if run is None or str(run["status"]) != "running":
                    raise DatabaseError("page requires a running sync run")
                for item in page_objects:
                    if item.app_id != str(run["app_id"]):
                        raise DatabaseError("source page escaped its configured application scope")
                    if (
                        item.identity.source != str(run["source"])
                        or item.identity.source_instance != str(run["source_instance"])
                        or any(
                            actor.source != str(run["source"])
                            or actor.source_instance != str(run["source_instance"])
                            for actor in item.actors
                        )
                    ):
                        raise DatabaseError(
                            "source page escaped its configured sync-run source scope"
                        )
                for unavailable in page_unavailable:
                    if unavailable.source != str(
                        run["source"]
                    ) or unavailable.source_instance != str(run["source_instance"]):
                        raise DatabaseError(
                            "source page escaped its configured sync-run source scope"
                        )
                page_actor_ids: set[str] = set()
                for item in page_objects:
                    observation_id, actor_ids = self._store_object(run_id, item)
                    observation_ids.append(observation_id)
                    page_actor_ids.update(actor_ids)
                for unavailable in page_unavailable:
                    self._record_object_unavailable(run_id, unavailable)
                if progress is not None:
                    self.update_run_progress(run_id, progress)
                actor_apps: list[str] = []
                if page_actor_ids:
                    placeholders = ",".join("?" for _ in page_actor_ids)
                    actor_apps = [
                        str(row["app_id"])
                        for row in self.connection.execute(
                            f"""
                            SELECT DISTINCT object.app_id
                            FROM participations participation
                            JOIN source_objects object
                              ON object.id=participation.source_object_id
                            WHERE participation.actor_id IN ({placeholders})
                            """,
                            tuple(sorted(page_actor_ids)),
                        )
                    ]
                mark_read_states_changed(self.connection, [str(run["app_id"]), *actor_apps])
        except sqlite3.Error as exc:
            raise DatabaseError("failed to persist source page") from exc
        return observation_ids

    def _store_actor(self, actor: ActorObservation) -> str:
        redactor = self.redactor or Redactor(b"worktrace-validation-only")
        external_actor_id = (
            redactor.hash_email(actor.external_actor_id)
            if "@" in actor.external_actor_id
            else redactor.protect_identifier(
                actor.external_actor_id,
                namespace=f"actor:{actor.source}:{actor.source_instance}",
            )
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
        existing = self.connection.execute(
            "SELECT is_self, email_hash, identity_policy_version FROM actors "
            "WHERE source=? AND source_instance=? AND external_actor_id=?",
            (actor.source, actor.source_instance, external_actor_id),
        ).fetchone()
        if existing is not None and (
            int(existing["identity_policy_version"]) != 1
            or bool(existing["is_self"]) != actor.is_self
            or (
                existing["email_hash"] is not None
                and email_hash is not None
                and existing["email_hash"] != email_hash
            )
        ):
            raise DatabaseError(
                "identity_reconciliation_required: source pages cannot change accepted identity"
            )
        self.connection.execute(
            """
            INSERT INTO actors(
                id, source, source_instance, external_actor_id, display_name, email_hash, is_self,
                identity_policy_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(source, source_instance, external_actor_id) DO UPDATE SET
                display_name=excluded.display_name
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

    def _store_object(self, run_id: str, item: NormalizedObject) -> tuple[str, set[str]]:
        identity = item.identity
        validation_redactor = self.redactor or Redactor(b"worktrace-validation-only")
        external_id = validation_redactor.protect_identifier(
            identity.external_id,
            namespace=f"object:{identity.source}:{identity.kind}",
        )
        canonical_url: str | None = None
        if identity.canonical_url is not None:
            redacted_url = validation_redactor.redact_payload(
                identity.canonical_url,
                field_name="url",
            )
            if not isinstance(redacted_url, str):
                raise DatabaseError("canonical URL redaction produced an invalid value")
            canonical_url = redacted_url
        if self.redactor is None and (
            external_id != identity.external_id or canonical_url != identity.canonical_url
        ):
            raise DatabaseError("redaction is required before source identity persistence")
        object_id = stable_id(
            "obj",
            item.app_id,
            identity.source,
            identity.source_instance,
            identity.kind,
            external_id,
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
                external_id,
                canonical_url,
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
                external_id,
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
        pending_references: list[JsonValue] = []
        for reference in item.pending_references:
            persisted_reference: dict[str, JsonValue] = {
                "target_source": validation_redactor.redact_text(reference.target_source),
                "target_kind": validation_redactor.redact_text(reference.target_kind),
                "target_external_id": validation_redactor.protect_identifier(
                    reference.target_external_id,
                    namespace=f"object:{reference.target_source}:{reference.target_kind}",
                ),
                "relationship_type": validation_redactor.redact_text(reference.relationship_type),
                "extraction_method": validation_redactor.redact_text(reference.extraction_method),
                "exact_value": (
                    validation_redactor.redact_text(reference.exact_value)
                    if reference.exact_value is not None
                    else None
                ),
            }
            if self.redactor is None:
                raw_reference = {
                    "target_source": reference.target_source,
                    "target_kind": reference.target_kind,
                    "target_external_id": reference.target_external_id,
                    "relationship_type": reference.relationship_type,
                    "extraction_method": reference.extraction_method,
                    "exact_value": reference.exact_value,
                }
                if persisted_reference != raw_reference:
                    raise DatabaseError("redaction is required before reference persistence")
            pending_references.append(persisted_reference)
        persisted_data["_pending_references"] = pending_references
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
        return observation_id, set(actors.values())

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
            object_id = self._record_object_unavailable(
                run_id,
                AvailabilityObservation(
                    source=source,
                    source_instance=source_instance,
                    kind=kind,
                    external_id=external_id,
                    reason=reason,
                ),
            )
            row = self.connection.execute(
                "SELECT app_id FROM sync_runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is not None:
                mark_read_states_changed(self.connection, [str(row["app_id"])])
            return object_id

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
            raise DatabaseError("availability event escaped its sync-run source scope")
        validation_redactor = self.redactor or Redactor(b"worktrace-validation-only")
        external_id = validation_redactor.protect_identifier(
            unavailable.external_id,
            namespace=f"object:{unavailable.source}:{unavailable.kind}",
        )
        if self.redactor is None and external_id != unavailable.external_id:
            raise DatabaseError("redaction is required before source identity persistence")
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
                external_id,
            ),
        ).fetchone()
        if row is None:
            object_id = stable_id(
                "obj",
                str(run["app_id"]),
                unavailable.source,
                unavailable.source_instance,
                unavailable.kind,
                external_id,
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
                    external_id,
                    run_id,
                    run_id,
                ),
            )
        else:
            object_id = str(row["id"])
        return self._append_availability_event(run_id, object_id, "unavailable", unavailable.reason)

    def current_observations(self, app_id: str) -> list[sqlite3.Row]:
        return authoritative_current_observations(self.connection, app_id)

    def object_row(self, object_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM source_objects WHERE id=?", (object_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"evidence object not found: {object_id}")
        return cast(sqlite3.Row, row)
