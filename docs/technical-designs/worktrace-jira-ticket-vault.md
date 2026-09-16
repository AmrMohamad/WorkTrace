# Technical Design: Complete Jira Ticket Bundles and Encrypted Vault

**Status**: Design-only contract for parent [#46](https://github.com/AmrMohamad/WorkTrace/issues/46)

**Author**: Hisham (Tech Lead)

**Date**: 2026-09-16

**PRD**: [Complete Jira Ticket Bundles and Encrypted Vault](../prds/worktrace-jira-ticket-vault.md)

**Tracking**: [#47](https://github.com/AmrMohamad/WorkTrace/issues/47)

## Overview

This design extends WorkTrace with an encrypted, local Jira ticket vault while keeping the existing
SQLite evidence ledger, CLI mutation boundary, read-only TUI, and seven-tool MCP surface. It is an
additive foundation: no production code, dependency, or migration is included here. The design is
the implementation contract for #48, #49, #50, and the controlled rollout in #51.

The collection is not a Jira global snapshot. It is a versioned observation of selected roots and
their allowed one-hop context as visible to one authorized account at one fetch epoch. Every
resource has its own availability and completeness; a successful issue response never implies that
its comments, worklogs, properties, links, media, or attachments were all accessible.

## Governing contracts

- [Product contract](../product-contract.md), [evidence model](../evidence-model.md), and
  [source authority](../source-authority.md) remain authoritative for attribution and claims.
- [Threat model](../threat-model.md) gains the vault trust boundary and attachment controls.
- [AgDR-0008](../agdr/AgDR-0008-jira-ticket-vault-migration.md) records the material schema,
  crypto, dependency, recovery, and rollback decisions.
- Assignment is a relationship, not ownership. Direct context is never participation. A Jira
  status, attachment, comment, or link cannot prove implementation, release, deployment, impact,
  or measurable success.

## Domain model and identity

### Collection identity

`jira_collection_id = jcol:<base32url(HMAC-SHA256(collection_id_key, site_canonical))>` is the
ledger-facing site identity. The site canonical form is the normalized HTTPS origin (lowercase
host, default port removed, path normalized, no query/fragment). It is not an app ID and must not
contain credentials. A collection's logical issue identity is:

```text
jira_issue_identity = <site_canonical, numeric_issue_id>
jira_issue_object_id = jira:<site_fingerprint>:issue:<numeric_issue_id>
```

The numeric ID, not a mutable key or application map, is the deduplication key. The latest observed
key, project key, and canonical web URL are metadata. If a key is renamed or an issue appears under
another configured app, the same issue object remains one identity; app mappings are separate
read-model associations and never split collection identity.

### Root selection

The configured interval is a half-open UTC range derived from local dates `2024-01-28` through
`2026-09-06` in the configured IANA timezone: local start-of-day through local start-of-day after
the end date. The selector records timezone, local dates, derived UTC bounds, provider calendar
metadata, and policy version in the collection manifest.

Discovery is intentionally two phase:

1. Query an expanded day range to avoid provider day-boundary and timezone loss. Persist each
   returned issue ID and discovery reason (`assigned`, `updated`, `created`, `explicit`, or
   `context`) without treating the broad query as proof of overlap.
2. For each candidate, retrieve the full assignment changelog and verify an interval where the
   configured account was assigned overlaps the configured interval. Open-ended predecessor or
   successor state, inaccessible changelog pages, or contradictory boundary data yields
   `boundary_unknown`; it is retained and never silently treated as in- or out-of-scope.

Only verified assignment roots become `root` members. Explicit exact-key selection is allowed only
when separately authorized and is labelled `explicit_root`, not assignment. A root's current full
context is collected outside the historical interval: selection dates bound root eligibility, not
the hydration of current fields or resources.

### Allowed context edges

For each selected root, resolve exactly one hop from the root's current issue response:

| Edge | Allowed target | Evidence meaning |
|---|---|---|
| `jira_parent_of` / `jira_subtask_of` | Parent or true subtask | Structural Jira context |
| `jira_issue_link:<type>` | Either endpoint of a typed Jira issue link | Source-declared context |

Targets may be in any accessible Jira project on the same site. There is no second-hop traversal,
project-wide expansion, JQL-by-title expansion, remote-link crawling, or text-mention traversal.
An inaccessible target remains a typed reference with endpoint identity, key if known, and
`unavailable` endpoint status. Context membership never creates a participation, actor, candidate
root, assignment interval, or ownership claim. Remote links are recorded as metadata (URL is
redacted before persistence); their targets are never fetched.

Jira issue links are direction-labelled at the UI/API representation, but the underlying relation
is bidirectional. Preserve the link type ID/name and inward/outward labels without inventing a
semantic arrow beyond the returned endpoint fields. See the [Jira issue linking model](https://developer.atlassian.com/cloud/jira/platform/issue-linking-model/).

## Resource inventory and completeness

Each resource is a first-class row with a stable locator and independent state. The required
resource kinds are:

| Resource kind | Locator | Required capture |
|---|---|---|
| `issue_fields` | issue ID + API revision | All returned fields and custom fields, normalized plus encrypted raw JSON |
| `comments` | issue ID + page/start cursor + comment ID | All accessible pages; ADF/body, visibility, author, create/update data |
| `changelog` | issue ID + page/cursor + history ID | Every returned history and every field item, including non-assignment fields |
| `worklogs` | issue ID + page/start + worklog ID | All accessible pages, properties, visibility and ADF comment |
| `issue_properties` | issue ID + property key | Key listing and each accessible value |
| `issue_links` | issue ID + link ID | Typed endpoints and labels |
| `remote_links` | issue ID + link ID | Metadata only; no target crawl |
| `watchers_votes` | issue ID + endpoint | Accessible watcher/vote data and endpoint status |
| `attachments_manifest` | issue ID + attachment ID | Every returned attachment's metadata and manifest hash |
| `attachment_original` | attachment ID + vault object ID | Original bytes for every type or explicit unavailable outcome |
| `embedded_media` | issue/field/comment locator + media ID | Mapping to Jira-hosted media and accessibility; no implicit crawl |
| `context_issue` | site + numeric issue ID | Same inventory for each one-hop target, with `context` role |

Completeness values are `complete`, `partial`, `unavailable`, `not_requested`, `unsupported`,
`boundary_unknown`, and `unknown`. Each row records `expected_pages` when known, `pages_seen`,
`items_seen`, `source_updated_at`, `fetched_at`, HTTP outcome class, retry count, and a sanitized
error. `complete` means complete for that resource's requested scope, never complete for Jira as a
whole. Empty-but-successful resources are complete with zero items; a denied endpoint is
unavailable, not empty.

## Additive SQLite schema contract

The implementation adds a forward migration after the current schema. Existing tables and IDs are
unchanged. The following logical schema is normative; exact SQLite affinity may follow current
conventions.

### `jira_collections`

```text
id TEXT PRIMARY KEY                         -- jcol stable ID
site_canonical TEXT NOT NULL
site_fingerprint TEXT NOT NULL
identity_key_version INTEGER NOT NULL
app_id TEXT NULL REFERENCES apps(id)       -- optional association, not identity
scope_json TEXT NOT NULL                   -- dates, timezone, policy, project allowlist
manifest_hash TEXT NOT NULL
vault_id TEXT NOT NULL
status TEXT NOT NULL
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
activated_at TEXT NULL
```

### `jira_collection_issues`

```text
collection_id TEXT NOT NULL REFERENCES jira_collections(id)
issue_id TEXT NOT NULL                      -- numeric Jira ID as text
object_id TEXT NOT NULL                     -- stable source object ID
issue_key TEXT NOT NULL DEFAULT ''
role TEXT NOT NULL CHECK (role IN ('root','context','explicit_root'))
selection_reason_json TEXT NOT NULL
assignment_status TEXT NOT NULL
boundary_status TEXT NOT NULL
current_revision INTEGER NOT NULL DEFAULT 0
latest_updated_at TEXT NULL
PRIMARY KEY (collection_id, issue_id)
```

### `jira_resource_states`

```text
id TEXT PRIMARY KEY                           -- jres:<collection>:<issue>:<kind>:<locator_hash>
collection_id TEXT NOT NULL REFERENCES jira_collections(id)
issue_id TEXT NOT NULL
kind TEXT NOT NULL
locator_json TEXT NOT NULL                    -- page/cursor/id, canonicalized
role TEXT NOT NULL CHECK (role IN ('root','context','shared'))
state TEXT NOT NULL
completeness TEXT NOT NULL
availability TEXT NOT NULL
expected_count INTEGER NULL
seen_count INTEGER NOT NULL DEFAULT 0
page_cursor TEXT NULL
attempt INTEGER NOT NULL DEFAULT 0
raw_vault_object_id TEXT NULL
redaction_version TEXT NOT NULL
source_updated_at TEXT NULL
fetched_at TEXT NULL
error_json TEXT NULL
UNIQUE (collection_id, issue_id, kind, locator_json)
```

### `jira_attachment_objects` and `jira_search_chunks`

```text
jira_attachment_objects(
  id TEXT PRIMARY KEY,                         -- jatt:<site>:<numeric_attachment_id>
  collection_id TEXT NOT NULL,
  issue_id TEXT NOT NULL,
  attachment_id TEXT NOT NULL,
  filename TEXT NOT NULL,
  mime_type TEXT NOT NULL DEFAULT '',
  declared_size INTEGER NULL,
  manifest_sha256 TEXT NOT NULL,
  original_state TEXT NOT NULL,
  vault_object_id TEXT NULL,
  ciphertext_sha256 TEXT NULL,
  extracted_state TEXT NOT NULL,
  source_locator TEXT NOT NULL,
  UNIQUE (collection_id, attachment_id)
)

jira_search_chunks(
  id TEXT PRIMARY KEY,                         -- jchunk:<attachment>:<ordinal>
  collection_id TEXT NOT NULL,
  issue_id TEXT NOT NULL,
  attachment_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  locator_json TEXT NOT NULL,                  -- page, sheet, slide, or text range
  text_redacted TEXT NOT NULL,
  chars INTEGER NOT NULL,
  extraction_version TEXT NOT NULL,
  UNIQUE (attachment_id, ordinal)
)
```

Raw issue resource JSON, raw ADF, raw changelog pages, and original attachment bytes never enter
SQLite. SQLite stores only redacted normalized metadata, status/error summaries, hashes, stable
locators, and extracted redacted chunks. The vault manifest is immutable once the collection
revision is activated.

## Vault format and key lifecycle

### Encrypted object format

Every raw payload/original is one independent secretstream object. The immutable descriptor is a
canonical UTF-8 JSON object with sorted keys and no secrets:

```json
{
  "format": "worktrace-jira-vault-object",
  "format_version": 1,
  "collection_id": "jcol:...",
  "object_id": "jatt:...",
  "kind": "attachment_original",
  "content_length": 1234,
  "content_sha256": "...",
  "key_version": 1,
  "chunk_size": 1048576
}
```

The descriptor bytes are associated data (AAD) on every chunk and are stored in the immutable
vault manifest with object path, ciphertext hash, key version, and finalization time. The file
header stores a magic value, format version, descriptor length/bytes, secretstream header, then
authenticated ciphertext chunks. Chunks use `TAG_MESSAGE` except the final chunk, which must use
`TAG_FINAL`; decryption must reject EOF without a valid final tag, any AAD mismatch, descriptor
mismatch, ciphertext hash mismatch, or trailing bytes. Use PyNaCl's libsodium binding to
`crypto_secretstream_xchacha20poly1305_*`; libsodium documents the header, authenticated chunks,
final tags, and rekey tags in its [secretstream contract](https://libsodium.gitbook.io/doc/secret-key_cryptography/secretstream).

The object writer streams to a private temporary path, fsyncs, atomically renames only after the
final tag and ciphertext hash are verified, then records the manifest row in a short SQLite
transaction. A partial file is never addressable as an original. Reads decrypt to a pipe or
explicit export destination; originals are not cached in SQLite or TUI memory beyond the bounded
stream buffer.

### Key storage and rotation

At installation, generate a random 32-byte vault key using the cryptographic library. Store it in
an explicit macOS Keychain backend through `keyring` with exactly:

```text
service = "WorkTrace Jira Vault"
account = "<installation-id>:<key-version>"
secret  = raw vault key bytes (encoded only as required by keyring API)
```

The implementation must verify that the selected backend is the macOS Keychain backend before key
creation/use. Missing, inaccessible, or ambiguous backends fail closed. There is no plaintext
file fallback, environment fallback, SQLite key column, or reuse of WorkTrace's email HMAC key.
The Keychain service/account descriptor is metadata only; it is never a vault secret.

Rotation creates a new key version, writes new objects with that version, and rewraps or rewrites
old objects only in an explicit maintenance operation after a complete backup. Both versions remain
available until all objects are re-encrypted and manifest-verified. Retire a version only after a
fresh restore test and explicit user confirmation; record lifecycle events without recording key
material. A missing retired version makes affected originals unavailable, not silently plaintext.

### Recovery export

`worktrace jira key-export` is intentionally not part of the public command set in this delivery;
the foundation unit may expose it only as an explicit, documented recovery subcommand after review.
The format is nevertheless fixed now: a versioned JSON envelope containing installation ID,
collection/vault IDs, key versions, KDF parameters, salt, nonce/header, and authenticated ciphertext
of the wrapped vault key. The passphrase derives a wrapping key with Argon2id using recorded memory,
iterations, and parallelism parameters; authenticated encryption covers the envelope descriptor and
wrapped key. The export never contains plaintext key material, is never logged, and refuses stdout,
overwrite, or an untrusted destination. Import requires an explicit fresh-install recovery flow,
validates the envelope and manifest bindings, and never merges into a non-empty vault automatically.

## Jira collection pipeline

The CLI orchestrator owns this sequence:

1. Validate credentials/origin, verified Jira account, configured site/project scope, interval,
   keychain backend, schema, vault directory permissions, free-space reserve, and transfer budget.
2. Create a collection in `paused`/`running` state with immutable scope and a manifest seed.
3. Discover expanded days and persist candidates; then fetch complete assignment changelog pages
   and classify roots (`verified_overlap`, `boundary_unknown`, or excluded with reason).
4. Fetch each selected root's current issue fields and one-hop allowed context. Schedule all
   resource families and persist resource state before fetching pages.
5. Fetch pages in bounded tasks. A resource's incomplete page stream starts from its first page on
   resume; a completed resource is idempotently skipped by stable locator/hash. At most two
   attachment downloads run concurrently. One SQLite writer serializes short transactions.
6. For every attachment, issue a GET to `/rest/api/3/attachment/content/{id}?redirect=false`.
   Reject all redirects and any response whose final credential origin is not the configured Jira
   origin. Do not send credentials to a redirect host. Stream, hash, encrypt, and commit the
   original only after final-tag verification.
7. After all resources, re-fetch the issue's `updated` value and attachment manifest. If either
   changed, retry the affected issue once from resource boundaries. If it changes again or cannot
   be compared, mark the revision `unstable_partial` and do not activate it.
8. Build redacted normalized projections and extraction jobs only from verified originals. Activate
   the revision only when all selected resources have terminal outcomes and the manifest is stable.

Default network policy is 30 seconds per request and three attempts for timeout/429/5xx only; 401,
403, 404, invalid redirect, and malformed responses are terminal for that resource. The invocation
  transfer budget defaults to 20 GiB and is adjustable only by an explicit CLI option. Keep a 2 GiB
  free-space reserve. Pause is durable at a resource boundary and stops scheduling new network
  work. A cancellation does not delete prior objects or rewrite statuses.

## Extraction and search

Extraction runs after encrypted preservation and has no Jira credentials, network, or child-process
execution capability. Use a subprocess with a sanitized environment, no shell, a private temporary
directory, and resource limits: 25 MiB input/decompressed parser stream, 60 seconds wall time,
512 MiB memory, 1,000 pages, 1,000,000 output characters. For OOXML, reject more than 10,000 ZIP
entries or 100 MiB inflated content; parse XML through `defusedxml`. Text extraction supports plain
text, Markdown, CSV, JSON, XML, and HTML, text PDFs, and DOCX/XLSX/PPTX. It does not OCR images or
transcribe audio/video. A parser failure or unsupported type leaves the encrypted original intact
and records `unsupported` or `failed` extraction state. PDF parsing must apply the input/page limits;
pypdf documents that content-stream parsing can have high memory cost ([text extraction guidance](https://github.com/py-pdf/pypdf/blob/main/docs/user/extract-text.md)).

Extracted text is normalized, redacted with the existing versioned redactor, bounded, and inserted
as chunks with source locator, attachment ID, extraction version, and character count. Search reads
only these redacted chunks and issue metadata. It never decrypts originals implicitly. Search readiness
is independent from original availability: an unsupported attachment can be preserved while search
is `unsupported`, and a failed extraction cannot be reported as no matching text.

## Public CLI and TUI contracts

The exact new command names are:

```text
worktrace jira collect
worktrace jira resume
worktrace jira status
worktrace jira search
worktrace jira show
worktrace jira attachment-export
worktrace ui --jira-collection
```

JSON output includes `schema_version`, stable IDs, `as_of`, collection scope, per-dimension status,
per-resource completeness, counts, continuation/next action, and limitations. It never includes
raw payloads, vault paths, keychain secrets, authorization values, original bytes, or unredacted
source text. `show` returns redacted metadata and extracted chunks only.

`attachment-export` requires a validated attachment ID belonging to the requested collection, an
explicit private output path, no-overwrite semantics, restrictive permissions, and a confirmation
flag appropriate to the CLI's existing safety conventions. It refuses stdout, paths inside the
ledger/vault, symlink destinations, automatic opening/launching, and provider URLs. Export is not
an evidence write and does not alter the vault.

The TUI launches only from `worktrace ui --jira-collection COLLECTION_ID`. It uses worker-local
SQLite `mode=ro` connections with `PRAGMA query_only=ON`, renders redacted metadata/chunks using the
existing literal terminal encoder, and exposes no vault key, original bytes, provider client,
network, writer, export, migration, or backup capability. It is query-only and does not change the
seven MCP signatures. MCP continues to return at most 20 records and no attachment/raw payload.

## State machines

Collection and resource dimensions are independent. The legal collection transitions are:

```text
new -> preflight_failed
new -> paused -> running -> paused
running -> partial | failed | complete | complete_with_unavailable_resources
partial -> running | failed | paused
paused -> running | failed
```

Only `running` may fetch. `complete*` is immutable except an explicit new revision; `failed` is
terminal for that attempt and may be superseded by a new attempt; `paused` is resumable with the
same scope/vault/config binding. A revision with `unstable_partial` cannot activate.

Resource states are:

```text
planned -> fetching -> complete
planned -> fetching -> unavailable | unsupported | failed | paused
fetching (stale after crash) -> planned
complete -> superseded (only by explicit stable revision)
```

`download_integrity=verified` requires ciphertext hash, plaintext hash, declared-size agreement
when available, and final tag. `original_availability=unavailable` is distinct from
`extraction=unsupported`. `search_readiness=ready` requires all requested searchable resources to
be successfully extracted and indexed; non-searchable resources do not block readiness.

## Backup, restore, compatibility, rollback

The CLI's vault-inclusive backup begins a coherent epoch: stop accepting new collection work,
quiesce the single DB writer at the current resource boundary, checkpoint/backup SQLite, capture
config and HMAC material through their existing protected path, then copy the immutable vault
manifest and ciphertext objects. The epoch manifest binds separate hashes for SQLite, configuration,
HMAC verifier material, vault manifest, ciphertext inventory, schema versions, and key versions. A
backup is valid only if every binding verifies; a paused resource remains paused and is not made
complete by backup.

Restore is to a fresh empty destination only. It verifies epoch metadata, SQLite integrity,
configuration/HMAC continuity, vault manifest, ciphertext hashes, keychain key versions, and schema
compatibility before opening the ledger. Missing key versions, mismatched HMAC/config, invalid
final tags, incomplete ciphertext, or non-empty destination causes fail-closed refusal. Restore
never deletes existing data, overwrites a destination, merges collections, or runs automatically.

Schema migration is forward-only and CLI-owned. Older binaries reject newer schema/vault formats;
newer binaries retain all prior observation and decision IDs. Migration must take a coherent backup
first, write additive tables, preserve current app-scoped authority, and expose migration status
without activating an incomplete Jira revision. Rollback means restoring the prior coherent epoch
to a fresh destination; it does not reverse individual decisions or silently delete new history.

## Module ownership seams

| Concern | Owning module for implementation | Explicit non-owner |
|---|---|---|
| Site/issue identity and selector | `importers/jira_selection.py` successor | App mapping, TUI |
| Jira endpoint paging and retries | `adapters/jira.py` successor | Vault crypto, MCP |
| Resource state/orchestration | `importers/jira_vault.py` | Adapter HTTP details, TUI |
| SQLite schema/repository | `db/migrations.py`, `db/repository.py` | Vault filesystem writer |
| Vault format/keychain | `vault/format.py`, `vault/keychain.py` | HMAC identity key, MCP |
| Extraction/indexing | `vault/extract.py`, `vault/search.py` | Network/provider, TUI |
| CLI contract | `cli.py` and focused command module | TUI internal subprocess |
| Query-only TUI | `tui/jira_collection.py` | Provider, writer, original export |
| MCP | existing seven tools/read models | Vault access and new signatures |
| Backup/restore | `db/backup.py`, `vault/backup.py` coordinator | Automatic scheduler |

No module may create a second source of truth for evidence, silently broaden context traversal, or
write a secret into SQLite/logs/arguments. New dependency use belongs only to #48's foundation.

## Implementation and acceptance map

| Unit | Issue | Deliverable | Required tests/evidence |
|---|---:|---|---|
| Foundation | [#48](https://github.com/AmrMohamad/WorkTrace/issues/48) | Additive schema; vault object format; Keychain backend; recovery envelope; extraction sandbox; epoch backup/restore | Crypto vectors including missing final tag/AAD; keychain fail-closed; migration/recovery; parser bombs/limits; fresh restore |
| Collection | [#49](https://github.com/AmrMohamad/WorkTrace/issues/49) | Root selection; assignment overlap; one-hop context; resource paging; attachment downloads; resume/pause; recheck/status | HTTP fixtures for all resource families, permission loss, redirect refusal, 2-download bound, 20 GiB/2 GiB budgets, unstable revision |
| Investigation | [#50](https://github.com/AmrMohamad/WorkTrace/issues/50) | Redacted extraction/search; CLI status/search/show/export; query-only TUI; wheel packaging | Search locator parity, unsupported originals, export path safety, TUI capability negatives, existing seven-tool MCP and TUI regressions |
| Rollout | [#51](https://github.com/AmrMohamad/WorkTrace/issues/51) | Controlled migration, pilot, authorized full collection, independent QA | Backup epoch readback, live Jira identity/permissions, pilot resource accounting, fresh restore, limitations and rollback report |

Acceptance is blocked if any material resource is silently omitted, context becomes participation,
an original is plaintext or in SQLite/MCP, a redirect receives credentials, a partial file is
presented as evidence, or a claim upgrades beyond source authority. Static/docs/fixture proof does
not establish live Jira parity or user authorization.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Jira permission or endpoint drift | Per-resource states, raw payload hashes, stable error classes, no global snapshot claim |
| Issue changes during collection | Final updated/attachment-manifest recheck, one retry, `unstable_partial` |
| Credential leakage through attachment redirect | `redirect=false`, origin allowlist, no credential forwarding on redirect |
| Malicious/bomb attachment | Encrypt first; isolated extractor with byte/time/memory/page/entry limits and `defusedxml` |
| Key loss | Explicit Keychain lifecycle, versioned recovery export, fresh-destination restore test |
| Partial DB/vault backup | Quiesced epoch and bound hashes for each artifact/key version |
| Vault disclosure via read surfaces | No MCP/TUI originals; explicit private no-overwrite export only |
| Resource fan-out and disk exhaustion | Two downloads, short transactions, transfer budget, 2 GiB reserve, durable pause |
| Attribution overreach | Root/context roles, existing evidence model, no participation from links/attachments |

## Reversal triggers and unresolved rollout questions

Revisit this design only if Jira's current API no longer supplies a required resource, libsodium/
PyNaCl cannot provide the specified authenticated stream, macOS Keychain cannot meet the explicit
backend contract, or measured fixture/live volumes invalidate the stated bounds. A future change
must add a successor AgDR and preserve old encrypted objects or mark them unavailable. Live tenant
permissions, actual custom-field/embedded-media shapes, and collection volume are intentionally
unverified until #51.

## Sources

- [Jira REST v3 introduction](https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro) for expansion, pagination, ADF, and v3 contract.
- [Issue attachments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/) for content, metadata, range responses, and permission outcomes.
- [Issue comments](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-comments/) for paginated comments and visibility restrictions.
- [Issue changelogs](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/) for oldest-first paginated histories and field items.
- [Issue worklogs](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-worklogs/) and [issue properties](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-properties/) for bounded resource retrieval and permissions.
- [Jira issue linking model](https://developer.atlassian.com/cloud/jira/platform/issue-linking-model/) for link direction and endpoint interpretation.
- [libsodium secretstream](https://libsodium.gitbook.io/doc/secret-key_cryptography/secretstream) and [PyNaCl bindings](https://github.com/pyca/pynacl/blob/main/src/nacl/bindings/crypto_secretstream.py) for the authenticated stream primitive.
- [keyring documentation](https://keyring.readthedocs.io/en/stable/) for macOS Keychain support, API, and backend security considerations.
- [pypdf extraction guidance](https://github.com/py-pdf/pypdf/blob/main/docs/user/extract-text.md) and [defusedxml security notes](https://github.com/tiran/defusedxml/blob/main/README.md) for parser limits and XML-bomb controls.
