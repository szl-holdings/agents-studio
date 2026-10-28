import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const source = readFileSync(join(fileURLToPath(new URL("..", import.meta.url)), "static", "studio.js"), "utf8");

function harness() {
  const nodes = new Map();
  const makeNode = () => ({
    disabled: false, hidden: false, value: "", textContent: "", className: "",
    scrollTop: 0, scrollHeight: 0, children: [], parent: null,
    classList: { toggle() {}, remove() {} },
    replaceChildren(...children) { this.children = []; this.append(...children); },
    querySelectorAll() {
      return this.children.filter((node) =>
        node.className.split(" ").some((name) => ["message", "run-state"].includes(name)));
    },
    append(...children) {
      for (const child of children) { child.parent = this; this.children.push(child); }
    },
    remove() {
      if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this);
    },
    focus() {},
  });
  const document = {
    body: makeNode(),
    createElement: makeNode,
    getElementById(id) {
      if (!nodes.has(id)) {
        nodes.set(id, makeNode());
      }
      return nodes.get(id);
    },
  };
  const window = { lucide: { createIcons() {} }, confirm: () => true };
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

function ok(body) { return { ok: true, status: 200, json: async () => body }; }
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
function signedOut() {
  return { user: null, csrf: "signed-out-csrf",
    ready: { auth: true, store: false, model: true } };
}
function signedInHarness() {
  const result = harness();
  result.context.sessionData = {
    user: { name: "Synthetic user" }, csrf: "synthetic-csrf",
    ready: { auth: true, store: true, model: true },
  };
  result.context.savedData = {
    conversation: { id: "conversation-a", title: "Synthetic private title" },
    messages: [{ role: "user", content: "Synthetic private message" }], runs: [],
  };
  vm.runInContext(`session = sessionData; current = savedData.conversation.id;
    conversations = [savedData.conversation]; renderConversation(savedData); renderList(); controls()`,
  result.context);
  result.nodes.get("prompt").value = "Synthetic private draft";
  result.nodes.get("search").value = "Synthetic private search";
  return result;
}
function assertSignedOut({ context, nodes }) {
  assert.equal(vm.runInContext("session?.user ?? null", context), null);
  assert.equal(vm.runInContext("current", context), null);
  assert.equal(vm.runInContext("snapshot", context), null);
  assert.equal(vm.runInContext("conversations.length", context), 0);
  assert.equal(nodes.get("messages").children.length, 0);
  assert.equal(nodes.get("title").textContent, "Agents Studio");
  assert.equal(nodes.get("welcome").hidden, false);
  assert.equal(nodes.get("export").disabled, true);
  assert.equal(nodes.get("prompt").value, "");
  assert.equal(nodes.get("search").value, "");
}

test("logout invalidates a conversation response that resolves after sign-out", async () => {
  const h = signedInHarness();
  const pending = deferred();
  h.context.fetch = async (path) => {
    if (path === "/api/conversations/conversation-a") return pending.promise;
    if (path === "/auth/logout") return ok({ status: "signed_out" });
    if (path === "/api/session") return ok(signedOut());
    throw new Error("Unexpected request: " + path);
  };
  const opening = vm.runInContext('openConversation("conversation-a")', h.context);
  await h.nodes.get("logout").onclick();
  pending.resolve(ok(h.context.savedData));
  await opening;
  assertSignedOut(h);
});

test("logout invalidates a prior session reload before it can restore signed-in state", async () => {
  const h = signedInHarness();
  const pending = deferred();
  let sessionReads = 0;
  const calls = [];
  h.context.fetch = async (path) => {
    calls.push(path);
    if (path === "/auth/logout") return ok({ status: "signed_out" });
    if (path === "/api/session") return ++sessionReads === 1 ? pending.promise : ok(signedOut());
    if (path === "/api/conversations") return ok({ conversations: [h.context.savedData.conversation] });
    throw new Error("Unexpected request: " + path);
  };
  const reloading = vm.runInContext("reload()", h.context);
  await h.nodes.get("logout").onclick();
  pending.resolve(ok(h.context.sessionData));
  await reloading;
  assertSignedOut(h);
  assert.equal(calls.includes("/api/conversations"), false);
});

test("confirmed deletion invalidates a reload begun by an earlier create continuation", async () => {
  const h = signedInHarness();
  const create = deferred(), deletion = deferred(), oldSession = deferred(), readStarted = deferred();
  let sessionReads = 0;
  h.context.fetch = async (path, options) => {
    if (path === "/api/conversations" && options.method === "POST") return create.promise;
    if (path === "/api/account") return deletion.promise;
    if (path === "/api/session") {
      if (++sessionReads === 1) { readStarted.resolve(); return oldSession.promise; }
      return ok(signedOut());
    }
    if (path === "/api/conversations") return ok({ conversations: [h.context.savedData.conversation] });
    if (path === "/api/conversations/conversation-a") return ok(h.context.savedData);
    throw new Error("Unexpected request: " + path);
  };
  const creating = vm.runInContext("createConversation()", h.context);
  const deleting = h.nodes.get("delete-account").onclick();
  create.resolve(ok({ conversation: h.context.savedData.conversation }));
  await readStarted.promise;
  deletion.resolve(ok({ status: "deleted" }));
  await deleting;
  oldSession.resolve(ok(h.context.sessionData));
  await creating;
  assertSignedOut(h);
});

test("newer anonymous reload wins over an older signed-in session response", async () => {
  const h = signedInHarness();
  const pending = deferred();
  let sessionReads = 0, lists = 0;
  h.context.fetch = async (path) => {
    if (path === "/api/session") return ++sessionReads === 1 ? pending.promise : ok(signedOut());
    if (path === "/api/conversations") { lists++; return ok({ conversations: [] }); }
    throw new Error("Unexpected request: " + path);
  };
  const older = vm.runInContext("reload()", h.context);
  await vm.runInContext("reload()", h.context);
  pending.resolve(ok(h.context.sessionData));
  await older;
  assertSignedOut(h);
  assert.equal(lists, 0);
});

test("newer anonymous reload wins over an older conversation list response", async () => {
  const h = signedInHarness();
  const pending = deferred(), listStarted = deferred();
  let sessionReads = 0;
  h.context.fetch = async (path) => {
    if (path === "/api/session") return ok(++sessionReads === 1 ? h.context.sessionData : signedOut());
    if (path === "/api/conversations") { listStarted.resolve(); return pending.promise; }
    if (path === "/api/conversations/conversation-a") return ok(h.context.savedData);
    throw new Error("Unexpected request: " + path);
  };
  const older = vm.runInContext("reload()", h.context);
  await listStarted.promise;
  await vm.runInContext("reload()", h.context);
  pending.resolve(ok({ conversations: [h.context.savedData.conversation] }));
  await older;
  assertSignedOut(h);
});

test("newer signed-in reload wins over an older same-generation list response", async () => {
  const h = signedInHarness();
  vm.runInContext("current = null", h.context);
  const oldList = deferred(), listStarted = deferred();
  let lists = 0;
  h.context.fetch = async (path) => {
    if (path === "/api/session") return ok(h.context.sessionData);
    if (path === "/api/conversations") {
      if (++lists === 1) { listStarted.resolve(); return oldList.promise; }
      return ok({ conversations: [{ id: "newer-list", title: "Newer synthetic list" }] });
    }
    throw new Error("Unexpected request: " + path);
  };
  const older = vm.runInContext("reload()", h.context);
  await listStarted.promise;
  await vm.runInContext("reload()", h.context);
  oldList.resolve(ok({ conversations: [h.context.savedData.conversation] }));
  await older;
  assert.equal(vm.runInContext("conversations[0].id", h.context), "newer-list");
});

test("reload does not reopen old selection after newer navigation begins", async () => {
  const h = signedInHarness();
  const listing = deferred(), listStarted = deferred(), openingB = deferred();
  const calls = [];
  h.context.fetch = async (path) => {
    calls.push(path);
    if (path === "/api/session") return ok(h.context.sessionData);
    if (path === "/api/conversations") { listStarted.resolve(); return listing.promise; }
    if (path === "/api/conversations/conversation-b") return openingB.promise;
    if (path === "/api/conversations/conversation-a") return ok(h.context.savedData);
    throw new Error("Unexpected request: " + path);
  };
  const reloading = vm.runInContext("reload()", h.context);
  await listStarted.promise;
  const opening = vm.runInContext('openConversation("conversation-b")', h.context);
  listing.resolve(ok({ conversations: [
    h.context.savedData.conversation, { id: "conversation-b", title: "Newer conversation" },
  ] }));
  await reloading;
  openingB.resolve(ok({
    conversation: { id: "conversation-b", title: "Newer conversation" }, messages: [], runs: [],
  }));
  await opening;
  assert.equal(vm.runInContext("current", h.context), "conversation-b");
  assert.equal(vm.runInContext("snapshot.conversation.id", h.context), "conversation-b");
  assert.equal(calls.includes("/api/conversations/conversation-a"), false);
});

for (const [button, endpoint] of [["logout", "/auth/logout"], ["delete-account", "/api/account"]]) {
  test(button + " clears private UI after confirmed success even when session refresh fails", async () => {
    const h = signedInHarness();
    h.context.fetch = async (path) => {
      if (path === endpoint) return ok({ status: button === "logout" ? "signed_out" : "deleted" });
      if (path === "/api/session") throw new Error("Synthetic offline refresh");
      throw new Error("Unexpected request: " + path);
    };
    await h.nodes.get(button).onclick();
    assertSignedOut(h);
  });
}

test("failed deletion preserves saved data and does not claim confirmed erasure", async () => {
  const h = signedInHarness();
  h.context.fetch = async (path) => {
    assert.equal(path, "/api/account");
    return { ok: false, status: 503, json: async () => ({ error: "erasure_uncertain" }) };
  };
  await h.nodes.get("delete-account").onclick();
  assert.equal(vm.runInContext("session.user.name", h.context), "Synthetic user");
  assert.equal(vm.runInContext("snapshot.conversation.id", h.context), "conversation-a");
  assert.equal(h.nodes.get("messages").children.length, 1);
  assert.match(h.nodes.get("notice").textContent, /uncertain/);
  assert.doesNotMatch(h.nodes.get("notice").textContent, /was deleted/);
});

test("newer signed-in reload wins over an older same-generation session response", async () => {
  const h = signedInHarness();
  vm.runInContext("current = null", h.context);
  const pending = deferred();
  let sessionReads = 0, lists = 0;
  const newer = { ...h.context.sessionData, user: { name: "Newer synthetic user" } };
  h.context.fetch = async (path) => {
    if (path === "/api/session") return ++sessionReads === 1 ? pending.promise : ok(newer);
    if (path === "/api/conversations") { lists++; return ok({ conversations: [] }); }
    throw new Error("Unexpected request: " + path);
  };
  const older = vm.runInContext("reload()", h.context);
  await vm.runInContext("reload()", h.context);
  pending.resolve(ok(h.context.sessionData));
  await older;
  assert.equal(vm.runInContext("session.user.name", h.context), "Newer synthetic user");
  assert.equal(lists, 1);
});
