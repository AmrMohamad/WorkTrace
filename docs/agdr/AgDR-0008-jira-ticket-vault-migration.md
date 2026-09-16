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

Collection identity is Jira site plus numeric issue ID, independent of application mapping:

```text
<site_canonical, numeric_issue_id>
jira:<site_fingerprint>:issue:<numeric_issue_id>
```

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

The additive migration introduces logical `jira_collections`, `jira_collection_issues`,
`jira_resource_states`, `jira_attachment_objects`, and `jira_search_chunks` tables as specified in
the technical design. Existing source-object, observation, participation, decision, app, and
read-revision IDs and semantics remain unchanged. Collection identity is not made app-scoped by
adding an app foreign key; an optional app association is projection metadata only.

### 3. Vault object format

Each raw resource/original is an independent versioned object with a canonical immutable descriptor
and associated data:

```text
magic | format_version | descriptor_length | descriptor_json
      | secretstream_header | ciphertext_chunks...
```

Descriptor fields include format, collection/object IDs, kind, plaintext length/hash, key version,
and chunk size. Descriptor bytes are AAD on every chunk. Chunks use
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
the email HMAC key. `keyring` documents the macOS Keychain backend and its access-control caveat in
its [security considerations](https://keyring.readthedocs.io/en/stable/).

Rotation creates a new version, writes/re-encrypts objects explicitly, verifies all manifests, and
retains old versions until a fresh restore test and explicit retirement. No key material is logged
or persisted in the ledger.

Recovery export is a versioned envelope containing only installation/vault IDs, key versions, KDF
parameters, salt, nonce/header, and authenticated ciphertext of the wrapped key. Argon2id derives
the wrapping key from a user passphrase. Export refuses stdout, overwrite, and untrusted paths.
Import is explicit, validates all bindings, requires a fresh empty destination, and never merges or
deletes automatically.

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

Attachment content uses `GET /rest/api/3/attachment/content/{id}?redirect=false`. Redirects are
forbidden; credentials are sent only to the configured Jira origin. A response is accepted only
after declared-size/hash/final-tag verification. Two downloads may run concurrently, with one DB
writer and short transactions. Network timeout is 30 seconds, with three attempts for timeout,
429, and 5xx only. Invocation transfer budget defaults to 20 GiB and is adjustable; preserve 2 GiB
free space. Incomplete resource streams start over at that resource boundary; verified resources
remain idempotently complete. Pause is durable.

Before activation, recheck issue `updated` and attachment manifest. One changed retry is allowed;
another change or failed comparison produces `unstable_partial` and prevents activation.

Vault-inclusive backup is an epoch: quiesce the writer at a resource boundary, checkpoint/backup
SQLite, bind protected config and HMAC material separately, and bind the immutable vault manifest,
ciphertext hashes, and key versions. Restore is fresh-destination-only and fail-closed on any
binding, key, schema, hash, final-tag, or non-empty-destination failure. No automatic deletion,
restore, merge, or overwrite is permitted. Rollback means restoring a prior coherent epoch to a
fresh destination; it does not erase intervening decisions.

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
