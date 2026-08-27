CREATE TABLE apps (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT '',
    business_type TEXT NOT NULL DEFAULT ''
);

CREATE TABLE import_sessions (
    id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL REFERENCES apps(id),
    status TEXT NOT NULL CHECK (status IN ('running', 'complete', 'partial', 'failed')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE sync_runs (
    id TEXT PRIMARY KEY,
    import_session_id TEXT REFERENCES import_sessions(id),
    app_id TEXT NOT NULL REFERENCES apps(id),
    source TEXT NOT NULL CHECK (source IN ('git', 'jira', 'gitlab', 'manual')),
    source_instance TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'full_snapshot',
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'complete', 'failed', 'running_stale')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    adapter_version TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    progress_json TEXT NOT NULL DEFAULT '{}',
    error_summary TEXT,
    completeness TEXT NOT NULL DEFAULT 'unknown',
    UNIQUE (id, app_id)
);

CREATE TABLE source_objects (
    id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL REFERENCES apps(id),
    source TEXT NOT NULL,
    source_instance TEXT NOT NULL,
    kind TEXT NOT NULL,
    external_id TEXT NOT NULL,
    canonical_url TEXT,
    first_seen_run_id TEXT REFERENCES sync_runs(id),
    last_seen_run_id TEXT REFERENCES sync_runs(id),
    availability TEXT NOT NULL DEFAULT 'visible',
    UNIQUE (app_id, source, source_instance, kind, external_id)
);

CREATE TABLE observations (
    id TEXT PRIMARY KEY,
    source_object_id TEXT NOT NULL REFERENCES source_objects(id),
    sync_run_id TEXT NOT NULL REFERENCES sync_runs(id),
    source_updated_at TEXT,
    fetched_at TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    title TEXT,
    body_text TEXT,
    data_json TEXT NOT NULL,
    completeness TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    redaction_version TEXT NOT NULL,
    UNIQUE (source_object_id, sync_run_id, payload_hash)
);

CREATE TABLE actors (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_instance TEXT NOT NULL,
    external_actor_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email_hash TEXT,
    is_self INTEGER NOT NULL DEFAULT 0 CHECK (is_self IN (0, 1)),
    UNIQUE (source, source_instance, external_actor_id)
);

CREATE TABLE participations (
    id TEXT PRIMARY KEY,
    source_object_id TEXT NOT NULL REFERENCES source_objects(id),
    observation_id TEXT REFERENCES observations(id),
    actor_id TEXT NOT NULL REFERENCES actors(id),
    role TEXT NOT NULL,
    effective_from TEXT,
    effective_to TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE (source_object_id, observation_id, actor_id, role, effective_from, effective_to)
);

CREATE TABLE "references" (
    id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL REFERENCES apps(id),
    from_object_id TEXT NOT NULL REFERENCES source_objects(id),
    to_object_id TEXT NOT NULL REFERENCES source_objects(id),
    relationship_type TEXT NOT NULL,
    extraction_method TEXT NOT NULL,
    exact_value TEXT,
    supporting_observation_id TEXT REFERENCES observations(id),
    derived INTEGER NOT NULL DEFAULT 1 CHECK (derived IN (0, 1)),
    UNIQUE (from_object_id, to_object_id, relationship_type, extraction_method, exact_value)
);

CREATE TABLE candidate_groups (
    id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL REFERENCES apps(id),
    seed_object_id TEXT NOT NULL REFERENCES source_objects(id),
    generator_version TEXT NOT NULL,
    suggested_title TEXT NOT NULL,
    suggested_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    generated_at TEXT NOT NULL,
    UNIQUE (app_id, seed_object_id)
);

CREATE TABLE candidate_members (
    candidate_id TEXT NOT NULL REFERENCES candidate_groups(id) ON DELETE CASCADE,
    source_object_id TEXT NOT NULL REFERENCES source_objects(id),
    membership_reason TEXT NOT NULL,
    context_only INTEGER NOT NULL DEFAULT 0 CHECK (context_only IN (0, 1)),
    PRIMARY KEY (candidate_id, source_object_id)
);

CREATE TABLE human_decisions (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    target_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor_label TEXT NOT NULL DEFAULT 'local-user',
    created_at TEXT NOT NULL,
    undo_target_id TEXT,
    FOREIGN KEY (undo_target_id) REFERENCES human_decisions(id)
);
