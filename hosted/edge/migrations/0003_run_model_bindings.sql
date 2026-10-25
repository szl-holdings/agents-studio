-- Additive, idempotent model-pin storage for an identified version-1 D1.
-- Do not backfill existing runs: their model identity is UNKNOWN.
CREATE TABLE IF NOT EXISTS run_model_bindings (
  run_id TEXT PRIMARY KEY REFERENCES runs (id) ON DELETE CASCADE,
  model_id TEXT NOT NULL CHECK (length(model_id) BETWEEN 1 AND 200),
  model_revision TEXT NOT NULL CHECK (length(model_revision) = 40
    AND model_revision NOT GLOB '*[^0-9a-f]*'),
  model_weights_sha256 TEXT NOT NULL CHECK (length(model_weights_sha256) = 64
    AND model_weights_sha256 NOT GLOB '*[^0-9a-f]*')
);
