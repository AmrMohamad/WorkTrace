"""Exact, typed reference extraction."""

from __future__ import annotations

import re
from collections.abc import Iterable


def exact_jira_keys(text: str, allowed_project_keys: Iterable[str]) -> tuple[str, ...]:
    """Extract exact Jira keys in configured projects, preserving no ownership meaning."""

    projects = sorted({key.strip().upper() for key in allowed_project_keys if key.strip()})
    if not projects:
        return ()
    alternatives = "|".join(re.escape(key) for key in projects)
    pattern = re.compile(rf"(?<![A-Z0-9_-])((?:{alternatives})-[1-9][0-9]*)(?![A-Z0-9_-])")
    return tuple(dict.fromkeys(match.group(1).upper() for match in pattern.finditer(text.upper())))
