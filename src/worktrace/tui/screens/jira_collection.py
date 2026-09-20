from __future__ import annotations

import json
from typing import ClassVar, cast

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Footer, Input, Static

from worktrace.read_workspace import ReadOnlyWorkspace
from worktrace.tui.screens.base import WorkTraceScreen
from worktrace.tui.terminal_text import literal_dynamic_text


class JiraCollectionScreen(WorkTraceScreen):
    """Query-only Jira archive browser with stable-ID navigation."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "app.quit", "Quit"),
        Binding("escape", "back", "Back"),
        Binding("r", "refresh_data", "Refresh"),
        Binding("n", "next_page", "Next page"),
        Binding("p", "previous_page", "Previous page"),
    ]

    def __init__(self, workspace: ReadOnlyWorkspace, collection_id: str) -> None:
        super().__init__()
        self.workspace = workspace
        self.collection_id = collection_id
        self._query = ""
        self._view_token: str | None = None
        self._page: dict[str, object] | None = None
        self._history: list[dict[str, object]] = []
        self._mode = "search"
        self._issue: dict[str, object] | None = None
        self._attachment: dict[str, object] | None = None

    def compose(self) -> ComposeResult:
        yield Static(literal_dynamic_text("Jira collection (read-only)"), classes="page-title")
        with VerticalScroll(id="jira-collection-content"):
            yield Static(id="jira-collection-summary", classes="notice", markup=False)
            yield Input(placeholder="Literal redacted search", id="jira-search-query")
            yield Static(
                "Enter searches; Enter on a result opens the ticket. Esc backs up.",
                id="jira-collection-status",
                markup=False,
            )
            yield DataTable(id="jira-search-results", cursor_type="row", zebra_stripes=True)
            yield Static(id="jira-issue-detail", classes="notice", markup=False)
            yield DataTable(id="jira-issue-attachments", cursor_type="row", zebra_stripes=True)
            yield Static(id="jira-attachment-detail", classes="notice", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#jira-search-results", DataTable).add_columns(
            "Issue", "Attachment", "Locator", "Text"
        )
        self.query_one("#jira-issue-attachments", DataTable).add_columns(
            "Attachment", "MIME", "Original", "Extraction"
        )
        self.query_one("#jira-search-query", Input).focus()
        self.action_refresh_data()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "jira-search-query":
            self._query = event.value
            self._history.clear()
            self._search(None)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "jira-search-results":
            rows = cast(list[dict[str, object]], self._page.get("hits", []) if self._page else [])
            if 0 <= event.cursor_row < len(rows):
                self._open_issue(str(rows[event.cursor_row]["issue_id"]))
        elif event.data_table.id == "jira-issue-attachments" and self._issue is not None:
            attachments = cast(list[dict[str, object]], self._issue.get("attachments", []))
            if 0 <= event.cursor_row < len(attachments):
                self._open_attachment(str(attachments[event.cursor_row]["attachment_id"]))

    def selected_stable_id(self) -> tuple[str, frozenset[str]] | None:
        table = self.focused
        if isinstance(table, DataTable) and table.id == "jira-search-results" and self._page:
            rows = cast(list[dict[str, object]], self._page.get("hits", []))
            if 0 <= table.cursor_row < len(rows):
                return str(rows[table.cursor_row]["issue_id"]), frozenset({"jira-issue"})
        if isinstance(table, DataTable) and table.id == "jira-issue-attachments" and self._issue:
            rows = cast(list[dict[str, object]], self._issue.get("attachments", []))
            if 0 <= table.cursor_row < len(rows):
                return str(rows[table.cursor_row]["attachment_id"]), frozenset({"jira-attachment"})
        return None

    def action_refresh_data(self) -> None:
        try:
            summary = self.workspace.jira_collection_summary(self.collection_id)
            self.query_one("#jira-collection-summary", Static).update(
                literal_dynamic_text(json.dumps(summary, sort_keys=True, indent=2))
            )
            if self._mode == "search":
                next_cursor = self._page.get("next_cursor") if self._page else None
                self._search(next_cursor if isinstance(next_cursor, str) else None)
            elif self._mode == "issue" and self._issue:
                issue = cast(dict[str, object], self._issue.get("issue", {}))
                self._open_issue(str(issue.get("issue_id", "")))
        except Exception as error:
            self.query_one("#jira-collection-status", Static).update(
                literal_dynamic_text(f"Jira collection unavailable: {error}")
            )

    def action_next_page(self) -> None:
        if self._mode == "search" and self._page:
            cursor = self._page.get("next_cursor")
            if isinstance(cursor, str):
                self._history.append(self._page)
                self._search(cursor)

    def action_previous_page(self) -> None:
        if self._mode == "search" and self._history:
            self._page = self._history.pop()
            self._render_search()

    def action_back(self) -> None:
        if self._mode == "attachment":
            self._attachment = None
            self._mode = "issue"
            self._render_issue()
        elif self._mode == "issue":
            self._issue = None
            self._mode = "search"
            self._render_search()
        else:
            self.app.pop_screen()

    def _search(self, cursor: str | None) -> None:
        try:
            page = self.workspace.jira_search(
                self.collection_id,
                self._query,
                cursor=cursor,
                expected_view_token=self._view_token,
            )
            self._page = page
            self._view_token = str(page["view_token"])
            self._mode = "search"
            self._render_search()
        except Exception as error:
            self.query_one("#jira-collection-status", Static).update(
                literal_dynamic_text(f"Jira search unavailable: {error}")
            )

    def _render_search(self) -> None:
        table = self.query_one("#jira-search-results", DataTable)
        table.clear()
        for hit in cast(list[dict[str, object]], self._page.get("hits", []) if self._page else []):
            table.add_row(
                literal_dynamic_text(hit.get("issue_id")),
                literal_dynamic_text(hit.get("attachment_id")),
                literal_dynamic_text(json.dumps(hit.get("locator", {}), sort_keys=True)),
                literal_dynamic_text(hit.get("text")),
            )
        self.query_one("#jira-collection-status", Static).update(
            literal_dynamic_text(
                f"Search {self._query!r} — {len(table.rows)} result(s); n/p page, Enter open"
            )
        )

    def _open_issue(self, issue_id: str) -> None:
        try:
            self._issue = self.workspace.jira_issue(
                self.collection_id, issue_id, expected_view_token=self._view_token
            )
            self._mode = "issue"
            self._render_issue()
        except Exception as error:
            self.query_one("#jira-collection-status", Static).update(
                literal_dynamic_text(f"Jira issue unavailable: {error}")
            )

    def _render_issue(self) -> None:
        if self._issue is None:
            return
        self.query_one("#jira-issue-detail", Static).update(
            literal_dynamic_text(json.dumps(self._issue.get("issue", {}), sort_keys=True, indent=2))
        )
        table = self.query_one("#jira-issue-attachments", DataTable)
        table.clear()
        for attachment in cast(list[dict[str, object]], self._issue.get("attachments", [])):
            table.add_row(
                literal_dynamic_text(attachment.get("attachment_id")),
                literal_dynamic_text(attachment.get("mime_type")),
                literal_dynamic_text(attachment.get("original_state")),
                literal_dynamic_text(attachment.get("extracted_state")),
            )
        self.query_one("#jira-collection-status", Static).update(
            literal_dynamic_text("Ticket detail — Enter attachment, Esc back to search")
        )

    def _open_attachment(self, attachment_id: str) -> None:
        try:
            self._attachment = self.workspace.jira_attachment(
                self.collection_id, attachment_id, expected_view_token=self._view_token
            )
            self._mode = "attachment"
            self.query_one("#jira-attachment-detail", Static).update(
                literal_dynamic_text(json.dumps(self._attachment, sort_keys=True, indent=2))
            )
            self.query_one("#jira-collection-status", Static).update(
                literal_dynamic_text("Attachment/resource detail — Esc back to ticket")
            )
        except Exception as error:
            self.query_one("#jira-collection-status", Static).update(
                literal_dynamic_text(f"Jira attachment unavailable: {error}")
            )
