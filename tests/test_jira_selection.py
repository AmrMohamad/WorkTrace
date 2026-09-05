from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest

from worktrace.adapters.base import (
    NormalizedRecord,
    Participation,
    ParticipationRole,
    Reference,
    ReferenceStrength,
)
from worktrace.candidates.decisions import append_decision, undo_decision
from worktrace.config import AppConfig, IdentityConfig, WorkTraceConfig
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.db.repository import EvidenceRepository
from worktrace.errors import ScopeViolation
from worktrace.identity import initialize_identity
from worktrace.importers.jira_selection import select_jira_seeds
from worktrace.importers.orchestrator import record_to_object
from worktrace.normalize import actor_identity, build_record
from worktrace.normalize.redaction import Redactor

_KEY = b"synthetic-selector-key"
_REDACTOR = Redactor(_KEY)


@pytest.fixture
def state(tmp_path: Path) -> Iterator[tuple[EvidenceRepository, WorkTraceConfig]]:
    app = AppConfig("sample", "Sample", "", "", ("DEMO",), (), (), (r"DEMO-[0-9]+",), (), (), ())
    config = WorkTraceConfig(
        1,
        tmp_path,
        date(2026, 1, 1),
        date(2026, 12, 31),
        IdentityConfig(
            "Fixture Engineer", ("self@example.test",), ("Fixture Engineer",), None, None, None
        ),
        (app,),
        tmp_path / "config.toml",
    )
    path = tmp_path / "ledger.sqlite3"
    connection = connect(path)
    migrate(connection, path)
    connection.execute("INSERT INTO apps(id,name) VALUES ('sample','Sample')")
    connection.commit()
    initialize_identity(connection, config, _KEY)
    try:
        yield EvidenceRepository(connection), config
    finally:
        connection.close()


def record(
    identifier: str,
    title: str,
    *,
    personal: bool = True,
    role: ParticipationRole = ParticipationRole.AUTHOR,
    references: tuple[Reference, ...] = (),
    kind: str = "commit",
) -> NormalizedRecord:
    actor = actor_identity(
        source_kind="git",
        source_instance="fixture",
        redactor=_REDACTOR,
        provider_actor_id=None,
        display_name="Fixture Engineer",
        email="self@example.test" if personal else "colleague@example.test",
    )
    return build_record(
        source_kind="git",
        source_instance="fixture",
        object_type=kind,
        external_id=identifier,
        app_id="sample",
        observed_at="2026-02-01T12:00:00Z",
        source_updated_at="2026-01-10T12:00:00Z",
        payload={"title": title},
        redactor=_REDACTOR,
        participations=(Participation(actor, role),),
        references=references,
    )


def publish(repository: EvidenceRepository, records: list[NormalizedRecord]) -> dict[str, str]:
    run = repository.start_sync_run("sample", "git", "fixture", {"mode": "fixture"})
    repository.store_page(
        run,
        [
            record_to_object(
                item, {_REDACTOR.hash_email("self@example.test")}, {"Fixture Engineer"}
            )
            for item in records
        ],
    )
    repository.finish_sync_run(run, "complete", "complete_for_scope")
    return {
        str(row["external_id"]): str(row["id"]) for row in repository.current_observations("sample")
    }


def test_selects_every_personal_key_above_500_with_observation_support(state) -> None:
    repository, config = state
    observations = publish(repository, [record(str(i), f"DEMO-{i} change") for i in range(1, 503)])
    result = select_jira_seeds(repository, config.apps[0], configuration=config)
    assert set(result.keys) == {f"DEMO-{i}" for i in range(1, 503)}
    assert result.as_dict()["selected_count"] == result.as_dict()["discovered_count"] == 502
    assert result.as_dict()["omitted_count"] == 0
    for seed in result.seeds:
        assert seed.supporting_observation_ids == (observations[seed.key.removeprefix("DEMO-")],)
        assert seed.reasons == ("self_role:git_author",)


def test_same_name_colleague_excluded_and_role_reasons_union_without_ownership(state) -> None:
    repository, config = state
    observations = publish(
        repository,
        [
            record("author", "DEMO-1 change"),
            record("reviewer", "DEMO-1 review", role=ParticipationRole.REVIEWER),
            record("committer", "DEMO-2 integrate", role=ParticipationRole.COMMITTER),
            record("colleague", "DEMO-3 unrelated change", personal=False),
        ],
    )
    result = select_jira_seeds(
        repository, config.apps[0], configuration=config, explicit_keys=("demo-1",)
    )
    assert result.keys == ("DEMO-1", "DEMO-2")
    assert result.discovered_count == 3
    first, second = result.seeds
    assert set(first.supporting_observation_ids) == {
        observations["author"],
        observations["reviewer"],
    }
    assert set(first.reasons) == {
        "explicit_user_key",
        "self_role:git_author",
        "self_role:git_reviewer",
    }
    assert second.reasons == ("self_role:git_committer",)
    assert result.as_dict()["omitted_count"] == 1


@pytest.mark.parametrize("key", ["OTHER-1", "DEMO-1 OR DEMO-2", "DEMO-x", ""])
def test_explicit_keys_must_be_exact_and_in_scope(state, key: str) -> None:
    repository, config = state
    with pytest.raises(ScopeViolation):
        select_jira_seeds(repository, config.apps[0], configuration=config, explicit_keys=(key,))


def test_branch_requires_structural_tie_and_git_parents_are_not_followed(state) -> None:
    repository, config = state

    def reference(target: str, relationship: str) -> Reference:
        return Reference(relationship, target, ReferenceStrength.STRUCTURED, "git", "commit")

    observations = publish(
        repository,
        [
            record("root", "DEMO-1 personal", references=(reference("parent", "git_parent"),)),
            record("parent", "DEMO-2 ancestor", personal=False),
            record(
                "branch",
                "refs/heads/DEMO-3",
                personal=False,
                kind="ref",
                references=(reference("root", "git_ref_target"),),
            ),
            record("unrelated-branch", "refs/heads/DEMO-4", personal=False, kind="ref"),
        ],
    )
    result = select_jira_seeds(repository, config.apps[0], configuration=config)
    assert result.keys == ("DEMO-1", "DEMO-3")
    assert result.discovered_count == 4
    assert result.seeds[1].supporting_observation_ids == (observations["branch"],)
    assert set(result.seeds[1].reasons) == {
        "related_collaborator_context",
        "branch_reference_context_not_ownership",
    }


def test_confirmed_lineage_without_suggestion_respects_remove_and_undo(state) -> None:
    repository, config = state
    publish(repository, [record("colleague", "DEMO-7 collaborator work", personal=False)])
    object_id = str(repository.connection.execute("SELECT id FROM source_objects").fetchone()[0])
    target = "candidate:previous-suggestion"
    confirmation = append_decision(
        repository.connection,
        "confirm_candidate",
        target,
        {
            "contribution_id": "contribution:reviewed",
            "app_id": "sample",
            "title": "Reviewed work",
            "members": [object_id],
            "context_members": [],
        },
    )

    def selected():
        return select_jira_seeds(repository, config.apps[0], configuration=config)

    assert selected().keys == ("DEMO-7",)
    assert selected().seeds[0].reasons == ("confirmed_contribution:" + target,)
    removed = append_decision(
        repository.connection, "remove_member", target, {"source_object_id": object_id}
    )
    assert selected().keys == ()
    undo_decision(repository.connection, removed)
    assert selected().keys == ("DEMO-7",)
    undo_decision(repository.connection, confirmation)
    assert selected().keys == ()
    assert repository.connection.execute("SELECT count(*) FROM human_decisions").fetchone()[0] == 4


def test_only_current_observations_supply_keys_and_support(state) -> None:
    repository, config = state
    previous = publish(repository, [record("commit", "DEMO-1 previous wording")])
    current = publish(repository, [record("commit", "DEMO-2 current wording")])
    assert previous["commit"] != current["commit"]
    result = select_jira_seeds(repository, config.apps[0], configuration=config)
    assert result.keys == ("DEMO-2",)
    assert result.seeds[0].supporting_observation_ids == (current["commit"],)
    assert repository.connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 2


def test_explicit_key_needs_no_invented_observation(state) -> None:
    repository, config = state
    result = select_jira_seeds(
        repository, config.apps[0], configuration=config, explicit_keys=(" DEMO-9 ", "demo-9")
    )
    assert result.keys == ("DEMO-9",)
    assert result.seeds[0].supporting_observation_ids == ()
    assert result.seeds[0].reasons == ("explicit_user_key",)
    assert result.as_dict()["selected_count"] == result.as_dict()["discovered_count"] == 1


def test_confirmed_noncurrent_record_can_seed_recovery_with_historical_label(state) -> None:
    repository, config = state
    observations = publish(repository, [record("old", "DEMO-7 confirmed", personal=False)])
    object_id = str(repository.connection.execute("SELECT id FROM source_objects").fetchone()[0])
    append_decision(
        repository.connection,
        "confirm_candidate",
        "candidate:history",
        {
            "app_id": "sample",
            "contribution_id": "contribution:history",
            "members": [object_id],
            "context_members": [],
        },
    )
    publish(repository, [])
    result = select_jira_seeds(repository, config.apps[0], configuration=config)
    assert result.keys == ("DEMO-7",)
    assert result.seeds[0].supporting_observation_ids == (observations["old"],)
    assert "historical_confirmed_observation_not_current" in result.seeds[0].reasons
    assert repository.current_observations("sample") == []


@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("kind,field", [("issue", "key"), ("issue_comment", "issue_key")])
def test_confirmed_jira_structured_key_survives_normal_summary_and_retirement(
    state, retired, kind, field
) -> None:
    repository, config = state
    item = build_record(
        source_kind="jira",
        source_instance="jira",
        object_type=kind,
        external_id="10001",
        app_id="sample",
        observed_at="2026-02-01T12:00:00Z",
        source_updated_at="2026-01-10T12:00:00Z",
        payload={field: "DEMO-7", "summary": "Improve search"},
        redactor=_REDACTOR,
    )
    run = repository.start_sync_run("sample", "jira", "jira", {"selection_policy_version": 2})
    repository.store_page(run, [record_to_object(item, set())])
    repository.finish_sync_run(run, "complete", "complete_for_scope")
    row = repository.current_observations("sample")[0]
    append_decision(
        repository.connection,
        "confirm_candidate",
        "candidate:jira-history",
        {
            "app_id": "sample",
            "contribution_id": "contribution:jira-history",
            "members": [str(row["source_object_id"])],
            "context_members": [],
        },
    )
    if retired:
        replacement = repository.start_sync_run(
            "sample", "jira", "jira", {"selection_policy_version": 2}
        )
        repository.finish_sync_run(replacement, "complete", "complete_for_scope")
    selected = select_jira_seeds(repository, config.apps[0], configuration=config)
    assert selected.keys == ("DEMO-7",)
    assert selected.seeds[0].supporting_observation_ids == (str(row["id"]),)
    assert "confirmed_contribution:candidate:jira-history" in selected.seeds[0].reasons
    assert ("historical_confirmed_observation_not_current" in selected.seeds[0].reasons) is retired
