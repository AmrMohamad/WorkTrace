# AgDR-0007: Reliable agent evidence workflow and migration

## Status

Proposed on 2026-09-05T14:26:00Z for contract issue [#25](https://github.com/AmrMohamad/WorkTrace/issues/25)
under [trustworthiness epic #3](https://github.com/AmrMohamad/WorkTrace/issues/3).
Baseline: `08de854d749e59a360f8ec3b7c4865743937520a`.
The user approved the remediation requirements; architecture/security review and merge are pending.
This record describes intended behavior, not implemented or verified remediation.

> In the context of WorkTrace's local evidence ledger, facing incorrect personal attribution,
> lost historical discovery and inconsistent agent traversal, we choose explicit policy repair
> and bounded canonical reads to make investigations explainable, accepting forward migrations,
> deliberate cursor invalidation and one seventh read-only MCP tool.

## Outcome and scope

An authorized engineer imports configured Git/Jira history; an agent discovers a record, follows
citable relationships, distinguishes personal participation and collaborators, proposes a grouping,
and produces the canonical 30-question worksheet after approved corrections. Unsupported answers
stay unknown. Import permission does not approve grouping, ownership, attestations or publication.

Keep Python, SQLite, Typer, existing adapters/linker/candidate projectors, append-only decisions and
Phase 4 v2. The CLI owns writes; MCP and the TUI remain offline readers. No new dependency, provider,
service, graph framework, background synchronization, full-diff storage, embedding or TUI feature.
The separate TUI hardening/discovery backlog [#17](https://github.com/AmrMohamad/WorkTrace/issues/17)
is not folded into this work.

## Evidence and options

Verified source owners at the baseline:

| Owner | Observed behavior requiring correction |
|---|---|
| `importers/orchestrator.py:record_to_object` | Display-name fallback can set self identity. |
| `db/repository.py:_store_actor` | Staged pages overwrite existing actor self flags. |
| `db/migrations.py:backup_database` | Automatic migration backup copies the live database file. |
| `adapters/jira.py:_discovery_queries`, `_subresource_in_window` | Historical discovery uses latest issue update; comments prefer update over creation. |
| `candidates/builder.py:rebuild_candidates` | Historical query matches without surviving self roles need explicit conservative seed eligibility. |
| `cli.py:_discovered_jira_keys`, `import_all` | Whole-app key selection silently caps at 500; credential failure invents a source instance. |
| `mcp_server/tools.py:_builder`, `_cursor_response` | Multi-query reads lack a transaction; cursors are finalized before size limiting. |
| `read_models/candidates.py:candidate_page` | Keyset query rejects a caller-owned transaction. |
| `packets/builder.py:search_evidence`, `mcp_server/limits.py:enforce_total_limit` | Freshness dates drive search; generic compaction can remove unsent results. |
| `cli.py:merge`, `split` | Creation payloads lose material/context distinctions. |

| Option | Benefit | Cost |
|---|---|---|
| Fix future imports only; retain current protocol | Small initial delta | Legacy flags, unexplained relationships and unreliable traversal remain. |
| Reconcile identity and evolve existing canonical reads | Repairs stored and new evidence through current owners | Explicit migrations, policy state, protocol transition and public-path tests. |
| Add a persistent graph/search subsystem | Alternative query strategy | Duplicates authority and adds infrastructure without measured need. |

Choose the second option because it addresses the observed failures within established ownership.

## Decisions

### A: exact identity, repair and the first safe migration

Use the existing email HMAC/key and explicitly configured historical aliases for recorded Git
identity. Names remain display data. Jira uses its verified account ID; optional GitLab uses a
verified numeric user ID and explicit configured/verified email identities for email-only commits.
Author, committer, co-author, reviewer, assignee, MR author and merger remain separate roles.
Recorded Git identity is not cryptographic proof of the person, feature ownership or business impact.

Persist accepted identity policy version and a canonical non-secret configuration fingerprint.
Resolve that policy before a source run. New actors may use it; ordinary page writes must not
reclassify existing actors. Conflicting classification or changed policy requires reconciliation.
Legacy unreconciled self flags cannot support personal implementation claims in any read surface.

CLI `repair-identities APP_ID` defaults to dry run. Validate key continuity against a ledger-bound
verifier, not presence alone; an environment override is subject to the same check. Legacy ledgers
have no verifier, so enrollment requires independently established stored identity/alias evidence
or a trusted matching recovery pair. Zero alias matches cannot establish continuity. If continuity
is unknown, block apply and imports pending an explicitly reviewed recovery/enrollment step; never
mint a key for a populated ledger. Test missing and wrong-but-present keys. Report promotions,
demotions, unresolved identities,
affected apps and confirmed contributions using stable IDs, not raw emails. Apply recomputes the
proposal inside its write transaction and rejects changed scope/policy. Normal CLI source-instance
IDs include app ID, but actor uniqueness does not: inspect actual participation-to-app reachability
and require expanded authorization before an app-scoped repair affects another app.

Atomically commit classification, accepted policy, private audit (versions/fingerprints/counts/
reasons), affected-app revision increments and rebuild/re-review state. Audit is not provider
evidence. Preserve object/observation/participation/decision IDs. Confirmed history and attestations
stay inspectable with re-review warnings after suggestions disappear. Failed imports cannot restore
old flags. Repair alone does not certify a newly selected source snapshot.

Before A's first forward migration, replace the automatic raw-copy backup with SQLite's online
backup API, use a private non-overwriting destination, and test WAL coherence and failure recovery.
Preserve the matching identity key separately. Introduce `apps.read_revision` and its atomic helper
in A because repair already needs durable invalidation; D finishes the all-writer/protocol contract.

### B: historical activity and current source versions

Use an explicit IANA work-period timezone, stored with the run, with half-open UTC timestamps
covering local start-of-first-day through start-of-day-after-last-day. Legacy config retains its
documented UTC interpretation until explicitly changed; never use the machine timezone implicitly.
Record verified provider calendar metadata separately. Select a conservative JQL superset when
calendar boundaries differ or remain uncertain, validate surviving event times, and expose gaps.

Separate reason-labelled historical-updater, historical-assignee, creator-created and exact-key
queries. Do not apply latest-issue-update rejection to historical or exact-key matches. Deduplicate
by stable issue ID, union reasons and normalize once; stage discovery metadata by run when needed
to retain bounded memory and immutable observations. A verified historical selection can seed a
conservative review suggestion without inventing a participation event, precise date or authorship.

Keep comments whose creation OR visible last edit is relevant, preserving distinct actors/times.
Current wording edited later is not historic wording. Retain assignment predecessor/successor
evidence needed to establish overlap, explicitly as context; unknown endpoints remain unknown.
One claim-appropriate activity projection drives labels, candidate/evidence date filters and
`identity.when`. Source update/fetch times remain freshness data. Unfiltered queries include undated
records; date-filtered queries disclose their undated policy and never substitute import dates.

### C: discovery coverage, preflight and activation

Select exact in-scope Jira keys from correctly identified personal roles, canonical confirmed work,
explicit approved keys and relevant collaborator context; retain supporting observation IDs and
selection reasons. Do not follow every Git parent. Process selected keys with existing bounded
chunks, or report discovered/selected/omitted counts and policy when a limit applies.

Version selector policy. Before narrower replacement, show records losing current status and
confirmed contributions using them; preserve prior authority until the full configured-range
replacement satisfies activation. Preserve old observations/decisions and label legacy coverage.
A failed upstream import supplies only explicitly labelled previous authority or a failed dependency.

Missing credentials/origin/identity are session preflight `not_started` results with no invented
source instance. Retain legacy audit rows; exclude only verified synthetic placeholders from real
source health. Execution, coverage, requested scope, snapshot activation, derived readiness and
review availability remain independent. Known source gaps require acknowledged scope for analysis.

Importer and doctor share a local Git environment builder stripping WorkTrace provider/HMAC
variables and hostile Git configuration/path overrides. Keep argument arrays, `shell=False`,
no-prompt/no-optional-lock/no-replace policy and applicable no-external-diff/no-textconv flags;
preserve intentional mailmap behavior. This reduces credential reachability; it is not a sandbox.

### D: coherent reads, revisions and lossless traversal

Every MCP response uses one short query-only read snapshot. Shared paging joins a caller-owned
transaction and only closes one it owns. Release the snapshot and connection before the next call.

Audit every visible writer: app metadata; sessions/preflight/run status and exposed progress;
source pages/actors/objects/availability; decisions/undo; repair; references/candidates. Increment
affected apps' `read_revision` in the same transaction as the mutation, including actual shared-row
reachability. Helper-owned commits cannot split these operations. Production entry-point tests
must prove invalidation; manually incrementing a counter in tests is insufficient.

Read configuration once per call and reload for the next production call. A `view_token` binds app,
revision, read-model version and canonical configuration fingerprint (work interval/timezone,
mappings, identity policy and projection rules; no credentials or HMAC secrets). Add an MCP-process
epoch so restart invalidates old tokens. Database replacement/recovery requires writers and MCP
stopped and an explicit restart, preventing restored revision collisions from continuing a session.
An unaccepted identity/selector policy remains a readiness failure, not merely a new token.

Every app-specific tool accepts optional `expected_view_token`, including reads resolving app from
a contribution/evidence ID. Scope resolution, token comparison and projection share the snapshot;
mismatch returns `evidence_changed` with restart guidance. Tokens confer no access authority.

Candidate AND evidence-search cursors bind version, tool/collection, app, filters, view, generation
where relevant and deterministic position. Reject old `offset:` cursors with an explicit upgrade/
restart error. Reuse keyset candidates with at most 20 returned candidates and 200 scanned rows;
short/empty pages may continue. TUI command shapes and its own cursor contract remain unchanged.

Admit results within the serialized 20,000-character response budget before advancing continuation.
Reserve envelope/warnings/tokens/continuations. Advance past excluded or delivered rows, never an
eligible unsent row dropped for size. Compact one oversized row to stable IDs and essential metadata
or error without skipping it. Final generic shaping cannot delete page items or identifiers.
Protect cursor/token metadata from generic identifier/text redaction. Both context collections share
one total budget; their independent continuation positions follow only delivered/excluded rows.

Compact packets retain all 30 v2 IDs/statuses, support/contradiction presence and limitations;
bounded section/question detail retrieval stays in `build_phase4_packet`. No contradiction status
is sacrificed to fit. Query row bounds are not total-history cost promises: separately measure
authority/decision rows, projections and evidence bytes; remove unrelated body hydration. A
persistent read index requires representative measurements demonstrating need.

### E: explainable context and preserved human decisions

Add exactly one seventh tool, `get_evidence_context(app_id, object_id, relation_cursor=None,
membership_cursor=None, limit=10, expected_view_token=None)`. Object IDs are not observation IDs.
Return scoped authoritative references with direction/type/exact values, supporting observations
and endpoint availability. Relationships and memberships paginate independently without recursion.

Locate potential groups from generated members and exact accepted decision membership fields,
creation snapshots and lineages. Canonical projection then verifies effective membership and
deduplicates lineages. Respect add/remove/context/undo/ignore/merge/split and confirmed history after
seed disappearance. Budget exhaustion returns `complete=false` plus continuation, not false absence.
Expose overlaps without automatically grouping them or promoting textual mentions to structural proof.

CLI merges/splits preserve material/context roles, including undo/rebuild survival. Material in any
approved merge input wins; objects contextual in all inputs stay context. If optional GitLab is used,
an identical full SHA connects observations only with explicit repository/project correspondence;
never infer correspondence merely from app co-membership. Keep source observations and readiness
separate. Optional GitLab work must not invalidate the Git+Jira-only acceptance result.

Update registration, server instructions, product/threat documentation, example allowlist and
exact-count tests atomically in E. This intentionally supersedes only the six-tool and unchanged
MCP-cursor promises of AgDR-0005/0006 and corresponding instruction modules as their slices land.
Before E the runtime remains six tools; before D legacy cursors retain current behavior. Terminal
safety, all read-only boundaries, Phase 4 v2 IDs, response/excerpt limits and other authority rules persist.

## Delivery, upgrade and acceptance

| Slice | Issue | Required outcome and proof |
|---|---|---|
| Contract | [#25](https://github.com/AmrMohamad/WorkTrace/issues/25) | This decision independently reviewed and merged before Build. |
| A | [#26](https://github.com/AmrMohamad/WorkTrace/issues/26) | Coherent backup, identity repair/policy and revision foundation; legacy, shared-scope and failed-import regressions. |
| B | [#27](https://github.com/AmrMohamad/WorkTrace/issues/27) | Historical discovery/eligibility and dates; late edits, no-code work, boundary and downstream filter cases. |
| C | [#28](https://github.com/AmrMohamad/WorkTrace/issues/28) | Coverage/activation/preflight and Git environment; 501+ keys, collaborators, replacement and secret-isolation cases. |
| D | [#29](https://github.com/AmrMohamad/WorkTrace/issues/29) | All-writer revision audit, nested snapshots and lossless traversal; WAL/rollback-journal, stale/filter/scope and oversized-page tests. |
| E | [#30](https://github.com/AmrMohamad/WorkTrace/issues/30) | Seven tools, effective memberships and context-preserving CLI corrections; public search/context/summary and high-fanout cases. |
| F | [#31](https://github.com/AmrMohamad/WorkTrace/issues/31) | Real CLI import/linker/generator discovery BEFORE fixture grouping, actual STDIO, independently installed wheel and consumer runbook. |

Each slice has one PR, tests/format/lint/types/build/coverage, independent review, green CI,
explicit per-PR human merge approval and post-merge QA. Only necessary prerequisite mechanisms
move earlier (backup/revision foundation in A); subsequent slices must not claim final readiness.

Upgrade pins the binary, coherently backs up the database/key pair, applies forward migrations,
previews and approves identity repair, imports the full same scope with corrected policy, rebuilds,
reviews confirmed-history effects, then restarts MCP and discards old cursors. Readers reject newer
unsupported schemas; older schemas direct to CLI migration. Old binaries must not write newer
schemas. Recover using the coherent pair; never delete decisions to hide an incompatibility.
Identity policy, audit/re-review and run selection metadata introduced during A-C must be readable
in each intermediate release; fresh and populated-legacy fixtures cover migration/retry/recovery.
The CLI is the sole writer; MCP/TUI are local consumers. Stop concurrent CLI writers and MCP/TUI
for schema/repair cutover and recovery. Use bounded lock waits and abort on contention or failed
backup before mutation. No zero-downtime migration is promised. Before cutover the old coherent
database/key pair remains recoverable; after new decisions, prefer forward repair because restoring
that older pair would discard intervening history. Never perform that restore implicitly.

F exercises temporary real Git, synthetic Jira HTTP and actual CLI orchestration/canonical services;
human approval simulation occurs only after discovery assertions. The separate installed wheel runs
the initialize/list/search/context/summary/packet STDIO journey outside the source checkout. Preserve
TUI regression checks for shared reads. `docs/agent-workflow.md` is a consumer runbook covering
scope, readiness, investigation, grouping approval and evidence-cited answers, not agent instructions.

Offline tool readiness is distinct from live-provider acceptance or a particular worksheet's
readiness. Private-ledger migrations/repairs, imports and consequential grouping remain separately
authorized operational actions. Technical root cause may require separately authorized bounded
repository evidence; application KPIs do not establish a contribution's causal impact.

## Consequences and reversal

The existing ledger remains authoritative and repairable, while dormant cursors must restart and
new-schema binaries must remain pinned through upgrade. Stop a slice for a lost ID/decision,
unreconciled personal claim, mixed response or skipped eligible record. Revisit only the failing
boundary if measured limits or provider semantics invalidate it; do not add speculative services.

## Sources

- User-adopted reliable agent workflow specification and issue #25 acceptance criteria.
- [AgDR-0004](AgDR-0004-snapshot-and-mr-attribution-safety.md), [AgDR-0005](AgDR-0005-read-only-textual-review-workstation.md), [AgDR-0006](AgDR-0006-phase4-v2-packet-compatibility.md).
- [Atlassian JQL functions](https://support.atlassian.com/jira-software-cloud/docs/jql-functions/): historical activity selection is day-granular; not a historical-text archive.
- [Jira search](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/) and [comments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/): provider query and current comment metadata contracts.
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html) and [Python sqlite3](https://docs.python.org/3.12/library/sqlite3.html): explicit read lifetime and coherent backup API.
