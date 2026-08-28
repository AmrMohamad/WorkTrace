# AgDR-0002: ApexYard public delivery boundary

## Status

Accepted for WorkTrace v0.1 publication on 2026-08-27.

## Context

WorkTrace was implemented as an initially repo-less greenfield project governed from a separate
ApexYard ops checkout and private portfolio. Public delivery must preserve that control plane,
make future Codex sessions route to it, and avoid publishing private portfolio data or copying a
generated governance runtime into the product repository.

The initial public repository also needed a base branch before GitHub could enforce pull-request
and required-check protection. A greenfield repository cannot open a pull request without an
existing base commit.

## Decision

1. Keep ApexYard and its private portfolio separate from WorkTrace. WorkTrace stores only public
   source, synthetic fixtures, documentation, and portable project instructions.
2. Use the root `AGENTS.md` as the native Codex entry point and explicitly route consequential
   work through `AGENTS.ApexYard.md`. That module verifies an environment-selected or sibling ops
   checkout before mutation and never claims that sibling files are auto-discovered.
3. Use one explicitly authorized, content-free root commit on public `main` as the greenfield base
   exception. All source is delivered afterward through a ticketed Draft pull request.
4. Use GitHub branch protection and one required `quality` workflow as the public repository's
   mechanical controls. The separate ApexYard adapter remains responsible for local governance
   only when a harness loads and trusts it. The initial required status context is necessarily
   unbound because the empty base has no check run. After the Draft pull request creates the first
   `quality` check, bind the requirement to the observed GitHub Actions app ID and verify the
   protection readback before treating the gate as authoritative.
5. Pin third-party GitHub Actions to reviewed immutable commit SHAs. The workflow pins Checkout,
   Python setup, and uv setup while also pinning uv itself to version 0.10.6.
6. Keep GitHub approvals at zero for this sole-maintainer repository to avoid a self-review
   deadlock. ApexYard's independent review evidence and explicit per-PR user approval remain
   required before merge.

## Consequences

- A future Codex session started in WorkTrace receives the public routing rules immediately.
- Local ApexYard hook execution remains a separately verifiable proof layer; project instructions
  cannot substitute for hook trust.
- Public branch protection blocks direct source delivery and requires the named quality check.
- Until the first check run allows app binding, another installed integration capable of writing
  the same unbound context name could satisfy that status requirement. The Draft pull request must
  not become merge-eligible while this residual risk remains.
- Private portfolio registration is delivered independently so private paths and governance
  context never enter the public WorkTrace diff.
- No live Jira, GitLab, or proprietary-repository validation is implied by public CI.

## Rejected alternatives

- Copying ApexYard hooks or the generated Codex adapter into WorkTrace would create configuration
  drift and a nested installation boundary.
- Editing global Codex hooks would broaden the change beyond WorkTrace and could affect unrelated
  projects.
- Requiring one GitHub approval would make the sole maintainer unable to satisfy protection using
  their own review.
- Publishing source directly on `main` would bypass the normal ticket, review, QA, and explicit
  merge gates.
