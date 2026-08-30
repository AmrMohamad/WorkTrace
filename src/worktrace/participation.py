"""Source-qualified participation vocabulary and claim-safe projections."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum


class ParticipationCategory(StrEnum):
    IMPLEMENTED = "implemented"
    REVIEWED = "reviewed"
    ASSIGNED = "assigned"
    MERGED = "merged"
    DEPLOYED = "deployed"
    RELEASE_ASSOCIATED = "release_associated"
    CONTEXT = "context"


_CANONICAL_BY_SOURCE_KIND_ROLE: dict[tuple[str, str, str], str] = {
    ("git", "git_commit", "author"): "git_author",
    ("git", "git_commit", "co_author"): "git_coauthor",
    ("git", "git_commit", "committer"): "git_committer",
    ("git", "git_commit", "reviewer"): "git_reviewer",
    ("git", "git_tag", "author"): "git_tag_author",
    ("git", "git_tag", "git_author"): "git_tag_author",
    ("gitlab", "gitlab_mr", "author"): "mr_author",
    ("gitlab", "gitlab_mr", "assignee"): "mr_assignee",
    ("gitlab", "gitlab_mr", "reviewer"): "mr_reviewer",
    ("gitlab", "gitlab_mr", "merger"): "mr_merger",
    ("gitlab", "gitlab_merge_request_commit", "author"): "gitlab_commit_author",
    ("gitlab", "gitlab_merge_request_commit", "co_author"): "gitlab_commit_coauthor",
    ("gitlab", "gitlab_merge_request_commit", "committer"): "gitlab_commit_committer",
    ("gitlab", "gitlab_merge_request_commit", "reviewer"): "gitlab_commit_reviewer",
    ("gitlab", "gitlab_discussion", "author"): "gitlab_discussion_author",
    (
        "gitlab",
        "gitlab_merge_request_discussion_note",
        "author",
    ): "gitlab_discussion_author",
    ("gitlab", "git_deployment", "deployer"): "gitlab_deployer",
    ("gitlab", "gitlab_release", "release_author"): "gitlab_release_author",
    ("gitlab", "gitlab_release", "author"): "gitlab_release_author",
    ("jira", "jira_issue", "assignee"): "jira_assignee",
    ("jira", "jira_issue", "reporter"): "jira_reporter",
    ("jira", "jira_issue", "creator"): "jira_creator",
    ("jira", "jira_issue_comment", "author"): "jira_comment_author",
    ("jira", "jira_issue_changelog", "author"): "jira_changelog_author",
    ("jira", "jira_issue_changelog", "assignee"): "jira_assignee",
}

_KNOWN_CANONICAL = {
    "git_author",
    "git_tag_author",
    "git_coauthor",
    "git_committer",
    "git_reviewer",
    "mr_author",
    "mr_assignee",
    "mr_reviewer",
    "mr_merger",
    "gitlab_commit_author",
    "gitlab_commit_coauthor",
    "gitlab_commit_committer",
    "gitlab_commit_reviewer",
    "gitlab_discussion_author",
    "gitlab_deployer",
    "gitlab_release_author",
    "jira_assignee",
    "jira_reporter",
    "jira_creator",
    "jira_comment_author",
    "jira_changelog_author",
}

_CATEGORIES: dict[str, frozenset[ParticipationCategory]] = {
    "git_author": frozenset({ParticipationCategory.IMPLEMENTED}),
    "git_tag_author": frozenset({ParticipationCategory.RELEASE_ASSOCIATED}),
    "git_coauthor": frozenset({ParticipationCategory.IMPLEMENTED}),
    "git_reviewer": frozenset({ParticipationCategory.REVIEWED}),
    # Opening or submitting an MR is a factual coordination record.  Changed
    # paths describe the MR, not who authored the implementation in it.
    "mr_author": frozenset({ParticipationCategory.CONTEXT}),
    "mr_assignee": frozenset({ParticipationCategory.ASSIGNED}),
    "mr_reviewer": frozenset({ParticipationCategory.REVIEWED}),
    "mr_merger": frozenset({ParticipationCategory.MERGED}),
    "gitlab_commit_author": frozenset({ParticipationCategory.IMPLEMENTED}),
    "gitlab_commit_coauthor": frozenset({ParticipationCategory.IMPLEMENTED}),
    "gitlab_commit_reviewer": frozenset({ParticipationCategory.REVIEWED}),
    "gitlab_deployer": frozenset({ParticipationCategory.DEPLOYED}),
    "gitlab_release_author": frozenset({ParticipationCategory.RELEASE_ASSOCIATED}),
    "jira_assignee": frozenset({ParticipationCategory.ASSIGNED}),
}


def canonical_role(source: str, kind: str, role: str) -> str:
    """Return the canonical role without guessing across source/object boundaries."""

    normalized = role.strip().casefold()
    source_kind_role = (source.casefold(), kind.casefold(), normalized)
    if source_kind_role in _CANONICAL_BY_SOURCE_KIND_ROLE:
        return _CANONICAL_BY_SOURCE_KIND_ROLE[source_kind_role]
    if normalized in _KNOWN_CANONICAL:
        return normalized
    return normalized


def categories_for(source: str, kind: str, role: str) -> frozenset[ParticipationCategory]:
    return _CATEGORIES.get(canonical_role(source, kind, role), frozenset())


def categories_for_evidence(
    source: str,
    kind: str,
    role: str,
    data: Mapping[str, object],
) -> frozenset[ParticipationCategory]:
    """Project a role using the minimum object evidence required by its claim."""

    # ``data`` remains part of this API because some role projections may need
    # object-specific evidence in the future.  MR paths intentionally do not
    # promote MR authorship into implementation authorship.
    del data
    return categories_for(source, kind, role)


def supports_category(
    source: str,
    kind: str,
    role: str,
    category: ParticipationCategory,
) -> bool:
    return category in categories_for(source, kind, role)


def is_implementation_role(source: str, kind: str, role: str) -> bool:
    return supports_category(source, kind, role, ParticipationCategory.IMPLEMENTED)


def is_implementation_evidence(
    source: str, kind: str, role: str, data: Mapping[str, object]
) -> bool:
    return ParticipationCategory.IMPLEMENTED in categories_for_evidence(source, kind, role, data)
