from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import worktrace.read_workspace as read_workspace_module
from tests.test_packets_golden import _packet_state
from worktrace.candidates.decisions import append_decision
from worktrace.db.connection import connect
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


def test_contribution_review_uses_one_snapshot_during_concurrent_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, candidate_id = _workspace(tmp_path)
    journal = sqlite3.connect(workspace.database_path, autocommit=True)
    try:
        assert str(journal.execute("PRAGMA journal_mode = WAL").fetchone()[0]) == "wal"
    finally:
        journal.close()

    baseline = workspace.contribution_review("sample_store", candidate_id)
    summary = baseline.packet["evidence_summary"]
    assert isinstance(summary, dict)
    members = summary["members"]
    assert isinstance(members, list)
    member_ids = [
        str(member["object_id"])
        for member in members
        if isinstance(member, dict) and member.get("object_id")
    ]
    baseline_contribution = baseline.packet["contribution"]
    assert isinstance(baseline_contribution, dict)
    baseline_signature = (
        baseline.status,
        baseline_contribution.get("title"),
        baseline_contribution.get("title_authority"),
        baseline_contribution.get("title_status"),
    )

    projection_ready = threading.Event()
    writer_committed = threading.Event()
    original_project_candidate = read_workspace_module.project_candidate
    paused = False

    def project_then_pause(*args: Any, **kwargs: Any) -> Any:
        nonlocal paused
        projected = original_project_candidate(*args, **kwargs)
        if not paused:
            paused = True
            projection_ready.set()
            assert writer_committed.wait(timeout=5)
        return projected

    monkeypatch.setattr(read_workspace_module, "project_candidate", project_then_pause)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(workspace.contribution_review, "sample_store", candidate_id)
        assert projection_ready.wait(timeout=5)
        writer = connect(workspace.database_path)
        try:
            append_decision(
                writer,
                "confirm_candidate",
                candidate_id,
                {
                    "contribution_id": "contribution:concurrent-confirmation",
                    "app_id": "sample_store",
                    "title": "Concurrent human-reviewed title",
                    "members": member_ids,
                },
            )
        finally:
            writer.close()
            writer_committed.set()
        interleaved = future.result(timeout=5)

    interleaved_contribution = interleaved.packet["contribution"]
    assert isinstance(interleaved_contribution, dict)
    assert (
        interleaved.status,
        interleaved_contribution.get("title"),
        interleaved_contribution.get("title_authority"),
        interleaved_contribution.get("title_status"),
    ) == baseline_signature

    settled = workspace.contribution_review("sample_store", candidate_id)
    settled_contribution = settled.packet["contribution"]
    assert isinstance(settled_contribution, dict)
    assert settled.status == "confirmed"
    assert settled_contribution["title"] == "Concurrent human-reviewed title"


def test_contribution_review_rolls_back_snapshot_before_closing_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, candidate_id = _workspace(tmp_path)
    original_connect = read_workspace_module.connect_read_only
    events: list[str | tuple[str, bool]] = []

    class TrackingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str) -> object:
            return getattr(self.connection, name)

        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            command = sql.strip().split(maxsplit=1)[0].upper()
            if command in {"BEGIN", "COMMIT", "ROLLBACK"}:
                events.append(command)
            return self.connection.execute(sql, *args)

        def close(self) -> None:
            events.append(("CLOSE", self.connection.in_transaction))
            self.connection.close()

    def tracking_connect(path: Path, *, busy_timeout_ms: int) -> Any:
        return TrackingConnection(original_connect(path, busy_timeout_ms=busy_timeout_ms))

    def fail_packet(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("synthetic packet failure")

    monkeypatch.setattr(read_workspace_module, "connect_read_only", tracking_connect)
    monkeypatch.setattr(read_workspace_module.PacketBuilder, "build_packet", fail_packet)

    with pytest.raises(RuntimeError, match="synthetic packet failure"):
        workspace.contribution_review("sample_store", candidate_id)

    assert events.count("BEGIN") == 1
    assert events.count("ROLLBACK") == 1
    assert "COMMIT" not in events
    assert events[-1] == ("CLOSE", False)


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


def test_contribution_review_bounds_lock_contention_to_busy_error(tmp_path: Path) -> None:
    workspace, candidate_id = _workspace(tmp_path)
    blocker = sqlite3.connect(workspace.database_path, timeout=0.1, isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(DatabaseBusy):
            workspace.contribution_review("sample_store", candidate_id)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
