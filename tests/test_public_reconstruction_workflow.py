from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.public_workflow_support import PublicWorkflowFixture, _issue
from worktrace.config import load_config  # type: ignore[import-untyped]
from worktrace.db.connection import connect, connect_read_only  # type: ignore[import-untyped]


def test_public_reconstruction_is_import_derived_and_correctable(
    tmp_path: Path, monkeypatch: Any
) -> None:
    fixture = PublicWorkflowFixture.create(tmp_path, monkeypatch)
    initialized = fixture.invoke("init")
    assert initialized.exit_code == 0, initialized.output
    imported = fixture.invoke("import", "all", "sample")
    assert imported.exit_code == 0, imported.output
    execution = json.loads(imported.output)
    assert {source["source"] for source in execution["sources"]} == {"git", "jira"}
    assert fixture.jira_requests and all(
        "jira.fixture" not in path for path in fixture.jira_requests
    )
    rebuilt = fixture.invoke("rebuild", "all", "sample")
    assert rebuilt.exit_code == 0, rebuilt.output

    found = fixture.tools.search_evidence(app_id="sample", query="DEMO-1 context", limit=10)
    assert found["results"]
    object_id = str(found["results"][0]["object_id"])
    context = fixture.tools.get_evidence_context(app_id="sample", object_id=object_id)
    assert any(
        item["relationship_type"] == "mentions_commit_sha" for item in context["relations"]["items"]
    )
    membership = next(
        item for item in context["memberships"]["items"] if item["basis"] == "suggestion"
    )
    candidate_id = str(membership["candidate_id"])
    summary = fixture.tools.get_contribution_summary(contribution_id=candidate_id)
    packet = fixture.tools.build_phase4_packet(contribution_id=candidate_id)
    assert summary["contribution"]["candidate_id"] == candidate_id
    assert packet["schema_version"] == 2

    confirmed = fixture.invoke("confirm", candidate_id)
    assert confirmed.exit_code == 0, confirmed.output
    removed = fixture.invoke("remove-member", candidate_id, object_id)
    assert removed.exit_code == 0, removed.output
    assert not any(
        item["basis"] == "confirmed" and item["candidate_id"] == candidate_id
        for item in fixture.tools.get_evidence_context(app_id="sample", object_id=object_id)[
            "memberships"
        ]["items"]
    )
    undone = fixture.invoke("undo", str(json.loads(removed.output)["decision_id"]))
    assert undone.exit_code == 0, undone.output
    restored = fixture.tools.get_evidence_context(app_id="sample", object_id=object_id)
    assert any(item["basis"] == "confirmed" for item in restored["memberships"]["items"])


def test_bulk_import_uses_exact_key_chunks_and_reaches_late_key(
    tmp_path: Path, monkeypatch: Any
) -> None:
    issues = {
        f"DEMO-{number}": _issue(str(10_000 + number), f"DEMO-{number}", "fixture")
        for number in range(1, 503)
    }
    fixture = PublicWorkflowFixture.create(tmp_path, monkeypatch, bulk_keys=502, jira_issues=issues)
    assert fixture.invoke("init").exit_code == 0
    imported = fixture.invoke("import", "all", "sample")
    assert imported.exit_code == 0, imported.output
    assert len([jql for jql in fixture.jira_jql if "key in" in jql]) >= 2
    late = fixture.tools.search_evidence(app_id="sample", query="DEMO-502", limit=10)
    assert any(item["source"] == "jira" for item in late["results"])


def test_dense_jira_context_has_independent_continuations_before_decisions(
    tmp_path: Path, monkeypatch: Any
) -> None:
    fixture = PublicWorkflowFixture.create(tmp_path, monkeypatch, dense_context=True)
    assert fixture.invoke("init").exit_code == 0
    assert fixture.invoke("import", "all", "sample").exit_code == 0
    assert fixture.invoke("rebuild", "all", "sample").exit_code == 0
    found = fixture.tools.search_evidence(
        app_id="sample", query=fixture.dense_object_query, limit=10
    )
    jira = next(item for item in found["results"] if item["source"] == "jira")
    context = fixture.tools.get_evidence_context(
        app_id="sample", object_id=str(jira["object_id"]), limit=1
    )
    assert context["relations"]["items"]
    assert context["relations"]["next_cursor"] is not None
    assert context["memberships"]["items"]
    assert context["memberships"]["next_cursor"] is not None


def test_actual_cli_approval_commands_reject_invalid_followups(
    tmp_path: Path, monkeypatch: Any
) -> None:
    fixture = PublicWorkflowFixture.create(tmp_path, monkeypatch, dense_context=True)
    assert fixture.invoke("init").exit_code == 0
    assert fixture.invoke("import", "all", "sample").exit_code == 0
    assert fixture.invoke("rebuild", "all", "sample").exit_code == 0
    jira = next(
        item
        for item in fixture.tools.search_evidence(
            app_id="sample", query=fixture.dense_object_query, limit=10
        )["results"]
        if item["source"] == "jira"
    )
    object_id = str(jira["object_id"])
    memberships = fixture.tools.get_evidence_context(app_id="sample", object_id=object_id)[
        "memberships"
    ]["items"]
    candidates = list(dict.fromkeys(str(item["candidate_id"]) for item in memberships))
    assert len(candidates) >= 2
    first, second = candidates[:2]
    assert fixture.invoke("confirm", first).exit_code == 0
    added = fixture.invoke("add-member", first, object_id)
    assert added.exit_code == 0
    removed = fixture.invoke("remove-member", first, object_id)
    assert removed.exit_code == 0
    assert fixture.invoke("undo", str(json.loads(removed.output)["decision_id"])).exit_code == 0
    merged = fixture.invoke("merge", first, second)
    assert merged.exit_code == 0, merged.output
    merged_id = str(json.loads(merged.output)["contribution_id"])
    assert fixture.invoke("split", merged_id, object_id).exit_code == 0
    configuration = load_config(fixture.config_path)
    connection = connect(configuration.database_path)
    try:
        before = int(connection.execute("SELECT COUNT(*) FROM human_decisions").fetchone()[0])
    finally:
        connection.close()
    assert fixture.invoke("ignore", first).exit_code == 0
    rejected = fixture.invoke("split", first, object_id)
    assert rejected.exit_code != 0
    connection = connect(configuration.database_path)
    try:
        after = int(connection.execute("SELECT COUNT(*) FROM human_decisions").fetchone()[0])
    finally:
        connection.close()
    assert after == before + 1


def test_imported_no_code_jira_history_stays_unknown(tmp_path: Path, monkeypatch: Any) -> None:
    no_code = _issue("10009", "DEMO-9", "No-code investigation")
    no_code["fields"]["updated"] = "2027-01-02T12:00:00Z"  # type: ignore[index]
    comment: dict[str, object] = {
        "id": "40001",
        "created": "2026-05-01T12:00:00Z",
        "updated": "2027-01-02T12:00:00Z",
        "author": {"accountId": "self-jira", "displayName": "Renamed Engineer"},
        "updateAuthor": {"accountId": "colleague", "displayName": "Fixture Engineer"},
        "body": {"type": "doc", "content": []},
    }
    fixture = PublicWorkflowFixture.create(
        tmp_path,
        monkeypatch,
        jira_issues={"DEMO-1": _issue("10001", "DEMO-1", "Later edited issue"), "DEMO-9": no_code},
        jira_comments=[comment],
        jira_changelog=[
            {
                "id": "50001",
                "created": "2026-06-01T12:00:00Z",
                "author": {"accountId": "colleague", "displayName": "Fixture Engineer"},
                "items": [
                    {
                        "field": "assignee",
                        "from": "former",
                        "fromString": "Former",
                        "to": "self-jira",
                        "toString": "Renamed Engineer",
                    }
                ],
            }
        ],
    )
    assert fixture.invoke("init").exit_code == 0
    assert fixture.invoke("import", "all", "sample").exit_code == 0
    result = next(
        item
        for item in fixture.tools.search_evidence(app_id="sample", query="No-code", limit=10)[
            "results"
        ]
        if item["source"] == "jira"
    )
    context = fixture.tools.get_evidence_context(
        app_id="sample", object_id=str(result["object_id"])
    )
    candidate = next(
        item for item in context["memberships"]["items"] if item["basis"] == "suggestion"
    )
    packet = fixture.tools.build_phase4_packet(contribution_id=str(candidate["candidate_id"]))
    implemented = next(
        question
        for section in packet["sections"].values()
        for question in section
        if question["question_id"] == "action.implemented"
    )
    assert implemented["status"] == "unknown"
    configuration = load_config(fixture.config_path)
    connection = connect_read_only(configuration.database_path)
    try:
        changelog_data = json.loads(
            str(
                connection.execute(
                    "SELECT data_json FROM observations observation JOIN source_objects object "
                    "ON object.id=observation.source_object_id "
                    "WHERE object.kind='jira_issue_changelog'"
                ).fetchone()[0]
            )
        )
        assert changelog_data["interval_observations"]
    finally:
        connection.close()


def test_imported_comment_history_and_git_identity_roles(tmp_path: Path, monkeypatch: Any) -> None:
    comment: dict[str, object] = {
        "id": "40001",
        "created": "2026-05-01T12:00:00Z",
        "updated": "2027-01-02T12:00:00Z",
        "author": {"accountId": "self-jira", "displayName": "Renamed Engineer"},
        "updateAuthor": {"accountId": "colleague", "displayName": "Fixture Engineer"},
        "body": {"type": "doc", "content": []},
    }
    fixture = PublicWorkflowFixture.create(tmp_path, monkeypatch, jira_comments=[comment])
    assert fixture.invoke("init").exit_code == 0
    assert fixture.invoke("import", "all", "sample").exit_code == 0
    configuration = load_config(fixture.config_path)
    connection = connect_read_only(configuration.database_path)
    try:
        comment_data = json.loads(
            str(
                connection.execute(
                    "SELECT data_json FROM observations observation JOIN source_objects object "
                    "ON object.id=observation.source_object_id "
                    "WHERE object.kind='jira_issue_comment'"
                ).fetchone()[0]
            )
        )
        assert comment_data["historical_wording_unknown"] is True
        actor_rows = list(
            connection.execute(
                "SELECT actor.email_hash, actor.is_self, participation.role "
                "FROM participations participation "
                "JOIN actors actor ON actor.id=participation.actor_id "
                "JOIN source_objects object ON object.id=participation.source_object_id "
                "WHERE object.source='git'"
            )
        )
        assert any(row["is_self"] and row["role"] == "git_author" for row in actor_rows)
        assert any(not row["is_self"] and row["role"] == "git_author" for row in actor_rows)
        assert any(row["is_self"] and row["role"] == "git_committer" for row in actor_rows)
    finally:
        connection.close()


def test_undated_historical_jira_is_visible_only_without_date_filter(
    tmp_path: Path, monkeypatch: Any
) -> None:
    undated = _issue("10010", "DEMO-10", "Undated historical investigation")
    fields = undated["fields"]
    assert isinstance(fields, dict)
    fields.update({"created": None, "updated": None, "resolutiondate": None})
    fixture = PublicWorkflowFixture.create(
        tmp_path,
        monkeypatch,
        jira_issues={"DEMO-1": _issue("10001", "DEMO-1", "Later edited issue"), "DEMO-10": undated},
    )
    assert fixture.invoke("init").exit_code == 0
    assert fixture.invoke("import", "all", "sample").exit_code == 0
    unfiltered = fixture.tools.search_evidence(
        app_id="sample", query="Undated historical", limit=10
    )
    assert any(item["period_status"] == "unknown" for item in unfiltered["results"])
    filtered = fixture.tools.search_evidence(
        app_id="sample",
        query="Undated historical",
        date_from="2026-01-01",
        date_to="2026-12-31",
        limit=10,
    )
    assert filtered["results"] == []
    assert filtered["date_filter_policy"] == "undated_excluded"
