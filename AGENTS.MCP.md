# WorkTrace MCP module

Load for MCP server code, tool schemas, limits, configuration, or Codex integration.

- The MCP process opens SQLite read-only and receives no Jira/GitLab credentials.
- Expose exactly six tools: `list_contribution_candidates`, `get_contribution_summary`, `build_phase4_packet`, `list_evidence_gaps`, `search_evidence`, and `get_evidence_excerpt`.
- Inputs accept stable app/contribution/evidence IDs, filters, cursors, and bounded limits—never paths, commands, URLs to follow, or SQL.
- Enforce configured app/source scope server-side. A server instruction is guidance, not an access-control boundary.
- Return redacted structured data with stable evidence IDs, `as_of`, source completeness, staleness, contradictions, and limitations.
- Label excerpts as `untrusted_source_excerpt`; never concatenate source text into server instructions or execute/follow it.
- Maximum records: 20. Default excerpt: 1,200 characters. Explicit excerpt: 4,000. Total serialized response text: 20,000 characters. No attachments or complete diffs.
- Every response uses one short query-only SQLite snapshot. Return a view token and validate optional expected_view_token in that snapshot; tokens certify consistency, not authority or readiness.
- Candidate/search pages process at most 200 raw matching rows. Advance versioned continuation only past excluded or delivered records; never trim admitted rows after cursor calculation. Legacy offset cursors require restart.
- Preserve all 30 Phase 4 v2 question identities/statuses and contradiction signals when compacting. Section/question detail retrieval remains within build_phase4_packet, with view-bound continuations.
- The broader excerpt tool is documented for prompt approval in Codex configuration; the other five tools remain read-only automatic reads.
- MCP unit tests prove scope, stable-ID validation, read-only behavior, redaction, and output limits. A direct tool call proves only the exercised local database/server path.
