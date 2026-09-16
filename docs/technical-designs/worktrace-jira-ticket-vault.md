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

### Site, collection, run, revision, and ticket identity

Keep five identities distinct:

```text
site_id       = jira-site:<lowercase hex SHA-256(canonical HTTPS origin)>
collection_id = jcol:<UUIDv4>                         # one immutable requested scope/instance
run_id        = jrun:<UUIDv4>                         # one attempt against that instance
revision_id   = jrev:<collection_id>:<monotonic integer>
ticket_id     = jira:<site_id>:issue:<numeric Jira issue ID>
```

The site canonical form is the normalized HTTPS origin (lowercase host, default port removed,
normalized path, no query/fragment). Site identity is non-secret SHA-256, not HMAC and not a vault
key. Deterministic vector: `https://jira.example.test` →
`sha256=5521c7ed7714cbf69b5714241341c02057f405f72ee9a199333e98b3bac49f03` and
`https://jira.example.test/` canonicalizes to the same input. A query, fragment, credentials,
non-HTTPS origin, or non-default port is rejected rather than hashed.

`collection_id` identifies an instance and therefore never collides when the same site is collected
under a new scope or configuration. A run is an attempt; it cannot become authoritative by itself.
An activated revision is immutable and remains queryable after a later revision supersedes it. Every
resource state, attachment revision object, extracted chunk, vault object path, and vault manifest
row carries both `collection_id` and `revision_id` (and `run_id` where provenance requires it).
The provider attachment identity is separate:

```text
provider_attachment_id = jira:<site_id>:attachment:<numeric attachment ID>
revision_attachment_id = jatt:<collection_id>:<revision_id>:<provider_attachment_id>
```

The numeric issue ID, not a mutable key or app map, is the stable ticket deduplication key. Key,
project key, URL, and current fields are revision metadata. A renamed or remapped issue remains one
ticket identity while its historical revisions and optional app projections remain distinct.

The archive provenance rail is separate from the existing app-scoped `source_objects`,
`observations`, and `references` rail. Site-scoped archive evidence IDs are:

```text
jare:<site_id>:<collection_id>:<revision_id>:<resource_kind>:<locator_hash>
```

`jira_archive_app_associations` may project a redacted archive evidence ID into an `app_id` with an
explicit configured/derived reason. It has no foreign key back from archive resources into app
authority, candidates, or participation. Cross-project context can never affect app-scoped source
authority or candidate generation automatically.

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

### `jira_archive_sites`, `jira_collections`, `jira_collection_runs`, and revisions

```text
jira_archive_sites(
  id TEXT PRIMARY KEY,                       -- jira-site:<sha256(origin)>
  canonical_origin TEXT NOT NULL UNIQUE,
  hash_algorithm TEXT NOT NULL CHECK (hash_algorithm = 'sha256')
)

jira_collections(
  id TEXT PRIMARY KEY,                       -- jcol UUIDv4
  site_id TEXT NOT NULL REFERENCES jira_archive_sites(id),
  scope_json TEXT NOT NULL,                  -- immutable approved roots/context projects+issues
  scope_hash TEXT NOT NULL,
  approval_token_hash TEXT NOT NULL,
  policy_version INTEGER NOT NULL,
  config_fingerprint TEXT NOT NULL,
  vault_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  retired_at TEXT NULL
)

jira_collection_runs(
  id TEXT PRIMARY KEY,                       -- jrun UUIDv4
  collection_id TEXT NOT NULL REFERENCES jira_collections(id),
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT NULL,
  progress_json TEXT NOT NULL,
  error_json TEXT NULL
)

jira_archive_revisions(
  id TEXT PRIMARY KEY,                       -- jrev:<collection>:<integer>
  collection_id TEXT NOT NULL REFERENCES jira_collections(id),
  run_id TEXT NOT NULL REFERENCES jira_collection_runs(id),
  revision_number INTEGER NOT NULL,
  status TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  activated_at TEXT NULL,
  superseded_at TEXT NULL,
  UNIQUE (collection_id, revision_number)
)

jira_archive_app_associations(
  archive_evidence_id TEXT NOT NULL,
  app_id TEXT NOT NULL REFERENCES apps(id),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (archive_evidence_id, app_id)
)
```

### `jira_collection_issues`

```text
collection_id TEXT NOT NULL REFERENCES jira_collections(id)
revision_id TEXT NOT NULL REFERENCES jira_archive_revisions(id)
run_id TEXT NOT NULL REFERENCES jira_collection_runs(id)
issue_id TEXT NOT NULL                      -- numeric Jira ID as text
object_id TEXT NOT NULL                     -- ticket_id from the archive rail, not app source_objects
issue_key TEXT NOT NULL DEFAULT ''
role TEXT NOT NULL CHECK (role IN ('root','context','explicit_root'))
selection_reason_json TEXT NOT NULL
assignment_status TEXT NOT NULL
boundary_status TEXT NOT NULL
latest_updated_at TEXT NULL
PRIMARY KEY (revision_id, issue_id)
```

### `jira_resource_states`

```text
id TEXT PRIMARY KEY                           -- jres:<collection>:<revision>:<issue>:<locator_hash>
collection_id TEXT NOT NULL REFERENCES jira_collections(id)
revision_id TEXT NOT NULL REFERENCES jira_archive_revisions(id)
run_id TEXT NOT NULL REFERENCES jira_collection_runs(id)
archive_evidence_id TEXT NOT NULL UNIQUE
issue_id TEXT NOT NULL                      -- numeric Jira ID; ticket identity is site-scoped
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
UNIQUE (revision_id, issue_id, kind, locator_json)
```

### `jira_attachment_objects` and `jira_search_chunks`

```text
jira_attachment_objects(
  id TEXT PRIMARY KEY,                         -- jatt:<collection>:<revision>:<provider_id>
  collection_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  issue_id TEXT NOT NULL,
  attachment_id TEXT NOT NULL,                 -- provider_id, stable at site scope
  archive_evidence_id TEXT NOT NULL UNIQUE,
  filename TEXT NOT NULL,
  mime_type TEXT NOT NULL DEFAULT '',
  declared_size INTEGER NULL,
  manifest_sha256 TEXT NOT NULL,
  original_state TEXT NOT NULL,
  vault_object_id TEXT NULL,
  ciphertext_sha256 TEXT NULL,
  extracted_state TEXT NOT NULL,
  source_locator TEXT NOT NULL,
  UNIQUE (revision_id, attachment_id)
)

jira_search_chunks(
  id TEXT PRIMARY KEY,                         -- jchunk:<collection>:<revision>:<attachment>:<ordinal>
  collection_id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  issue_id TEXT NOT NULL,
  attachment_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  locator_json TEXT NOT NULL,                  -- page, sheet, slide, or text range
  text_redacted TEXT NOT NULL,
  chars INTEGER NOT NULL,
  extraction_version TEXT NOT NULL,
  UNIQUE (revision_id, attachment_id, ordinal)
)
```

Raw issue resource JSON, raw ADF, raw changelog pages, and original attachment bytes never enter
SQLite. SQLite stores only redacted normalized metadata, status/error summaries, hashes, stable
locators, and extracted redacted chunks. The vault manifest is immutable once its collection
revision is activated. Historical revisions remain queryable and are never rewritten in place.

The #49 implementation owns a dedicated archive seam rather than routing through app evidence
owners: `src/worktrace/archive/jira/selector.py` (selection/assignment),
`src/worktrace/archive/jira/provider.py` (HTTP/resource adapters), and
`src/worktrace/archive/jira/orchestrator.py` (run/revision/resource state). Existing
`source_objects`/`observations` imports may optionally receive a redacted projection only through
the explicit association table above.

## Vault format and key lifecycle

### Encrypted object format

Every raw payload/original is one independent secretstream object. The immutable descriptor is a
canonical UTF-8 JSON object with sorted keys and no secrets. Canonical encoding is UTF-8,
`json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)`, with no
leading/trailing whitespace. A parser rejects duplicate keys, invalid UTF-8, NaN/Infinity,
non-canonical numbers/escapes, unknown required-field versions, and any descriptor whose bytes
differ from canonical re-serialization. Descriptor length is a big-endian `u32`, bounded to
65,536 bytes.

```json
{
  "format": "worktrace-jira-vault-object",
  "format_version": 1,
  "site_id": "jira-site:...",
  "collection_id": "jcol:...",
  "revision_id": "jrev:jcol:...:1",
  "object_id": "jatt:jcol:...:jrev:...:jira-site:...:attachment:123",
  "kind": "attachment_original",
  "content_length": 1234,
  "content_sha256": "...",
  "key_version": 1,
  "chunk_size": 1048576
}
```

The normative binary grammar is big-endian and streaming-only:

```text
magic[4] = WTVA | version[1] = 0x01 | descriptor_len[u32]
descriptor[descriptor_len] | secretstream_header[24]
repeat { record_len[u32] | ciphertext[record_len] }
```

`record_len` must be `17..1,048,593` (`chunk_size=1,048,576` plus the 17-byte secretstream
overhead). The final record is mandatory even for a zero-byte plaintext: its ciphertext decrypts to
zero bytes and carries `TAG_FINAL`. Non-final records carry `TAG_MESSAGE`. A reader consumes one
length-prefixed record at a time, rejects malformed/oversized lengths, EOF/truncation, missing final
tag, any record after final, or trailing bytes, and never seeks or loads the object into memory.
Descriptor bytes are associated data (AAD) on every chunk and are stored in the immutable vault
manifest with object path, ciphertext hash, key version, and finalization time. Use PyNaCl's libsodium binding to
`crypto_secretstream_xchacha20poly1305_*`; libsodium documents the header, authenticated chunks,
final tags, and rekey tags in its [secretstream contract](https://libsodium.gitbook.io/doc/secret-key_cryptography/secretstream).

The object writer streams to a private temporary path, fsyncs, and atomically renames only after the
final tag and ciphertext hash are verified, then records the manifest row in a short SQLite
transaction. A partial file is never addressable as an original. Object paths are
`vault/<site_id>/<collection_id>/<revision_id>/<revision_attachment_id>.wtva`; path components are
generated IDs, never provider filenames. Reads decrypt to a pipe or explicit export destination;
originals are not cached in SQLite or TUI memory beyond the bounded stream buffer. Test vectors must
cover malformed length, duplicate/non-canonical descriptor, oversized record, empty object,
truncation, extra record, descriptor substitution, and record reordering.

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
The Keychain service/account descriptor is metadata only; it is never a vault secret. The key is
available only to the logged-in user session and the authorized Python executable under the
Keychain ACL; this is not protection from malware running as that same user. A same-host backup may
reference verified Keychain key versions, but a portable backup must not assume Keychain portability.

Rotation creates a new key version, writes new objects with that version, and rewraps or rewrites
old objects only in an explicit maintenance operation after a complete backup. Both versions remain
available until all objects are re-encrypted and manifest-verified. Retire a version only after a
fresh restore test and explicit user confirmation; record lifecycle events without recording key
material. A missing retired version makes affected originals unavailable, not silently plaintext.

### Recovery export

Recovery is an explicit part of the vault backup flow, not a new standalone public command. Its
version-1 envelope has this exact logical JSON shape and binary encoding:

```json
{
  "format": "worktrace-jira-recovery",
  "version": 1,
  "installation_id": "install:...",
  "site_ids": ["jira-site:..."],
  "collection_ids": ["jcol:..."],
  "vault_id": "vault:...",
  "epoch_id": "epoch:...",
  "key_versions": [1],
  "schema_version": 1,
  "kdf": {"name": "argon2id", "opslimit": 3, "memlimit": 67108864, "dk_len": 32},
  "aead": {"name": "xchacha20-poly1305-ietf"}
}
```

The JSON above is the descriptor only: it excludes ciphertext, salt, nonce, and any `aad_b64` or
other wrapper fields. Canonical JSON uses the vault descriptor rules. The binary file is
`WTRK` magic, version byte `0x01`, flags byte `0x00`, endian marker `0x4245` (big-endian),
descriptor length `u32` (maximum 8,192), exact canonical descriptor bytes, salt (exactly 16 random
bytes), nonce (exactly 24 random bytes), ciphertext length `u32` (maximum 4,096), and ciphertext
bytes. The descriptor binds installation ID, site IDs, collection/vault IDs, key versions, epoch
ID, and schema/vault format versions. The exact descriptor bytes are the AEAD AAD; they are not
encoded again as an envelope field. Salt, nonce, and ciphertext fields use unpadded base64url only
in diagnostic JSON, never as a second binary representation. Argon2id uses
PyNaCl's `opslimit` and `memlimit` parameters only; no parallelism field is accepted or used;
derive exactly 32 bytes. Accepted costs are `1 <= opslimit <= 10` and
`8 MiB <= memlimit <= 1 GiB`; the default is 3/64 MiB. Values outside this range, duplicate or
unknown fields, duplicate JSON keys, non-canonical encodings, oversized descriptor/ciphertext,
unsupported KDF/AEAD/version, or downgrade to a lower format are rejected before decryption.

The wrapping AEAD is PyNaCl `nacl.secret.Aead` XChaCha20-Poly1305-IETF with a 32-byte key and
the binary descriptor bytes as cryptographic AAD. The plaintext is a
versioned `WRAP` record containing `u32` key-version count followed by exactly one 32-byte vault
key per declared version; duplicate versions, extra bytes, or a key count above 64 are rejected.
The envelope never contains plaintext keys, is never logged, refuses stdout/overwrite/untrusted
paths, and requires an interactive passphrase entered twice (minimum 12 Unicode scalar values).
Passphrases are held only for the operation and best-effort cleared. Wrong passphrase, malformed
AAD, or authentication failure is indistinguishable to the caller and never creates partial state.

Portable epoch backups must include and hash a verified recovery envelope; same-host restore may use
the Keychain only when the required key versions are present, but still verifies the envelope/hash.
Import is explicit, validates all bindings, requires a fresh empty destination, and never merges,
deletes, or restores automatically. #48 must add a known-answer fixture containing fixed salt,
nonce, passphrase, KDF parameters, canonical descriptor bytes, ciphertext, and a decoder result;
also cover wrong passphrase and every reject case above without circular fields.

## Jira collection pipeline

The CLI orchestrator owns this sequence:

1. `collect-preview` freshly verifies `WORKTRACE_JIRA_*` credentials, canonical site origin, and
   Jira account identity, then validates the configured interval/timezone. It enumerates only
   bounded metadata/JQL roots and one-hop relationships across visible same-site projects; it does
   not hydrate raw resources or download attachments. The preview returns root/context IDs and
   project keys, unresolved refs, visible attachment estimates, policy, limitations, canonical
   `scope_hash`, and expiring `approval_token`.
2. `collect` requires `--approve-scope TOKEN`; validate the token against the provider-scope view,
   config fingerprint, scope hash, expiry, and policy. Reject forged/stale tokens and persist the
   approved project+issue set/policy in the immutable collection manifest. Create a collection
   instance, a run, and a new immutable revision row; only the run carries mutable progress.
3. Discover expanded days and persist candidates; then fetch complete assignment changelog pages
   and classify roots (`verified_overlap`, `boundary_unknown`, or excluded with reason), rejecting
   any root not in the approved set.
4. Fetch each approved root's current issue fields and one-hop allowed context. If provider changes
   reveal a new target/project, pause with `scope_expansion_required`, retain the unresolved ref,
   and require a new preview/token; never hydrate it automatically. Schedule all
   resource families and persist resource state before fetching pages.
5. Fetch pages in bounded tasks. A resource's incomplete page stream starts from its first page on
   resume; a completed resource is idempotently skipped by stable locator/hash. At most two
   attachment downloads run concurrently. One SQLite writer serializes short transactions.
6. For every attachment, use an `httpx` client configured with `trust_env=False` and
   `follow_redirects=False`, construct the path from validated numeric IDs and the configured
   origin, and issue GET `/rest/api/3/attachment/content/{id}?redirect=false`. Reject every 3xx
   before reading a body, proxy, or redirect. Credentials are sent only to the exact configured
   origin; no auth header is copied to another host. Stream, hash, encrypt, and commit the original
   only after final-tag verification. The adapter test seam must assert origin/path construction,
   `trust_env=False`, `follow_redirects=False`, and no credential/header reachability on 3xx.
7. After all resources, re-fetch the issue's `updated` value and attachment manifest. If either
   changed, retry the affected issue once from resource boundaries. If it changes again or cannot
   be compared, mark affected resource families `unstable`, retain prior completed resources and
   the pending unstable resource, set collection outcome `unstable_partial`, and do not activate it.
8. Build redacted normalized projections and extraction jobs only from verified originals. Activate
   the revision only when all selected resources have terminal outcomes and the manifest is stable.

Default network policy is 30 seconds per request and three attempts for timeout/429/5xx only; 401,
403, 404, invalid redirect, and malformed responses are terminal for that resource. The invocation
  transfer budget defaults to 20 GiB and is adjustable only by an explicit CLI option. Keep a 2 GiB
  free-space reserve. Pause is durable at a resource boundary and stops scheduling new network
  work. A cancellation does not delete prior objects or rewrite statuses.

## Extraction and search

Extraction runs after encrypted preservation. It must run under generated profile version `1` for
`sandbox-exec` on supported macOS hosts after a capability probe. The profile integrity hash is
SHA-256 over the exact profile bytes. At install/runtime,
resolve and hash the exact venv/interpreter executable, dyld/system libraries, Python stdlib,
WorkTrace worker module, and approved parser packages; the profile's immutable read/execute
allowlist contains only those resolved paths and denies every other file read/write, network,
subprocess, and process creation. The profile hash is recorded in the extraction status and checked
before each job. If the probe, profile hash, allowlist path, symlink check, executable hash, or
capability is invalid, save `extraction_unavailable` and retain the original; never run
unsandboxed. Input/output use only inherited stdin/stdout/stderr pipes: no plaintext temporary
fallback is permitted. The worker is the exact installed interpreter/module, uses `shell=False`, a
fixed empty working directory, an empty allowlist environment, and `FD_CLOEXEC` on all unrelated
descriptors. Apply 25 MiB
input/decompressed parser stream, 60 seconds wall time, 512 MiB memory, 1,000 pages, and 1,000,000
output-character limits. On timeout send TERM, wait briefly, KILL if needed, and reap; all pipe ends
close in `finally`. For OOXML, reject more than 10,000 ZIP entries or 100 MiB inflated content;
parse XML through `defusedxml`. Text extraction supports plain text, Markdown, CSV, JSON, XML, and
HTML, text PDFs, and DOCX/XLSX/PPTX. It does not OCR images or transcribe audio/video. A parser failure or unsupported type
leaves the encrypted original intact and records `unsupported` or `failed`. PDF parsing must apply
the input/page limits; pypdf documents that content-stream parsing can have high memory cost
([text extraction guidance](https://github.com/py-pdf/pypdf/blob/main/docs/user/extract-text.md)).

The worker protocol is also bounded and versioned. Parent sends raw bytes on stdin plus a small
canonical JSON header on a separate control pipe (`schema_version`, revision attachment ID, MIME
type, declared length, and limits); no path or provider filename is accepted. Worker stdout is one
canonical JSON result, with `status` in `complete|unsupported|limit|failed`, bounded redacted-free
text chunks, page/character counts, parser version, and no filesystem path. Exit codes are `0`
complete, `10` unsupported, `11` limit exceeded, `12` parser failure, and `13` sandbox/capability
failure; signal/timeout is mapped to `14` after TERM/KILL/reap. The parent redacts stdout before
SQLite insertion and never treats `unsupported`, `limit`, or `failed` as an empty match.

Extracted text is normalized, redacted with the existing versioned redactor, bounded, and inserted
as chunks with source locator, attachment ID, extraction version, and character count. Search reads
only these redacted chunks and issue metadata. It never decrypts originals implicitly. Search readiness
is independent from original availability: an unsupported attachment can be preserved while search
is `unsupported`, and a failed extraction cannot be reported as no matching text.

## Public CLI and TUI contracts

The exact new command names are:

```text
worktrace jira collect-preview --scope assigned-during-employment --context-depth 1 --config CONFIG
worktrace jira collect --scope assigned-during-employment --context-depth 1 --attachments all --config CONFIG --approve-scope TOKEN
worktrace jira resume
worktrace jira status
worktrace jira search
worktrace jira show
worktrace jira attachment-export
worktrace ui --jira-collection
```

The fixed collection command prefix is
`worktrace jira collect --scope assigned-during-employment --context-depth 1 --attachments all --config CONFIG`;
`--approve-scope TOKEN` is mandatory and is the only additional approval argument. Preview JSON is
bounded metadata only:

```json
{
  "schema_version": 1,
  "site_id": "jira-site:...",
  "account_id": "jira-account:...",
  "interval": {"from": "2024-01-28", "to": "2026-09-06", "timezone": "Area/City"},
  "policy_version": 1,
  "roots": [{"issue_id": "10001", "project_key": "DEMO"}],
  "context": [{"issue_id": "10002", "project_key": "OTHER", "relationship": "blocks"}],
  "unresolved_refs": [],
  "attachment_estimate": {"visible_count": 2, "visible_bytes": 4096},
  "limitations": [],
  "scope_hash": "sha256:...",
  "approval_token": "scope:..."
}
```

Preview does not return issue bodies, comments, changelogs, raw payloads, or attachment bytes.
Its `scope_hash` covers the canonical site/account, approved root/context project+issue set,
policy, interval/timezone, and provider-scope view. Any change invalidates the token.
Preview tests must cover forged/stale tokens, injected projects, changed context, partial or
inaccessible preview enumeration, and an HTTP assertion that no content or attachment download is
performed.

JSON output includes `schema_version`, stable IDs, `as_of`, collection scope, per-dimension status,
per-resource completeness, counts, continuation/next action, and limitations. It never includes
raw payloads, vault paths, keychain secrets, authorization values, original bytes, or unredacted
source text. `show` returns redacted metadata and extracted chunks only.

`attachment-export` requires a validated attachment ID belonging to the requested collection, an
explicit private output path, no-overwrite semantics, restrictive permissions, and a confirmation
flag appropriate to the CLI's existing safety conventions. It refuses stdout, paths inside the
ledger/vault, symlink destinations, automatic opening/launching, and provider URLs. Export is not
an evidence write and does not alter the vault.

The exact vault portability commands are:

```text
worktrace jira backup COLLECTION_ID --output NEW_DIRECTORY --yes
worktrace jira restore --input EPOCH_DIRECTORY --destination FRESH_DIRECTORY --yes
```

`jira backup` requires an explicit non-existing output directory and `--yes`; it quiesces at a
resource boundary and emits a JSON epoch manifest. `jira restore` requires an explicit non-existing
or empty fresh destination and `--yes`; it verifies the epoch, recovery-envelope hash, and all
bindings before opening the ledger. `worktrace backup` remains the existing DB-only operation and
must print a warning when Jira vault state exists; it never implies vault portability.

The public JSON envelopes are exact at the contract level (additional diagnostic fields are not
permitted without a schema-version bump):

```json
{
  "schema_version": 1,
  "collection_id": "jcol:...",
  "run_id": "jrun:...",
  "revision_id": "jrev:jcol:...:1",
  "collection_outcome": "complete_with_unavailable_resources",
  "status": "complete_with_unavailable_resources",
  "dimensions": {
    "selection": "complete",
    "enumeration": "complete",
    "original_availability": "complete_with_unavailable_resources",
    "download_integrity": "complete_with_unavailable_resources",
    "extraction": "complete_with_unavailable_resources",
    "search_readiness": "ready",
    "app_mapping": "not_configured"
  },
  "resource_counts": {"complete": 10, "partial": 0, "unavailable": 1, "unstable": 0},
  "as_of": "2026-09-16T12:00:00Z",
  "next_action": null,
  "limitations": []
}
```

An unstable status response sets both `collection_outcome` and `status` to `unstable_partial`,
reports affected resources under `resource_counts.unstable`, sets `next_action` to `resume`, and
has no activated revision for the attempt. The collection outcome is never placed in resource counts.

`jira backup` emits `{ "schema_version": 1, "epoch_id": "epoch:<UUIDv4>",
"collection_id": "jcol:...", "revision_id": "jrev:...", "sqlite_sha256": "...",
"config_binding_sha256": "...", "hmac_binding_sha256": "...", "vault_manifest_sha256":
"...", "ciphertext_count": 10, "key_versions": [1], "recovery_envelope_sha256": "...",
"complete": true }`. `jira restore` emits `{ "schema_version": 1, "epoch_id": "epoch:...",
"destination": "<redacted-basename>", "verified": true, "status": "restored" }` and never
prints the absolute destination, keys, paths, or plaintext. A refusal emits the same versioned
envelope with `verified: false`, a stable error code, and a sanitized limitation; it performs no
partial restore.

Export uses a trusted existing parent chain with no symlink components. Prefer descriptor-relative
`openat`/`O_NOFOLLOW`; where unavailable, resolve and recheck every parent with `lstat`, refuse
symlinks/races, and document the weaker portable fallback. Create a generated-ID filename under a
same-directory temp path with `O_CREAT|O_EXCL|O_NOFOLLOW`, mode 0600; fsync, atomically rename with
no replacement, then fsync the parent. Provider filenames never become paths.

The TUI accepts the additive `--jira-collection COLLECTION_ID` option alongside the existing
`--app APP_ID` and `--candidate CANDIDATE_ID` options. `--jira-collection` is mutually exclusive
with both existing options; without it the existing app/candidate TUI behavior is preserved. The
TUI launches a Jira collection view only from `worktrace ui --jira-collection COLLECTION_ID`. It
uses worker-local
SQLite `mode=ro` connections with `PRAGMA query_only=ON`, renders redacted metadata/chunks using the
existing literal terminal encoder, and exposes no vault key, original bytes, provider client,
network, writer, export, migration, or backup capability. It is query-only and does not change the
seven MCP signatures. MCP continues to return at most 20 records and no attachment/raw payload.

## State machines

Collection and resource dimensions are independent. The legal collection transitions are:

```text
new -> preflight_failed
new -> paused -> running -> paused
running -> rechecking | partial | failed
rechecking -> complete | complete_with_unavailable_resources | partial | failed | unstable_partial
partial -> running | failed | paused
paused -> running | failed
unstable_partial -> running | failed
```

Only `running` may fetch and `rechecking` may perform the final version/manifest comparison. A
recheck that changes again enters public terminal state `unstable_partial`, while each affected
resource enters `unstable`; prior completed resources remain complete and the unstable resource is
pending. `unstable_partial` resumes through `resume` by creating a new run and revision attempt,
resetting and refetching affected issue fields, attachment manifests, originals, and derived chunks
before a new recheck; it never jumps directly to rechecking and is never complete or successful.
`complete*` is immutable except an explicit new revision; `failed` is terminal for
that attempt and may be superseded by a new attempt; `paused` is resumable with the same binding.

Exit codes are part of the public contract: `0` for `complete` or
`complete_with_unavailable_resources`; `2` for `paused`, `partial`, or `unstable_partial` requiring
user action; `1` for failed/preflight/integrity/security refusal; and `3` for invalid CLI input or
incompatible schema/format. `status` uses the same mapping and always emits JSON when requested.

Resource states are:

```text
planned -> fetching -> complete
planned -> fetching -> unavailable | unsupported | failed | paused
rechecking -> unstable
unstable -> planned
fetching (stale after crash) -> planned
complete -> superseded (only by explicit stable revision)
```

`download_integrity=verified` requires ciphertext hash, plaintext hash, declared-size agreement
when available, and final tag. `original_availability=unavailable` is distinct from
`extraction=unsupported`. `search_readiness=ready` requires all requested searchable resources to
be successfully extracted and indexed; non-searchable resources do not block readiness.

## Backup, restore, purge, compatibility, rollback

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

The shipped `worktrace purge --yes` signature remains unchanged only when no Jira collection, vault,
or key references exist. If any exist, it fails before deleting the database, HMAC material, or
backups and instructs the user to use the Jira command. Jira purge is
`worktrace jira purge COLLECTION_ID --include-vault --yes`: it quiesces active jobs, verifies no
backup is in progress, computes shared manifest references, deletes collection projections and only
unreferenced ciphertexts, retires only unreferenced Keychain versions, and reports logical deletion
(not secure erasure). Backup retention is an explicit user choice; no backup is deleted implicitly.
A whole-installation vault purge requires a separate explicit command/flag and never follows legacy
purge implicitly. Without `--include-vault --yes`, Jira purge cannot touch vault objects.

Schema migration is forward-only and CLI-owned. Older binaries reject newer schema/vault formats;
newer binaries retain all prior observation and decision IDs. Migration must take a coherent backup
first, write additive tables, preserve current app-scoped authority, and expose migration status
without activating an incomplete Jira revision. Rollback means restoring the prior coherent epoch
to a fresh destination; it does not reverse individual decisions or silently delete new history.

## Module ownership seams

| Concern | Owning module for implementation | Explicit non-owner |
|---|---|---|
| Site/issue identity and selector | `src/worktrace/archive/jira/selector.py` (#49) | Legacy `importers/jira_selection.py`, app mapping, TUI |
| Jira endpoint paging and retries | `src/worktrace/archive/jira/provider.py` (#49) | Legacy `adapters/jira.py`, vault crypto, MCP |
| Resource state/orchestration | `src/worktrace/archive/jira/orchestrator.py` (#49) | Legacy importers, adapter HTTP details, TUI |
| SQLite archive schema/repository/models | `src/worktrace/archive/jira/models.py`, `db/migrations.py`, `db/repository.py` (#49) | Legacy app source rail, vault filesystem writer |
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
| Collection | [#49](https://github.com/AmrMohamad/WorkTrace/issues/49) | Preview approval, root selection; assignment overlap; one-hop context; resource paging; attachment downloads; resume/pause; recheck/status | Preview forged/stale-token, project-injection, changed-context, partial/inaccessible, and no-content-download tests; HTTP fixtures for all resource families, permission loss, redirect refusal, 2-download bound, 20 GiB/2 GiB budgets, unstable revision, legacy/new purge compatibility and shared-reference accounting; adversarial A→B→C→C refetch proves C is not mixed into activated A/B |
| Investigation | [#50](https://github.com/AmrMohamad/WorkTrace/issues/50) | Redacted extraction/search; CLI status/search/show/export; query-only TUI; wheel packaging | Search locator parity, parser JSON/exit contract and sandbox negatives, unsupported originals, export path safety, TUI capability negatives, existing seven-tool MCP and TUI regressions |
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
