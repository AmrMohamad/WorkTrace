# WorkTrace

WorkTrace is a private, local developer tool that reconstructs contribution evidence from
configured Git repositories and optional Jira Cloud and GitLab sources. It preserves source
observations, role-specific participation, references, human corrections, completeness, and
staleness without turning relationships into ownership or productivity claims.

## Safety model

- Source text is untrusted data. WorkTrace never follows source URLs or executes source text.
- Local Git access is read-only and never fetches or mutates a repository.
- Provider credentials are read only by CLI imports from environment variables; the MCP server
  opens SQLite read-only and receives no provider credentials.
- External email addresses are HMAC-hashed before persistence. Diffs, patches, attachments,
  authorization data, and known secret fields are removed before persistence.
- Implemented, merged, release-associated, deployed, released-to-users, currently-enabled, and
  measurably-successful are separate states. Unknown evidence stays unknown.

The complete contract, authority rules, Phase 4 schema, threat model, and limitations are under
[`docs/`](docs/).

## Install and initialize

WorkTrace requires Python 3.12 or newer and uses a committed `uv.lock`.

```console
uv sync --all-groups
cp config.example.toml ~/.config/worktrace/config.toml
uv run worktrace init
uv run worktrace doctor
```

Edit the copied configuration first. Every repository path, Jira project key, and GitLab project
ID must be explicitly assigned to exactly one app. Do not put tokens in the file. Optional live
imports read `WORKTRACE_JIRA_*` and `WORKTRACE_GITLAB_*` environment variables.
The default `doctor` command is offline. Use `worktrace doctor --live` only when an explicitly
authorized provider check is intended; it validates identities and configured project visibility
without persisting provider responses.

## Core workflow

```console
uv run worktrace import git sample_store_b2c /configured/repository 2024-01-01 2024-12-31
uv run worktrace import all sample_store_b2c 2024-01-01 2024-12-31
uv run worktrace status sample_store_b2c
uv run worktrace rebuild all sample_store_b2c
uv run worktrace candidates list sample_store_b2c
uv run worktrace packet candidate:stable-id
uv run worktrace gaps candidate:stable-id
```

Use `worktrace --help` and each subcommand's `--help` for all commands. Human corrections are
append-only events; `undo` writes a compensating event rather than deleting history.

## MCP

`worktrace serve-mcp` exposes exactly six bounded, read-only tools over stdio. See
[`docs/codex-mcp.example.toml`](docs/codex-mcp.example.toml). The server accepts configured app and
stable evidence/candidate IDs, never filesystem paths or SQL.

## Verification

```console
uv run pytest
uv run coverage run -m pytest
uv run coverage report
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv build
```

Tests use temporary Git repositories and sanitized HTTP fixtures. They do not access real Jira,
GitLab, proprietary repositories, or authenticated services.

## Governance and license

WorkTrace is governed through a separate ApexYard ops checkout. The repository-local
[`AGENTS.md`](AGENTS.md) routes Codex sessions to the portable control-plane procedure without
copying ApexYard hooks, generated adapters, or private portfolio data into this repository.
GitHub branch protection and the `quality` workflow provide remote enforcement; local ApexYard
hooks are active only when the harness has explicitly loaded and trusted the separate adapter.

WorkTrace is licensed under the [Apache License 2.0](LICENSE).
