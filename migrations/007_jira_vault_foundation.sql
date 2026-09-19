CREATE TABLE jira_archive_sites (
    id TEXT PRIMARY KEY,
    canonical_origin TEXT NOT NULL UNIQUE,
    hash_algorithm TEXT NOT NULL CHECK (hash_algorithm = 'sha256')
);

CREATE TABLE jira_collections (
    id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES jira_archive_sites(id),
    scope_json TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    approval_token_hash TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    config_fingerprint TEXT NOT NULL,
    vault_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT NULL
);

CREATE TABLE jira_collection_runs (
    id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES jira_collections(id),
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NULL,
    progress_json TEXT NOT NULL,
    error_json TEXT NULL
);

CREATE TABLE jira_archive_revisions (
    id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES jira_collections(id),
    run_id TEXT NOT NULL REFERENCES jira_collection_runs(id),
    revision_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    activated_at TEXT NULL,
    superseded_at TEXT NULL,
    UNIQUE (collection_id, revision_number)
);

CREATE TABLE jira_archive_app_associations (
    archive_evidence_id TEXT NOT NULL,
    app_id TEXT NOT NULL REFERENCES apps(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (archive_evidence_id, app_id)
);

CREATE TABLE jira_scope_previews (
    preview_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    scope_hash TEXT NOT NULL,
    provider_view_hash TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    site_id TEXT NOT NULL REFERENCES jira_archive_sites(id),
    verified_account_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT NULL,
    preview_status TEXT NOT NULL
);

CREATE TABLE jira_collection_issues (
    collection_id TEXT NOT NULL REFERENCES jira_collections(id),
    revision_id TEXT NOT NULL REFERENCES jira_archive_revisions(id),
    run_id TEXT NOT NULL REFERENCES jira_collection_runs(id),
    issue_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    issue_key TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL CHECK (role IN ('root', 'context', 'explicit_root')),
    selection_reason_json TEXT NOT NULL,
    assignment_status TEXT NOT NULL,
    boundary_status TEXT NOT NULL,
    latest_updated_at TEXT NULL,
    PRIMARY KEY (revision_id, issue_id)
);

CREATE TABLE jira_resource_states (
    id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES jira_collections(id),
    revision_id TEXT NOT NULL REFERENCES jira_archive_revisions(id),
    run_id TEXT NOT NULL REFERENCES jira_collection_runs(id),
    archive_evidence_id TEXT NOT NULL UNIQUE,
    issue_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    locator_json TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('root', 'context', 'shared')),
    state TEXT NOT NULL,
    completeness TEXT NOT NULL,
    availability TEXT NOT NULL,
    expected_count INTEGER NULL,
    seen_count INTEGER NOT NULL DEFAULT 0,
    page_cursor TEXT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    raw_vault_object_id TEXT NULL,
    raw_vault_object_path TEXT NULL,
    redaction_version TEXT NOT NULL,
    source_updated_at TEXT NULL,
    fetched_at TEXT NULL,
    error_json TEXT NULL,
    UNIQUE (revision_id, issue_id, kind, locator_json)
);

CREATE TABLE jira_attachment_objects (
    id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES jira_collections(id),
    revision_id TEXT NOT NULL REFERENCES jira_archive_revisions(id),
    issue_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    archive_evidence_id TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL DEFAULT '',
    declared_size INTEGER NULL,
    manifest_sha256 TEXT NOT NULL,
    original_state TEXT NOT NULL,
    vault_object_id TEXT NULL,
    vault_object_path TEXT NULL,
    ciphertext_sha256 TEXT NULL,
    extracted_state TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    UNIQUE (revision_id, attachment_id)
);

CREATE TABLE jira_search_chunks (
    id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL REFERENCES jira_collections(id),
    revision_id TEXT NOT NULL REFERENCES jira_archive_revisions(id),
    issue_id TEXT NOT NULL,
    attachment_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    locator_json TEXT NOT NULL,
    text_redacted TEXT NOT NULL,
    chars INTEGER NOT NULL,
    extraction_version TEXT NOT NULL,
    UNIQUE (revision_id, attachment_id, ordinal)
);

CREATE INDEX jira_collections_site_idx ON jira_collections(site_id, created_at);
CREATE INDEX jira_collection_runs_collection_idx ON jira_collection_runs(collection_id, started_at);
CREATE INDEX jira_revisions_collection_idx ON jira_archive_revisions(collection_id, revision_number);
CREATE INDEX jira_collection_issues_revision_role_idx
    ON jira_collection_issues(revision_id, role, issue_id);
CREATE INDEX jira_resource_states_revision_kind_idx
    ON jira_resource_states(revision_id, kind, state);
CREATE INDEX jira_resource_states_run_cursor_idx
    ON jira_resource_states(run_id, state, page_cursor);
CREATE INDEX jira_resource_states_vault_path_idx
    ON jira_resource_states(revision_id, raw_vault_object_path);
CREATE INDEX jira_attachment_objects_revision_state_idx
    ON jira_attachment_objects(revision_id, original_state, extracted_state);
CREATE INDEX jira_attachment_objects_vault_path_idx
    ON jira_attachment_objects(revision_id, vault_object_path);
CREATE INDEX jira_search_chunks_revision_attachment_idx
    ON jira_search_chunks(revision_id, attachment_id, ordinal);
