CREATE INDEX idx_sync_runs_current ON sync_runs(app_id, source, status, completed_at DESC);
CREATE INDEX idx_source_objects_scope ON source_objects(app_id, source, kind, external_id);
CREATE INDEX idx_observations_object_time ON observations(source_object_id, fetched_at DESC);
CREATE INDEX idx_observations_run ON observations(sync_run_id);
CREATE INDEX idx_participations_actor_role ON participations(actor_id, role);
CREATE INDEX idx_references_from ON "references"(app_id, from_object_id, relationship_type);
CREATE INDEX idx_references_to ON "references"(app_id, to_object_id, relationship_type);
CREATE INDEX idx_candidates_app_time ON candidate_groups(app_id, generated_at DESC);
CREATE INDEX idx_decisions_target_time ON human_decisions(target_id, created_at, id);
