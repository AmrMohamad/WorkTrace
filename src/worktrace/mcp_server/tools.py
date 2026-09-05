from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import cast

from worktrace.candidates.decisions import decision_scope_map
from worktrace.config import WorkTraceConfig, load_config
from worktrace.constants import DEFAULT_EXCERPT_CHARS
from worktrace.db.authority import authoritative_current_observation_ctes
from worktrace.db.connection import connect_read_only
from worktrace.db.read_state import READ_MODEL_VERSION, read_snapshot
from worktrace.db.readiness import DatabaseReadinessStatus, database_readiness
from worktrace.errors import DatabaseError, NotFound, ScopeViolation
from worktrace.mcp_server.protocol import (
    ProtocolError,
    check_expected,
    decode_cursor,
    encode_cursor,
    fingerprint,
    view_token,
)
from worktrace.mcp_server.responses import admit_context, admit_page, bounded_response, shape_packet
from worktrace.mcp_server.schemas import app_id as validate_app_id
from worktrace.mcp_server.schemas import (
    bounded_limit,
    excerpt_limit,
    iso_date,
    optional_filter,
    query_text,
    stable_id,
)
from worktrace.mcp_server.schemas import source_types as validate_source_types
from worktrace.packets.builder import PacketBuilder
from worktrace.packets.schema import PHASE4_QUESTIONS
from worktrace.read_models.agent_pages import scan_candidates, scan_evidence
from worktrace.read_models.evidence_context import (
    context_readiness,
    describe_object,
    scan_memberships,
    scan_relations,
)


def _protocol_result[**P](
    function: Callable[P, dict[str, object]],
) -> Callable[P, dict[str, object]]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> dict[str, object]:
        try:
            return function(*args, **kwargs)
        except ProtocolError as exc:
            return exc.response()

    return wrapped


def _dates(first: str | None, last: str | None) -> tuple[str | None, str | None]:
    first, last = iso_date(first, "date_from"), iso_date(last, "date_to")
    if first and last and first > last:
        raise ScopeViolation("date_from must not be after date_to")
    return first, last


def _ascii_lower(value: str | None) -> str | None:
    return (
        value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
        if value
        else value
    )


class WorkTraceTools:
    """Seven SQLite-only operations with short snapshots and bound continuations."""

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        database_path: Path | None = None,
        config: WorkTraceConfig | None = None,
    ) -> None:
        self._config_path, self._database_path, self._config = config_path, database_path, config
        self._epoch = secrets.token_hex(16)

    def _load_config(self) -> WorkTraceConfig:
        return self._config or load_config(self._config_path)

    @contextmanager
    def _builder(self) -> Iterator[PacketBuilder]:
        config = self._load_config()
        connection = connect_read_only(
            (self._database_path or config.database_path).expanduser().resolve()
        )
        try:
            with read_snapshot(connection):
                if database_readiness(connection).status is not DatabaseReadinessStatus.READY:
                    raise DatabaseError(
                        "unsupported database schema; use the matching CLI to upgrade"
                    )
                yield PacketBuilder(connection, config)
        finally:
            connection.close()

    def _metadata(
        self, builder: PacketBuilder, app_id: str, expected: str | None
    ) -> dict[str, object]:
        token = view_token(builder, app_id, self._epoch)
        check_expected(expected, token)
        return {
            "app_id": app_id,
            "view_token": token,
            "read_model_version": READ_MODEL_VERSION,
            "source_text_trust": "untrusted",
            "source_text_is_untrusted": True,
        }

    @staticmethod
    def _page_envelope(
        builder: PacketBuilder, meta: dict[str, object], first: str | None, last: str | None
    ) -> dict[str, object]:
        app_id = str(meta["app_id"])
        row = builder.connection.execute(
            f"WITH {authoritative_current_observation_ctes()} "
            "SELECT MAX(o.fetched_at) FROM authoritative_current_observations o "
            "JOIN source_objects so ON so.id=o.source_object_id WHERE so.app_id=?",
            (app_id,),
        ).fetchone()
        return {
            **meta,
            "as_of": row[0] if row else None,
            "source_status": builder.source_status(app_id),
            "date_filter_policy": "undated_excluded" if first or last else "undated_included",
        }

    @staticmethod
    def _evidence_app(builder: PacketBuilder, identifier: str) -> str:
        rows = builder.connection.execute(
            "SELECT so.app_id FROM observations o "
            "JOIN source_objects so ON so.id=o.source_object_id "
            'WHERE o.id=? UNION SELECT app_id FROM "references" WHERE id=? '
            "UNION SELECT app_id FROM source_objects WHERE id=? "
            "UNION SELECT so.app_id FROM participations p JOIN source_objects so "
            "ON so.id=p.source_object_id WHERE p.id=? "
            "UNION SELECT so.app_id FROM source_object_availability_events e "
            "JOIN source_objects so ON so.id=e.source_object_id WHERE e.id=?",
            (identifier,) * 5,
        ).fetchall()
        if len(rows) > 1:
            raise ScopeViolation("evidence has ambiguous application scope")
        result = (
            str(rows[0][0])
            if rows
            else decision_scope_map(builder.connection, active_only=False).get(identifier)
        )
        if result is None:
            if builder.connection.execute(
                "SELECT 1 FROM human_decisions WHERE id=?", (identifier,)
            ).fetchone():
                raise ScopeViolation("manual evidence has no configured application scope")
            raise NotFound(f"evidence not found: {identifier}")
        builder.config.app(result)
        return result

    @_protocol_result
    def list_contribution_candidates(
        self,
        *,
        app_id: str,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        expected_view_token: str | None = None,
    ) -> dict[str, object]:
        app_id, limit = validate_app_id(app_id), bounded_limit(limit)
        first, last = _dates(date_from, date_to)
        filters = fingerprint({"date_from": first, "date_to": last})
        with self._builder() as builder:
            meta = self._metadata(builder, app_id, expected_view_token)
            view = str(meta["view_token"])
            decoded = decode_cursor(
                cursor, collection="candidates", app_id=app_id, view=view, filters=filters
            )
            position = cast(dict[str, str], decoded["position"]) if decoded else None
            generation, rows = scan_candidates(
                builder,
                app_id,
                after=position["candidate_id"] if position else None,
                date_from=first,
                date_to=last,
            )
            if decoded and decoded["generation"] != generation:
                raise ProtocolError(
                    "evidence_changed",
                    "Candidate generation changed; restart the investigation.",
                    view_token=view,
                )
            return admit_page(
                self._page_envelope(builder, meta, first, last),
                rows,
                item_key="candidates",
                limit=limit,
                initial_position=position,
                make_cursor=lambda p: encode_cursor(
                    collection="candidates",
                    app_id=app_id,
                    view=view,
                    filters=filters,
                    position=p,
                    generation=generation,
                ),
            )

    @_protocol_result
    def search_evidence(
        self,
        *,
        query: str,
        app_id: str,
        source_types: list[str] | None = None,
        actor_id: str | None = None,
        module: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        expected_view_token: str | None = None,
    ) -> dict[str, object]:
        app_id, query, limit = validate_app_id(app_id), query_text(query), bounded_limit(limit)
        sources = tuple(sorted(validate_source_types(source_types)))
        actor = stable_id(actor_id, "actor_id") if actor_id is not None else None
        module = optional_filter(module, "module")
        first, last = _dates(date_from, date_to)
        filters = fingerprint(
            {
                "query": _ascii_lower(query),
                "sources": sources,
                "actor": actor,
                "module": _ascii_lower(module),
                "date_from": first,
                "date_to": last,
            }
        )
        with self._builder() as builder:
            meta = self._metadata(builder, app_id, expected_view_token)
            view = str(meta["view_token"])
            decoded = decode_cursor(
                cursor, collection="evidence", app_id=app_id, view=view, filters=filters
            )
            position = cast(dict[str, str], decoded["position"]) if decoded else None
            rows = scan_evidence(
                builder,
                query,
                app_id,
                source_types=sources,
                actor_id=actor,
                module=module,
                date_from=first,
                date_to=last,
                after=(position["sort_time"], position["observation_id"]) if position else None,
            )
            return admit_page(
                self._page_envelope(builder, meta, first, last),
                rows,
                item_key="results",
                limit=limit,
                initial_position=position,
                make_cursor=lambda p: encode_cursor(
                    collection="evidence", app_id=app_id, view=view, filters=filters, position=p
                ),
            )

    @_protocol_result
    def get_contribution_summary(
        self, *, contribution_id: str, expected_view_token: str | None = None
    ) -> dict[str, object]:
        identifier = stable_id(contribution_id, "contribution_id")
        with self._builder() as builder:
            app_id = builder._resolve_contribution(identifier).app_id
            meta = self._metadata(builder, app_id, expected_view_token)
            return bounded_response({**builder.contribution_summary(identifier), **meta})

    @_protocol_result
    def list_evidence_gaps(
        self, *, contribution_id: str, expected_view_token: str | None = None
    ) -> dict[str, object]:
        identifier = stable_id(contribution_id, "contribution_id")
        with self._builder() as builder:
            app_id = builder._resolve_contribution(identifier).app_id
            meta = self._metadata(builder, app_id, expected_view_token)
            return bounded_response({**builder.evidence_gaps(identifier), **meta})

    @_protocol_result
    def get_evidence_excerpt(
        self,
        *,
        evidence_id: str,
        max_chars: int = DEFAULT_EXCERPT_CHARS,
        expected_view_token: str | None = None,
    ) -> dict[str, object]:
        identifier, size = stable_id(evidence_id, "evidence_id"), excerpt_limit(max_chars)
        with self._builder() as builder:
            app_id = self._evidence_app(builder, identifier)
            meta = self._metadata(builder, app_id, expected_view_token)
            return bounded_response({**builder.evidence_excerpt(identifier, size), **meta})

    @_protocol_result
    def build_phase4_packet(
        self,
        *,
        contribution_id: str,
        expected_view_token: str | None = None,
        section: str | None = None,
        question_id: str | None = None,
        detail_cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, object]:
        identifier, limit = stable_id(contribution_id, "contribution_id"), bounded_limit(limit)
        if section is not None and question_id is not None:
            raise ScopeViolation("section and question_id are mutually exclusive")
        if section is not None and section not in {s.section for s in PHASE4_QUESTIONS}:
            raise ScopeViolation("section is not a canonical Phase 4 section")
        if question_id is not None and question_id not in {s.question_id for s in PHASE4_QUESTIONS}:
            raise ScopeViolation("question_id is not a canonical Phase 4 question")
        if detail_cursor is not None and section is None and question_id is None:
            raise ScopeViolation("detail_cursor requires a section or question_id")
        filters = fingerprint(
            {"contribution_id": identifier, "section": section, "question_id": question_id}
        )
        with self._builder() as builder:
            app_id = builder._resolve_contribution(identifier).app_id
            meta = self._metadata(builder, app_id, expected_view_token)
            view = str(meta["view_token"])
            decoded = decode_cursor(
                detail_cursor,
                collection="packet_details",
                app_id=app_id,
                view=view,
                filters=filters,
            )
            position = cast(dict[str, str], decoded["position"]) if decoded else None
            return shape_packet(
                {**builder.build_packet(identifier), **meta},
                section=section,
                question_id=question_id,
                after=position,
                limit=limit,
                make_cursor=lambda p: encode_cursor(
                    collection="packet_details",
                    app_id=app_id,
                    view=view,
                    filters=filters,
                    position=p,
                ),
            )

    @_protocol_result
    def get_evidence_context(
        self,
        *,
        app_id: str,
        object_id: str,
        relation_cursor: str | None = None,
        membership_cursor: str | None = None,
        limit: int = 10,
        expected_view_token: str | None = None,
    ) -> dict[str, object]:
        app_id, object_id, limit = (
            validate_app_id(app_id),
            stable_id(object_id, "object_id"),
            bounded_limit(limit),
        )
        if len(object_id) > 256:
            raise ScopeViolation("object_id is limited to 256 characters")
        object_binding = fingerprint({"app_id": app_id, "object_id": object_id})
        filters = fingerprint({"object_id": object_id})
        with self._builder() as builder:
            object_description = describe_object(builder, app_id, object_id)
            meta = self._metadata(builder, app_id, expected_view_token)
            view = str(meta["view_token"])
            relation = decode_cursor(
                relation_cursor,
                collection="context_relations",
                app_id=app_id,
                view=view,
                filters=filters,
                object_fingerprint=object_binding,
            )
            membership = decode_cursor(
                membership_cursor,
                collection="context_memberships",
                app_id=app_id,
                view=view,
                filters=filters,
                object_fingerprint=object_binding,
            )
            relation_position = cast(dict[str, str], relation["position"]) if relation else None
            membership_position = (
                cast(dict[str, str], membership["position"]) if membership else None
            )
            relation_rows = (
                scan_relations(
                    builder,
                    app_id,
                    object_id,
                    after=(
                        relation_position["key"]
                        if relation_position and relation_position["phase"] == "after"
                        else None
                    ),
                )
                if relation is not None or membership is None
                else None
            )
            if membership is not None or relation is None:
                generation, membership_rows = scan_memberships(
                    builder,
                    app_id,
                    object_id,
                    after=(
                        membership_position["key"]
                        if membership_position and membership_position["phase"] == "after"
                        else None
                    ),
                )
            else:
                generation, membership_rows = None, None
            if membership is not None and membership["generation"] != generation:
                raise ProtocolError(
                    "evidence_changed",
                    "Membership generation changed; restart the investigation.",
                    view_token=view,
                )

            def cursor(
                collection: str,
                position: dict[str, str] | None,
                generation_token: str | None = None,
            ) -> str:
                return encode_cursor(
                    collection=collection,
                    app_id=app_id,
                    view=view,
                    filters=filters,
                    object_fingerprint=object_binding,
                    generation=generation_token,
                    position=position or {"phase": "start", "key": "-"},
                )

            return admit_context(
                {
                    **self._page_envelope(builder, meta, None, None),
                    "object": object_description,
                    "context_readiness": context_readiness(builder, app_id),
                },
                relation_rows=relation_rows,
                membership_rows=membership_rows,
                limit=limit,
                relation_initial=relation_position,
                membership_initial=membership_position,
                relation_cursor=lambda position: cursor("context_relations", position),
                membership_cursor=lambda position: cursor(
                    "context_memberships", position, generation
                ),
            )


def assert_read_only(connection: sqlite3.Connection) -> None:
    query_only = connection.execute("PRAGMA query_only").fetchone()
    if query_only is None or int(query_only[0]) != 1:
        raise RuntimeError("WorkTrace MCP requires a query-only SQLite connection")
