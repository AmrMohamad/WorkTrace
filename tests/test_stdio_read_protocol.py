from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import cast

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from worktrace.config import load_config
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.db.repository import EvidenceRepository


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""\
schema_version = 1
[data]
directory = {str(tmp_path / "data")!r}
[employment]
from = "2026-01-01"
to = "2026-12-31"
[identity]
display_name = "Fixture Engineer"
git_author_emails = []
git_author_names = []
[[apps]]
id = "sample_store"
name = "Sample Store"
market = "XX"
business_type = "fixture"
repo_paths = [{str(tmp_path)!r}]
""",
        encoding="utf-8",
    )
    return path


def _seed_ledger(config_path: Path) -> None:
    config = load_config(config_path)
    config.data_directory.mkdir(parents=True, exist_ok=True)
    connection = connect(config.database_path)
    try:
        migrate(connection, config.database_path)
        connection.execute(
            "INSERT INTO apps(id, name, market, business_type) "
            "VALUES ('sample_store', 'Sample Store', 'XX', 'fixture')"
        )
        for index in range(2):
            run_id = f"run:stdio:{index}"
            object_id = f"obj:stdio:{index}"
            connection.execute(
                """
                INSERT INTO sync_runs(
                    id, app_id, source, source_instance, status, started_at, completed_at,
                    adapter_version, scope_json, completeness
                ) VALUES (?, 'sample_store', 'manual', 'stdio', 'complete', ?, ?,
                          'fixture', '{}', 'complete')
                """,
                (
                    run_id,
                    f"2026-01-0{index + 1}T00:00:00+00:00",
                    f"2026-01-0{index + 1}T00:00:00+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO source_objects(
                    id, app_id, source, source_instance, kind, external_id,
                    first_seen_run_id, last_seen_run_id
                ) VALUES (?, 'sample_store', 'manual', 'stdio', 'manual_evidence', ?, ?, ?)
                """,
                (object_id, f"stdio-{index}", run_id, run_id),
            )
            connection.execute(
                """
                INSERT INTO observations(
                    id, source_object_id, sync_run_id, source_updated_at, fetched_at, payload_hash,
                    title, body_text, data_json, completeness, adapter_version,
                    normalization_version, redaction_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', 'complete', 'fixture', '1', '1')
                """,
                (
                    f"obs:stdio:{index}",
                    object_id,
                    run_id,
                    f"2026-01-0{index + 1}T00:00:00+00:00",
                    f"2026-01-0{index + 1}T00:00:00+00:00",
                    f"hash-{index}",
                    f"stdio evidence {index}",
                    f"stdio searchable fixture {index}",
                ),
            )
            connection.execute(
                """
                INSERT INTO candidate_groups(
                    id, app_id, seed_object_id, generator_version, suggested_title,
                    suggested_type, generated_at
                ) VALUES (?, 'sample_store', ?, 'fixture', ?, 'manual', '2026-01-02T00:00:00+00:00')
                """,
                (f"candidate:stdio:{index}", object_id, f"stdio evidence {index}"),
            )
            connection.execute(
                "INSERT INTO candidate_members(candidate_id, source_object_id, membership_reason) "
                "VALUES (?, ?, 'fixture')",
                (f"candidate:stdio:{index}", object_id),
            )
        connection.commit()
    finally:
        connection.close()


def _payload(result: object) -> dict[str, object]:
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


def _server(config_path: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=os.environ.get("WORKTRACE_TEST_SERVER_PYTHON", sys.executable),
        args=["-m", "worktrace", "serve-mcp", "--config", str(config_path)],
        cwd=config_path.parent,
        env={"PYTHONPATH": ""},
    )


@pytest.mark.asyncio
async def test_stdio_read_protocol_binds_tokens_paging_mutation_and_restart(tmp_path: Path) -> None:
    config_path = _config(tmp_path)
    _seed_ledger(config_path)
    parameters = _server(config_path)

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        listed = await session.list_tools()
        assert initialized.server_info.name == "WorkTrace"
        assert {tool.name for tool in listed.tools} == {
            "list_contribution_candidates",
            "get_contribution_summary",
            "build_phase4_packet",
            "list_evidence_gaps",
            "search_evidence",
            "get_evidence_excerpt",
            "get_evidence_context",
        }
        for tool in listed.tools:
            assert "expected_view_token" in tool.input_schema["properties"]

        search = _payload(
            await session.call_tool(
                "search_evidence", {"query": "stdio", "app_id": "sample_store", "limit": 1}
            )
        )
        token = cast(str, search["view_token"])
        assert isinstance(search["next_cursor"], str)
        context = _payload(
            await session.call_tool(
                "get_evidence_context",
                {
                    "app_id": "sample_store",
                    "object_id": "obj:stdio:0",
                    "limit": 1,
                    "expected_view_token": token,
                },
            )
        )
        assert context["view_token"] == token
        relation_cursor = context["relations"]["next_cursor"]
        if isinstance(relation_cursor, str):
            relation_page = _payload(
                await session.call_tool(
                    "get_evidence_context",
                    {
                        "app_id": "sample_store",
                        "object_id": "obj:stdio:0",
                        "relation_cursor": relation_cursor,
                        "expected_view_token": token,
                    },
                )
            )
            assert relation_page["memberships"]["requested"] is False
        continued = _payload(
            await session.call_tool(
                "search_evidence",
                {
                    "query": "stdio",
                    "app_id": "sample_store",
                    "limit": 1,
                    "cursor": search["next_cursor"],
                    "expected_view_token": token,
                },
            )
        )
        assert continued["view_token"] == token
        assert continued["results"]

        candidates = _payload(
            await session.call_tool(
                "list_contribution_candidates",
                {"app_id": "sample_store", "limit": 1, "expected_view_token": token},
            )
        )
        first_candidate = cast(dict[str, str], cast(list[object], candidates["candidates"])[0])
        candidate_id = first_candidate["candidate_id"]
        packet = _payload(
            await session.call_tool(
                "build_phase4_packet",
                {
                    "contribution_id": candidate_id,
                    "expected_view_token": token,
                    "question_id": "identity.what",
                    "limit": 1,
                },
            )
        )
        assert packet["view_token"] == token
        assert packet["response_mode"] == "question_details"

        config = load_config(config_path)
        writer = connect(config.database_path)
        try:
            repository = EvidenceRepository(writer)
            session_id = repository.create_import_session(
                config.app("sample_store"), date(2026, 1, 1), date(2026, 1, 2)
            )
            repository.update_import_session_progress(session_id, {"stdio": "changed"})
        finally:
            writer.close()

        stale = _payload(
            await session.call_tool(
                "list_contribution_candidates",
                {"app_id": "sample_store", "expected_view_token": token},
            )
        )
        assert cast(dict[str, str], stale["error"])["code"] == "evidence_changed"
        refreshed = _payload(
            await session.call_tool("list_contribution_candidates", {"app_id": "sample_store"})
        )

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as restarted,
    ):
        await restarted.initialize()
        epoch_stale = _payload(
            await restarted.call_tool(
                "list_contribution_candidates",
                {"app_id": "sample_store", "expected_view_token": refreshed["view_token"]},
            )
        )
    assert cast(dict[str, str], epoch_stale["error"])["code"] == "evidence_changed"
