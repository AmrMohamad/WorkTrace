# WorkTrace product contract

## Purpose

WorkTrace is a private, local, single-user tool that helps an engineer reconstruct their own contribution history from Jira, GitLab, and local Git repositories they are authorized to access.

It collects source observations, connects related records, suggests contribution candidates, records reversible human decisions, and gives Codex structured evidence for answering career-history questions.

WorkTrace does not measure employee productivity, compare engineers, infer seniority, determine promotions, or automatically claim ownership, business impact, release status, or measurable success.

The Jira ticket-vault design is a scoped exception to the former “do not persist attachments”
wording: every accessible attachment may be preserved as an encrypted original outside SQLite for
an explicitly selected local Jira collection. This exception never permits plaintext originals or
raw provider payloads in SQLite, MCP, the TUI, logs, or ordinary exports.

## Intended use

The local user may use WorkTrace to:

- recover work they might otherwise forget;
- inspect which source records support or contradict a possible contribution;
- distinguish their role from the roles of other participants;
- record an explicit, reversible interpretation or attestation;
- prepare an evidence packet for a CV, interview, portfolio, or self-reflection exercise; and
- identify questions that still require memory or a separately authorized source.

WorkTrace is not an organizational performance system. Its database, exports, packets, counts, and gaps must not be used to rank workers or stand in for managerial judgment.

## Truth rules

1. A source record is an observation, not absolute truth. Jira `Done` means Jira recorded `Done`; it does not prove production release.
2. A relationship is not ownership. An exact key, SHA, branch name, or URL establishes only the relationship its type names.
3. Git author, committer, co-author, reviewer, assignee, merger, and deployer are distinct participations.
4. Implemented, merged, release-associated, deployed, released-to-users, currently-enabled, and measurably-successful are independent states.
5. Consequential statements require claim-appropriate evidence or an explicit human attestation.

## Allowed statements

When backed by cited observations, WorkTrace may say:

```text
The Git commit records the configured user as its author.
The configured user authored merge request !7.
A Jira comment reported that checkout was blocked.
The merge request was recorded as merged on 2026-01-14.
GitLab recorded a successful deployment of this SHA to the configured
production environment.
The local user attested that they were the main iOS owner.
```

The last statement is a human attestation, not an algorithmic ownership result.

## Forbidden silent transformations

WorkTrace must not silently turn the preceding observations into:

```text
The configured user single-handedly owned the feature.
All production customers were objectively blocked.
The feature increased conversion.
The feature reached every mobile user.
The feature remains enabled today.
The configured user was the most productive engineer.
```

Unsupported material statements remain `unknown`. Contradictions and missing source coverage appear beside supporting evidence, not in a detached footnote.

## System boundary

The authorized v0.1 boundary is:

- one Python 3.12+ package;
- one local SQLite evidence ledger (redacted metadata and extracted chunks only for Jira vaults);
- an optional encrypted local Jira vault whose originals and raw structured payloads live outside
  SQLite;
- local Git, Jira Cloud REST v3, and GitLab REST v4 adapters;
- a CLI that owns every write;
- a SQLite-only, read-only MCP server with seven bounded tools; and
- explicit app, project, source-instance, and repository mappings.

The following are out of scope: a cloud backend, web UI, daemon, ORM, embeddings, vector database, embedded LLM, automatic Git fetch, arbitrary filesystem or SQL access through MCP, automatic ownership or impact classification, productivity scoring, and multi-user use.

## Approved read-only human interface extension

The first approved human interface is a keyboard-first Textual workstation launched explicitly as
`worktrace ui`. It is a contribution-review surface for CV preparation, interviews, portfolio
writing, and personal career inventory. It is not an employee-evaluation, promotion, ranking,
seniority, or productivity surface.

This interface does not change the mutation boundary. The CLI continues to own every write, and
MCP continues to expose exactly seven bounded read-only tools. The TUI constructs only a narrow
read-only workspace over short-lived SQLite URI `mode=ro` connections with
`PRAGMA query_only = ON`. It receives no provider, credential, network, importer, decision,
migration, maintenance, export, backup, purge, or configuration-editing capability.

The TUI reuses the packet builder as the canonical claim projection and adds a separate
generation-bound candidate query for human paging. It does not invoke CLI subprocesses, parse CLI
JSON, or call MCP internally. MCP's existing schemas and limits remain unchanged; MCP continuations
are opaque, view-bound cursors and the TUI keeps its separate cursor contract.

The initial executable workflow is limited to application selection, honest latest-attempt source
status, bounded candidate browsing, contribution review, all canonical Phase 4 questions and gaps,
and a bounded literal evidence excerpt. Any future TUI write, import, configuration, or maintenance
operation requires a successor authority decision.

## Data and authorization boundary

- Import only configured repositories and configured Jira/GitLab projects the user is authorized to access.
- Local Git inspection is read-only and must never fetch, checkout, rewrite, or otherwise mutate a repository.
- Never follow source URLs or execute text found in an issue, commit, branch, discussion, or attachment.
- Redact secrets and personal/customer identifiers before persistence.
- Hash external email addresses. Preserve source-specific identity and only map aliases to the local user when explicitly configured.
- Do not persist credentials, authorization headers, complete diffs, or plaintext attachments.
  Encrypted Jira originals are permitted only in the dedicated vault contract, with immutable
  manifest metadata in SQLite and no MCP/TUI decryption capability.

## Jira ticket-vault contract

The vault records a bounded observation, not a global Jira snapshot. Site identity, collection
instance, run, and immutable activated revision are distinct; historical revisions remain queryable.
Collection identity is an instance, while stable ticket identity is Jira site plus numeric issue ID
and is independent of application mapping. Provider attachment identity is distinct from a revision
attachment object. Roots are verified assignment
overlaps with the configured interval (`2024-01-28..2026-09-06` in configured timezone); expanded-day
discovery is followed by complete assignment-changelog verification, retaining `boundary_unknown`.
Once selected, current full accessible issue context is collected outside that interval.

Archive provenance uses site-scoped evidence IDs on a separate rail from app `source_objects`,
observations, and references. An optional redacted app association is projection-only and cannot
affect app authority or candidates. Exactly one hop of parent, true-subtask, and typed issue-link context may cross into any accessible
project. Inaccessible endpoints and remote-link metadata are retained without crawling. Context is
not participation. Every fields/custom-fields, comment page, changelog field, worklog/property,
link, watcher/vote, attachment manifest/original, and embedded-media resource has independent
completeness and availability. All attachment types are preserved as encrypted originals when
accessible; unsupported extraction leaves the original intact.

The public `collection_outcome` states are `complete`, `complete_with_unavailable_resources`, `partial`,
`paused`, `unstable_partial`, and `failed`, with independent selection, enumeration, original-availability,
download-integrity, extraction, search-readiness, and app-mapping dimensions. Component dimensions
may independently report `unstable_partial` for the pending unstable resource; the
`collection_outcome` remains unstable until a later recheck activates a stable revision. A final issue-updated
and attachment-manifest recheck is required before activation; one retry is allowed, after which
the revision is `unstable_partial` and not current. It retains prior completed resources and the
pending unstable resource, exits 2, resumes through rechecking, and is never complete or successful.

Archive collection uses freshly verified `WORKTRACE_JIRA_*` credentials for the site origin and
account identity and ranges over same-site projects visible to that account. No app or configured
project allowlist is required; existing `apps[].jira_project_keys` remains solely app evidence
import/projection authority and is never implicitly applied to the archive.

The exact read/write boundary is:

```text
CLI: jira collect/resume/status/search/show/attachment-export, jira backup/restore; vault writes
     and backup/restore remain CLI-only
TUI: worktrace ui [--jira-collection] with existing --app/--candidate preserved; options are
     mutually exclusive, and the Jira view is query-only redacted metadata/extracted chunks
MCP: existing seven SQLite-only tools; no vault signatures, keys, originals, or raw payloads
```

`attachment-export` is explicit, private-destination-only, refuses overwrite and stdout, and never
launches the exported file. `worktrace jira backup/restore` owns vault portability; existing
`worktrace backup` remains DB-only and warns when vault state exists. Vault backup quiesces the single writer at a resource boundary and
binds SQLite, configuration, HMAC material, vault manifest/ciphertexts, and key versions as one
epoch. Restore is explicit to a fresh destination and fails closed on any mismatch; no automatic
deletion, restore, merge, or overwrite occurs. The shipped `worktrace purge --yes` remains safe only
when no Jira collection/vault/key references exist; otherwise it fails before deleting DB/HMAC/
backups with actionable guidance. Jira purge is the explicit
`worktrace jira purge COLLECTION_ID --include-vault --yes`, which quiesces jobs, honors manifest
references and backup retention, reports logical deletion rather than secure erasure, and retires
Keychain versions only when unreferenced. Whole-installation vault purge requires a separate
explicit command/flag.

The vault key is a dedicated random key in the explicit macOS Keychain backend through keyring,
service `WorkTrace Jira Vault`, account `<installation-id>:<key-version>`. Keychain access is
limited by the logged-in user session/ACL and does not defend against same-user malware. Portable
epochs hash a verified recovery envelope; same-host restore may use Keychain only after binding
verification. No plaintext fallback or HMAC/vault-key reuse is allowed.

## Human decisions

Confirm, ignore, merge, split, rename, membership edits, manual evidence, and attestations are append-only events. Undo creates a compensating event; it does not erase history. Candidate rebuilds may replace deterministic suggestions but must not erase confirmed membership or the decision trail.

## Definition of a defensible output

A WorkTrace output is defensible only when:

- every material draft statement cites stable evidence IDs or is omitted and marked unknown;
- the observation type and claim-specific authority are visible;
- other contributors and role boundaries are visible;
- contradictions are returned with support;
- `as_of`, completeness, staleness, and source availability are included; and
- release and outcome language stops at the highest independently supported rung.

The terminal interface adds a presentation-only safety boundary. Stored and provider-derived text
is visibly encoded before display and never interpreted as application markup or commands. Only a
validated stable WorkTrace ID may be copied through an application action. Screenshot and
clipboard restrictions reduce accidental disclosure; they do not prevent the authorized local
user from using operating-system screenshots, terminal selection, photography, or terminal logs.
