from __future__ import annotations

import json
from pathlib import Path

import respx

from tests.test_jira_history import JiraFixture, invoke, issue, search
from tests.test_jira_history import workspace as workspace
from worktrace.config import load_config
from worktrace.db.connection import connect
from worktrace.packets.builder import PacketBuilder


def test_selector_replacement_requires_exact_reviewed_impact_and_preserves_decisions(
    workspace: Path,
) -> None:
    fixture = JiraFixture([issue(created="2024-01-02T00:00:00Z")])
    config = load_config(workspace)
    with respx.mock() as mock:
        mock.route().mock(side_effect=fixture)
        invoke(workspace, "import", "all", "sample")
        with connect(config.database_path) as connection:
            candidate = str(connection.execute("SELECT id FROM candidate_groups").fetchone()[0])
            object_id = str(connection.execute("SELECT id FROM source_objects").fetchone()[0])
        invoke(workspace, "confirm", candidate)
        with connect(config.database_path) as connection:
            decisions = list(connection.execute("SELECT id,payload_json FROM human_decisions"))
        fixture.issues = [issue(2)]
        preview = invoke(workspace, "import", "all", "sample", success=False)
        impact = preview["sources"][0]["selector_replacement"]
        assert impact["removed_object_ids"] == [object_id]
        assert impact["affected_confirmed_contributions"] == [candidate]
        with connect(config.database_path) as connection:
            assert {
                row["external_id"] for row in search(PacketBuilder(connection, config))["results"]
            } == {"1"}
        wrong = invoke(
            workspace,
            "import",
            "all",
            "sample",
            "--approve-selector-replacement",
            "wrong",
            success=False,
        )
        assert (
            wrong["sources"][0]["selector_replacement"]["proposal_token"]
            == impact["proposal_token"]
        )
        accepted = invoke(
            workspace,
            "import",
            "all",
            "sample",
            "--approve-selector-replacement",
            impact["proposal_token"],
        )
        assert accepted["sources"][0]["selector_replacement"]["approved"] is True
    with connect(config.database_path) as connection:
        assert {
            row["external_id"] for row in search(PacketBuilder(connection, config))["results"]
        } == {"2"}
        assert [
            tuple(row) for row in connection.execute("SELECT id,payload_json FROM human_decisions")
        ] == [tuple(row) for row in decisions]
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM observations WHERE source_object_id=?", (object_id,)
            ).fetchone()[0]
            == 1
        )


def test_changed_replacement_invalidates_old_approval(workspace: Path) -> None:
    fixture = JiraFixture([issue(1), issue(2)])
    with respx.mock() as mock:
        mock.route().mock(side_effect=fixture)
        invoke(workspace, "import", "all", "sample")
        fixture.issues = [issue(2)]
        preview = invoke(workspace, "import", "all", "sample", success=False)
        token = preview["sources"][0]["selector_replacement"]["proposal_token"]
        fixture.issues = [issue(3)]
        changed = invoke(
            workspace,
            "import",
            "all",
            "sample",
            "--approve-selector-replacement",
            token,
            success=False,
        )
        assert changed["sources"][0]["selector_replacement"]["proposal_token"] != token


def test_replacement_preview_audit_does_not_backfill_old_policy(workspace: Path) -> None:
    fixture = JiraFixture()
    with respx.mock() as mock:
        mock.route().mock(side_effect=fixture)
        invoke(workspace, "import", "all", "sample")
    with connect(load_config(workspace).database_path) as connection:
        scope = json.loads(connection.execute("SELECT scope_json FROM sync_runs").fetchone()[0])
        assert scope["selection_policy_version"] == 3
        assert scope["jira_seed_selection"]["policy_version"] == 3


def test_real_cli_chunks_502_explicit_keys_and_discloses_unreturned_keys(workspace: Path) -> None:
    fixture = JiraFixture()
    options = tuple(value for number in range(1, 503) for value in ("--jira-key", f"DEMO-{number}"))
    with respx.mock() as mock:
        mock.route().mock(side_effect=fixture)
        result = invoke(workspace, "import", "all", "sample", *options)
    exact_queries = [query for query in fixture.queries if "key in (" in query]
    assert len(exact_queries) == 11
    assert any('"DEMO-502"' in query for query in exact_queries)
    source = result["sources"][0]
    assert source["jira_seed_selection"]["selected_count"] == 502
    assert source["jira_key_retrieval"]["requested_count"] == 502
    assert source["jira_key_retrieval"]["unreturned_count"] == 501
    assert source["coverage"] == "limited"
    assert any(
        "deletion or absence of work is not established" in value for value in source["limitations"]
    )
