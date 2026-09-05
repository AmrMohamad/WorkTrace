from __future__ import annotations

import base64
from dataclasses import replace
from pathlib import Path

import pytest

from tests.test_mcp_security import _mcp_state
from worktrace.candidates.decisions import append_decision
from worktrace.db.connection import connect
from worktrace.db.repository import EvidenceRepository
from worktrace.errors import ScopeViolation
from worktrace.mcp_server.protocol import ProtocolError, decode_cursor, encode_cursor, fingerprint
from worktrace.mcp_server.tools import WorkTraceTools
from worktrace.packets.builder import PacketBuilder


def test_view_token_shared_by_all_six_reads_and_invalidated_by_real_writer(tmp_path: Path) -> None:
    database, _, service = _mcp_state(tmp_path)
    token = service.search_evidence(query="Synthetic", app_id="sample_store")["view_token"]
    calls = [
        lambda t: service.list_contribution_candidates(
            app_id="sample_store", expected_view_token=t
        ),
        lambda t: service.search_evidence(
            query="Synthetic", app_id="sample_store", expected_view_token=t
        ),
        lambda t: service.get_contribution_summary(
            contribution_id="candidate:manual_1", expected_view_token=t
        ),
        lambda t: service.build_phase4_packet(
            contribution_id="candidate:manual_1", expected_view_token=t
        ),
        lambda t: service.list_evidence_gaps(
            contribution_id="candidate:manual_1", expected_view_token=t
        ),
        lambda t: service.get_evidence_excerpt(evidence_id="obs:manual_1", expected_view_token=t),
    ]
    for call in calls:
        assert call(token)["view_token"] == token
    with connect(database) as connection:
        append_decision(
            connection, "rename_contribution", "candidate:manual_1", {"title": "Changed"}
        )
    for call in calls:
        response = call(token)
        assert response["error"]["code"] == "evidence_changed"
        assert not {"results", "candidates", "sections", "text"} & response.keys()
        assert response["view_token"] != token


def test_restart_and_configuration_changes_invalidate_investigation(tmp_path: Path) -> None:
    database, config, service = _mcp_state(tmp_path)
    first = service.list_contribution_candidates(app_id="sample_store", limit=1)
    restarted = WorkTraceTools(config=config, database_path=database)
    assert (
        restarted.list_contribution_candidates(app_id="sample_store", cursor=first["next_cursor"])[
            "error"
        ]["code"]
        == "evidence_changed"
    )
    service._config = replace(config, employment_timezone="America/New_York")
    assert (
        service.get_evidence_excerpt(
            evidence_id="obs:manual_1", expected_view_token=first["view_token"]
        )["error"]["code"]
        == "evidence_changed"
    )


def test_filter_bound_search_traversal_and_normalized_equivalence(tmp_path: Path) -> None:
    _, _, service = _mcp_state(tmp_path)
    first = service.search_evidence(
        query=" SYNTHETIC ", app_id="sample_store", source_types=["MANUAL", "manual"], limit=3
    )
    second = service.search_evidence(
        query="synthetic",
        app_id="sample_store",
        source_types=["manual"],
        limit=5,
        cursor=first["next_cursor"],
    )
    assert "error" not in second
    assert {r["evidence_id"] for r in first["results"]}.isdisjoint(
        r["evidence_id"] for r in second["results"]
    )
    changed = service.search_evidence(
        query="different",
        app_id="sample_store",
        source_types=["manual"],
        cursor=first["next_cursor"],
    )
    assert changed["error"]["code"] == "cursor_filter_mismatch"
    with pytest.raises(ScopeViolation):
        service.search_evidence(
            query="x", app_id="sample_store", date_from="2026-02-02", date_to="2026-01-01"
        )


@pytest.mark.parametrize("cursor", ["offset:0", "offset:999", "bad", "wtc1:!", "x" * 2049])
def test_bad_and_legacy_cursors_fail_explicitly(tmp_path: Path, cursor: str) -> None:
    _, _, service = _mcp_state(tmp_path)
    response = service.list_contribution_candidates(app_id="sample_store", cursor=cursor)
    assert response["error"]["code"] == (
        "cursor_upgrade_required" if cursor.startswith("offset:") else "invalid_cursor"
    )


def test_cursor_scope_generation_types_and_duplicate_json_keys() -> None:
    token = "view:1:" + "a" * 64
    filters = fingerprint({})
    cursor = encode_cursor(
        collection="candidates",
        app_id="one",
        view=token,
        filters=filters,
        position={"candidate_id": "candidate:one"},
        generation="b" * 64,
    )
    with pytest.raises(ProtocolError) as mismatch:
        decode_cursor(cursor, collection="candidates", app_id="two", view=token, filters=filters)
    assert mismatch.value.code == "cursor_scope_mismatch"
    with pytest.raises(ProtocolError):
        decode_cursor(cursor, collection="evidence", app_id="one", view=token, filters=filters)
    bad = "wtc1:" + base64.urlsafe_b64encode(b'{"v":1,"v":1}').decode().rstrip("=")
    with pytest.raises(ProtocolError) as duplicate:
        decode_cursor(bad, collection="candidates", app_id="one", view=token, filters=filters)
    assert duplicate.value.code == "invalid_cursor"


def test_actual_mcp_projection_stays_in_wal_snapshot_during_concurrent_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, config, service = _mcp_state(tmp_path)
    writer = connect(database)
    writer.autocommit = True
    writer.execute("PRAGMA journal_mode=WAL")
    writer.close()
    original = PacketBuilder.source_status
    wrote = False

    def interrupted(builder, app_id):
        nonlocal wrote
        if not wrote:
            wrote = True
            with connect(database) as connection:
                repository = EvidenceRepository(connection)
                session = repository.create_import_session(
                    config.apps[0], config.employment_from, config.employment_to
                )
                repository.finish_import_session(
                    session,
                    "failed",
                    {
                        "sources": [
                            {
                                "source": "git",
                                "preflight": {"status": "not_started", "reason": "fixture"},
                            }
                        ]
                    },
                )
        return original(builder, app_id)

    monkeypatch.setattr(PacketBuilder, "source_status", interrupted)
    during = service.list_contribution_candidates(app_id="sample_store", limit=1)
    assert not during["source_status"]["git"].get("preflight")
    after = service.list_contribution_candidates(app_id="sample_store", limit=1)
    assert after["source_status"]["git"]["preflight"][0]["status"] == "not_started"
    assert during["view_token"] != after["view_token"]


def test_production_configuration_is_reloaded_between_calls(tmp_path: Path) -> None:
    database, config, _ = _mcp_state(tmp_path)
    path = tmp_path / "settings.toml"
    text = f"""schema_version=1
[data]
directory={str(tmp_path)!r}
[employment]
from="2020-01-01"
to="2026-12-31"
[identity]
display_name="Fixture Engineer"
git_author_names=["Fixture Engineer"]
[[apps]]
id="sample_store"
name="Sample Store"
repo_paths=[{str(tmp_path / "repo")!r}]
"""
    path.write_text(text)
    service = WorkTraceTools(config_path=path, database_path=database)
    first = service.search_evidence(query="Synthetic", app_id=config.apps[0].id)
    path.write_text(text.replace('name="Sample Store"', 'name="Renamed Store"'))
    second = service.search_evidence(
        query="Synthetic", app_id=config.apps[0].id, expected_view_token=first["view_token"]
    )
    assert second["error"]["code"] == "evidence_changed"
