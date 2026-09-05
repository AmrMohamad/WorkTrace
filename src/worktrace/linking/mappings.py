"""Explicit GitLab-project to local-repository SHA mapping guards.

The opaque source-instance identities are the only correspondence used here.
Neither app co-membership nor Git remote metadata establishes a mapping.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from worktrace.config import AppConfig
from worktrace.db.repository import source_instance_id

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$", re.IGNORECASE)
_METHOD_PREFIX = "explicit_repo_project_full_sha:"


def _field(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _full_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.casefold()
    return normalized if _FULL_SHA_RE.fullmatch(normalized) else None


def mapped_source_instance_pairs(app: AppConfig) -> frozenset[tuple[str, str]]:
    """Return configured `(gitlab, git)` opaque source-instance pairs."""

    return frozenset(
        (
            source_instance_id(app.id, "gitlab", mapping.gitlab_project_id),
            source_instance_id(app.id, "git", mapping.repo_path),
        )
        for mapping in app.gitlab_repository_mappings
    )


def mapped_commit_sha_allowed(
    app: AppConfig,
    from_object: object,
    to_object: object,
    exact_value: object,
) -> bool:
    """Check one proposed GitLab-to-Git SHA link against live explicit mapping."""

    sha = _full_sha(exact_value)
    target_sha = _full_sha(_field(to_object, "external_id"))
    if sha is None or target_sha is None or sha != target_sha or len(sha) != len(target_sha):
        return False
    if (
        _field(from_object, "source") != "gitlab"
        or _field(to_object, "source") != "git"
        or _field(to_object, "kind") != "git_commit"
    ):
        return False
    for object_value in (from_object, to_object):
        object_app = _field(object_value, "app_id")
        if object_app is not None and object_app != app.id:
            return False
    source_pair = (_field(from_object, "source_instance"), _field(to_object, "source_instance"))
    return (
        isinstance(source_pair[0], str)
        and isinstance(source_pair[1], str)
        and source_pair in mapped_source_instance_pairs(app)
    )


def reference_mapping_allowed(
    app: AppConfig,
    reference: object,
    from_object: object,
    to_object: object,
) -> bool:
    """Fail closed unless a stored mapped-SHA reference remains currently valid.

    This is intentionally suitable for context reads: malformed legacy rows and
    removed mappings are simply not admissible and never cause a read failure.
    """

    if _field(reference, "relationship_type") != "mapped_commit_sha":
        return False
    method = _field(reference, "extraction_method")
    if not isinstance(method, str) or not method.startswith(_METHOD_PREFIX):
        return False
    return mapped_commit_sha_allowed(app, from_object, to_object, _field(reference, "exact_value"))
