from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.public_workflow_support import PublicWorkflowFixture
from tests.stdio_protocol_support import checkout_server, import_fixture, run_protocol


@pytest.mark.asyncio
async def test_stdio_read_protocol_uses_imported_git_jira_evidence_and_real_cli_mutation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    fixture = PublicWorkflowFixture.create(tmp_path, monkeypatch, dense_context=True)
    import_fixture(fixture)

    await run_protocol(checkout_server(fixture.config_path), fixture, assert_cli_mutation=True)
