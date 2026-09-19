ALTER TABLE jira_archive_revisions ADD COLUMN base_revision_id TEXT NULL
    REFERENCES jira_archive_revisions(id);

ALTER TABLE jira_resource_states ADD COLUMN logical_resource_id TEXT NULL;
UPDATE jira_resource_states SET logical_resource_id = id WHERE logical_resource_id IS NULL;

ALTER TABLE jira_attachment_objects ADD COLUMN logical_resource_id TEXT NULL;
UPDATE jira_attachment_objects SET logical_resource_id = id WHERE logical_resource_id IS NULL;

CREATE INDEX jira_revisions_base_idx
    ON jira_archive_revisions(collection_id, base_revision_id);
CREATE INDEX jira_resource_states_logical_idx
    ON jira_resource_states(collection_id, logical_resource_id, revision_id);
CREATE INDEX jira_attachment_objects_logical_idx
    ON jira_attachment_objects(collection_id, logical_resource_id, revision_id);
