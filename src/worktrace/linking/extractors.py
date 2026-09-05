from __future__ import annotations

import re

from worktrace.config import AppConfig

SHA_RE = re.compile(r"(?<![0-9a-f])([0-9a-f]{7,40})(?![0-9a-f])", re.IGNORECASE)
FULL_SHA_RE = re.compile(r"(?<![0-9a-f])([0-9a-f]{40}|[0-9a-f]{64})(?![0-9a-f])", re.IGNORECASE)
MR_RE = re.compile(r"(?<![A-Za-z0-9])!(?P<iid>[1-9][0-9]*)")


def extract_jira_keys(text: str, app: AppConfig) -> set[str]:
    results: set[str] = set()
    for pattern in app.jira_key_patterns:
        results.update(match.upper() for match in re.findall(pattern, text, flags=re.IGNORECASE))
    return {key for key in results if app.allows_jira_key(key)}


def extract_commit_shas(text: str) -> set[str]:
    return {match.lower() for match in SHA_RE.findall(text)}


def extract_full_commit_shas(text: str) -> set[str]:
    """Extract only unambiguous full Git object identifiers for cross-provider mapping."""

    return {match.lower() for match in FULL_SHA_RE.findall(text)}


def extract_mr_iids(text: str) -> set[str]:
    return {match.group("iid") for match in MR_RE.finditer(text)}
