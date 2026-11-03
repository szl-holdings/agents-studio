import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { test } from "node:test";
import worker from "../edge/store.mjs";

const schema = readFileSync(new URL("../edge/schema.sql", import.meta.url), "utf8");
const migration = readFileSync(new URL("../edge/migrations/0002_admission_key_epoch.sql", import.meta.url), "utf8");
const bindingMigration = readFileSync(new URL("../edge/migrations/0003_run_model_bindings.sql", import.meta.url), "utf8");
const dispatchMigration = readFileSync(new URL("../edge/migrations/0004_snowflake_dispatch_hold.sql", import.meta.url), "utf8");
const OWNER_A = "a".repeat(64);
const OWNER_B = "b".repeat(64);
const SECRET = "synthetic-local-test-secret-not-for-deployment";
const ADMISSION_SECRET = "synthetic-independent-admission-key-for-tests";
const ROTATED_SECRET = "synthetic-rotated-store-secret-for-tests";
const ROTATED_ADMISSION_SECRET = "synthetic-rotated-admission-secret-for-tests";
const METRICS_SECRET = "synthetic-independent-metrics-secret-for-tests";
const DISPATCH_SECRET = "synthetic-independent-dispatch-secret-for-tests";

// The adapter models the D1 binding API and executes every guard against SQLite.
// It does not claim to test Cloudflare's deployed runtime or network semantics.
class LocalD1 {
  constructor() {
    this.sqlite = new DatabaseSync(":memory:");
    this.sqlite.exec(schema);
    this.calls = [];
    this.failOn = null;
  }

  prepare(sql) {
    return { bind: (...values) => ({ sql, values }) };
  }

  async batch(statements) {
    this.calls.push(...statements);
    this.sqlite.exec("BEGIN IMMEDIATE");
    try {
      const results = statements.map(({ sql, values }) => {
        if (this.failOn && sql.includes(this.failOn)) throw new Error("synthetic database failure");
        const statement = this.sqlite.prepare(sql);
        if (statement.columns().length) {
          return { success: true, results: statement.all(...values), meta: { changes: 0 } };
        }
        const result = statement.run(...values);
        return { success: true, results: [], meta: { changes: Number(result.changes) } };
      });
      this.sqlite.exec("COMMIT");
      return results;
    } catch (error) {
      this.sqlite.exec("ROLLBACK");
      throw error;
    }
  }

  count(table) {
    assert.ok(["conversations", "conversation_admissions", "owner_conversation_admissions",
      "admission_epoch_schema", "admission_key_epochs",
      "runs", "run_model_bindings", "run_admissions", "owner_run_admissions", "messages",
      "receipts", "erasure_receipts", "snowflake_dispatch_schema",
      "snowflake_dispatch_holds"].includes(table));
    return this.sqlite.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get().n;
  }
}

function environment(t) {
  const DB = new LocalD1();
  t.after(() => DB.sqlite.close());
  return { DB, STORE_SECRET: SECRET, ADMISSION_SECRET, SOURCE_REVISION: "c".repeat(40) };
}

async function call(env, data, options = {}) {
  const response = await worker.fetch(new Request("https://store.example/v1/store", {
    method: "POST",
    headers: { authorization: `Bearer ${SECRET}`, "content-type": "application/json", ...options.headers },
    body: options.body ?? JSON.stringify(data),
  }), env);
  return { status: response.status, data: await response.json(), headers: response.headers };
}

async function conversation(env, owner = OWNER_A) {
  const id = crypto.randomUUID();
  const result = await call(env, { op: "create", owner, id, title: "Synthetic conversation" });
  assert.equal(result.status, 200);
  assert.equal(result.data.status, "created");
  return id;
}

function reservation(id, changes = {}) {
  return { op: "reserve", owner: OWNER_A, conversation_id: id,
    idempotency_key: crypto.randomUUID(), prompt: "A synthetic question", max_output_tokens: 128,
    expected_message_count: 0, ...changes };
}

function modelBinding(changes = {}) {
  return { id: "Qwen/Qwen2.5-0.5B-Instruct", revision: "a".repeat(40),
    weights_sha256: "b".repeat(64), ...changes };
}

function completion(run, changes = {}) {
  return { op: "complete", owner: run.owner, run_id: run.id, output: "A synthetic answer",
    output_tokens: 5, provider_response_id: null, ...changes };
}

function seedConversation(env, owner = OWNER_A, createdAt = "2026-01-01T00:00:00.000Z") {
  const id = crypto.randomUUID();
  env.DB.sqlite.prepare("INSERT INTO conversations VALUES (?, ?, ?, ?)")
    .run(owner, id, "Synthetic fixture", createdAt);
  return id;
}

function seedCompleted(env, owner, id, day = new Date().toISOString().slice(0, 10)) {
  const runId = crypto.randomUUID();
  env.DB.sqlite.prepare(`INSERT INTO runs (id, owner, conversation_id, idempotency_key,
    input_digest, state, prompt, max_output_tokens, output, output_tokens, provider_response_id,
    quota_day, created_at, finished_at) VALUES (?, ?, ?, ?, ?, 'completed', 'fixture',
    128, 'fixture answer', 3, NULL, ?, '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:01.000Z')`)
    .run(runId, owner, id, crypto.randomUUID(), "d".repeat(64), day);
  return runId;
}

async function digest(value) {
  const bytes = new TextEncoder().encode(value);
  const result = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(result)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function dispatchReservation(requestId = crypto.randomUUID(), changes = {}) {
  const sourceRevision = "d".repeat(40);
  const now = new Date();
  const day = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - 1))
    .toISOString().slice(0, 10);
  return { op: "reserve", request_id: requestId,
    target: { account: "synthetic-account", user: "SZL_STUDIO_METRICS_SERVICE",
      role: "SZL_STUDIO_METRICS_WRITER", warehouse: "METRICS_WH", database: "METRICS_DB",
      schema: "TELEMETRY", table: "DAILY_ADMISSIONS" },
    rows: [{ day, conversations_admitted: 2, runs_admitted: 3,
      source_revision: sourceRevision, schema_version: "szl.metrics.daily-admissions/v2",
      idempotency_key: await digest(`${sourceRevision}:szl.metrics.daily-admissions/v2:${day}`) }],
    ...changes };
}

async function dispatchCall(env, data, secret = DISPATCH_SECRET) {
  const response = await worker.fetch(new Request("https://store.example/v1/snowflake-dispatch", {
    method: "POST", headers: { authorization: `Bearer ${secret}`,
      "content-type": "application/json" }, body: JSON.stringify(data),
  }), env);
  return { status: response.status, data: await response.json() };
}

test("health is a read-only declaration, not database readiness", async (t) => {
  const env = environment(t);
  const response = await worker.fetch(new Request("https://store.example/healthz"), env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    evidence_class: "DECLARED", source_revision: "c".repeat(40), database_bound: true,
    authentication_configured: true, admission_configured: true,
    storage_readiness: "UNKNOWN", receipt_minted: false,
  });
  assert.equal(env.DB.calls.length, 0);
  assert.equal(env.DB.count("receipts"), 0);
  assert.equal(response.headers.get("cache-control"), "no-store");
});

test("metrics expose only recent owner-free admission counters under an independent credential", async (t) => {
  const env = { ...environment(t), METRICS_SECRET };
  const now = new Date();
  const utcDay = (offset) => new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(),
    now.getUTCDate() + offset)).toISOString().slice(0, 10);
  const firstDay = utcDay(-30), lastDay = utcDay(-1);
  env.DB.sqlite.prepare("INSERT INTO conversation_admissions VALUES (?, 2)").run(lastDay);
  env.DB.sqlite.prepare("INSERT INTO run_admissions VALUES (?, 3)").run(lastDay);
  env.DB.sqlite.prepare("INSERT INTO run_admissions VALUES (?, 4)").run(firstDay);
  for (const outside of [utcDay(-31), utcDay(0), utcDay(1)]) {
    env.DB.sqlite.prepare("INSERT INTO run_admissions VALUES (?, 8)").run(outside);
  }
  seedConversation(env);
  const req = (secret, suffix = "", method = "GET") => new Request(
    `https://store.example/v1/metrics${suffix}`,
    { method, headers: { authorization: `Bearer ${secret}` } });
  const noAuth = await worker.fetch(req(SECRET), env);
  assert.equal(noAuth.status, 401);
  assert.equal(env.DB.calls.length, 0);
  const wrongScope = await call(env, { op: "check" },
    { headers: { authorization: `Bearer ${METRICS_SECRET}` } });
  assert.equal(wrongScope.status, 401);
  assert.equal(env.DB.calls.length, 0);
  const response = await worker.fetch(req(METRICS_SECRET), env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { evidence_class: "DECLARED", receipt_minted: false,
    days: [{ day: firstDay, conversations_admitted: 0, runs_admitted: 4 },
      { day: lastDay, conversations_admitted: 2, runs_admitted: 3 }] });
  assert.ok(env.DB.calls.every(({ sql }) => sql.trim().startsWith("SELECT")));
  assert.equal(env.DB.count("receipts"), 0);
  assert.equal(response.headers.get("cache-control"), "no-store");
  const firstRead = env.DB.count("run_admissions");
  const repeated = await worker.fetch(req(METRICS_SECRET), env);
  assert.equal(repeated.status, 200);
  assert.equal(env.DB.count("run_admissions"), firstRead);
  assert.equal(env.DB.count("receipts"), 0);
  assert.equal((await worker.fetch(req(METRICS_SECRET, "?extra=1"), env)).status, 404);
  assert.equal((await worker.fetch(req(METRICS_SECRET, "", "POST"), env)).status, 405);
  for (const bad of [undefined, "short", SECRET, ADMISSION_SECRET]) {
    assert.equal((await worker.fetch(req(METRICS_SECRET), { ...env, METRICS_SECRET: bad })).status, 503);
  }
  env.DB.failOn = "SUM(conversations)";
  const unavailable = await worker.fetch(req(METRICS_SECRET), env);
  assert.equal(unavailable.status, 503);
  assert.deepEqual(await unavailable.json(), { error: "store_unavailable" });
});

test("durable dispatch custody permits only the first exact reservation", async (t) => {
  const env = { ...environment(t), SNOWFLAKE_DISPATCH_SECRET: DISPATCH_SECRET,
    SNOWFLAKE_EXPORTER_REVISION: "d".repeat(40) };
  const request = await dispatchReservation();
  const first = await dispatchCall(env, request);
  assert.equal(first.status, 201);
  assert.equal(first.data.status, "reserved");
  assert.equal(first.data.dispatch_permitted, true);
  assert.equal(first.data.hold.state, "HELD_UNKNOWN");
  assert.equal(first.data.hold.evidence_class, "DECLARED");
  assert.equal(first.data.hold.signing_status, "UNSIGNED");
  assert.equal(first.data.hold.request_id, request.request_id);
  assert.equal(first.data.hold.payload_sha256, await digest(JSON.stringify(request.rows)));
  assert.equal(first.data.hold.target_sha256, await digest(JSON.stringify(request.target)));
  assert.equal(env.DB.count("snowflake_dispatch_holds"), 1);

  // A lost HTTP acknowledgement can only be followed by this held replay. It
  // never emits a second dispatch permission, even for the same request body.
  const replay = await dispatchCall(env, request);
  assert.equal(replay.status, 201);
  assert.equal(replay.data.status, "held");
  assert.equal(replay.data.dispatch_permitted, false);
  assert.deepEqual(replay.data.hold, first.data.hold);

  const different = await dispatchCall(env, await dispatchReservation());
  assert.equal(different.status, 409);
  assert.deepEqual(different.data, { error: "dispatch_held" });
  assert.equal(env.DB.count("snowflake_dispatch_holds"), 1);

  env.DB.calls.length = 0;
  const status = await dispatchCall(env, { op: "status" });
  assert.equal(status.status, 200);
  assert.equal(status.data.dispatch_permitted, false);
  assert.deepEqual(status.data.hold, first.data.hold);
  assert.ok(env.DB.calls.every(({ sql }) => sql.trim().startsWith("SELECT")));
});

test("dispatch custody is credential-isolated, validated, and atomic", async (t) => {
  const env = { ...environment(t), METRICS_SECRET,
    SNOWFLAKE_DISPATCH_SECRET: DISPATCH_SECRET,
    SNOWFLAKE_EXPORTER_REVISION: "d".repeat(40) };
  const request = await dispatchReservation();
  for (const secret of [SECRET, ADMISSION_SECRET, METRICS_SECRET, `${DISPATCH_SECRET}x`]) {
    assert.equal((await dispatchCall(env, request, secret)).status, 401);
  }
  for (const value of [undefined, "short", SECRET, ADMISSION_SECRET, METRICS_SECRET]) {
    const result = await dispatchCall({ ...env, SNOWFLAKE_DISPATCH_SECRET: value }, request);
    assert.equal(result.status, 503);
  }
  assert.equal(env.DB.count("snowflake_dispatch_holds"), 0);

  for (const revision of [undefined, "c".repeat(40)]) {
    const result = await dispatchCall({ ...env, SNOWFLAKE_EXPORTER_REVISION: revision }, request);
    assert.equal(result.status, 503);
  }
  assert.equal(env.DB.count("snowflake_dispatch_holds"), 0);

  const malformed = [
    { ...request, request_id: crypto.randomUUID().replace(/-4/, "-1") },
    { ...request, target: { ...request.target, table: "OTHER" } },
    { ...request, rows: [{ ...request.rows[0], conversations_admitted: true }] },
    { ...request, rows: [{ ...request.rows[0], idempotency_key: "a".repeat(64) }] },
    { ...request, rows: [request.rows[0], request.rows[0]] },
  ];
  for (const body of malformed) assert.equal((await dispatchCall(env, body)).status, 400);
  assert.equal(env.DB.count("snowflake_dispatch_holds"), 0);

  env.DB.failOn = "INSERT INTO snowflake_dispatch_holds";
  assert.equal((await dispatchCall(env, request)).status, 503);
  env.DB.failOn = null;
  assert.equal(env.DB.count("snowflake_dispatch_holds"), 0);

  const [a, b] = await Promise.all([
    dispatchCall(env, request), dispatchCall(env, await dispatchReservation()),
  ]);
  assert.equal([a, b].filter((result) => result.data.dispatch_permitted === true).length, 1);
  assert.equal([a.status, b.status].filter((status) => status === 409).length, 1);
  assert.equal(env.DB.count("snowflake_dispatch_holds"), 1);
});

test("dispatch migration is idempotent and never fabricates a prior hold", (t) => {
  const db = new DatabaseSync(":memory:");
  t.after(() => db.close());
  db.exec(`CREATE TABLE studio_schema (id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL CHECK (version = 1));
    INSERT INTO studio_schema (id, version) VALUES (1, 1);`);
  db.exec(dispatchMigration);
  db.exec(dispatchMigration);
  assert.equal(db.prepare("SELECT version FROM snowflake_dispatch_schema WHERE id = 1").get().version, 1);
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM snowflake_dispatch_holds").get().n, 0);
});

test("authenticated readiness checks schema without creating state", async (t) => {
  const env = environment(t);
  const result = await call(env, { op: "check" });
  assert.equal(result.status, 200);
  assert.deepEqual(result.data, { status: "available", schema_version: 1,
    admission_epoch_version: 1, model_binding_enforced: false, receipt_minted: false });
  assert.ok(env.DB.calls.every(({ sql }) => sql.trim().startsWith("SELECT")));
  assert.equal(env.DB.count("receipts"), 0);
  env.DB.failOn = "SELECT owner, conversation_id, run_id";
  assert.deepEqual((await call(env, { op: "check" })).data, { error: "store_unavailable" });
});

test("missing, short and incorrect secrets fail closed before SQL", async (t) => {
  const env = environment(t);
  for (const authorization of ["", "Basic test", `Bearer ${SECRET}x`, "Bearer " + "x".repeat(300)]) {
    const result = await call(env, { op: "check" }, { headers: { authorization } });
    assert.equal(result.status, 401);
  }
  for (const secret of [undefined, "short", "x".repeat(32) + " ", "x".repeat(257)]) {
    assert.equal((await call({ ...env, STORE_SECRET: secret }, { op: "check" })).status, 503);
  }
  for (const admissionSecret of [undefined, "short", SECRET, "x".repeat(32) + " ",
    "x".repeat(257)]) {
    assert.equal((await call({ ...env, ADMISSION_SECRET: admissionSecret }, { op: "check" })).status, 503);
  }
  assert.equal((await call({ ...env, DB: undefined }, { op: "check" })).status, 503);
  assert.equal(env.DB.calls.length, 0);
});

test("the additive migration retains version-1 schema and admission rows", (t) => {
  const db = new DatabaseSync(":memory:");
  t.after(() => db.close());
  db.exec(`CREATE TABLE studio_schema (id INTEGER PRIMARY KEY CHECK (id = 1),
      version INTEGER NOT NULL CHECK (version = 1));
    INSERT INTO studio_schema (id, version) VALUES (1, 1);
    CREATE TABLE conversation_admissions (day TEXT PRIMARY KEY, admitted INTEGER NOT NULL);
    INSERT INTO conversation_admissions VALUES ('2026-10-17', 7);
    CREATE TABLE run_admissions (day TEXT PRIMARY KEY, admitted INTEGER NOT NULL);
    INSERT INTO run_admissions VALUES ('2026-10-17', 3);`);
  db.exec(migration);
  db.exec(migration);
  assert.equal(db.prepare("SELECT version FROM studio_schema WHERE id = 1").get().version, 1);
  assert.equal(db.prepare("SELECT admitted FROM conversation_admissions").get().admitted, 7);
  assert.equal(db.prepare("SELECT admitted FROM run_admissions").get().admitted, 3);
  assert.equal(db.prepare("SELECT version FROM admission_epoch_schema WHERE id = 1").get().version, 1);
  assert.equal(db.prepare("SELECT COUNT(*) AS n FROM admission_key_epochs").get().n, 0);
});

test("model-binding migration is idempotent and preserves legacy run and admission rows", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  const legacy = (await call(env, reservation(id))).data.run;
  const admitted = env.DB.sqlite.prepare("SELECT admitted FROM run_admissions").get().admitted;
  env.DB.sqlite.exec("DROP TABLE run_model_bindings");
  assert.equal((await call(env, { op: "check" })).status, 503);
  env.DB.sqlite.exec(bindingMigration);
  env.DB.sqlite.exec(bindingMigration);
  assert.equal((await call(env, { op: "check" })).status, 200);
  assert.equal(env.DB.sqlite.prepare("SELECT version FROM studio_schema WHERE id = 1").get().version, 1);
  assert.equal(env.DB.sqlite.prepare("SELECT admitted FROM run_admissions").get().admitted, admitted);
  assert.equal(env.DB.count("runs"), 1);
  assert.equal(env.DB.count("run_model_bindings"), 0);
  const history = (await call(env, { op: "get", owner: OWNER_A, id })).data.runs[0];
  assert.equal(history.id, legacy.id);
  assert.deepEqual([history.model_id, history.model_revision, history.model_weights_sha256,
    history.model_binding_evidence], [null, null, null, "UNKNOWN"]);
});

test("legacy current-day admissions hold until the next UTC day", async (t) => {
  const env = environment(t);
  env.DB.sqlite.prepare("INSERT INTO conversation_admissions VALUES (date('now'), 1)").run();
  assert.equal((await call(env, { op: "check" })).status, 503);
  assert.equal((await call(env, { op: "create", owner: OWNER_A,
    id: crypto.randomUUID(), title: "Held migration day" })).data.error, "admission_unavailable");
  assert.equal(env.DB.count("conversations"), 0);
  env.DB.sqlite.prepare("UPDATE conversation_admissions SET day = '2000-01-01'").run();
  assert.equal((await call(env, { op: "check" })).status, 200);
  assert.equal((await call(env, { op: "create", owner: OWNER_A,
    id: crypto.randomUUID(), title: "New UTC day" })).status, 200);
});

test("owner-only legacy counter rows also hold admission", async (t) => {
  const env = environment(t);
  env.DB.sqlite.prepare("INSERT INTO owner_run_admissions VALUES (date('now'), ?, 1)")
    .run("f".repeat(64));
  assert.equal((await call(env, { op: "check" })).status, 503);
  assert.equal((await call(env, { op: "create", owner: OWNER_A,
    id: crypto.randomUUID(), title: "Held owner counter" })).data.error, "admission_unavailable");
  assert.equal(env.DB.count("conversations"), 0);
});

test("an admission-key outage allows completion and erasure but not new admissions", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  const run = (await call(env, reservation(id))).data.run;
  const unavailable = { ...env, ADMISSION_SECRET: undefined };
  assert.equal((await call(unavailable, { op: "check" })).status, 503);
  assert.equal((await call(unavailable, reservation(id))).status, 503);
  assert.equal((await call(unavailable, completion(run))).data.run.state, "completed");
  assert.equal((await call(unavailable, { op: "purge", owner: OWNER_A })).data.status, "deleted");
  assert.equal(env.DB.count("conversations"), 0);
  assert.equal(env.DB.count("admission_key_epochs"), 1);
});

test("same-day admission-key rotation stays held after account erasure", async (t) => {
  const env = environment(t);
  const first = await conversation(env);
  const second = await conversation(env);
  const run = (await call(env, reservation(first))).data.run;
  const epoch = env.DB.sqlite.prepare("SELECT * FROM admission_key_epochs").get();
  assert.match(epoch.key_fingerprint, /^[a-f0-9]{64}$/);
  assert.notEqual(epoch.key_fingerprint, ADMISSION_SECRET);
  const rotated = { ...env, ADMISSION_SECRET: ROTATED_ADMISSION_SECRET };
  assert.equal((await call(rotated, { op: "check" })).status, 503);
  assert.equal((await call(rotated, reservation(second))).data.error, "admission_unavailable");
  assert.equal((await call(rotated, { op: "create", owner: OWNER_B,
    id: crypto.randomUUID(), title: "Denied rotation" })).data.error, "admission_unavailable");
  assert.equal((await call(rotated, { op: "uncertain", owner: OWNER_A,
    run_id: run.id })).data.run.state, "uncertain");
  assert.equal((await call(rotated, { op: "purge", owner: OWNER_A })).data.status, "deleted");
  assert.equal((await call(rotated, { op: "create", owner: OWNER_A,
    id: crypto.randomUUID(), title: "Denied after purge" })).data.error, "admission_unavailable");
  assert.deepEqual(env.DB.sqlite.prepare("SELECT * FROM admission_key_epochs").get(), epoch);
  assert.equal(env.DB.count("conversations"), 0);
  assert.equal((await call(env, { op: "check" })).status, 200);
});

test("rotating store authentication does not reset owner admission quotas", async (t) => {
  const env = environment(t);
  const ids = [];
  for (let i = 0; i < 19; i++) ids.push(await conversation(env));
  const ownerTag = env.DB.sqlite.prepare("SELECT owner_tag FROM owner_conversation_admissions").get().owner_tag;
  const firstRun = (await call(env, reservation(ids[0]))).data.run;
  await call(env, { op: "uncertain", owner: OWNER_A, run_id: firstRun.id });
  env.DB.sqlite.prepare("UPDATE owner_run_admissions SET admitted = 19").run();
  const runTag = env.DB.sqlite.prepare("SELECT owner_tag FROM owner_run_admissions").get().owner_tag;
  const rotated = { ...env, STORE_SECRET: ROTATED_SECRET };
  const options = { headers: { authorization: `Bearer ${ROTATED_SECRET}` } };
  assert.equal((await call(rotated, { op: "create", owner: OWNER_A,
    id: crypto.randomUUID(), title: "Twentieth" }, options)).status, 200);
  assert.equal((await call(rotated, { op: "create", owner: OWNER_A,
    id: crypto.randomUUID(), title: "Over limit" }, options)).status, 429);
  assert.equal(env.DB.sqlite.prepare("SELECT owner_tag FROM owner_conversation_admissions").get().owner_tag,
    ownerTag);
  assert.equal(env.DB.sqlite.prepare("SELECT admitted FROM owner_conversation_admissions").get().admitted, 20);
  assert.equal((await call(rotated, reservation(ids[1]), options)).status, 200);
  assert.equal((await call(rotated, reservation(ids[2]), options)).status, 429);
  assert.equal(env.DB.sqlite.prepare("SELECT owner_tag FROM owner_run_admissions").get().owner_tag, runTag);
  assert.equal(env.DB.sqlite.prepare("SELECT admitted FROM owner_run_admissions").get().admitted, 20);
});

test("only fixed paths, methods, fields and JSON operations are accepted", async (t) => {
  const env = environment(t);
  for (const url of ["https://store.example/sql", "https://store.example/v1/store?sql=DROP"]) {
    assert.equal((await worker.fetch(new Request(url), env)).status, 404);
  }
  assert.equal((await worker.fetch(new Request("https://store.example/v1/store"), env)).status, 405);
  assert.equal((await call(env, { op: "list", owner: OWNER_A, sql: "DROP TABLE runs" })).status, 400);
  assert.equal((await call(env, { op: "sql", owner: OWNER_A })).status, 400);
  assert.equal((await call(env, { op: "check", owner: OWNER_A })).status, 400);
  assert.equal((await call(env, null)).status, 400);
  assert.equal((await call(env, [], { body: "{" })).status, 400);
  assert.equal((await call(env, { op: "check" }, { headers: { "content-type": "text/plain" } })).status, 415);
  assert.equal(env.DB.calls.length, 0);
});

test("JSON body is bounded even with absent or dishonest Content-Length", async (t) => {
  const env = environment(t);
  assert.equal((await call(env, {}, { body: "x".repeat(32769) })).status, 413);
  assert.equal((await call(env, {}, { body: "x".repeat(32769), headers: { "content-length": "1" } })).status, 413);
  assert.equal((await call(env, { op: "check" }, { headers: { "content-length": "32769" } })).status, 413);
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new Uint8Array(20000));
      controller.enqueue(new Uint8Array(20000));
      controller.close();
    },
  });
  const response = await worker.fetch(new Request("https://store.example/v1/store", {
    method: "POST", duplex: "half", body,
    headers: { authorization: `Bearer ${SECRET}`, "content-type": "application/json" },
  }), env);
  assert.equal(response.status, 413);
  const invalid = await call(env, {}, { body: new Uint8Array([0xff]) });
  assert.equal(invalid.status, 400);
  assert.equal(env.DB.calls.length, 0);
});

test("create and reads isolate owners and duplicate creation emits no receipt", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  const duplicate = await call(env, { op: "create", owner: OWNER_A, id, title: "Synthetic conversation" });
  assert.equal(duplicate.data.status, "replay");
  assert.equal(duplicate.data.receipt, null);
  assert.equal((await call(env, { op: "create", owner: OWNER_A, id, title: "Replacement title" })).status, 409);
  assert.equal((await call(env, { op: "get", owner: OWNER_B, id })).status, 404);
  assert.deepEqual((await call(env, { op: "list", owner: OWNER_B })).data.conversations, []);
  assert.equal((await call(env, { op: "list", owner: OWNER_A })).data.conversations.length, 1);
  const before = env.DB.calls.length;
  const result = await call(env, { op: "get", owner: OWNER_A, id });
  assert.equal(result.data.conversation.id, id);
  assert.ok(env.DB.calls.slice(before).every(({ sql }) => sql.trim().startsWith("SELECT")));
  assert.equal(env.DB.count("receipts"), 1);
  const receipt = env.DB.sqlite.prepare("SELECT * FROM receipts").get();
  assert.equal(receipt.signing_status, "UNSIGNED");
  assert.equal(receipt.evidence_class, "DECLARED");
  assert.equal(receipt.source_revision, env.SOURCE_REVISION);
});

test("concurrent conversation creation cannot exceed the owner limit", async (t) => {
  const env = environment(t);
  for (let i = 0; i < 99; i++) seedConversation(env);
  const results = await Promise.all(Array.from({ length: 8 }, () => call(env,
    { op: "create", owner: OWNER_A, id: crypto.randomUUID(), title: "Bounded" })));
  assert.equal(results.filter((result) => result.status === 200).length, 1);
  assert.equal(results.filter((result) => result.status === 429).length, 7);
  assert.equal(env.DB.count("conversations"), 100);
  assert.equal(env.DB.count("receipts"), 1);
  assert.equal((await call(env, { op: "create", owner: OWNER_B, id: crypto.randomUUID(), title: "Other owner" })).status, 200);
});

test("owner daily conversation creation limit is atomic", async (t) => {
  const env = environment(t);
  for (let i = 0; i < 19; i++) await conversation(env);
  const results = await Promise.all(Array.from({ length: 8 }, () => call(env,
    { op: "create", owner: OWNER_A, id: crypto.randomUUID(), title: "Bounded daily" })));
  assert.equal(results.filter((result) => result.data.status === "created").length, 1);
  assert.equal(results.filter((result) => result.data.error === "conversation_limit").length, 7);
  assert.equal(env.DB.sqlite.prepare("SELECT COUNT(*) AS n FROM conversations WHERE owner = ?")
    .get(OWNER_A).n, 20);
  assert.equal(env.DB.sqlite.prepare("SELECT admitted FROM conversation_admissions").get().admitted, 20);
  const ownerAdmission = env.DB.sqlite.prepare("SELECT * FROM owner_conversation_admissions").get();
  assert.equal(ownerAdmission.admitted, 20);
  assert.match(ownerAdmission.owner_tag, /^[a-f0-9]{64}$/);
  assert.notEqual(ownerAdmission.owner_tag, OWNER_A);
  assert.equal((await call(env, { op: "purge", owner: OWNER_A })).data.status, "deleted");
  assert.equal(env.DB.sqlite.prepare("SELECT admitted FROM owner_conversation_admissions").get().admitted, 20);
  assert.equal((await call(env, { op: "create", owner: OWNER_A,
    id: crypto.randomUUID(), title: "Same owner after purge" })).status, 429);
  assert.equal((await call(env, { op: "create", owner: OWNER_B,
    id: crypto.randomUUID(), title: "Other owner" })).status, 200);
});

test("global daily conversation admissions survive owner rotation and deletion", async (t) => {
  const env = environment(t);
  await conversation(env, "f".repeat(64));
  assert.equal((await call(env, { op: "purge", owner: "f".repeat(64) })).status, 200);
  env.DB.sqlite.prepare("UPDATE conversation_admissions SET admitted = 199 WHERE day = date('now')").run();
  const requests = Array.from({ length: 8 }, (_, i) => ({ op: "create",
    owner: (i + 1).toString(16).padStart(64, "0"), id: crypto.randomUUID(), title: "Bounded global" }));
  const results = await Promise.all(requests.map((request) => call(env, request)));
  assert.equal(results.filter((result) => result.data.status === "created").length, 1);
  assert.equal(results.filter((result) => result.data.error === "conversation_limit").length, 7);
  assert.equal(env.DB.sqlite.prepare("SELECT admitted FROM conversation_admissions").get().admitted, 200);
  assert.equal(env.DB.count("conversations"), 1);
  assert.equal(env.DB.count("receipts"), 1);
  const admitted = requests[results.findIndex((result) => result.data.status === "created")];
  assert.equal((await call(env, admitted)).data.status, "replay");
  assert.equal((await call(env, { op: "purge", owner: admitted.owner })).data.status, "deleted");
  assert.equal(env.DB.count("conversations"), 0);
  assert.equal(env.DB.sqlite.prepare("SELECT admitted FROM conversation_admissions").get().admitted, 200);
  assert.equal((await call(env, { op: "create", owner: OWNER_B,
    id: crypto.randomUUID(), title: "After deletion" })).status, 429);
});

test("owner admission tags are purpose-specific and expired rows clear on writes", async (t) => {
  const env = environment(t);
  const oldTag = "f".repeat(64);
  env.DB.sqlite.prepare("INSERT INTO owner_conversation_admissions VALUES (?, ?, 1)")
    .run("2000-01-01", oldTag);
  env.DB.sqlite.prepare("INSERT INTO owner_run_admissions VALUES (?, ?, 1)")
    .run("2000-01-01", oldTag);
  const id = await conversation(env);
  assert.equal(env.DB.sqlite.prepare("SELECT COUNT(*) AS n FROM owner_conversation_admissions WHERE day < date('now')")
    .get().n, 0);
  assert.equal(env.DB.sqlite.prepare("SELECT COUNT(*) AS n FROM owner_run_admissions WHERE day < date('now')")
    .get().n, 0);
  const conversationTag = env.DB.sqlite.prepare("SELECT owner_tag FROM owner_conversation_admissions").get().owner_tag;
  const run = (await call(env, reservation(id))).data.run;
  const runTag = env.DB.sqlite.prepare("SELECT owner_tag FROM owner_run_admissions").get().owner_tag;
  assert.notEqual(conversationTag, OWNER_A);
  assert.notEqual(runTag, OWNER_A);
  assert.notEqual(conversationTag, runTag);
  await call(env, { op: "uncertain", owner: OWNER_A, run_id: run.id });
  assert.equal((await call(env, { op: "purge", owner: OWNER_A })).data.status, "deleted");
  assert.equal(env.DB.sqlite.prepare("SELECT owner_tag FROM owner_conversation_admissions").get().owner_tag,
    conversationTag);
  assert.equal(env.DB.sqlite.prepare("SELECT owner_tag FROM owner_run_admissions").get().owner_tag,
    runTag);
});

test("reservation persists prompt once, binds input and replays without dispatch authority", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  const request = reservation(id);
  const first = await call(env, request);
  assert.equal(first.status, 200);
  assert.equal(first.data.status, "pending");
  assert.match(first.data.run.input_digest, /^[a-f0-9]{64}$/);
  assert.equal(first.data.receipt.kind, "run.reserved");
  const repeated = await call(env, request);
  assert.equal(repeated.data.status, "replay");
  assert.equal(repeated.data.run.id, first.data.run.id);
  assert.equal(repeated.data.receipt, null);
  for (const changes of [{ prompt: "Different" }, { max_output_tokens: 10 }, { conversation_id: crypto.randomUUID() }]) {
    const conflict = await call(env, { ...request, ...changes });
    assert.equal(conflict.status, 409);
    assert.equal(conflict.data.error, "idempotency_conflict");
  }
  assert.equal((await call(env, reservation(id, { expected_message_count: 1 }))).data.error, "conversation_unresolved");
  assert.equal(env.DB.count("runs"), 1);
  assert.equal(env.DB.count("messages"), 1);
  assert.equal(env.DB.count("receipts"), 2);
});

test("model pin is stored once and returned on reserve, completion and history", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  const binding = modelBinding();
  const request = reservation(id, { model_binding: binding });
  const first = await call(env, request);
  assert.equal(first.data.status, "pending");
  assert.deepEqual([first.data.run.model_id, first.data.run.model_revision,
    first.data.run.model_weights_sha256, first.data.run.model_binding_evidence],
  [binding.id, binding.revision, binding.weights_sha256, "DECLARED"]);
  assert.equal(env.DB.count("run_model_bindings"), 1);
  const replay = await call(env, { ...request,
    model_binding: modelBinding({ revision: "c".repeat(40) }) });
  assert.equal(replay.data.status, "replay");
  assert.equal(replay.data.run.model_revision, binding.revision);
  assert.equal(replay.data.receipt, null);
  assert.equal(env.DB.count("run_model_bindings"), 1);
  const complete = await call(env, completion(first.data.run));
  assert.equal(complete.data.status, "completed");
  assert.equal(complete.data.run.model_binding_evidence, "DECLARED");
  assert.equal(complete.data.run.model_weights_sha256, binding.weights_sha256);
  const history = (await call(env, { op: "get", owner: OWNER_A, id })).data;
  assert.equal(history.runs[0].model_id, binding.id);
  assert.equal(history.runs[0].model_revision, binding.revision);
  assert.equal(history.runs[0].model_weights_sha256, binding.weights_sha256);
  assert.equal(history.runs[0].model_binding_evidence, "DECLARED");
});

test("legacy reservation replays without acquiring a later model pin", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  const request = reservation(id);
  const legacy = await call(env, request);
  assert.equal(legacy.data.run.model_binding_evidence, "UNKNOWN");
  const replay = await call(env, { ...request, model_binding: modelBinding() });
  assert.equal(replay.data.status, "replay");
  assert.deepEqual([replay.data.run.model_id, replay.data.run.model_revision,
    replay.data.run.model_weights_sha256, replay.data.run.model_binding_evidence],
  [null, null, null, "UNKNOWN"]);
  assert.equal(env.DB.count("run_model_bindings"), 0);
  const done = await call(env, completion(legacy.data.run));
  assert.equal(done.data.run.model_binding_evidence, "UNKNOWN");
});

test("exact model-binding enforcement is visible and rejects legacy reserve before D1", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  assert.equal((await call(env, reservation(id))).data.status, "pending");
  const legacyId = await conversation(env);
  const enforced = { ...env, REQUIRE_MODEL_BINDING: "1" };
  const check = await call(enforced, { op: "check" });
  assert.equal(check.data.model_binding_enforced, true);
  assert.equal(check.data.receipt_minted, false);
  assert.equal((await call({ ...env, REQUIRE_MODEL_BINDING: "true" },
    { op: "check" })).data.model_binding_enforced, false);
  const before = env.DB.calls.length;
  const denied = await call(enforced, reservation(legacyId));
  assert.equal(denied.status, 400);
  assert.deepEqual(denied.data, { error: "model_binding_required" });
  assert.equal(env.DB.calls.length, before);
  assert.equal(env.DB.count("runs"), 1);
  assert.equal(env.DB.count("run_model_bindings"), 0);
  const pinned = await call(enforced, reservation(legacyId, { model_binding: modelBinding() }));
  assert.equal(pinned.data.status, "pending");
  assert.equal(pinned.data.run.model_binding_evidence, "DECLARED");
});

test("malformed model pins are rejected before admission or content writes", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  for (const invalid of [null, [], {}, modelBinding({ id: "../escape" }),
    modelBinding({ id: "Qwen//model" }), modelBinding({ id: "Qwen/model/extra" }),
    modelBinding({ id: "Qwen/model?revision=main" }), modelBinding({ id: "a".repeat(201) }),
    modelBinding({ revision: "A".repeat(40) }), modelBinding({ revision: "a".repeat(39) }),
    modelBinding({ weights_sha256: "B".repeat(64) }),
    modelBinding({ weights_sha256: "b".repeat(63) }),
    modelBinding({ extra: "field" })]) {
    const result = await call(env, reservation(id, { model_binding: invalid }));
    assert.equal(result.status, 400);
  }
  assert.equal(env.DB.count("runs"), 0);
  assert.equal(env.DB.count("run_model_bindings"), 0);
  assert.equal(env.DB.count("messages"), 0);
  assert.equal(env.DB.count("run_admissions"), 0);
  assert.equal(env.DB.count("receipts"), 1);
});

test("failed model-binding insert rolls back the full reservation batch", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  env.DB.failOn = "INSERT INTO run_model_bindings";
  assert.equal((await call(env, reservation(id, { model_binding: modelBinding() }))).status, 503);
  assert.deepEqual([env.DB.count("runs"), env.DB.count("run_model_bindings"),
    env.DB.count("messages"), env.DB.count("run_admissions"), env.DB.count("receipts")],
  [0, 0, 0, 0, 1]);
  env.DB.failOn = null;
  assert.equal((await call(env, reservation(id, { model_binding: modelBinding() }))).data.status,
    "pending");
});

test("concurrent same-key reservations create exactly one new pending run", async (t) => {
  const env = environment(t);
  const request = reservation(await conversation(env));
  const results = await Promise.all(Array.from({ length: 10 }, () => call(env, request)));
  assert.equal(results.filter((result) => result.data.status === "pending").length, 1);
  assert.equal(results.filter((result) => result.data.status === "replay").length, 9);
  assert.equal(new Set(results.map((result) => result.data.run.id)).size, 1);
  assert.equal(env.DB.count("runs"), 1);
  assert.equal(env.DB.count("receipts"), 2);
  assert.equal(env.DB.sqlite.prepare("SELECT admitted FROM run_admissions").get().admitted, 1);
});

test("owner is bound on reserve, completion and uncertain mutations", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  assert.equal((await call(env, reservation(id, { owner: OWNER_B }))).status, 404);
  const run = (await call(env, reservation(id))).data.run;
  assert.equal((await call(env, completion(run, { owner: OWNER_B }))).status, 404);
  assert.equal((await call(env, { op: "uncertain", owner: OWNER_B, run_id: run.id })).status, 404);
  assert.equal(env.DB.count("messages"), 1);
  assert.equal(env.DB.count("receipts"), 2);
  const otherId = await conversation(env, OWNER_B);
  const other = await call(env, reservation(otherId, { owner: OWNER_B, idempotency_key: run.idempotency_key }));
  assert.equal(other.data.status, "pending");
});

test("completion writes once and never replaces output or provider identity", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  const run = (await call(env, reservation(id))).data.run;
  const result = await call(env, completion(run));
  assert.equal(result.status, 200);
  assert.equal(result.data.status, "completed");
  assert.equal(result.data.run.provider_response_id, null);
  assert.equal(result.data.receipt.kind, "run.completed");
  assert.equal((await call(env, completion(run))).data.status, "replay");
  assert.equal((await call(env, completion(run, { output: "Replacement" }))).status, 409);
  assert.equal((await call(env, completion(run, { provider_response_id: "different-response" }))).status, 409);
  assert.equal((await call(env, { op: "uncertain", owner: OWNER_A, run_id: run.id })).status, 409);
  assert.equal(env.DB.count("messages"), 2);
  assert.equal(env.DB.count("receipts"), 3);
  const history = (await call(env, { op: "get", owner: OWNER_A, id })).data;
  assert.deepEqual(history.messages.map((message) => message.role), ["user", "assistant"]);
  assert.equal(history.messages[1].content, "A synthetic answer");
  assert.equal((await call(env, reservation(id, { expected_message_count: 2 }))).data.status, "pending");
});

test("uncertain submissions stay charged and cannot be retried or completed", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  const request = reservation(id);
  const run = (await call(env, request)).data.run;
  const result = await call(env, { op: "uncertain", owner: OWNER_A, run_id: run.id });
  assert.equal(result.data.status, "uncertain");
  assert.equal(result.data.receipt.kind, "run.uncertain");
  assert.equal((await call(env, { op: "uncertain", owner: OWNER_A, run_id: run.id })).data.status, "replay");
  const replay = await call(env, request);
  assert.equal(replay.data.status, "replay");
  assert.equal(replay.data.run.state, "uncertain");
  assert.equal((await call(env, reservation(id, { expected_message_count: 1 }))).data.error, "conversation_unresolved");
  assert.equal((await call(env, completion(run))).data.error, "run_not_pending");
  assert.equal(env.DB.count("runs"), 1);
  assert.equal(env.DB.count("messages"), 1);
  assert.equal(env.DB.count("receipts"), 3);
});

test("stale context is rejected atomically while an existing key still replays", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  const original = reservation(id);
  const run = (await call(env, original)).data.run;
  await call(env, completion(run));
  const before = [env.DB.count("runs"), env.DB.count("messages"), env.DB.count("receipts")];
  const stale = await call(env, reservation(id));
  assert.equal(stale.status, 409);
  assert.equal(stale.data.error, "context_changed");
  const replay = await call(env, original);
  assert.equal(replay.data.status, "replay");
  assert.equal(replay.data.run.state, "completed");
  assert.equal((await call(env, { ...original, expected_message_count: 2 })).data.status, "replay");
  assert.deepEqual([env.DB.count("runs"), env.DB.count("messages"), env.DB.count("receipts")], before);
  assert.equal((await call(env, reservation(id, { expected_message_count: 2 }))).data.status, "pending");
});

test("context disagreement is reported before quota and admits no partial write", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  const run = (await call(env, reservation(id))).data.run;
  await call(env, completion(run));
  for (let i = 0; i < 19; i++) seedCompleted(env, OWNER_A, id);
  env.DB.sqlite.prepare("UPDATE owner_run_admissions SET admitted = 20").run();
  const denied = await call(env, reservation(id));
  assert.equal(denied.status, 409);
  assert.equal(denied.data.error, "context_changed");
  assert.equal((await call(env, reservation(id, { expected_message_count: 2 }))).data.error, "user_daily_limit");
  assert.equal(env.DB.count("runs"), 20);
  assert.equal(env.DB.count("messages"), 2);
  assert.equal(env.DB.count("receipts"), 3);
});

test("tokens, UTF-8 bytes and prompt character boundaries are enforced before writes", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  for (const changes of [{ prompt: "x".repeat(2001) }, { prompt: "\ud800" }, { prompt: "\0" },
    { max_output_tokens: 129 }, { max_output_tokens: 0 }, { max_output_tokens: 1.5 }, { max_output_tokens: "5" },
    { expected_message_count: undefined }, { expected_message_count: -1 }, { expected_message_count: 20 },
    { expected_message_count: 0.5 }, { expected_message_count: "0" }]) {
    assert.equal((await call(env, reservation(id, changes))).status, 400);
  }
  const run = (await call(env, reservation(id, { prompt: "\u{1f600}".repeat(2000), max_output_tokens: 8 }))).data.run;
  assert.equal((await call(env, completion(run, { output_tokens: 9 }))).data.error, "output_token_limit");
  assert.equal((await call(env, completion(run, { output_tokens: -1 }))).status, 400);
  assert.equal((await call(env, completion(run, { output_tokens: "5" }))).status, 400);
  assert.equal((await call(env, completion(run, { output: "\u{1f600}".repeat(2049) }))).status, 413);
  assert.equal((await call(env, completion(run, { output_tokens: undefined }))).status, 400);
  assert.equal(env.DB.count("messages"), 1);
  assert.equal((await call(env, completion(run, { output: "\u{1f600}".repeat(2048), output_tokens: 8 }))).status, 200);
});

test("daily owner quota is atomic and includes pending and uncertain runs", async (t) => {
  const env = environment(t);
  const fixture = seedConversation(env);
  for (let i = 0; i < 18; i++) seedCompleted(env, OWNER_A, fixture);
  const uncertainId = await conversation(env);
  const uncertainRun = (await call(env, reservation(uncertainId))).data.run;
  await call(env, { op: "uncertain", owner: OWNER_A, run_id: uncertainRun.id });
  env.DB.sqlite.prepare("UPDATE owner_run_admissions SET admitted = 19").run();
  const id1 = await conversation(env);
  const id2 = await conversation(env);
  const results = await Promise.all([call(env, reservation(id1)), call(env, reservation(id2))]);
  assert.equal(results.filter((result) => result.data.status === "pending").length, 1);
  assert.equal(results.filter((result) => result.data.error === "user_daily_limit").length, 1);
  assert.equal(env.DB.count("runs"), 20);
  assert.equal(env.DB.sqlite.prepare("SELECT admitted FROM owner_run_admissions").get().admitted, 20);
  assert.equal((await call(env, { op: "purge", owner: OWNER_A })).data.status, "deleted");
  assert.equal(env.DB.sqlite.prepare("SELECT admitted FROM owner_run_admissions").get().admitted, 20);
  const freshA = await conversation(env);
  assert.equal((await call(env, reservation(freshA))).data.error, "user_daily_limit");
  const freshB = await conversation(env, OWNER_B);
  assert.equal((await call(env, reservation(freshB, { owner: OWNER_B }))).data.status, "pending");
});

test("global daily quota is atomic across different owners", async (t) => {
  const env = environment(t);
  const fixtureOwner = "e".repeat(64);
  const fixture = seedConversation(env, fixtureOwner);
  for (let i = 0; i < 199; i++) seedCompleted(env, fixtureOwner, fixture);
  const idA = await conversation(env);
  const idB = await conversation(env, OWNER_B);
  env.DB.sqlite.prepare("INSERT INTO run_admissions VALUES (date('now'), 199)").run();
  const requests = [reservation(idA), reservation(idB, { owner: OWNER_B })];
  const results = await Promise.all(requests.map((request) => call(env, request)));
  assert.equal(results.filter((result) => result.data.status === "pending").length, 1);
  assert.equal(results.filter((result) => result.data.error === "global_daily_limit").length, 1);
  assert.equal(env.DB.count("runs"), 200);
  assert.equal(env.DB.sqlite.prepare("SELECT admitted FROM run_admissions").get().admitted, 200);
  const admittedIndex = results.findIndex((result) => result.data.status === "pending");
  assert.equal((await call(env, requests[admittedIndex])).data.status, "replay");
  assert.equal(env.DB.sqlite.prepare("SELECT admitted FROM run_admissions").get().admitted, 200);
  const admittedOwner = admittedIndex === 0 ? OWNER_A : OWNER_B;
  assert.equal((await call(env, { op: "purge", owner: admittedOwner })).data.status, "deleted");
  assert.equal(env.DB.count("runs"), 199);
  assert.equal(env.DB.sqlite.prepare("SELECT admitted FROM run_admissions").get().admitted, 200);
  const deniedIndex = 1 - admittedIndex;
  const deniedId = deniedIndex === 0 ? idA : idB;
  const deniedOwner = deniedIndex === 0 ? OWNER_A : OWNER_B;
  assert.equal((await call(env, reservation(deniedId, { owner: deniedOwner }))).data.error,
    "global_daily_limit");
});

test("conversation run cap and history reads remain bounded", async (t) => {
  const env = environment(t);
  const id = seedConversation(env);
  const noHistory = seedConversation(env);
  for (let i = 0; i < 100; i++) {
    seedCompleted(env, OWNER_A, noHistory, "2000-01-01");
    const runId = seedCompleted(env, OWNER_A, id, "2000-01-01");
    for (const role of ["user", "assistant"]) {
      env.DB.sqlite.prepare("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)")
        .run(OWNER_A, id, runId, role, "fixture", `2026-01-01T00:00:${String(i % 60).padStart(2, "0")}.000Z`);
    }
  }
  assert.equal((await call(env, reservation(noHistory))).data.error, "conversation_run_limit");
  assert.equal((await call(env, reservation(id))).data.error, "context_changed");
  const result = await call(env, { op: "get", owner: OWNER_A, id });
  assert.equal(result.data.messages.length, 100);
  assert.equal(result.data.runs.length, 100);
  assert.equal(env.DB.count("receipts"), 0);
  const newId = await conversation(env);
  assert.equal((await call(env, reservation(newId))).data.status, "pending");
});

test("a failed receipt insert rolls back every state write", async (t) => {
  const env = environment(t);
  env.DB.failOn = "INSERT INTO receipts";
  const failed = await call(env, { op: "create", owner: OWNER_A, id: crypto.randomUUID(), title: "Rollback" });
  assert.equal(failed.status, 503);
  assert.deepEqual(failed.data, { error: "store_unavailable" });
  assert.equal(env.DB.count("conversations"), 0);
  assert.equal(env.DB.count("conversation_admissions"), 0);
  env.DB.failOn = null;
  const id = await conversation(env);
  env.DB.failOn = "INSERT INTO receipts";
  assert.equal((await call(env, reservation(id))).status, 503);
  assert.equal(env.DB.count("runs"), 0);
  assert.equal(env.DB.count("messages"), 0);
  assert.equal(env.DB.count("run_admissions"), 0);
  env.DB.failOn = null;
  const run = (await call(env, reservation(id))).data.run;
  env.DB.failOn = "INSERT INTO receipts";
  assert.equal((await call(env, completion(run))).status, 503);
  assert.equal(env.DB.sqlite.prepare("SELECT state FROM runs WHERE id = ?").get(run.id).state, "pending");
  assert.equal(env.DB.count("messages"), 1);
  assert.equal(env.DB.count("receipts"), 2);
});

test("invalid D1 results and internal errors never reveal database details", async (t) => {
  const env = environment(t);
  const original = env.DB.batch.bind(env.DB);
  env.DB.batch = async () => [{ success: false, error: `synthetic detail ${SECRET}` }];
  assert.deepEqual((await call(env, { op: "check" })).data, { error: "store_unavailable" });
  env.DB.batch = async () => { throw new Error(`synthetic detail ${SECRET}`); };
  const response = await call(env, { op: "check" });
  assert.equal(response.status, 503);
  assert.ok(!JSON.stringify(response.data).includes(SECRET));
  env.DB.batch = original;
});

test("purge deletes one owner's content and receipts with an owner-free durable record", async (t) => {
  const env = environment(t);
  const completedId = await conversation(env);
  const pendingId = await conversation(env);
  const completedRun = (await call(env, reservation(completedId, { prompt: "Private prompt" }))).data.run;
  await call(env, completion(completedRun, { output: "Private output" }));
  await call(env, reservation(pendingId, { prompt: "Another private prompt" }));
  const otherId = await conversation(env, OWNER_B);
  const otherRun = (await call(env, reservation(otherId, {
    owner: OWNER_B, prompt: "Keep the other owner's prompt",
  }))).data.run;
  await call(env, completion(otherRun, { output: "Keep the other owner's output" }));

  assert.equal((await call(env, { op: "purge", owner: OWNER_A, extra: true })).status, 400);
  const result = await call(env, { op: "purge", owner: OWNER_A });
  assert.equal(result.status, 200);
  assert.equal(result.data.status, "deleted");
  assert.deepEqual(result.data.deleted, { messages: 3, runs: 2, receipts: 5, conversations: 2 });
  assert.equal(result.data.receipt.kind, "account.data_erased");
  assert.equal(result.data.receipt.evidence_class, "DECLARED");
  assert.equal(result.data.receipt.signing_status, "UNSIGNED");
  assert.equal(result.data.receipt.source_revision, env.SOURCE_REVISION);
  assert.deepEqual(Object.keys(result.data.receipt).sort(),
    ["id", "kind", "created_at", "source_revision", "evidence_class", "signing_status"].sort());
  for (const table of ["conversations", "runs", "messages", "receipts"]) {
    assert.equal(env.DB.sqlite.prepare(`SELECT COUNT(*) AS n FROM ${table} WHERE owner = ?`)
      .get(OWNER_A).n, 0);
  }
  assert.equal(env.DB.count("erasure_receipts"), 1);
  const other = await call(env, { op: "get", owner: OWNER_B, id: otherId });
  assert.equal(other.data.messages[0].content, "Keep the other owner's prompt");
  assert.equal(other.data.messages[1].content, "Keep the other owner's output");
  assert.equal(env.DB.sqlite.prepare("SELECT COUNT(*) AS n FROM receipts WHERE owner = ?").get(OWNER_B).n, 3);

  const replay = await call(env, { op: "purge", owner: OWNER_A });
  assert.equal(replay.data.status, "replay");
  assert.deepEqual(replay.data.deleted, { messages: 0, runs: 0, receipts: 0, conversations: 0 });
  assert.equal(replay.data.receipt, null);
  assert.equal(env.DB.count("erasure_receipts"), 1);
});

test("purge cascades model pins only for the erased owner's runs", async (t) => {
  const env = environment(t);
  const idA = await conversation(env);
  const idB = await conversation(env, OWNER_B);
  const runA = (await call(env, reservation(idA, { model_binding: modelBinding() }))).data.run;
  const runB = (await call(env, reservation(idB, { owner: OWNER_B,
    model_binding: modelBinding({ id: "SZLHOLDINGS/held-model" }) }))).data.run;
  assert.equal(env.DB.count("run_model_bindings"), 2);
  assert.equal((await call(env, { op: "purge", owner: OWNER_A })).data.status, "deleted");
  assert.equal(env.DB.count("run_model_bindings"), 1);
  assert.equal(env.DB.sqlite.prepare("SELECT * FROM run_model_bindings WHERE run_id = ?").get(runA.id),
    undefined);
  assert.equal(env.DB.sqlite.prepare("SELECT model_id FROM run_model_bindings WHERE run_id = ?")
    .get(runB.id).model_id, "SZLHOLDINGS/held-model");
  const remaining = (await call(env, { op: "get", owner: OWNER_B, id: idB })).data.runs[0];
  assert.equal(remaining.model_binding_evidence, "DECLARED");
});

test("purge rolls back all deletes if its receipt or a delete fails", async (t) => {
  const env = environment(t);
  const id = await conversation(env);
  await call(env, reservation(id));
  for (const failure of ["INSERT INTO erasure_receipts", "DELETE FROM runs"]) {
    env.DB.failOn = failure;
    assert.equal((await call(env, { op: "purge", owner: OWNER_A })).status, 503);
    assert.equal(env.DB.count("conversations"), 1);
    assert.equal(env.DB.count("runs"), 1);
    assert.equal(env.DB.count("messages"), 1);
    assert.equal(env.DB.count("receipts"), 2);
    assert.equal(env.DB.count("erasure_receipts"), 0);
  }
});

test("purge requires the service bearer before touching account rows", async (t) => {
  const env = environment(t);
  await conversation(env);
  const before = env.DB.calls.length;
  const result = await call(env, { op: "purge", owner: OWNER_A },
    { headers: { authorization: "Bearer invalid-secret" } });
  assert.equal(result.status, 401);
  assert.equal(env.DB.calls.length, before);
  assert.equal(env.DB.count("conversations"), 1);
  assert.equal(env.DB.count("erasure_receipts"), 0);
});
