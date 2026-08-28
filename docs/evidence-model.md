# Evidence model

## Design objective

WorkTrace uses evidence-oriented storage and claim-oriented output. SQLite records what a configured source reported or what a repository exposed at a particular time. Packet builders assess whether those observations support a particular claim. A polished career story is never stored as source truth.

## Stable source objects and versioned observations

A source object is the stable identity of an issue, comment, merge request, discussion note, commit, tag, deployment, or manual record. A stable ID incorporates the source, source instance, kind, and external identifier, for example:

```text
jira:fixture.example:DEMO-101
gitlab:101:mr:7
git:sample-store:commit:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
```

An observation is the immutable, redacted version WorkTrace saw in one sync run. Edits create later observations; they do not overwrite prior ones. Each observation records:

- source object and sync run;
- source update time and fetch time;
- normalized payload hash;
- title, bounded body text, and structured normalized data;
- completeness and availability context; and
- adapter, normalization, and redaction versions.

This separation permits explanations such as “Jira showed In Progress in run A and Done in run B” without treating either historical snapshot as timeless truth.

## Import provenance

Every import has a parent session and role-specific source runs. A run records its configured app, source, source instance, full-snapshot scope, status, timestamps, adapter version, progress, completeness, and error summary.

- Pages persist transactionally so a crash does not discard already normalized observations.
- Only a completed run is eligible to become current evidence for that source and scope.
- A partial, failed, interrupted, or stale-running run is visible but is never silently promoted over the previous complete run.
- Stable source identity and payload hashes make retries idempotent.
- Previous evidence remains available when a source becomes unavailable, but packets mark it stale or unavailable.

## Actors and participations

Actors retain source-specific identity. External identities are not merged by display name. External emails are hashed before persistence. Only explicitly configured and verified aliases map to the local user.

Participations attach an actor to a source object, and optionally an observation, using a precise role and effective interval. Roles include:

```text
git_author
git_committer
git_coauthor
jira_creator
jira_comment_author
jira_changelog_author
jira_assignee
jira_reporter
mr_author
mr_assignee
mr_reviewer
mr_merger
gitlab_commit_author
gitlab_commit_coauthor
gitlab_commit_committer
gitlab_commit_reviewer
gitlab_discussion_author
gitlab_deployer
gitlab_release_author
```

Assignment is an observed relationship, not ownership. A commit with another author and the local user as committer remains `committer_only` evidence for the local user.

## Typed references

References state exactly what was observed. Structural examples include `mr_contains_commit`, `git_reverts_commit`, `git_cherry_picks_commit`, `deployment_contains_sha`, and `tag_points_to_commit`. Textual examples include `mentions_jira_key`, `mentions_commit_sha`, and `mentions_mr`. Human relationships include `human_confirms_same_contribution` and `human_marks_context_only`.

Each reference preserves its extraction method, exact matched value, and supporting observation. Generic `related_to` is used only when the source itself offers no more precise relationship.

An exact reference does not prove ownership, authorship, or confirmed membership. It only makes the referenced object available to deterministic candidate building or review.

## Candidate groups and decisions

Candidate groups are bounded, deterministic, rebuildable suggestions. Their stable identity derives from app and seed object, not a mutable title. Candidate membership records why an object was added and whether it is context-only.

Candidate rebuilds may delete and recreate derived references and suggestions. Confirmed contributions and membership changes are projected from append-only human decisions. The decision stream records action, target, payload, local actor label, timestamp, and an optional undo target. Undo is a compensating `undo_decision` event.

## Private export v3

`worktrace-export-v3` exports authoritative-current ledger rows plus a claim-safe projection of candidate and decision history. `candidate_groups.seed_object_id` is populated only when the immutable identity seed is present in the exported current object set. When display metadata is deterministically reselected from another eligible member, `metadata_source_object_id` names that current object and `unsupported_seed_object_id` preserves the non-citable identity seed as an explicit unsupported stable ID.

`human_decisions` contains the complete in-app append-only closure for exported candidates, contributions, source objects, and their undo chains. Historical undo scope follows cycle-safe stable decision-ID ancestry rather than decision timestamps. Legacy nested undo rows remain inactive but may be inspected when their target lineage resolves to exactly one app; missing, mismatched, cyclic, cross-app, or ambiguous ancestry remains unscoped and is excluded from excerpts and export. Creation snapshots (`confirm_candidate`, `merge_contributions`, and `split_contribution`) remain exportable after deterministic candidate rebuilds delete their derived row. `unsupported_contribution_history` carries only human-supplied title/state, stable unsupported member IDs, and decision IDs; it never hydrates quarantined provider metadata or evidence. A creation payload is in scope only when its surviving candidate or resolvable snapshot members establish exactly the requested app; its declared `app_id` cannot override that ownership.

## Evidence dimensions

WorkTrace does not collapse evidence quality into a numeric confidence score.

### Observation type

```text
source_asserted
repository_observed
derived
human_attested
unknown
```

### Claim-specific authority

```text
authoritative
supporting
contextual
inappropriate
```

Authority always depends on the claim. A Git author field is authoritative for “this actor authored this commit” and inappropriate for “this actor owned the whole feature.” A successful GitLab production deployment is authoritative for the GitLab deployment observation and inappropriate for App Store availability.

### Completeness

```text
complete_for_scope
partial
selection_biased
source_unavailable
unknown
```

### Consistency

```text
supported
contradicted
unresolved
unknown
```

### Freshness

Every claim read model includes the applicable `observed_at`, `source_updated_at`, `last_seen_at`, and `stale_warning` values.

### Claim status

```text
supported
partially_supported
human_attested
contradicted
unresolved
unknown
```

Numeric confidence, ownership scores, contribution-value scores, activity rankings, productivity measures, and seniority inference are forbidden.

## Claim and citation invariant

A material statement changes how a reader understands the contribution, the local user's role, delivery state, affected scope, or result. Every such draft must contain at least one stable supporting evidence ID. Evidence IDs cite observations or explicit attestations, not only a candidate ID. If the evidence is inappropriate or absent, the draft is `null`, the status is `unknown` or `unresolved`, and the missing fact becomes a gap.

Supporting and contradicting evidence are stored and returned separately. A contradiction is not discarded because one source appears more convenient.

## Rebuild invariants

- Rebuilding references with the same current observations and builder version produces the same typed relationships.
- Rebuilding candidates with the same references and builder version produces the same suggested groups.
- Human decisions and attestations survive every derived rebuild.
- Replaying the same decision stream yields the same confirmed view.
- Interrupted runs, reordered pages, and retries do not duplicate logical source objects.
