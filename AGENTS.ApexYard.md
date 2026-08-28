# ApexYard control-plane route

WorkTrace is governed by a separate ApexYard ops checkout. This repository must remain a normal
Python project: do not copy ApexYard hooks, generated adapters, roles, skills, private portfolio
configuration, or session markers here, and do not create a nested ApexYard installation.

## Resolve and verify the control plane

Before any mutation, multi-step implementation, architecture decision, ticket, branch, commit,
pull request, release, or other delivery action:

1. Build an ordered candidate list: a non-empty `APEXYARD_OPS_ROOT` first, followed by the sibling
   path `../apexyard` relative to the WorkTrace repository root. An invalid environment override
   does not prevent checking the sibling.
2. Accept the first candidate only when `git -C <candidate> rev-parse --show-toplevel` resolves to the
   same canonical directory and it contains `.apexyard-fork`, `AGENTS.md`, `.claude/`, and
   `bin/sync-codex-adapter.sh`.
3. Read the ops root `AGENTS.md`, then resolve the private onboarding file and registry through
   the portfolio helpers before reading them. Also read the minimum applicable ApexYard rules,
   roles, and skills.
4. Run portfolio helpers under Bash, from the ops checkout. Validate the private portfolio and
   run `bash bin/sync-codex-adapter.sh --check-installed` before relying on the installation.
5. Confirm that the registry entry for `worktrace` points at the current repository and the
   intended tracker before creating or using a ticket marker.

If neither candidate verifies, stop before mutation and report that the ApexYard control plane is
unavailable. Do not silently continue with invented ticket IDs, review evidence, or gate results.

## Execution boundary

- Run ApexYard commands with the ops checkout as the working directory. Its CLI does not discover
  an external managed checkout merely from an environment variable.
- Use `Step` or `Item` until a real tracker issue has been created and read back.
- Follow the ticket, architecture, coverage, review, QA, and explicit per-PR merge gates.
- Preserve unrelated dirty state in the ops checkout and private portfolio. Stage explicit paths.
- External repositories, issues, pull requests, credentials, provider access, merges, releases,
  and publications require the current user's authorization for that exact action.
- Keep public source and synthetic fixtures in WorkTrace. Keep local paths, portfolio metadata,
  proprietary context, credentials, and governance session state in the private portfolio.

## Mechanical enforcement truth

This module is durable project guidance, not proof that ApexYard hooks executed. Codex natively
loads the root `AGENTS.md`; the root explicitly routes this sibling module. ApexYard's generated
Codex hooks remain in the separate ops checkout and require harness trust there. GitHub branch
protection and required CI checks are the remote mechanical controls for this repository.
