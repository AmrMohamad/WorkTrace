from __future__ import annotations

from datetime import date

import httpx
import pytest

from worktrace.adapters.base import ActorIdentity, Participation, ParticipationRole
from worktrace.adapters.gitlab import GitLabAdapter, GitLabConfig
from worktrace.adapters.jira import JiraAdapter, JiraConfig
from worktrace.domain.models import NormalizedObject
from worktrace.errors import ConfigurationError, PermanentSourceError, ScopeViolation
from worktrace.importers.orchestrator import record_to_object
from worktrace.normalize import Redactor, actor_identity, build_record
from worktrace.participation import is_implementation_role

_REDACTOR = Redactor(b"synthetic-identity-key")
_SELF_EMAIL = "engineer@example.com"
_OLD_EMAIL = "old-work@example.com"
_SELF_IDS = {_REDACTOR.hash_email(_SELF_EMAIL), _REDACTOR.hash_email(_OLD_EMAIL)}


def _actor(
    source: str,
    *,
    name: str = "Same Name",
    email: str | None = None,
    provider_id: str | None = None,
) -> ActorIdentity:
    return actor_identity(
        source_kind=source,
        source_instance="synthetic-source",
        redactor=_REDACTOR,
        provider_actor_id=provider_id,
        display_name=name,
        email=email,
    )


def _normalized(
    source: str,
    object_type: str,
    participations: tuple[Participation, ...],
    identities: set[str],
) -> NormalizedObject:
    record = build_record(
        source_kind=source,
        source_instance="synthetic-source",
        object_type=object_type,
        external_id="synthetic-object",
        app_id="sample_store",
        observed_at="2026-01-31T00:00:00Z",
        source_updated_at="2026-01-01T00:00:00Z",
        payload={"title": "Synthetic identity record"},
        redactor=_REDACTOR,
        participations=participations,
    )
    return record_to_object(record, identities, {"same name"})


@pytest.mark.parametrize("email", ["someone-else@example.com", None])
def test_same_name_different_or_missing_email_is_not_self(email: str | None) -> None:
    actor = _actor("git", email=email)
    result = _normalized(
        "git", "commit", (Participation(actor, ParticipationRole.AUTHOR),), _SELF_IDS
    )
    assert not result.actors[0].is_self


def test_configured_old_email_is_self_despite_name_change() -> None:
    actor = _actor("git", name="Changed Name", email=_OLD_EMAIL)
    result = _normalized(
        "git", "commit", (Participation(actor, ParticipationRole.AUTHOR),), _SELF_IDS
    )
    assert result.actors[0].is_self


def test_git_provider_id_cannot_substitute_for_configured_email() -> None:
    actor = _actor("git", email="someone-else@example.com", provider_id="123")
    result = _normalized(
        "git", "commit", (Participation(actor, ParticipationRole.AUTHOR),), {"123"}
    )
    assert not result.actors[0].is_self


def test_committer_only_is_not_implementation_and_coauthor_retains_role() -> None:
    others = _actor("git", email="someone-else@example.com")
    committer = _actor("git", email=_SELF_EMAIL)
    coauthor = _actor("git", name="Old Name", email=_OLD_EMAIL)
    result = _normalized(
        "git",
        "commit",
        (
            Participation(others, ParticipationRole.AUTHOR),
            Participation(committer, ParticipationRole.COMMITTER),
            Participation(coauthor, ParticipationRole.CO_AUTHOR),
        ),
        _SELF_IDS,
    )
    flags = {actor.external_actor_id: actor.is_self for actor in result.actors}
    assert not flags[others.source_actor_id]
    assert flags[committer.source_actor_id] and flags[coauthor.source_actor_id]
    roles = {part.actor_external_id: part.role for part in result.participations}
    assert roles[others.source_actor_id] == "git_author"
    assert roles[committer.source_actor_id] == "git_committer"
    assert not is_implementation_role("git", "git_commit", roles[committer.source_actor_id])
    assert roles[coauthor.source_actor_id] == "git_coauthor"
    assert is_implementation_role("git", "git_commit", roles[coauthor.source_actor_id])


@pytest.mark.parametrize("provider_id, expected", [("configured-account", True), ("other", False)])
def test_jira_requires_exact_configured_account(provider_id: str, expected: bool) -> None:
    actor = _actor("jira", provider_id=provider_id, email=_SELF_EMAIL)
    result = _normalized(
        "jira",
        "issue",
        (Participation(actor, ParticipationRole.ASSIGNEE),),
        {"configured-account"},
    )
    assert result.actors[0].is_self is expected


@pytest.mark.parametrize(
    "object_type, provider_id, email, expected",
    [
        ("merge_request", "42", "other@example.com", True),
        ("merge_request", "99", _SELF_EMAIL, False),
        ("merge_request_commit", "99", _SELF_EMAIL, False),
        ("merge_request_commit", "invalid-provider", _SELF_EMAIL, False),
        ("merge_request_commit", None, _SELF_EMAIL, True),
        ("merge_request_commit", None, _OLD_EMAIL, True),
        ("merge_request_commit", None, "other@example.com", False),
        ("merge_request_commit", None, None, False),
        ("merge_request", None, _SELF_EMAIL, False),
    ],
)
def test_gitlab_provider_ids_take_precedence_and_email_only_commits_match_exactly(
    object_type: str, provider_id: str | None, email: str | None, expected: bool
) -> None:
    actor = _actor("gitlab", provider_id=provider_id, email=email)
    result = _normalized(
        "gitlab",
        object_type,
        (Participation(actor, ParticipationRole.AUTHOR),),
        {"42", *_SELF_IDS},
    )
    assert result.actors[0].is_self is expected


def _config(*, user_id: int | None = None, username: str = "engineer") -> GitLabConfig:
    return GitLabConfig(
        base_url="https://gitlab.example",
        source_instance="synthetic-source",
        app_id="sample_store",
        project_id=77,
        email_key=b"synthetic-identity-key",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        user_id=user_id,
        username=username,
    )


def test_gitlab_username_resolves_numeric_identity_once_before_pages() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v4/user":
            return httpx.Response(
                200, json={"id": 42, "username": "engineer", "email": _SELF_EMAIL}
            )
        return httpx.Response(200, json=[])

    aliases = {_REDACTOR.hash_email(_OLD_EMAIL)}
    with httpx.Client(
        base_url="https://gitlab.example", transport=httpx.MockTransport(handler)
    ) as client:
        adapter = GitLabAdapter(_config(), client)
        resolved = adapter.resolved_self_ids(aliases)
        assert resolved == {"42", *_SELF_IDS}
        assert aliases == {_REDACTOR.hash_email(_OLD_EMAIL)}
        assert [request.url.path for request in requests] == ["/api/v4/user"]
        list(adapter.iter_pages())
        assert adapter.resolved_self_ids(set()) == {"42", _REDACTOR.hash_email(_SELF_EMAIL)}
    assert sum(request.url.path == "/api/v4/user" for request in requests) == 1
    assert any(request.url.params.get("author_id") == "42" for request in requests)


@pytest.mark.parametrize("provider_id", [0, -1, True, "engineer", "\uff14\uff12", "", None])
def test_gitlab_rejects_malformed_numeric_identity(provider_id: object) -> None:
    with httpx.Client(
        base_url="https://gitlab.example",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": provider_id, "username": "engineer"})
        ),
    ) as client:
        adapter = GitLabAdapter(_config(), client)
        with pytest.raises(PermanentSourceError):
            adapter.resolved_self_ids(_SELF_IDS)


@pytest.mark.parametrize("provider_id, username", [(99, "engineer"), (42, "different-user")])
def test_gitlab_identity_conflict_fails_before_resolving_aliases(
    provider_id: int, username: str
) -> None:
    with httpx.Client(
        base_url="https://gitlab.example",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"id": provider_id, "username": username, "email": _SELF_EMAIL}
            )
        ),
    ) as client:
        adapter = GitLabAdapter(_config(user_id=42), client)
        with pytest.raises(ScopeViolation):
            adapter.resolved_self_ids(_SELF_IDS)


def _jira_config(account_id: str | None = "configured-account") -> JiraConfig:
    return JiraConfig(
        base_url="https://jira.example",
        source_instance="synthetic-source",
        app_id="sample_store",
        project_keys=("DEMO",),
        email_key=b"synthetic-identity-key",
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        account_id=account_id,
        discovered_issue_keys=("DEMO-1",),
    )


def test_jira_resolves_configured_account_once_before_pages() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/rest/api/3/myself":
            return httpx.Response(200, json={"accountId": "configured-account"})
        return httpx.Response(200, json={"issues": [], "isLast": True})

    with httpx.Client(
        base_url="https://jira.example", transport=httpx.MockTransport(handler)
    ) as client:
        adapter = JiraAdapter(_jira_config(), client)
        assert adapter.resolved_self_id() == "configured-account"
        list(adapter.iter_discovery_pages())
        assert adapter.resolved_self_id() == "configured-account"
    assert sum(request.url.path == "/rest/api/3/myself" for request in requests) == 1


@pytest.mark.parametrize("account_id", ["other-account", None])
def test_jira_does_not_resolve_conflicting_provider_account(account_id: str | None) -> None:
    with (
        httpx.Client(
            base_url="https://jira.example",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"accountId": account_id})
            ),
        ) as client,
        pytest.raises(ScopeViolation),
    ):
        JiraAdapter(_jira_config(), client).resolved_self_id()


def test_jira_exact_key_discovery_does_not_invent_self_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("Missing configured identity must fail before network access")

    with (
        httpx.Client(
            base_url="https://jira.example", transport=httpx.MockTransport(handler)
        ) as client,
        pytest.raises(ConfigurationError, match="configured account_id"),
    ):
        JiraAdapter(_jira_config(None), client).resolved_self_id()
