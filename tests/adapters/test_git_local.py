from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from worktrace.adapters.base import ParticipationRole, ReferenceStrength
from worktrace.adapters.git_local import LocalGitAdapter, LocalGitConfig
from worktrace.errors import ScopeViolation


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        shell=False,
    )
    return result.stdout


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Committer Dev")
    _git(repo, "config", "user.email", "committer@example.com")
    (repo / "one.txt").write_text("one\n", encoding="utf-8")
    (repo / "binary.dat").write_bytes(b"\x00\xff\x00fixture")
    (repo / ".mailmap").write_text(
        "Canonical Author <canonical@example.test> Author Dev <author@example.com>\n",
        encoding="utf-8",
    )
    _git(repo, "add", "one.txt", "binary.dat", ".mailmap")
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Author Dev",
            "GIT_AUTHOR_EMAIL": "author@example.com",
            "GIT_AUTHOR_DATE": "2026-01-02T10:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-02T11:00:00+00:00",
        }
    )
    _git(
        repo,
        "commit",
        "-q",
        "-m",
        (
            "Implement MOB-42\n\n"
            "Co-authored-by: Pair Dev <pair@example.com>\n"
            "Reviewed-by: Review Dev <review@example.com>"
        ),
        env=environment,
    )
    _git(repo, "tag", "v0.1")
    _git(
        repo,
        "remote",
        "add",
        "origin",
        "https://user:secret@git.example/repo.git?token=fixture-secret",
    )
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def test_full_snapshot_preserves_roles_refs_and_redacts_emails(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    before_status = _git(repo, "status", "--porcelain=v1")
    adapter = LocalGitAdapter(
        LocalGitConfig(
            repository_path=repo,
            allowed_root=tmp_path,
            source_instance="sample-store-local",
            app_id="sample_store",
            email_key=b"test-key",
            jira_project_keys=("MOB",),
        )
    )

    pages = list(adapter.iter_pages())
    commit = next(page.records[0] for page in pages if page.resource_type == "commit")
    roles = {participation.role for participation in commit.participations}

    assert roles == {
        ParticipationRole.AUTHOR,
        ParticipationRole.COMMITTER,
        ParticipationRole.CO_AUTHOR,
        ParticipationRole.REVIEWER,
    }
    assert all("@example.com" not in repr(item) for item in commit.participations)
    assert commit.participations[0].actor.display_name == "Canonical Author"
    assert commit.participations[0].actor.email_hash is not None
    assert any(
        reference.target_external_id == "MOB-42"
        and reference.strength is ReferenceStrength.EXACT_TEXT
        for reference in commit.references
    )
    assert "diff" not in commit.payload
    changed_paths = commit.payload["changed_paths"]
    assert any(
        item["path"] == "binary.dat"
        and item["binary"] is True
        and item["status"] == "added"
        and item["new_file"] is True
        and item["additions"] is None
        and item["deletions"] is None
        for item in changed_paths
    )
    assert any(
        record.payload.get("ref_kind") == "tag"
        for page in pages
        if page.resource_type == "ref"
        for record in page.records
    )
    remote_ref = next(
        record
        for page in pages
        if page.resource_type == "ref"
        for record in page.records
        if record.payload.get("ref_kind") == "remote_tracking_branch"
    )
    assert remote_ref.payload["remote_identity"] == {
        "name": "origin",
        "locations": [{"kind": "https", "host": "git.example", "path": "/repo.git"}],
        "clone_local_observation": True,
    }
    assert "secret" not in repr(remote_ref)
    assert _git(repo, "status", "--porcelain=v1") == before_status


def test_repository_must_be_inside_allowed_root(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    outside = tmp_path / "other"
    outside.mkdir()

    with pytest.raises(ScopeViolation):
        LocalGitAdapter(
            LocalGitConfig(
                repository_path=repo,
                allowed_root=outside,
                source_instance="repo",
                app_id="app",
                email_key=b"test-key",
            )
        )


def test_numstat_preserves_raw_path_delimiters_without_patches(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    raw_name = b"odd-\tline\n.bin"
    raw_path = os.path.join(os.fsencode(repo), raw_name)
    descriptor = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, b"fixture\n")
    finally:
        os.close(descriptor)
    subprocess.run(
        [b"git", b"-C", os.fsencode(repo), b"add", raw_name],
        check=True,
        capture_output=True,
        shell=False,
    )
    _git(repo, "commit", "-q", "-m", "Add byte-safe fixture path")
    commit_sha = _git(repo, "rev-parse", "HEAD").strip()

    pages = list(
        LocalGitAdapter(
            LocalGitConfig(
                repository_path=repo,
                allowed_root=tmp_path,
                source_instance="sample-store-local",
                app_id="sample_store",
                email_key=b"test-key",
            )
        ).iter_pages()
    )
    commit = next(
        record
        for page in pages
        if page.resource_type == "commit"
        for record in page.records
        if record.identity.external_id == commit_sha
    )

    assert commit.payload["changed_paths"] == [
        {
            "path": "odd-\tline\n.bin",
            "path_encoding": "utf-8",
            "old_path": None,
            "old_path_encoding": None,
            "additions": 1,
            "deletions": 0,
            "binary": False,
            "status": "added",
            "status_code": "A",
            "new_file": True,
            "deleted_file": False,
            "renamed_file": False,
        }
    ]
    assert LocalGitAdapter._decode_git_path(b"odd-\xff.bin") == (
        "odd-\\xff.bin",
        "escaped-bytes",
    )
    assert "patch" not in commit.payload
    assert "diff" not in commit.payload


def test_changed_paths_preserve_deletion_and_rename_status(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    _git(repo, "mv", "one.txt", "renamed.txt")
    (repo / "binary.dat").unlink()
    _git(repo, "add", "binary.dat")
    _git(repo, "commit", "-q", "-m", "Rename text and delete binary fixture")
    commit_sha = _git(repo, "rev-parse", "HEAD").strip()

    records = [
        record
        for page in LocalGitAdapter(
            LocalGitConfig(
                repository_path=repo,
                allowed_root=tmp_path,
                source_instance="sample-store-local",
                app_id="sample_store",
                email_key=b"test-key",
            )
        ).iter_pages()
        for record in page.records
    ]
    commit = next(record for record in records if record.identity.external_id == commit_sha)
    changed_paths = commit.payload["changed_paths"]

    deleted = next(item for item in changed_paths if item["path"] == "binary.dat")
    assert deleted["status"] == "deleted"
    assert deleted["deleted_file"] is True
    assert deleted["renamed_file"] is False
    assert deleted["binary"] is True

    renamed = next(item for item in changed_paths if item["path"] == "renamed.txt")
    assert renamed["old_path"] == "one.txt"
    assert renamed["status"] == "renamed"
    assert str(renamed["status_code"]).startswith("R")
    assert renamed["renamed_file"] is True
    assert renamed["deleted_file"] is False
