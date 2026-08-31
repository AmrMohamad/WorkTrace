from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.widgets import Static

from worktrace.tui.screens.base import WorkTraceModal
from worktrace.tui.terminal_text import literal_dynamic_text, terminal_safe_text

_EVIDENCE_PREFIXES = frozenset(
    {"obs", "participation", "part", "availability", "decision", "ref", "reference"}
)


def _append_field(output: Text, label: str, value: object) -> None:
    output.append(f"{label}: ")
    output.append_text(literal_dynamic_text(value))
    output.append("\n")


class EvidenceModal(WorkTraceModal):
    ALLOW_SELECT = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "app.copy_selected_id", "Copy evidence ID"),
        Binding("q,escape", "dismiss", "Close"),
    ]

    def __init__(self, excerpt: dict[str, object]) -> None:
        super().__init__()
        self._excerpt = excerpt
        evidence_id = excerpt.get("evidence_id")
        self._evidence_id = evidence_id if isinstance(evidence_id, str) else None

    def compose(self) -> ComposeResult:
        meta = Text("UNTRUSTED SOURCE TEXT\n\n")
        _append_field(meta, "Source", self._excerpt.get("source", "unknown"))
        _append_field(meta, "Kind", self._excerpt.get("kind", "unknown"))
        _append_field(meta, "Evidence ID", self._excerpt.get("evidence_id", "unknown"))
        _append_field(meta, "Observed", self._excerpt.get("as_of", "not recorded"))
        _append_field(meta, "Completeness", self._excerpt.get("completeness", "unknown"))
        _append_field(
            meta,
            "Source excerpt truncated",
            "yes" if self._excerpt.get("truncated") is True else "no",
        )

        raw_text = self._excerpt.get("text")
        if raw_text is None:
            raw_text = (
                self._excerpt.get("reason") or "No textual body is available for this evidence."
            )
        encoded = terminal_safe_text(str(raw_text), max_output_chars=20_000)
        _append_field(meta, "Terminal presentation truncated", "yes" if encoded.truncated else "no")
        _append_field(meta, "Encoded terminal controls", encoded.encoded_control_count)

        with VerticalScroll(id="evidence-dialog", classes="excerpt-scroll"):
            yield Static(meta, markup=False)
            yield Static(Text(encoded.text), id="evidence-body", markup=False)
            yield Static("\ny  Copy evidence ID    Esc  Close", markup=False)

    def selected_stable_id(self) -> tuple[str, frozenset[str]] | None:
        if self._evidence_id is None:
            return None
        return self._evidence_id, _EVIDENCE_PREFIXES
