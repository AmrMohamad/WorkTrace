from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from worktrace.config import WorkTraceConfig, load_config
from worktrace.constants import DEFAULT_EXCERPT_CHARS
from worktrace.db.connection import connect_read_only
from worktrace.mcp_server.limits import enforce_total_limit
from worktrace.mcp_server.schemas import app_id as validate_app_id
from worktrace.mcp_server.schemas import (
    bounded_limit,
    decode_cursor,
    encode_cursor,
    excerpt_limit,
    iso_date,
    optional_filter,
    query_text,
    stable_id,
)
from worktrace.mcp_server.schemas import source_types as validate_source_types
from worktrace.packets.builder import PacketBuilder


class WorkTraceTools:
    """Six bounded read operations; every call opens SQLite in read-only mode."""

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        database_path: Path | None = None,
        config: WorkTraceConfig | None = None,
    ) -> None:
        self._config_path = config_path
        self._database_path = database_path
        self._config = config

    def _load_config(self) -> WorkTraceConfig:
        return self._config or load_config(self._config_path)

    @contextmanager
    def _builder(self) -> Iterator[PacketBuilder]:
        config = self._load_config()
        path = (self._database_path or config.database_path).expanduser().resolve()
        connection = connect_read_only(path)
        try:
            yield PacketBuilder(connection, config)
        finally:
            connection.close()

    @staticmethod
    def _bounded_response(result: dict[str, object]) -> dict[str, object]:
        result["source_text_trust"] = "untrusted"
        result["source_text_is_untrusted"] = True
        return enforce_total_limit(result)

    @classmethod
    def _cursor_response(cls, result: dict[str, object]) -> dict[str, object]:
        next_offset = result.pop("next_offset", None)
        result["next_cursor"] = encode_cursor(next_offset)
        return cls._bounded_response(result)

    def list_contribution_candidates(
        self,
        *,
        app_id: str,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, object]:
        validated_app = validate_app_id(app_id)
        with self._builder() as builder:
            result = builder.list_candidates(
                validated_app,
                date_from=iso_date(date_from, "date_from"),
                date_to=iso_date(date_to, "date_to"),
                limit=bounded_limit(limit),
                offset=decode_cursor(cursor),
            )
        return self._cursor_response(result)

    def get_contribution_summary(self, *, contribution_id: str) -> dict[str, object]:
        identifier = stable_id(contribution_id, "contribution_id")
        with self._builder() as builder:
            result = builder.contribution_summary(identifier)
        return self._bounded_response(result)

    def build_phase4_packet(self, *, contribution_id: str) -> dict[str, object]:
        identifier = stable_id(contribution_id, "contribution_id")
        with self._builder() as builder:
            result = builder.build_packet(identifier)
        return self._bounded_response(result)

    def list_evidence_gaps(self, *, contribution_id: str) -> dict[str, object]:
        identifier = stable_id(contribution_id, "contribution_id")
        with self._builder() as builder:
            result = builder.evidence_gaps(identifier)
        return self._bounded_response(result)

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
    ) -> dict[str, object]:
        validated_actor = stable_id(actor_id, "actor_id") if actor_id is not None else None
        with self._builder() as builder:
            result = builder.search_evidence(
                query_text(query),
                validate_app_id(app_id),
                source_types=validate_source_types(source_types),
                actor_id=validated_actor,
                module=optional_filter(module, "module"),
                date_from=iso_date(date_from, "date_from"),
                date_to=iso_date(date_to, "date_to"),
                limit=bounded_limit(limit),
                offset=decode_cursor(cursor),
            )
        return self._cursor_response(result)

    def get_evidence_excerpt(
        self,
        *,
        evidence_id: str,
        max_chars: int = DEFAULT_EXCERPT_CHARS,
    ) -> dict[str, object]:
        identifier = stable_id(evidence_id, "evidence_id")
        with self._builder() as builder:
            result = builder.evidence_excerpt(identifier, excerpt_limit(max_chars))
        return self._bounded_response(result)


def assert_read_only(connection: sqlite3.Connection) -> None:
    """Small integration oracle for callers that inject a connection in tests."""

    query_only = connection.execute("PRAGMA query_only").fetchone()
    if query_only is None or int(query_only[0]) != 1:
        raise RuntimeError("WorkTrace MCP requires a query-only SQLite connection")
