# WorkTrace evidence and assurance module

Load for provenance, attribution, claims, candidates, decisions, release interpretation, packets, gaps, contradictions, or people-facing output.

- Stable source objects own identity; immutable observations record what one adapter version saw in one sync run, after normalization and redaction.
- Store source instance, source/version timestamps, fetch time, adapter/normalization/redaction versions, payload hash, run completeness, and availability.
- Actors retain source-specific identity. Never merge external people by display name; hash email addresses and map only explicitly configured aliases to self.
- Participations are role-specific and may have effective intervals. Assignment is an observed relationship, not ownership.
- References are typed and exact. Text mentions remain context-only until stronger structural or human evidence exists.
- Candidate groups are deterministic, bounded, rebuildable suggestions. Confirmed membership is captured by append-only decisions and must survive candidate rebuilds.
- Human decisions are immutable, timestamped, attributable to the local user, and reversible through compensating `undo_decision` events.
- Use qualitative claim status, observation type, authority, completeness, consistency, and freshness. Never emit numeric confidence, activity value, or contribution scores.
- Claim authority is claim-specific. Jira `Done` does not prove release; GitLab `merged` does not prove deployment; deployment does not prove mobile user availability or current enablement.
- Participation summaries expose evidence facts and other contributors. Ownership wording remains unresolved until attested.
- Phase 4 packets keep identity, problem/context, action, and result separate. Every material draft cites evidence IDs; otherwise draft nothing and list the missing information.
