from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input, Static

from tests.public_workflow_support import PublicWorkflowFixture
from tests.test_packets_golden import _packet_state
from worktrace.config import load_config
from worktrace.read_models.evidence_search import EvidenceSearchInvalidated
from worktrace.read_workspace import DatabaseBusy, ReadOnlyWorkspace
from worktrace.tui.app import WorkTraceApp
from worktrace.tui.messages import EvidenceSearchPageLoaded
from worktrace.tui.modals.evidence import EvidenceModal
from worktrace.tui.screens.applications import ApplicationScreen
from worktrace.tui.screens.candidates import CandidateScreen
from worktrace.tui.screens.contribution import ContributionScreen
from worktrace.tui.screens.evidence_search import EvidenceSearchScreen


class RecordingWorkTraceApp(WorkTraceApp):
    def __init__(self, workspace: ReadOnlyWorkspace, **kwargs: object) -> None:
        super().__init__(workspace, **kwargs)  # type: ignore[arg-type]
        self.copied: list[str] = []

    def copy_to_clipboard(self, text: str) -> None:
        self.copied.append(text)


async def _pause_until(
    pilot: object,
    predicate: object,
    *,
    attempts: int = 80,
) -> None:
    for _ in range(attempts):
        if predicate():  # type: ignore[operator]
            await pilot.pause()  # type: ignore[attr-defined]
            return
        await pilot.pause()  # type: ignore[attr-defined]
    raise AssertionError("Textual state did not settle")


def _app(tmp_path: Path) -> RecordingWorkTraceApp:
    connection, _, config, candidate_id = _packet_state(tmp_path)
    assert candidate_id == "candidate:phase4"
    connection.close()
    return RecordingWorkTraceApp(ReadOnlyWorkspace(config), initial_app_id="sample_store")


def _imported_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RecordingWorkTraceApp:
    fixture = PublicWorkflowFixture.create(tmp_path, monkeypatch)
    assert fixture.invoke("init").exit_code == 0
    imported = fixture.invoke("import", "all", "sample")
    assert imported.exit_code == 0, imported.output
    rebuilt = fixture.invoke("rebuild", "all", "sample")
    assert rebuilt.exit_code == 0, rebuilt.output
    return RecordingWorkTraceApp(
        ReadOnlyWorkspace(load_config(fixture.config_path)), initial_app_id="sample"
    )


def _paged_imported_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[RecordingWorkTraceApp, PublicWorkflowFixture]:
    fixture = PublicWorkflowFixture.create(tmp_path, monkeypatch)
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Fixture Engineer",
            "GIT_AUTHOR_EMAIL": "old-alias@example.test",
            "GIT_COMMITTER_NAME": "Fixture Engineer",
            "GIT_COMMITTER_EMAIL": "integrator@example.test",
            "GIT_AUTHOR_DATE": "2026-04-02T12:00:00Z",
            "GIT_COMMITTER_DATE": "2026-04-02T12:00:00Z",
        }
    )
    for index in range(22):
        path = fixture.repository_path / f"page-{index:02d}.txt"
        path.write_text(f"page {index}\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", path.name],
            check=True,
            cwd=fixture.repository_path,
            env=environment,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"PAGE SEARCH {index:02d}"],
            check=True,
            cwd=fixture.repository_path,
            env=environment,
            capture_output=True,
            text=True,
        )
    assert fixture.invoke("init").exit_code == 0
    imported = fixture.invoke("import", "all", "sample")
    assert imported.exit_code == 0, imported.output
    rebuilt = fixture.invoke("rebuild", "all", "sample")
    assert rebuilt.exit_code == 0, rebuilt.output
    return (
        RecordingWorkTraceApp(
            ReadOnlyWorkspace(load_config(fixture.config_path)), initial_app_id="sample"
        ),
        fixture,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("size", "minimum_results", "minimum_links"),
    [((80, 24), 5, 6), ((120, 40), 13, 14)],
)
async def test_evidence_search_keyboard_journey_preserves_search_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int],
    minimum_results: int,
    minimum_links: int,
) -> None:
    app = _imported_app(tmp_path, monkeypatch)

    async with app.run_test(size=size) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        candidates = app.screen
        assert isinstance(candidates, CandidateScreen)
        await _pause_until(
            pilot,
            lambda: (
                candidates.query_one("#candidate-table", DataTable).row_count > 0
                and candidates._page is not None
            ),
        )
        await pilot.press("/")
        await _pause_until(pilot, lambda: isinstance(app.screen, EvidenceSearchScreen))
        search = app.screen
        assert isinstance(search, EvidenceSearchScreen)
        query = search.query_one("#evidence-search-query", Input)
        results = search.query_one("#evidence-search-results", DataTable)
        links = search.query_one("#evidence-search-links", DataTable)
        assert query.has_focus
        assert results.size.height >= minimum_results
        assert links.size.height >= minimum_links

        await pilot.press(*"demo-1", "space", *"context", "enter")
        await _pause_until(pilot, lambda: results.row_count > 0 and search._page is not None)
        successful_page = search._page
        assert successful_page is not None
        assert search._submitted_filters is not None
        assert search._submitted_filters.query == "demo-1 context"
        assert "Submitted filters: query: demo-1 context" in str(
            search.query_one("#evidence-search-status", Static).render()
        )

        # Editable fields keep printable navigation keys as text.  Once focus leaves the
        # form, Tab reaches the result and canonical-link tables in deterministic order.
        await pilot.press("n", "p")
        assert query.value.endswith("np")
        await pilot.press("tab", "tab", "tab", "tab", "tab", "tab")
        assert results.has_focus
        linked_result_row = next(index for index, item in enumerate(search._items) if item.links)
        for _ in range(linked_result_row):
            await pilot.press("j")
        await _pause_until(pilot, lambda: links.row_count > 0)
        selected = search._items[results.cursor_row]
        detail = search.query_one("#evidence-search-result-detail", Static).render()
        assert detail.plain == (
            f"Observation ID: {selected.evidence_id}\nSource object ID: {selected.object_id}"
        )
        assert detail.spans == []
        await pilot.press("y")
        assert app.copied == [selected.evidence_id]

        await pilot.press("enter")
        await _pause_until(pilot, lambda: isinstance(app.screen, EvidenceModal))
        await pilot.press("escape")
        assert app.screen is search
        assert (
            search.query_one("#evidence-search-results", DataTable).cursor_row == linked_result_row
        )

        await pilot.press("tab")
        await _pause_until(pilot, lambda: links.has_focus and links.row_count > 0)
        link_id = search._selected_links[links.cursor_row].identifier
        await pilot.press("y")
        assert app.copied[-1] == link_id
        await pilot.press("enter")
        await _pause_until(pilot, lambda: isinstance(app.screen, ContributionScreen))
        assert len(app.screen_stack) == 4
        await pilot.press("escape")
        assert app.screen is search
        assert len(app.screen_stack) == 3
        await pilot.press("escape")
        assert app.screen is candidates
        assert len(app.screen_stack) == 2

        # Invalid replacement input starts no worker and cannot discard the successful page.
        await pilot.press("/")
        await _pause_until(pilot, lambda: isinstance(app.screen, EvidenceSearchScreen))
        retry_search = app.screen
        assert isinstance(retry_search, EvidenceSearchScreen)
        retry_query = retry_search.query_one("#evidence-search-query", Input)
        await pilot.press(*"demo-1", "space", *"context", "enter")
        await _pause_until(pilot, lambda: retry_search._page is not None)
        retained = retry_search._page
        retry_query.focus()
        retry_query.value = ""
        await pilot.press("enter")
        assert retry_search._page is retained
        assert "query must contain" in str(
            retry_search.query_one("#evidence-search-message", Static).render()
        )


@pytest.mark.asyncio
async def test_search_invalidation_failed_restart_clears_every_copyable_state(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        candidates = app.screen
        assert isinstance(candidates, CandidateScreen)
        await _pause_until(
            pilot,
            lambda: candidates.query_one("#candidate-table", DataTable).row_count == 1,
        )
        await pilot.press("/")
        await _pause_until(pilot, lambda: isinstance(app.screen, EvidenceSearchScreen))
        search = app.screen
        assert isinstance(search, EvidenceSearchScreen)
        await pilot.press(*"checkout", "enter")
        results = search.query_one("#evidence-search-results", DataTable)
        links = search.query_one("#evidence-search-links", DataTable)
        await _pause_until(pilot, lambda: results.row_count > 0 and search._page is not None)

        calls = 0

        def invalidated_then_busy(*_args: object, **_kwargs: object) -> object:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise EvidenceSearchInvalidated("synthetic revision change")
            raise DatabaseBusy("synthetic busy database detail")

        original = app.workspace.search_evidence
        app.workspace.search_evidence = invalidated_then_busy  # type: ignore[method-assign]
        try:
            results.focus()
            await pilot.pause()
            await pilot.press("r")
            await _pause_until(pilot, lambda: calls == 2 and search._pending_direction is None)
        finally:
            app.workspace.search_evidence = original  # type: ignore[method-assign]

        message = str(search.query_one("#evidence-search-message", Static).render()).casefold()
        assert search._page is None
        assert search._items == ()
        assert search._selected_links == ()
        assert search._committed_history == []
        assert results.row_count == 0
        assert links.row_count == 0
        assert search.selected_stable_id() is None
        await pilot.press("y")
        assert app.copied == []
        assert "retry" in message
        assert "successful search page remains" not in message


@pytest.mark.asyncio
async def test_late_search_page_cannot_replace_the_mounted_request(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        candidates = app.screen
        assert isinstance(candidates, CandidateScreen)
        await _pause_until(
            pilot,
            lambda: candidates.query_one("#candidate-table", DataTable).row_count == 1,
        )
        await pilot.press("/")
        await _pause_until(pilot, lambda: isinstance(app.screen, EvidenceSearchScreen))
        search = app.screen
        assert isinstance(search, EvidenceSearchScreen)
        await pilot.press(*"checkout", "enter")
        results = search.query_one("#evidence-search-results", DataTable)
        await _pause_until(pilot, lambda: search._page is not None and results.row_count > 0)
        committed = search._page
        assert committed is not None
        committed_ids = tuple(item.evidence_id for item in search._items)

        search.on_evidence_search_page_loaded(
            EvidenceSearchPageLoaded(search._page_request_id - 1, committed)
        )
        assert search._page is committed
        assert tuple(item.evidence_id for item in search._items) == committed_ids


@pytest.mark.asyncio
async def test_evidence_search_screen_disables_selection_and_ambient_copy(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        candidates = app.screen
        assert isinstance(candidates, CandidateScreen)
        await _pause_until(pilot, lambda: candidates._page is not None)
        await pilot.press("/")
        await _pause_until(pilot, lambda: isinstance(app.screen, EvidenceSearchScreen))
        search = app.screen
        assert isinstance(search, EvidenceSearchScreen)
        copy_actions = [
            binding.action
            for bindings in search._bindings.key_to_bindings.values()
            for binding in bindings
            if binding.key in {"ctrl+c", "super+c"}
        ]
        assert search.ALLOW_SELECT is False
        assert copy_actions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("switch_application", [False, True])
async def test_late_held_search_cannot_replace_superseding_query_or_application_screen(
    tmp_path: Path,
    switch_application: bool,
) -> None:
    app = _app(tmp_path)
    started = threading.Event()
    release = threading.Event()
    original = app.workspace.search_evidence
    calls = 0

    def held_first(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(timeout=5)
        return original(*args, **kwargs)

    app.workspace.search_evidence = held_first  # type: ignore[method-assign]
    try:
        async with app.run_test(size=(80, 24)) as pilot:
            await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
            candidates = app.screen
            assert isinstance(candidates, CandidateScreen)
            await _pause_until(pilot, lambda: candidates._page is not None)
            await pilot.press("/")
            await _pause_until(pilot, lambda: isinstance(app.screen, EvidenceSearchScreen))
            search = app.screen
            assert isinstance(search, EvidenceSearchScreen)
            await pilot.press(*"synthetic", "enter")
            assert started.wait(timeout=5)

            if switch_application:
                app.action_switch_application()
                await _pause_until(pilot, lambda: isinstance(app.screen, ApplicationScreen))
                application_screen = app.screen
                assert isinstance(application_screen, ApplicationScreen)
                stack_depth = len(app.screen_stack)
                release.set()
                await pilot.pause()
                assert app.screen is application_screen
                assert len(app.screen_stack) == stack_depth
            else:
                query = search.query_one("#evidence-search-query", Input)
                query.value = "checkout"
                await pilot.press("enter")
                results = search.query_one("#evidence-search-results", DataTable)
                await _pause_until(
                    pilot, lambda: search._page is not None and results.row_count > 0
                )
                committed = search._page
                assert committed is not None
                release.set()
                await pilot.pause()
                assert app.screen is search
                assert search._page is committed
                assert search._submitted_filters is not None
                assert search._submitted_filters.query == "checkout"
                assert search._pending_direction is None
    finally:
        release.set()
        app.workspace.search_evidence = original  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_previous_page_detects_cli_revision_change_before_republishing_page_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, fixture = _paged_imported_app(tmp_path, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        candidates = app.screen
        assert isinstance(candidates, CandidateScreen)
        await _pause_until(pilot, lambda: candidates._page is not None)
        await pilot.press("/")
        await _pause_until(pilot, lambda: isinstance(app.screen, EvidenceSearchScreen))
        search = app.screen
        assert isinstance(search, EvidenceSearchScreen)
        await pilot.press(*"page", "space", *"search", "enter")
        results = search.query_one("#evidence-search-results", DataTable)
        await _pause_until(
            pilot,
            lambda: search._page is not None and results.row_count == 20,
        )
        first_revision = search._page.revision
        results.focus()
        await pilot.press("n")
        await _pause_until(
            pilot,
            lambda: (
                search._page is not None
                and len(search._committed_history) == 2
                and results.row_count == 2
            ),
        )
        assert search._page.revision == first_revision

        changed = fixture.invoke("rebuild", "all", "sample")
        assert changed.exit_code == 0, changed.output
        await pilot.press("p")
        await _pause_until(
            pilot,
            lambda: (
                search._pending_direction is None
                and search._page is not None
                and len(search._committed_history) == 1
                and search._page.revision != first_revision
            ),
        )
        assert results.row_count == 20
        assert app.copied == []


@pytest.mark.asyncio
async def test_failed_valid_replacement_search_retains_successful_page_and_filters(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        candidates = app.screen
        assert isinstance(candidates, CandidateScreen)
        await _pause_until(pilot, lambda: candidates._page is not None)
        await pilot.press("/")
        await _pause_until(pilot, lambda: isinstance(app.screen, EvidenceSearchScreen))
        search = app.screen
        assert isinstance(search, EvidenceSearchScreen)
        await pilot.press(*"checkout", "enter")
        results = search.query_one("#evidence-search-results", DataTable)
        await _pause_until(pilot, lambda: search._page is not None and results.row_count > 0)
        retained_page = search._page
        retained_ids = tuple(item.evidence_id for item in search._items)
        retained_filters = search._submitted_filters

        def fail_replacement(*_args: object, **_kwargs: object) -> object:
            raise DatabaseBusy("synthetic replacement failure")

        original = app.workspace.search_evidence
        app.workspace.search_evidence = fail_replacement  # type: ignore[method-assign]
        try:
            query = search.query_one("#evidence-search-query", Input)
            query.value = "replacement"
            await pilot.press("enter")
            await _pause_until(pilot, lambda: search._pending_direction is None)
        finally:
            app.workspace.search_evidence = original  # type: ignore[method-assign]

        assert search._page is retained_page
        assert tuple(item.evidence_id for item in search._items) == retained_ids
        assert search._submitted_filters is not retained_filters
        assert search._submitted_filters is not None
        assert search._submitted_filters.query == "replacement"
        assert retained_page.filters == retained_filters
        assert retained_page.filters.query == "checkout"
        assert len(search._committed_history) == 1
        assert results.row_count == len(retained_ids)
        assert (
            "successful search page remains"
            in str(search.query_one("#evidence-search-message", Static).render()).casefold()
        )
