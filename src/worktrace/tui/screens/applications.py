from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import DataTable, Footer, Static

from worktrace.read_workspace import ApplicationSummary
from worktrace.tui.screens.base import WorkTraceScreen
from worktrace.tui.terminal_text import literal_dynamic_text

if TYPE_CHECKING:
    from worktrace.tui.app import WorkTraceApp


class ApplicationScreen(WorkTraceScreen):
    ALLOW_SELECT = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", "Next"),
        Binding("k", "cursor_up", "Previous"),
        Binding("q,escape", "app.quit", "Quit"),
    ]

    def __init__(self, applications: tuple[ApplicationSummary, ...]) -> None:
        super().__init__()
        self._applications = applications

    def compose(self) -> ComposeResult:
        yield Static("Select an application", classes="page-title", markup=False)
        yield DataTable(id="applications-table", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#applications-table", DataTable)
        table.add_columns("Application", "Market", "Type", "Sources")
        for index, application in enumerate(self._applications):
            table.add_row(
                literal_dynamic_text(application.name),
                literal_dynamic_text(application.market or "Unknown"),
                literal_dynamic_text(application.business_type or "Unknown"),
                literal_dynamic_text(", ".join(application.sources) or "None"),
                key=f"application-row-{index}",
            )
        if self._applications:
            table.focus()

    def action_cursor_down(self) -> None:
        self.query_one("#applications-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#applications-table", DataTable).action_cursor_up()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        index = event.cursor_row
        if 0 <= index < len(self._applications):
            cast("WorkTraceApp", self.app).open_application(self._applications[index].app_id)
