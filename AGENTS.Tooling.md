# WorkTrace Python tooling module

Load for Python, CLI, SQLite, adapters, subprocesses, configuration, imports, packaging, or tests.

- Runtime is Python 3.12+ with a committed `uv.lock`; use only declared project dependencies.
- The console entry point is `worktrace`; all writes are explicit CLI commands.
- Use standard-library `sqlite3`, `subprocess`, `tomllib`, dataclasses, hashing, JSON, and paths. Do not add an ORM, daemon, web service, or shell wrapper.
- Local Git is read-only: never fetch, pull, checkout, modify refs, stash, or execute source text. Always pass argument arrays with `shell=False` and a configured repository root.
- Remote imports use bounded retries only for 429/5xx/timeouts. Fail 401/403 immediately and preserve previous complete evidence.
- Persist each remote page transactionally. Interrupted/running-stale runs are never current evidence; stable identities make retries idempotent.
- Configuration explicitly maps apps to repositories, Jira keys, and GitLab project IDs. Reject duplicate or out-of-scope mappings.
- Tests use temporary Git repositories and sanitized HTTP fixtures. Cover repeated execution, failure paths, recovery, redaction, path/scope boundaries, CLI exit behavior, and deterministic rebuilds.
- No build/test result proves live Jira/GitLab access or proprietary repository parity unless that exact integration ran.

## Read-only human TUI

- The first `worktrace ui` release is structurally read-only. Construct only `ReadOnlyWorkspace`; do not expose provider clients, HTTP/network access, importers, decisions, migrations, maintenance, configuration editing, exports, backups, purge, or a write-capable SQLite connection.
- Do not call WorkTrace CLI commands as subprocesses, parse CLI JSON as an internal API, or call MCP from the TUI. Keep all seven MCP tools and their response limits; MCP uses versioned, view-bound cursors while TUI cursor shapes remain unchanged.
- Create every TUI SQLite connection inside its Textual worker with URI `mode=ro`, `PRAGMA query_only=ON`, and a 500 ms busy timeout. Close it in `finally`; never retain a connection while the user is idle or move it across threads.
- Reuse the shared generation-bound candidate query with caller-owned read snapshots. Preserve TUI command/cursor shapes and scan bounds. Do not add an index without measured evidence.
- Before importing Textual, scrub the provider credential/HMAC variables and Textual control variables listed in the approved technical design. The TUI never migrates an older database; direct the user to the CLI and reject newer unsupported schemas.
- Pass every dynamic configuration, ledger, provider, and stored-error string through the terminal presentation encoder, then construct a literal `Text(encoded.text)` renderable for dynamic table/tree/list cells and other renderable surfaces. Dynamic widget updates, modal titles and bodies, errors, and notifications must use that `Text`, `markup=False`, or an equivalent explicitly literal API. Never pass encoded dynamic content as a bare string to a markup-capable Textual/Rich API, and never derive widget identities, CSS selectors, commands, actions, or bindings from dynamic text.
- `WorkTraceApp` and every normal and modal WorkTrace screen must set `ALLOW_SELECT = False`; screens remove inherited `ctrl+c`/`super+c -> screen.copy_text` bindings and override `action_copy_text()` as a no-op. Preserve only the separate explicit action that validates and copies one stable WorkTrace ID exactly.
- The command palette is a fixed allowlist without Screenshot. Only validated WorkTrace stable IDs may reach an application clipboard action; do not add evidence-body, packet, title, error, URL, screenshot, log, or export capture actions.
- Keep the complete review journey keyboard-operable at 80x24 and verify the full layout at 120x40. Package TCSS explicitly and test the installed wheel. Do not expose an incomplete TUI shell.
