# PRD: Complete Jira Ticket Bundles and Encrypted Vault

**Status**: Proposed design for parent feature [#46](https://github.com/AmrMohamad/WorkTrace/issues/46)

**Author**: Mariam (Product Manager)

**Created**: 2026-09-16

**Last Updated**: 2026-09-16

**Design unit**: [#47 — Specify complete Jira ticket archive and encrypted vault](https://github.com/AmrMohamad/WorkTrace/issues/47)

## Outcome

An authorized local user can collect a decision-complete, offline Jira ticket bundle for every
verified assignment root in the configured work interval. The bundle preserves all accessible
current Jira context, all attachment types as encrypted originals, redacted searchable projections
where supported, and honest per-resource availability. It supports restart, pause, status, and
private explicit attachment export without changing WorkTrace's evidence authority model.

This document supersedes the existing attachment exclusion only for this Jira vault scope. It does
not permit attachments in SQLite, MCP responses, automatic exports, or automatic provider writes.

The model distinguishes the non-secret site identity, collection instance, collection run, and
immutable activated revision. Historical revisions remain queryable. Stable ticket identity is
site plus numeric Jira issue ID; provider attachment ID is distinct from each revision's attachment
object ID. Archive provenance uses site-scoped evidence IDs on a separate rail from app
`source_objects`/observations/references; an optional app association is projection-only and cannot
alter app authority or candidates.

## User and safety boundary

The only user is the local engineer reviewing their own authorized history. Assignment, issue links,
comments, attachments, or ticket status are observations and never establish ownership, authorship,
release, deployment, impact, or productivity. Context tickets are not participation. Inaccessible
objects and restricted resources remain visible as unresolved gaps, not silently omitted.

The CLI owns collection, vault writes, extraction, indexing, resume, backup, restore, and export.
The TUI is query-only. MCP remains exactly the seven existing SQLite-only tools and has no vault
capability, attachment bytes, raw payloads, or new signature.

## Goals

- Select roots from verified Jira assignment overlap with the configured inclusive local date range
  `2024-01-28..2026-09-06` and configured timezone.
- Expand discovery by day, then verify exact assignment overlap using the complete assignment
  changelog; retain `boundary_unknown` when the source cannot establish a boundary.
- Identify a collection by Jira site plus numeric issue ID, independent of application mapping.
- Hydrate the full current accessible root and one-hop direct context across any accessible Jira
  project: parent, true subtasks, and typed issue links.
- Account for every resource individually, including all attachment types and embedded Jira media.
- Encrypt raw structured payloads and attachment originals outside SQLite; store only redacted
  metadata and extracted chunks in SQLite.
- Provide truthful status dimensions, resource-granularity restart, durable pause, bounded network
  and extraction work, and a coherent vault-inclusive backup/restore contract.
- Preserve existing IDs, roles, decisions, app-scoped authority, and unknown/contradicted states.
- Keep a dedicated archive provider/selector/orchestrator seam for #49; do not route archive
  resources through app-scoped source-object ownership.

## Non-goals

- OCR, image understanding, audio/video transcription, archive extraction, or executing/opening
  attachment content automatically.
- Crawling remote links, following arbitrary URLs, following redirects, or importing unconfigured
  repositories/projects as applications.
- Provider deletion, Jira mutation, automatic deletion, automatic restore, background daemon, or
  multi-user vault sharing.
- Numeric confidence, ownership or productivity scoring, or any claim that a collection is a global
  Jira snapshot.

## Functional requirements

| ID | Requirement | Priority | Acceptance signal |
|---|---|---:|---|
| FR-1 | Verify configured date/timezone and assignment roots before collection | Must | Root has selection evidence or `boundary_unknown` |
| FR-2 | Separate site, collection instance, run, revision, ticket, and attachment identities | Must | Historical revisions query; same ticket cannot fork by app mapping |
| FR-3 | Traverse exactly one direct-context hop across accessible projects | Must | Parent/subtask/typed link edges are retained; no second hop |
| FR-4 | Capture all declared current resource families with page/field completeness | Must | Each resource has a terminal state and locator |
| FR-5 | Save every attachment type as an encrypted original or explicit unavailable outcome | Must | No MIME/type allowlist drops an original |
| FR-6 | Extract only bounded supported formats after original encryption | Must | Redacted chunks cite attachment and locator |
| FR-7 | Resume at resource boundaries and pause durably | Must | Incomplete stream restarts; verified resources remain idempotent |
| FR-8 | Recheck issue update and attachment manifest before activation | Must | One retry then `unstable_partial` |
| FR-9 | Expose exact CLI workflow, vault backup/restore, and query-only TUI | Must | Commands and mutually exclusive TUI options match design |
| FR-10 | Back up and restore SQLite/config/HMAC/vault as one coherent epoch | Must | Fresh destination opens only after all bindings verify |

## User workflow

```text
worktrace jira collect APP_ID [--from DATE --to DATE --timezone ZONE]
  -> preflight identity, site, keychain, free space, and configured scope
  -> discover expanded days; verify assignment changelog; show proposed roots
  -> collect current issue/context resources and encrypted attachment originals
  -> recheck versions/manifests; activate only a stable complete revision

worktrace jira status COLLECTION_ID
worktrace jira resume COLLECTION_ID
worktrace jira search COLLECTION_ID QUERY
worktrace jira show COLLECTION_ID ISSUE_ID
worktrace jira attachment-export COLLECTION_ID ATTACHMENT_ID --output PRIVATE_PATH
worktrace jira backup COLLECTION_ID --output NEW_DIRECTORY --yes
worktrace jira restore --input EPOCH_DIRECTORY --destination FRESH_DIRECTORY --yes
worktrace ui --jira-collection COLLECTION_ID
```

`collect` is explicit and write-capable. `resume` may continue only an interrupted/paused collection
with the same scope, vault, and configuration binding. `status`, `search`, and `show` are redacted
reads. `attachment-export` is explicit, private-destination-only, refuses overwrite and refuses to
launch the result. `worktrace ui --jira-collection` displays redacted metadata/extracted chunks
only; it has no vault key, original bytes, provider, network, or export capability. The option is
additive and mutually exclusive with the existing `--app` and `--candidate` options; omitting it
preserves the existing app/candidate TUI workflow.

Both commands require explicit confirmation and refuse overwrite. `worktrace backup` remains
DB-only and warns when Jira vault state exists. Restore accepts only a fresh destination and fails
closed on any epoch, key, manifest, hash, schema, or recovery-envelope mismatch. No automatic
deletion or restore occurs.

Exit codes are stable: `0` for complete or complete-with-unavailable-resources, `2` for paused,
partial, or unstable-partial status requiring action, `1` for failed/preflight/integrity/security
refusal, and `3` for invalid input or incompatible schema/format. An explicit purge may include
vault data only with `--include-jira-vault --yes`; it quiesces jobs, honors manifest references and
backup retention, retires unreferenced keys, and reports logical deletion rather than secure
erasure.

## Status contract

Status reports dimensions independently; no aggregate hides a failing dimension:

`selection`, `enumeration`, `original_availability`, `download_integrity`, `extraction`,
`search_readiness`, and `app_mapping`.

Public collection terminal states are `complete`, `complete_with_unavailable_resources`, `partial`,
`paused`, and `failed`. A collection is `complete` only when all selected resources have verified
terminal outcomes and the final manifest/version recheck is stable. Unsupported extraction does not
make an original unavailable. Missing permission, a revoked/deleted resource, an invalid redirect,
or an integrity failure is explicit and citable.

## Success measures

| Measure | Target | Proof |
|---|---|---|
| Resource accounting | 100% of declared resources receive a terminal state | Fixture contract test |
| Attachment preservation | Every type is encrypted or explicitly unavailable | Vault integrity/recovery tests |
| Attribution safety | No context resource creates participation | Evidence contract tests |
| Restart safety | No duplicate logical resources; incomplete stream starts over | Resume/retry tests |
| Disclosure safety | SQLite/MCP contain no originals or raw payloads | Negative persistence and MCP tests |
| Bounded work | Limits and budgets are enforced and reported | Limit, budget, and subprocess tests |
| User investigation | Redacted search/show works offline; export is explicit/private | CLI/TUI acceptance |

## Constraints and open questions

The first implementation must use the existing Python/SQLite/CLI boundaries. Future foundation work
may add the optional dependency group `PyNaCl`, `keyring`, `pypdf`, and `defusedxml`; this PR adds
none. Live Jira tenant permissions, exact custom-field shapes, embedded-media endpoint availability,
and real attachment volume remain rollout acceptance questions, not reasons to weaken the contract.

## Acceptance map

The implementation and rollout units are deliberately separate:

| Unit | Issue | Scope | Exit evidence |
|---|---:|---|---|
| Foundation | [#48](https://github.com/AmrMohamad/WorkTrace/issues/48) | Additive schema, vault format/key lifecycle, bounded extraction worker, coherent backup/restore | Unit tests, security review, fresh restore |
| Collection | [#49](https://github.com/AmrMohamad/WorkTrace/issues/49) | Selection, complete resources, one-hop context, downloads, resume, status | Sanitized Jira fixtures and restart/limit proof |
| Investigation | [#50](https://github.com/AmrMohamad/WorkTrace/issues/50) | Extraction/indexing, CLI reads/export, query-only TUI, packaging | Search/TUI/export proof and MCP regression |
| Rollout | [#51](https://github.com/AmrMohamad/WorkTrace/issues/51) | Controlled migration, pilot, authorized full collection, independent QA | Fresh backup/restore, live pilot, limitations report |

Build begins only after independent architecture and security approval of this design. Each unit
keeps source inspection, static checks, tests, CLI runtime, TUI runtime, MCP runtime, and live Jira
validation as separate evidence layers.

## Sources

- [Jira Cloud REST API v3 introduction](https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro): expansion, pagination, ADF, and current API version.
- [Issue comments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/), [issue attachments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/), [issue worklogs](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/), and [issue properties](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-properties/): resource and permission contracts.
- [Jira issue linking model](https://developer.atlassian.com/cloud/jira/platform/issue-linking-model/): typed, bidirectional link interpretation.
- [libsodium secretstream](https://libsodium.gitbook.io/doc/secret-key_cryptography/secretstream): authenticated chunks, headers, final tags, and rekey tags.
- [keyring documentation](https://keyring.readthedocs.io/en/stable/): macOS Keychain backend and failure/security considerations.
- [pypdf text extraction](https://github.com/py-pdf/pypdf/blob/main/docs/user/extract-text.md): bounded content-stream concerns and non-OCR limitation.
- [defusedxml security notes](https://github.com/tiran/defusedxml/blob/main/README.md): XML entity expansion and external-resource hazards.
