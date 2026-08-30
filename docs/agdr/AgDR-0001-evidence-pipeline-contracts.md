# AgDR-0001: Evidence-Pipeline Contracts

> In the context of a private local contribution-reconstruction ledger, facing source-shape drift and consequential false-attribution risk, WorkTrace will preserve source-qualified observations and centralize claim projections, use user-root discovery with bounded context, record exact-object availability events, and restrict structural Jira grouping to true subtasks. This achieves defensible evidence packets while accepting that previously uncollected evidence remains unknown until reimport.

## Context

The adapters already preserve provider-specific facts, but packet consumers interpret a different role vocabulary; release logic reads incompatible provider shapes; remote discovery is overbroad; availability lacks a provenance-bearing lifecycle; and broad Jira hierarchy can collapse unrelated work. Existing ledgers must remain auditable and human decisions must survive migration and rebuild.

## Options considered

| Option | Advantages | Costs and risks |
|---|---|---|
| Rewrite imported roles and payloads into one generic schema | Simple consumers | Destroys source meaning, complicates migration, and conflates provider roles |
| Preserve source roles and centralize claim projections | Immutable provenance, explicit authority, legacy compatibility | Requires one carefully tested classifier |
| Continue project-wide discovery and filter packets later | Simple adapters | Persists unrelated employee history and can still miss historical participation |
| Discover configured-user roots then bounded context | Data minimization and better relevance | Provider selection bias must be reported |
| Infer unavailability from snapshot absence | Cheap reconciliation | User-scoped/date-scoped searches can falsely erase visible objects |
| Append exact-object availability events | Defensible transitions and recovery | Requires one forward migration and explicit hydration errors |
| Auto-group all Jira hierarchy | High recall | Epic siblings become false contribution groups |
| Auto-group only confirmed subtasks | Safe structural signal | Epic membership needs human confirmation |

## Decision

Chosen:

1. Keep raw source-qualified roles and interpret them through one shared claim classifier. Canonical roles are `git_author`, `git_coauthor`, `git_committer`, `git_reviewer`, `git_tag_author`; `mr_author`, `mr_assignee`, `mr_reviewer`, `mr_merger`; `gitlab_commit_author`, `gitlab_commit_committer`, `gitlab_commit_coauthor`, `gitlab_commit_reviewer`; `gitlab_discussion_author`, `gitlab_deployer`, `gitlab_release_author`; and `jira_assignee`, `jira_reporter`, `jira_creator`, `jira_comment_author`, `jira_changelog_author`. `mr_author` records MR submission/context only: changed paths can describe scope but never prove implementation authorship. Annotated tag authorship supports release association only. Only commit author/co-author evidence or an explicit implementation attestation supports implementation. Legacy aliases are accepted only when source and object kind disambiguate them.
2. Run local Git first, GitLab second, and Jira third so commit SHAs and exact Jira keys constrain remote discovery. Rebuild derived references and candidates once after source runs.
3. Preserve seven independent release rungs and normalize provider shapes at the packet boundary, while future observations also carry convenient scalar fields. The approved v0.1 scope imports successful deployments only for configured production environments; failed/canceled deployment history is explicitly unknown rather than inferred absent.
4. Add append-only availability events and a current projection. Events have stable identity, source-object/run foreign keys, visible/unavailable state, reason, observation time, and per-run uniqueness. Only an exact stable-object 404 may append unavailable; 401/403, nested-resource 404, and search absence may not. Only completing the run commits its events to the projection.
5. Emit directed Jira hierarchy references. Only provider-confirmed subtasks are structural; epic/story/custom/unknown parentage is context-only.
6. Retain Git numstat and remote-tracking refs because the v0.1 contract requires factual reconstruction, while explicitly prohibiting productivity aggregation and remote-freshness claims.
7. Use five bounded jittered retry attempts because that is the authorized v0.1 contract.
8. Add `coverage` as a dev-only dependency so ApexYard's greater-than-80-percent gate is executable without changing runtime architecture.

## Consequences

- Existing immutable observations remain valid and become readable through compatibility projection.
- Missing paths, fix versions, discovery roots, and remote refs remain unknown until reimport.
- Legacy overbroad runs remain historical but are not current candidate, confirmed-member packet, search, excerpt, MCP, or export evidence after the selection-policy change. Unsupported confirmed members become explicit gaps. Removal requires an app/source/run-scoped, user-confirmed CLI purge.
- Migration `003` seeds pre-existing objects with deterministic visible baseline events tied to their last-seen runs, adds projected reason/time, and preserves stable source, observation, evidence, candidate, and human-decision IDs. Failed/partial runs cannot update the projection; reappearance appends a visible event.
- Default doctor remains offline; authenticated checks require `doctor --live`.
- No additional runtime dependency beyond the package baseline recorded in AgDR-0003, and no
  external service, is introduced by this remediation.

## Reversal triggers

Revisit only if provider APIs cannot support bounded user-root discovery, if exact-object availability lookups prove operationally infeasible, or if real fixtures show confirmed subtask grouping still produces false candidates. A reversal must preserve the ledger and add a new AgDR rather than rewriting this record.

## Verification

- Adapter-shaped tests for every role and provider payload.
- Ten end-to-end golden cases through adapters, ledger, rebuilds, packets, gaps, and MCP.
- Populated migration, rollback, interruption, reappearance, and decision-retention tests.
- Redaction, scope, URL sanitization, MCP read-only/output-bound, and forged-attribution tests.
- Pytest, branch coverage above 80%, Ruff, mypy, package build, CLI smoke, and MCP handshake.

## Artifacts

- `docs/technical-designs/worktrace-v0.1-evidence-pipeline-remediation.md`
- `docs/agdr/AgDR-0003-local-python-runtime-and-evidence-authority.md`
- `migrations/003_availability_events.sql`
