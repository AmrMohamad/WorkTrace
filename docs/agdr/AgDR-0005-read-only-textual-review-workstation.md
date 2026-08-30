# AgDR-0005: Structurally read-only Textual review workstation

## Status

Proposed for WorkTrace's first human interface on 2026-08-30.

## Context

WorkTrace exposes automation through a JSON-oriented Typer CLI and agent reads through six bounded
MCP tools. A human reviewing years of evidence must currently connect candidates, participation,
delivery states, Phase 4 questions, gaps, and excerpts across separate JSON responses. The approved
PRD requires one keyboard-first terminal journey without weakening the product's existing authority
model: the CLI owns every write and MCP remains SQLite-only and read-only.

A full-screen terminal interface is a new presentation surface for proprietary, provider-derived
text. It also creates risks absent from plain JSON: event-loop blocking, terminal control
sequences, Rich/Textual markup, framework screenshot and clipboard actions, and accidental access
to provider credentials or write services.

## Options considered

| Option | Advantages | Costs and risks |
|---|---|---|
| Continue with CLI and MCP only | No dependency or new presentation surface | Does not satisfy the approved human-review journey |
| Build a web UI or local HTTP service | Familiar layout and browser accessibility tools | Adds a server, browser trust boundary, network surface, and deployment complexity outside the product contract |
| Build a prompt-based REPL | Small dependency and simple flow | Weak support for connected tables, drill-down context, responsive layouts, and headless full-screen interaction tests |
| Build a Textual TUI that invokes CLI/MCP | Reuses visible adapters | Makes JSON/protocol output an internal API, weakens typing and cancellation, and couples human behavior to agent/automation limits |
| Build a Textual TUI over a narrow read-only workspace | Complete terminal workflow, shared canonical read models, testable layouts, no new mutation authority | Adds one dependency and requires explicit terminal/disclosure controls |

## Decision

Chosen: **Textual 8.x over a capability-enforced `ReadOnlyWorkspace`**, because it satisfies the
approved terminal workflow while preserving CLI and MCP authority boundaries.

1. The complete TUI is launched explicitly through `worktrace ui`. It never replaces no-argument
   CLI help and is not exposed before the full application-to-excerpt journey exists.
2. The runtime dependency is bounded to `textual>=8.2.8,<9`, locked with uv, and its TCSS resource
   is explicitly included and verified in the wheel.
3. The TUI constructs only `ReadOnlyWorkspace`. Each operation creates a worker-local SQLite
   `mode=ro` connection, verifies `PRAGMA query_only = ON`, uses a 500-millisecond busy timeout,
   and closes in `finally`.
4. Read-only authority is proven by capabilities, not an impossible import-purity rule. The TUI
   receives no provider adapter, HTTP client, importer, decision writer, migration operation,
   maintenance operation, export, backup, purge, configuration editor, or write connection.
5. The TUI neither invokes the CLI nor calls MCP internally. `PacketBuilder` remains the canonical
   claim projection. `WorkTraceTools`, its six schemas, its response limits, and its `offset:`
   cursor remain unchanged.
6. The TUI gets a separate generation-bound, scan-bounded candidate query. Its cursor contains a
   token derived from app ID, generation timestamp, and generator version plus the last raw
   candidate ID scanned. It does not contain a candidate count and does not require a new index.
7. Initial readiness compares the read-only ledger's `PRAGMA user_version` with the packaged
   supported version. Older ledgers direct the user to CLI initialization; newer ledgers fail as
   incompatible. The TUI never migrates.
8. Provider and Textual control environment variables named in the technical design are removed
   from the process before Textual import. The TUI route cannot obtain Jira/GitLab credentials or
   the email HMAC key and disables environment-driven Textual logging, drivers, input, and
   screenshots.
9. All dynamic configuration, ledger, provider, and stored-error text crosses a presentation-only
   terminal encoder and then one literal-renderable sink. Table/tree/list cells and every other
   renderable surface receive `Text(encoded.text)` rather than a bare string. Dynamic widget
   updates, modal titles and bodies, errors, and notifications use that literal `Text`,
   `markup=False`, or an equivalent explicitly literal API. Encoded dynamic content must never be
   passed as a bare `str` to a markup-capable Textual/Rich API. Provider excerpts render through
   the same boundary in a scrollable literal `Static`. Dynamic text never supplies widget
   identities, CSS selectors, commands, bindings, actions, or palette items.
10. The command palette is a fixed allowlist without Textual's default screenshot command. Only a
    validated WorkTrace stable ID may reach the application clipboard action. `WorkTraceApp` and
    every normal and modal WorkTrace screen set `ALLOW_SELECT = False`; screens remove the inherited
    `ctrl+c`/`super+c -> screen.copy_text` bindings and override `action_copy_text()` as a no-op,
    so selected text cannot reach `App.copy_to_clipboard`. The explicit stable-ID action remains a
    separate validated path. Provider URLs are non-clickable.
11. Every database operation runs in a Textual thread worker. Immutable DTOs return by thread-safe
    messages; widgets update only on the UI thread; monotonic request IDs discard stale results.
12. The journey is keyboard complete at 80x24 and may add non-exclusive side-by-side context at
    120x40. `NO_COLOR`, visible focus, and text/symbol state labels are required. No claim of full
    screen-reader accessibility is made.

## Consequences

- Humans gain a coherent contribution-review workflow without a new write or provider authority.
- The CLI remains the sole mutation boundary, and automation/agent clients keep their current
  contracts.
- The candidate browser cannot reuse the current full-projection candidate list; a new bounded
  read query must land before the TUI.
- Terminal encoding is deliberately separate from persisted normalization and redaction, so search
  and evidence identities remain stable.
- Removing application screenshot/body-copy actions reduces accidental disclosure but cannot stop
  an authorized local user from using operating-system capture, terminal-emulator selection
  outside Textual's application actions, photography, or terminal logging.
- Textual's redraw model is not represented as fully screen-reader accessible. CLI JSON and MCP
  remain the structured alternatives.
- Imports, decisions, attestations, configuration editing, maintenance, persistent preferences,
  database watching, writer locks, backup redesign, and WAL reconciliation remain outside this
  authority decision.

## Reversal triggers

Revisit if Textual 8.x cannot provide the verified keyboard, worker, packaging, 80x24, or terminal
safety behavior; if measured candidate reads cannot remain bounded without a different read model;
or if the product intentionally authorizes a human mutation surface. A mutation-capable TUI
requires a successor AgDR that defines shared write services, audit parity, locking, backup,
recovery, and cancellation; it must not silently broaden this record.

## Verification

- Architecture tests prove query-only connections reject writes and TUI journeys do not invoke
  credentials, providers, sockets, or file writes.
- Candidate tests prove fixed scan bounds, generation invalidation, short-page continuation, and no
  full-table projection.
- Terminal tests cover CSI, OSC, DCS, C0/C1, bidi, lone-surrogate, markup, prompt-injection, and
  expansion-limit cases. Candidate rows and representative tree/list, modal, error, and
  notification surfaces prove Rich/action markup remains visible literal text, produces no
  markup-derived spans or links, and creates no actions, commands, or bindings.
- Textual tests prove the keyboard journey at 80x24 and 120x40, stale-worker rejection, fixed
  commands, stable-ID-only clipboard, and fresh-process `NO_COLOR` behavior. `WorkTraceApp` and
  every screen/modal disable automatic selection, and screens neutralize inherited
  `screen.copy_text`; mouse selection plus `ctrl+c`/`super+c` over evidence leaves the clipboard
  unchanged and never invokes `copy_to_clipboard`, while the validated-ID action copies the exact
  ID once.
- An isolated wheel smoke proves the dependency, entry point, and TCSS resource are installed.
- Existing CLI and six-tool MCP regression suites remain green.

## Artifacts

- `docs/prds/worktrace-read-only-human-tui.md`
- `docs/technical-designs/worktrace-read-only-human-tui.md`
- `docs/threat-model.md`
- `docs/known-limitations.md`
