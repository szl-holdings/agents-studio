import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const source = readFileSync(join(fileURLToPath(new URL("..", import.meta.url)), "static", "studio.js"), "utf8");

function harness() {
  const nodes = new Map();
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) {
        nodes.set(id, {
          disabled: false, hidden: false, value: "", textContent: "",
          classList: { toggle() {} },
        });
      }
      return nodes.get(id);
    },
  };
  const window = { lucide: { createIcons() {} } };
  window.top = window;
  const context = vm.createContext({
    document, window,
    fetch: () => new Promise(() => {}),
  });
  vm.runInContext(source, context);
  nodes.get("prompt").value = "Synthetic prompt";
  return { context, nodes };
}

test("deferred store check permits server-guarded actions without claiming readiness", async () => {
  const { context, nodes } = harness();
  context.sessionData = {
    user: { name: "Synthetic user" }, csrf: "synthetic-csrf",
    ready: { auth: true, store: false, model: true },
    store_evidence: "UNKNOWN", store_check_deferred: true,
  };
  vm.runInContext("session = sessionData; controls()", context);
  assert.equal(nodes.get("new").disabled, false);
  assert.equal(nodes.get("prompt").disabled, false);
  assert.equal(nodes.get("send").disabled, false);
  assert.equal(nodes.get("status").textContent, "Check deferred");
  assert.equal(context.sessionData.ready.store, false);

  const calls = [];
  context.fetch = async (path, options) => {
    calls.push({ path, method: options.method });
    return { ok: false, status: 503, json: async () => ({ error: "store_unavailable" }) };
  };
  await nodes.get("new").onclick();
  assert.deepEqual(calls, [{ path: "/api/conversations", method: "POST" }]);
});

test("store failure or missing model still disables unsafe actions", () => {
  const { context, nodes } = harness();
  context.sessionData = {
    user: { name: "Synthetic user" },
    ready: { auth: true, store: false, model: true },
    store_check_deferred: true, store_failure: "connect_failure",
  };
  vm.runInContext("session = sessionData; controls()", context);
  assert.equal(nodes.get("new").disabled, true);
  assert.equal(nodes.get("prompt").disabled, true);
  assert.equal(nodes.get("send").disabled, true);
  assert.equal(nodes.get("status").textContent, "Storage unavailable");

  context.sessionData.store_failure = undefined;
  context.sessionData.ready.model = false;
  vm.runInContext("session = sessionData; controls()", context);
  assert.equal(nodes.get("new").disabled, false);
  assert.equal(nodes.get("prompt").disabled, true);
  assert.equal(nodes.get("send").disabled, true);
  assert.equal(nodes.get("status").textContent, "Check deferred");
});
