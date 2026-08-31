from __future__ import annotations

import hashlib
import os
import socket
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from rich.text import Text
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Static, TabbedContent

from tests.test_packets_golden import _packet_state
from worktrace.read_workspace import ReadOnlyWorkspace
from worktrace.tui.app import HelpModal, SmallTerminalScreen, WorkTraceApp
from worktrace.tui.modals.evidence import EvidenceModal
from worktrace.tui.screens.applications import ApplicationScreen
from worktrace.tui.screens.candidates import CandidateScreen
from worktrace.tui.screens.contribution import ContributionScreen


class RecordingWorkTraceApp(WorkTraceApp):
    def __init__(self, workspace: ReadOnlyWorkspace, **kwargs: object) -> None:
        super().__init__(workspace, **kwargs)  # type: ignore[arg-type]
        self.copied: list[str] = []
        self.notifications: list[str] = []

    def copy_to_clipboard(self, text: str) -> None:
        self.copied.append(text)

    def notify(self, message: str, **kwargs: object) -> None:
        self.notifications.append(message)
        super().notify(message, **kwargs)  # type: ignore[arg-type]


def _app(tmp_path: Path, **kwargs: object) -> tuple[RecordingWorkTraceApp, str]:
    connection, _, config, candidate_id = _packet_state(tmp_path)
    connection.close()
    return RecordingWorkTraceApp(ReadOnlyWorkspace(config), **kwargs), candidate_id


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


async def _drag_selection_attempt(
    pilot: object,
    widget: Static,
    *,
    start: tuple[int, int],
    end: tuple[int, int],
) -> None:
    assert await pilot.mouse_down(widget, offset=start)  # type: ignore[attr-defined]
    assert await pilot.mouse_up(widget, offset=end)  # type: ignore[attr-defined]
    await pilot.pause()  # type: ignore[attr-defined]


def _copy_binding_actions(screen: Screen[object]) -> list[str]:
    return [
        binding.action
        for bindings in screen._bindings.key_to_bindings.values()
        for binding in bindings
        if binding.key in {"ctrl+c", "super+c"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_keyboard_contribution_review_journey(tmp_path: Path, size: tuple[int, int]) -> None:
    app, candidate_id = _app(tmp_path)

    async with app.run_test(size=size) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        candidate_screen = app.screen
        assert isinstance(candidate_screen, CandidateScreen)
        table = candidate_screen.query_one("#candidate-table", DataTable)
        await _pause_until(pilot, lambda: table.row_count == 1)
        await pilot.press("enter")
        await _pause_until(
            pilot,
            lambda: isinstance(app.screen, ContributionScreen) and app.screen._review is not None,
        )
        contribution = app.screen
        assert isinstance(contribution, ContributionScreen)
        assert contribution.candidate_id == candidate_id
        assert contribution._review is not None
        packet_questions = [
            str(question["question_id"])
            for questions in contribution._review.packet["sections"].values()
            for question in questions
        ]
        assert [str(question[1]["question_id"]) for question in contribution._questions] == (
            packet_questions
        )
        assert len(packet_questions) == 30

        for key, tab_id in zip(
            "12345", ("summary", "evidence", "participation", "delivery", "questions"), strict=True
        ):
            await pilot.press(key)
            assert contribution.query_one("#review-tabs", TabbedContent).active == tab_id

        await pilot.press("2")
        assert contribution.query_one("#evidence-table", DataTable).has_focus
        await pilot.press("enter")
        await _pause_until(pilot, lambda: isinstance(app.screen, EvidenceModal))
        modal = app.screen
        assert isinstance(modal, EvidenceModal)
        body = modal.query_one("#evidence-body", Static).render()
        assert body.plain == "Synthetic and sanitized evidence."
        assert body.spans == []
        await pilot.press("ctrl+c")
        await pilot.press("super+c")
        assert app.copied == []
        await pilot.press("y")
        assert len(app.copied) == 1
        assert app.copied[0].startswith("obs:")
        await pilot.press("escape")
        assert isinstance(app.screen, ContributionScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, CandidateScreen)


@pytest.mark.asyncio
async def test_multiple_apps_and_scoped_deep_link(tmp_path: Path) -> None:
    from tests.tui_support import add_second_application

    connection, _, config, _ = _packet_state(tmp_path)
    config = add_second_application(connection, config, tmp_path)
    connection.close()
    app = RecordingWorkTraceApp(
        ReadOnlyWorkspace(config),
        initial_app_id="sample_store",
        initial_candidate_id="candidate:other",
    )

    async with app.run_test(size=(120, 40)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, ContributionScreen))
        screen = app.screen
        assert isinstance(screen, ContributionScreen)
        await _pause_until(
            pilot,
            lambda: (
                "outside the selected application"
                in str(screen.query_one("#contribution-message", Static).render())
            ),
        )
        assert screen._review is None

    app = RecordingWorkTraceApp(ReadOnlyWorkspace(config))
    async with app.run_test(size=(120, 40)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, ApplicationScreen))


@pytest.mark.asyncio
async def test_compact_terminal_reduces_candidate_columns(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async with app.run_test(size=(70, 18)) as pilot:
        assert isinstance(app.screen, SmallTerminalScreen)
        terminal_dialog = app.screen.query_one("#terminal-dialog", Static)
        assert terminal_dialog.size.width <= 70
        assert terminal_dialog.size.height <= 18
        await pilot.press("c")
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        table = app.screen.query_one("#candidate-table", DataTable)
        assert len(table.columns) == 4
        assert app.compact_mode is True


@pytest.mark.asyncio
async def test_compact_help_scrolls_to_final_commands_and_q_closes(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async with app.run_test(size=(70, 18)) as pilot:
        assert isinstance(app.screen, SmallTerminalScreen)
        await pilot.press("c")
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        candidate_screen = app.screen
        await pilot.press("?")
        await _pause_until(pilot, lambda: isinstance(app.screen, HelpModal))
        help_modal = app.screen
        assert isinstance(help_modal, HelpModal)
        help_scroll = help_modal.query_one("#help-dialog", VerticalScroll)
        help_content = help_modal.query_one("#help-content", Static)
        assert help_scroll.has_focus
        assert help_scroll.size.width <= 70
        assert help_scroll.size.height <= 18
        assert help_scroll.max_scroll_y > 0
        assert "1-5     Contribution tabs" in help_content.render().plain

        await pilot.press("end")
        await pilot.pause()
        assert help_scroll.scroll_y == help_scroll.max_scroll_y
        await pilot.press("q")
        assert app.screen is candidate_screen


@pytest.mark.asyncio
async def test_modal_mouse_selection_cannot_copy_but_explicit_evidence_id_can(
    tmp_path: Path,
) -> None:
    app, _ = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        candidate_screen = app.screen
        assert isinstance(candidate_screen, CandidateScreen)
        candidate_table = candidate_screen.query_one("#candidate-table", DataTable)
        await _pause_until(pilot, lambda: candidate_table.row_count == 1)

        await pilot.press("?")
        await _pause_until(pilot, lambda: isinstance(app.screen, HelpModal))
        help_modal = app.screen
        assert isinstance(help_modal, HelpModal)
        help_content = help_modal.query_one("#help-content", Static)
        await _drag_selection_attempt(
            pilot,
            help_content,
            start=(1, 1),
            end=(20, 4),
        )
        await pilot.press("ctrl+c", "super+c")
        assert app.copied == []
        assert help_modal.selections == {}
        assert help_modal.get_selected_text() is None
        await pilot.press("q")
        assert app.screen is candidate_screen

        await pilot.press("enter")
        await _pause_until(
            pilot,
            lambda: isinstance(app.screen, ContributionScreen) and app.screen._review is not None,
        )
        await pilot.press("2", "enter")
        await _pause_until(pilot, lambda: isinstance(app.screen, EvidenceModal))
        evidence_modal = app.screen
        assert isinstance(evidence_modal, EvidenceModal)
        evidence_body = evidence_modal.query_one("#evidence-body", Static)
        await _drag_selection_attempt(
            pilot,
            evidence_body,
            start=(1, 0),
            end=(20, 0),
        )
        await pilot.press("ctrl+c", "super+c")
        assert app.copied == []
        assert evidence_modal.selections == {}
        assert evidence_modal.get_selected_text() is None

        selected = evidence_modal.selected_stable_id()
        assert selected is not None
        expected_evidence_id = selected[0]
        assert expected_evidence_id.startswith("obs:")
        await pilot.press("y")
        assert app.copied == [expected_evidence_id]


@pytest.mark.asyncio
async def test_palette_is_fixed_and_screens_disable_selected_text_copy(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)

    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        titles = [command.title for command in app.get_system_commands(app.screen)]
        assert titles == ["Quit", "Keyboard help", "Switch application", "Refresh"]
        assert "Screenshot" not in titles
        assert app.ALLOW_SELECT is False
        assert app.screen.ALLOW_SELECT is False
        assert _copy_binding_actions(app.screen) == []


@pytest.mark.asyncio
async def test_stale_candidate_page_result_is_ignored(tmp_path: Path) -> None:
    from worktrace.read_models.candidates import CandidatePage
    from worktrace.tui.messages import CandidatePageLoaded

    app, _ = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        screen = app.screen
        assert isinstance(screen, CandidateScreen)
        await _pause_until(
            pilot,
            lambda: screen.query_one("#candidate-table", DataTable).row_count == 1,
        )
        original_ids = tuple(item.candidate_id for item in screen._items)
        screen.on_candidate_page_loaded(
            CandidatePageLoaded(
                screen._page_request_id - 1,
                CandidatePage(generation_token=None, items=(), next_cursor=None),
            )
        )
        assert tuple(item.candidate_id for item in screen._items) == original_ids


@pytest.mark.asyncio
async def test_candidate_next_previous_and_generation_restart(tmp_path: Path) -> None:
    from tests.tui_support import add_visible_candidate_page

    connection, _, config, _ = _packet_state(tmp_path)
    add_visible_candidate_page(connection)
    connection.close()
    app = RecordingWorkTraceApp(
        ReadOnlyWorkspace(config),
        initial_app_id="sample_store",
    )
    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        screen = app.screen
        assert isinstance(screen, CandidateScreen)
        table = screen.query_one("#candidate-table", DataTable)
        await _pause_until(pilot, lambda: screen._page is not None)
        assert table.row_count == 25
        assert screen._page is not None and screen._page.next_cursor is not None

        await pilot.press("n")
        await _pause_until(pilot, lambda: table.row_count == 6)
        assert len(screen._cursor_stack) == 2
        await pilot.press("p")
        await _pause_until(pilot, lambda: table.row_count == 25)
        assert len(screen._cursor_stack) == 1

        writer = sqlite3.connect(app.workspace.database_path)
        try:
            writer.execute(
                "UPDATE candidate_groups SET generated_at='2026-08-31T00:00:00+00:00' "
                "WHERE app_id='sample_store'"
            )
            writer.commit()
        finally:
            writer.close()
        await pilot.press("n")
        await _pause_until(
            pilot,
            lambda: any("suggestions changed" in message for message in app.notifications),
        )
        assert screen._cursor_stack == [None]


@pytest.mark.asyncio
async def test_database_busy_and_empty_candidate_states_are_actionable(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        screen = app.screen
        assert isinstance(screen, CandidateScreen)
        await _pause_until(pilot, lambda: screen._page is not None)

        original_candidate_page = app.workspace.candidate_page

        def busy(*args: object, **kwargs: object) -> object:
            from worktrace.read_workspace import DatabaseBusy

            raise DatabaseBusy("secret raw database detail")

        app.workspace.candidate_page = busy  # type: ignore[method-assign]
        await pilot.press("r")
        await _pause_until(
            pilot,
            lambda: "data is busy" in str(screen.query_one("#candidate-message", Static).render()),
        )
        assert "secret raw database detail" not in str(
            screen.query_one("#candidate-message", Static).render()
        )
        app.workspace.candidate_page = original_candidate_page  # type: ignore[method-assign]

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    connection, _, config, _ = _packet_state(empty_root)
    connection.execute("DELETE FROM candidate_members")
    connection.execute("DELETE FROM candidate_groups")
    connection.commit()
    connection.close()
    empty_app = RecordingWorkTraceApp(ReadOnlyWorkspace(config))
    async with empty_app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(empty_app.screen, CandidateScreen))
        screen = empty_app.screen
        assert isinstance(screen, CandidateScreen)
        await _pause_until(
            pilot,
            lambda: (
                "No current contribution candidates"
                in str(screen.query_one("#candidate-message", Static).render())
            ),
        )


@pytest.mark.asyncio
async def test_dynamic_provider_text_is_literal_in_real_ui_sinks(tmp_path: Path) -> None:
    danger = "[bold][@click=app.quit]Danger[/]\x1b]52;c;YQ==\x07"
    connection, _, config, _ = _packet_state(tmp_path)
    connection.execute("UPDATE observations SET title=?, body_text=?", (danger, danger))
    connection.execute(
        "UPDATE sync_runs SET error_summary=? WHERE id='run:jira_partial'",
        (danger,),
    )
    connection.execute("UPDATE candidate_groups SET suggested_title=?", (danger,))
    connection.commit()
    connection.close()
    app = RecordingWorkTraceApp(ReadOnlyWorkspace(config))

    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        candidate = app.screen
        assert isinstance(candidate, CandidateScreen)
        table = candidate.query_one("#candidate-table", DataTable)
        await _pause_until(pilot, lambda: table.row_count == 1)
        title_cell = table.get_row_at(0)[1]
        assert isinstance(title_cell, Text)
        assert "[bold][@click=app.quit]Danger[/]<ESC>]52" in title_cell.plain
        assert title_cell.spans == []
        status_render = candidate.query_one("#source-status", Static).render()
        assert "[bold][@click=app.quit]Danger[/]<ESC>]52" in status_render.plain
        assert status_render.spans == []

        await pilot.press("enter")
        await _pause_until(
            pilot,
            lambda: isinstance(app.screen, ContributionScreen) and app.screen._review is not None,
        )
        contribution = app.screen
        assert isinstance(contribution, ContributionScreen)
        title_render = contribution.query_one("#contribution-title", Static).render()
        assert "[bold][@click=app.quit]Danger[/]<ESC>]52" in title_render.plain
        assert title_render.spans == []
        await pilot.press("2", "enter")
        await _pause_until(pilot, lambda: isinstance(app.screen, EvidenceModal))
        body = app.screen.query_one("#evidence-body", Static).render()
        assert "[bold][@click=app.quit]Danger[/]<ESC>]52" in body.plain
        assert body.spans == []


def _directory_fingerprint(path: Path) -> dict[str, str]:
    return {
        str(file.relative_to(path)): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(path.rglob("*"))
        if file.is_file()
    }


@pytest.mark.asyncio
async def test_tui_journey_cannot_reach_providers_network_or_managed_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    import worktrace.config as config_module
    import worktrace.local_security as local_security_module
    from worktrace.adapters.git_local import LocalGitAdapter
    from worktrace.adapters.gitlab import GitLabAdapter
    from worktrace.adapters.jira import JiraAdapter

    app, _ = _app(tmp_path)
    before = _directory_fingerprint(tmp_path)

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("forbidden capability reached")

    monkeypatch.setattr(config_module, "jira_credentials", forbidden)
    monkeypatch.setattr(config_module, "gitlab_credentials", forbidden)
    monkeypatch.setattr(local_security_module, "email_hmac_key", forbidden)
    monkeypatch.setattr(LocalGitAdapter, "__init__", forbidden)
    monkeypatch.setattr(JiraAdapter, "__init__", forbidden)
    monkeypatch.setattr(GitLabAdapter, "__init__", forbidden)
    monkeypatch.setattr(httpx.Client, "__init__", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)

    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        table = app.screen.query_one("#candidate-table", DataTable)
        await _pause_until(pilot, lambda: table.row_count == 1)
        await pilot.press("enter")
        await _pause_until(
            pilot,
            lambda: isinstance(app.screen, ContributionScreen) and app.screen._review is not None,
        )

    assert _directory_fingerprint(tmp_path) == before


@pytest.mark.asyncio
async def test_stale_review_and_excerpt_results_are_ignored(tmp_path: Path) -> None:
    from worktrace.tui.messages import ContributionReviewLoaded, EvidenceExcerptLoaded

    app, _ = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        table = app.screen.query_one("#candidate-table", DataTable)
        await _pause_until(pilot, lambda: table.row_count == 1)
        await pilot.press("enter")
        await _pause_until(
            pilot,
            lambda: isinstance(app.screen, ContributionScreen) and app.screen._review is not None,
        )
        screen = app.screen
        assert isinstance(screen, ContributionScreen)
        assert screen._review is not None
        current = screen._review
        screen.on_contribution_review_loaded(
            ContributionReviewLoaded(
                screen._review_request_id - 1,
                replace(current, status="stale result"),
            )
        )
        assert screen._review is current
        stale_excerpt_request_id = screen._excerpt_request_id
        screen.action_refresh()
        screen.on_evidence_excerpt_loaded(
            EvidenceExcerptLoaded(
                stale_excerpt_request_id,
                {"evidence_id": "obs:stale", "app_id": "sample_store"},
            )
        )
        assert app.screen is screen
        await _pause_until(
            pilot,
            lambda: (
                "Read-only review loaded"
                in str(screen.query_one("#contribution-message", Static).render())
            ),
        )


@pytest.mark.asyncio
async def test_rapid_screen_replacement_has_no_deferred_widget_callbacks(tmp_path: Path) -> None:
    from textual.widgets import Header

    app, _ = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        for _ in range(4):
            app.show_applications()
            app.open_application("sample_store")
        await _pause_until(pilot, lambda: isinstance(app.screen, CandidateScreen))
        await pilot.pause()
        assert app.screen.query_one("#candidate-table", DataTable) is not None
        assert list(app.query(Header)) == []


def test_no_color_is_honored_in_a_fresh_process() -> None:
    script = """
import asyncio
from worktrace.tui.app import WorkTraceApp

class EmptyWorkspace:
    def applications(self):
        return ()

async def main():
    app = WorkTraceApp(EmptyWorkspace())
    async with app.run_test(size=(80, 24)) as pilot:
        for _ in range(4):
            await pilot.pause()
        assert 'nocolor' in app.pseudo_classes

asyncio.run(main())
"""
    environment = dict(os.environ)
    environment["NO_COLOR"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
