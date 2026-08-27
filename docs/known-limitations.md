# Known limitations

This document records intentional v0.1 boundaries and validation gaps. It must not be read as proof that every listed capability has been exercised against a live provider.

## Product boundary

- WorkTrace is a private, local, single-user self-reconstruction tool. Multi-user sharing, organizational reporting, performance evaluation, rankings, and productivity or seniority inference are unsupported.
- There is no web UI, cloud backend, daemon, embedded LLM, vector database, embeddings, or fuzzy semantic matching.
- CLI invocations perform imports on demand. v0.1 uses full historical snapshots rather than incremental synchronization.
- Codex history, Slack, email, Firebase/telemetry, Figma, and App Store Connect are not source adapters in v0.1.

## Evidence limitations

- Source systems contain assertions and mutable records, not objective truth. Jira comments can describe an impact but do not independently prove its scope.
- Exact Jira keys, SHAs, MR references, branch matches, and URLs establish typed relationships only. They do not prove implementation or ownership.
- Git author, committer, and co-author data describe Git objects. Squashes, rebases, patches, bot activity, and history rewriting can still leave incomplete context.
- Jira assignment intervals describe recorded assignment, not work performed.
- GitLab MR author/reviewer/merger roles do not establish sole or main feature ownership.
- Human attestations remain explicitly user-supplied. They are reversible but are not independently verified.
- Candidate generation is deterministic and intentionally conservative. Poorly documented work, investigations, release coordination, review-only work, and records without exact links may be missed. Every golden miss must be documented rather than hidden with fuzzy matching.

## Delivery and outcome limitations

- Jira `Done` and GitLab `merged` do not prove release.
- A tag, fix version, release branch, or release note proves release association only.
- A successful deployment proves only that GitLab recorded the configured SHA/ref in the configured environment.
- v0.1 cannot independently prove App Store/TestFlight availability, phased rollout completion, feature-flag enablement, current user availability, or current use.
- Conversion, stability, error-rate, time-saving, customer-impact, and other success claims require manual or future metric evidence. WorkTrace never derives them from activity volume.

## Source and synchronization limitations

- Local Git is only as current as the configured clone. WorkTrace warns about staleness and never fetches automatically.
- Full snapshots can be slow for long histories. Incremental cursors, overlapping update
  windows, and absence-based reconciliation are deliberately postponed. v0.1 records
  unavailability only after an exact stable-object lookup returns unavailable; search absence is
  never treated as deletion.
- Jira comment-only participation can remain undiscovered when no other user-history function or
  exact key selects the issue. Import summaries and run scopes record this known selection bias.
- Remote-tracking Git refs are clone-local observations. They may be stale and are never described
  as the current state of the remote server.
- APIs may omit deleted objects, restricted discussions, inaccessible projects, or very recent updates. Packets show partial, unavailable, and stale states but cannot recover data the source does not expose.
- Permission loss after an earlier import leaves the previous redacted observation available but stale/unavailable; it does not prove current state.
- Duplicate patch detection cannot always establish identity across rebases and squash histories. Explicit MR metadata and human review remain necessary.

## Privacy and security limitations

- Redaction is best effort. It reduces known secret and personal-data patterns but cannot identify every sensitive fact. Review exports before sharing.
- Employee display names may be retained where required to explain participation. External emails are hashed, but a small identity set may still be inferable from context.
- The SQLite database, backups, and exports are sensitive local files. WorkTrace does not provide encryption at rest beyond operating-system facilities.
- MCP source excerpts remain untrusted data even after redaction. Prompt approval for a broader excerpt is a review step, not a guarantee that the content is safe or correct.
- The MCP server's read-only design limits WorkTrace actions; it cannot prevent a separate client/tool from acting on malicious source text. Codex must keep source text as data.

## Search and packet limitations

- v0.1 search uses exact IDs, case-insensitive text, and structured date/actor/module/source filters. It does not use embeddings or semantic similarity.
- Phase 4 drafts are bounded summaries, not final CV or interview prose. Unsupported material answers remain null/unknown.
- The interview-defensibility view is a breakdown of supported, partial, and missing dimensions, not a boolean guarantee.
- Evidence counts are inspection aids within a contribution. They are not comparable measures of value or productivity.

## Validation gaps without live authorization

Offline fixtures and temporary Git repositories can validate parsing, provenance, attribution safeguards, recovery, deterministic rebuilds, redaction, and MCP scope/limits. They do not validate:

- the configured user's current Jira/GitLab identity or permissions;
- Jira Cloud/GitLab response parity for a real tenant;
- actual project pagination, historical completeness, rate limits, or permission loss;
- source UI parity for sampled real objects;
- freshness or branch/tag state of proprietary local clones;
- real production deployments, mobile releases, feature flags, metrics, or current use; or
- whether a historical candidate corresponds to the user's remembered contribution.

Live provider or proprietary repository validation requires explicit configuration, credentials, and authorization. Test success must remain reported separately from that external validation.
