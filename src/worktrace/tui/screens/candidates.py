from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Footer, Static

from worktrace.read_models.candidates import CandidateCursor, CandidateListItem, CandidatePage
from worktrace.read_workspace import ApplicationSummary
from worktrace.tui.messages import (
    CandidatePageLoaded,
    FailureKind,
    ReadFailed,
    SourceStatusLoaded,
    failure_kind,
)
from worktrace.tui.screens.base import WorkTraceScreen
from worktrace.tui.terminal_text import literal_dynamic_text

if TYPE_CHECKING:
    from worktrace.tui.app import WorkTraceApp


_FAILURE_TEXT = {
    FailureKind.BUSY: (
        "WorkTrace data is busy. Another process may be importing or rebuilding. Press r to retry."
    ),
    FailureKind.GENERATION_CHANGED: (
        "Candidate suggestions changed while this page was open. Restarting at page one."
    ),
    FailureKind.UPGRADE_REQUIRED: (
        "The database is older than this WorkTrace version. Exit and run `worktrace init`."
    ),
    FailureKind.UNSUPPORTED_NEWER: (
        "The database is newer than this WorkTrace version. Upgrade WorkTrace before retrying."
    ),
    FailureKind.NOT_FOUND: "The requested candidate is no longer available. Press r to refresh.",
    FailureKind.OUT_OF_SCOPE: (
        "The requested data is outside the selected application. Press a to switch application."
    ),
    FailureKind.DATABASE: "WorkTrace could not read the database. Run `worktrace doctor`.",
    FailureKind.UNEXPECTED: "WorkTrace could not complete this read. Press r to retry.",
}


def _append_dynamic(target: Text, value: object) -> None:
    target.append_text(literal_dynamic_text(value))


def _source_status_text(status: dict[str, object]) -> Text:
    output = Text("Latest source attempts\n")
    if not status:
        output.append("No source attempts are recorded.\n")
        output.append("Next: run an import from the WorkTrace CLI.")
        return output
    for source, raw_group in status.items():
        _append_dynamic(output, source)
        output.append("\n")
        group = raw_group if isinstance(raw_group, dict) else {}
        instances = group.get("instances", [])
        if not isinstance(instances, list) or not instances:
            output.append("  status: unavailable | authoritative current: no\n")
            output.append("  Next: inspect this source with `worktrace status`.\n")
            continue
        for raw_instance in instances:
            instance = raw_instance if isinstance(raw_instance, dict) else {}
            output.append("  ")
            _append_dynamic(output, instance.get("source_instance", "unknown"))
            output.append(" | status: ")
            _append_dynamic(output, instance.get("status", "unknown"))
            output.append(" | completeness: ")
            _append_dynamic(output, instance.get("completeness", "unknown"))
            output.append(" | completed: ")
            _append_dynamic(output, instance.get("completed_at") or "not recorded")
            output.append(" | stale: ")
            _append_dynamic(output, "yes" if instance.get("stale") is True else "no")
            output.append(" | authoritative current: ")
            _append_dynamic(
                output,
                "yes" if instance.get("authoritative_current") is True else "no",
            )
            output.append("\n")
            error = instance.get("error_summary")
            if error:
                output.append("    error: ")
                _append_dynamic(output, error)
                output.append("\n")
            limitations = instance.get("limitations", [])
            if isinstance(limitations, list):
                for limitation in limitations:
                    output.append("    limitation: ")
                    _append_dynamic(output, limitation)
                    output.append("\n")
    return output


class CandidateScreen(WorkTraceScreen):
    ALLOW_SELECT = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "Next"),
        Binding("k", "cursor_up", "Previous"),
        Binding("n", "next_page", "Next page"),
        Binding("p", "previous_page", "Previous page"),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "app.switch_application", "Switch app"),
        Binding("y", "app.copy_selected_id", "Copy ID"),
        Binding("q,escape", "app.quit", "Quit"),
    ]

    def __init__(self, application: ApplicationSummary) -> None:
        super().__init__()
        self.application = application
        self._source_request_id = 0
        self._page_request_id = 0
        self._cursor_stack: list[CandidateCursor | None] = [None]
        self._page: CandidatePage | None = None
        self._items: tuple[CandidateListItem, ...] = ()

    def compose(self) -> ComposeResult:
        yield Static(id="candidate-title", classes="page-title", markup=False)
        with VerticalScroll(id="source-status-scroll", can_focus=True):
            yield Static(id="source-status", markup=False)
        yield Static(
            "Candidate groups are deterministic review suggestions, not ownership claims.",
            classes="notice",
            markup=False,
        )
        yield Static("Loading candidate page…", id="candidate-message", markup=False)
        yield DataTable(id="candidate-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        title = Text("WorkTrace / ")
        _append_dynamic(title, self.application.name)
        self.query_one("#candidate-title", Static).update(title)
        table = self.query_one("#candidate-table", DataTable)
        if cast("WorkTraceApp", self.app).compact_mode:
            self.add_class("compact")
            table.add_columns("State", "Contribution", "Period", "Sources")
        else:
            table.add_columns("State", "Contribution", "Authority", "Period", "Sources", "Roles")
        table.focus()
        self._load_source_status()
        self._load_current_page()

    def refresh_data(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        self._cursor_stack = [None]
        self._page = None
        self.query_one("#candidate-message", Static).update("Refreshing read-only data…")
        self._load_source_status()
        self._load_current_page()

    def action_cursor_down(self) -> None:
        focused = self.focused
        if isinstance(focused, VerticalScroll):
            focused.action_scroll_down()
        else:
            self.query_one("#candidate-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        focused = self.focused
        if isinstance(focused, VerticalScroll):
            focused.action_scroll_up()
        else:
            self.query_one("#candidate-table", DataTable).action_cursor_up()

    def action_next_page(self) -> None:
        if self._page is None or self._page.next_cursor is None:
            self.app.notify("No later candidate page is available.", markup=False)
            return
        self._cursor_stack.append(self._page.next_cursor)
        self._load_current_page()

    def action_previous_page(self) -> None:
        if len(self._cursor_stack) == 1:
            self.app.notify("Already on the first candidate page.", markup=False)
            return
        self._cursor_stack.pop()
        self._load_current_page()

    def selected_stable_id(self) -> tuple[str, frozenset[str]] | None:
        table = self.query_one("#candidate-table", DataTable)
        if 0 <= table.cursor_row < len(self._items):
            return self._items[table.cursor_row].candidate_id, frozenset({"candidate"})
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "candidate-table":
            return
        if 0 <= event.cursor_row < len(self._items):
            item = self._items[event.cursor_row]
            cast("WorkTraceApp", self.app).open_contribution(
                self.application.app_id,
                item.candidate_id,
                return_to_existing=True,
            )

    def _load_current_page(self) -> None:
        self._page_request_id += 1
        request_id = self._page_request_id
        self.query_one("#candidate-message", Static).update("Loading candidate page…")
        self.load_candidate_page(request_id, self._cursor_stack[-1])

    def _load_source_status(self) -> None:
        self._source_request_id += 1
        self.load_source_status(self._source_request_id)

    @work(thread=True, exclusive=True, group="candidate-page", exit_on_error=False)
    def load_candidate_page(
        self,
        request_id: int,
        cursor: CandidateCursor | None,
    ) -> None:
        try:
            page = cast("WorkTraceApp", self.app).workspace.candidate_page(
                self.application.app_id,
                cursor=cursor,
            )
        except Exception as error:
            self.post_message(ReadFailed("candidate_page", request_id, failure_kind(error)))
        else:
            self.post_message(CandidatePageLoaded(request_id, page))

    @work(thread=True, exclusive=True, group="source-status", exit_on_error=False)
    def load_source_status(self, request_id: int) -> None:
        try:
            status = cast("WorkTraceApp", self.app).workspace.source_status(self.application.app_id)
        except Exception as error:
            self.post_message(ReadFailed("source_status", request_id, failure_kind(error)))
        else:
            self.post_message(SourceStatusLoaded(request_id, status))

    def on_source_status_loaded(self, message: SourceStatusLoaded) -> None:
        if message.request_id != self._source_request_id:
            return
        self.query_one("#source-status", Static).update(_source_status_text(message.status))

    def on_candidate_page_loaded(self, message: CandidatePageLoaded) -> None:
        if message.request_id != self._page_request_id:
            return
        self._page = message.page
        self._items = message.page.items
        table = self.query_one("#candidate-table", DataTable)
        table.clear()
        for index, item in enumerate(self._items):
            title = item.title or "Untitled candidate"
            period = item.period_from or "Unknown"
            if item.period_to and item.period_to != item.period_from:
                period = f"{period} → {item.period_to}"
            cells = [
                literal_dynamic_text(item.status),
                literal_dynamic_text(title),
            ]
            if not cast("WorkTraceApp", self.app).compact_mode:
                cells.append(literal_dynamic_text(item.title_authority))
            cells.extend(
                (
                    literal_dynamic_text(period),
                    literal_dynamic_text(" ".join(item.source_coverage) or "None"),
                )
            )
            if not cast("WorkTraceApp", self.app).compact_mode:
                cells.append(
                    literal_dynamic_text(" ".join(item.participation_indicators) or "None")
                )
            table.add_row(*cells, key=f"candidate-row-{index}")
        if not self._items:
            if message.page.next_cursor is not None:
                state = "No visible candidates in this bounded scan. Press n to continue."
            else:
                state = (
                    "No current contribution candidates. Use `worktrace status` and "
                    "`worktrace rebuild candidates APP_ID` from the CLI."
                )
        else:
            state = f"Page {len(self._cursor_stack)} · {len(self._items)} candidates"
            if message.page.next_cursor is not None and len(self._items) < 25:
                state += " · hidden or unavailable candidates were skipped; press n to continue"
        self.query_one("#candidate-message", Static).update(state)

    def on_read_failed(self, message: ReadFailed) -> None:
        if message.operation == "source_status":
            if message.request_id != self._source_request_id:
                return
            self.query_one("#source-status", Static).update(_FAILURE_TEXT[message.kind])
            return
        if message.operation != "candidate_page" or message.request_id != self._page_request_id:
            return
        if message.kind is FailureKind.GENERATION_CHANGED:
            self._cursor_stack = [None]
            self.app.notify(_FAILURE_TEXT[message.kind], markup=False)
            self._load_current_page()
            return
        self.query_one("#candidate-message", Static).update(_FAILURE_TEXT[message.kind])
