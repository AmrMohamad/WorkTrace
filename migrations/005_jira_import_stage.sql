CREATE TABLE jira_import_stage (
    run_id TEXT NOT NULL REFERENCES sync_runs(id),
    kind TEXT NOT NULL,
    external_id TEXT NOT NULL,
    event_at TEXT NOT NULL DEFAULT '',
    record_json TEXT NOT NULL,
    PRIMARY KEY (run_id, kind, external_id)
);
CREATE INDEX idx_jira_stage_order ON jira_import_stage(run_id, kind, event_at, external_id);
