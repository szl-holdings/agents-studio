-- Add durable single-dispatch custody without changing Studio schema version 1.
-- The hold has no DELETE or UPDATE path. An unknown provider outcome therefore
-- remains held across Worker deploys, scheduler runs, and runner replacement.
CREATE TABLE IF NOT EXISTS snowflake_dispatch_schema (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version INTEGER NOT NULL CHECK (version = 1)
);
INSERT OR IGNORE INTO snowflake_dispatch_schema (id, version) VALUES (1, 1);
CREATE TABLE IF NOT EXISTS snowflake_dispatch_holds (
  slot INTEGER PRIMARY KEY CHECK (slot = 1),
  request_id TEXT NOT NULL UNIQUE CHECK (length(request_id) = 36),
  target_json TEXT NOT NULL CHECK (length(target_json) BETWEEN 2 AND 1024),
  target_sha256 TEXT NOT NULL CHECK (length(target_sha256) = 64),
  payload_json TEXT NOT NULL CHECK (length(payload_json) BETWEEN 2 AND 16384),
  payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
  state TEXT NOT NULL CHECK (state = 'HELD_UNKNOWN'),
  created_at TEXT NOT NULL,
  evidence_class TEXT NOT NULL CHECK (evidence_class = 'DECLARED'),
  signing_status TEXT NOT NULL CHECK (signing_status = 'UNSIGNED')
);
