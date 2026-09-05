ALTER TABLE actors ADD COLUMN identity_policy_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE apps ADD COLUMN read_revision INTEGER NOT NULL DEFAULT 0;

CREATE TABLE app_identity_policy (
    app_id TEXT PRIMARY KEY REFERENCES apps(id),
    version INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    rebuild_required INTEGER NOT NULL DEFAULT 0 CHECK (rebuild_required IN (0, 1))
);

CREATE TABLE identity_key_binding (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    ledger_id TEXT NOT NULL,
    verifier TEXT NOT NULL
);

CREATE TABLE identity_repair_audit (
    id TEXT PRIMARY KEY,
    app_id TEXT NOT NULL REFERENCES apps(id),
    created_at TEXT NOT NULL,
    report_json TEXT NOT NULL
);

CREATE TABLE identity_rereview (
    app_id TEXT NOT NULL REFERENCES apps(id),
    target_id TEXT NOT NULL,
    repair_id TEXT NOT NULL REFERENCES identity_repair_audit(id),
    PRIMARY KEY (app_id, target_id, repair_id)
);
