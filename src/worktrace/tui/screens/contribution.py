from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, ClassVar, cast

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Footer, Static, TabbedContent, TabPane

from worktrace.read_workspace import ContributionReview
from worktrace.tui.messages import (
    ContributionReviewLoaded,
    EvidenceExcerptLoaded,
    FailureKind,
    ReadFailed,
    failure_kind,
)
from worktrace.tui.modals.evidence import EvidenceModal
from worktrace.tui.screens.base import WorkTraceScreen
from worktrace.tui.terminal_text import literal_dynamic_text

if TYPE_CHECKING:
    from worktrace.tui.app import WorkTraceApp

_EVIDENCE_PREFIXES = frozenset(
    {"obs", "participation", "part", "availability", "decision", "ref", "reference"}
)
_OBJECT_PREFIXES = frozenset({"obj"})
_CONTRIBUTION_PREFIXES = frozenset({"candidate", "contribution"})
_STATUS_SYMBOLS = {
    "supported": "✓ Supported",
    "partially_supported": "≈ Partially supported",
    "human_attested": "H Human-attested",
    "contradicted": "! Contradicted",
    "unknown": "? Unknown",
    "unresolved": "? Unresolved",
    "requires_human_confirmation": "? Requires human confirmation",
}
_FAILURE_TEXT = {
    FailureKind.BUSY: (
        "WorkTrace data is busy. Another process may be importing or rebuilding. Press r to retry."
    ),
    FailureKind.NOT_FOUND: (
        "This contribution or evidence is no longer available. Press r to retry."
    ),
    FailureKind.OUT_OF_SCOPE: (
        "The requested record is outside the selected application. Press q to return to candidates."
    ),
    FailureKind.UPGRADE_REQUIRED: "Exit and run `worktrace init`, then retry.",
    FailureKind.UNSUPPORTED_NEWER: "Upgrade WorkTrace before opening this database.",
    FailureKind.GENERATION_CHANGED: "Candidate suggestions changed. Return and refresh candidates.",
    FailureKind.DATABASE: "WorkTrace could not read the database. Run `worktrace doctor`.",
    FailureKind.UNEXPECTED: "WorkTrace could not complete this read. Press r to retry.",
}


def _append_dynamic(output: Text, value: object) -> None:
    output.append_text(literal_dynamic_text(value))


def _append_field(output: Text, label: str, value: object) -> None:
    output.append(f"{label}: ")
    _append_dynamic(output, "Unknown" if value is None or value == "" else value)
    output.append("\n")


def _append_values(output: Text, label: str, value: object) -> None:
    output.append(f"{label}: ")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = list(value)
        if not values:
            output.append("None\n")
            return
        output.append("\n")
        for item in values:
            output.append("  • ")
            _append_dynamic(output, item)
            output.append("\n")
        return
    _append_dynamic(output, "None" if value is None else value)
    output.append("\n")


def _append_structure(output: Text, value: object, *, indent: int = 0) -> None:
    prefix = " " * indent
    if isinstance(value, Mapping):
        for key, child in value.items():
            output.append(prefix)
            _append_dynamic(output, str(key).replace("_", " ").title())
            if isinstance(child, (Mapping, list, tuple)) and child:
                output.append(":\n")
                _append_structure(output, child, indent=indent + 2)
            else:
                output.append(": ")
                _append_dynamic(output, "None" if child in (None, "", [], {}) else child)
                output.append("\n")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value:
            output.append(prefix + "None\n")
            return
        for child in value:
            output.append(prefix + "• ")
            if isinstance(child, (Mapping, list, tuple)):
                output.append("\n")
                _append_structure(output, child, indent=indent + 2)
            else:
                _append_dynamic(output, child)
                output.append("\n")
        return
    output.append(prefix)
    _append_dynamic(output, value)
    output.append("\n")


def _status_label(status: object) -> str:
    value = str(status or "unknown")
    return _STATUS_SYMBOLS.get(value, f"? {value.replace('_', ' ').title()}")


class ContributionScreen(WorkTraceScreen):
    ALLOW_SELECT = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "show_tab('summary')", "Summary"),
        Binding("2", "show_tab('evidence')", "Evidence"),
        Binding("3", "show_tab('participation')", "Participation"),
        Binding("4", "show_tab('delivery')", "Delivery"),
        Binding("5", "show_tab('questions')", "Questions"),
        Binding("j", "cursor_down", "Next"),
        Binding("k", "cursor_up", "Previous"),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "app.switch_application", "Switch app"),
        Binding("y", "app.copy_selected_id", "Copy ID"),
        Binding("q,escape", "back", "Back"),
    ]

    def __init__(
        self,
        app_id: str,
        candidate_id: str,
        *,
        return_to_existing: bool,
    ) -> None:
        super().__init__()
        self.app_id = app_id
        self.candidate_id = candidate_id
        self.return_to_existing = return_to_existing
        self._review_request_id = 0
        self._excerpt_request_id = 0
        self._review: ContributionReview | None = None
        self._evidence_rows: list[dict[str, object]] = []
        self._questions: list[tuple[str, dict[str, object]]] = []
        self._gaps_by_question: dict[str, dict[str, object]] = {}

    def compose(self) -> ComposeResult:
        yield Static("Loading contribution review…", id="contribution-title", classes="page-title")
        yield Static("Loading read-only packet…", id="contribution-message", markup=False)
        with TabbedContent(initial="summary", id="review-tabs"):
            with (
                TabPane("Summary", id="summary"),
                VerticalScroll(id="summary-scroll", classes="review-scroll"),
            ):
                yield Static(id="summary-content", markup=False)
            with TabPane("Evidence", id="evidence"):
                yield DataTable(id="evidence-table", cursor_type="cell", zebra_stripes=True)
                yield Static(id="evidence-detail", classes="notice", markup=False)
            with (
                TabPane("Participation", id="participation"),
                VerticalScroll(id="participation-scroll", classes="review-scroll"),
            ):
                yield Static(id="participation-content", markup=False)
            with (
                TabPane("Delivery", id="delivery"),
                VerticalScroll(id="delivery-scroll", classes="review-scroll"),
            ):
                yield Static(id="delivery-content", markup=False)
            with TabPane("Questions", id="questions"):
                yield DataTable(id="questions-table", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="question-scroll", classes="review-scroll"):
                    yield Static(id="question-detail", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        evidence = self.query_one("#evidence-table", DataTable)
        evidence.add_columns("State", "Source", "Kind", "Title", "Evidence ID", "Object ID")
        questions = self.query_one("#questions-table", DataTable)
        questions.add_columns("State", "Section", "Question")
        self._load_review()

    def refresh_data(self) -> None:
        self.action_refresh()

    def action_refresh(self) -> None:
        self._excerpt_request_id += 1
        self.query_one("#contribution-message", Static).update("Refreshing read-only packet…")
        self._load_review()

    def action_back(self) -> None:
        self._review_request_id += 1
        self._excerpt_request_id += 1
        if self.return_to_existing:
            self.app.pop_screen()
        else:
            cast("WorkTraceApp", self.app).open_application(self.app_id)

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one("#review-tabs", TabbedContent).active = tab_id
        if tab_id == "evidence":
            self.query_one("#evidence-table", DataTable).focus()
        elif tab_id == "questions":
            self.query_one("#questions-table", DataTable).focus()
        elif tab_id in {"summary", "participation", "delivery"}:
            self.query_one(f"#{tab_id}-scroll", VerticalScroll).focus()

    def action_cursor_down(self) -> None:
        focused = self.focused
        if isinstance(focused, DataTable):
            focused.action_cursor_down()
        elif isinstance(focused, VerticalScroll):
            focused.action_scroll_down()

    def action_cursor_up(self) -> None:
        focused = self.focused
        if isinstance(focused, DataTable):
            focused.action_cursor_up()
        elif isinstance(focused, VerticalScroll):
            focused.action_scroll_up()

    def selected_stable_id(self) -> tuple[str, frozenset[str]] | None:
        review = self._review
        if review is None:
            return None
        active = self.query_one("#review-tabs", TabbedContent).active
        if active == "evidence":
            table = self.query_one("#evidence-table", DataTable)
            if not 0 <= table.cursor_row < len(self._evidence_rows):
                return None
            row = self._evidence_rows[table.cursor_row]
            if table.cursor_column == 5:
                object_id = row.get("object_id")
                return (
                    (object_id, _OBJECT_PREFIXES)
                    if isinstance(object_id, str) and object_id
                    else None
                )
            evidence_id = row.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id:
                return evidence_id, _EVIDENCE_PREFIXES
            object_id = row.get("object_id")
            return (
                (object_id, _OBJECT_PREFIXES) if isinstance(object_id, str) and object_id else None
            )
        if active == "questions":
            table = self.query_one("#questions-table", DataTable)
            if 0 <= table.cursor_row < len(self._questions):
                question = self._questions[table.cursor_row][1]
                evidence_ids = question.get("supporting_evidence_ids", [])
                if isinstance(evidence_ids, list) and evidence_ids:
                    first = evidence_ids[0]
                    if isinstance(first, str):
                        return first, _EVIDENCE_PREFIXES
        return review.resolved_contribution_id, _CONTRIBUTION_PREFIXES

    @work(thread=True, exclusive=True, group="contribution-review", exit_on_error=False)
    def load_review(self, request_id: int) -> None:
        try:
            review = cast("WorkTraceApp", self.app).workspace.contribution_review(
                self.app_id,
                self.candidate_id,
            )
        except Exception as error:
            self.post_message(ReadFailed("contribution_review", request_id, failure_kind(error)))
        else:
            self.post_message(ContributionReviewLoaded(request_id, review))

    @work(thread=True, exclusive=True, group="evidence-excerpt", exit_on_error=False)
    def load_excerpt(self, request_id: int, evidence_id: str) -> None:
        try:
            excerpt = cast("WorkTraceApp", self.app).workspace.evidence_excerpt(
                self.app_id,
                evidence_id,
                max_chars=4_000,
            )
        except Exception as error:
            self.post_message(ReadFailed("evidence_excerpt", request_id, failure_kind(error)))
        else:
            self.post_message(EvidenceExcerptLoaded(request_id, excerpt))

    def _load_review(self) -> None:
        self._review_request_id += 1
        self.load_review(self._review_request_id)

    def _load_excerpt(self, evidence_id: str) -> None:
        self._excerpt_request_id += 1
        self.query_one("#contribution-message", Static).update("Loading bounded evidence excerpt…")
        self.load_excerpt(self._excerpt_request_id, evidence_id)

    def on_contribution_review_loaded(self, message: ContributionReviewLoaded) -> None:
        if message.request_id != self._review_request_id:
            return
        self._review = message.review
        self._render_review(message.review)

    def on_evidence_excerpt_loaded(self, message: EvidenceExcerptLoaded) -> None:
        if message.request_id != self._excerpt_request_id:
            return
        self.query_one("#contribution-message", Static).update("Read-only review loaded.")
        self.app.push_screen(EvidenceModal(message.excerpt))

    def on_read_failed(self, message: ReadFailed) -> None:
        if message.operation == "contribution_review":
            if message.request_id != self._review_request_id:
                return
        elif message.operation == "evidence_excerpt":
            if message.request_id != self._excerpt_request_id:
                return
        else:
            return
        self.query_one("#contribution-message", Static).update(_FAILURE_TEXT[message.kind])

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "evidence-table":
            self._open_evidence_row(event.cursor_row)
        elif event.data_table.id == "questions-table":
            self._render_question_detail(event.cursor_row)
            self.query_one("#question-scroll", VerticalScroll).focus()

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        if event.data_table.id == "evidence-table":
            self._open_evidence_row(event.coordinate.row)

    def _open_evidence_row(self, row_index: int) -> None:
        if 0 <= row_index < len(self._evidence_rows):
            evidence_id = self._evidence_rows[row_index].get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id:
                self._load_excerpt(evidence_id)
            else:
                self.app.notify(
                    "No current evidence excerpt is available for this member.", markup=False
                )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "evidence-table":
            self._render_evidence_detail(event.cursor_row)
        elif event.data_table.id == "questions-table":
            self._render_question_detail(event.cursor_row)

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        if event.data_table.id == "evidence-table":
            self._render_evidence_detail(event.coordinate.row)

    def _render_review(self, review: ContributionReview) -> None:
        contribution = review.packet.get("contribution")
        contribution_data = contribution if isinstance(contribution, dict) else {}
        title = contribution_data.get("title") or "Untitled contribution"
        title_text = Text()
        _append_dynamic(title_text, title)
        self.query_one("#contribution-title", Static).update(title_text)
        self.query_one("#contribution-message", Static).update("Read-only review loaded.")
        self._render_summary(review, contribution_data)
        self._render_evidence(review)
        self._render_participation(review)
        self._render_delivery(review)
        self._render_questions(review)

    def _render_summary(
        self,
        review: ContributionReview,
        contribution: dict[str, object],
    ) -> None:
        output = Text()
        _append_field(output, "Candidate ID", review.candidate_id)
        _append_field(output, "Contribution ID", review.resolved_contribution_id)
        _append_field(output, "Application", review.app_id)
        _append_field(output, "Status", review.status)
        _append_field(output, "Type", contribution.get("type"))
        _append_field(output, "Title authority", contribution.get("title_authority"))
        _append_field(output, "Title status", contribution.get("title_status"))
        _append_field(output, "Period from", contribution.get("date_from"))
        _append_field(output, "Period to", contribution.get("date_to"))
        _append_field(output, "As of", review.packet.get("as_of"))
        evidence_summary = review.packet.get("evidence_summary")
        summary = evidence_summary if isinstance(evidence_summary, dict) else {}
        _append_values(output, "Sources", sorted(self._member_sources(summary)))
        _append_values(output, "Modules", summary.get("modules", []))
        members = summary.get("members", [])
        _append_field(output, "Current members", len(members) if isinstance(members, list) else 0)
        unsupported = summary.get("unsupported_member_ids", [])
        _append_field(
            output,
            "Current evidence",
            "Incomplete" if isinstance(unsupported, list) and unsupported else "Complete",
        )
        _append_values(output, "Unsupported member IDs", unsupported)
        output.append("\nContradictions\n")
        _append_structure(output, review.packet.get("contradictions", []), indent=2)
        output.append("\nLimitations\n")
        _append_structure(output, review.packet.get("limitations", []), indent=2)
        self.query_one("#summary-content", Static).update(output)

    @staticmethod
    def _member_sources(summary: dict[str, object]) -> set[str]:
        members = summary.get("members", [])
        if not isinstance(members, list):
            return set()
        return {
            str(member["source"])
            for member in members
            if isinstance(member, dict) and member.get("source")
        }

    def _render_evidence(self, review: ContributionReview) -> None:
        summary = review.packet.get("evidence_summary")
        summary_data = summary if isinstance(summary, dict) else {}
        raw_members = summary_data.get("members", [])
        members = (
            [item for item in raw_members if isinstance(item, dict)]
            if isinstance(raw_members, list)
            else []
        )
        self._evidence_rows = [dict(item) for item in members]
        self._evidence_rows.sort(
            key=lambda item: (str(item.get("source", "")), str(item.get("evidence_id", "")))
        )
        for unsupported in review.unsupported_members:
            self._evidence_rows.append(
                {
                    "state": "unsupported current",
                    "source": unsupported.source,
                    "kind": unsupported.kind,
                    "title": "No authoritative current observation",
                    "evidence_id": None,
                    "object_id": unsupported.object_id,
                    "external_id": unsupported.external_id,
                }
            )
        table = self.query_one("#evidence-table", DataTable)
        table.clear()
        for index, row in enumerate(self._evidence_rows):
            state = row.get("state") or (
                "context only" if row.get("context_only") is True else "material current"
            )
            table.add_row(
                literal_dynamic_text(state),
                literal_dynamic_text(row.get("source", "unknown")),
                literal_dynamic_text(row.get("kind", "unknown")),
                literal_dynamic_text(row.get("title") or "No title"),
                literal_dynamic_text(row.get("evidence_id") or "None"),
                literal_dynamic_text(row.get("object_id") or "None"),
                key=f"evidence-row-{index}",
            )
        if self._evidence_rows:
            self._render_evidence_detail(0)
        else:
            self.query_one("#evidence-detail", Static).update(
                "No current or unsupported evidence members are recorded."
            )

    def _render_evidence_detail(self, index: int) -> None:
        if not 0 <= index < len(self._evidence_rows):
            return
        output = Text()
        _append_structure(output, self._evidence_rows[index])
        self.query_one("#evidence-detail", Static).update(output)

    def _render_participation(self, review: ContributionReview) -> None:
        output = Text("Observed participation facts\n\n")
        _append_structure(output, review.packet.get("participation", {}))
        output.append("\nOwnership remains unresolved unless explicitly human-attested.\n")
        self.query_one("#participation-content", Static).update(output)

    def _render_delivery(self, review: ContributionReview) -> None:
        output = Text("Seven independent delivery states\n\n")
        raw_ladder = review.packet.get("release_ladder")
        ladder = raw_ladder if isinstance(raw_ladder, dict) else {}
        for rung, raw_detail in ladder.items():
            detail = raw_detail if isinstance(raw_detail, dict) else {}
            _append_dynamic(output, str(rung).replace("_", " ").title())
            output.append(" — ")
            output.append(_status_label(detail.get("status")))
            output.append("\n")
            _append_field(output, "  Statement", detail.get("statement"))
            _append_values(
                output, "  Supporting evidence", detail.get("supporting_evidence_ids", [])
            )
            _append_values(output, "  Limitations", detail.get("limitations", []))
            output.append("\n")
        self.query_one("#delivery-content", Static).update(output)

    def _render_questions(self, review: ContributionReview) -> None:
        self._questions = []
        sections = review.packet.get("sections")
        if isinstance(sections, dict):
            for section, raw_questions in sections.items():
                if not isinstance(raw_questions, list):
                    continue
                for raw_question in raw_questions:
                    if isinstance(raw_question, dict):
                        self._questions.append((str(section), raw_question))
        self._gaps_by_question = {}
        raw_gaps = review.gaps.get("unknown_questions", [])
        if isinstance(raw_gaps, list):
            for raw_gap in raw_gaps:
                if isinstance(raw_gap, dict) and isinstance(raw_gap.get("question_id"), str):
                    self._gaps_by_question[str(raw_gap["question_id"])] = raw_gap
        table = self.query_one("#questions-table", DataTable)
        table.clear()
        for index, (section, question) in enumerate(self._questions):
            table.add_row(
                literal_dynamic_text(_status_label(question.get("status"))),
                literal_dynamic_text(section.replace("_", " ").title()),
                literal_dynamic_text(question.get("question", "Unknown question")),
                key=f"question-row-{index}",
            )
        if self._questions:
            self._render_question_detail(0)
        else:
            self.query_one("#question-detail", Static).update("No Phase 4 questions were returned.")

    def _render_question_detail(self, index: int) -> None:
        if not 0 <= index < len(self._questions):
            return
        section, question = self._questions[index]
        output = Text()
        _append_field(output, "Section", section)
        _append_field(output, "Question ID", question.get("question_id"))
        _append_field(output, "Question", question.get("question"))
        _append_field(output, "Status", _status_label(question.get("status")))
        _append_field(output, "Answer", question.get("answer_draft"))
        _append_values(output, "Supporting evidence", question.get("supporting_evidence_ids", []))
        _append_values(
            output, "Contradicting evidence", question.get("contradicting_evidence_ids", [])
        )
        _append_values(output, "Limitations", question.get("limitations", []))
        _append_values(output, "Missing information", question.get("missing_information", []))
        question_id = question.get("question_id")
        if isinstance(question_id, str) and question_id in self._gaps_by_question:
            output.append("\nPacket-derived gap\n")
            _append_structure(output, self._gaps_by_question[question_id], indent=2)
        self.query_one("#question-detail", Static).update(output)
