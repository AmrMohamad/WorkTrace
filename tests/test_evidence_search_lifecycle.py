"""Reader lifecycle proof for the bounded TUI evidence-search contract."""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import worktrace.read_models.evidence_search as evidence_search_module
from tests.public_workflow_support import PublicWorkflowFixture
from tests.test_read_workspace import _workspace
from worktrace.candidates.decisions import append_decision
from worktrace.config import load_config
from worktrace.db.connection import connect
from worktrace.errors import NotFound
from worktrace.read_models.evidence_search import (
    EvidenceSearchInvalidated,
    normalize_evidence_search_filters,
)
from worktrace.read_workspace import ReadOnlyWorkspace


def _result_id(result: Any, key: str) -> str:
    payload = json.loads(result.output)
    value = payload[key]
    assert isinstance(value, str) and value
    return value


def test_search_snapshot_stays_coherent_during_writer_and_next_read_invalidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer cannot mix its new decision with this page's old snapshot."""

    workspace, candidate_id = _workspace(tmp_path)
    journal = sqlite3.connect(workspace.database_path, autocommit=True)
    try:
        assert str(journal.execute("PRAGMA journal_mode = WAL").fetchone()[0]) == "wal"
    finally:
        journal.close()
    filters = normalize_evidence_search_filters("checkout")
    baseline = workspace.search_evidence("sample_store", filters)
    ready, committed = threading.Event(), threading.Event()
    original_scan = evidence_search_module.scan_evidence
    paused = False

    def scan_then_pause(*args: Any, **kwargs: Any) -> Any:
        nonlocal paused
        rows = original_scan(*args, **kwargs)

        def gated() -> Any:
            nonlocal paused
            for row in rows:
                if not paused:
                    paused = True
                    ready.set()
                    assert committed.wait(timeout=5)
                yield row

        return gated()

    monkeypatch.setattr(evidence_search_module, "scan_evidence", scan_then_pause)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(workspace.search_evidence, "sample_store", filters)
        assert ready.wait(timeout=5)
        writer = connect(workspace.database_path)
        try:
            append_decision(
                writer,
                "confirm_candidate",
                candidate_id,
                {
                    "app_id": "sample_store",
                    "contribution_id": "contribution:concurrent-search",
                    "title": "Concurrent search decision",
                    "members": ["obj:commit", "obj:mr", "obj:jira"],
                },
            )
        finally:
            writer.close()
            committed.set()
        interleaved = future.result(timeout=5)

    assert interleaved.revision == baseline.revision
    assert interleaved.items == baseline.items
    with pytest.raises(EvidenceSearchInvalidated):
        workspace.search_evidence("sample_store", filters, expected_revision=interleaved.revision)


def test_expected_revision_rejects_cursorless_history_before_scanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, candidate_id = _workspace(tmp_path)
    filters = normalize_evidence_search_filters("checkout")
    first = workspace.search_evidence("sample_store", filters)
    writer = connect(workspace.database_path)
    try:
        append_decision(writer, "ignore_candidate", candidate_id, {"reason": "new revision"})
    finally:
        writer.close()

    def must_not_scan(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("expected-revision mismatch must happen before scanner work")

    monkeypatch.setattr(evidence_search_module, "scan_evidence", must_not_scan)
    with pytest.raises(EvidenceSearchInvalidated):
        workspace.search_evidence("sample_store", filters, expected_revision=first.revision)


def test_public_cli_corrections_keep_search_links_and_canonical_review_in_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Imported evidence is discovered before CLI corrections reshape its lineage."""

    fixture = PublicWorkflowFixture.create(tmp_path, monkeypatch, bulk_keys=2)
    assert fixture.invoke("init").exit_code == 0
    assert fixture.invoke("import", "all", "sample").exit_code == 0
    assert fixture.invoke("rebuild", "all", "sample").exit_code == 0
    workspace = ReadOnlyWorkspace(load_config(fixture.config_path))
    candidates = workspace.candidate_page("sample", page_size=50).items
    primary = next(item for item in candidates if item.title and "DEMO-1 context" in item.title)
    secondary = next(item for item in candidates if item.title and "DEMO-2" in item.title)
    first_filters = normalize_evidence_search_filters("DEMO-1 context")
    second_filters = normalize_evidence_search_filters("DEMO-2")
    before = workspace.search_evidence("sample", first_filters)
    first_item = next(item for item in before.items if item.links)
    primary_link = next(
        link for link in first_item.links if link.candidate_id == primary.candidate_id
    )
    assert primary_link.contribution_id is None and primary_link.role == "material"
    context_link = next(link for link in first_item.links if link.role == "context")
    context_review = workspace.contribution_review("sample", context_link.identifier)
    context_summary = context_review.packet["evidence_summary"]
    assert isinstance(context_summary, dict)
    assert any(
        isinstance(member, dict)
        and member.get("object_id") == first_item.object_id
        and member.get("context_only") is True
        for member in context_summary["members"]
    )

    confirmed = fixture.invoke("confirm", primary.candidate_id)
    assert confirmed.exit_code == 0, confirmed.output
    contribution_id = _result_id(confirmed, "contribution_id")
    confirmed_link = next(
        link
        for item in workspace.search_evidence("sample", first_filters).items
        for link in item.links
        if link.candidate_id == primary.candidate_id
    )
    assert confirmed_link.contribution_id == contribution_id
    assert workspace.contribution_review("sample", confirmed_link.identifier).status == "confirmed"

    second_item = next(
        item for item in workspace.search_evidence("sample", second_filters).items if item.links
    )
    second_object_id = second_item.object_id
    added = fixture.invoke("add-member", primary.candidate_id, second_object_id)
    assert added.exit_code == 0, added.output
    assert any(
        link.candidate_id == primary.candidate_id and link.role == "material"
        for item in workspace.search_evidence("sample", second_filters).items
        for link in item.links
    )

    removed = fixture.invoke("remove-member", primary.candidate_id, first_item.object_id)
    assert removed.exit_code == 0, removed.output
    assert not any(
        link.candidate_id == primary.candidate_id
        for item in workspace.search_evidence("sample", first_filters).items
        for link in item.links
    )
    assert fixture.invoke("undo", _result_id(removed, "decision_id")).exit_code == 0
    assert any(
        link.candidate_id == primary.candidate_id
        for item in workspace.search_evidence("sample", first_filters).items
        for link in item.links
    )

    merged = fixture.invoke("merge", primary.candidate_id, secondary.candidate_id)
    assert merged.exit_code == 0, merged.output
    merged_id = _result_id(merged, "contribution_id")
    assert workspace.contribution_review("sample", merged_id).status == "confirmed"
    split = fixture.invoke("split", primary.candidate_id, first_item.object_id)
    assert split.exit_code == 0, split.output
    split_id = _result_id(split, "contribution_id")
    split_review = workspace.contribution_review("sample", split_id)
    assert split_review.status == "confirmed"
    members = split_review.packet["evidence_summary"]
    assert isinstance(members, dict)
    assert {member["object_id"] for member in members["members"]} == {first_item.object_id}

    ignored = fixture.invoke("ignore", primary.candidate_id)
    assert ignored.exit_code == 0, ignored.output
    with pytest.raises(NotFound):
        workspace.contribution_review("sample", primary.candidate_id)
    assert fixture.invoke("undo", _result_id(ignored, "decision_id")).exit_code == 0
    assert workspace.contribution_review("sample", split_id).status == "confirmed"

    # Controlled edge state: generated suggestions may be rebuilt away, while
    # an active confirmed lineage remains inspectable from its canonical link.
    writer = connect(workspace.database_path)
    try:
        writer.execute("DELETE FROM candidate_groups WHERE id=?", (primary.candidate_id,))
        writer.commit()
    finally:
        writer.close()
    rowless_link = next(
        link
        for item in workspace.search_evidence("sample", first_filters).items
        for link in item.links
        if link.contribution_id == split_id
    )
    assert workspace.contribution_review("sample", rowless_link.identifier).status == "confirmed"
