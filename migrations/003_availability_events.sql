ALTER TABLE source_objects ADD COLUMN availability_reason TEXT;
ALTER TABLE source_objects ADD COLUMN availability_observed_at TEXT;

CREATE TABLE source_object_availability_events (
    id TEXT PRIMARY KEY,
    source_object_id TEXT NOT NULL REFERENCES source_objects(id),
    sync_run_id TEXT NOT NULL REFERENCES sync_runs(id),
    state TEXT NOT NULL CHECK (state IN ('visible', 'unavailable')),
    reason TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE (source_object_id, sync_run_id, state, reason)
);

CREATE INDEX idx_availability_events_object_time
ON source_object_availability_events(source_object_id, observed_at, id);

INSERT INTO source_object_availability_events(
    id, source_object_id, sync_run_id, state, reason, observed_at
)
SELECT
    'availability:legacy:' || so.id,
    so.id,
    so.last_seen_run_id,
    CASE WHEN so.availability = 'unavailable' THEN 'unavailable' ELSE 'visible' END,
    'migration_baseline',
    COALESCE(sr.completed_at, sr.started_at, CURRENT_TIMESTAMP)
FROM source_objects so
JOIN sync_runs sr ON sr.id = so.last_seen_run_id;

UPDATE source_objects
SET availability_reason = 'migration_baseline',
    availability_observed_at = COALESCE(
        (SELECT sr.completed_at FROM sync_runs sr WHERE sr.id=source_objects.last_seen_run_id),
        (SELECT sr.started_at FROM sync_runs sr WHERE sr.id=source_objects.last_seen_run_id),
        CURRENT_TIMESTAMP
    );
