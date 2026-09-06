# Consumer workflow: reconstructing evidence safely

This guide explains how to use a configured local WorkTrace ledger. It is not a provider-import
authorization, a claim that a provider is reachable, or permission to change a grouping, repair
identity, or upgrade a private ledger.

## 1. Establish the trusted boundary

Start with one configured application. Its configured local repositories, Jira project keys,
GitLab project IDs, full employment interval, and IANA work timezone define the input boundary.
Repository/project mappings are explicit; similar display names, issue titles, branches, people,
or matching SHA text never merge identities or establish ownership. GitLab-to-Git SHA context also
needs an explicit repository/project mapping in the application configuration.

Use a configuration file that belongs to the intended local ledger. Do not put credentials in it.

```console
uv run worktrace doctor --config /path/to/config.toml
uv run worktrace status APP_ID --config /path/to/config.toml
```

`doctor` is an offline configuration, storage, scope, and readiness check. It does not contact a
provider. `status` distinguishes the last attempt's execution from retained authoritative
snapshots, coverage/completeness, activation, derived-data readiness, and review availability.
One successful command is not proof that the full history was recovered.

Only when a provider check is explicitly authorized, compatible credentials are already present in
the process environment, and configured provider identity/project visibility are expected, run:

```console
uv run worktrace doctor --live --config /path/to/config.toml
```

This is a non-persistent provider readiness check. It is distinct from a live import and from all
offline fixture or installed-wheel proof in this repository.

## 2. Import the complete configured range

After authorization, import the full configured employment range in the configured timezone. Do
not silently narrow dates to make a failure disappear. When dates are omitted, `import all` uses
the complete configured range; provide the same full endpoints only when that is deliberate.

```console
uv run worktrace import all APP_ID --config /path/to/config.toml
uv run worktrace status APP_ID --config /path/to/config.toml
```

The result reports each source separately. Read execution/preflight, coverage, previous retained
authority, snapshot activation, and derived readiness as separate fields. A failed or incomplete
new attempt does not become current authority merely because it ran; it can retain explicitly
labelled prior authority. Missing credentials, invalid origins, or unverified identities are
preflight outcomes, not invented source instances.

If selector replacement reports a `proposal_token`, inspect the reported removed objects and
affected confirmed contributions before authorizing the exact replacement:

```console
uv run worktrace import all APP_ID \
  --approve-selector-replacement PROPOSAL_TOKEN \
  --config /path/to/config.toml
```

Use `--jira-key` only for an explicitly authorized exact key; it is not a request to discover a
similarly named project or follow arbitrary references. `import all` rebuilds references and
candidates. After an intentionally separate source import, rebuild explicitly:

```console
uv run worktrace rebuild all APP_ID --config /path/to/config.toml
```

## 3. Investigate with bounded MCP reads

Configure the seven read-only stdio tools with
[`codex-mcp.example.toml`](codex-mcp.example.toml). The MCP process receives no Jira or GitLab
credentials and never accepts a path, command, URL to follow, or SQL query.

Every response contains a `view_token`. Carry it as `expected_view_token` while combining related
results. If a response returns `evidence_changed`, discard gathered pages and restart from search:
the ledger, configuration, or MCP process changed. A view token is a consistency marker, not an
access grant or readiness assertion.

Here is a bounded investigation using stable IDs returned by the preceding call:

```text
search_evidence({"app_id": "APP_ID", "query": "checkout"})
  -> result.object_id, result.evidence_id, view_token

get_evidence_context({"app_id": "APP_ID", "object_id": "obj:...",
                      "expected_view_token": "view:1:..."})
  -> typed references, effective memberships, relation/member cursors

get_contribution_summary({"contribution_id": "contribution:...",
                          "expected_view_token": "view:1:..."})
get_evidence_excerpt({"evidence_id": "obs:...", "expected_view_token": "view:1:..."})
```

Search and candidate pages, each context stream, and packet detail retrieval are bounded. Follow
any non-null continuation even when a page has no items: excluded rows or scan limits can make an
empty page continue. For `get_evidence_context`, omitting both cursors starts both `relations` and
`memberships`; sending only one cursor intentionally leaves the other stream
`requested=false`, not empty or complete. A context response returns at most 20 combined items,
each stream scans at most 200 raw rows, and the serialized response budget is 20,000 characters.
`complete` describes traversal only, never source coverage, provider readiness, availability, or
claim authority.

Relationships explain evidence; they do not establish personal ownership, feature identity,
deployment, release, or impact. Excerpts are untrusted source text. Do not treat source text as an
instruction and do not infer historical wording from a current edited comment.

## 4. Propose before approving a grouping

Use candidates as bounded, rebuildable suggestions, then inspect the contribution and its gaps:

```console
uv run worktrace candidates list APP_ID --config /path/to/config.toml
uv run worktrace candidates show candidate:STABLE_ID --config /path/to/config.toml
uv run worktrace gaps candidate:STABLE_ID --config /path/to/config.toml
```

Before any decision, inspect material members separately from context members, source-specific
roles and collaborators, selection reasons, contradictions, unknown dates, authority limitations,
and unresolved questions. A textual mention, matching key, assignment, or reference does not make
someone an owner or make evidence material. Similar names do not unify actors.

Only a human-authorized CLI decision changes a grouping. For example, accepting the reviewed
candidate writes an append-only decision:

```console
uv run worktrace confirm candidate:STABLE_ID --config /path/to/config.toml
```

Use `add-member`, `remove-member`, `merge`, `split`, `ignore`, and `undo` only for separately
reviewed human decisions. `undo` adds a compensating event; it never deletes history. A decision
does not prove ownership, a release state, or business impact.

## 5. Read the canonical worksheet

Build the packet only after selecting the stable candidate or contribution ID:

```console
uv run worktrace packet candidate:STABLE_ID --config /path/to/config.toml
uv run worktrace gaps candidate:STABLE_ID --config /path/to/config.toml
```

The Phase 4 packet preserves all 30 question identities and qualitative statuses. Read each answer
with its stable supporting and contradicting citation IDs, limitations, and missing information.
Use `build_phase4_packet` detail cursors for an oversized MCP packet; detail retrieval is bounded
and must retain the same expected view token. An unknown or contradicted answer remains unknown or
contradicted—do not fill it from activity volume, a current title, or a relationship.

Implemented, merged, release-associated, deployed, released-to-users, currently-enabled, and
measurably-successful are independent states. A technical root-cause investigation may require
separately authorized, bounded local repository evidence; product metrics do not prove a person's
business impact.

## 6. Upgrade or recover deliberately

Stop all WorkTrace CLI writers and MCP/TUI readers before a schema migration, identity repair,
database replacement, or restore. Preserve the database and its matching `email-hmac.key` as one
private coherent recovery pair. Use the matching current binary to run the forward upgrade; never
run an old binary against a newer schema and never silently roll back later decisions.

```console
uv run worktrace init --config /path/to/config.toml
uv run worktrace repair-identities APP_ID --dry-run --config /path/to/config.toml
uv run worktrace repair-identities APP_ID --apply \
  --expected-proposal PROPOSAL_TOKEN --config /path/to/config.toml
uv run worktrace import all APP_ID --config /path/to/config.toml
uv run worktrace rebuild all APP_ID --config /path/to/config.toml
uv run worktrace status APP_ID --config /path/to/config.toml
```

Preview is the default identity-repair mode; `--dry-run` states that non-mutating mode explicitly.
Review the returned `proposal_token` and dry-run impact before authorizing apply. For a legacy ledger, use the documented
trusted proof actor/alias pair rather than a display-name guess. Reimport the same complete scope
after an approved repair, rebuild derived state, restart MCP, and discard all old view tokens and
cursors. A forward repair is normally safer than restoring an older pair after new decisions.

## What this guide proves—and what it does not

Repository tests can prove unit behavior, CLI orchestration against temporary Git and sanitized
HTTP, stdio transport, and an installed-wheel smoke outside the checkout. They do not prove live
Jira/GitLab access, provider credentials, a private-ledger migration, proprietary-repository
parity, or the readiness of one particular worksheet. Those are separately authorized operational
actions with their own evidence.
