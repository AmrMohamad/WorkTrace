# AgDR-0004: Snapshot and MR Attribution Safety

> In the context of a local evidence ledger where a later source run becomes
> authoritative for its source instance, WorkTrace will admit only complete
> configured employment snapshots and will treat merge-request authorship as
> submission context rather than implementation authorship. This prevents a
> plausible-looking ledger from hiding prior history or claiming work authored
> by another contributor.

## Context

The v0.1 authority projection selects the latest complete run per application,
source, and source instance. Accepting arbitrary sub-windows therefore allows a
later partial run to conceal earlier evidence. The previous role projection
also promoted `mr_author` to implementation when changed paths were complete.
An MR and its file list prove a submitted change contains paths, but do not
prove that the submitting actor authored the commits.

## Decision

1. Import commands accept the configured employment range exactly. Omitting
   both dates resolves to that range; supplying one date is invalid. A narrower
   request fails with `unsafe_scope_replacement` before an import session or
   sync run is created.
2. A newly configured employment range may expand historical coverage but may
   not contract past an authoritative run's stored date range. Missing scope
   dates fall back to the parent import session; a non-manual authoritative
   range that remains unknown or malformed blocks replacement.
3. `mr_author` projects to `context`. It remains a self-authored MR candidate
   seed even when paths are missing or truncated.
4. Only Git or GitLab commit author/co-author evidence, or a local human
   implementation attestation, may support an implementation claim. Changed
   paths enrich scope only.
5. Existing observations, source objects, participations, references, and
   decisions remain immutable. Rebuilding candidates changes only derived
   projection rows.

## Consequences

- Yearly or quarterly replacement imports are intentionally unavailable in
  v0.1. A future scoped-snapshot design must add an explicit authority scope
  key before relaxing this rule.
- MR-only evidence remains useful for discovery and coordination questions but
  cannot support the implementation release rung or `action.implemented`.
- Correcting prior GitLab self-classification still requires a same-scope
  reimport once verified provider identity handling is available.

## Upgrade action

After upgrading an existing ledger, rebuild candidate suggestions so newly
eligible self-authored MR seeds are visible without changing immutable source
records or human decisions:

```bash
uv run worktrace rebuild candidates <app-id>
```

Use `uv run worktrace rebuild all <app-id>` when references also need a
deterministic rebuild.

## Verification

- A narrow import fails before creating an import session or sync run.
- A configured range cannot contract beyond a prior authoritative run.
- A self-authored MR with complete or incomplete changed paths stays a
  candidate seed but has only `context` participation.
- A human implementation attestation remains distinct from provider-observed
  implementation evidence.
