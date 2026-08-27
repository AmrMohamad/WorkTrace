from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

from worktrace.adapters.base import (
    NormalizedPage,
    NormalizedRecord,
    UnavailableObjectDescriptor,
)
from worktrace.config import AppConfig
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.db.repository import EvidenceRepository
from worktrace.importers.orchestrator import import_snapshot
from worktrace.normalize.records import build_record
from worktrace.normalize.redaction import Redactor


def _app() -> AppConfig:
    return AppConfig(
        id="sample_store",
        name="Sample Store",
        market="XX",
        business_type="fixture",
        jira_project_keys=("DEMO",),
        gitlab_project_ids=(101,),
        repo_paths=(),
        jira_key_patterns=(r"DEMO-[0-9]+",),
        production_environments=("production",),
        release_tag_patterns=("v*",),
        ignored_paths=(),
    )


def _record(*, title: str, observed_at: str) -> NormalizedRecord:
    return build_record(
        source_kind="git",
        source_instance="fixture-repository",
        object_type="commit",
        external_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        app_id="sample_store",
        observed_at=observed_at,
        source_updated_at="2026-01-10T10:00:00Z",
        payload={
            "title": title,
            "body": "DEMO-101 synthetic commit",
            "sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        },
        redactor=Redactor(email_key=b"fixture-only-key"),
    )


@dataclass
class StaticAdapter:
    pages: tuple[NormalizedPage, ...]
    interrupt_after: int | None = None

    def iter_pages(self) -> Iterator[NormalizedPage]:
        for index, page in enumerate(self.pages, start=1):
            yield page
            if self.interrupt_after == index:
                raise RuntimeError("synthetic page interruption")


def _page(*, title: str, cursor: str | None, is_last: bool) -> NormalizedPage:
    record = _record(title=title, observed_at="2026-01-11T12:00:00Z")
    return NormalizedPage(
        source_kind="git",
        source_instance="fixture-repository",
        resource_type="commits",
        cursor=cursor,
        next_cursor=None if is_last else "next-page",
        is_last=is_last,
        records=(record,),
    )


def _repository(tmp_path: Path) -> tuple[sqlite3.Connection, EvidenceRepository]:
    database_path = tmp_path / "worktrace.sqlite3"
    connection = connect(database_path)
    migrate(connection, database_path)
    connection.execute(
        "INSERT INTO apps(id, name, market, business_type) VALUES (?, ?, ?, ?)",
        ("sample_store", "Sample Store", "XX", "fixture"),
    )
    connection.commit()
    return connection, EvidenceRepository(connection)


def test_interrupted_page_marks_source_partial_without_finishing_shared_parent(
    tmp_path: Path,
) -> None:
    connection, repository = _repository(tmp_path)
    try:
        app = _app()
        parent_id = repository.create_import_session(app, date(2026, 1, 1), date(2026, 1, 31))
        result = import_snapshot(
            app,
            StaticAdapter((_page(title="Page one", cursor=None, is_last=False),), 1),
            repository,
            source="git",
            source_instance="fixture-repository",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
            import_session_id=parent_id,
            finish_session=False,
        )

        assert result.status == "partial"
        assert result.session_id == parent_id
        assert result.pages == 1
        assert result.records == 1
        assert result.error == "synthetic page interruption"
        parent = connection.execute(
            "SELECT status, completed_at FROM import_sessions WHERE id=?", (parent_id,)
        ).fetchone()
        assert tuple(parent) == ("running", None)
        source = connection.execute(
            "SELECT status, completeness, error_summary FROM sync_runs WHERE id=?",
            (result.run_id,),
        ).fetchone()
        assert tuple(source) == ("failed", "partial", "synthetic page interruption")
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM observations WHERE sync_run_id=?", (result.run_id,)
            ).fetchone()[0]
            == 1
        )
        assert repository.current_observations("sample_store") == []
    finally:
        connection.close()


def test_repeated_snapshot_reuses_stable_source_identity(tmp_path: Path) -> None:
    connection, repository = _repository(tmp_path)
    try:
        app = _app()
        first = import_snapshot(
            app,
            StaticAdapter((_page(title="First snapshot", cursor=None, is_last=True),)),
            repository,
            source="git",
            source_instance="fixture-repository",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )
        second = import_snapshot(
            app,
            StaticAdapter((_page(title="Second snapshot", cursor=None, is_last=True),)),
            repository,
            source="git",
            source_instance="fixture-repository",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )

        assert first.status == second.status == "complete"
        assert first.run_id != second.run_id
        objects = connection.execute(
            "SELECT id, first_seen_run_id, last_seen_run_id FROM source_objects"
        ).fetchall()
        assert len(objects) == 1
        assert objects[0]["first_seen_run_id"] == first.run_id
        assert objects[0]["last_seen_run_id"] == second.run_id
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
        current = repository.current_observations("sample_store")
        assert len(current) == 1
        assert current[0]["title"] == "Second snapshot"
        assert current[0]["sync_run_id"] == second.run_id
    finally:
        connection.close()


def test_exact_object_unavailability_flows_through_page_transaction(tmp_path: Path) -> None:
    connection, repository = _repository(tmp_path)
    try:
        app = _app()
        first = import_snapshot(
            app,
            StaticAdapter((_page(title="Visible", cursor=None, is_last=True),)),
            repository,
            source="git",
            source_instance="fixture-repository",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )
        unavailable_page = NormalizedPage(
            source_kind="git",
            source_instance="fixture-repository",
            resource_type="commits",
            cursor=None,
            next_cursor=None,
            is_last=True,
            records=(),
            unavailable_objects=(
                UnavailableObjectDescriptor(
                    kind="git_commit",
                    external_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                ),
            ),
        )
        second = import_snapshot(
            app,
            StaticAdapter((unavailable_page,)),
            repository,
            source="git",
            source_instance="fixture-repository",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )

        assert first.status == second.status == "complete"
        state = connection.execute(
            "SELECT availability, availability_reason FROM source_objects"
        ).fetchone()
        assert tuple(state) == ("unavailable", "not_found")
        event = connection.execute(
            """
            SELECT state, reason FROM source_object_availability_events
            WHERE sync_run_id=?
            """,
            (second.run_id,),
        ).fetchone()
        assert tuple(event) == ("unavailable", "not_found")
    finally:
        connection.close()


def test_selection_events_mark_run_and_affected_observation_selection_biased(
    tmp_path: Path,
) -> None:
    connection, repository = _repository(tmp_path)
    try:
        page = NormalizedPage(
            source_kind="git",
            source_instance="fixture-repository",
            resource_type="commits",
            cursor=None,
            next_cursor=None,
            is_last=True,
            records=(_record(title="Bounded snapshot", observed_at="2026-01-11T12:00:00Z"),),
            limitations=("Synthetic provider selection was truncated.",),
            selection_events=(
                {
                    "kind": "synthetic_cap",
                    "input_count": 3,
                    "selected_count": 1,
                    "dropped_count": 2,
                    "selection_policy": "newest_first",
                },
            ),
            records_selection_biased=True,
        )
        result = import_snapshot(
            _app(),
            StaticAdapter((page,)),
            repository,
            source="git",
            source_instance="fixture-repository",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )

        assert result.status == "complete"
        assert result.completeness == "selection_biased"
        assert result.limitations == ("Synthetic provider selection was truncated.",)
        run = connection.execute(
            "SELECT status, completeness, progress_json FROM sync_runs WHERE id=?",
            (result.run_id,),
        ).fetchone()
        assert (run["status"], run["completeness"]) == ("complete", "selection_biased")
        progress = json.loads(str(run["progress_json"]))
        assert progress["selection_biased"] is True
        assert progress["selection_events"] == [
            {
                "kind": "synthetic_cap",
                "input_count": 3,
                "selected_count": 1,
                "dropped_count": 2,
                "selection_policy": "newest_first",
            }
        ]
        observation = connection.execute(
            "SELECT completeness FROM observations WHERE sync_run_id=?",
            (result.run_id,),
        ).fetchone()
        assert observation["completeness"] == "selection_biased"
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("record_source", "record_instance"),
    [("jira", "jira-main"), ("git", "other-repository")],
)
def test_record_identity_cannot_escape_import_run_source_scope(
    tmp_path: Path,
    record_source: str,
    record_instance: str,
) -> None:
    connection, repository = _repository(tmp_path)
    try:
        mismatched = build_record(
            source_kind=record_source,
            source_instance=record_instance,
            object_type="issue" if record_source == "jira" else "commit",
            external_id="DEMO-999" if record_source == "jira" else "b" * 40,
            app_id="sample_store",
            observed_at="2026-01-11T12:00:00Z",
            source_updated_at="2026-01-10T10:00:00Z",
            payload={"title": "Borrowed source authority"},
            redactor=Redactor(email_key=b"fixture-only-key"),
        )
        page = NormalizedPage(
            source_kind="git",
            source_instance="fixture-repository",
            resource_type="commits",
            cursor=None,
            next_cursor=None,
            is_last=True,
            records=(mismatched,),
        )

        result = import_snapshot(
            _app(),
            StaticAdapter((page,)),
            repository,
            source="git",
            source_instance="fixture-repository",
            date_from=date(2026, 1, 1),
            date_to=date(2026, 1, 31),
        )

        assert result.status == "partial"
        assert result.pages == 0
        assert result.records == 0
        assert result.error == "adapter record escaped its configured source scope"
        assert connection.execute("SELECT COUNT(*) FROM source_objects").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM source_object_availability_events").fetchone()[
                0
            ]
            == 0
        )
        run = connection.execute(
            "SELECT status, completeness, progress_json FROM sync_runs WHERE id=?",
            (result.run_id,),
        ).fetchone()
        assert tuple(run) == ("failed", "partial", "{}")
    finally:
        connection.close()
