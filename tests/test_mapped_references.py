from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from worktrace.config import (
    AppConfig,
    GitLabRepositoryMapping,
    IdentityConfig,
    WorkTraceConfig,
    load_config,
)
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.db.repository import EvidenceRepository, source_instance_id
from worktrace.domain.enums import Completeness
from worktrace.domain.models import NormalizedObject, PendingReference, SourceIdentity
from worktrace.errors import ConfigurationError
from worktrace.linking.builder import _objects, rebuild_references
from worktrace.linking.mappings import mapped_commit_sha_allowed, mapped_source_instance_pairs


def _config(apps: str, directory: Path) -> Path:
    path = directory / "config.toml"
    path.write_text(
        f"""schema_version = 1

[data]
directory = {str(directory / "data")!r}

[employment]
from = "2024-01-01"
to = "2026-08-26"

[identity]
display_name = "Fixture Engineer"

{apps}
""",
        encoding="utf-8",
    )
    return path


def test_repository_mappings_are_scoped_strict_and_normalized(tmp_path: Path) -> None:
    repo_a = (tmp_path / "a").resolve()
    repo_b = (tmp_path / "b").resolve()
    apps = f"""[[apps]]
id = "sample"
name = "Sample"
repo_paths = [{str(repo_a)!r}, {str(repo_b)!r}]
gitlab_project_ids = [101, 202]
jira_project_keys = []

[[apps.gitlab_repository_mappings]]
repo_path = {str(repo_b)!r}
gitlab_project_id = 202

[[apps.gitlab_repository_mappings]]
repo_path = {str(repo_a)!r}
gitlab_project_id = 101
"""
    app = load_config(_config(apps, tmp_path)).app("sample")

    assert [
        (item.repo_path, item.gitlab_project_id) for item in app.gitlab_repository_mappings
    ] == [
        (repo_a, 101),
        (repo_b, 202),
    ]
    assert mapped_source_instance_pairs(app) == {
        (source_instance_id("sample", "gitlab", 101), source_instance_id("sample", "git", repo_a)),
        (source_instance_id("sample", "gitlab", 202), source_instance_id("sample", "git", repo_b)),
    }


@pytest.mark.parametrize(
    "mapping, message",
    [
        ('repo_path = ""\ngitlab_project_id = 101', "repo_path"),
        ('repo_path = "{repo}"\ngitlab_project_id = true', "positive integer"),
        ('repo_path = "{repo}"\ngitlab_project_id = 0', "positive integer"),
        ('repo_path = "{repo}"\ngitlab_project_id = 999', "not configured"),
    ],
)
def test_repository_mapping_rejects_invalid_values(
    tmp_path: Path, mapping: str, message: str
) -> None:
    repo = (tmp_path / "repo").resolve()
    app = f"""[[apps]]
id = "sample"
name = "Sample"
repo_paths = [{str(repo)!r}]
gitlab_project_ids = [101]
jira_project_keys = []

[[apps.gitlab_repository_mappings]]
{mapping.format(repo=repo)}
"""
    with pytest.raises(ConfigurationError, match=message):
        load_config(_config(app, tmp_path))


def _app(repo_a: Path, repo_b: Path, *, mapped: bool) -> AppConfig:
    return AppConfig(
        id="sample",
        name="Sample",
        market="",
        business_type="",
        jira_project_keys=(),
        gitlab_project_ids=(101,),
        repo_paths=(repo_a, repo_b),
        jira_key_patterns=(),
        production_environments=(),
        release_tag_patterns=(),
        ignored_paths=(),
        gitlab_repository_mappings=((GitLabRepositoryMapping(repo_a, 101),) if mapped else ()),
    )


def _worktrace_config(app: AppConfig, directory: Path) -> WorkTraceConfig:
    return WorkTraceConfig(
        schema_version=1,
        data_directory=directory,
        employment_from=date(2024, 1, 1),
        employment_to=date(2026, 8, 26),
        identity=IdentityConfig("Fixture", (), (), None, None, None),
        apps=(app,),
        config_path=directory / "config.toml",
    )


def _object(
    source: str,
    instance: str,
    kind: str,
    external_id: str,
    *,
    title: str = "",
    data: dict[str, object] | None = None,
    pending: tuple[PendingReference, ...] = (),
) -> NormalizedObject:
    return NormalizedObject(
        identity=SourceIdentity(source, instance, kind, external_id),
        app_id="sample",
        title=title,
        body_text="",
        source_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        actors=(),
        participations=(),
        pending_references=pending,
        data=data or {},  # type: ignore[arg-type]
        completeness=Completeness.COMPLETE,
    )


@pytest.mark.parametrize("sha", ["a" * 40, "b" * 64])
def test_mapped_sha_paths_are_explicit_and_do_not_join_decoy_repositories(
    tmp_path: Path, sha: str
) -> None:
    repo_a, repo_b = (tmp_path / "a").resolve(), (tmp_path / "b").resolve()
    app = _app(repo_a, repo_b, mapped=True)
    connection = connect(tmp_path / "ledger.sqlite3")
    try:
        migrate(connection, tmp_path / "ledger.sqlite3")
        repository = EvidenceRepository(connection)
        repository.ensure_apps(_worktrace_config(app, tmp_path))
        git_a = source_instance_id("sample", "git", repo_a)
        git_b = source_instance_id("sample", "git", repo_b)
        gitlab = source_instance_id("sample", "gitlab", 101)
        run = repository.start_sync_run("sample", "git", git_a, {"mode": "fixture"})
        repository.store_page(run, [_object("git", git_a, "git_commit", sha)])
        repository.finish_sync_run(run, "complete", "complete_for_scope")
        run = repository.start_sync_run("sample", "git", git_b, {"mode": "fixture"})
        repository.store_page(run, [_object("git", git_b, "git_commit", sha)])
        repository.finish_sync_run(run, "complete", "complete_for_scope")
        run = repository.start_sync_run(
            "sample", "gitlab", gitlab, {"mode": "fixture", "selection_policy_version": 2}
        )
        repository.store_page(
            run,
            [
                _object(
                    "gitlab",
                    gitlab,
                    "gitlab_mr",
                    "101:1",
                    title=sha,
                    data={"commit_shas": [sha]},
                    pending=(
                        PendingReference(
                            "git",
                            "git_commit",
                            sha,
                            "gitlab_source_head",
                            "structured",
                            sha,
                        ),
                    ),
                ),
                _object(
                    "gitlab",
                    gitlab,
                    "gitlab_merge_request_commit",
                    f"101:1:{sha}",
                    data={"sha": sha},
                ),
            ],
        )
        repository.finish_sync_run(run, "complete", "complete_for_scope")

        current = _objects(repository, "sample")
        gitlab_object = next(item for item in current if item.source == "gitlab")
        git_object = next(item for item in current if item.source_instance == git_a)
        assert mapped_commit_sha_allowed(app, gitlab_object, git_object, sha)
        rebuild_references(app, repository)
        rows = list(
            connection.execute(
                'SELECT relationship_type, extraction_method, to_object_id FROM "references" '
                "ORDER BY extraction_method"
            )
        )
        assert {str(row["relationship_type"]) for row in rows} == {"mapped_commit_sha"}
        assert {str(row["to_object_id"]) for row in rows} == {
            str(
                connection.execute(
                    "SELECT id FROM source_objects WHERE source_instance=?", (git_a,)
                ).fetchone()[0]
            )
        }
        assert {str(row["extraction_method"]) for row in rows} == {
            "explicit_repo_project_full_sha:textual_reference",
            "explicit_repo_project_full_sha:mr_contains_commit",
            "explicit_repo_project_full_sha:source_head",
            "explicit_repo_project_full_sha:commit_record",
        }
    finally:
        connection.close()


def test_mapping_guard_rejects_abbreviated_or_removed_mapping(tmp_path: Path) -> None:
    repo_a, repo_b = (tmp_path / "a").resolve(), (tmp_path / "b").resolve()
    sha = "a" * 40
    app = _app(repo_a, repo_b, mapped=True)
    source = {
        "app_id": "sample",
        "source": "gitlab",
        "source_instance": source_instance_id("sample", "gitlab", 101),
    }
    target = {
        "app_id": "sample",
        "source": "git",
        "source_instance": source_instance_id("sample", "git", repo_a),
        "kind": "git_commit",
        "external_id": sha,
    }
    assert mapped_commit_sha_allowed(app, source, target, sha)
    assert not mapped_commit_sha_allowed(app, source, target, sha[:12])
    assert not mapped_commit_sha_allowed(_app(repo_a, repo_b, mapped=False), source, target, sha)
