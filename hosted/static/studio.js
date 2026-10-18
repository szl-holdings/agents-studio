"use strict";
const $ = (id) => document.getElementById(id);
let session = null;
let conversations = [];
let current = null;
let snapshot = null;
let busy = false;
let sequence = 0;

function icons() { window.lucide?.createIcons(); }
function notice(message = "") { $("notice").textContent = message; $("notice").hidden = !message; }
function reportLoad(error) {
  notice(session?.store_failure ? storageMessage(session.store_failure) : friendly(error));
}
function controls() {
  const signedIn = Boolean(session?.user);
  const ready = signedIn && session?.ready?.auth && session?.ready?.store && session?.ready?.model;
  $("new").disabled = !signedIn || !session?.ready?.store || busy;
  $("prompt").disabled = !ready || busy;
  $("send").disabled = !ready || !$("prompt").value.trim() || busy;
  $("login").disabled = !session?.ready?.auth || busy;
  $("login").hidden = signedIn;
  $("register").hidden = signedIn;
  $("logout").hidden = !signedIn;
  $("delete-account").hidden = !signedIn;
  $("delete-account").disabled = busy;
  $("export").disabled = !snapshot || busy;
  $("identity").textContent = session?.user?.name || "Not signed in";
  const storageDown = Boolean(session?.store_failure);
  $("status").textContent = busy ? "Running" : ready ? "Available"
    : storageDown ? "Storage unavailable" : signedIn ? "Unavailable" : "Sign in";
  $("status").classList.toggle("error", (signedIn && !ready) || storageDown);
}
function storageMessage(failure) {
  const messages = {
    missing_configuration: "Storage is not configured. Chat stays unavailable.",
    dns_failure: "Storage could not be resolved. Chat stays unavailable.",
    connect_failure: "Storage could not be reached. Chat stays unavailable.",
    tls_failure: "Storage could not complete a verified connection. Chat stays unavailable.",
    response_timeout: "Storage did not answer in time. Chat stays unavailable.",
    invalid_response: "Storage returned an unusable response. Chat stays unavailable.",
    schema_mismatch: "Storage schema does not match this application. Chat stays unavailable.",
  };
  return messages[failure] || "Storage is temporarily unavailable. Chat stays unavailable.";
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin", redirect: "error", cache: "no-store", ...options,
    headers: {"Content-Type": "application/json", "X-CSRF-Token": session?.csrf || "", ...options.headers},
  });
  const body = await response.json();
  if (!response.ok) {
    const error = new Error(body.error || "request_failed");
    error.status = response.status;
    throw error;
  }
  return body;
}
function friendly(error) {
  const messages = {
    quota_exceeded: "The request limit has been reached. Your saved conversation is unchanged.",
    model_busy: "The model is busy. Please try again shortly.",
    context_changed: "This conversation changed. Refresh before sending another message.",
    context_limit: "This conversation is too long for the CPU model. Start a new conversation.",
    authentication_required: "Please sign in again.",
    model_unavailable: "The model is not available yet. Your history remains saved.",
    store_unavailable: "Saved history is unavailable. No generation will be started.",
    erasure_uncertain: "The deletion outcome is uncertain. It was not retried. Refresh before trying again.",
    conversation_pending: "This conversation has an unresolved run. Refresh to check its saved status.",
  };
  return messages[error.message] || "The request did not finish normally. Refresh to check its saved status before starting another run.";
}
function renderList() {
  const query = $("search").value.toLowerCase();
  $("conversations").replaceChildren();
  for (const item of conversations.filter((c) => c.title.toLowerCase().includes(query))) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = item.title;
    button.title = item.title;
    button.classList.toggle("active", item.id === current);
    button.onclick = () => { if (!busy) openConversation(item.id).catch((e) => notice(friendly(e))); };
    $("conversations").append(button);
  }
}
function renderConversation(data) {
  snapshot = data;
  $("welcome").hidden = true;
  $("messages").querySelectorAll(".message,.run-state").forEach((node) => node.remove());
  $("title").textContent = data.conversation.title;
  for (const message of data.messages || []) {
    const article = document.createElement("article");
    article.className = `message ${message.role === "user" ? "user" : "assistant"}`;
    const heading = document.createElement("h3");
    heading.textContent = message.role === "user" ? "You" : "Assistant";
    const text = document.createElement("pre");
    text.textContent = message.content;
    article.append(heading, text);
    $("messages").append(article);
  }
  const unresolved = (data.runs || []).filter((run) => ["pending", "uncertain"].includes(run.state));
  if (unresolved.length) {
    const state = document.createElement("p");
    state.className = "run-state";
    state.textContent = "Run outcome pending or uncertain. It will not be sent again automatically.";
    $("messages").append(state);
  }
  $("messages").scrollTop = $("messages").scrollHeight;
  controls();
}
async function openConversation(id) {
  const request = ++sequence;
  const data = await api(`/api/conversations/${encodeURIComponent(id)}`);
  if (request !== sequence) return;
  current = id;
  renderConversation(data);
  renderList();
  document.body.classList.remove("menu-open");
}
async function reload() {
  session = await api("/api/session");
  controls();
  notice(session.store_failure ? storageMessage(session.store_failure) : "");
  if (session.user) {
    const data = await api("/api/conversations");
    conversations = data.conversations;
    renderList();
    if (current) await openConversation(current);
  } else {
    conversations = []; current = null; snapshot = null; renderList();
    $("messages").querySelectorAll(".message,.run-state").forEach((node) => node.remove());
    $("welcome").hidden = false;
    $("title").textContent = "Agents Studio";
  }
}
async function createConversation(title = "New conversation") {
  const data = await api("/api/conversations", {method: "POST", body: JSON.stringify({title})});
  current = data.conversation.id;
  await reload();
}
$("new").onclick = async () => {
  notice();
  try { await createConversation(); $("prompt").focus(); } catch (error) { notice(friendly(error)); }
};
$("refresh").onclick = () => { if (!busy) reload().catch(reportLoad); };
$("menu").onclick = () => document.body.classList.toggle("menu-open");
$("search").oninput = renderList;
$("prompt").oninput = () => { $("counter").textContent = `${$("prompt").value.length} / 2,000`; controls(); };
if (window.top !== window) $("login-label").textContent = "Open sign-in tab";
$("login").onclick = () => {
  if (window.top !== window) {
    window.open("/", "_blank", "noopener,noreferrer");
    return;
  }
  const form = document.createElement("form");
  form.method = "POST"; form.action = "/auth/login";
  const csrf = document.createElement("input");
  csrf.type = "hidden"; csrf.name = "csrf"; csrf.value = session.csrf;
  form.append(csrf); document.body.append(form); form.submit();
};
$("logout").onclick = async () => {
  try { await api("/auth/logout", {method: "POST", body: "{}"}); await reload(); notice(); }
  catch (error) { notice(friendly(error)); }
};
$("delete-account").onclick = async () => {
  if (busy || !window.confirm("Delete all your saved conversations and runs? This cannot be undone.")) return;
  busy = true; controls(); notice();
  try {
    await api("/api/account", {method: "DELETE", body: "{}"});
    sequence++;
    await reload();
    notice("Your saved data was deleted and you have been signed out.");
    document.body.classList.remove("menu-open");
  } catch (error) { notice(friendly(error)); }
  finally { busy = false; controls(); }
};
$("composer").onsubmit = async (event) => {
  event.preventDefault();
  if (busy || $("send").disabled) return;
  const prompt = $("prompt").value.trim();
  busy = true; controls(); notice();
  try {
    if (!current) await createConversation(prompt.slice(0, 80));
    const key = crypto.randomUUID();
    const result = await api("/api/runs", {method: "POST", body: JSON.stringify({conversation_id: current, prompt, idempotency_key: key})});
    $("prompt").value = ""; $("counter").textContent = "0 / 2,000";
    await openConversation(current);
    if (result.status === "uncertain") notice("The run outcome is uncertain. No automatic retry was made.");
  } catch (error) {
    notice(friendly(error));
    if (current) await openConversation(current).catch(() => {});
  } finally { busy = false; controls(); }
};
$("export").onclick = () => {
  if (!snapshot) return;
  const blob = new Blob([JSON.stringify(snapshot, null, 2)], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a"); link.href = url; link.download = "agents-studio-conversation.json";
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
};
icons();
reload().catch((error) => { reportLoad(error); controls(); });
