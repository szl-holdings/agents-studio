const encoder = new TextEncoder();
const MAX_BODY_BYTES = 32768;
const MAX_OUTPUT_BYTES = 8192;
const OWNER = /^[a-f0-9]{64}$/;
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const IDENTIFIER = /^[A-Za-z0-9._:-]+$/;
const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._-]*(?:\/[A-Za-z0-9][A-Za-z0-9._-]*)?$/;
const REVISION = /^[a-f0-9]{40}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
  "x-content-type-options": "nosniff",
};

class StoreError extends Error {
  constructor(code, status = 400) {
    super(code);
    this.code = code;
    this.status = status;
  }
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: HEADERS });
}

function fields(data, required, optional = []) {
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new StoreError("invalid_request");
  }
  const allowed = new Set([...required, ...optional]);
  if (required.some((key) => !Object.hasOwn(data, key)) ||
      Object.keys(data).some((key) => !allowed.has(key))) {
    throw new StoreError("invalid_fields");
  }
}

function string(value, maximum, pattern) {
  if (typeof value !== "string" || !value.trim() || [...value].length > maximum ||
      value.includes("\0") || (pattern && !pattern.test(value)) ||
      [...value].some((char) => char.codePointAt(0) >= 0xd800 && char.codePointAt(0) <= 0xdfff)) {
    throw new StoreError("invalid_value");
  }
  return value;
}

function integer(value, minimum, maximum) {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new StoreError("invalid_value");
  }
  return value;
}

function revision(env) {
  return /^[a-f0-9]{40}$/.test(env.SOURCE_REVISION || "") ? env.SOURCE_REVISION : "UNKNOWN";
}

function validSecret(value) {
  return typeof value === "string" && /^[\x21-\x7e]{32,256}$/.test(value);
}

function configuredSecret(env) {
  return validSecret(env.STORE_SECRET);
}

function configuredAdmissionSecret(env) {
  return validSecret(env.ADMISSION_SECRET) && env.ADMISSION_SECRET !== env.STORE_SECRET;
}

function modelBindingEnforced(env) {
  return env.REQUIRE_MODEL_BINDING === "1";
}

const EPOCH_GUARD = `(
  EXISTS (SELECT 1 FROM studio_schema WHERE id = 1 AND version = 1)
  AND EXISTS (SELECT 1 FROM admission_epoch_schema WHERE id = 1 AND version = 1)
  AND (EXISTS (SELECT 1 FROM admission_key_epochs WHERE day = ? AND key_fingerprint = ?)
    OR (NOT EXISTS (SELECT 1 FROM admission_key_epochs WHERE day = ?)
      AND NOT EXISTS (SELECT 1 FROM conversation_admissions WHERE day = ?)
      AND NOT EXISTS (SELECT 1 FROM run_admissions WHERE day = ?)
      AND NOT EXISTS (SELECT 1 FROM owner_conversation_admissions WHERE day = ?)
      AND NOT EXISTS (SELECT 1 FROM owner_run_admissions WHERE day = ?)))
)`;

function epochParams(day, fingerprint) {
  return [day, fingerprint, day, day, day, day, day];
}

function epochState(db, day) {
  return prepared(db, `SELECT
    (SELECT version FROM studio_schema WHERE id = 1) AS schema_version,
    (SELECT version FROM admission_epoch_schema WHERE id = 1) AS admission_version,
    (SELECT key_fingerprint FROM admission_key_epochs WHERE day = ?) AS fingerprint,
    COALESCE((SELECT admitted FROM conversation_admissions WHERE day = ?), 0) AS conversations,
    COALESCE((SELECT admitted FROM run_admissions WHERE day = ?), 0) AS runs,
    (SELECT COUNT(*) FROM owner_conversation_admissions WHERE day = ?) AS owner_conversations,
    (SELECT COUNT(*) FROM owner_run_admissions WHERE day = ?) AS owner_runs`,
  [day, day, day, day, day]);
}

function epochUnavailable(state, fingerprint) {
  return state.schema_version !== 1 || state.admission_version !== 1 || (state.fingerprint !== fingerprint &&
    (state.fingerprint !== null || state.conversations > 0 || state.runs > 0 ||
      state.owner_conversations > 0 || state.owner_runs > 0));
}

async function authorized(request, secret) {
  const header = request.headers.get("authorization") || "";
  if (!header.startsWith("Bearer ") || header.length > 263) return false;
  // Web Crypto verification compares the authentication tags without string equality.
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
  const tag = await crypto.subtle.sign("HMAC", key, encoder.encode(secret));
  return crypto.subtle.verify("HMAC", key, tag, encoder.encode(header.slice(7)));
}

async function boundedJson(request) {
  if (request.headers.get("content-type")?.split(";")[0].trim() !== "application/json") {
    throw new StoreError("json_required", 415);
  }
  const declared = request.headers.get("content-length");
  if (declared !== null && (!/^\d+$/.test(declared) || Number(declared) > MAX_BODY_BYTES)) {
    throw new StoreError("body_too_large", 413);
  }
  if (!request.body) throw new StoreError("invalid_json");
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BODY_BYTES) {
        await reader.cancel();
        throw new StoreError("body_too_large", 413);
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    throw new StoreError("invalid_json");
  }
}

function prepared(db, sql, values = []) {
  return db.prepare(sql).bind(...values);
}

async function batch(db, statements) {
  const results = await db.batch(statements);
  if (results.length !== statements.length || results.some((result) => result.success !== true)) {
    throw new Error("storage batch failed");
  }
  return results;
}

function receipt(db, id, owner, kind, entity, now, source) {
  return prepared(db, `INSERT INTO receipts
    (id, owner, kind, entity_id, created_at, source_revision, evidence_class, signing_status)
    SELECT ?, ?, ?, ?, ?, ?, 'DECLARED', 'UNSIGNED' WHERE changes() = 1`,
  [id, owner, kind, entity, now, source]);
}

function readReceipt(db, owner, id) {
  return prepared(db, "SELECT * FROM receipts WHERE owner = ? AND id = ?", [owner, id]);
}

function readRun(db, owner, id) {
  return prepared(db, `${RUN_WITH_BINDING} WHERE r.owner = ? AND r.id = ?`, [owner, id]);
}

const RUN_WITH_BINDING = `SELECT r.*, b.model_id, b.model_revision, b.model_weights_sha256,
  CASE WHEN b.run_id IS NULL THEN 'UNKNOWN' ELSE 'DECLARED' END AS model_binding_evidence
  FROM runs r LEFT JOIN run_model_bindings b ON b.run_id = r.id`;

async function ownerAdmissionTag(secret, kind, day, owner) {
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const message = encoder.encode(`szl-studio-admission-v1\0${kind}\0${day}\0${owner}`);
  const tag = await crypto.subtle.sign("HMAC", key, message);
  return [...new Uint8Array(tag)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function admissionKeyFingerprint(secret) {
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const tag = await crypto.subtle.sign("HMAC", key, encoder.encode("szl-studio-admission-key-epoch-v1"));
  return [...new Uint8Array(tag)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function cleanupExpiredAdmissions(db, day, receiptId, erasure = false) {
  const marker = erasure ? "erasure_receipts" : "receipts";
  return ["owner_conversation_admissions", "owner_run_admissions"].map((table) =>
    prepared(db, `DELETE FROM ${table} WHERE day < ?
      AND EXISTS (SELECT 1 FROM ${marker} WHERE id = ?)`, [day, receiptId]));
}

async function check(db, admissionSecret, bindingEnforced) {
  const day = new Date().toISOString().slice(0, 10);
  const fingerprint = await admissionKeyFingerprint(admissionSecret);
  const rows = await batch(db, [
    prepared(db, "SELECT version FROM studio_schema WHERE id = 1"),
    prepared(db, "SELECT owner, id, title, created_at FROM conversations LIMIT 0"),
    prepared(db, "SELECT day, admitted FROM conversation_admissions LIMIT 0"),
    prepared(db, "SELECT day, owner_tag, admitted FROM owner_conversation_admissions LIMIT 0"),
    prepared(db, `SELECT id, owner, conversation_id, idempotency_key, input_digest,
      state, prompt, max_output_tokens, output, output_tokens, provider_response_id,
      quota_day, created_at, finished_at FROM runs LIMIT 0`),
    prepared(db, `SELECT run_id, model_id, model_revision, model_weights_sha256
      FROM run_model_bindings LIMIT 0`),
    prepared(db, "SELECT day, admitted FROM run_admissions LIMIT 0"),
    prepared(db, "SELECT day, owner_tag, admitted FROM owner_run_admissions LIMIT 0"),
    prepared(db, "SELECT owner, conversation_id, run_id, role, content, created_at FROM messages LIMIT 0"),
    prepared(db, `SELECT id, owner, kind, entity_id, created_at, source_revision,
      evidence_class, signing_status FROM receipts LIMIT 0`),
    prepared(db, `SELECT id, kind, created_at, source_revision,
      evidence_class, signing_status FROM erasure_receipts LIMIT 0`),
    prepared(db, "SELECT id, version FROM admission_epoch_schema LIMIT 0"),
    prepared(db, "SELECT day, key_fingerprint FROM admission_key_epochs LIMIT 0"),
    epochState(db, day),
  ]);
  if (rows[0].results[0]?.version !== 1 || epochUnavailable(rows[13].results[0], fingerprint)) {
    throw new StoreError("schema_unavailable", 503);
  }
  return { status: "available", schema_version: 1, admission_epoch_version: 1,
    model_binding_enforced: bindingEnforced, receipt_minted: false };
}

async function create(db, data, source, admissionSecret) {
  fields(data, ["op", "owner", "id", "title"]);
  string(data.id, 36, UUID);
  string(data.title, 120);
  const now = new Date().toISOString();
  const day = now.slice(0, 10);
  const fingerprint = await admissionKeyFingerprint(admissionSecret);
  const ownerTag = await ownerAdmissionTag(admissionSecret, "conversation", day, data.owner);
  const receiptId = crypto.randomUUID();
  const results = await batch(db, [
    prepared(db, `INSERT INTO conversations (owner, id, title, created_at)
      SELECT ?, ?, ?, ? WHERE
      (SELECT COUNT(*) FROM conversations WHERE owner = ?) < 100
      AND COALESCE((SELECT admitted FROM owner_conversation_admissions
        WHERE day = ? AND owner_tag = ?), 0) < 20
      AND COALESCE((SELECT admitted FROM conversation_admissions
        WHERE day = ?), 0) < 200
      AND NOT EXISTS (SELECT 1 FROM conversations WHERE owner = ? AND id = ?)
      AND ${EPOCH_GUARD}`,
    [data.owner, data.id, data.title, now, data.owner, day, ownerTag, day, data.owner, data.id,
      ...epochParams(day, fingerprint)]),
    prepared(db, `INSERT INTO conversation_admissions (day, admitted)
      SELECT ?, 1 WHERE changes() = 1
      ON CONFLICT(day) DO UPDATE SET admitted = admitted + 1`, [day]),
    prepared(db, `INSERT INTO owner_conversation_admissions (day, owner_tag, admitted)
      SELECT ?, ?, 1 WHERE changes() = 1
      ON CONFLICT(day, owner_tag) DO UPDATE SET admitted = admitted + 1`, [day, ownerTag]),
    receipt(db, receiptId, data.owner, "conversation.created", data.id, now, source),
    prepared(db, `INSERT INTO admission_key_epochs (day, key_fingerprint)
      SELECT ?, ? WHERE changes() = 1 ON CONFLICT(day) DO NOTHING`, [day, fingerprint]),
    prepared(db, "SELECT * FROM conversations WHERE owner = ? AND id = ?", [data.owner, data.id]),
    readReceipt(db, data.owner, receiptId),
    epochState(db, day),
    ...cleanupExpiredAdmissions(db, day, receiptId),
  ]);
  const conversation = results[5].results[0];
  if (!conversation) {
    if (epochUnavailable(results[7].results[0], fingerprint)) {
      throw new StoreError("admission_unavailable", 503);
    }
    throw new StoreError("conversation_limit", 429);
  }
  if (!results[0].meta.changes) {
    if (conversation.title !== data.title) throw new StoreError("conversation_conflict", 409);
    return { status: "replay", conversation, receipt: null };
  }
  return { status: "created", conversation, receipt: results[6].results[0] };
}

async function list(db, data) {
  fields(data, ["op", "owner"]);
  const results = await batch(db, [prepared(db,
    "SELECT * FROM conversations WHERE owner = ? ORDER BY created_at DESC, id DESC LIMIT 100", [data.owner])]);
  return { conversations: results[0].results, receipt_minted: false };
}

async function get(db, data) {
  fields(data, ["op", "owner", "id"]);
  string(data.id, 36, UUID);
  const results = await batch(db, [
    prepared(db, "SELECT * FROM conversations WHERE owner = ? AND id = ?", [data.owner, data.id]),
    prepared(db, `SELECT * FROM messages WHERE owner = ? AND conversation_id = ?
      ORDER BY created_at DESC, run_id DESC, role ASC LIMIT 100`, [data.owner, data.id]),
    prepared(db, `${RUN_WITH_BINDING} WHERE r.owner = ? AND r.conversation_id = ?
      ORDER BY r.created_at DESC, r.id DESC LIMIT 100`, [data.owner, data.id]),
  ]);
  if (!results[0].results[0]) throw new StoreError("not_found", 404);
  return { conversation: results[0].results[0], messages: results[1].results.reverse(),
    runs: results[2].results.reverse(), receipt_minted: false };
}

async function reserve(db, data, source, admissionSecret, bindingEnforced) {
  fields(data, ["op", "owner", "conversation_id", "idempotency_key", "prompt", "max_output_tokens",
    "expected_message_count"], ["model_binding"]);
  if (bindingEnforced && !Object.hasOwn(data, "model_binding")) {
    throw new StoreError("model_binding_required");
  }
  string(data.conversation_id, 36, UUID);
  string(data.idempotency_key, 128, IDENTIFIER);
  string(data.prompt, 2000);
  integer(data.max_output_tokens, 1, 128);
  integer(data.expected_message_count, 0, 19);
  if (Object.hasOwn(data, "model_binding")) {
    fields(data.model_binding, ["id", "revision", "weights_sha256"]);
    string(data.model_binding.id, 200, MODEL_ID);
    string(data.model_binding.revision, 40, REVISION);
    string(data.model_binding.weights_sha256, 64, SHA256);
  }
  const digestBytes = await crypto.subtle.digest("SHA-256", encoder.encode(JSON.stringify({
    conversation_id: data.conversation_id, prompt: data.prompt, max_output_tokens: data.max_output_tokens,
  })));
  const digest = [...new Uint8Array(digestBytes)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  const runId = crypto.randomUUID();
  const receiptId = crypto.randomUUID();
  const now = new Date().toISOString();
  const day = now.slice(0, 10);
  const fingerprint = await admissionKeyFingerprint(admissionSecret);
  const ownerTag = await ownerAdmissionTag(admissionSecret, "run", day, data.owner);
  // D1 batch is transactional. The INSERT guards and unique partial index decide
  // reservation ownership together; separate read-then-write quota checks are unsafe.
  const bindingStatement = data.model_binding ? [prepared(db, `INSERT INTO run_model_bindings
    (run_id, model_id, model_revision, model_weights_sha256)
    SELECT ?, ?, ?, ? WHERE changes() = 1
    AND EXISTS (SELECT 1 FROM runs WHERE id = ? AND owner = ?)`,
  [runId, data.model_binding.id, data.model_binding.revision,
    data.model_binding.weights_sha256, runId, data.owner])] : [];
  const readIndex = 6 + bindingStatement.length;
  const results = await batch(db, [
    prepared(db, `INSERT INTO runs (id, owner, conversation_id, idempotency_key,
      input_digest, state, prompt, max_output_tokens, quota_day, created_at)
      SELECT ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?
      WHERE EXISTS (SELECT 1 FROM conversations WHERE owner = ? AND id = ?)
      AND NOT EXISTS (SELECT 1 FROM runs WHERE owner = ? AND idempotency_key = ?)
      AND NOT EXISTS (SELECT 1 FROM runs WHERE owner = ? AND conversation_id = ?
        AND state IN ('pending', 'uncertain'))
      AND (SELECT COUNT(*) FROM messages WHERE owner = ? AND conversation_id = ?) = ?
      AND (SELECT COUNT(*) FROM runs WHERE owner = ? AND conversation_id = ?) < 100
      AND COALESCE((SELECT admitted FROM owner_run_admissions
        WHERE day = ? AND owner_tag = ?), 0) < 20
      AND COALESCE((SELECT admitted FROM run_admissions WHERE day = ?), 0) < 200
      AND ${EPOCH_GUARD}`,
    [runId, data.owner, data.conversation_id, data.idempotency_key, digest, data.prompt,
      data.max_output_tokens, day, now, data.owner, data.conversation_id, data.owner,
      data.idempotency_key, data.owner, data.conversation_id, data.owner,
      data.conversation_id, data.expected_message_count, data.owner, data.conversation_id,
      day, ownerTag, day, ...epochParams(day, fingerprint)]),
    prepared(db, `INSERT INTO run_admissions (day, admitted)
      SELECT ?, 1 WHERE changes() = 1
      ON CONFLICT(day) DO UPDATE SET admitted = admitted + 1`, [day]),
    prepared(db, `INSERT INTO owner_run_admissions (day, owner_tag, admitted)
      SELECT ?, ?, 1 WHERE changes() = 1
      ON CONFLICT(day, owner_tag) DO UPDATE SET admitted = admitted + 1`, [day, ownerTag]),
    receipt(db, receiptId, data.owner, "run.reserved", runId, now, source),
    prepared(db, `INSERT INTO admission_key_epochs (day, key_fingerprint)
      SELECT ?, ? WHERE changes() = 1 ON CONFLICT(day) DO NOTHING`, [day, fingerprint]),
    prepared(db, `INSERT INTO messages (owner, conversation_id, run_id, role, content, created_at)
      SELECT owner, conversation_id, id, 'user', prompt, created_at FROM runs
      WHERE owner = ? AND id = ?`, [data.owner, runId]),
    ...bindingStatement,
    prepared(db, `${RUN_WITH_BINDING} WHERE r.owner = ? AND r.idempotency_key = ?`,
      [data.owner, data.idempotency_key]),
    readReceipt(db, data.owner, receiptId),
    prepared(db, `SELECT
      EXISTS(SELECT 1 FROM conversations WHERE owner = ? AND id = ?) AS owned,
      EXISTS(SELECT 1 FROM runs WHERE owner = ? AND conversation_id = ?
        AND state IN ('pending', 'uncertain')) AS unresolved,
      (SELECT COUNT(*) FROM messages WHERE owner = ? AND conversation_id = ?) AS message_count,
      (SELECT COUNT(*) FROM runs WHERE owner = ? AND conversation_id = ?) AS conversation_count,
      COALESCE((SELECT admitted FROM owner_run_admissions
        WHERE day = ? AND owner_tag = ?), 0) AS user_count,
      COALESCE((SELECT admitted FROM run_admissions WHERE day = ?), 0) AS global_count`,
    [data.owner, data.conversation_id, data.owner, data.conversation_id,
      data.owner, data.conversation_id, data.owner, data.conversation_id,
      day, ownerTag, day]),
    epochState(db, day),
    ...cleanupExpiredAdmissions(db, day, receiptId),
  ]);
  const run = results[readIndex].results[0];
  if (run) {
    if (run.input_digest !== digest) throw new StoreError("idempotency_conflict", 409);
    return { status: run.id === runId ? "pending" : "replay", run,
      receipt: results[readIndex + 1].results[0] || null };
  }
  if (epochUnavailable(results[readIndex + 3].results[0], fingerprint)) {
    throw new StoreError("admission_unavailable", 503);
  }
  const limits = results[readIndex + 2].results[0];
  if (!limits.owned) throw new StoreError("not_found", 404);
  if (limits.message_count !== data.expected_message_count) throw new StoreError("context_changed", 409);
  if (limits.unresolved) throw new StoreError("conversation_unresolved", 409);
  if (limits.conversation_count >= 100) throw new StoreError("conversation_run_limit", 429);
  if (limits.user_count >= 20) throw new StoreError("user_daily_limit", 429);
  if (limits.global_count >= 200) throw new StoreError("global_daily_limit", 429);
  throw new StoreError("reservation_unavailable", 503);
}

async function finish(db, data, source) {
  const completed = data.op === "complete";
  fields(data, completed ? ["op", "owner", "run_id", "output", "output_tokens", "provider_response_id"] :
    ["op", "owner", "run_id"]);
  string(data.run_id, 36, UUID);
  if (completed) {
    string(data.output, MAX_OUTPUT_BYTES);
    if (encoder.encode(data.output).byteLength > MAX_OUTPUT_BYTES) throw new StoreError("output_too_large", 413);
    integer(data.output_tokens, 0, 128);
    if (data.provider_response_id !== null) string(data.provider_response_id, 200, IDENTIFIER);
  }
  const now = new Date().toISOString();
  const receiptId = crypto.randomUUID();
  const statements = [completed ? prepared(db, `UPDATE runs SET state = 'completed',
    output = ?, output_tokens = ?, provider_response_id = ?, finished_at = ?
    WHERE owner = ? AND id = ? AND state = 'pending' AND max_output_tokens >= ?`,
  [data.output, data.output_tokens, data.provider_response_id, now, data.owner, data.run_id, data.output_tokens]) :
    prepared(db, `UPDATE runs SET state = 'uncertain', finished_at = ?
      WHERE owner = ? AND id = ? AND state = 'pending'`, [now, data.owner, data.run_id])];
  if (completed) {
    statements.push(prepared(db, `INSERT INTO messages
      (owner, conversation_id, run_id, role, content, created_at)
      SELECT owner, conversation_id, id, 'assistant', output, finished_at FROM runs
      WHERE owner = ? AND id = ? AND changes() = 1`, [data.owner, data.run_id]));
  }
  statements.push(receipt(db, receiptId, data.owner,
    completed ? "run.completed" : "run.uncertain", data.run_id, now, source));
  const readIndex = statements.length;
  statements.push(readRun(db, data.owner, data.run_id), readReceipt(db, data.owner, receiptId));
  statements.push(...cleanupExpiredAdmissions(db, now.slice(0, 10), receiptId));
  const results = await batch(db, statements);
  const run = results[readIndex].results[0];
  if (!run) throw new StoreError("not_found", 404);
  if (!results[0].meta.changes) {
    if (completed && run.state === "completed" && run.output === data.output &&
        run.output_tokens === data.output_tokens && run.provider_response_id === data.provider_response_id) {
      return { status: "replay", run, receipt: null };
    }
    if (!completed && run.state === "uncertain") return { status: "replay", run, receipt: null };
    if (completed && run.state === "pending" && data.output_tokens > run.max_output_tokens) {
      throw new StoreError("output_token_limit", 400);
    }
    throw new StoreError("run_not_pending", 409);
  }
  return { status: run.state, run, receipt: results[readIndex + 1].results[0] };
}

async function purge(db, data, source) {
  fields(data, ["op", "owner"]);
  const id = crypto.randomUUID();
  const now = new Date().toISOString();
  // D1 batch commits the count, receipt, and ordered deletes together.
  const results = await batch(db, [
    prepared(db, `SELECT
      (SELECT COUNT(*) FROM messages WHERE owner = ?) AS messages,
      (SELECT COUNT(*) FROM runs WHERE owner = ?) AS runs,
      (SELECT COUNT(*) FROM receipts WHERE owner = ?) AS receipts,
      (SELECT COUNT(*) FROM conversations WHERE owner = ?) AS conversations`,
    [data.owner, data.owner, data.owner, data.owner]),
    prepared(db, `INSERT INTO erasure_receipts
      (id, kind, created_at, source_revision, evidence_class, signing_status)
      SELECT ?, 'account.data_erased', ?, ?, 'DECLARED', 'UNSIGNED'
      WHERE EXISTS (SELECT 1 FROM messages WHERE owner = ?)
        OR EXISTS (SELECT 1 FROM runs WHERE owner = ?)
        OR EXISTS (SELECT 1 FROM receipts WHERE owner = ?)
        OR EXISTS (SELECT 1 FROM conversations WHERE owner = ?)`,
    [id, now, source, data.owner, data.owner, data.owner, data.owner]),
    prepared(db, "DELETE FROM messages WHERE owner = ?", [data.owner]),
    prepared(db, "DELETE FROM runs WHERE owner = ?", [data.owner]),
    prepared(db, "DELETE FROM receipts WHERE owner = ?", [data.owner]),
    prepared(db, "DELETE FROM conversations WHERE owner = ?", [data.owner]),
    prepared(db, "SELECT * FROM erasure_receipts WHERE id = ?", [id]),
    ...cleanupExpiredAdmissions(db, now.slice(0, 10), id, true),
  ]);
  const deleted = results[0].results[0];
  if (!results[1].meta.changes) {
    return { status: "replay", deleted, receipt: null };
  }
  return { status: "deleted", deleted, receipt: results[6].results[0] };
}

export default {
  async fetch(request, env) {
    try {
      const url = new URL(request.url);
      if (url.pathname === "/healthz" && request.method === "GET" && !url.search) {
        return json({ evidence_class: "DECLARED", source_revision: revision(env),
          database_bound: Boolean(env.DB), authentication_configured: configuredSecret(env),
          admission_configured: configuredAdmissionSecret(env),
          storage_readiness: "UNKNOWN", receipt_minted: false });
      }
      if (url.pathname !== "/v1/store" || url.search) throw new StoreError("not_found", 404);
      if (request.method !== "POST") throw new StoreError("method_not_allowed", 405);
      if (!configuredSecret(env) || !env.DB) throw new StoreError("store_unavailable", 503);
      if (!await authorized(request, env.STORE_SECRET)) throw new StoreError("unauthorized", 401);
      const data = await boundedJson(request);
      if (!configuredAdmissionSecret(env) && !["purge", "complete", "uncertain"].includes(data?.op)) {
        throw new StoreError("store_unavailable", 503);
      }
      if (data?.op === "check") {
        fields(data, ["op"]);
        return json(await check(env.DB, env.ADMISSION_SECRET, modelBindingEnforced(env)));
      }
      string(data?.owner, 64, OWNER);
      let result;
      switch (data?.op) {
        case "list": result = await list(env.DB, data); break;
        case "create": result = await create(env.DB, data, revision(env), env.ADMISSION_SECRET); break;
        case "get": result = await get(env.DB, data); break;
        case "reserve": result = await reserve(env.DB, data, revision(env), env.ADMISSION_SECRET,
          modelBindingEnforced(env)); break;
        case "complete":
        case "uncertain": result = await finish(env.DB, data, revision(env)); break;
        case "purge": result = await purge(env.DB, data, revision(env)); break;
        default: throw new StoreError("unknown_operation");
      }
      return json(result);
    } catch (error) {
      if (error instanceof StoreError) return json({ error: error.code }, error.status);
      return json({ error: "store_unavailable" }, 503);
    }
  },
};
