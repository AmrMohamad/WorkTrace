from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Static

from worktrace.mcp_server.schemas import stable_id
from worktrace.read_workspace import ApplicationSummary, ReadOnlyWorkspace
from worktrace.tui.messages import ApplicationsLoaded, FailureKind, ReadFailed, failure_kind
from worktrace.tui.screens.applications import ApplicationScreen
from worktrace.tui.screens.base import WorkTraceModal, WorkTraceScreen
from worktrace.tui.screens.candidates import CandidateScreen

_HELP_TEXT = """WorkTrace read-only review

?       Help
Ctrl+P  Safe command palette
Tab     Move focus between panels
a       Switch application
r       Refresh current data
q/Esc   Back, or quit from candidates
Ctrl+Q  Quit
y       Copy the selected validated WorkTrace ID
j/k     Move selection
Arrows  Scroll focused details
Enter   Open
n/p     Next/previous candidate page
1-5     Contribution tabs

The UI cannot import, modify, rebuild, export, or contact providers.
"""

_INITIAL_FAILURE_TEXT = {
    FailureKind.BUSY: (
        "WorkTrace data is busy. Another process may be importing or rebuilding.\n\n"
        "Next: wait for it to finish, then press r to retry."
    ),
    FailureKind.UPGRADE_REQUIRED: (
        "The database is older than this WorkTrace version.\n\n"
        "Next: exit and run `worktrace init`, then retry."
    ),
    FailureKind.UNSUPPORTED_NEWER: (
        "The database is newer than this WorkTrace version.\n\nNext: upgrade WorkTrace, then retry."
    ),
    FailureKind.DATABASE: (
        "WorkTrace could not read the configured database.\n\n"
        "Next: exit and run `worktrace doctor`."
    ),
    FailureKind.NOT_FOUND: "The requested review record is unavailable. Press r to restart.",
    FailureKind.OUT_OF_SCOPE: (
        "The requested review record is outside the configured application.\n\n"
        "Next: press a to select a configured application or q to quit."
    ),
    FailureKind.GENERATION_CHANGED: "Candidate suggestions changed. Press r to restart.",
    FailureKind.UNEXPECTED: (
        "WorkTrace could not complete the read. No data was changed.\n\n"
        "Next: press r to retry or q to quit."
    ),
}


class LoadingScreen(WorkTraceScreen):
    ALLOW_SELECT = False
    BINDINGS: ClassVar[list[BindingType]] = [Binding("q,escape", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Static("Loading WorkTrace read-only data…", classes="loading", markup=False)
        yield Footer()


class InitialErrorScreen(WorkTraceScreen):
    ALLOW_SELECT = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "app.restart", "Retry"),
        Binding("a", "app.switch_application", "Switch app"),
        Binding("q,escape", "app.quit", "Quit"),
    ]

    def __init__(self, kind: FailureKind) -> None:
        super().__init__()
        self._kind = kind

    def compose(self) -> ComposeResult:
        yield Static("WorkTrace", classes="page-title", markup=False)
        yield Static(_INITIAL_FAILURE_TEXT[self._kind], classes="error", markup=False)
        yield Footer()


class HelpModal(WorkTraceModal):
    ALLOW_SELECT = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q,escape", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog", can_focus=True):
            yield Static(_HELP_TEXT, id="help-content", markup=False)

    def on_mount(self) -> None:
        self.query_one("#help-dialog", VerticalScroll).focus()


class SmallTerminalScreen(ModalScreen[bool], inherit_bindings=False):
    ALLOW_SELECT = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("c", "continue_compact", "Continue compact"),
        Binding("r", "recheck", "Recheck"),
        Binding("q,escape", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(
            "Terminal size is limited\n\n"
            "Recommended minimum: 80 columns x 24 rows\n\n"
            "c  Continue in compact mode\n"
            "r  Recheck size\n"
            "q  Quit",
            id="terminal-dialog",
            markup=False,
        )

    def action_copy_text(self) -> None:
        """Disable Textual's inherited selected-text clipboard path."""

    def action_continue_compact(self) -> None:
        self.dismiss(True)

    def action_recheck(self) -> None:
        if self.app.size.width >= 80 and self.app.size.height >= 24:
            self.dismiss(True)

    def action_quit(self) -> None:
        self.app.exit()


class WorkTraceApp(App[None], inherit_bindings=False):
    CSS_PATH = Path(__file__).with_name("worktrace.tcss")
    ALLOW_SELECT = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+p", "command_palette", "Commands"),
        Binding("ctrl+q", "quit", "Quit"),
        Binding("y", "copy_selected_id", "Copy ID", show=False),
    ]

    def __init__(
        self,
        workspace: ReadOnlyWorkspace,
        *,
        initial_app_id: str | None = None,
        initial_candidate_id: str | None = None,
    ) -> None:
        super().__init__()
        self.workspace = workspace
        self.initial_app_id = initial_app_id
        self.initial_candidate_id = initial_candidate_id
        self._applications: tuple[ApplicationSummary, ...] = ()
        self._applications_request_id = 0
        self.current_app_id: str | None = None
        self.compact_mode = False

    def on_mount(self) -> None:
        if self.size.width < 80 or self.size.height < 24:
            self.push_screen(SmallTerminalScreen(), self._after_terminal_check)
        else:
            self.action_restart()

    def _after_terminal_check(self, proceed: bool | None) -> None:
        if proceed:
            self.compact_mode = self.size.width < 80 or self.size.height < 24
            self.action_restart()

    def get_system_commands(self, screen: Screen[object]) -> Iterable[SystemCommand]:
        yield SystemCommand("Quit", "Quit WorkTrace", self.action_quit)
        yield SystemCommand("Keyboard help", "Show safe keyboard commands", self.action_show_help)
        yield SystemCommand(
            "Switch application",
            "Select another configured application",
            self.action_switch_application,
        )
        yield SystemCommand("Refresh", "Refresh the current read-only view", self.action_refresh)
        if isinstance(screen, (CandidateScreen,)) is False and self.current_app_id is not None:
            yield SystemCommand(
                "Return to candidates",
                "Return to the selected application's candidate page",
                self.action_return_to_candidates,
            )

    def action_copy_text(self) -> None:
        """No selected-text clipboard path exists in WorkTrace."""

    def action_restart(self) -> None:
        self._applications_request_id += 1
        request_id = self._applications_request_id
        self._show_screen(LoadingScreen())
        self.load_applications(request_id)

    def _show_screen(self, screen: Screen[None]) -> None:
        if self.screen.id == "_default":
            self.push_screen(screen)
        else:
            self.switch_screen(screen)

    def action_show_help(self) -> None:
        self.push_screen(HelpModal())

    def action_switch_application(self) -> None:
        self.show_applications()

    def action_refresh(self) -> None:
        refresh = getattr(self.screen, "refresh_data", None)
        if callable(refresh):
            refresh()
        else:
            self.action_restart()

    def action_return_to_candidates(self) -> None:
        if self.current_app_id is not None:
            self.open_application(self.current_app_id)

    def action_copy_selected_id(self) -> None:
        selected = getattr(self.screen, "selected_stable_id", lambda: None)()
        if selected is None:
            self.notify("No stable WorkTrace ID is selected.", markup=False)
            return
        value, allowed_prefixes = selected
        prefix = value.partition(":")[0]
        if prefix not in allowed_prefixes:
            self.notify("The selected value is not an allowed WorkTrace ID.", markup=False)
            return
        try:
            stable_id(value, "selected_id")
        except Exception:
            self.notify("The selected value is not a valid WorkTrace ID.", markup=False)
            return
        self.copy_to_clipboard(value)
        self.notify("Stable WorkTrace ID copied.", markup=False)

    @work(thread=True, exclusive=True, group="applications", exit_on_error=False)
    def load_applications(self, request_id: int) -> None:
        try:
            applications = self.workspace.applications()
        except Exception as error:
            self.post_message(ReadFailed("applications", request_id, failure_kind(error)))
        else:
            self.post_message(ApplicationsLoaded(request_id, applications))

    def on_applications_loaded(self, message: ApplicationsLoaded) -> None:
        if message.request_id != self._applications_request_id:
            return
        self._applications = message.applications
        if not self._applications:
            self._show_screen(InitialErrorScreen(FailureKind.NOT_FOUND))
            return
        available = {application.app_id for application in self._applications}
        if self.initial_app_id is not None:
            if self.initial_app_id not in available:
                self._show_screen(InitialErrorScreen(FailureKind.OUT_OF_SCOPE))
                return
            initial_app_id = self.initial_app_id
            initial_candidate_id = self.initial_candidate_id
            self.initial_app_id = None
            self.initial_candidate_id = None
            if initial_candidate_id is not None:
                self.current_app_id = initial_app_id
                self.open_contribution(
                    initial_app_id,
                    initial_candidate_id,
                    return_to_existing=False,
                )
            else:
                self.open_application(initial_app_id)
            return
        if len(self._applications) == 1:
            self.open_application(self._applications[0].app_id)
        else:
            self.show_applications()

    def on_read_failed(self, message: ReadFailed) -> None:
        if (
            message.operation != "applications"
            or message.request_id != self._applications_request_id
        ):
            return
        self._show_screen(InitialErrorScreen(message.kind))

    def show_applications(self) -> None:
        if not self._applications:
            self.action_restart()
            return
        self._show_screen(ApplicationScreen(self._applications))

    def _application(self, app_id: str) -> ApplicationSummary | None:
        return next(
            (application for application in self._applications if application.app_id == app_id),
            None,
        )

    def open_application(self, app_id: str) -> None:
        application = self._application(app_id)
        if application is None:
            self._show_screen(InitialErrorScreen(FailureKind.OUT_OF_SCOPE))
            return
        self.current_app_id = app_id
        self._show_screen(CandidateScreen(application))

    def open_contribution(
        self,
        app_id: str,
        candidate_id: str,
        *,
        return_to_existing: bool,
    ) -> None:
        from worktrace.tui.screens.contribution import ContributionScreen

        self.current_app_id = app_id
        screen = ContributionScreen(
            app_id,
            candidate_id,
            return_to_existing=return_to_existing,
        )
        if return_to_existing:
            self.push_screen(screen)
        else:
            self._show_screen(screen)


def run_worktrace_ui(
    workspace: ReadOnlyWorkspace,
    *,
    initial_app_id: str | None = None,
    initial_candidate_id: str | None = None,
) -> None:
    WorkTraceApp(
        workspace,
        initial_app_id=initial_app_id,
        initial_candidate_id=initial_candidate_id,
    ).run()
