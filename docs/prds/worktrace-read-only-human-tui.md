<!-- Source shape: ApexYard templates/prd.md · github.com/me2resh/apexyard · MIT -->

# PRD: WorkTrace Read-Only Human TUI

**Status**: Accepted and delivered for the original read-only review journey

**Author**: Mariam (Product Manager)

**Created**: 2026-08-30

**Last Updated**: 2026-08-30

**Parent Feature**: [#6 — Read-only human contribution-review TUI](https://github.com/AmrMohamad/WorkTrace/issues/6)

## Delivery record

The original product and implementation sequence is complete:

| Product/implementation item | Issue | Delivered by |
|---|---|---|
| Product requirements | [#7](https://github.com/AmrMohamad/WorkTrace/issues/7) | [PR #12](https://github.com/AmrMohamad/WorkTrace/pull/12) — merged 2026-08-30 |
| Technical design and security contract | [#8](https://github.com/AmrMohamad/WorkTrace/issues/8) | [PR #13](https://github.com/AmrMohamad/WorkTrace/pull/13) — merged 2026-08-30 |
| Phase 4 v2 prerequisite | [#9](https://github.com/AmrMohamad/WorkTrace/issues/9) | [PR #14](https://github.com/AmrMohamad/WorkTrace/pull/14) — merged 2026-08-30 |
| Generation-bound candidate read | [#10](https://github.com/AmrMohamad/WorkTrace/issues/10) | [PR #15](https://github.com/AmrMohamad/WorkTrace/pull/15) — merged 2026-08-31 |
| Read-only TUI vertical slice | [#11](https://github.com/AmrMohamad/WorkTrace/issues/11) | [PR #16](https://github.com/AmrMohamad/WorkTrace/pull/16) — merged 2026-08-31 |

The current MCP surface is seven bounded read-only tools with view-bound `wtc1:` candidate and
evidence cursors. References to six tools and `offset:` cursors below describe the original PRD
baseline; [AgDR-0007](../agdr/AgDR-0007-agent-evidence-workflow-migration.md) supersedes only
those historical MCP constraints. This addendum approves the #22 discovery contract; it does not
claim that search is implemented. The representative private-ledger measurement remains deferred,
so the later parent feature remains open.

## Overview

### Problem Statement

At this PRD's original baseline, WorkTrace could be inspected through a JSON-oriented CLI and six
bounded read-only MCP tools. Those interfaces preserve automation and agent workflows, but they make a multi-year human review cumbersome: the engineer must remember commands, copy opaque identifiers, inspect large JSON responses, and mentally connect candidates, contributions, participation, delivery states, questions, gaps, and exact evidence.

The first human interface must make one contribution understandable in a focused terminal session without creating a second evidence model or a new mutation surface.

### Target User

**Primary**: A software engineer privately reviewing their own contribution history for CV preparation, interview preparation, portfolio writing, or personal career inventory.

**Secondary**: None in the first release. WorkTrace is not an employee-evaluation, promotion, ranking, seniority, or productivity system.

### North-Star Outcome

A successful session means the engineer can select one contribution and understand its identity, evidence, participation, contradictions, delivery state, and remaining Phase 4 gaps without reading raw JSON or asking an agent to explain WorkTrace's internal structure.

### Goals

1. Complete one end-to-end contribution review using only the keyboard in an 80x24 compact layout and a 120x40 full layout.
2. Keep every material answer traceable to stable evidence identifiers while preserving unknown, partial, human-attested, contradicted, stale, and unavailable states.
3. Explain source health and failures honestly, with a concrete next action and no implied data preservation that the current read model cannot prove.
4. Preserve the existing CLI and MCP contracts while adding no human write capability in the first release.
5. Reduce accidental disclosure by rendering stored text literally and limiting application clipboard actions to validated WorkTrace identifiers.

### Non-Goals

- Importing evidence or contacting Jira, GitLab, local Git repositories, or any other provider.
- Confirming, ignoring, renaming, merging, splitting, or changing candidate membership.
- Adding manual evidence, attestations, or any other human decision.
- Rebuilding, migrating, exporting, backing up, purging, or editing configuration.
- Persisting UI preferences, watching the database, running background synchronization, or adding a daemon or web server.
- Copying evidence bodies, packets, titles, errors, or provider URLs through application actions.
- Preventing the authorized local user from taking an operating-system screenshot, selecting terminal text, photographing the screen, or logging terminal output.
- Producing productivity, value, ownership, performance, promotion, ranking, or contribution scores.
- Claiming full screen-reader accessibility for a redraw-heavy terminal application.

### Success Metrics

No usage telemetry is collected. Success is measured during acceptance testing with synthetic data.

| Metric | Target | How Measured |
|--------|--------|--------------|
| Complete review journey | One application-to-evidence journey succeeds without raw JSON | Keyboard-only behavioral test and human acceptance walkthrough |
| Evidence traceability | Every displayed material statement exposes its supporting or contradicting stable IDs | Contract and UI assertions |
| Question completeness | Every question returned by the canonical packet appears exactly once | Read-model-to-UI parity test |
| Bounded browsing | One candidate page never projects beyond its configured scan budget | Query diagnostics in tests |
| Small-terminal usability | The complete journey works at 80x24 | Headless terminal-size acceptance test |
| Full-layout usability | Candidate context and contribution details use the available space at 120x40 | Headless terminal-size acceptance test |
| Read-only integrity | No database, managed data-directory, provider, or network state changes during UI journeys | Capability and before/after integrity tests |

## User Stories

### US-1: Select the review scope

> As an engineer with one or more configured applications, I want to select the application I am reviewing so that every candidate and contribution remains within the intended scope.

**Acceptance Criteria**:

- [ ] One configured application opens automatically.
- [ ] Multiple configured applications produce a keyboard-operable selection screen.
- [ ] Missing configuration or database state exits with stable CLI instructions; no setup wizard opens.
- [ ] A supplied candidate deep link is rejected unless an application is supplied and the candidate resolves inside that application.

### US-2: Understand source condition

> As an engineer reviewing evidence, I want to see the latest source-attempt condition so that I know which limitations apply before interpreting a contribution.

**Acceptance Criteria**:

- [ ] Source status shows the latest attempt status, completeness, completion time, staleness, authoritative-current state, sanitized error, and limitations when present.
- [ ] The interface does not label a timestamp as "Last complete" or claim that a previous snapshot was retained without an authoritative-snapshot query proving it.
- [ ] Every unavailable, busy, or stale state explains what happened and gives a safe next action.

### US-3: Browse bounded candidate pages

> As an engineer with years of evidence, I want predictable candidate pages so that I can move through the review set without a full-dataset pause.

**Acceptance Criteria**:

- [ ] The first page and later pages load without blocking terminal interaction.
- [ ] Next and previous navigation preserves a bounded in-memory cursor history for the current session.
- [ ] A changed candidate generation clears pagination and restarts at page one with an explicit explanation.
- [ ] A short page caused by skipped candidates remains navigable when more raw candidates exist.
- [ ] Candidate rows show only status, title and provenance, period, source coverage, and high-level participation indicators.
- [ ] Generic candidate-authority warnings appear once at screen level rather than being repeated on every row.

### US-4: Review one contribution

> As an engineer preparing defensible career material, I want one coherent contribution view so that I can distinguish observed facts, derived grouping, human assertions, contradictions, and unanswered questions.

**Acceptance Criteria**:

- [ ] Summary shows title authority, stable IDs, application, type, status, period, modules, sources, current evidence, unsupported members, and contradictions.
- [ ] Evidence groups current and unsupported records by source and opens a bounded excerpt without losing contribution context.
- [ ] Participation preserves author, committer, co-author, reviewer, assignee, merger, deployer, and other-contributor roles without inferring ownership.
- [ ] Delivery presents implemented, merged, release-associated, deployed, released-to-users, currently-enabled, and measurably-successful as seven independent states.
- [ ] Questions render every question returned by the canonical Phase 4 packet exactly once and display the answer, support, contradiction, limitation, and missing-information explanation from the same review result.
- [ ] Unknown and unresolved states remain first-class and are not presented as tasks the user must complete.

### US-5: Inspect evidence safely

> As an engineer inspecting provider-derived text, I want literal bounded excerpts so that terminal control data or rich markup cannot masquerade as application chrome or actions.

**Acceptance Criteria**:

- [ ] Evidence is labeled `UNTRUSTED SOURCE TEXT` and includes source, kind, evidence ID, observation time, completeness, and independent truncation indicators.
- [ ] Provider text is plain, scrollable, non-clickable, and never interpreted as Rich/Textual markup.
- [ ] Dangerous terminal controls and bidirectional formatting characters appear as visible tokens.
- [ ] The application offers copying only for a selected, validated stable WorkTrace ID.

### US-6: Operate without memorizing commands

> As a keyboard-first user, I want consistent navigation and contextual help so that the review workflow remains discoverable.

**Acceptance Criteria**:

- [ ] `?` opens context help and `Ctrl+P` exposes only a fixed allowlist of safe commands.
- [ ] `a`, `r`, `q`/`Esc`, `Ctrl+Q`, `y`, `j`/`k`, `Enter`, `n`/`p`, and contribution tab keys `1`-`5` behave consistently in their documented scopes.
- [ ] Every action is keyboard accessible; mouse input is optional.
- [ ] Focus is visible and no meaning depends on color alone.
- [ ] `NO_COLOR` remains understandable in a fresh process.
- [ ] At 120x40, the candidate browser may show a selected-row preview beside the table and contribution evidence may show selection and detail together without hiding any required action.

## Edge Cases

| Scenario | Expected Behavior |
|----------|-------------------|
| Standard input or output is not a TTY | Exit code 2 with stable CLI guidance; do not import or launch the full-screen UI. |
| Configuration is missing or invalid | Exit with the expected path and CLI validation commands; do not create or edit configuration. |
| Database is missing | Exit with initialization and doctor commands. |
| Database schema is older | Exit with instructions to run `worktrace init`; do not migrate. |
| Database schema is newer than supported | Exit with a version incompatibility message. |
| Database is temporarily busy | Return control promptly, show a sanitized busy message, and offer retry or return. |
| No applications are configured | Show an actionable empty state and return safely. |
| No candidate generation exists | Show an empty state with status and rebuild CLI guidance. |
| Candidate generation changes between pages | Explain invalidation, clear prior cursors, and restart at page one. |
| Another CLI process changes decisions between pages | Do not claim snapshot consistency; explicit refresh restarts pagination. |
| Evidence is unavailable or unsupported-current | Preserve the contribution view, label the limitation, and expose the relevant stable IDs. |
| Evidence excerpt is empty | Show normalized metadata and an explicit empty-body state. |
| Evidence and terminal bounds both truncate | Show the two truncation states separately. |
| Provider text contains markup, prompts, URLs, controls, or bidi overrides | Render the encoded value literally with no command, click, URL, or clipboard behavior. |
| Terminal is below 80x24 | Offer compact drill-down, recheck, or quit; never hide required actions. |
| A stale worker completes after a newer request | Ignore the stale result and keep the newest screen state. |

## Requirements

### Functional Requirements

| ID | Requirement | Priority | Notes |
|----|-------------|----------|-------|
| FR-1 | Launch explicitly through `worktrace ui`. | Must | Existing no-argument CLI help remains unchanged. |
| FR-2 | Support application selection and scoped app/candidate deep links. | Must | Candidate requires application. |
| FR-3 | Display honest latest source-attempt health. | Must | No inferred retained-snapshot wording. |
| FR-4 | Browse generation-safe, bounded candidate pages. | Must | Existing MCP pagination is unchanged. |
| FR-5 | Review Summary, Evidence, Participation, Delivery, and Questions for one contribution. | Must | One coherent review result supplies packet and gaps. |
| FR-6 | Open a bounded, safely encoded evidence excerpt. | Must | No provider URL action or evidence-body copy. |
| FR-7 | Copy only validated stable WorkTrace IDs. | Must | Candidate, contribution, evidence, and source-object IDs as applicable. |
| FR-8 | Provide contextual keyboard help and a fixed safe command palette. | Must | No screenshot command. |
| FR-9 | Refresh the current read model and restart pagination explicitly. | Must | No automatic database watcher. |
| FR-10 | Preserve the current CLI and seven MCP tools. | Must | No CLI subprocess or MCP-as-internal-API usage. |

### Non-Functional Requirements

| Category | Requirement | Target |
|----------|-------------|--------|
| Authority | UI capabilities are structurally read-only | No provider, network, write, maintenance, export, migration, or configuration capability is reachable |
| Responsiveness | Database work never blocks the UI event loop | Immediate loading state; stale results discarded |
| Bounded work | Candidate-page effort is proportional to a fixed scan budget | Default page 25; no more than 200 raw scans by default |
| Terminal safety | Dynamic text is rendered literally after presentation encoding | No raw dangerous control survives; no dynamic command or widget identity |
| Disclosure | Application capture actions are narrowly scoped | Stable IDs only; no screenshot, body, packet, error, title, or URL copy action |
| Accessibility | Keyboard, focus, symbols, labels, monochrome, and compact layout are required | Verified at 80x24 and with `NO_COLOR`; no full screen-reader claim |
| Responsive layout | Compact and full layouts preserve the same review semantics | Complete drill-down at 80x24; additional side-by-side context at 120x40 |
| Compatibility | Existing automation and agent surfaces remain stable | Existing CLI/MCP smoke and regression tests pass |
| Packaging | Installed distributions contain the complete UI | Textual dependency locked; TCSS resource present in wheel |

## Interaction Specification

### Review Flow

```text
worktrace ui
    -> select application when needed
    -> inspect latest source-attempt status
    -> browse a bounded candidate page
    -> open one contribution
    -> review Summary / Evidence / Participation / Delivery / Questions
    -> open one bounded evidence excerpt
    -> return without losing candidate-page context
```

### Primary Surfaces

1. **Application Selection** — shown only when more than one application is configured.
2. **Candidate Browser** — includes source-attempt status, page navigation, candidate authority notice, and bounded rows.
3. **Contribution Review** — five tabs covering the complete review model.
4. **Evidence Excerpt** — modal, bounded, literal, non-clickable, and labeled untrusted.

At 80x24, the workflow uses a single primary pane and drill-down navigation. At 120x40, the candidate browser may add a selected-row preview and contribution evidence may use side-by-side selection and detail. The larger layout adds context, not capabilities; no required information or action is exclusive to it.

No dashboard-only shell is part of this release. The first executable UI must ship the complete journey.

## Evidence discovery addendum — accepted contract for [#22](https://github.com/AmrMohamad/WorkTrace/issues/22), not implemented

This addendum adds a bounded evidence-search journey above the delivered candidate browser. It is
not a replacement browser mode and does not make a search match evidence of contribution ownership
or business impact.

### Search journey and filters

From a settled candidate page, `/` pushes a dedicated search screen and focuses the required query
field. The original candidate-screen instance stays underneath, including its selection, committed
page history, and stack depth. The form has an optional one-source filter (`git`, `jira`, `gitlab`,
or `manual`), optional module text, optional inclusive ISO `From` and `To` activity dates, and an
explicit Search action. Enter in a text field submits; edits never query automatically.

The submitted query is trimmed, nonempty, has no NUL, and is at most 500 characters. Module text is
trimmed, nonempty when supplied, has no NUL, and is at most 200 characters. Source must be one of
the listed configured evidence source types. Dates must be ISO calendar dates and `From` must not be
after `To`. Validation occurs before a worker is started: an error leaves the last successful page
and its selection intact.

Query matching keeps the existing scanner's case-insensitive literal-substring behavior over current
observation title, body, and retained observation data; `%`, `_`, and `\\` are escaped rather than
acting as query wildcards. Module text is the existing case-insensitive literal substring match over
retained observation data. It is not a module classifier, a separate authority signal, or a ranking
system. Date filters use recorded activity periods, not fetch or source-freshness timestamps. With
no dates, undated evidence remains eligible; with either date filter, it is excluded and that policy
is displayed.

### Results, links, and continuation

The screen presents a result table and a separate canonical-link list for its selected result. Its
status text shows the submitted filters, page state, and link-coverage limitations.

A search page returns at most 20 eligible current observations after scanning at most 200 raw rows,
ordered by freshness then observation ID. A short or empty page may still carry a continuation when
the scanner excluded rows or stopped at its raw-row budget; only an absent continuation means the
traversal ended. Every result presents stable observation and object IDs, source/kind, bounded title
and period metadata, readiness/limitations, and any canonical contribution links.

Links are enrichment, not an ownership assertion. The reader follows generated membership and exact
accepted decision fields, resolves aliases and active lineages, then returns only canonically
confirmed effective material/context membership. It attempts at most 50 **distinct canonical group
projections across a page**, counting attempted projections that yield no effective link. That budget
is fairly allocated across results so a high-fanout result cannot consume the page. At most five
links display for one result; a canonical group is reused only inside the one read snapshot. No
persistent cache, search index, or new provider capability is introduced. This is a canonical
projection-work budget, not a total bound on broader authority or decision-context work; those costs
remain separately measurable.

The page reports link coverage honestly: it distinguishes no effective links after complete
evaluation, verified links with possible links not evaluated or displayed, and no returned links
because evaluation was incomplete. It never states an exact unseen total. Returned links retain
suggested/confirmed status, material/context role, and applicable evidence limitations through
confirmation, add/remove, undo, merge, split, and ignore decisions.

### Navigation, state, and disclosure

The screen has separate result and selected-result link lists. Tab moves among the form, results,
and links; Enter on a result opens the existing bounded excerpt modal, and Enter on a canonical link
opens contribution review. Back from review restores that same search page and selection; Back from
search restores the original candidate page and selection; global Return reveals that original
candidate browser. Closing an excerpt returns to search. Application switching uses existing
screen-stack normalization, so it leaves no hidden nested screen. Printable navigation keys remain
ordinary text while an editable field has focus. At 80x24, filters scroll while useful result/link
areas remain available; 120x40 exposes more rows without changing behavior.

`n` and `p` request next/previous pages only when an editable field does not have focus and no page
request is pending. They operate on committed history only; a page and its history entry change
together only after the matching request succeeds.

The screen separates draft form values, successfully submitted filters, displayed results and
selection, committed cursor history, and a pending request. Pending page navigation is ignored and
only a matching successful response commits data/history. Older, closed-screen, or other-app
responses are ignored. An ordinary read failure preserves and labels the last successful page.
Evidence invalidation clears stale rows and links, attempts one restart from the submitted filters,
and, if that restart fails, leaves a recoverable empty state with Retry. A search continuation binds
application, submitted filters, read revision, read-model version, and the last scanned evidence key;
changing filters starts a new traversal and a changed revision invalidates it.

A confirmed canonical link may open contribution history even when the generated candidate row no
longer exists. Ignored, removed, or out-of-scope targets receive a recoverable error; a missing
current observation does not erase an effective confirmed membership. Its confirmed status is
derived from the active lineage; an existing generated candidate retains its established generated
status. Every dynamic title, filter
summary, source value, error, and link label follows the existing terminal encoder and literal
rendering path. Text selection/copy remains disabled; only the explicit validated stable-ID action
can copy an ID. Search adds no provider client, import, configuration writer, export, persistent
history, or refresh loop.

## Technical Notes

Technical design is owned by the Tech Lead and Solution Architect in [#8](https://github.com/AmrMohamad/WorkTrace/issues/8). The PRD establishes these product constraints for that design:

- The CLI remains the only mutation boundary.
- MCP remains seven bounded, read-only tools.
- The TUI adds no provider, credential, network, write, maintenance, export, or migration capability.
- The packet remains the canonical claim projection; the TUI does not invent claim semantics.
- The executable UI is delivered only after its packet and bounded-query prerequisites.

## Launch Plan

This is a local developer-tool release, not a remote service rollout.

- [x] User-approved product direction and non-goals.
- [x] PRD reviewed and merged in [PR #12](https://github.com/AmrMohamad/WorkTrace/pull/12).
- [x] Technical design and both required AgDRs reviewed and merged in [PR #13](https://github.com/AmrMohamad/WorkTrace/pull/13).
- [x] Phase 4 packet v2 merged in [PR #14](https://github.com/AmrMohamad/WorkTrace/pull/14).
- [x] Bounded candidate read model merged in [PR #15](https://github.com/AmrMohamad/WorkTrace/pull/15).
- [x] Complete TUI vertical slice merged in [PR #16](https://github.com/AmrMohamad/WorkTrace/pull/16); its later navigation acceptance is recorded separately.

No telemetry, remote feature flag, provider rollout, or background enablement is introduced.

## Open Questions

There are no product-scope blockers. Technical questions about schema compatibility, pagination, terminal encoding, packaging, and worker composition are deliberately assigned to [#8](https://github.com/AmrMohamad/WorkTrace/issues/8).

## Delivery Sequence

| Milestone | Tracking Issue | Status |
|-----------|----------------|--------|
| PRD approved | [#7](https://github.com/AmrMohamad/WorkTrace/issues/7) | Complete — [PR #12](https://github.com/AmrMohamad/WorkTrace/pull/12) |
| Technical design approved | [#8](https://github.com/AmrMohamad/WorkTrace/issues/8) | Complete — [PR #13](https://github.com/AmrMohamad/WorkTrace/pull/13) |
| Phase 4 v2 complete | [#9](https://github.com/AmrMohamad/WorkTrace/issues/9) | Complete — [PR #14](https://github.com/AmrMohamad/WorkTrace/pull/14) |
| Bounded read model complete | [#10](https://github.com/AmrMohamad/WorkTrace/issues/10) | Complete — [PR #15](https://github.com/AmrMohamad/WorkTrace/pull/15) |
| Read-only TUI complete | [#11](https://github.com/AmrMohamad/WorkTrace/issues/11) | Complete — [PR #16](https://github.com/AmrMohamad/WorkTrace/pull/16) |

No calendar date is committed before Engineering completes technical design and estimation.

## Approvals

| Role | Name | Date | Status |
|------|------|------|--------|
| Product Manager | Mariam | 2026-08-30 | Author |
| Product authority | Amr Mohamad | 2026-08-30 | Approved through the implementation request |
| Tech Lead | Hisham | 2026-08-30 | Delivered in [PR #13](https://github.com/AmrMohamad/WorkTrace/pull/13) |
| Solution Architect | Tariq | 2026-08-30 | Accepted design delivered in [PR #13](https://github.com/AmrMohamad/WorkTrace/pull/13) |
