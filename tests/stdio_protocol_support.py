"""Shared end-to-end MCP stdio assertions for checkout and wheel servers."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from worktrace.packets.schema import PHASE4_QUESTIONS

_TOOL_NAMES = {
    "list_contribution_candidates",
    "get_contribution_summary",
    "build_phase4_packet",
    "list_evidence_gaps",
    "search_evidence",
    "get_evidence_excerpt",
    "get_evidence_context",
}


def import_fixture(fixture: Any) -> None:
    for args in (("init",), ("import", "all", "sample"), ("rebuild", "all", "sample")):
        result = fixture.invoke(*args)
        assert result.exit_code == 0, result.output


def checkout_server(config_path: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=os.environ.get("WORKTRACE_TEST_SERVER_PYTHON", sys.executable),
        args=["-m", "worktrace", "serve-mcp", "--config", str(config_path)],
        cwd=os.environ.get("WORKTRACE_TEST_SERVER_CWD", str(config_path.parent)),
        env={"PYTHONPATH": ""},
    )


def payload(result: object) -> dict[str, object]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", ())
    assert content
    text = getattr(content[0], "text", None)
    assert isinstance(text, str)
    value = json.loads(text)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _items(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [cast(dict[str, object], item) for item in value]


def _stream(page: dict[str, object], name: str) -> dict[str, object]:
    value = page[name]
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


async def _consume_context_stream(
    session: ClientSession,
    *,
    object_id: str,
    view_token: str,
    stream_name: str,
    cursor: str,
) -> None:
    cursor_argument = "relation_cursor" if stream_name == "relations" else "membership_cursor"
    other_stream = "memberships" if stream_name == "relations" else "relations"
    seen = {cursor}
    while cursor:
        assert len(seen) <= 10, f"{stream_name} continuation exceeded the bounded loop"
        page = payload(
            await session.call_tool(
                "get_evidence_context",
                {
                    "app_id": "sample",
                    "object_id": object_id,
                    cursor_argument: cursor,
                    "limit": 1,
                    "expected_view_token": view_token,
                },
            )
        )
        active = _stream(page, stream_name)
        pending = _stream(page, other_stream)
        assert active["requested"] is True
        assert pending["requested"] is False
        _items(active["items"])
        next_cursor = active["next_cursor"]
        assert next_cursor is None or isinstance(next_cursor, str)
        if next_cursor is None:
            return
        if next_cursor in seen:
            pytest.fail(f"{stream_name} continuation repeated a cursor")
        seen.add(next_cursor)
        cursor = next_cursor


async def _consume_packet_details(
    session: ClientSession, contribution_id: str, view_token: str, cursor: str
) -> None:
    seen = {cursor}
    while cursor:
        assert len(seen) <= 10, "packet-detail continuation exceeded the bounded loop"
        page = payload(
            await session.call_tool(
                "build_phase4_packet",
                {
                    "contribution_id": contribution_id,
                    "question_id": "identity.what",
                    "detail_cursor": cursor,
                    "limit": 1,
                    "expected_view_token": view_token,
                },
            )
        )
        assert page["response_mode"] == "question_details"
        _items(page["details"])
        next_cursor = page["detail_cursor"]
        assert next_cursor is None or isinstance(next_cursor, str)
        if next_cursor is None:
            return
        if next_cursor in seen:
            pytest.fail("packet-detail continuation repeated a cursor")
        seen.add(next_cursor)
        cursor = next_cursor


def _questions(packet: dict[str, object]) -> Iterable[dict[str, object]]:
    sections = packet["sections"]
    assert isinstance(sections, dict)
    for section in sections.values():
        yield from _items(section)


async def run_protocol(
    parameters: StdioServerParameters, fixture: Any, *, assert_cli_mutation: bool
) -> None:
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        listed = await session.list_tools()
        assert initialized.server_info.name == "WorkTrace"
        assert {tool.name for tool in listed.tools} == _TOOL_NAMES
        for tool in listed.tools:
            assert tool.input_schema["type"] == "object"
            assert "expected_view_token" in tool.input_schema["properties"]
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.annotations.open_world_hint is False

        paged_search = payload(
            await session.call_tool(
                "search_evidence", {"query": "DEMO", "app_id": "sample", "limit": 1}
            )
        )
        assert isinstance(paged_search["next_cursor"], str)
        paged_search_continuation = payload(
            await session.call_tool(
                "search_evidence",
                {
                    "query": "DEMO",
                    "app_id": "sample",
                    "limit": 1,
                    "cursor": paged_search["next_cursor"],
                    "expected_view_token": paged_search["view_token"],
                },
            )
        )
        _items(paged_search_continuation["results"])

        search = payload(
            await session.call_tool(
                "search_evidence",
                {"query": fixture.dense_object_query, "app_id": "sample", "limit": 1},
            )
        )
        token = cast(str, search["view_token"])
        _items(search["results"])

        all_results = payload(
            await session.call_tool(
                "search_evidence",
                {
                    "query": fixture.dense_object_query,
                    "app_id": "sample",
                    "limit": 20,
                    "expected_view_token": token,
                },
            )
        )
        searched = next(item for item in _items(all_results["results"]) if item["source"] == "jira")
        object_id = cast(str, searched["object_id"])
        evidence_id = cast(str, searched["evidence_id"])
        context = payload(
            await session.call_tool(
                "get_evidence_context",
                {
                    "app_id": "sample",
                    "object_id": object_id,
                    "limit": 1,
                    "expected_view_token": token,
                },
            )
        )
        relations = _stream(context, "relations")
        memberships = _stream(context, "memberships")
        assert relations["requested"] is True
        assert memberships["requested"] is True
        relation_cursor = relations["next_cursor"]
        membership_cursor = memberships["next_cursor"]
        assert isinstance(relation_cursor, str)
        assert isinstance(membership_cursor, str)
        await _consume_context_stream(
            session,
            object_id=object_id,
            view_token=token,
            stream_name="relations",
            cursor=relation_cursor,
        )
        await _consume_context_stream(
            session,
            object_id=object_id,
            view_token=token,
            stream_name="memberships",
            cursor=membership_cursor,
        )

        candidates = payload(
            await session.call_tool(
                "list_contribution_candidates",
                {"app_id": "sample", "limit": 1, "expected_view_token": token},
            )
        )
        assert isinstance(candidates["next_cursor"], str)
        candidate_continuation = payload(
            await session.call_tool(
                "list_contribution_candidates",
                {
                    "app_id": "sample",
                    "limit": 1,
                    "cursor": candidates["next_cursor"],
                    "expected_view_token": token,
                },
            )
        )
        _items(candidate_continuation["candidates"])

        candidate_id = cast(str, _items(memberships["items"])[0]["candidate_id"])
        summary = payload(
            await session.call_tool(
                "get_contribution_summary",
                {"contribution_id": candidate_id, "expected_view_token": token},
            )
        )
        assert cast(dict[str, object], summary["contribution"])["candidate_id"] == candidate_id
        packet = payload(
            await session.call_tool(
                "build_phase4_packet",
                {"contribution_id": candidate_id, "limit": 1, "expected_view_token": token},
            )
        )
        questions = list(_questions(packet))
        assert len(questions) == 30
        assert {str(question["question_id"]) for question in questions} == {
            item.question_id for item in PHASE4_QUESTIONS
        }
        for question in questions:
            assert question["status"] in {"supported", "partially_supported", "unknown"}
            assert isinstance(question["has_support"], bool)
            assert isinstance(question["has_contradictions"], bool)
            assert isinstance(question["limitation_count"], int)
        assert isinstance(packet["has_contradictions"], bool)
        assert isinstance(packet["has_limitations"], bool)

        details = payload(
            await session.call_tool(
                "build_phase4_packet",
                {
                    "contribution_id": candidate_id,
                    "question_id": "identity.what",
                    "limit": 1,
                    "expected_view_token": token,
                },
            )
        )
        assert details["response_mode"] == "question_details"
        _items(details["details"])
        detail_cursor = details["detail_cursor"]
        assert isinstance(detail_cursor, str)
        await _consume_packet_details(session, candidate_id, token, detail_cursor)

        gaps = payload(
            await session.call_tool(
                "list_evidence_gaps",
                {"contribution_id": candidate_id, "expected_view_token": token},
            )
        )
        assert gaps["view_token"] == token
        _items(gaps["unknown_questions"])
        _items(gaps["contradictions"])
        _items(gaps["limitations"])
        excerpt = payload(
            await session.call_tool(
                "get_evidence_excerpt",
                {"evidence_id": evidence_id, "expected_view_token": token},
            )
        )
        assert excerpt["view_token"] == token
        assert excerpt["source_text_is_untrusted"] is True

        if assert_cli_mutation:
            mutation = fixture.invoke("confirm", candidate_id)
            assert mutation.exit_code == 0, mutation.output
            stale = payload(
                await session.call_tool(
                    "list_contribution_candidates",
                    {"app_id": "sample", "expected_view_token": token},
                )
            )
            assert cast(dict[str, object], stale["error"])["code"] == "evidence_changed"
            refreshed = payload(
                await session.call_tool("list_contribution_candidates", {"app_id": "sample"})
            )
            restart_token = cast(str, refreshed["view_token"])
        else:
            restart_token = token

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as restarted,
    ):
        await restarted.initialize()
        epoch_stale = payload(
            await restarted.call_tool(
                "list_contribution_candidates",
                {"app_id": "sample", "expected_view_token": restart_token},
            )
        )
    assert cast(dict[str, object], epoch_stale["error"])["code"] == "evidence_changed"
