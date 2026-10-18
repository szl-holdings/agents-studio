-- Additive migration for an identified Agents Studio schema-version-1 D1.
-- Do not backfill a current-day key epoch: the old key cannot be proven.
CREATE TABLE IF NOT EXISTS admission_epoch_schema (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version INTEGER NOT NULL CHECK (version = 1)
);
INSERT OR IGNORE INTO admission_epoch_schema (id, version) VALUES (1, 1);
CREATE TABLE IF NOT EXISTS admission_key_epochs (
  day TEXT PRIMARY KEY,
  key_fingerprint TEXT NOT NULL CHECK (length(key_fingerprint) = 64)
);
