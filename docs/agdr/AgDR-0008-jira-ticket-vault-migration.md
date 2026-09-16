# AgDR-0008: Jira ticket vault, encrypted originals, and additive migration

## Status

**Proposed.** This record is authored with the design for [#47](https://github.com/AmrMohamad/WorkTrace/issues/47)
and must receive independent architecture and security approval before Build. It authorizes no
production implementation, dependency change, migration, provider access, or live collection.

## Context

WorkTrace currently stores a bounded, redacted Jira evidence projection and deliberately excludes
attachments. Parent feature [#46](https://github.com/AmrMohamad/WorkTrace/issues/46) requires complete
accessible ticket context, encrypted attachment originals of every type, bounded extraction, and
offline investigation while retaining WorkTrace's provenance and authority rules.

The material decisions are the storage boundary, a new cryptographic stream format, OS key
ownership/recovery, parser dependencies, and an additive schema/migration/backup contract. This
record explicitly supersedes the earlier product/security wording that prohibited attachment
persistence, but only for encrypted originals outside SQLite and only within an explicitly selected
Jira collection. It does not supersede the prohibition on raw payloads or originals in SQLite, MCP,
TUI, exports, logs, or credentials.

## Options considered

| Decision | Options | Choice and reason |
|---|---|---|
| Original storage | Drop originals; plaintext files; SQLite blobs; encrypted vault | Encrypted vault outside SQLite preserves every type without expanding MCP/ledger disclosure |
| Encryption | Home-grown AES/HMAC; per-file ad hoc AEAD; libsodium secretstream | PyNaCl/libsodium secretstream provides authenticated chunks, explicit final tag, and stream recovery semantics |
| Key owner | HMAC key reuse; plaintext config; OS Keychain; passphrase-only vault | Dedicated random key in explicit macOS Keychain backend; no plaintext fallback or HMAC reuse |
| Recovery | No recovery; plaintext export; passphrase-wrapped versioned envelope | Explicit Argon2id + authenticated encrypted recovery envelope and fresh-destination restore |
| Extraction | In-process unrestricted parsers; OCR/transcription; bounded subprocess | Credential/network-free bounded subprocess; supported text/PDF/OOXML only; no OCR/transcription |
| Migration | Replace tables; dual ledger; additive schema | Additive forward migration, coherent backup first, old IDs/decisions preserved |

## Decisions

### 1. Identity and selection

Keep site identity, collection instance, run, activated revision, ticket identity, and provider
attachment identity distinct:

```text
site_id       = jira-site:<lowercase hex SHA-256(canonical HTTPS origin)>
collection_id = jcol:<UUIDv4>
run_id        = jrun:<UUIDv4>
revision_id   = jrev:<collection_id>:<monotonic integer>
ticket_id     = jira:<site_id>:issue:<numeric issue ID>
attachment_id = jira:<site_id>:attachment:<numeric attachment ID>
```

Site identity is non-secret SHA-256; no `collection_id_key` exists and no HMAC/vault-key reuse is
permitted. The deterministic vector for canonical `https://jira.example.test` is
`5521c7ed7714cbf69b5714241341c02057f405f72ee9a199333e98b3bac49f03`; a trailing slash canonicalizes
to the same origin. Credentials, query/fragment, non-HTTPS, and non-default ports are rejected.
The collection is an instance, the run is an attempt, and the activated revision is immutable and
queryable after supersession. Every resource, attachment revision object, chunk, vault object path,
and manifest carries collection+revision IDs. Archive evidence IDs are site-scoped and live on a
separate provenance rail from app `source_objects`/observations/references; an optional app
association cannot affect app authority or candidates.

Roots are verified assignment intervals overlapping configured local dates `2024-01-28..2026-09-06`
in the configured timezone. Discovery first queries an expanded day range, then retrieves full
assignment changelogs to verify overlap. `boundary_unknown` is retained when endpoints or pages are
inaccessible/ambiguous. Once selected, the current full accessible ticket context is collected
without restricting fields/resources to the historical interval. Direct context is exactly one hop
through parent, true subtask, or typed Jira issue-link edges across any accessible project. Remote
links are metadata only and are never crawled. Context does not create participation.

### 2. Resource and SQLite boundary

Every issue resource has an independent state and completeness row: all returned fields/custom
fields, every comment page, every changelog field/history, worklogs/properties, typed links,
remote-link metadata, accessible watcher/vote data, attachment manifest, attachment original, and
embedded Jira media mapping. Empty success is complete with zero items; denied/deleted/omitted
resources are explicit unavailable/partial states.

Raw structured payloads and original bytes are encrypted files outside SQLite. SQLite stores only
redacted metadata, hashes, stable locators, statuses/errors, and extracted redacted chunks. All
attachment MIME types are eligible for preservation; unsupported extraction never deletes the
original. MCP and TUI cannot decrypt or return originals.

The additive migration introduces site/collection/run/revision tables plus
`jira_collection_issues`, `jira_resource_states`, `jira_attachment_objects`, and
`jira_search_chunks` as specified in the technical design. Existing source-object, observation,
participation, decision, app, and read-revision IDs and semantics remain unchanged. A dedicated
`archive/jira` provider/selector/orchestrator seam owns #49; it does not reuse app-scoped authority
tables as its archive source of truth.

### 3. Vault object format

Each raw resource/original is an independent versioned object with a canonical immutable descriptor
and associated data. Canonical JSON is UTF-8, sorted keys, separators `,`/`:`, `ensure_ascii=true`,
`allow_nan=false`, no whitespace, and rejects duplicate keys, invalid UTF-8, non-canonical numbers/
escapes, unknown fields, and reserialization mismatch. Descriptor length is a big-endian `u32`
bounded to 65,536 bytes:

```text
magic[4] = WTVA | version[1] = 0x01 | descriptor_len[u32]
descriptor[descriptor_len] | secretstream_header[24]
repeat { record_len[u32] | ciphertext[record_len] }
```

Descriptor fields include format, collection/revision/object IDs, kind, plaintext length/hash, key
version, and chunk size. `record_len` is big-endian and must be `17..1,048,593` for 1 MiB
plaintext chunks. There is one mandatory zero-byte final record for an empty plaintext; all other
records use `TAG_MESSAGE`, and the last uses `TAG_FINAL`. Parsing is streaming-only: reject malformed
or oversized lengths, truncation, missing final tag, any record after final, and trailing bytes.
Descriptor bytes are AAD on every chunk. Chunks use
`crypto_secretstream_xchacha20poly1305` with `TAG_MESSAGE` except the final chunk, which must use
`TAG_FINAL`. A reader rejects a missing final tag, altered descriptor/AAD, invalid tag, plaintext
hash/length mismatch, ciphertext hash mismatch, malformed header, or trailing bytes. The writer
fsyncs and atomically renames only after finalization. The [libsodium secretstream documentation](https://libsodium.gitbook.io/doc/secret-key_cryptography/secretstream)
defines headers, authenticated chunks, final tags, and explicit rekey tags; PyNaCl exposes the
binding and constants in its [source](https://github.com/pyca/pynacl/blob/main/src/nacl/bindings/crypto_secretstream.py).

### 4. Keychain, recovery, and lifecycle

Generate a dedicated random vault key. Use the explicit macOS Keychain backend through `keyring`:

```text
service = WorkTrace Jira Vault
account = <installation-id>:<key-version>
```

The implementation verifies the selected backend before use and fails closed if it is unavailable
or ambiguous. There is no plaintext fallback, environment fallback, SQLite key column, or reuse of
the email HMAC key. Keychain access is limited to the logged-in user session and authorized Python
executable; it does not protect against malware running as that user. `keyring` documents the macOS
Keychain backend and its access-control caveat in
its [security considerations](https://keyring.readthedocs.io/en/stable/).

Rotation creates a new version, writes/re-encrypts objects explicitly, verifies all manifests, and
retains old versions until a fresh restore test and explicit retirement. No key material is logged
or persisted in the ledger.

Recovery is part of the explicit backup flow. Envelope v1 uses canonical JSON descriptor fields
`format=worktrace-jira-recovery`, `version=1`, Argon2id `opslimit`/`memlimit` only (no parallelism),
`dk_len=32`, 16-byte salt, and PyNaCl `nacl.secret.Aead` XChaCha20-Poly1305-IETF with a 24-byte
nonce and descriptor bytes as cryptographic AAD. The descriptor excludes ciphertext, salt, nonce,
`aad_b64`, and other wrapper fields. On-disk grammar is `WTRK | 0x01 | flags=0x00 | endian=0x4245 |
descriptor_len[u32] <=8192 | descriptor | salt[16] | nonce[24] | ciphertext_len[u32] <=4096 |
ciphertext`. Reject downgrade, unknown/duplicate fields, duplicate versions, non-canonical bytes,
oversize, wrong passphrase, or authentication failure. Salt, nonce, and ciphertext use unpadded
base64url only in diagnostic JSON, never as a second binary representation. Accepted costs are
opslimit 1..10 and memlimit 8 MiB..1 GiB (default 3/64 MiB). Require a twice-entered passphrase of
at least 12 Unicode scalar values; never log or persist it. Import is explicit, validates all
bindings, requires a fresh empty destination, and never merges or deletes. #48 must include a
known-answer fixture with fixed salt, nonce, passphrase, KDF parameters, descriptor bytes,
ciphertext, and decoder result; no circular fields.

### 5. Dependencies and parser safety

The future foundation dependency group is `PyNaCl`, `keyring`, `pypdf`, and `defusedxml` only. The
group is not added by this design PR. PyNaCl supplies the libsodium binding rather than inventing a
crypto primitive; keyring supplies the OS backend abstraction; pypdf provides text extraction but
warns that PDF content streams can have high memory cost ([guidance](https://github.com/py-pdf/pypdf/blob/main/docs/user/extract-text.md));
defusedxml addresses entity expansion and external-resource hazards ([security notes](https://github.com/tiran/defusedxml/blob/main/README.md)).
All are permissively licensed, established Python packages with narrow responsibilities. A future
implementation records exact versions, licenses, advisories, and lockfile changes in #48.

Extraction runs without credentials, network, or child execution. Enforce 25 MiB input/decompressed
stream, 60 seconds, 512 MiB memory, 1,000 pages, 1,000,000 output characters, and for OOXML 10,000
ZIP entries/100 MiB inflated content. Supported types are plain text, Markdown, CSV, JSON, XML,
HTML, text PDFs, DOCX, XLSX, and PPTX. No OCR or transcription is part of this decision.

### 6. Download, restart, and backup

Attachment content uses an `httpx` client with `trust_env=False` and `follow_redirects=False`,
exact origin/path construction, and `GET /rest/api/3/attachment/content/{id}?redirect=false`.
Reject every 3xx before reading a body; never send auth to redirects or proxies. A response is
accepted only after declared-size/hash/final-tag verification. Two downloads may run concurrently,
with one DB writer and short transactions. Network timeout is 30 seconds, with three attempts for
timeout, 429, and 5xx only. Invocation transfer budget defaults to 20 GiB and is adjustable;
preserve 2 GiB free space. Incomplete resource streams start over at that resource boundary;
verified resources remain idempotently complete. Pause is durable.

Before activation, recheck issue `updated` and attachment manifest. One changed retry is allowed;
another change or failed comparison marks affected resource families `unstable`, sets collection
outcome `unstable_partial`, and prevents activation. Resume creates a new run/revision attempt and
refetches the affected issue families before rechecking.

Vault-inclusive backup is an epoch: quiesce the writer at a resource boundary, checkpoint/backup
SQLite, bind protected config and HMAC material separately, and bind the immutable vault manifest,
ciphertext hashes, and key versions. Restore is fresh-destination-only and fail-closed on any
binding, key, schema, hash, final-tag, or non-empty-destination failure. No automatic deletion,
restore, merge, or overwrite is permitted. Rollback means restoring a prior coherent epoch to a
fresh destination; it does not erase intervening decisions. `worktrace backup` remains DB-only and
warns when a vault exists. The explicit commands are
`worktrace jira backup COLLECTION_ID --output NEW_DIRECTORY --yes` and
`worktrace jira restore --input EPOCH_DIRECTORY --destination FRESH_DIRECTORY --yes`. Both refuse
overwrite; restore requires a fresh destination and verifies a portable recovery-envelope hash.

Extraction must use generated profile version `1` for macOS `sandbox-exec`, with SHA-256 integrity
over the exact profile bytes. Install/runtime capability probing resolves and hashes only the exact venv/interpreter executable,
dyld/system libraries, Python stdlib, WorkTrace worker module, and approved parser packages in an
immutable read/execute allowlist; all other file reads/writes, network, subprocess, and process
creation are denied. Input/output use inherited pipes only. If the capability, profile/allowlist
hash, path, symlink, or executable check fails, persist `extraction_unavailable` and do not run
unsandboxed. The exact worker uses `shell=False`, an empty allowlist environment, fixed cwd,
close-on-exec descriptors, resource limits, TERM/KILL timeout handling, and reap. No plaintext temp
fallback is permitted.

The shipped `worktrace purge --yes` remains unchanged only when no Jira collection, vault, or key
references exist. If any exist, it fails before deleting DB/HMAC/backups and instructs the user to
use `worktrace jira purge COLLECTION_ID --include-vault --yes`. Jira purge quiesces active jobs,
checks backup references, deletes collection projections and only unreferenced ciphertexts, retires
only unreferenced Keychain versions, and reports logical deletion rather than secure erasure.
Backup retention is never changed implicitly. Whole-installation vault purge requires a separate
explicit command/flag and never follows legacy purge.

## Consequences

- Complete attachments can be preserved without exposing plaintext to SQLite, MCP, or TUI.
- Disk/key management and explicit recovery become user-visible operational responsibilities.
- Resource-level status is more honest but more verbose than one source-level success flag.
- A new dependency group and additive migration are required in #48, after this decision is approved.
- Existing app-scoped authority, candidate decisions, attribution roles, and seven MCP signatures
  remain stable; Jira vault identity is deliberately site/object scoped.
- Static tests and synthetic fixtures cannot establish real Jira permissions or production volume;
  those remain #51 acceptance gates.

## Compatibility and rollback

Old binaries continue to read existing ledgers but cannot read newer schema/vault formats and must
fail closed. New binaries preserve all old observation/decision IDs and do not promote incomplete
collections. Migration is forward-only after an explicit coherent backup. A failed migration stops
before activation; restore requires the prior epoch in a fresh destination and never uses an implicit
destructive rollback.

## Verification obligations

- Format vectors: valid stream, altered AAD, missing final tag, truncation, trailing bytes, wrong
  key/version, size/hash mismatch, atomic temp-file failure.
- Key lifecycle: backend selection, missing Keychain, rotation, retirement, Argon2id recovery, wrong
  passphrase, malformed envelope, and fresh restore.
- Boundary: no originals/raw payloads in SQLite, MCP, TUI, logs, or CLI JSON; no HMAC-key reuse.
- Collection: assignment boundary, one-hop/cross-project context, all resource states, redirects,
  retry classes, download limit, transfer/free-space budgets, pause/resume, unstable recheck.
- Extraction: parser failures, XML/OOXML bombs, all byte/time/memory/page/character limits, no
  network/credentials/child execution, and stable redacted locators.
- Compatibility: populated legacy ledger, additive migration, app mappings, decisions, MCP seven
  tools, and fresh restore.

## Reversal triggers

Reconsider only on evidence that a required Jira resource cannot be represented, the chosen
primitive/backends cannot satisfy authenticated storage, or observed volume invalidates the bounds.
Any replacement must preserve or explicitly mark existing originals unavailable and add a successor
AgDR; no silent downgrade to plaintext or SQLite blobs is allowed.
