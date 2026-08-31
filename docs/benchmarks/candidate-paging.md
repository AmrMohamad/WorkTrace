# Candidate paging benchmark

Measured on 2026-08-31 for issue #10 using the deterministic fixture in
`tests/test_candidate_paging.py`.

## Fixture

- 3,000 candidates in one transactional generation.
- 3,000 authoritative current observations and 3,000 coherent participations spanning manual,
  Git, Jira, and GitLab sources.
- 1,000 ignored candidates.
- 100 confirmed lineages, 50 of which retain an additional unsupported member.
- 1,900 ordinary current candidates.
- One current availability event and one separately scoped application fixture.
- Default page size 25, batch size 50, and scan budget 100.

This deliberately measures a short first page whose first 100 raw candidates are all skipped. It
is a bounded worst case for the default scan budget, not a forecast of every real portfolio.

## Query plan

SQLite 3.53.1 reports:

```text
Active generation
SEARCH candidate_groups USING INDEX idx_candidates_app_time (app_id=?)

Mixed-timestamp probes
SEARCH candidate_groups USING COVERING INDEX idx_candidates_app_time
    (app_id=? AND generated_at<?)
SEARCH candidate_groups USING COVERING INDEX idx_candidates_app_time
    (app_id=? AND generated_at>?)

Raw keyset batch
SEARCH candidate_groups USING INDEX sqlite_autoindex_candidate_groups_1 (id>?)
```

Neither query performs a full `candidate_groups` table scan or uses a temporary B-tree. The raw
query deliberately uses the existing primary-key index to preserve `id > after_candidate_id`
ordering without a migration.

## Local results

Environment: Apple M2, arm64, macOS 26.7, Python 3.12.12. After three warm-up reads, 20 fresh
read-only connections were measured for each case.

### Fully skipped default window

| Measure | Result |
|---|---:|
| Median page build | 67.618 ms |
| P95 page build | 70.873 ms |
| Minimum / maximum | 65.112 / 71.011 ms |
| Raw candidates scanned | 100 |
| Candidate projections | 100 |
| Traced SQLite statements | 2,011 |

### Full visible page

The cursor starts immediately before a mixed-source current candidate window in the same fixture.
The page fills after projecting 25 raw rows:

| Measure | Result |
|---|---:|
| Median page build | 66.132 ms |
| P95 page build | 79.594 ms |
| Minimum / maximum | 63.536 / 81.815 ms |
| Visible candidates | 25 |
| Raw candidates scanned | 25 |
| Candidate projections | 25 |
| Traced SQLite statements | 1,710 |

### Decision-projection correction

The first visible-page measurement exposed 115,482 statements, a 1,610.941 ms median, and a
1,739.408 ms P95. Each visible row was independently rebuilding the global active-decision stream,
scope map, and lineage graph. That violated the page-proportional work invariant even though the
candidate scan itself was bounded.

The final implementation builds that canonical decision projection once per page transaction,
groups decisions by target, and maps lineages by candidate/contribution before projecting rows.
The high-decision fixture now reads the global decision stream exactly once. A paired test compares
the same 25-row page with zero and 1,100 unrelated decisions and bounds the additional statements
to one page-level decision projection rather than 25 repeated global projections. Default
`PacketBuilder` and `project_candidate` callers retain their original behavior when no context is
supplied, and contextual/default projection parity is tested directly.

### Authority/evidence-projection correction

Independent dense review found that every raw row rebuilt the application-wide authoritative
observation projection and every material member separately rebuilt the observation plus
availability projection. Before correction, a 3,000-current-observation fixture took about
4.859 seconds for a fully skipped default page and 1.437 seconds for a visible page. A separate
300-current probe counted 200 authority-CTE executions in one default scan window.

The final implementation builds one immutable, application-scoped authority/evidence context
inside each page transaction. It projects authoritative current observations plus availability
once, converts those rows to frozen `EvidenceRecord` values once, and derives coherent
participations once against the already-canonical current-observation IDs. Per-row candidate,
record, and participation projection uses those maps; context-only membership creates a replaced
record and never mutates the shared value.

The dense trace now records exactly one authority/evidence context query, one participation-context
query, one active-decision stream, zero legacy per-row authoritative-participation CTEs, and zero
per-member `WHERE current.source_object_id=...` queries. Low/dense evidence fixtures contain 25 and
3,000 current observations respectively; each still builds exactly one context and produces
field-identical selected candidate rows. Contextual/default parity covers visible, ignored,
confirmed-plus-unsupported, cross-application, availability, title, participation, decision/undo,
and context-only cases.

There is intentionally no CI wall-clock assertion. Correctness tests enforce the scan and
projection bounds, one page-level decision projection, one page-level authority/evidence context,
contextual/default output parity, and the query-plan shape. Issue #10 does not create a second,
semantically weaker row projection to improve a local timing result.

## Residual limitation

Candidate IDs are globally unique, but the existing indexes do not combine application and
candidate ID. The primary-key keyset query may step over rows for other applications internally
before collecting its bounded matching batch. The public scan budget still bounds rows projected
for the selected application. A composite index is intentionally deferred until a representative
multi-application ledger demonstrates a material problem.

Three operations remain proportional to selected-application history, but each runs exactly once
per page transaction: the authority/availability projection (3,000 rows in this fixture), coherent
participation projection (3,000 rows), and active decision/scope/lineage projection (1,100
decisions). They are never retained across pages. On the measured dense fixture their combined
cost remains within a 70.873 ms P95 worst-case page and a 79.594 ms P95 visible page. A later
change should revisit them only if representative portfolios exceed the interaction target; this
PR intentionally adds no index, materialization, or persistent cache.
