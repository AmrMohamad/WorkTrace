from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import DataTable, Input

from worktrace import cli as cli_module
from worktrace.archive.jira.repository import JiraArchiveRepository
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.errors import RecoveryError
from worktrace.normalize.redaction import Redactor
from worktrace.tui.app import WorkTraceApp
from worktrace.tui.screens.jira_collection import JiraCollectionScreen
from worktrace.vault import extract as extract_module
from worktrace.vault.export import export_attachment
from worktrace.vault.extract import ExtractionResult
from worktrace.vault.format import VaultDescriptor, write_vault_object
from worktrace.vault.search import index_collection_attachments, search_collection


class _FakeWorkerProcess:
    def __init__(self, *, timeout: bool = False) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(
            json.dumps(
                {
                    "status": "complete",
                    "text": "hello",
                    "pages": 0,
                    "chars": 5,
                    "parser_version": "1",
                    "exit_code": 0,
                    "reason": None,
                }
            ).encode()
        )
        self.stderr = io.BytesIO()
        self.returncode = 0
        self.timeout = timeout
        self.waits = 0
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.timeout and self.waits == 1:
            raise extract_module.subprocess.TimeoutExpired("sandbox-exec", timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def test_worker_command_is_sandboxed_and_protocol_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    process = _FakeWorkerProcess()

    def fake_popen(command: list[str], **kwargs: object) -> _FakeWorkerProcess:
        seen["command"] = command
        seen["kwargs"] = kwargs
        return process

    monkeypatch.setattr(extract_module, "sandbox_available", lambda: True)
    monkeypatch.setattr(extract_module.subprocess, "Popen", fake_popen)
    result = extract_module.run_worker(
        BytesIO(b"hello"), mime_type="text/plain", filename="provider-secret.txt"
    )
    command = seen["command"]
    kwargs = seen["kwargs"]
    assert isinstance(command, list)
    assert command[0:2] == ["/usr/bin/sandbox-exec", "-p"]
    assert "deny network*" in command[2]
    assert "deny process-fork" in command[2]
    assert kwargs["cwd"] != "/"
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert set(environment) == {"PYTHONPATH", "PYTHONUTF8", "WORKTRACE_CONTROL_FD"}
    assert int(environment["WORKTRACE_CONTROL_FD"]) in kwargs["pass_fds"]
    assert result.status == "complete"


def test_worker_timeout_terms_then_reaps_before_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeWorkerProcess(timeout=True)
    monkeypatch.setattr(extract_module, "sandbox_available", lambda: True)
    monkeypatch.setattr(extract_module.subprocess, "Popen", lambda *_args, **_kwargs: process)
    result = extract_module.run_worker(
        BytesIO(b"hello"), mime_type="text/plain", filename="note.txt"
    )
    assert result.status == "failed"
    assert result.exit_code == 14
    assert process.terminated is True
    assert process.killed is False


def _archive(tmp_path: Path) -> tuple[sqlite3.Connection, str, str, Path, bytes]:
    database = tmp_path / "ledger.sqlite3"
    connection = connect(database)
    migrate(connection, database)
    repository = JiraArchiveRepository(connection)
    site_id = repository.ensure_site("https://jira.example.test")
    collection_id = repository.create_collection(
        site_id=site_id,
        scope={"roots": [{"issue_id": "10001"}]},
        scope_hash="scope",
        approval_token_hash="token",
        policy_version=1,
        config_fingerprint="config",
        vault_id="vault:test",
    )
    run_id = repository.start_run(collection_id)
    revision_id = repository.create_revision(collection_id, run_id, "manifest")
    connection.execute(
        "UPDATE jira_archive_revisions SET status='active' WHERE id=?", (revision_id,)
    )
    connection.execute(
        "INSERT INTO jira_collection_issues "
        "(collection_id,revision_id,run_id,issue_id,object_id,issue_key,role,"
        "selection_reason_json,assignment_status,boundary_status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            collection_id,
            revision_id,
            run_id,
            "10001",
            "jira:issue:10001",
            "DEMO-1",
            "root",
            "{}",
            "assigned",
            "inside",
        ),
    )
    connection.commit()
    vault = tmp_path / "vault"
    vault.mkdir()
    data = b"redacted@example.test\nplain"
    key = b"k" * 32
    descriptor = VaultDescriptor(
        site_id=site_id,
        collection_id=collection_id,
        revision_id=revision_id,
        object_id="jatt:test",
        kind="attachment_original",
        content_length=len(data),
        content_sha256=hashlib.sha256(data).hexdigest(),
        key_version=1,
    )
    result = write_vault_object(
        BytesIO(data), vault / "object.wtva", descriptor, key, vault_root=vault
    )
    connection.execute(
        "INSERT INTO jira_attachment_objects "
        "(id,collection_id,revision_id,issue_id,attachment_id,archive_evidence_id,filename,"
        "mime_type,declared_size,manifest_sha256,original_state,vault_object_id,vault_object_path,"
        "vault_key_version,ciphertext_sha256,extracted_state,source_locator,logical_resource_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "jatt:test",
            collection_id,
            revision_id,
            "10001",
            "att-1",
            "jare:test",
            "note.txt",
            "text/plain",
            len(data),
            "manifest",
            "complete",
            "jatt:test",
            "object.wtva",
            1,
            result.ciphertext_sha256,
            "not_requested",
            "{}",
            "jatt-family:test",
        ),
    )
    connection.commit()
    return connection, collection_id, revision_id, vault, key


def test_post_collection_index_is_redacted_and_idempotent(tmp_path: Path) -> None:
    connection, collection_id, _revision_id, vault, key = _archive(tmp_path)
    try:
        calls = 0

        def worker(source: object, received_key: bytes, **kwargs: object) -> ExtractionResult:
            nonlocal calls
            calls += 1
            assert received_key == key
            assert kwargs["attachment_id"] == "att-1"
            return ExtractionResult("complete", "redacted@example.test\nplain", 0, 27)

        redactor = Redactor(b"hmac-key")
        assert (
            index_collection_attachments(
                connection,
                collection_id=collection_id,
                vault_root=str(vault),
                key_for_version=lambda _version: key,
                redactor=redactor,
                worker=worker,
            )["complete"]
            == 1
        )
        first = connection.execute(
            "SELECT COUNT(*), text_redacted FROM jira_search_chunks"
        ).fetchone()
        assert first[0] == 1
        assert "redacted@example.test" not in first[1]
        assert (
            index_collection_attachments(
                connection,
                collection_id=collection_id,
                vault_root=str(vault),
                key_for_version=lambda _version: key,
                redactor=redactor,
                worker=worker,
            )["complete"]
            == 1
        )
        assert calls == 2
        assert connection.execute("SELECT COUNT(*) FROM jira_search_chunks").fetchone()[0] == 1
    finally:
        connection.close()


def test_index_resolves_each_vault_key_version_without_fallback(tmp_path: Path) -> None:
    connection, collection_id, revision_id, vault, key1 = _archive(tmp_path)
    key2 = b"2" * 32
    try:
        site_id = str(
            connection.execute(
                "SELECT site_id FROM jira_collections WHERE id=?", (collection_id,)
            ).fetchone()[0]
        )
        data = b"second version"
        descriptor = VaultDescriptor(
            site_id=site_id,
            collection_id=collection_id,
            revision_id=revision_id,
            object_id="jatt:test2",
            kind="attachment_original",
            content_length=len(data),
            content_sha256=hashlib.sha256(data).hexdigest(),
            key_version=2,
        )
        result = write_vault_object(
            BytesIO(data), vault / "object2.wtva", descriptor, key2, vault_root=vault
        )
        connection.execute(
            "INSERT INTO jira_attachment_objects "
            "(id,collection_id,revision_id,issue_id,attachment_id,archive_evidence_id,filename,"
            "mime_type,declared_size,manifest_sha256,original_state,vault_object_id,vault_object_path,"
            "vault_key_version,ciphertext_sha256,extracted_state,source_locator,"
            "logical_resource_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "jatt:test2",
                collection_id,
                revision_id,
                "10001",
                "att-2",
                "jare:test2",
                "second.txt",
                "text/plain",
                len(data),
                "manifest2",
                "complete",
                "jatt:test2",
                "object2.wtva",
                2,
                result.ciphertext_sha256,
                "not_requested",
                "{}",
                "jatt-family:test2",
            ),
        )
        connection.commit()
        observed: dict[str, bytes] = {}

        def worker(source: object, received_key: bytes, **kwargs: object) -> ExtractionResult:
            observed[str(kwargs["attachment_id"])] = received_key
            return ExtractionResult("complete", "indexed", 0, 7)

        counts = index_collection_attachments(
            connection,
            collection_id=collection_id,
            vault_root=str(vault),
            key_for_version={1: key1, 2: key2}.get,
            redactor=Redactor(b"hmac-key"),
            worker=worker,
        )
        assert counts["complete"] == 2
        assert observed == {"att-1": key1, "att-2": key2}
    finally:
        connection.close()


def test_missing_vault_key_marks_attachment_unavailable_without_chunks(tmp_path: Path) -> None:
    connection, collection_id, _revision_id, vault, _key = _archive(tmp_path)
    try:
        called = False

        def worker(*_args: object, **_kwargs: object) -> ExtractionResult:
            nonlocal called
            called = True
            return ExtractionResult("complete", "must not run", 0, 12)

        counts = index_collection_attachments(
            connection,
            collection_id=collection_id,
            vault_root=str(vault),
            key_for_version=lambda _version: None,
            redactor=Redactor(b"hmac-key"),
            worker=worker,
        )
        assert counts["extraction_unavailable"] == 1
        assert called is False
        assert (
            connection.execute(
                "SELECT extracted_state FROM jira_attachment_objects WHERE attachment_id='att-1'"
            ).fetchone()[0]
            == "extraction_unavailable"
        )
        assert connection.execute("SELECT COUNT(*) FROM jira_search_chunks").fetchone()[0] == 0
    finally:
        connection.close()


def test_export_key_lookup_is_versioned_and_never_creates(monkeypatch: pytest.MonkeyPatch) -> None:
    keychain = type(
        "FakeKeychain",
        (),
        {
            "get": lambda self, version: {1: b"1" * 32, 2: b"2" * 32}.get(version),
            "ensure": lambda self, _version=1: pytest.fail("export must not ensure a key"),
        },
    )()
    monkeypatch.setattr(cli_module.MacOSKeychain, "open", lambda _installation: keychain)
    configuration = SimpleNamespace(config_path=Path("/private/config.toml"))
    assert cli_module._vault_key_for_config(configuration, 2) == b"2" * 32
    with pytest.raises(cli_module.WorkTraceError, match="version is missing"):
        cli_module._vault_key_for_config(configuration, 3)


def test_search_cursor_is_keyset_view_bound_and_tamper_evident(tmp_path: Path) -> None:
    connection, collection_id, revision_id, _vault, _key = _archive(tmp_path)
    try:
        for ordinal in range(2):
            connection.execute(
                "INSERT INTO jira_search_chunks "
                "(id,collection_id,revision_id,issue_id,attachment_id,ordinal,locator_json,"
                "text_redacted,chars,extraction_version) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"chunk:{ordinal}",
                    collection_id,
                    revision_id,
                    "10001",
                    "att-1",
                    ordinal,
                    "{}",
                    f"needle-{ordinal}",
                    8,
                    "1",
                ),
            )
        connection.commit()
        first = search_collection(connection, collection_id, "needle", limit=1)
        cursor = first["next_cursor"]
        assert isinstance(cursor, str)
        assert len(first["hits"]) == 1
        second = search_collection(connection, collection_id, "needle", limit=1, cursor=cursor)
        assert [hit["ordinal"] for hit in second["hits"]] == [1]
        with pytest.raises(ValueError):
            search_collection(connection, collection_id, "other", limit=1, cursor=cursor)
        with pytest.raises(ValueError):
            search_collection(
                connection,
                collection_id,
                "needle",
                limit=1,
                cursor=cursor + "tampered",
            )
    finally:
        connection.close()


def test_export_requires_trusted_vault_source_and_private_existing_parent(tmp_path: Path) -> None:
    _connection, collection_id, revision_id, vault, key = _archive(tmp_path)
    try:
        output_parent = tmp_path / "exports"
        output_parent.mkdir(mode=0o700)
        destination = output_parent / "copy.txt"
        exported = export_attachment(
            source=vault / "object.wtva",
            destination=destination,
            vault_root=vault,
            key=key,
            collection_id=collection_id,
            revision_id=revision_id,
            object_id="jatt:test",
        )
        assert exported.read_bytes() == b"redacted@example.test\nplain"
        assert oct(os.stat(exported).st_mode & 0o777) == "0o600"
        with pytest.raises(RecoveryError):
            export_attachment(
                source=tmp_path / "outside.wtva",
                destination=output_parent / "outside.txt",
                vault_root=vault,
                key=key,
            )
    finally:
        _connection.close()


class _TuiWorkspace:
    def jira_collection_summary(self, collection_id: str) -> dict[str, object]:
        return {"collection_id": collection_id, "revision_status": "active"}

    def jira_search(self, collection_id: str, query: str, **_: object) -> dict[str, object]:
        return {
            "view_token": "view:1",
            "hits": [
                {
                    "issue_id": "10001",
                    "attachment_id": "att-1",
                    "locator": {},
                    "text": "<hostile>[literal]",
                }
            ],
            "next_cursor": None,
        }

    def jira_issue(self, collection_id: str, issue_id: str, **_: object) -> dict[str, object]:
        return {
            "issue": {"issue_id": issue_id, "issue_key": "DEMO-1"},
            "attachments": [
                {
                    "attachment_id": "att-1",
                    "mime_type": "text/plain",
                    "original_state": "complete",
                    "extracted_state": "complete",
                }
            ],
        }

    def jira_attachment(
        self, collection_id: str, attachment_id: str, **_: object
    ) -> dict[str, object]:
        return {"attachment": {"attachment_id": attachment_id}, "chunks": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_tui_jira_search_issue_attachment_back_journey(size: tuple[int, int]) -> None:
    app = WorkTraceApp(_TuiWorkspace(), initial_jira_collection="jcol:test")  # type: ignore[arg-type]
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, JiraCollectionScreen)
        query = screen.query_one("#jira-search-query", Input)
        query.focus()
        await pilot.press(*list("needle"))
        await pilot.press("enter")
        await pilot.pause()
        results = screen.query_one("#jira-search-results", DataTable)
        assert results.row_count == 1
        results.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert screen._mode == "issue"
        attachments = screen.query_one("#jira-issue-attachments", DataTable)
        attachments.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert screen._mode == "attachment"
        await pilot.press("escape")
        assert screen._mode == "issue"
        await pilot.press("escape")
        assert screen._mode == "search"
        assert screen._query == "needle"
