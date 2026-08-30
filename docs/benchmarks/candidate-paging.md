# Candidate paging benchmark

Measured on 2026-08-31 for issue #10 using the deterministic fixture in
`tests/test_candidate_paging.py`.

## Fixture

- 3,000 candidates in one transactional generation.
- 1,000 ignored candidates.
- 100 confirmed lineages whose current evidence is unsupported.
- 1,850 additional candidates without authoritative current evidence.
- 50 current candidates spanning manual, Git, Jira, and GitLab evidence.
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
| Median page build | 127.646 ms |
| P95 page build | 132.768 ms |
| Minimum / maximum | 126.149 / 134.196 ms |
| Raw candidates scanned | 100 |
| Candidate projections | 100 |
| Traced SQLite statements | 1,909 |

### Full visible page

The cursor starts immediately before the 50 current mixed-source candidates in the same fixture.
The page fills after projecting 25 raw rows:

| Measure | Result |
|---|---:|
| Median page build | 78.021 ms |
| P95 page build | 80.325 ms |
| Minimum / maximum | 77.390 / 81.054 ms |
| Visible candidates | 25 |
| Raw candidates scanned | 25 |
| Candidate projections | 25 |
| Traced SQLite statements | 1,808 |

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

There is intentionally no CI wall-clock assertion. Correctness tests enforce the scan and
projection bounds, one page-level decision projection, contextual/default output parity, and the
query-plan shape. A later performance ticket should still be based on measured human usage before
changing canonical evidence/participation projection or adding an index; issue #10 does not create
a second, semantically weaker row projection to improve a local timing result.

## Residual limitation

Candidate IDs are globally unique, but the existing indexes do not combine application and
candidate ID. The primary-key keyset query may step over rows for other applications internally
before collecting its bounded matching batch. The public scan budget still bounds rows projected
for the selected application. A composite index is intentionally deferred until a representative
multi-application ledger demonstrates a material problem.
