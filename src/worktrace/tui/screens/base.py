from __future__ import annotations

from typing import ClassVar

from textual.binding import Binding, BindingType
from textual.screen import ModalScreen, Screen


class WorkTraceScreen(Screen[None], inherit_bindings=False):
    ALLOW_SELECT = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("tab", "app.focus_next", "Next focus", show=False),
        Binding("shift+tab", "app.focus_previous", "Previous focus", show=False),
        Binding("?", "app.show_help", "Help"),
    ]

    def action_copy_text(self) -> None:
        """Disable Textual's inherited selected-text clipboard path."""

    def selected_stable_id(self) -> tuple[str, frozenset[str]] | None:
        return None


class WorkTraceModal(ModalScreen[None], inherit_bindings=False):
    ALLOW_SELECT = False
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss", "Close"),
        Binding("tab", "app.focus_next", "Next focus", show=False),
        Binding("shift+tab", "app.focus_previous", "Previous focus", show=False),
    ]

    def action_copy_text(self) -> None:
        """Disable Textual's inherited selected-text clipboard path."""

    def selected_stable_id(self) -> tuple[str, frozenset[str]] | None:
        return None
