from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import httpx
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
        return {
            "startAt": start_at,
            "maxResults": 100,
            "total": 0,
            "comments": [] if kind == "comments" else None,
            "keys": [] if kind == "issue_properties" else None,
            "values": [] if kind != "issue_properties" else None,
        }

    class _Attachment:
        def __enter__(self):
            raise PermissionDenied("fixture attachment unavailable")

        def __exit__(self, *args: object) -> None:
            return None

    def attachment_content(self, attachment_id: str):
        self.content_requests += 1
        return self._Attachment()


class TwoPassJira(FakeJira):
    def __init__(
        self, first: bytes, second: bytes | None = None, *, unknown_size: bool = False
    ) -> None:
        super().__init__()
        self.first = first
        self.second = second if second is not None else first
        self.pass_number = 0
        if unknown_size:
            self.issue["fields"]["attachment"][0]["size"] = None

    @contextmanager
    def attachment_content(self, attachment_id: str):
        self.content_requests += 1
        self.pass_number += 1
        content = self.first if self.pass_number % 2 else self.second
        yield httpx.Response(
            200,
            content=content,
            headers={"ETag": "fixture-v1", "Content-Length": str(len(content))},
        )


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


def _collector(tmp_path: Path, provider: FakeJira) -> tuple[JiraCollector, object]:
    configuration = load_config(_config(tmp_path))
    database = configuration.database_path
    database.parent.mkdir()
    connection = connect(database)
    migrate(connection, database)
    return (
        JiraCollector(
            connection,
            configuration,
            provider,
            vault_key=b"k" * 32,
            limits=CollectorLimits(free_space_floor_bytes=0),
        ),
        connection,
    )


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


def test_attachment_two_pass_preserves_known_and_unknown_sizes(tmp_path: Path) -> None:
    provider = TwoPassJira(b"attachment-data", unknown_size=True)
    collector, connection = _collector(tmp_path, provider)
    preview = collector.preview()
    result = collector.collect(str(preview["approval_token"]))
    assert result["status"] == "complete"
    assert provider.content_requests == 2  # shared attachment, two passes
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM jira_attachment_objects WHERE original_state='complete'"
        ).fetchone()[0]
        == 1
    )
    connection.close()


def test_attachment_validator_change_never_publishes_or_activates(tmp_path: Path) -> None:
    class ChangedValidator(TwoPassJira):
        @contextmanager
        def attachment_content(self, attachment_id: str):
            self.content_requests += 1
            self.pass_number += 1
            content = self.first
            yield httpx.Response(
                200,
                content=content,
                headers={
                    "ETag": f"fixture-v{self.pass_number}",
                    "Content-Length": str(len(content)),
                },
            )

    provider = ChangedValidator(b"attachment-data")
    collector, connection = _collector(tmp_path, provider)
    preview = collector.preview()
    result = collector.collect(str(preview["approval_token"]))
    assert result["status"] == "unstable_partial"
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM jira_attachment_objects WHERE original_state='complete'"
        ).fetchone()[0]
        == 0
    )
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM jira_archive_revisions WHERE status='active'"
        ).fetchone()[0]
        == 0
    )
    connection.close()


def test_resume_requires_fresh_unchanged_scope_before_new_revision(tmp_path: Path) -> None:
    provider = FakeJira()
    collector, connection = _collector(tmp_path, provider)
    preview = collector.preview()
    first = collector.collect(str(preview["approval_token"]))
    collection_id = str(first["collection_id"])
    connection.execute(
        "UPDATE jira_collection_runs SET status='paused' WHERE id=?",
        (first["run_id"],),
    )
    connection.commit()
    provider.issue["fields"]["parent"] = {"id": "10004", "key": "OTHER-4"}

    original_metadata = provider.issue_metadata

    def changed_metadata(issue_id: str) -> dict[str, object]:
        if issue_id == "10004":
            return {"id": issue_id, "key": "OTHER-4", "fields": {"project": {"key": "OTHER"}}}
        return original_metadata(issue_id)

    provider.issue_metadata = changed_metadata  # type: ignore[method-assign]
    resumed = collector.resume(collection_id)
    assert resumed["status"] == "paused"
    assert resumed["next_action"] == "new_preview_required"
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM jira_archive_revisions WHERE collection_id=?", (collection_id,)
        ).fetchone()[0]
        == 1
    )
    connection.close()


def test_malformed_resource_pagination_never_completes(tmp_path: Path) -> None:
    class MalformedPageJira(FakeJira):
        def resource_page(
            self, issue_id: str, kind: str, *, start_at: int = 0
        ) -> dict[str, object]:
            return {"startAt": start_at, "maxResults": 100, "values": []}

    provider = MalformedPageJira()
    collector, connection = _collector(tmp_path, provider)
    preview = collector.preview()
    result = collector.collect(str(preview["approval_token"]))
    assert result["status"] == "partial"
    assert result["next_action"] == "resume"
    connection.close()
