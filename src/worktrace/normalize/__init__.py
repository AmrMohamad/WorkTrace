"""Deterministic source normalization and redaction."""

from worktrace.normalize.actors import actor_identity, parse_git_trailers
from worktrace.normalize.identities import stable_actor_id, stable_source_object_id
from worktrace.normalize.records import build_record, normalize_timestamp, observed_now
from worktrace.normalize.redaction import Redactor, extract_jira_text
from worktrace.normalize.references import exact_jira_keys

__all__ = [
    "Redactor",
    "actor_identity",
    "build_record",
    "exact_jira_keys",
    "extract_jira_text",
    "normalize_timestamp",
    "observed_now",
    "parse_git_trailers",
    "stable_actor_id",
    "stable_source_object_id",
]
