from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import worktrace.read_workspace as read_workspace_module
from tests.test_packets_golden import _packet_state
from worktrace.errors import ScopeViolation
from worktrace.read_workspace import (
    DatabaseBusy,
    DatabaseUpgradeRequired,
    DatabaseVersionUnsupported,
    ReadOnlyWorkspace,
)


def _workspace(tmp_path: Path) -> tuple[ReadOnlyWorkspace, str]:
    connection, _, config, candidate_id = _packet_state(tmp_path)
    connection.close()
    return ReadOnlyWorkspace(config), candidate_id


def test_read_only_workspace_completes_one_coherent_review(tmp_path: Path) -> None:
    workspace, candidate_id = _workspace(tmp_path)

    applications = workspace.applications()
    page = workspace.candidate_page("sample_store")
    status = workspace.source_status("sample_store")
    review = workspace.contribution_review("sample_store", candidate_id)
    members = review.packet["evidence_summary"]
    assert isinstance(members, dict)
    raw_members = members["members"]
    assert isinstance(raw_members, list)
    evidence_id = str(raw_members[0]["evidence_id"])
    excerpt = workspace.evidence_excerpt("sample_store", evidence_id, max_chars=1_200)

    assert [application.app_id for application in applications] == ["sample_store"]
    assert [item.candidate_id for item in page.items] == [candidate_id]
    assert set(status) == {"git", "gitlab", "jira", "manual"}
    assert review.app_id == "sample_store"
    assert review.packet["schema_version"] == 2
    assert review.gaps["contribution_id"] == review.resolved_contribution_id
    assert excerpt["evidence_id"] == evidence_id


def test_workspace_builds_packet_once_and_derives_gaps_from_same_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, candidate_id = _workspace(tmp_path)
    original_build = read_workspace_module.PacketBuilder.build_packet
    original_gaps = read_workspace_module.build_gap_report
    packet_ids: list[int] = []
    gap_packet_ids: list[int] = []

    def build_packet(builder: Any, identifier: str) -> dict[str, object]:
        packet = original_build(builder, identifier)
        packet_ids.append(id(packet))
        return packet

    def build_gaps(packet: dict[str, object]) -> dict[str, object]:
        gap_packet_ids.append(id(packet))
        return original_gaps(packet)

    monkeypatch.setattr(read_workspace_module.PacketBuilder, "build_packet", build_packet)
    monkeypatch.setattr(read_workspace_module, "build_gap_report", build_gaps)

    workspace.contribution_review("sample_store", candidate_id)

    assert len(packet_ids) == 1
    assert gap_packet_ids == packet_ids


def test_workspace_connection_is_worker_created_query_only_500ms_and_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, _ = _workspace(tmp_path)
    original = read_workspace_module.connect_read_only
    events: list[tuple[str, int, int]] = []

    class TrackingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str) -> object:
            return getattr(self.connection, name)

        def close(self) -> None:
            events.append(("close", threading.get_ident(), 500))
            self.connection.close()

    def tracking_connect(path: Path, *, busy_timeout_ms: int) -> Any:
        events.append(("open", threading.get_ident(), busy_timeout_ms))
        connection = original(path, busy_timeout_ms=busy_timeout_ms)
        assert int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
        connection.execute("PRAGMA query_only = OFF")
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO apps(id, name) VALUES ('bad', 'Bad')")
        connection.execute("PRAGMA query_only = ON")
        return TrackingConnection(connection)

    monkeypatch.setattr(read_workspace_module, "connect_read_only", tracking_connect)
    main_thread = threading.get_ident()

    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(workspace.applications).result()[0].app_id == "sample_store"

    assert events[0][0] == "open"
    assert events[-1][0] == "close"
    assert events[0][1] == events[-1][1]
    assert events[0][1] != main_thread
    assert events[0][2] == 500


def test_workspace_rejects_cross_application_candidate_and_evidence(tmp_path: Path) -> None:
    from tests.tui_support import add_second_application

    connection, _, config, _ = _packet_state(tmp_path)
    config = add_second_application(connection, config, tmp_path)
    connection.close()
    workspace = ReadOnlyWorkspace(config)

    with pytest.raises(ScopeViolation):
        workspace.contribution_review("sample_store", "candidate:other")
    with pytest.raises(ScopeViolation):
        workspace.evidence_excerpt("sample_store", "obs:other", max_chars=1_200)


def test_workspace_reports_older_and_newer_database_versions(tmp_path: Path) -> None:
    from worktrace.db.migrations import migrations

    workspace, _ = _workspace(tmp_path)
    database_path = workspace.database_path
    supported = migrations()[-1].version
    writer = sqlite3.connect(database_path)
    try:
        writer.execute(f"PRAGMA user_version = {supported - 1}")
        writer.commit()
        with pytest.raises(DatabaseUpgradeRequired):
            workspace.applications()

        writer.execute(f"PRAGMA user_version = {supported + 1}")
        writer.commit()
        with pytest.raises(DatabaseVersionUnsupported):
            workspace.applications()
    finally:
        writer.close()


def test_workspace_bounds_lock_contention_to_busy_error(tmp_path: Path) -> None:
    workspace, _ = _workspace(tmp_path)
    blocker = sqlite3.connect(workspace.database_path, timeout=0.1, isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(DatabaseBusy):
            workspace.applications()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
