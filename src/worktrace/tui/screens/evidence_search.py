from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal, cast

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widgets import Button, DataTable, Footer, Input, Static

from worktrace.read_models.evidence_search import (
    CandidateLink,
    EvidenceSearchCursor,
    EvidenceSearchFilters,
    EvidenceSearchItem,
    EvidenceSearchPage,
    EvidenceSearchValidationError,
    normalize_evidence_search_filters,
)
from worktrace.tui.messages import (
    EvidenceExcerptLoaded,
    EvidenceSearchPageLoaded,
    FailureKind,
    ReadFailed,
    failure_kind,
)
from worktrace.tui.modals.evidence import EvidenceModal
from worktrace.tui.screens.base import WorkTraceScreen
from worktrace.tui.terminal_text import literal_dynamic_text

if TYPE_CHECKING:
    from worktrace.read_workspace import ApplicationSummary
    from worktrace.tui.app import WorkTraceApp


_PageDirection = Literal["submit", "next", "previous", "restart"]
_EVIDENCE_PREFIXES = frozenset(
    {"obs", "participation", "part", "availability", "decision", "ref", "reference"}
)
_CONTRIBUTION_PREFIXES = frozenset({"candidate", "contribution"})

_FAILURE_TEXT = {
    FailureKind.BUSY: "WorkTrace data is busy.",
    FailureKind.EVIDENCE_CHANGED: "Evidence changed while searching. Restarting this search once.",
    FailureKind.NOT_FOUND: "The requested evidence is no longer available. Retry the search.",
    FailureKind.OUT_OF_SCOPE: "The requested evidence is outside this application.",
    FailureKind.UPGRADE_REQUIRED: "Exit and run `worktrace init`, then retry.",
    FailureKind.UNSUPPORTED_NEWER: "Upgrade WorkTrace before opening this database.",
    FailureKind.DATABASE: "WorkTrace could not read the database. Run `worktrace doctor`.",
    FailureKind.UNEXPECTED: "WorkTrace could not complete this read.",
}


@dataclass(slots=True)
class _HistoryEntry:
    cursor: EvidenceSearchCursor | None
    revision: int
    result_row: int = 0
    link_row: int = 0


def _append_dynamic(output: Text, value: object) -> None:
    output.append_text(literal_dynamic_text(value))


def _append_filter(output: Text, label: str, value: str | None) -> None:
    output.append(f"{label}: ")
    _append_dynamic(output, value or "any")
    output.append("  ")


def _readiness_summary(readiness: object) -> str:
    """Return a bounded, literal-safe summary without treating source state as truth."""
    if not isinstance(readiness, Mapping):
        return "not recorded"
    sources = sorted(str(source) for source in readiness)
    return ", ".join(sources) if sources else "no source status recorded"


class EvidenceSearchScreen(WorkTraceScreen):
    """Bounded, explicitly submitted evidence discovery above candidate review."""

    ALLOW_SELECT = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("n", "next_page", "Next page"),
        Binding("p", "previous_page", "Previous page"),
        Binding("r", "retry", "Retry"),
        Binding("a", "app.switch_application", "Switch app"),
        Binding("y", "app.copy_selected_id", "Copy ID"),
        Binding("q,escape", "back", "Back"),
    ]

    def __init__(self, application: ApplicationSummary) -> None:
        super().__init__()
        self.application = application
        self._draft_query = ""
        self._draft_source = ""
        self._draft_module = ""
        self._draft_from = ""
        self._draft_to = ""
        self._submitted_filters: EvidenceSearchFilters | None = None
        self._page: EvidenceSearchPage | None = None
        self._items: tuple[EvidenceSearchItem, ...] = ()
        self._selected_links: tuple[CandidateLink, ...] = ()
        self._committed_history: list[_HistoryEntry] = []
        self._pending_direction: _PageDirection | None = None
        self._pending_cursor: EvidenceSearchCursor | None = None
        self._pending_filters: EvidenceSearchFilters | None = None
        self._pending_expected_revision: int | None = None
        self._page_request_id = 0
        self._excerpt_request_id = 0
        self._restart_attempted = False

    def compose(self) -> ComposeResult:
        title = Text("Evidence search / ")
        _append_dynamic(title, self.application.name)
        yield Static(title, id="evidence-search-title", classes="page-title", markup=False)
        yield Static(
            "Enter literal search terms, then choose Search. No search runs automatically.",
            id="evidence-search-message",
            markup=False,
        )
        with VerticalScroll(id="evidence-search-form", can_focus=True):
            yield Input(placeholder="Query (required)", id="evidence-search-query")
            yield Input(
                placeholder="Source: git, jira, gitlab, or manual",
                id="evidence-search-source",
            )
            yield Input(placeholder="Module text (optional)", id="evidence-search-module")
            yield Input(placeholder="From: YYYY-MM-DD (optional)", id="evidence-search-from")
            yield Input(placeholder="To: YYYY-MM-DD (optional)", id="evidence-search-to")
            yield Button("Search", id="evidence-search-submit", variant="primary")
        yield Static(id="evidence-search-status", classes="notice", markup=False)
        yield DataTable(id="evidence-search-results", cursor_type="row", zebra_stripes=True)
        yield Static(
            "No selected evidence result.",
            id="evidence-search-result-detail",
            classes="notice",
            markup=False,
        )
        yield DataTable(id="evidence-search-links", cursor_type="row", zebra_stripes=True)
        yield Static(id="evidence-search-link-detail", classes="notice", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        results = self.query_one("#evidence-search-results", DataTable)
        results.add_columns("Source", "Kind", "Evidence", "Period", "Links")
        links = self.query_one("#evidence-search-links", DataTable)
        links.add_columns("Contribution", "State", "Role", "Basis", "Evidence")
        self.query_one("#evidence-search-query", Input).focus()
        self._render_status()

    def on_unmount(self) -> None:
        # Workers own their connections; invalidating IDs makes late messages inert.
        self._page_request_id += 1
        self._excerpt_request_id += 1

    def on_input_changed(self, event: Input.Changed) -> None:
        value = event.value
        if event.input.id == "evidence-search-query":
            self._draft_query = value
        elif event.input.id == "evidence-search-source":
            self._draft_source = value
        elif event.input.id == "evidence-search-module":
            self._draft_module = value
        elif event.input.id == "evidence-search-from":
            self._draft_from = value
        elif event.input.id == "evidence-search-to":
            self._draft_to = value

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "evidence-search-submit":
            self.action_submit()

    def action_submit(self) -> None:
        try:
            filters = normalize_evidence_search_filters(
                self._draft_query,
                source=self._draft_source,
                module_text=self._draft_module,
                date_from=self._draft_from,
                date_to=self._draft_to,
            )
        except EvidenceSearchValidationError as error:
            self.query_one("#evidence-search-message", Static).update(literal_dynamic_text(error))
            return
        self._restart_attempted = False
        self._submitted_filters = filters
        self._begin_page_load(None, "submit", filters, expected_revision=None)

    def action_next_page(self) -> None:
        if self._editable_focus() or self._page_load_in_flight():
            return
        if self._page is None:
            self.app.notify("Submit a search before moving to another page.", markup=False)
            return
        if self._page.next_cursor is None:
            self.app.notify("No later evidence page is available.", markup=False)
            return
        self._save_current_selection()
        self._begin_page_load(
            self._page.next_cursor,
            "next",
            self._page.filters,
            expected_revision=self._page.revision,
        )

    def action_previous_page(self) -> None:
        if self._editable_focus() or self._page_load_in_flight():
            return
        if len(self._committed_history) <= 1 or self._page is None:
            self.app.notify("Already on the first evidence page.", markup=False)
            return
        self._save_current_selection()
        self._begin_page_load(
            self._committed_history[-2].cursor,
            "previous",
            self._page.filters,
            expected_revision=self._committed_history[-2].revision,
        )

    def action_retry(self) -> None:
        if self._page_load_in_flight() or self._submitted_filters is None:
            return
        self._restart_attempted = False
        self._begin_page_load(None, "restart", self._submitted_filters, expected_revision=None)

    def action_back(self) -> None:
        self._page_request_id += 1
        self._excerpt_request_id += 1
        self.app.pop_screen()

    def selected_stable_id(self) -> tuple[str, frozenset[str]] | None:
        focused = self.focused
        if (
            isinstance(focused, DataTable)
            and focused.id == "evidence-search-results"
            and 0 <= focused.cursor_row < len(self._items)
        ):
            return self._items[focused.cursor_row].evidence_id, _EVIDENCE_PREFIXES
        if (
            isinstance(focused, DataTable)
            and focused.id == "evidence-search-links"
            and 0 <= focused.cursor_row < len(self._selected_links)
        ):
            return self._selected_links[focused.cursor_row].identifier, _CONTRIBUTION_PREFIXES
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "evidence-search-results":
            self._open_evidence_row(event.cursor_row)
        elif event.data_table.id == "evidence-search-links":
            self._open_link_row(event.cursor_row)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "evidence-search-results":
            self._select_result(event.cursor_row)
        elif event.data_table.id == "evidence-search-links":
            self._render_link_detail(event.cursor_row)

    def _editable_focus(self) -> bool:
        return isinstance(self.focused, Input)

    def _page_load_in_flight(self) -> bool:
        return self._pending_direction is not None

    def _begin_page_load(
        self,
        cursor: EvidenceSearchCursor | None,
        direction: _PageDirection,
        filters: EvidenceSearchFilters,
        *,
        expected_revision: int | None,
    ) -> None:
        self._page_request_id += 1
        request_id = self._page_request_id
        self._pending_direction = direction
        self._pending_cursor = cursor
        self._pending_filters = filters
        self._pending_expected_revision = expected_revision
        self.query_one("#evidence-search-message", Static).update("Searching current evidence…")
        self.search_evidence_page(request_id, filters, cursor, expected_revision)

    @work(thread=True, exclusive=True, group="evidence-search-page", exit_on_error=False)
    def search_evidence_page(
        self,
        request_id: int,
        filters: EvidenceSearchFilters,
        cursor: EvidenceSearchCursor | None,
        expected_revision: int | None,
    ) -> None:
        try:
            page = cast("WorkTraceApp", self.app).workspace.search_evidence(
                self.application.app_id,
                filters,
                cursor=cursor,
                expected_revision=expected_revision,
            )
        except Exception as error:
            self.post_message(ReadFailed("evidence_search", request_id, failure_kind(error)))
        else:
            self.post_message(EvidenceSearchPageLoaded(request_id, page))

    @work(thread=True, exclusive=True, group="evidence-search-excerpt", exit_on_error=False)
    def load_excerpt(self, request_id: int, evidence_id: str) -> None:
        try:
            excerpt = cast("WorkTraceApp", self.app).workspace.evidence_excerpt(
                self.application.app_id,
                evidence_id,
                max_chars=4_000,
            )
        except Exception as error:
            self.post_message(
                ReadFailed("evidence_search_excerpt", request_id, failure_kind(error))
            )
        else:
            self.post_message(EvidenceExcerptLoaded(request_id, excerpt))

    def on_evidence_search_page_loaded(self, message: EvidenceSearchPageLoaded) -> None:
        if not self._accept_page_response(message.request_id):
            return
        direction = self._pending_direction
        cursor = self._pending_cursor
        filters = self._pending_filters
        expected_revision = self._pending_expected_revision
        self._pending_direction = None
        self._pending_cursor = None
        self._pending_filters = None
        self._pending_expected_revision = None
        if direction is None or filters is None:
            return
        if expected_revision is not None and message.page.revision != expected_revision:
            self._restart_after_invalidation()
            return
        if direction == "submit":
            self._committed_history = [_HistoryEntry(cursor, message.page.revision)]
        elif direction == "next":
            self._committed_history.append(_HistoryEntry(cursor, message.page.revision))
        elif direction == "previous" and len(self._committed_history) > 1:
            self._committed_history.pop()
        elif direction == "restart":
            self._committed_history = [_HistoryEntry(cursor, message.page.revision)]
        self._page = message.page
        self._items = message.page.items
        self._render_page()
        self._restore_current_selection()
        self._render_status()
        self.query_one("#evidence-search-message", Static).update(
            "Read-only evidence search loaded."
        )

    def on_evidence_excerpt_loaded(self, message: EvidenceExcerptLoaded) -> None:
        if message.request_id != self._excerpt_request_id:
            return
        self.query_one("#evidence-search-message", Static).update(
            "Read-only evidence search loaded."
        )
        self.app.push_screen(EvidenceModal(message.excerpt))

    def on_read_failed(self, message: ReadFailed) -> None:
        if message.operation == "evidence_search_excerpt":
            if message.request_id == self._excerpt_request_id:
                self.query_one("#evidence-search-message", Static).update(
                    _FAILURE_TEXT[message.kind]
                )
            return
        if message.operation != "evidence_search" or not self._accept_page_response(
            message.request_id
        ):
            return
        direction = self._pending_direction
        self._pending_direction = None
        self._pending_cursor = None
        self._pending_filters = None
        self._pending_expected_revision = None
        if message.kind is FailureKind.EVIDENCE_CHANGED:
            self._restart_after_invalidation()
            return
        failure = _FAILURE_TEXT[message.kind]
        if self._page is None:
            if direction == "restart":
                failure += " The automatic restart failed. No stale rows remain. Press r to retry."
            else:
                failure += " No evidence page was loaded. Press r to retry."
        else:
            failure += " The successful search page remains available."
        self.query_one("#evidence-search-message", Static).update(failure)

    def _restart_after_invalidation(self) -> None:
        if self._submitted_filters is not None and not self._restart_attempted:
            self._restart_attempted = True
            self._clear_loaded_page()
            self.query_one("#evidence-search-message", Static).update(
                _FAILURE_TEXT[FailureKind.EVIDENCE_CHANGED]
            )
            self._begin_page_load(
                None,
                "restart",
                self._submitted_filters,
                expected_revision=None,
            )
            return
        self._clear_loaded_page()
        self.query_one("#evidence-search-message", Static).update(
            "Evidence changed again. No stale rows remain. Press r to retry."
        )

    def _accept_page_response(self, request_id: int) -> bool:
        return (
            request_id == self._page_request_id
            and self.is_mounted
            and cast("WorkTraceApp", self.app).current_app_id == self.application.app_id
        )

    def _clear_loaded_page(self) -> None:
        self._page = None
        self._items = ()
        self._selected_links = ()
        self._committed_history = []
        self.query_one("#evidence-search-results", DataTable).clear()
        self.query_one("#evidence-search-links", DataTable).clear()
        self.query_one("#evidence-search-link-detail", Static).update(
            "No selected canonical contribution link."
        )
        self._render_status()

    def _render_page(self) -> None:
        results = self.query_one("#evidence-search-results", DataTable)
        results.clear()
        for index, item in enumerate(self._items):
            period = item.period_from or "Undated"
            if item.period_to and item.period_to != item.period_from:
                period = f"{period} → {item.period_to}"
            coverage = (
                "complete" if item.link_completeness else item.link_limit_reason or "incomplete"
            )
            results.add_row(
                literal_dynamic_text(item.source),
                literal_dynamic_text(item.kind),
                literal_dynamic_text(item.title or item.evidence_id),
                literal_dynamic_text(period),
                literal_dynamic_text(f"{len(item.links)} {coverage}"),
                key=f"evidence-search-row-{index}",
            )
        if not self._items:
            if self._page is not None and self._page.next_cursor is not None:
                state = "No eligible evidence on this bounded scan. Press n to continue."
            else:
                state = "No current evidence matched the submitted filters."
            self.query_one("#evidence-search-message", Static).update(state)
        self._select_result(0)

    def _select_result(self, row_index: int) -> None:
        if not 0 <= row_index < len(self._items):
            self._selected_links = ()
            self.query_one("#evidence-search-links", DataTable).clear()
            self.query_one("#evidence-search-result-detail", Static).update(
                "No selected evidence result."
            )
            self.query_one("#evidence-search-link-detail", Static).update(
                "No selected canonical contribution link."
            )
            return
        item = self._items[row_index]
        self._selected_links = item.links
        result_detail = Text("Observation ID: ")
        _append_dynamic(result_detail, item.evidence_id)
        result_detail.append("\nSource object ID: ")
        _append_dynamic(result_detail, item.object_id)
        self.query_one("#evidence-search-result-detail", Static).update(result_detail)
        links = self.query_one("#evidence-search-links", DataTable)
        links.clear()
        for index, link in enumerate(item.links):
            links.add_row(
                literal_dynamic_text(link.identifier),
                literal_dynamic_text(link.status),
                literal_dynamic_text(link.role),
                literal_dynamic_text(link.confirmation_basis),
                literal_dynamic_text(link.evidence_state),
                key=f"evidence-search-link-{index}",
            )
        if item.link_completeness:
            detail = "Canonical link evaluation complete."
        else:
            reason = (
                "projection budget"
                if item.link_limit_reason == "projection_budget"
                else "display cap"
            )
            detail = f"Canonical links are incomplete: {reason} reached; exact total is unknown."
        self.query_one("#evidence-search-link-detail", Static).update(detail)
        self._render_link_detail(0)

    def _render_link_detail(self, row_index: int) -> None:
        if not 0 <= row_index < len(self._selected_links):
            return
        link = self._selected_links[row_index]
        output = Text()
        results = self.query_one("#evidence-search-results", DataTable)
        item = (
            self._items[results.cursor_row] if 0 <= results.cursor_row < len(self._items) else None
        )
        if item is not None:
            output.append("Link evaluation: ")
            if item.link_completeness:
                output.append("complete")
            else:
                _append_dynamic(
                    output,
                    (
                        f"incomplete ({item.link_limit_reason or 'unknown reason'}; "
                        "exact total unknown)"
                    ),
                )
            output.append("\n")
        output.append("Canonical contribution: ")
        _append_dynamic(output, link.identifier)
        output.append("  |  limitations: ")
        _append_dynamic(output, "; ".join(link.limitations) or "none recorded")
        self.query_one("#evidence-search-link-detail", Static).update(output)

    def _open_evidence_row(self, row_index: int) -> None:
        if not 0 <= row_index < len(self._items):
            return
        self._excerpt_request_id += 1
        request_id = self._excerpt_request_id
        self.query_one("#evidence-search-message", Static).update(
            "Loading bounded evidence excerpt…"
        )
        self.load_excerpt(request_id, self._items[row_index].evidence_id)

    def _open_link_row(self, row_index: int) -> None:
        if not 0 <= row_index < len(self._selected_links):
            return
        cast("WorkTraceApp", self.app).open_contribution(
            self.application.app_id,
            self._selected_links[row_index].identifier,
            return_to_existing=True,
        )

    def _save_current_selection(self) -> None:
        if not self._committed_history:
            return
        results = self.query_one("#evidence-search-results", DataTable)
        links = self.query_one("#evidence-search-links", DataTable)
        self._committed_history[-1].result_row = max(0, results.cursor_row)
        self._committed_history[-1].link_row = max(0, links.cursor_row)

    def _restore_current_selection(self) -> None:
        if not self._committed_history or not self._items:
            return
        entry = self._committed_history[-1]
        result_row = min(entry.result_row, len(self._items) - 1)
        results = self.query_one("#evidence-search-results", DataTable)
        results.move_cursor(row=result_row)
        self._select_result(result_row)
        if self._selected_links:
            links = self.query_one("#evidence-search-links", DataTable)
            links.move_cursor(row=min(entry.link_row, len(self._selected_links) - 1))
            self._render_link_detail(links.cursor_row)

    def _render_status(self) -> None:
        output = Text()
        filters = self._submitted_filters
        if filters is None:
            output.append("Submitted filters: none")
        else:
            output.append("Submitted filters: ")
            _append_filter(output, "query", filters.query)
            _append_filter(output, "source", filters.source)
            _append_filter(output, "module", filters.module_text)
            _append_filter(output, "from", filters.date_from)
            _append_filter(output, "to", filters.date_to)
        page = self._page
        if page is not None:
            if filters is not None and page.filters != filters:
                output.append("\nDisplayed page filters: ")
                _append_filter(output, "query", page.filters.query)
                _append_filter(output, "source", page.filters.source)
                _append_filter(output, "module", page.filters.module_text)
                _append_filter(output, "from", page.filters.date_from)
                _append_filter(output, "to", page.filters.date_to)
            output.append("\nPage: ")
            _append_dynamic(output, len(self._committed_history))
            output.append("  |  readiness: ")
            _append_dynamic(output, _readiness_summary(page.readiness))
            output.append("  |  scanned rows: ")
            _append_dynamic(output, page.diagnostics.scanned_rows)
            output.append("  |  projection attempts: ")
            _append_dynamic(output, page.diagnostics.projection_attempts)
            output.append("/")
            _append_dynamic(output, page.diagnostics.projection_budget)
            if page.limitations:
                output.append("\nLimitations: ")
                _append_dynamic(output, "; ".join(page.limitations))
        self.query_one("#evidence-search-status", Static).update(output)
