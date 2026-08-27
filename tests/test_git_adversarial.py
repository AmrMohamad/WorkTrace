from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import date
from pathlib import Path

from worktrace.adapters.base import NormalizedRecord, ParticipationRole
from worktrace.adapters.git_local import LocalGitAdapter, LocalGitConfig
from worktrace.config import AppConfig
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.db.repository import EvidenceRepository
from worktrace.importers.orchestrator import import_snapshot
from worktrace.linking.builder import rebuild_references
from worktrace.normalize.redaction import Redactor


def _git(repository: Path, *arguments: str, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def _identity_environment(
    *,
    author_name: str,
    author_email: str,
    committer_name: str,
    committer_email: str,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": author_name,
            "GIT_AUTHOR_EMAIL": author_email,
            "GIT_AUTHOR_DATE": "2026-01-10T10:00:00Z",
            "GIT_COMMITTER_NAME": committer_name,
            "GIT_COMMITTER_EMAIL": committer_email,
            "GIT_COMMITTER_DATE": "2026-01-10T10:05:00Z",
        }
    )
    return environment


def _commit(
    repository: Path,
    message: str,
    *,
    author_name: str = "Fixture Engineer",
    author_email: str = "self@example.test",
    committer_name: str = "Fixture Engineer",
    committer_email: str = "self@example.test",
    allow_empty: bool = False,
) -> str:
    command = ["commit"]
    if allow_empty:
        command.append("--allow-empty")
    command.extend(["-m", message])
    _git(
        repository,
        *command,
        environment=_identity_environment(
            author_name=author_name,
            author_email=author_email,
            committer_name=committer_name,
            committer_email=committer_email,
        ),
    )
    return _git(repository, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repository = tmp_path / "fixture-repository"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    (repository / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "base.txt")
    initial = _commit(
        repository,
        "DEMO-101 initial implementation\n\n"
        "Co-authored-by: Fixture Coauthor <coauthor@example.test>\n"
        "Reviewed-by: Fixture Reviewer <reviewer@example.test>",
        author_name="Fixture Author",
        author_email="author@example.test",
        committer_name="Fixture Integrator",
        committer_email="self@example.test",
    )

    _git(repository, "checkout", "-q", "-b", "topic")
    (repository / "topic.txt").write_text("topic\n", encoding="utf-8")
    _git(repository, "add", "topic.txt")
    topic = _commit(repository, "DEMO-101 topic implementation")

    _git(repository, "checkout", "-q", "main")
    (repository / "main.txt").write_text("main\n", encoding="utf-8")
    _git(repository, "add", "main.txt")
    _commit(repository, "DEMO-101 parallel main change")
    _git(
        repository,
        "merge",
        "--no-ff",
        "topic",
        "-m",
        "Merge topic for DEMO-101",
        environment=_identity_environment(
            author_name="Fixture Integrator",
            author_email="self@example.test",
            committer_name="Fixture Integrator",
            committer_email="self@example.test",
        ),
    )
    merge = _git(repository, "rev-parse", "HEAD")

    cherry_pick = _commit(
        repository,
        f"Backport DEMO-101\n\n(cherry picked from commit {topic})",
        allow_empty=True,
    )
    revert = _commit(
        repository,
        f"Revert DEMO-101 topic\n\nThis reverts commit {topic}.",
        allow_empty=True,
    )
    tag_environment = _identity_environment(
        author_name="Fixture Tagger",
        author_email="tagger@example.test",
        committer_name="Fixture Tagger",
        committer_email="tagger@example.test",
    )
    _git(repository, "tag", "-a", "v1.0.0", "-m", "Fixture release", environment=tag_environment)
    malicious_ref = "feature/DEMO-101;touch${IFS}PWNED"
    _git(repository, "branch", malicious_ref)

    return repository, {
        "initial": initial,
        "topic": topic,
        "merge": merge,
        "cherry_pick": cherry_pick,
        "revert": revert,
        "malicious_ref": f"refs/heads/{malicious_ref}",
    }


def _snapshot(repository: Path) -> bytes:
    tracked = _git(repository, "ls-files").splitlines()
    files = {
        name: hashlib.sha256((repository / name).read_bytes()).hexdigest()
        for name in sorted(tracked)
    }
    payload = {
        "head": _git(repository, "rev-parse", "HEAD"),
        "status": _git(repository, "status", "--porcelain=v2", "--branch"),
        "refs": _git(
            repository,
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/heads",
            "refs/tags",
        ),
        "files": files,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _records(adapter: LocalGitAdapter) -> list[NormalizedRecord]:
    return [record for page in adapter.iter_pages() for record in page.records]


def _record(records: list[NormalizedRecord], external_id: str) -> NormalizedRecord:
    return next(record for record in records if record.identity.external_id == external_id)


def _app(repository: Path) -> AppConfig:
    return AppConfig(
        id="sample_store",
        name="Sample Store",
        market="XX",
        business_type="fixture",
        jira_project_keys=("DEMO",),
        gitlab_project_ids=(),
        repo_paths=(repository,),
        jira_key_patterns=(r"DEMO-[0-9]+",),
        production_environments=(),
        release_tag_patterns=("v*",),
        ignored_paths=(),
    )


def _database(tmp_path: Path) -> tuple[sqlite3.Connection, EvidenceRepository]:
    path = tmp_path / "worktrace.sqlite3"
    connection = connect(path)
    migrate(connection, path)
    connection.execute(
        "INSERT INTO apps(id, name, market, business_type) "
        "VALUES ('sample_store', 'Sample Store', '', '')"
    )
    connection.commit()
    return connection, EvidenceRepository(connection)


def test_git_roles_and_relationships_remain_typed_without_repository_mutation(
    tmp_path: Path,
) -> None:
    repository_path, ids = _repository(tmp_path)
    before = _snapshot(repository_path)
    email_key = b"fixture-only-key"
    adapter = LocalGitAdapter(
        LocalGitConfig(
            repository_path=repository_path,
            allowed_root=repository_path,
            source_instance=str(repository_path),
            app_id="sample_store",
            email_key=email_key,
            jira_project_keys=("DEMO",),
            date_from=date(2026, 1, 1),
            date_to=date(2026, 12, 31),
            page_size=2,
        )
    )
    records = _records(adapter)

    initial = _record(records, ids["initial"])
    by_role = {participation.role: participation.actor for participation in initial.participations}
    assert by_role[ParticipationRole.AUTHOR].display_name == "Fixture Author"
    assert by_role[ParticipationRole.COMMITTER].display_name == "Fixture Integrator"
    assert (
        by_role[ParticipationRole.AUTHOR].email_hash
        != by_role[ParticipationRole.COMMITTER].email_hash
    )
    assert by_role[ParticipationRole.CO_AUTHOR].display_name == "Fixture Coauthor"
    assert by_role[ParticipationRole.REVIEWER].display_name == "Fixture Reviewer"

    merge = _record(records, ids["merge"])
    assert len([ref for ref in merge.references if ref.reference_type == "git_parent"]) == 2
    assert len([ref for ref in merge.references if ref.reference_type == "git_merge_parent"]) == 2
    assert merge.payload["is_merge"] is True
    assert merge.payload["merge_parent_shas"] == merge.payload["parent_shas"]
    cherry_pick = _record(records, ids["cherry_pick"])
    assert {
        (reference.reference_type, reference.target_external_id)
        for reference in cherry_pick.references
    } >= {("git_cherry_picks_commit", ids["topic"])}
    revert = _record(records, ids["revert"])
    assert {
        (reference.reference_type, reference.target_external_id) for reference in revert.references
    } >= {("git_reverts_commit", ids["topic"])}
    tag = _record(records, "refs/tags/v1.0.0")
    assert {reference.reference_type for reference in tag.references} >= {"git_ref_target"}
    malicious = _record(records, ids["malicious_ref"])
    assert malicious.payload["ref_name"] == ids["malicious_ref"]
    assert not (repository_path / "PWNED").exists()
    assert _snapshot(repository_path) == before

    connection, evidence = _database(tmp_path)
    try:
        result = import_snapshot(
            _app(repository_path),
            adapter,
            evidence,
            source="git",
            source_instance=str(repository_path),
            date_from=date(2026, 1, 1),
            date_to=date(2026, 12, 31),
            self_actor_ids={Redactor(email_key).hash_email("self@example.test")},
        )
        assert result.status == "complete"
        self_roles = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT p.role FROM participations p
                JOIN actors a ON a.id=p.actor_id
                WHERE a.is_self=1 AND p.source_object_id=(
                    SELECT id FROM source_objects WHERE external_id=?
                )
                """,
                (ids["initial"],),
            )
        }
        assert self_roles == {"git_committer"}

        assert rebuild_references(_app(repository_path), evidence) >= 1
        relationship_types = {
            str(row[0])
            for row in connection.execute('SELECT DISTINCT relationship_type FROM "references"')
        }
        assert {
            "git_parent_of",
            "git_cherry_picks_commit",
            "git_reverts_commit",
            "tag_points_to_commit",
        } <= relationship_types
        persisted = "\n".join(
            str(row[0]) for row in connection.execute("SELECT data_json FROM observations")
        ).casefold()
        assert '"diff"' not in persisted
        assert '"patch"' not in persisted
        assert _snapshot(repository_path) == before
    finally:
        connection.close()
