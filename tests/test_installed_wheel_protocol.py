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


def _installed_probe(python: Path, working_directory: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = ""
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
        env=environment,
        capture_output=True,
        text=True,
    )
    value = json.loads(probe.stdout)
    assert isinstance(value, dict)
    return {str(key): str(item) for key, item in value.items()}


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
