# AgDR-0003: Local Python runtime and evidence authority

## Status

Accepted for WorkTrace v0.1 on 2026-08-27.

## Context

WorkTrace v0.1 is a new, private, single-user evidence ledger. The public pull request adds the
entire executable package against an empty base, so its language, build backend, command-line,
provider client, MCP transport, and persistence choices are material decisions. The same boundary
must prevent provider failures, legacy discovery, bounded selection, and human-rejected candidates
from becoming stronger claims than the observations support.

The product must install as one Python package, work without a database or application service,
keep credentials out of MCP, and preserve append-only provenance in a user-controlled local data
directory. Provider payloads and identifiers are untrusted. Live Jira, GitLab, and proprietary
repository behavior is outside the public-fixture verification boundary.

## Options considered

| Concern | Chosen option | Alternatives rejected and trade-offs |
|---|---|---|
| Language and runtime | Python 3.12 or newer | Older Python would require compatibility code and lose the selected standard-library typing and SQLite APIs; another runtime would add a second toolchain without product benefit |
| Build backend | Hatchling, bounded to major version 1 | Setuptools is mature but needs more packaging configuration for this small `src` layout; Flit is compact but offers less explicit control over the packaged migration files |
| Command line | Typer, bounded below major version 1 | `argparse` avoids a dependency but makes the grouped, typed CLI and consistent validation more verbose; a web UI or daemon would expand the trust and deployment boundary |
| Provider HTTP | httpx, bounded below major version 1 | `urllib` avoids a dependency but makes deterministic transport testing, streaming limits, and client configuration harder; `requests` would not improve the chosen bounded synchronous design |
| Agent protocol | The official Python MCP package, major version 2 only | A hand-written JSON-RPC server would duplicate protocol and schema behavior; a network MCP service would add authentication and remote exposure not needed for a local stdio tool |
| Persistence | Python standard-library SQLite | A server database adds operations, credentials, and network state; an ORM hides SQL authority and migration behavior without removing the need to design it |

## Decision

1. Ship one `requires-python = ">=3.12"` package built with Hatchling. Include SQL migrations in
   the wheel and source distribution and verify both through an isolated Python 3.12 install.
2. Use Typer for the local CLI. Commands remain explicit, bounded, and non-interactive unless an
   operation already requires confirmation. WorkTrace does not introduce a web service or daemon.
3. Use one synchronous httpx client boundary for Jira and GitLab. Retry only the approved bounded
   transient cases. Read responses as streams, reject a trustworthy oversized `Content-Length`
   before reading, stop decoded/chunked reads at the configured byte cap, and close every response.
4. Use the official MCP v2 package only for six bounded, read-only stdio tools. MCP opens the
   ledger read-only, accepts no source credentials, follows no source URLs, and applies input,
   excerpt, result-count, and serialized-output limits.
5. Use SQLite through the Python standard library with explicit SQL, additive numbered migrations,
   foreign keys, WAL, immutable observations, and append-only human decisions. No database server,
   ORM, background synchronizer, or telemetry service is introduced.
6. Treat run success, evidence authority, and evidence completeness as separate facts. Jira and
   GitLab observations are eligible as current evidence only when a completed run records
   `selection_policy_version >= 2`. Unversioned and version-1 runs remain historical. Failed or
   partial runs are never current or citable.
7. A bounded discovery run that executes successfully may retain `status=complete`, but a cap hit
   records `completeness=selection_biased`, input/selected/dropped counts, the deterministic
   selection policy, and operator-facing limitations. Consumers must report the source incomplete
   for scope and must not turn the retained subset into a complete discovery claim.
8. A provider changed-path overflow marks that observation selection-biased and
   `scope_complete=false`. Retained paths remain inspectable context but cannot support complete
   module, affected-scope, or implementation claims.
9. Redact or deterministically pseudonymize provider-controlled identities and reference values
   before stable-ID construction and persistence. Project candidate state through the canonical
   append-only human-decision projector; ignored candidates are absent from packet and MCP reads,
   and contradictions cannot remain well-supported.

## Consequences

- The runtime has three intentional third-party dependencies plus one bounded build backend. Their
  lockfile and upper major-version bounds make upgrades explicit, but security and compatibility
  updates still require review.
- SQLite keeps installation and recovery local and inspectable, at the cost of supporting only one
  user's local workload rather than a shared concurrent service.
- Streaming limits bound decoded response memory at the adapter boundary. Provider JSON still has
  to fit inside that bound before normalization.
- Selection-biased records can preserve what was actually observed without claiming exhaustive
  coverage. Operators may need a narrower scope or a higher explicitly configured bound to close
  the resulting evidence gap.
- Deterministic pseudonyms preserve repeatable joins without retaining a secret-like provider
  identifier in plaintext; they are not reversible source identifiers.
- The public suite proves synthetic adapter and protocol behavior. It does not claim live provider,
  proprietary repository, or production-environment validation.

## Reversal triggers

Revisit this decision if the local single-user contract changes, Python 3.12 becomes unsupported,
the official MCP v2 line cannot preserve the read-only stdio boundary, SQLite cannot satisfy the
measured local workload, or a provider cannot expose the bounded evidence needed without a service.
Any replacement must migrate the ledger without rewriting observations or human decisions and must
record a new AgDR instead of editing away this history.

## Verification

- Locked dependency validation, strict type checking, formatting, linting, and branch coverage over
  80 percent.
- Source and wheel builds, isolated Python 3.12 wheel installation, CLI version/help smoke, and MCP
  stdio/read-only handshake.
- Regressions for unversioned/failed authority, identifier redaction, reviewer completion,
  correctable aliases, every discovery bound, changed-path overflow, contradiction projection,
  ignored decisions, authorized Git refs, streaming response overflow, and candidate truncation.

## Artifacts

- `pyproject.toml`
- `uv.lock`
- `docs/agdr/AgDR-0001-evidence-pipeline-contracts.md`
- `docs/technical-designs/worktrace-v0.1-evidence-pipeline-remediation.md`
