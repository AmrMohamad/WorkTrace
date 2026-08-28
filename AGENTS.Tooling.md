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
