# Claim-specific source authority

## Principle

Authority belongs to a source for a particular claim, not to the source in general. “Direct API data” is not a universal truth category: Jira, GitLab, local Git, and manual evidence answer different questions.

| Source evidence | Strong for | Does not establish by itself |
| --- | --- | --- |
| Jira issue fields | Recorded problem, requirements, priority, status, reporter, current assignment, fix-version association | Implementation authorship, production release, current enablement, measured impact |
| Jira comments | That an identified actor made a statement at a time | Objective severity, universal customer scope, correctness of the statement |
| Jira changelog | Recorded field transitions and assignment intervals | Work performed during the assignment |
| Git commit author | Authorship of that Git object | Sole feature ownership, business context, deployment |
| Git commit committer | Integration/commit action for that Git object | Implementation authorship |
| Co-authored-by trailer | Explicit co-author participation on that commit | Share of ownership or impact |
| Local refs/tags | Reachability and release association in the local clone | Fresh remote state or deployment |
| GitLab MR author | Authorship of the merge request | Authorship of every patch or overall feature ownership |
| GitLab reviewers/discussions | Review participation and recorded technical discussion | Implementation authorship unless separate code evidence exists |
| GitLab merged state | That GitLab recorded the MR as merged | Deployment or user availability |
| GitLab deployment | Recorded SHA/ref deployment to a configured environment | App Store availability, feature-flag state, or measurable success |
| Manual evidence | The local user's explicitly supplied statement or record | Independent verification; it remains labelled human-supplied |

## Authority examples

| Evidence | Claim | Authority |
| --- | --- | --- |
| Git commit author field | “The local user authored this Git commit.” | `authoritative` |
| Git commit author field | “The local user owned the whole feature.” | `inappropriate` |
| GitLab MR state | “GitLab recorded this MR as merged.” | `authoritative` |
| Jira blocker comment | “QA reported a blocker.” | `authoritative` |
| Jira blocker comment | “All customers were objectively blocked.” | `supporting` at most |
| GitLab production deployment | “GitLab recorded a successful deployment to the configured production environment.” | `authoritative` |
| GitLab production deployment | “Every mobile user received the feature.” | `inappropriate` |
| Human ownership attestation | “The local user attests they were main owner.” | `authoritative` for the existence of the attestation; `human_attested` for the ownership claim |

## Release evidence ladder

The following rungs are independent. A later rung is never inferred merely because an earlier one is supported.

1. **Implementation observed** — authored commits, authored MR patch metadata, or review participation. These describe distinct activities.
2. **Merged** — GitLab merged state or a merge commit reachable in configured local refs.
3. **Release associated** — Jira fixVersion, a Git tag, release branch, or release note. This is not deployment.
4. **Deployment observed** — successful GitLab deployment to an explicitly configured production environment, including environment, SHA/ref, actor, and time.
5. **Released to mobile users** — manual attestation or a future verified store/release source. This is not provided by the v0.1 remote adapters.
6. **Currently enabled or used** — current code plus sufficient enablement evidence, current telemetry, or an explicit attestation.
7. **Measurably successful** — claim-appropriate before/after metrics, crash/error data, conversion/order analytics, or another explicit measurement source.

Packets report every rung, its status, evidence IDs, contradictions, and limitations. Unsupported rungs remain unknown.

## Participation wording

The safe read model reports facts:

- Jira assignment intervals;
- authored, committer-only, and co-authored commits;
- authored merge requests;
- review discussions;
- merge or deployment actions; and
- other implementation authors.

It does not calculate a main-owner label. “Sole owner” and “main owner” require explicit human confirmation and must remain visibly attested. Counts may summarize evidence inside one contribution but must not become comparative activity or productivity metrics.

## Contradictions and source loss

When sources disagree, WorkTrace returns both observations. It may apply authority rules to a narrowly defined claim, but it does not delete the weaker record. If current access is lost, prior observations remain provenance-preserving history and acquire source-unavailable or stale warnings; they do not masquerade as current state.
