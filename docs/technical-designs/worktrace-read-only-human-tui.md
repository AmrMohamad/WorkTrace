# Technical Design: WorkTrace Read-Only Human TUI

**Status**: In Review

**Author**: Hisham (Tech Lead)

**Date**: 2026-08-30

**PRD**: [WorkTrace Read-Only Human TUI](../prds/worktrace-read-only-human-tui.md)

**Tracking**: [#8](https://github.com/AmrMohamad/WorkTrace/issues/8)

## Overview

### Summary

WorkTrace will add `worktrace ui`, a keyboard-first Textual workstation for reviewing one
contribution from application selection through a bounded evidence excerpt. The first release is
structurally read-only: it receives configuration and short-lived query-only SQLite connections,
but no provider, network, write, migration, maintenance, export, backup, purge, or configuration
editing capability.

The executable TUI is delivered only after the Phase 4 v2 packet correction and a separate
generation-bound candidate query. Existing CLI commands and all six MCP tools, including MCP's
opaque `offset:` cursors, remain unchanged.

### Goals

- Complete the approved application-to-evidence review journey at 80x24 and 120x40.
- Keep the packet builder as the canonical claim projection and derive gaps from the same packet.
- Bound candidate-page work without replacing the existing CLI or MCP candidate APIs.
- Make provider and stored text safe for literal terminal presentation without changing persisted
  or searchable evidence.
- Enforce the read-only authority boundary through the capabilities constructed for the TUI.
- Package the Textual dependency and TCSS resource in the installed wheel.

### Non-goals

- TUI imports, decisions, attestations, rebuilds, migrations, exports, backups, purges, or config
  editing.
- Jira, GitLab, local Git, HTTP, socket, background synchronization, database watching, or daemon
  access from the TUI.
- Calling WorkTrace CLI commands as subprocesses, parsing CLI JSON, or calling MCP internally.
- Changing the MCP tool count, schemas, bounds, response envelope, or `offset:` cursor.
- Persistent UI preferences, a generic service framework, or a new materialized candidate index.
- General evidence-body, packet, title, error, URL, or screenshot capture actions.

## Governing decisions

- [AgDR-0005](../agdr/AgDR-0005-read-only-textual-review-workstation.md) selects Textual and the
  capability-enforced, read-only runtime boundary.
- [AgDR-0006](../agdr/AgDR-0006-phase4-v2-packet-compatibility.md) defines the deliberate Phase 4
  v2 packet correction that must merge before the TUI.
- The approved PRD owns the user journey and interaction requirements. This document owns the
  implementation boundaries and delivery sequence.

## Architecture

### Components and authority

```mermaid
flowchart LR
    CLI[Typer CLI\nall mutations and existing reads]
    MCP[MCP adapter\nsix bounded read tools]
    TUI[Textual TUI\nhuman read-only review]
    WORKSPACE[ReadOnlyWorkspace]
    PAGE[TUI candidate page query]
    PACKET[PacketBuilder]
    DB[(SQLite ledger)]

    CLI --> DB
    MCP --> PACKET
    TUI --> WORKSPACE
    WORKSPACE --> PAGE
    WORKSPACE --> PACKET
    WORKSPACE --> DB
    PAGE --> DB
    PACKET --> DB
```

`ReadOnlyWorkspace` is a narrow composition root, not a general application-service layer. It is
the only object the TUI constructs for WorkTrace data access. It may use pure configuration and
read-model helpers, but it receives no repository writer, adapter, HTTP client, importer, decision
writer, migration operation, or maintenance operation.

Import purity is not the enforcement mechanism. Existing modules have transitive imports and a
broad refactor solely to make an import graph look pure would add risk without removing a
capability. Tests instead prove the objects constructed by the TUI cannot write the ledger, obtain
provider credentials, open a socket, or create files.

### Public entry point

The Typer adapter adds only:

```console
worktrace ui
worktrace ui --app APP_ID
worktrace ui --app APP_ID --candidate CANDIDATE_ID
```

Rules:

- `--candidate` requires `--app`.
- The candidate must resolve inside that application.
- Both standard input and standard output must be TTYs. Otherwise the command exits 2 with stable
  CLI guidance before importing Textual.
- Existing no-argument help and JSON command output do not change.
- Missing or invalid configuration and a missing database produce CLI instructions; the TUI does
  not create or repair either.

Before importing `worktrace.tui` or Textual, the command removes these variables from the current
process environment:

```text
WORKTRACE_JIRA_BASE_URL
WORKTRACE_JIRA_EMAIL
WORKTRACE_JIRA_API_TOKEN
WORKTRACE_GITLAB_BASE_URL
WORKTRACE_GITLAB_TOKEN
WORKTRACE_EMAIL_HMAC_KEY

TEXTUAL
TEXTUAL_DEBUG
TEXTUAL_DRIVER
TEXTUAL_LOG
TEXTUAL_DEVTOOLS_HOST
TEXTUAL_DEVTOOLS_PORT
TEXTUAL_PRESS
TEXTUAL_SCREENSHOT
TEXTUAL_SCREENSHOT_LOCATION
TEXTUAL_SCREENSHOT_FILENAME
```

This keeps provider credentials and Textual's environment-driven logging, alternate drivers,
automatic input, and screenshot behavior outside the TUI route. It is a process-local defense in
depth control, not protection from an authorized user who can inspect their own process or screen.

## Read model

### `ReadOnlyWorkspace`

The bounded read-model PR introduces this interface:

```python
class ReadOnlyWorkspace:
    def applications(self) -> tuple[ApplicationSummary, ...]: ...
    def source_status(self, app_id: str) -> dict[str, object]: ...
    def candidate_page(
        self,
        app_id: str,
        *,
        page_size: int,
        cursor: CandidateCursor | None,
    ) -> CandidatePage: ...
    def contribution_review(self, identifier: str) -> ContributionReview: ...
    def evidence_excerpt(
        self,
        evidence_id: str,
        *,
        max_chars: int,
    ) -> dict[str, object]: ...
```

Each operation opens its SQLite connection inside the Textual worker that invokes it, uses it for
one bounded operation, and closes it in `finally`. No connection crosses threads or remains open
while the user is idle.

`contribution_review()` resolves the contribution once, builds one Phase 4 packet, and derives the
gap response from that exact packet. It does not independently rebuild a summary, packet, and gap
report. `PacketBuilder` remains the canonical claim projection; `WorkTraceTools` remains an MCP
adapter and is not reused by the TUI.

### Database readiness and connection contract

The TUI connection uses SQLite URI `mode=ro`, verifies `PRAGMA query_only = ON`, and sets a
500-millisecond busy timeout. The existing default 10-second timeout remains unchanged for other
callers.

Initial readiness opens one read-only connection and compares `PRAGMA user_version` with the
current packaged migration version without invoking `migrate()`:

- equal: continue;
- older: exit with instructions to run `worktrace init` through the CLI;
- newer: exit with an incompatible-database message; and
- missing or unreadable: report the path and a safe next action.

The TUI never creates the database and never applies a migration. Lock contention is translated to
a sanitized `WorkTrace data is busy` state with Retry and Return actions rather than waiting ten
seconds.

### Source status

The UI renders the existing latest-attempt read model honestly. For each configured source instance
it may show:

```text
status
completeness
completed_at
stale
authoritative_current
error_summary
limitations
```

Every dynamic value is terminal-encoded before display. The UI does not label a timestamp `Last
complete` and does not claim `Previous snapshot retained`; proving a retained authoritative
snapshot requires a separate future query.

### Generation-bound candidate paging

The TUI uses a new query under `worktrace.read_models`, not
`PacketBuilder.list_candidates()` and not the MCP cursor codec:

```python
@dataclass(frozen=True, slots=True)
class CandidateCursor:
    generation_token: str
    after_candidate_id: str


@dataclass(frozen=True, slots=True)
class CandidatePage:
    generation_token: str | None
    items: tuple[CandidateListItem, ...]
    next_cursor: CandidateCursor | None
```

`CandidateListItem` contains only candidate ID, confirmed contribution ID when present, title and
title provenance, contribution type, status, period, source coverage, and high-level participation
indicators. Full packets and gap counts are not built for table rows.

The page operation runs in one short read transaction:

1. Read the active generation's `generated_at` and `generator_version` for the application.
2. Reject mixed active-generation metadata as inconsistent.
3. Hash `app_id`, `generated_at`, and `generator_version` into the generation token. Do not run
   `COUNT(*)` and do not include a count in the cursor.
4. For a supplied cursor, compare its token with the active token. A mismatch raises
   `CandidateGenerationChanged`.
5. Scan only that exact generation ordered by candidate ID using `id > after_candidate_id`.
6. Advance the scan cursor for every raw candidate examined, including ignored or unavailable
   candidates.
7. Project until the visible page is full, the generation ends, or the scan budget is exhausted.

Bounds:

```text
default page size    25
maximum page size    50
batch size           2 x requested page size
scan budget          min(4 x requested page size, 200)
```

An empty application returns no generation token and no cursor. Budget exhaustion may return a
short visible page with a continuation cursor. The cursor points to the last raw candidate scanned,
preventing skipped rows from creating loops. Scan and projection counts are internal test
diagnostics, not public DTO fields.

No index or migration is part of this design. The implementation records the query plan, rows
scanned, projection count, median local time, and p95 local time on a deterministic 3,000-candidate
fixture. A separate performance issue may propose an index only if evidence shows it is necessary.

This query guarantees page-internal consistency and no duplicate raw rows while candidate
generation and decision state remain unchanged. A separate CLI process may change decision
projections between pages; explicit Refresh clears the cursor stack and restarts at page one.

### Phase 4 v2 prerequisite

The Phase 4 v2 PR moves the canonical 30-question inventory to
`worktrace.packets.schema`, adds `schema_version: 2`, and preserves the current packet envelope:

```text
schema_version
contribution
as_of
source_status
evidence_summary
sections
participation
release_ladder
contradictions
defensibility
limitations
```

`as_of` remains the newest evidence timestamp represented by the contribution. Contribution dates
remain `date_from` and `date_to`. Gaps remain a separately derived response and are not duplicated
into the packet. The TUI iterates the returned sections; it does not hard-code a question count or
maintain a second question inventory.

## Terminal presentation and disclosure boundary

### Presentation encoder

`worktrace.tui.terminal_text` is presentation policy, not evidence normalization. It returns the
encoded text, encoded-control count, and terminal-truncation flag. Stored and searchable evidence
does not change.

The encoder:

- preserves ordinary Unicode and newline;
- expands tab to four spaces;
- visibly encodes every other C0 control, ESC, DEL, C1 control, U+2028, U+2029, and lone
  surrogate;
- visibly names bidi controls U+061C, U+200E, U+200F, U+202A-U+202E, and U+2066-U+2069;
- retains legitimate ZWJ and ZWNJ behavior; and
- enforces its output bound with a running output-length counter, including replacement expansion.

Evidence-excerpt truncation and terminal-presentation truncation are separate fields and separate
labels. Dynamic values from configuration, SQLite, providers, stored errors, titles, actor names,
paths, modules, IDs displayed as prose, limitations, and notifications all pass through the
encoder. Static source-controlled UI strings do not.

Dynamic text is never used to construct widget IDs, CSS selectors, bindings, action names, command
names, or command-palette entries. Provider text is rendered with a scrollable
`Static(markup=False)`, never Markdown, Rich markup, or a selectable `TextArea`.

The UI displays an encoded presentation value but retains the original DTO value separately. A
clipboard action validates and copies the original stable ID exactly; it never copies the encoded
label or any adjacent dynamic text.

### Clipboard, URLs, and screenshots

Only a value that passes the WorkTrace stable-ID validator may reach `copy_to_clipboard`. The UI
offers no application action to copy evidence bodies, packets, errors, titles, or provider URLs.
Provider URLs are neither clickable nor opened.

The command palette is an exact static allowlist and does not call Textual's default system-command
provider. It contains only Quit, keyboard help, Switch application, Refresh, and Return to
candidates when contextually valid. There is no screenshot command.

These controls reduce accidental disclosure. They do not prevent operating-system screenshots,
terminal selection, photography, terminal logging, or other actions by the authorized local user.

## TUI behavior

### Surfaces

The executable vertical slice has only four surfaces:

1. **Application Selection**: one configured application opens automatically; multiple
   applications produce a keyboard-operable list.
2. **Candidate Browser**: latest source attempts, one bounded candidate page, a single candidate
   authority notice, and next/previous cursor navigation.
3. **Contribution Review**: Summary, Evidence, Participation, Delivery, and Questions tabs from one
   `ContributionReview` DTO.
4. **Evidence Excerpt**: bounded, literal, scrollable, non-clickable, and labelled
   `UNTRUSTED SOURCE TEXT`.

Summary exposes title authority, stable IDs, current and unsupported members, source coverage, and
contradictions. Evidence groups current and unsupported records by source. Participation preserves
role-specific facts and other contributors without inferring ownership. Delivery renders all seven
independent rungs. Questions renders every returned Phase 4 question once and presents its answer,
support, contradiction, limitation, and gap explanation from the same packet-derived result. The
excerpt modal shows source, kind, evidence ID, observation time, completeness, and separate source
and terminal truncation states.

There is no dashboard-only shell. `worktrace ui` becomes public only in the PR that supplies the
complete journey.

### Layout

At 80x24 the UI uses one primary pane and drill-down navigation. At 120x40 the candidate screen may
add a selected-row preview and contribution evidence may place selection and detail side by side.
The larger layout adds context, never exclusive information or actions.

Below 80x24 the UI offers Continue in compact mode, Recheck, or Quit. Compact mode removes optional
columns and previews but keeps the complete drill-down journey.

### Keyboard contract

```text
?       context help
Ctrl+P  fixed safe command palette
a       switch application
r       refresh and restart pagination
q/Esc   back; quit at root
Ctrl+Q  quit
y       copy selected validated stable ID
j/k     move selection
Enter   open
n/p     next/previous candidate page
1-5     Summary/Evidence/Participation/Delivery/Questions
```

All actions are keyboard accessible, mouse use is optional, focus is visible, and every state uses
text or a symbol plus text rather than color alone. A fresh-process test covers `NO_COLOR`. The TUI
does not claim full screen-reader accessibility; the CLI JSON and MCP responses remain the
structured alternatives.

## Worker and state model

Every SQLite operation runs in a Textual thread worker. The worker constructs the connection,
builds an immutable DTO, posts a thread-safe message, and closes the connection. Widgets are
updated only on the UI thread.

Each request carries a monotonically increasing request ID. Screens discard results whose request
ID is no longer current. Exclusive workers prevent obsolete UI updates but are not described as
cancelling a SQLite statement that has already begun. No network work exists in this release.

UI state remains in memory and contains only the current app, screen, selected stable IDs, current
page, cursor history, request IDs, and compact-mode choice. It does not persist evidence, drafts,
credentials, URLs, or preferences.

## Packaging

The complete TUI PR adds `textual>=8.2.8,<9` and updates `uv.lock`. It explicitly includes
`worktrace/tui/worktrace.tcss` in the wheel through Hatchling configuration and verifies the
resource with `importlib.resources` after isolated installation.

No `textual-dev` dependency or snapshot-test plugin is added in the first release. Behavioral and
DOM assertions use Textual's built-in `run_test()` and `Pilot` support.

## Error handling

User-facing failures state what happened, why the review is limited, what was not changed, and the
next safe action. Dynamic error content is terminal-encoded and bounded. Expected states include:

- non-TTY invocation;
- missing or invalid configuration;
- missing, older, or newer database;
- database busy;
- no applications or no candidate generation;
- candidate generation changed;
- stale worker result;
- unavailable or unsupported-current evidence; and
- evidence and terminal truncation occurring independently.

Unexpected exceptions do not include evidence bodies or credentials in logs or notifications.

## Delivery plan

| Order | Issue | Deliverable | Dependency |
|---|---|---|---|
| 1 | [#9](https://github.com/AmrMohamad/WorkTrace/issues/9) | Phase 4 v2 schema and packet compatibility correction | This design approved |
| 2 | [#10](https://github.com/AmrMohamad/WorkTrace/issues/10) | Generation-bound candidate query and read-only connection/readiness support | #9 merged |
| 3 | [#11](https://github.com/AmrMohamad/WorkTrace/issues/11) | Complete Textual vertical slice, packaging, security boundary, and interaction tests | #9 and #10 merged |

Each issue has one implementation owner and one independently reviewed PR. The TUI is not exposed
in an incomplete shell PR.

## Testing strategy

### Phase 4 contract

- Exactly 30 unique v2 question IDs appear once and in documented order.
- Runtime and documentation use the same inventory; the UI parity test does not derive expected
  and actual values from the same UI constant.
- `action.review` uses self-participations classified as `reviewed`.
- Review-only evidence no longer supports `action.coordination`.
- Every non-null material answer retains stable evidence citations; unknown and unresolved answers
  remain null.
- CLI packet output and MCP packet output expose v2 while command and tool schemas remain stable.
- MCP continues to accept and return its existing opaque `offset:` cursor.

### Candidate query

- Default pages return at most 25 visible items and scan at most 100 raw rows; no allowed request
  scans more than 200.
- Projection count never exceeds the scan budget.
- Cursor advancement uses the last raw ID scanned.
- Stable generations produce no duplicate or omitted raw rows.
- Ignored/unavailable candidates do not loop; short pages continue correctly.
- Empty generations and generation invalidation are explicit.
- The query does not materialize or project the complete candidate table.
- A deterministic 3,000-candidate fixture records query-plan and local timing evidence without a
  fragile CI wall-clock gate.

### Authority and security

- Every connection is worker-local, `mode=ro`, query-only, uses a 500-millisecond busy timeout,
  closes in `finally`, and rejects a real write.
- Provider credential accessors, adapter/client constructors, socket connections, and file writes
  are not reached in TUI journeys.
- Managed database and data-directory contents are unchanged before and after journeys.
- Environment scrubbing occurs before Textual import.
- The command palette contains only approved commands and no screenshot/log/export file appears.
- Only validated stable IDs invoke the clipboard.
- The terminal corpus covers CSI, OSC 8/52, DCS, ST/BEL termination, C0/C1, CR, BS, DEL,
  U+2028/U+2029, bidi controls, lone surrogates, Rich/action markup, prompt-injection text, and
  output-expansion bounds. No dangerous raw control survives.

### Interaction and packaging

- One-app, multi-app, missing-state, deep-link, paging, refresh, generation-change, database-busy,
  contribution-tab, and evidence-excerpt journeys run through `run_test()` and `Pilot`.
- Stale worker results are ignored.
- Keyboard-only journeys pass at 80x24 and 120x40.
- `NO_COLOR` is verified in a fresh process with visible text/symbol states and focus.
- Synthetic fixtures only are used for UI tests.
- The built wheel passes `worktrace ui --help`, non-TTY rejection, module import, and TCSS resource
  checks in an isolated environment.
- Existing CLI and MCP regressions remain green.

Final verification for each code PR remains:

```text
uv run pytest
uv run coverage run -m pytest
uv run coverage report
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv build
isolated wheel smoke
```

Synthetic tests do not establish live Jira, GitLab, proprietary-repository, or real-terminal
accessibility parity.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| TUI gains accidental mutation or network authority | Construct only `ReadOnlyWorkspace`; prove real write, credential, socket, and file attempts are unreachable |
| Candidate page still scales with total history | Generation keyset scan, fixed budget, projection diagnostics, and 3,000-row fixture |
| Rebuild occurs during navigation | Generation token mismatch clears cursor history and restarts page one |
| Provider text changes terminal state or impersonates chrome | Central presentation encoder, `markup=False`, fixed commands/identities, adversarial corpus |
| Screenshot or clipboard action leaks bulk evidence | No screenshot command; stable-ID-only clipboard; explicit residual-risk wording |
| Packet and UI question inventories drift | One schema under `worktrace.packets`; packet-to-UI parity test |
| Thread worker result overwrites newer state | Monotonic request IDs and UI-thread stale-result rejection |
| Installed wheel lacks styles | Explicit TCSS inclusion and isolated resource smoke test |

## Open questions

No implementation-blocking question remains. Any future TUI mutation, import, maintenance,
configuration editing, persistent preference, database watching, writer-lock, backup, or WAL change
requires a successor authority decision and is outside issues #9-#11.

## Approvals

| Role | Name | Date | Status |
|---|---|---|---|
| Tech Lead | Hisham | 2026-08-30 | Author |
| Solution Architect | Tariq | — | Pending independent review |
| Security Auditor | — | — | Pending independent review |
| UI design | — | — | Pending independent review |
