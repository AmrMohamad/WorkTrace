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

## Historical Jira activity

Jira discovery independently searches historical updates, assignment, creation, and exact
configured-project keys. Later issue edits do not erase earlier work. Selection reasons explain
why an issue was collected; they do not prove authorship or a precise work date. Day-level Jira
queries use conservative expanded bounds, with the verified Jira calendar recorded separately
from the optional `[employment].timezone` work calendar (default `UTC`).

Comments retain creation and visible last-edit actors/times separately. A historically created
comment edited later contains **current wording**, not a recovered historical text version.
Assignment intervals require matching stable account IDs on both transitions; missing or
ambiguous endpoints remain unknown. Predecessor/successor records are contextual evidence.
Candidate periods, evidence date filters, and `identity.when` use activity dates, never fetch
or issue freshness dates. Undated evidence is included without date filters and excluded when
filtering; `period_status` distinguishes known, partially known, and unknown periods.

Schema 5 adds CLI-owned, run-scoped staging of redacted Jira records. Discovery deduplicates by
stable issue ID, unions reasons, and retains the first observed version if Jira changes between
queries. Interrupted staging is nonauthoritative and never reused by a new run; successful
finalization removes its temporary rows. Existing observations and human decisions are preserved.

After the coherent backup/forward migration described above, existing Jira ledgers need an
explicit **full configured-range reimport** to gain this metadata; there is no synthetic backfill.
Do not shrink the configured interval to work around an import failure. After authorization:

```console
uv run worktrace import all APP_ID
uv run worktrace status APP_ID
```

`import all` rebuilds references and candidates; separate source imports require an explicit
`worktrace rebuild all APP_ID`. The six-tool MCP contract and cursor encoding are unchanged.

## Discovery coverage and selector upgrades

Jira exact-key discovery uses exact personal participation, canonically confirmed contributions,
and explicit `--jira-key DEMO-42` inputs. Related collaborator/branch records remain context;
Git ancestry is not a discovery traversal. All selected keys use existing provider chunks, with
discovered/selected/omitted counts, policy and supporting observation IDs. Confirmed historical
records can seed recovery, explicitly labelled as historical rather than current evidence.

Jira selector v3 requires the full configured interval. If a replacement would remove current
records, the import remains nonauthoritative and reports removed object IDs, affected confirmed
contributions and a `proposal_token`. Review that impact before authorizing a repeat:

```console
uv run worktrace import all APP_ID --approve-selector-replacement PROPOSAL_TOKEN
```

The token binds the previous observations, proposed membership and confirmed contribution state;
changed impact requires a new preview. Observations and human decisions are never deleted by this
upgrade. Old selectors remain explicitly labelled until a successful approved replacement.

Missing credentials, invalid origins and unverified identities are session preflight failures,
not new provider source instances. `status` exposes current preparation separately from retained
authoritative snapshots; a later successful attempt clears current failure without deleting its
audit. Execution, coverage, snapshot activation, derived readiness and review availability are
separate facts. Successful execution does not mean every historical record was recoverable.
Provider/HMAC secrets and hostile Git overrides are stripped from local Git subprocesses; this
reduces credential exposure but is not a process sandbox.

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

Schema 6 fences the revision-aware read protocol: old schema-5 binaries refuse the upgraded
ledger. Stop all CLI writers and MCP/TUI readers, preserve the coherent database and matching
HMAC key, upgrade through the CLI, then restart MCP and discard old cursors. No identity key or
human decision is replaced. Restoring/replacing a database also requires stopped writers/readers
and an MCP restart; never silently roll back decisions made since a backup.

Every MCP call uses a short SQLite read snapshot and returns `view_token`. Pass it as
`expected_view_token` on related reads. `evidence_changed` means restart the investigation before
combining results. Configuration changes, visible ledger writes and server restarts invalidate
the view. Tokens are consistency markers, not authentication or evidence-readiness approval.

Candidate/search cursors now start with `wtc1:` and bind the app, collection, filters, view and
continuation position. Legacy `offset:` cursors return `cursor_upgrade_required`. Follow a
non-null continuation even after a short or empty page. Each call returns at most 20 records and
processes at most 200 raw matching rows; authority/decision context and SQLite search costs can
still grow with history. Unrelated evidence bodies are not hydrated for candidate-page summaries.

Small `build_phase4_packet` responses retain full Phase 4 v2 detail. Oversized packets compact
without dropping any of the 30 question IDs/statuses or support/contradiction presence. Request
detail with either `section` (the canonical section name) or `question_id`, then follow
`detail_cursor`, always carrying `expected_view_token`. Detail entries include stable citation
IDs and ordered answer/limitation chunks. An explicit size error never advances continuation.
The TUI keeps its existing navigation and cursor contract; no seventh tool is introduced.

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
