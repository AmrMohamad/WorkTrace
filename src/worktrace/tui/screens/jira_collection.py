from __future__ import annotations

import json
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widgets import Footer, Static

from worktrace.read_workspace import ReadOnlyWorkspace
from worktrace.tui.screens.base import WorkTraceScreen
from worktrace.tui.terminal_text import literal_dynamic_text


class JiraCollectionScreen(WorkTraceScreen):
    """Read-only Jira collection view; no vault/provider capability is exposed."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q,escape", "app.quit", "Quit"),
        Binding("r", "refresh_data", "Refresh"),
    ]

    def __init__(self, workspace: ReadOnlyWorkspace, collection_id: str) -> None:
        super().__init__()
        self.workspace = workspace
        self.collection_id = collection_id

    def compose(self) -> ComposeResult:
        yield Static(literal_dynamic_text("Jira collection (read-only)"), classes="page-title")
        with VerticalScroll(id="jira-collection-content"):
            yield Static("Loading…", id="jira-collection-summary", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.action_refresh_data()

    def action_refresh_data(self) -> None:
        try:
            summary = self.workspace.jira_collection_summary(self.collection_id)
            encoded = literal_dynamic_text(json.dumps(summary, sort_keys=True, indent=2))
            self.query_one("#jira-collection-summary", Static).update(encoded)
        except Exception as error:
            self.query_one("#jira-collection-summary", Static).update(
                literal_dynamic_text(f"Jira collection unavailable: {error}")
            )
