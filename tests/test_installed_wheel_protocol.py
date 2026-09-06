from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.public_workflow_support import PublicWorkflowFixture
from tests.stdio_protocol_support import checkout_server, import_fixture, run_protocol

_INSTALLED_COMMAND_TIMEOUT_SECONDS = 10


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(args, check=True, cwd=cwd, capture_output=True, text=True)


def _wheel_environment(tmp_path: Path, checkout: Path) -> tuple[Path, Path, Path]:
    configured_python = os.environ.get("WORKTRACE_TEST_SERVER_PYTHON")
    configured_prefix = os.environ.get("WORKTRACE_TEST_SERVER_PREFIX")
    configured_cwd = os.environ.get("WORKTRACE_TEST_SERVER_CWD")
    if configured_python and configured_prefix and configured_cwd:
        return Path(configured_python), Path(configured_prefix), Path(configured_cwd)

    uv = shutil.which("uv")
    assert uv is not None, "uv is required to create the isolated installed-wheel environment"
    _run(uv, "build", cwd=checkout)
    requirements = tmp_path / "runtime-requirements.txt"
    prefix = tmp_path / "installed-wheel-environment"
    working_directory = tmp_path / "outside-checkout-cwd"
    working_directory.mkdir()
    _run(
        uv,
        "export",
        "--locked",
        "--no-dev",
        "--no-emit-project",
        "--output-file",
        str(requirements),
        cwd=checkout,
    )
    _run(uv, "venv", str(prefix), "--python", "3.12", cwd=checkout)
    python = prefix / "bin" / "python"
    wheels = sorted((checkout / "dist").glob("*.whl"))
    assert wheels, "uv build did not produce a wheel"
    _run(
        uv,
        "pip",
        "install",
        "--python",
        str(python),
        "--requirement",
        str(requirements),
        *[str(wheel) for wheel in wheels],
        cwd=checkout,
    )
    return python, prefix, working_directory


def _installed_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = ""
    return environment


def _installed_probe(python: Path, working_directory: Path) -> dict[str, str]:
    probe = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import json, os, sys; from pathlib import Path; import worktrace; "
                "print(json.dumps({'executable': sys.executable, "
                "'package': str(Path(worktrace.__file__).resolve()), "
                "'cwd': os.getcwd(), 'pythonpath': os.environ.get('PYTHONPATH')}))"
            ),
        ],
        check=True,
        cwd=working_directory,
        env=_installed_environment(),
        capture_output=True,
        text=True,
    )
    value = json.loads(probe.stdout)
    assert isinstance(value, dict)
    return {str(key): str(item) for key, item in value.items()}


def _assert_installed_path(path: Path, *, prefix: Path, checkout: Path) -> None:
    assert "site-packages" in path.parts
    assert path.is_relative_to(prefix.resolve())
    assert not path.is_relative_to(checkout)


def test_installed_wheel_tui_entrypoint_imports_and_stylesheet_are_isolated(tmp_path: Path) -> None:
    checkout = Path(__file__).resolve().parents[1]
    python, prefix, working_directory = _wheel_environment(tmp_path, checkout)
    assert not prefix.resolve().is_relative_to(checkout)
    assert not working_directory.resolve().is_relative_to(checkout)

    config_path = working_directory / "synthetic-config.toml"
    config_path.write_text(
        f"""schema_version = 1
[data]
directory = {str(working_directory / "data")!r}
[employment]
from = "2026-01-01"
to = "2026-12-31"
[identity]
display_name = "Installed wheel smoke"
git_author_emails = []
git_author_names = []
jira_account_id = ""
[[apps]]
id = "smoke"
name = "Installed wheel smoke"
repo_paths = []
jira_project_keys = []
gitlab_project_ids = []
""",
        encoding="utf-8",
    )
    environment = _installed_environment()

    help_result = subprocess.run(
        [str(prefix / "bin" / "worktrace"), "ui", "--help"],
        check=False,
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=_INSTALLED_COMMAND_TIMEOUT_SECONDS,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert (
        "Review contribution evidence in an interactive, read-only terminal UI."
        in help_result.stdout
    )

    non_tty_result = subprocess.run(
        [str(prefix / "bin" / "worktrace"), "ui", "--config", str(config_path)],
        check=False,
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=_INSTALLED_COMMAND_TIMEOUT_SECONDS,
    )
    assert non_tty_result.returncode == 2
    assert "worktrace ui requires an interactive terminal" in non_tty_result.stderr
    assert "worktrace --help" in non_tty_result.stderr

    imports = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import json; from importlib.resources import files; import worktrace.tui.app; "
                "import worktrace.read_workspace; "
                "from worktrace.read_workspace import ReadOnlyWorkspace; "
                "stylesheet = files('worktrace.tui').joinpath('worktrace.tcss'); "
                "assert ReadOnlyWorkspace.__module__ == 'worktrace.read_workspace'; "
                "print(json.dumps({'tui': worktrace.tui.app.__file__, "
                "'workspace': worktrace.read_workspace.__file__, "
                "'stylesheet': str(stylesheet), 'stylesheet_text': stylesheet.read_text()}))"
            ),
        ],
        check=True,
        cwd=working_directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=_INSTALLED_COMMAND_TIMEOUT_SECONDS,
    )
    observed = json.loads(imports.stdout)
    assert isinstance(observed, dict)
    _assert_installed_path(Path(str(observed["tui"])).resolve(), prefix=prefix, checkout=checkout)
    _assert_installed_path(
        Path(str(observed["workspace"])).resolve(), prefix=prefix, checkout=checkout
    )
    _assert_installed_path(
        Path(str(observed["stylesheet"])).resolve(), prefix=prefix, checkout=checkout
    )
    assert str(observed["stylesheet_text"])


@pytest.mark.asyncio
async def test_installed_wheel_stdio_protocol_uses_site_packages_outside_checkout(
    tmp_path: Path, monkeypatch: Any
) -> None:
    checkout = Path(__file__).resolve().parents[1]
    python, prefix, working_directory = _wheel_environment(tmp_path, checkout)
    assert not prefix.resolve().is_relative_to(checkout)
    assert not working_directory.resolve().is_relative_to(checkout)
    observed = _installed_probe(python, working_directory)
    assert Path(observed["executable"]).resolve() == python.resolve()
    package = Path(observed["package"])
    assert "site-packages" in package.parts
    assert package.is_relative_to(prefix.resolve())
    assert not package.is_relative_to(checkout)
    assert Path(observed["cwd"]).resolve() == working_directory.resolve()
    assert observed["pythonpath"] == ""

    fixture = PublicWorkflowFixture.create(tmp_path / "fixture", monkeypatch, dense_context=True)
    import_fixture(fixture)
    parameters = checkout_server(fixture.config_path)
    parameters.command = str(python)
    parameters.args = ["-m", "worktrace", "serve-mcp", "--config", str(fixture.config_path)]
    parameters.cwd = str(working_directory)
    parameters.env = {"PYTHONPATH": ""}
    await run_protocol(parameters, fixture, assert_cli_mutation=False)
