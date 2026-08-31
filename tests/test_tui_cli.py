from __future__ import annotations

import builtins
import io
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

import worktrace.cli as cli
from tests.test_cli import _write_config
from worktrace.cli import _TUI_ENVIRONMENT_VARIABLES, app, launch_ui
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def _initialized_config(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    config_path = _write_config(tmp_path, repository)
    database_path = tmp_path / "data" / "worktrace.sqlite3"
    database_path.parent.mkdir()
    connection = connect(database_path)
    try:
        migrate(connection, database_path)
    finally:
        connection.close()
    return config_path


def test_ui_rejects_non_tty_without_importing_textual() -> None:
    script = """
import sys
from typer.testing import CliRunner
from worktrace.cli import app
assert 'textual' not in sys.modules
result = CliRunner().invoke(app, ['ui'])
assert result.exit_code == 2, result.output
assert 'requires an interactive terminal' in result.output
assert 'textual' not in sys.modules
assert 'worktrace.tui.app' not in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_candidate_option_requires_app_before_launch() -> None:
    result = CliRunner().invoke(app, ["ui", "--candidate", "candidate:phase4"])

    assert result.exit_code == 2
    assert "--candidate requires --app" in result.output


def test_ui_scrubs_environment_before_importing_tui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _initialized_config(tmp_path)
    for name in _TUI_ENVIRONMENT_VARIABLES:
        monkeypatch.setenv(name, "must-not-survive")
    monkeypatch.setattr(cli.sys, "stdin", _TTY())
    monkeypatch.setattr(cli.sys, "stdout", _TTY())
    imported: list[str] = []
    fake_module = types.ModuleType("worktrace.tui.app")

    def fake_run(*args: object, **kwargs: object) -> None:
        imported.append("run")

    fake_module.run_worktrace_ui = fake_run  # type: ignore[attr-defined]
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "worktrace.tui.app":
            assert all(name not in os.environ for name in _TUI_ENVIRONMENT_VARIABLES)
            return fake_module
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    launch_ui(app_id="sample_store", candidate_id=None, config=config_path)

    assert imported == ["run"]
    assert all(name not in os.environ for name in _TUI_ENVIRONMENT_VARIABLES)
