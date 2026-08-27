# Phase 4 packet schema

## Purpose

A Phase 4 packet is a bounded, evidence-backed worksheet for one confirmed contribution. It separates contribution identity, problem/context, action, and result. The packet helps Codex draft only what the ledger can defend and ask the user only about remaining gaps.

The packet is a read model, not a stored career narrative and not a claim that the contribution is interview-ready.

## Top-level envelope

```json
{
  "schema_version": 1,
  "contribution": {
    "id": "CONTRIB-001",
    "title": "Checkout validation correction",
    "app_id": "sample_store",
    "app_name": "Sample Store",
    "period": {"from": "2026-01-05", "to": "2026-01-14"}
  },
  "as_of": "2026-01-15T12:00:00Z",
  "source_status": {},
  "participation": {},
  "release_ladder": {},
  "sections": {},
  "contradictions": [],
  "gaps": [],
  "defensibility": {},
  "limitations": []
}
```

`as_of` is the packet-build time. `source_status` is included even when all configured sources are complete.

## Source status

Each configured source reports:

```json
{
  "status": "complete",
  "completeness": "complete_for_scope",
  "last_complete_run_id": "RUN-GIT-001",
  "last_completed_at": "2026-01-15T11:58:00Z",
  "source_updated_through": "2026-01-14T20:00:00Z",
  "stale_warning": null,
  "availability": "visible",
  "limitations": []
}
```

Allowed completeness values are `complete_for_scope`, `partial`, `selection_biased`, `source_unavailable`, and `unknown`. An interrupted or stale-running import is not current evidence.

## Question answer

Every entry in a Phase 4 section follows this logical schema:

```json
{
  "question_id": "identity.what",
  "question": "What was the contribution?",
  "answer_draft": "Corrected checkout name validation for multi-part names.",
  "status": "supported",
  "observation_types": ["source_asserted", "repository_observed"],
  "supporting_evidence_ids": ["jira:fixture.example:DEMO-101", "git:sample-store:commit:aaaaaaaa"],
  "contradicting_evidence_ids": [],
  "limitations": [],
  "missing_information": []
}
```

Allowed statuses are `supported`, `partially_supported`, `human_attested`, `contradicted`, `unresolved`, and `unknown`.

### Materiality and citation rules

- An answer about role, ownership, implementation, scope, delivery, release, current use, customer effect, or measurable result is material.
- A non-null material `answer_draft` must cite at least one stable supporting evidence ID.
- A candidate or contribution ID alone is not an evidence citation.
- When evidence is missing or inappropriate for the claim, `answer_draft` is `null`; status is `unknown` or `unresolved`; and the missing fact is listed.
- Supporting and contradicting evidence are separate arrays.
- Human attestations are cited and labelled `human_attested`; they are not converted to repository-observed facts.

## Question map

### Contribution identity

| ID | Question | Typical evidence |
| --- | --- | --- |
| `identity.what` | What was the contribution? | Confirmed title, Jira summary, MR title |
| `identity.app_flow` | Which app and business flow? | Explicit app mapping, module paths, Jira components |
| `identity.when` | When did it occur? | Earliest/latest relevant observations |
| `identity.origin` | Assigned, proposed, inherited, or discovered? | Jira history, reporter, comments, MR discussion, attestation |
| `identity.ownership` | Sole, main, or contributing role? | Participation facts plus human attestation |

### Problem and context

| ID | Question | Typical evidence |
| --- | --- | --- |
| `problem.what` | What problem existed? | Jira description/comments as source assertions |
| `problem.before` | What happened before the change? | Bug report, prior code metadata, discussions |
| `problem.severity` | How serious was it? | Priority, severity, hotfix label, reported blocker |
| `problem.affected` | Who or what was affected? | Source assertions and configured flow mapping |
| `problem.blocked` | What did it block? | Source assertion plus flow type |
| `problem.constraints` | What constraints existed? | Jira comments and MR discussion |
| `problem.ambiguity` | Was the requirement unclear? | Requirement edits, comments, review discussion |

### Action

| ID | Question | Typical evidence |
| --- | --- | --- |
| `action.implementation` | What did the user implement? | User-authored commits/MRs and changed modules |
| `action.decisions` | Which technical decisions did the user make? | Authored explanations and discussions |
| `action.technology` | Which tools or frameworks were involved? | Inspected changed paths/imports and descriptions |
| `action.reuse` | Was a reusable component produced? | Shared modules and later structural references |
| `action.architecture` | Which layers or data flow changed? | Changed modules and discussion |
| `action.coordination` | What coordination occurred? | Cross-role comments and discussions |
| `action.quality` | What tests, docs, or monitoring changed? | Changed-path metadata and MR description |
| `action.review` | Did the user review others? | Reviewer participations and discussion records |

### Result

| ID | Question | Typical evidence |
| --- | --- | --- |
| `result.change` | What changed? | Merged implementation and issue resolution observations |
| `result.measurement` | Is there a measurable before/after? | Manual or telemetry evidence only |
| `result.scope` | Which modules, screens, app, or market were affected? | Config and changed paths |
| `result.errors_time` | Were errors or time reduced? | Metrics only |
| `result.business` | Was conversion, stability, or another outcome improved? | Claim-appropriate metrics; otherwise unknown |
| `result.release` | How far did delivery progress? | Independent release ladder rungs |
| `result.current_use` | Is it still used or enabled? | Current evidence or attestation |
| `result.reuse` | Was it reused later? | Later structural references |
| `result.feedback` | Was feedback recorded? | Source comment or manual evidence |
| `result.defensibility` | Which parts are defensible in an interview? | Breakdown below, never a boolean |

## Participation summary

The packet exposes evidence facts rather than an ownership label:

```json
{
  "jira_assignment_intervals": [],
  "authored_commit_evidence_ids": [],
  "committer_only_commit_evidence_ids": [],
  "coauthored_commit_evidence_ids": [],
  "authored_merge_request_evidence_ids": [],
  "review_evidence_ids": [],
  "merge_action_evidence_ids": [],
  "deployment_action_evidence_ids": [],
  "other_implementation_authors": [],
  "ownership_statement": {"status": "requires_human_confirmation", "evidence_id": null}
}
```

Other contributors remain visible. Evidence counts may aid inspection within this contribution; they must not be promoted to a score or cross-person ranking.

## Release ladder

The packet contains seven independent rungs:

```text
implementation_observed
merged
release_associated
deployment_observed
released_to_users
currently_enabled_or_used
measurably_successful
```

Each rung has `status`, `supporting_evidence_ids`, `contradicting_evidence_ids`, and `limitations`. No rung inherits support from the preceding rung.

## Contradictions and gaps

A contradiction contains the claim/question ID, supporting evidence IDs, contradicting evidence IDs, and a neutral explanation. A gap contains the question ID, missing fact, why current evidence cannot answer it, whether human confirmation is required, and a bounded suggestion such as “check a verified release record.” Suggestions never cause WorkTrace to follow a URL or access a new source.

## Interview-defensibility breakdown

The packet must not return `interview_defensible: true`. It returns:

```json
{
  "well_supported": ["problem description", "personal implementation", "merge status"],
  "partially_supported": ["ownership level", "production release"],
  "missing": ["measurable result", "current use"],
  "suggested_follow_up": ["Confirm ownership wording", "Check an authorized mobile release record"]
}
```

The breakdown is computed from question statuses and authority, not activity volume.

## Determinism

For the same current complete observations, decision projection, packet schema version, authority rules, and `as_of`, rebuilding derived references and candidates must produce the same material packet. A packet always names the versions needed to explain a later difference.
