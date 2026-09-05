"""Shared environment policy for local Git metadata reads, not a sandbox."""

from __future__ import annotations

import os

_BLOCKED_PREFIXES = (
    "WORKTRACE_JIRA_",
    "WORKTRACE_GITLAB_",
    "WORKTRACE_EMAIL_HMAC",
    "GIT_CONFIG_",
    "GIT_TRACE",
)
_BLOCKED_VARIABLES = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_NAMESPACE",
        "GIT_SHALLOW_FILE",
        "GIT_GRAFT_FILE",
        "GIT_REPLACE_REF_BASE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXEC_PATH",
        "GIT_EXTERNAL_DIFF",
        "GIT_DIFF_OPTS",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSH_VARIANT",
        "GIT_PAGER",
    }
)


def local_git_environment() -> dict[str, str]:
    """Copy ambient settings without provider secrets or Git execution/path overrides.

    Normal repository and user configuration remains readable, including intentional
    mailmap settings. This only narrows the environment inherited by local Git reads.
    """
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in _BLOCKED_VARIABLES and not name.startswith(_BLOCKED_PREFIXES)
    }
    environment.update(
        GIT_OPTIONAL_LOCKS="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_TERMINAL_PROMPT="0",
    )
    return environment
