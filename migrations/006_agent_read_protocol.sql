CREATE TABLE agent_read_protocol (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
);

INSERT INTO agent_read_protocol(singleton, version) VALUES (1, 1);

-- Existing readers can have cached projections. Advance each ledger app once
-- while retaining its stable identity and all existing evidence/decisions.
UPDATE apps SET read_revision = read_revision + 1;
