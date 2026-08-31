from __future__ import annotations

from importlib.resources import files


def test_tui_stylesheet_is_packaged_as_a_resource() -> None:
    stylesheet = files("worktrace.tui").joinpath("worktrace.tcss")

    assert stylesheet.is_file()
