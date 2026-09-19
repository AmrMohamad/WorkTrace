from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from worktrace.archive.jira.orchestrator import CollectorLimits, JiraCollector
from worktrace.archive.jira.provider import MetadataPage, ProviderIdentity
from worktrace.archive.jira.selector import JiraSelector, token_hash
from worktrace.config import load_config
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.errors import PermissionDenied


class FakeJira:
    origin = "https://jira.example.test"

    def __init__(self) -> None:
        self.content_requests = 0
        self.metadata_requests: list[str] = []
        self.issue = {
            "id": "10001",
            "key": "DEMO-1",
            "fields": {
                "project": {"id": "10", "key": "DEMO", "name": "Forbidden project name"},
                "summary": "FORBIDDEN-SUMMARY",
                "parent": {"id": "10002", "key": "OTHER-2", "summary": "FORBIDDEN"},
                "subtasks": [],
                "issuelinks": [
                    {
                        "id": "l1",
                        "type": {"id": "100", "name": "blocks"},
                        "outwardIssue": {"id": "10003", "key": "OTHER-3", "summary": "FORBIDDEN"},
                    }
                ],
                "attachment": [
                    {"id": "1", "size": 12, "mimeType": "text/plain", "filename": "secret"}
                ],
                "updated": "2026-09-01T00:00:00Z",
            },
            "secret_field": "DO-NOT-PERSIST",
        }

    def verify_identity(self, expected_account_id: str | None = None) -> ProviderIdentity:
        assert expected_account_id in (None, "self")
        return ProviderIdentity("self", "UTC")

    def search_metadata(self, jql: str, *, next_token: str | None = None) -> MetadataPage:
        assert "assignee WAS" in jql
        return MetadataPage((self.issue,), None, True)

    def assignment_changelog(self, issue_id: str, *, start_at: int = 0) -> dict[str, object]:
        assert issue_id == "10001"
        return {
            "startAt": 0,
            "maxResults": 100,
            "total": 1,
            "values": [
                {
                    "created": "2025-01-01T00:00:00Z",
                    "items": [{"field": "assignee", "from": "other", "to": "self"}],
                }
            ],
        }

    def issue_metadata(self, issue_id: str) -> dict[str, object]:
        self.metadata_requests.append(issue_id)
        if issue_id == "10002":
            return {
                "id": "10002",
                "key": "OTHER-2",
                "fields": {"project": {"id": "20", "key": "OTHER"}, "parent": {"id": "10001"}},
            }
        if issue_id == "10003":
            raise PermissionDenied("context unavailable")
        raise AssertionError(issue_id)

    def issue_full(self, issue_id: str) -> dict[str, object]:
        assert issue_id in {"10001", "10002"}
        return self.issue | {"fields": dict(self.issue["fields"]), "id": issue_id}

    def resource_page(self, issue_id: str, kind: str, *, start_at: int = 0) -> dict[str, object]:
        return {"startAt": start_at, "maxResults": 100, "total": 0, "values": []}

    class _Attachment:
        def __enter__(self):
            raise PermissionDenied("fixture attachment unavailable")

        def __exit__(self, *args: object) -> None:
            return None

    def attachment_content(self, attachment_id: str):
        self.content_requests += 1
        return self._Attachment()


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""schema_version = 1
[data]
directory = {str(tmp_path / "data")!r}
[employment]
from = "2024-01-01"
to = "2026-09-06"
timezone = "UTC"
[identity]
display_name = "Fixture"
jira_account_id = "self"
[[apps]]
id = "sample"
name = "Sample"
jira_project_keys = ["DEMO"]
""",
        encoding="utf-8",
    )
    return path


def test_preview_is_metadata_only_bound_and_single_use(tmp_path: Path) -> None:
    configuration = load_config(_config(tmp_path))
    database = configuration.database_path
    database.parent.mkdir()
    connection = connect(database)
    migrate(connection, database)
    provider = FakeJira()
    collector = JiraCollector(
        connection,
        configuration,
        provider,
        vault_key=b"k" * 32,
        limits=CollectorLimits(free_space_floor_bytes=0),
    )
    preview = collector.preview()
    assert len(str(preview["approval_token"])) == 43
    assert "FORBIDDEN" not in json.dumps(preview)
    assert provider.content_requests == 0
    row = connection.execute("SELECT token_hash FROM jira_scope_previews").fetchone()
    assert row["token_hash"] == token_hash(str(preview["approval_token"]))
    assert str(preview["approval_token"]) not in row["token_hash"]

    result = collector.collect(str(preview["approval_token"]))
    assert result["status"] == "complete_with_unavailable_resources"
    assert provider.content_requests > 0
    with pytest.raises(PermissionDenied):
        collector.collect(str(preview["approval_token"]))
    connection.close()


def test_preview_rejects_forged_token_before_collection(tmp_path: Path) -> None:
    configuration = load_config(_config(tmp_path))
    database = configuration.database_path
    database.parent.mkdir()
    connection = connect(database)
    migrate(connection, database)
    collector = JiraCollector(connection, configuration, FakeJira(), vault_key=b"k" * 32)
    collector.preview()
    with pytest.raises(PermissionDenied):
        collector.collect("A" * 43)
    assert connection.execute("SELECT COUNT(*) FROM jira_collections").fetchone()[0] == 0
    connection.close()


def test_one_hop_context_deduplicates_cycles_and_keeps_inaccessible_refs() -> None:
    provider = FakeJira()
    selector = JiraSelector(
        provider,
        origin=provider.origin,
        account_id="self",
        date_from=date(2024, 1, 1),
        date_to=date(2026, 9, 6),
        timezone="UTC",
        config_fingerprint="sha256:test",
    )
    result = selector.preview()
    assert {item["issue_id"] for item in result.context} == {"10002"}
    assert result.unresolved_refs[0]["issue_id"] == "10003"
    assert provider.metadata_requests == ["10002", "10003"]
