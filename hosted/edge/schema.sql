PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS studio_schema (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version INTEGER NOT NULL CHECK (version = 1)
);
INSERT OR IGNORE INTO studio_schema (id, version) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS conversations (
  owner TEXT NOT NULL CHECK (length(owner) = 64),
  id TEXT NOT NULL,
  title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
  created_at TEXT NOT NULL,
  PRIMARY KEY (owner, id)
);
CREATE INDEX IF NOT EXISTS conversations_owner_created_at
  ON conversations (owner, created_at);

-- Global admission survives account erasure without retaining an owner identifier.
CREATE TABLE IF NOT EXISTS conversation_admissions (
  day TEXT PRIMARY KEY,
  admitted INTEGER NOT NULL CHECK (admitted BETWEEN 1 AND 200)
);
CREATE TABLE IF NOT EXISTS owner_conversation_admissions (
  day TEXT NOT NULL,
  owner_tag TEXT NOT NULL CHECK (length(owner_tag) = 64),
  admitted INTEGER NOT NULL CHECK (admitted BETWEEN 1 AND 20),
  PRIMARY KEY (day, owner_tag)
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
  input_digest TEXT NOT NULL CHECK (length(input_digest) = 64),
  state TEXT NOT NULL CHECK (state IN ('pending', 'completed', 'uncertain')),
  prompt TEXT NOT NULL CHECK (length(prompt) BETWEEN 1 AND 2000),
  max_output_tokens INTEGER NOT NULL CHECK (max_output_tokens BETWEEN 1 AND 128),
  output TEXT,
  output_tokens INTEGER CHECK (output_tokens BETWEEN 0 AND max_output_tokens),
  provider_response_id TEXT,
  quota_day TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  UNIQUE (owner, idempotency_key),
  UNIQUE (owner, conversation_id, id),
  FOREIGN KEY (owner, conversation_id) REFERENCES conversations (owner, id),
  CHECK (
    (state = 'pending' AND output IS NULL AND output_tokens IS NULL
      AND provider_response_id IS NULL AND finished_at IS NULL)
    OR (state = 'uncertain' AND output IS NULL AND output_tokens IS NULL
      AND provider_response_id IS NULL AND finished_at IS NOT NULL)
    OR (state = 'completed' AND output IS NOT NULL AND output_tokens IS NOT NULL
      AND finished_at IS NOT NULL)
  )
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_unresolved_conversation
  ON runs (owner, conversation_id) WHERE state IN ('pending', 'uncertain');
CREATE INDEX IF NOT EXISTS runs_owner_day ON runs (owner, quota_day);
CREATE INDEX IF NOT EXISTS runs_day ON runs (quota_day);
CREATE INDEX IF NOT EXISTS runs_conversation ON runs (owner, conversation_id, created_at);

-- Run admission is independent of retained account history and cannot be reset by purge.
CREATE TABLE IF NOT EXISTS run_admissions (
  day TEXT PRIMARY KEY,
  admitted INTEGER NOT NULL CHECK (admitted BETWEEN 1 AND 200)
);
CREATE TABLE IF NOT EXISTS owner_run_admissions (
  day TEXT NOT NULL,
  owner_tag TEXT NOT NULL CHECK (length(owner_tag) = 64),
  admitted INTEGER NOT NULL CHECK (admitted BETWEEN 1 AND 20),
  PRIMARY KEY (day, owner_tag)
);

-- This feature is additive to studio_schema version 1. A current-day counter
-- without a matching epoch is held because its key cannot be reconstructed.
CREATE TABLE IF NOT EXISTS admission_epoch_schema (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version INTEGER NOT NULL CHECK (version = 1)
);
INSERT OR IGNORE INTO admission_epoch_schema (id, version) VALUES (1, 1);
CREATE TABLE IF NOT EXISTS admission_key_epochs (
  day TEXT PRIMARY KEY,
  key_fingerprint TEXT NOT NULL CHECK (length(key_fingerprint) = 64)
);

CREATE TABLE IF NOT EXISTS messages (
  owner TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (owner, run_id, role),
  FOREIGN KEY (owner, conversation_id, run_id)
    REFERENCES runs (owner, conversation_id, id)
);
CREATE INDEX IF NOT EXISTS messages_conversation
  ON messages (owner, conversation_id, created_at);
-- Reservations compare an expected message count to this owner-scoped history
-- in the same transaction as quota admission. Reads do not issue reservations.

-- These records describe storage writes, not model quality or signed authorization.
CREATE TABLE IF NOT EXISTS receipts (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('conversation.created', 'run.reserved',
    'run.completed', 'run.uncertain')),
  entity_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  source_revision TEXT NOT NULL,
  evidence_class TEXT NOT NULL CHECK (evidence_class = 'DECLARED'),
  signing_status TEXT NOT NULL CHECK (signing_status = 'UNSIGNED')
);
CREATE INDEX IF NOT EXISTS receipts_owner_entity ON receipts (owner, entity_id);

-- The erasure receipt leaves no owner, entity, content, or derived owner hash.
CREATE TABLE IF NOT EXISTS erasure_receipts (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind = 'account.data_erased'),
  created_at TEXT NOT NULL,
  source_revision TEXT NOT NULL,
  evidence_class TEXT NOT NULL CHECK (evidence_class = 'DECLARED'),
  signing_status TEXT NOT NULL CHECK (signing_status = 'UNSIGNED')
);
