# AgDR-0006: Phase 4 v2 packet compatibility correction

## Status

**Accepted.** The Phase 4 v2 correction was implemented and merged in
[PR #14](https://github.com/AmrMohamad/WorkTrace/pull/14) on 2026-08-30.

The six-tool and `offset:` descriptions below are the historical compatibility constraints of that
correction. See **Current MCP supersession** for the installed contract.

## Context

The documented Phase 4 worksheet defines 30 questions: 5 contribution identity, 7 problem/context,
8 action, and 10 result. The runtime inventory currently defines 29, combines coordination and
review, and uses eight IDs that differ from the documentation. The generated packet also does not
emit an explicit schema version even though the documentation describes one.

The read-only TUI must render the canonical packet rather than create a second question inventory.
Leaving the drift unresolved would either make the TUI reproduce a known contract error or require
UI-specific aliases. WorkTrace is a private v0.1 tool and packets are generated read models rather
than persisted records, so the correction can be explicit and atomic.

## Options considered

| Option | Advantages | Costs and risks |
|---|---|---|
| Keep runtime v1 and make the TUI translate it | Avoids CLI/MCP packet change | Creates a second semantic contract and preserves the known 29/30 drift |
| Emit v1 and v2 indefinitely | Compatibility for unknown consumers | Doubles fixtures and projection paths in a private v0.1 tool without a proven need |
| Emit per-packet legacy aliases | Makes old IDs discoverable in each response | Repeats migration metadata in every packet and encourages mixed-ID consumers |
| Make one atomic v2 correction | One canonical schema, simplest long-term model, clean TUI consumption | Deliberately breaks consumers that depend on old question IDs |

## Decision

Chosen: **one deliberate Phase 4 packet v2 correction with no dual-version layer**.

1. Move the canonical inventory to `worktrace.packets.schema` as a tuple of simple frozen
   `Phase4QuestionSpec` values. Do not introduce a new top-level Phase 4 package or section enum.
2. Emit `schema_version: 2` and exactly the documented 30 questions, in documented order:

```text
identity.what
identity.app_flow
identity.when
identity.origin
identity.ownership

problem.what
problem.before
problem.severity
problem.affected
problem.blocked
problem.constraints
problem.ambiguity

action.implemented
action.decisions
action.technology
action.reuse
action.architecture
action.coordination
action.quality
action.review

result.change
result.measurement
result.scope
result.errors_time
result.business
result.release
result.current_use
result.reuse
result.feedback
result.defensibility
```

3. Rename the drifted runtime IDs as follows and document the mapping; do not emit the mapping in
   each packet:

| Runtime v1 ID | Canonical v2 ID |
|---|---|
| `problem.requirement_clarity` | `problem.ambiguity` |
| `action.tools` | `action.technology` |
| `result.changed` | `result.change` |
| `result.measured` | `result.measurement` |
| `result.efficiency` | `result.errors_time` |
| `result.released` | `result.release` |
| `result.reused` | `result.reuse` |
| `result.interview_defensible` | `result.defensibility` |

4. Add `action.review`. Build it from self-participations classified by the centralized
   `reviewed` participation category, including canonical Git reviewer, MR reviewer, and GitLab
   commit reviewer roles. A future explicit human review attestation may also support it.
5. Remove review-only participation from `action.coordination`. Assignment, merge, MR submission
   context, discussion/context, reporting, and claim-appropriate coordination attestations may
   continue to support coordination. A discussion comment does not automatically prove code
   review.
6. Preserve all current materiality, stable citation, contradiction, title-authority,
   participation, ownership, release-ladder, completeness, and unknown-answer rules.
7. Preserve the actual packet envelope:

```text
schema_version
contribution
as_of
source_status
evidence_summary
sections
participation
release_ladder
contradictions
defensibility
limitations
```

   `as_of` is the newest evidence timestamp represented by the contribution. Contribution dates
   remain `date_from` and `date_to`. Gaps remain a separately derived response rather than a packet
   field.
8. Update CLI packet output, the MCP packet response, documentation, fixtures, golden tests, and
   contract tests in one PR. At this correction's baseline, CLI command shapes, all six MCP tool
   schemas, their limits, and MCP's opaque `offset:` cursor did not change.
9. Do not migrate SQLite. No Phase 4 packet or question ID is persisted as ledger state.
   Packet schema version 2 is independent of the existing configuration schema version.

## Consequences

- Phase 4 packet consumers must move to schema version 2 and canonical question IDs atomically.
- The TUI can iterate one returned schema and needs no aliases or question constants of its own.
- Technical review and broader coordination become distinguishable claims with separate evidence.
- Generated packets remain reproducible from the ledger, current decision projection, schema
  version, and authority rules.
- Existing database identities, observations, decisions, contribution lineages, and evidence IDs
  remain unchanged.

## Reversal triggers

Revisit only if a known external v1 packet consumer cannot migrate atomically or if evidence shows
one canonical question cannot preserve the intended worksheet semantics. Any compatibility layer
must be justified by that observed consumer and must not alter persisted evidence.

## Verification

- An exact contract test asserts all 30 unique IDs and exact wording in order.
- Documentation parity compares the approved documented table with runtime output.
- Every packet emits all 30 questions exactly once and `schema_version: 2`.
- `action.review` is supported only by the centralized `reviewed` classification or an explicit
  claim-appropriate attestation.
- Review-only evidence does not support `action.coordination`.
- Every non-null material answer retains stable evidence citations; unknown and unresolved answers
  remain null.
- CLI and MCP golden fixtures updated atomically while the then-current six-tool MCP surface and
  cursor behavior remained unchanged.

## Current MCP supersession

[AgDR-0007](AgDR-0007-agent-evidence-workflow-migration.md), delivered through slices D and E in
[PR #36](https://github.com/AmrMohamad/WorkTrace/pull/36) and
[PR #37](https://github.com/AmrMohamad/WorkTrace/pull/37), supersedes only this record's original
six-tool and unchanged-`offset:` MCP compatibility statements. The current server has seven bounded
read-only tools and `wtc1:` candidate/evidence cursors; a legacy `offset:` cursor reports that an
upgrade/restart is required. This does not alter Phase 4 v2's 30-question schema, packet envelope,
or evidence authority rules.

## Artifacts

- `docs/phase4-schema.md`
- `src/worktrace/packets/schema.py` (planned in issue #9)
- `src/worktrace/packets/builder.py` (planned in issue #9)
- `docs/technical-designs/worktrace-read-only-human-tui.md`
