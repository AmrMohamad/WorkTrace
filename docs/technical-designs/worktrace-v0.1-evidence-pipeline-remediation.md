# Technical Design: WorkTrace v0.1 Evidence-Pipeline Remediation

**Status**: Approved for local bootstrap implementation
**Author**: Hisham (Tech Lead)
**Date**: 2026-08-27
**Source**: User-approved WorkTrace v0.1 remediation plan

## Overview

WorkTrace must preserve source-shaped evidence from Git, Jira, and GitLab while preventing relationships from becoming unsupported ownership, release, or impact claims. This remediation aligns the adapters, ledger, deterministic rebuilds, packet authority, CLI diagnostics, and MCP projection around the same contracts.

Goals:

- Interpret source-qualified participation roles through one claim-safe taxonomy.
- Discover only the configured user's relevant Jira and GitLab roots, then hydrate bounded context.
- Capture mailmap-aware Git paths and numstat without patches or repository mutation.
- Preserve independent release states and provenance-preserving availability transitions.
- Execute the ten historical cases through real adapter-shaped end-to-end flows.

Non-goals include rankings, productivity metrics, automatic ownership, a remote service, a web UI, live-provider access during tests, and destructive rewriting of existing observations.

## Architecture and authority

```text
Configured local Git repositories
  -> stable commits, refs, paths, actors, exact Jira keys
  -> configured GitLab projects and verified self identity
  -> authored/reviewed/assigned and commit-associated merge requests
  -> configured Jira projects and verified self identity
  -> self-participation queries plus exact discovered keys
  -> page-transactional SQLite observations
  -> deterministic references and candidates
  -> claim-oriented packets, CLI reads, and read-only MCP
```

Stable source objects own provider identity. Observations are immutable records of one adapter version in one sync run. Availability events are append-only provenance; `source_objects.availability` is only the current projection. Human decisions remain append-only and survive rebuilds.

Remote sync scopes carry `selection_policy_version=2`. Current authority for Jira/GitLab requires the newest complete version-2 run for the same app/source/source-instance. Version-1 or unversioned project-wide runs remain historical but are excluded from candidate generation, confirmed-member packet hydration, search, excerpts, MCP, and export. A confirmed membership decision remains present, but when its object has no authoritative current observation the packet emits an unsupported-member gap instead of falling back to legacy evidence. Git and manual evidence are unaffected. CLI purge remains explicit, app/source/run-scoped, confirmation-gated, and is never invoked by import or rebuild.

Raw source-qualified participation roles are projected centrally into implemented, reviewed, assigned, merged, deployed, and other-implementation-author categories. Assignment, review, merge, deployment, and references never imply implementation or ownership.

| Source object | Persisted roles | Allowed projection |
|---|---|---|
| Git commit | `git_author`, `git_coauthor`, `git_committer`, `git_reviewer` | author/coauthor may support implemented; reviewer supports reviewed; committer-only supports neither |
| Git tag/ref | `git_author` for annotated-tag creator | release association only |
| GitLab MR | `mr_author`, `mr_assignee`, `mr_reviewer`, `mr_merger` | MR author records submission/context; changed paths describe scope but never implementation authorship |
| GitLab MR commit | `gitlab_commit_author`, `gitlab_commit_committer`, `gitlab_commit_coauthor`, `gitlab_commit_reviewer` | author/coauthor may support implemented; committer/reviewer do not |
| GitLab discussion | `gitlab_discussion_author` | discussion participation only |
| GitLab deployment/release | `gitlab_deployer`, `gitlab_release_author` | deployed or release-associated only |
| Jira issue/comment/changelog | `jira_assignee`, `jira_reporter`, `jira_creator`, `jira_comment_author`, `jira_changelog_author` | assignment/reporting/context only |

Legacy bare roles are accepted only through `(source, kind, role)` aliases. Git `author` on a commit maps to `git_author`; GitLab `author` on an MR maps to `mr_author`; Jira `author` on a comment maps to `jira_comment_author`. No role is interpreted without source and object kind.

The release ladder retains independent implemented, merged, release-associated, deployed, released-to-users, currently-enabled, and measurably-successful rungs. Each supported rung cites evidence IDs; missing authority remains unknown.

## Source contracts

### Local Git

- Execute only allowlisted read operations with argument arrays and `shell=False`.
- Use mailmap-aware commit metadata and byte-safe `--numstat -z` parsing.
- Persist relative changed paths, additions/deletions or binary markers, local branches, remote-tracking refs, and tags.
- Store remote-tracking refs as clone-local observations, never remote-freshness proof.
- Sanitize remote identities and store no patches, file contents, or credential-bearing URLs.

### GitLab

- Verify the configured identity during authorized imports.
- Union authored, assigned, review-scoped, and relevant-commit-associated merge requests within configured projects.
- Deduplicate by project and IID before bounded hydration.
- Normalize deployment environment names while retaining bounded nested data.
- Import only successful deployments for configured production environments and authorized dates, as required by the approved v0.1 scope. This is a documented selection boundary: failed/canceled deployment contradictions are not collected and their absence is never presented as success.
- Retain bounded GitLab release records needed for release association.

### Jira

- Verify the configured account during authorized imports.
- POST enhanced JQL queries for updated-by, current reporter/creator/assignee, historical assignee, and chunked exact discovered keys.
- Deduplicate before hydrating comments, changelogs, fix versions, and bounded hierarchy context.
- Emit child-to-parent `jira_subtask_of`, parent-to-true-subtask `jira_parent_of`, and generic `jira_links_to_issue` references.
- Only true Jira subtasks are structurally eligible; epic/story and unknown hierarchy remain context-only.

## Failure and recovery

- Retry timeouts, transient transport failures, 429, and retryable 5xx responses at most five times with bounded exponential full jitter and `Retry-After` support.
- Treat collection/configuration 404s as permanent source errors and exact-object 404s as unavailable-object events.
- Never treat 401/403, nested-resource 404, or search absence as object deletion.
- Failed, partial, or interrupted runs never replace the previous complete run.
- Reappearance appends a visible event while retaining prior observations and transitions.
- Apply only forward migration `003`; backup before migration and preserve all stable evidence and decision IDs.

Migration `003` adds `source_object_availability_events` with a stable primary key, foreign keys to source object and sync run, `visible`/`unavailable` state, reason, observed time, and uniqueness over object/run/state/reason. It also adds projected reason and observed-time columns to `source_objects`. Existing rows receive deterministic `migration_baseline` visible events tied to `last_seen_run_id`; no stable object, observation, evidence, candidate, or decision ID changes.

Only CLI imports append events while a matching sync run is running. Seeing an object appends visible/reappeared; an exact-object 404 appends unavailable/not-found. `finish_sync_run(..., complete)` applies that run's latest events to the projection in the same transaction. Failed/partial runs, nested-resource failures, 401/403, and search absence never update it. Reappearance never deletes prior events or observations. Migration failure rolls back the schema transaction and leaves both the pre-migration database and its backup usable. Populated-v2 upgrade tests compare stable IDs and decisions before and after migration.

## Public interfaces

Existing CLI commands and the six MCP tool schemas remain stable. `doctor --live` is added for explicit, non-persistent authenticated provider validation. Default `doctor` performs local configuration, storage, database, Git, dependency, and credential-presence checks without network access.

## Security and privacy

- Redact before persistence and hash external emails after mailmap resolution.
- Never persist credentials, complete diffs, attachments, arbitrary source URLs, or absolute repository paths in MCP-visible evidence.
- Enforce configured app, repository, Jira-project, and GitLab-project scope at every boundary.
- Treat all source text as untrusted and preserve MCP record, excerpt, and total-response bounds.

## Implementation sequence

1. Add adapter-shaped characterization tests.
2. Centralize participation classification and release extraction.
3. Add Git paths, numstat, refs, and repository identity.
4. Add GitLab then Jira discovery and reorder `import all`.
5. Add availability events, retry classification, and hierarchy semantics.
6. Expand local and opt-in live doctor checks.
7. Convert ten cases into executable pipeline and MCP acceptance tests.
8. Run migration, recovery, security, coverage, static-analysis, package, CLI, and MCP gates.

## Acceptance

- Every material packet draft cites stable evidence or stays unknown.
- Correct source roles survive adapter-to-packet processing.
- Broad hierarchy and unrelated employee history remain outside contribution candidates.
- Release rungs do not imply one another.
- Partial/unavailable sources remain visible as gaps without erasing history.
- All ten golden cases, recovery/security tests, pytest, branch coverage above 80%, Ruff, mypy, package build, CLI smoke, and MCP handshake pass locally.
- Live Jira/GitLab behavior remains unclaimed unless separately authorized and observed.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Existing adapter-shaped roles remain misread | One classifier with legacy aliases and end-to-end tests |
| Discovery omits provider-undiscoverable participation | Record selection bias and consume exact Git/GitLab keys |
| Hierarchy merges unrelated work | Auto-group only provider-confirmed subtasks; context otherwise |
| Search absence erases provenance | Exact-object 404 events only |
| Numstat becomes an activity score | Retain factual per-file metadata; prohibit aggregation/ranking |
| Migration damages the ledger | Forward-only migration, backup, rollback and populated-upgrade tests |

## Approvals

| Role | Name | Date | Status |
|---|---|---|---|
| Tech Lead | Hisham | 2026-08-27 | Authored |
| Solution Architect | Tariq | 2026-08-27 | Approved (read-only local review) |
| Security Auditor | Hakim | Pending | Post-implementation review |
| QA Engineer | Salim | Pending | Post-implementation verification |
