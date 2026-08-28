# Golden fixture definitions

`cases.json` is a sanitized, offline definition of the ten historical case shapes named in the build plan. The labels preserve the intended recall checklist, but all tracker keys, project IDs, merge-request IDs, commit SHAs, actors, dates, text, and paths are synthetic.

`tests/test_golden_end_to_end.py` keeps all ten definitions parameterized and executes every case
through the real `LocalGitAdapter`, `JiraAdapter`, and `GitLabAdapter` using a temporary Git
repository and sanitized `httpx.MockTransport` responses. Adapter-emitted pages continue through
`import_snapshot`, SQLite persistence, reference and candidate rebuilds, append-only
confirmation/member decisions, packet and gap generation, and bounded read-only MCP tools. The
suite deliberately does not insert idealized evidence or packet rows directly.

The file is not evidence about any real contribution. A local operator must replace `known_records` through an authorized local corpus before using the cases for source-parity validation. Automated tests use only the synthetic identifiers and canonical source-qualified role names.

The expected result of a golden case is not an impressive story. It is:

- the candidate is found or the miss has a documented reason;
- precise participation roles remain intact;
- forbidden ownership, release, and impact claims are absent;
- material statements cite evidence IDs; and
- incomplete or contradictory evidence produces gaps.
