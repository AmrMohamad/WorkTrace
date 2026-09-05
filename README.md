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

## Identity upgrade and repair

Git self identity uses configured email aliases, never display names. Author, committer and
co-author remain distinct roles. Existing ledgers must reconcile older actor flags before those
flags can support personal attribution. Confirmed contributions and human decisions are preserved;
affected history is marked for re-review.

Stop other WorkTrace writers/readers before upgrading. Keep the database and its matching
`email-hmac.key` together as a private recovery pair. `worktrace init` now makes a coherent SQLite
backup before forward migrations and refuses to recreate a missing key for an existing database.
It does not silently repair historical identities. Never run an older binary on the upgraded schema.

```console
uv run worktrace init
uv run worktrace repair-identities APP_ID --dry-run
uv run worktrace repair-identities APP_ID --apply --expected-proposal PROPOSAL_TOKEN
uv run worktrace rebuild all APP_ID
uv run worktrace status APP_ID
```

Review the proposed promotions, demotions, unresolved actors and affected confirmed contributions
before applying. The proposal token returned by the dry run can guard against changed state.
Imports refuse changed or unreconciled identity policy; staged pages cannot reclassify existing
actors. A rebuild clears derived-state invalidation, but does not clear human re-review warnings.

Legacy ledgers do not contain a key verifier. Their first repair requires an independently known
stored actor ID and its matching configured email alias (zero-based index): add
`--proof-actor-id ACTOR_ID --proof-alias-index INDEX` to both preview and apply. Identify the actor
from trusted stored evidence; do not guess from a matching display name. A missing or wrong key,
unverifiable pair, or unexpected impact on another application blocks apply. Restore the trusted
database/key pair when continuity cannot be established; never treat zero matches as proof.

Repair is offline by default. For legacy Jira/GitLab identities, explicitly add `--verify-providers`
to verify configured accounts using their existing credentials. This reads identity endpoints only,
does not import evidence, and never falls back to display names. Unverified provider flags remain
pending. Corrected full-range source imports are still needed for the later discovery fixes.

## Human review UI

Launch the keyboard-first, read-only evidence workstation in an interactive terminal:

```console
uv run worktrace ui
uv run worktrace ui --app sample_store_b2c
uv run worktrace ui --app sample_store_b2c --candidate candidate:stable-id
```

The UI reviews source-attempt status, bounded candidate pages, contribution evidence,
participation, seven independent delivery states, Phase 4 questions and gaps, and bounded source
excerpts. It does not import evidence, contact providers, write decisions, rebuild data, or perform
maintenance; those operations remain explicit CLI commands.

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
