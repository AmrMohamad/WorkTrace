# WorkTrace product contract

## Purpose

WorkTrace is a private, local, single-user tool that helps an engineer reconstruct their own contribution history from Jira, GitLab, and local Git repositories they are authorized to access.

It collects source observations, connects related records, suggests contribution candidates, records reversible human decisions, and gives Codex structured evidence for answering career-history questions.

WorkTrace does not measure employee productivity, compare engineers, infer seniority, determine promotions, or automatically claim ownership, business impact, release status, or measurable success.

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
- one local SQLite evidence ledger;
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
- Do not persist credentials, authorization headers, complete diffs, or attachments.

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
