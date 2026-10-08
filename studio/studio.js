"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const fragment = new URLSearchParams(location.hash.slice(1));
  const incoming = fragment.get("token") || (location.hash && !location.hash.includes("=") ? location.hash.slice(1) : "");
  if (incoming) sessionStorage.setItem("szl-studio-token", incoming);
  const token = incoming || sessionStorage.getItem("szl-studio-token") || "";
  if (location.hash) history.replaceState(null, "", location.pathname);
  const state = { profile: {}, sessions: [], session: null, busy: false, activeJob: null, activeAction: null, activeTarget: null, messages: [], activities: [], cursor: 0, streamParts: new Map(), liveMessage: null, pollGeneration: 0, newDraft: false, pendingStart: false, submission: null, submissionMissing: false, disconnected: false, recovering: false, canCancel: false, cancellation: null, storageBlocked: false, pendingTarget: null, historyItems: [], historyTail: [], historyLoaded: false, historyMore: false, historyCursor: null, historyCursors: new Set(), historyError: false };
  let toastTimer;
  let cloudChecking = false;

  function icons() { if (window.lucide) window.lucide.createIcons(); }
  function element(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; }
  function icon(name) { const e = document.createElement("i"); e.dataset.lucide = name; e.setAttribute("aria-hidden", "true"); return e; }
  function iconButton(name, label, handler) { const e = element("button", undefined, "icon-button"); e.type = "button"; e.title = label; e.setAttribute("aria-label", label); e.append(icon(name)); e.addEventListener("click", handler); return e; }
  function toast(text) { clearTimeout(toastTimer); $("toast").textContent = text; $("toast").hidden = false; toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3500); }
  function notice(text = "") { $("notice").textContent = text; $("notice").hidden = !text; }
  function status(text, kind = "") { $("run-status").textContent = text; $("run-status").className = "status " + kind; }
  function safeUrl(value) { try { const u = new URL(value); return ["https:", "http:"].includes(u.protocol) && !u.username && !u.password ? u.href : null; } catch { return null; } }
  function link(label, href) { const url = safeUrl(href); if (!url) return element("span", label); const a = element("a", label); a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer"; return a; }

  // Only explicit Markdown token types become DOM elements; returned HTML is text.
  function markdownTokens(tokens, parent, depth = 0) {
    if (depth > 20) return;
    for (const t of tokens || []) {
      let node;
      if (t.type === "space") continue;
      if (t.type === "heading") node = element("h" + Math.min(4, Math.max(2, t.depth)));
      else if (t.type === "paragraph") node = element("p");
      else if (t.type === "strong") node = element("strong");
      else if (t.type === "em") node = element("em");
      else if (t.type === "del") node = element("del");
      else if (t.type === "blockquote") node = element("blockquote");
      else if (t.type === "br") node = element("br");
      else if (t.type === "hr") node = element("hr");
      else if (t.type === "codespan") node = element("code", t.text);
      else if (t.type === "code") { node = element("pre"); node.append(element("code", t.text)); }
      else if (t.type === "link") { node = link("", t.href); markdownTokens(t.tokens || [{ type: "text", text: t.text }], node, depth + 1); parent.append(node); continue; }
      else if (t.type === "image") { node = link(t.text || "Image source", t.href); }
      else if (t.type === "list") {
        node = element(t.ordered ? "ol" : "ul");
        if (t.ordered && Number.isInteger(t.start)) node.start = t.start;
        for (const item of t.items || []) { const li = element("li"); if (item.task) li.append(document.createTextNode(item.checked ? "[x] " : "[ ] ")); markdownTokens(item.tokens, li, depth + 1); node.append(li); }
      } else if (t.type === "table") {
        node = element("div", undefined, "table-scroll"); const table = element("table"); const thead = element("thead"); const row = element("tr");
        for (const cell of t.header || []) { const th = element("th"); markdownTokens(cell.tokens, th, depth + 1); row.append(th); } thead.append(row); table.append(thead);
        const body = element("tbody"); for (const cells of t.rows || []) { const tr = element("tr"); for (const cell of cells) { const td = element("td"); markdownTokens(cell.tokens, td, depth + 1); tr.append(td); } body.append(tr); } table.append(body); node.append(table);
      } else { if (t.tokens) { markdownTokens(t.tokens, parent, depth + 1); } else parent.append(document.createTextNode(t.text || t.raw || "")); continue; }
      if (t.tokens) markdownTokens(t.tokens, node, depth + 1);
      parent.append(node);
    }
  }
  function markdown(text, parent) {
    parent.replaceChildren();
    try { markdownTokens(window.marked.lexer(String(text || ""), { gfm: true }), parent); }
    catch { parent.textContent = String(text || ""); }
  }
  async function api(path, body, signal) {
    const options = { headers: { "X-Studio-Token": token }, cache: "no-store", signal };
    if (body !== undefined) { options.method = "POST"; options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    const response = await fetch(path, options); let value;
    try { value = await response.json(); } catch { throw new Error("The local server returned an unreadable response."); }
    if (!response.ok) { const error = new Error(value.error || `Request failed (${response.status}).`); error.status = response.status; throw error; }
    return value;
  }
  function saveSubmission(body) {
    try { sessionStorage.setItem("szl-studio-submission", JSON.stringify(body)); }
    catch { throw new Error("This tab cannot save its submission identity. Nothing was submitted."); }
    state.submission = body; state.submissionMissing = false;
  }
  function clearSubmission() {
    sessionStorage.removeItem("szl-studio-submission"); state.submission = null; state.submissionMissing = false;
  }
  function isGeneration(action) { return ["start", "send"].includes(action); }
  function appendMessage(message) { state.messages.push(message); if (state.historyLoaded) state.historyTail.push(message); }
  function resetHistory() { state.historyItems = []; state.historyTail = []; state.historyLoaded = false; state.historyMore = false; state.historyCursor = null; state.historyCursors.clear(); state.historyError = false; }
  function controls() {
    const locked = state.busy || !!state.submission || !!state.pendingTarget || state.storageBlocked || !token || !state.profile.credential_available || (!state.session && state.pendingStart);
    $("send").disabled = locked || !$("message").value.trim();
    $("new-chat").disabled = state.busy || !!state.submission || !!state.pendingTarget;
    $("refresh").disabled = state.busy || !state.session;
    $("load-history").disabled = state.busy || !state.profile.credential_available;
    $("export").disabled = state.messages.length === 0;
    $("message").disabled = state.busy || !!state.submission || !!state.pendingTarget || state.storageBlocked;
    const generation = state.activeJob && ["start", "send"].includes(state.activeAction);
    $("stop-run").hidden = !generation;
    $("stop-run").disabled = !state.canCancel || !!state.cancellation || state.disconnected;
    $("stop-run").title = state.cancellation ? "Stop requested; waiting for confirmed outcome" : "Stop active turn";
    $("send").hidden = !!generation;
    $("recovery-bar").hidden = !state.disconnected && !state.submissionMissing && !state.pendingTarget;
    $("check-outcome").hidden = !state.pendingTarget;
    $("check-outcome").disabled = state.busy || state.recovering || !state.profile.credential_available;
    $("recover-run").hidden = !!state.pendingTarget && !state.busy;
    $("recover-run").disabled = state.recovering;
    $("retry-submission").hidden = !state.submissionMissing;
    $("retry-submission").disabled = state.recovering || !state.profile.credential_available;
    $("recovery-label").textContent = state.pendingTarget ? "Provider outcome not yet reconciled" : state.submissionMissing ? "No acceptance record found" : "Run connection interrupted";
    $("load-older").hidden = !state.historyMore;
    $("load-older").disabled = state.busy || state.historyError || !state.historyCursor || !state.profile.credential_available;
    $("char-count").textContent = `${$("message").value.length.toLocaleString()} / 20,000`;
    for (const e of document.querySelectorAll("[data-prompt]")) e.disabled = state.busy || !!state.submission || !!state.pendingTarget;
  }
  function titleFor(id) { return state.sessions.find(s => s.id === id)?.title || "Saved conversation"; }
  function header() {
    $("conversation-title").textContent = state.session ? titleFor(state.session) : "New conversation";
    $("model-label").textContent = `${state.profile.name || "New agent"} / ${state.profile.model || "gpt-6-astra"}`;
    $("detail-model").textContent = state.profile.model || "gpt-6-astra";
    $("detail-key").textContent = state.profile.credential_available ? "Available on this computer" : "UNAVAILABLE";
    $("detail-session").textContent = state.session || "Not started";
    $("local-runtime-state").textContent = state.profile.name ? "Connected locally / 127.0.0.1" : "UNKNOWN";
    $("local-credential-state").textContent = state.profile.credential_available === true ? "Available to local process" : state.profile.credential_available === false ? "UNAVAILABLE" : "UNKNOWN";
    $("local-agent-name").textContent = `${state.profile.name || "New agent"} / ${state.profile.model || "gpt-6-astra"}`;
  }
  function renderSessions() {
    $("sessions").replaceChildren(); const query = $("search").value.toLowerCase();
    const list = state.sessions.filter(s => `${s.title || "Saved conversation"} ${s.id}`.toLowerCase().includes(query));
    $("session-count").textContent = state.sessions.length;
    if (!list.length) $("sessions").append(element("p", query ? "No matching conversations" : "No saved conversations", "empty-list"));
    for (const session of list) {
      const b = element("button", undefined, "session-item" + (session.id === state.session ? " active" : "")); b.disabled = state.busy || !!state.submission;
      b.append(icon("message-square")); const text = element("span"); text.append(element("b", session.title || "Saved conversation"));
      const date = session.updated_at ? new Date(session.updated_at) : null;
      text.append(element("small", date && !isNaN(date) ? date.toLocaleDateString(undefined, { month: "short", day: "numeric" }) : session.id.slice(0, 17) + "...")); b.append(text); b.title = session.id;
      b.addEventListener("click", () => selectSession(session.id)); $("sessions").append(b);
    } icons();
  }
  function renderMessages() {
    $("messages").replaceChildren(); $("empty").hidden = !!state.messages.length || state.busy;
    $("empty-heading").textContent = state.session ? "Your saved conversation" : "What are we working on?";
    $("load-history").hidden = !state.session;
    $("suggestions").hidden = !!state.session;
    for (const msg of state.messages) {
      const article = element("article", undefined, "message " + msg.role); if (msg.id) article.dataset.itemId = msg.id; const meta = element("div", undefined, "message-meta");
      meta.append(element("span", msg.role === "user" ? "YOU" : "SZL", "avatar")); meta.append(element("b", msg.role === "user" ? "You" : "New agent"));
      if (msg.provisional || msg.cancelled) meta.append(element("span", msg.cancelled ? "Partial / cancelled" : "Streaming / provisional", "provisional"));
      meta.append(iconButton("copy", "Copy message", () => copy(msg.text))); article.append(meta);
      const content = element("div", undefined, "message-content");
      if (msg.role === "user") { const p = element("p", msg.text); p.style.whiteSpace = "pre-wrap"; content.append(p); } else markdown(msg.text || (msg.provisional ? "Working..." : ""), content);
      article.append(content);
      if (msg.sources?.length) { const sources = element("div", undefined, "source-links"); for (const source of msg.sources) if (safeUrl(source.url)) sources.append(link(source.title || new URL(source.url).hostname, source.url)); article.append(sources); }
      $("messages").append(article);
    }
    icons(); controls();
  }
  function scrollBottom() { const box = $("chat-view"); box.scrollTop = box.scrollHeight; }
  async function copy(text) { try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast("Clipboard unavailable in this browser"); } }
  function activity(label, detail = "", kind = "activity") {
    if (state.activities.length >= 300) state.activities.shift();
    state.activities.push({ label, detail, kind, time: new Date().toISOString() }); renderActivity();
  }
  function renderActivity() {
    $("activity-list").replaceChildren(); $("activity-empty").hidden = state.activities.length > 0; $("activity-count").textContent = state.activities.length;
    for (const item of state.activities) { const li = element("li"); li.append(icon(item.kind)); const content = element("div", item.label); if (item.detail) content.append(element("small", item.detail)); li.append(content); const time = element("time", new Date(item.time).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" })); time.dateTime = item.time; li.append(time); $("activity-list").append(li); } icons();
  }
  function setView(tab) {
    for (const name of ["chat", "activity", "connections"]) {
      const selected = name === tab;
      $(name + "-view").hidden = !selected; $(name + "-tab").setAttribute("aria-selected", String(selected));
      $(name + "-tab").tabIndex = selected ? 0 : -1;
    }
  }
  function cloudValue(id, text, kind = "unknown") { $(id).textContent = text; $(id).className = "connection-value-" + kind; }
  function clearCloudObservation() {
    for (const id of ["control", "inference", "registry", "caller", "provider", "egress", "witness", "source"]) cloudValue("cloud-" + id + "-state", "UNKNOWN");
    $("cloud-revision").textContent = "UNKNOWN"; $("cloud-expected-revision").textContent = "UNKNOWN"; $("cloud-observed").textContent = "Not checked";
    $("cloud-error").textContent = ""; $("cloud-error").hidden = true;
  }
  function validateCloudObservation(value) {
    const object = v => v !== null && typeof v === "object" && !Array.isArray(v);
    const boolean = v => v === null || typeof v === "boolean";
    const http = v => v === null || Number.isInteger(v) && v >= 100 && v <= 599;
    const revision = v => v === null || typeof v === "string" && /^[0-9a-f]{40}$/.test(v);
    const states = ["READY", "UNAVAILABLE", "UNKNOWN"];
    const { source, control, inference, errors } = object(value) ? value : {};
    if (!object(value) || value.schema !== "szl.studio.cloud-connection/v1" || value.evidence_class !== "MEASURED" || value.origin !== "https://szlholdings-szl-router-control.hf.space" || typeof value.observed_at !== "string" || !Number.isFinite(Date.parse(value.observed_at)) || !object(source) || !object(control) || !object(inference) || !object(inference.checks)) throw new Error("The cloud observation is invalid.");
    if (![null, "szl-holdings/szl-router"].includes(source.repository) || !revision(source.revision) || typeof source.expected_revision !== "string" || !revision(source.expected_revision) || !["MATCH", "MISMATCH", "UNKNOWN"].includes(source.state) || !boolean(source.matches) || !boolean(source.controlled_files_match) || !http(source.http_status)) throw new Error("The cloud source observation is invalid.");
    if (source.state === "MATCH" && (source.matches !== true || source.controlled_files_match !== true || source.revision !== source.expected_revision || source.http_status !== 200)) throw new Error("The cloud source observation is inconsistent.");
    if (!states.includes(control.state) || !states.includes(inference.state) || !http(control.http_status) || !http(inference.http_status) || !boolean(inference.ready_for_requests)) throw new Error("The cloud readiness observation is invalid.");
    for (const key of ["registry_valid", "egress_enabled", "caller_auth_configured", "provider_configured"]) if (!boolean(inference.checks[key])) throw new Error("A cloud readiness field is invalid.");
    if (control.state === "READY" && control.http_status !== 200 || inference.state === "READY" && (inference.http_status !== 200 || inference.ready_for_requests !== true || Object.values(inference.checks).some(v => v !== true))) throw new Error("The cloud readiness observation is inconsistent.");
    for (const field of ["provider_reachability", "inference_witness", "basis"]) if (inference[field] !== null && (typeof inference[field] !== "string" || inference[field].length > 128)) throw new Error("A cloud evidence field is invalid.");
    if (!Array.isArray(errors) || errors.length > 6 || errors.some(e => !object(e) || !["/api/source", "/readyz", "/readyz/inference"].includes(e.endpoint) || typeof e.code !== "string" || e.code.length > 100 || typeof e.message !== "string" || e.message.length > 300)) throw new Error("The cloud error observation is invalid.");
    return value;
  }
  function showCloudObservation(value) {
    const { source, control, inference } = validateCloudObservation(value);
    cloudValue("cloud-control-state", control.state === "READY" ? "Ready / HTTP 200" : control.state + (control.http_status ? ` / HTTP ${control.http_status}` : ""), control.state === "READY" ? "good" : "warning");
    cloudValue("cloud-inference-state", inference.state === "READY" ? "Configuration ready / runtime UNKNOWN" : inference.state + (inference.http_status ? ` / HTTP ${inference.http_status}` : ""), inference.state === "UNKNOWN" ? "unknown" : "warning");
    const field = (id, flag, yes, no) => cloudValue(id, flag === true ? yes : flag === false ? no : "UNKNOWN", flag === true ? "good" : flag === false ? "warning" : "unknown");
    field("cloud-registry-state", inference.checks.registry_valid, "Valid", "Missing or invalid");
    field("cloud-caller-state", inference.checks.caller_auth_configured, "Configured", "Not configured");
    field("cloud-provider-state", inference.checks.provider_configured, "Configured", "Not configured");
    field("cloud-egress-state", inference.checks.egress_enabled, "Enabled", "Disabled");
    cloudValue("cloud-witness-state", "UNKNOWN / no independent runtime check");
    cloudValue("cloud-source-state", source.state === "MATCH" ? "MATCH / revision and file metadata" : source.state, source.state === "MATCH" ? "good" : source.state === "MISMATCH" ? "warning" : "unknown");
    $("cloud-revision").textContent = source.revision || "UNKNOWN"; $("cloud-expected-revision").textContent = source.expected_revision;
    $("cloud-observed").textContent = new Date(value.observed_at).toLocaleString() + " / MEASURED readback";
    if (value.errors.length) { $("cloud-error").textContent = value.errors.map(e => `${e.endpoint}: ${e.message}`).join(" "); $("cloud-error").hidden = false; }
  }
  async function checkCloudConnection() {
    if (cloudChecking || !token) return;
    cloudChecking = true; $("cloud-check").disabled = true; $("connections-view").setAttribute("aria-busy", "true"); clearCloudObservation(); $("cloud-observed").textContent = "Checking";
    try { showCloudObservation(await api("/api/cloud-status", undefined, AbortSignal.timeout(15000))); }
    catch (e) { clearCloudObservation(); $("cloud-error").textContent = `Connection check unavailable. ${e.message}`; $("cloud-error").hidden = false; $("cloud-observed").textContent = "UNAVAILABLE / no current readback"; }
    finally { cloudChecking = false; $("cloud-check").disabled = !token; $("connections-view").setAttribute("aria-busy", "false"); }
  }
  function clearView() { state.messages = []; state.activities = []; state.streamParts.clear(); state.liveMessage = null; resetHistory(); $("history-note").hidden = true; $("detail-turn").textContent = "Not observed"; $("detail-usage").textContent = "UNAVAILABLE"; renderMessages(); renderActivity(); notice(); }
  async function selectSession(id) {
    if (state.busy || state.submission) return; state.newDraft = false; state.session = id; clearView(); header(); renderSessions(); closeSidebar(); setView("chat"); await submitJob("status");
  }
  function newConversation() {
    if (state.busy || state.submission || state.pendingTarget) return; state.newDraft = true; state.session = null; clearView(); header(); renderSessions(); status("Ready"); $("operation-label").textContent = "No run submitted"; $("message").value = ""; controls(); closeSidebar(); setView("chat"); $("message").focus();
  }
  async function updateState() {
    const remote = await api("/api/state"); state.profile = remote.profile || {}; state.sessions = remote.sessions || [];
    state.pendingStart = !!remote.pending_start;
    if (!state.newDraft && !state.session) state.session = remote.current_session_id || null;
    const pendingSessions = remote.pending_submissions || [];
    const candidates = (remote.jobs || []).filter(j => isGeneration(j.action) && (j.status === "pending" || (j.status === "failed" && (j.action === "start" ? state.pendingStart : pendingSessions.includes(j.session_id)))));
    state.pendingTarget = candidates.find(j => j.client_submission_id && j.client_submission_id === state.submission?.client_submission_id) || candidates.find(j => j.id === state.pendingTarget?.id) || candidates.find(j => j.session_id === state.session || (j.action === "start" && !j.session_id && state.pendingStart)) || null;
    header(); renderSessions(); controls();
    return remote;
  }
  function messagesFromItems(items) {
    return [...items].reverse().filter(i => i.type === "message" && ["user", "assistant"].includes(i.role)).map(i => ({
      id: i.id,
      role: i.role,
      text: (i.content || []).filter(c => ["input_text", "output_text", "text"].includes(c.type)).map(c => c.text || "").join("\n"),
      provisional: i.status && !["completed", "cancelled"].includes(i.status) && i.role === "assistant",
      cancelled: i.status === "cancelled" && i.role === "assistant",
      sources: (i.content || []).flatMap(c => (c.annotations || []).map(a => a.url_citation || a).filter(a => a.url))
    })).filter(m => m.text);
  }
  function historyPage(page, append = false, after = null) {
    const items = Array.isArray(page) ? page : page?.data;
    if (!Array.isArray(items)) throw new Error("Saved history has no valid item list.");
    const hasMore = page.has_more === true;
    const cursor = page.last_id;
    if (hasMore && (!items.length || typeof cursor !== "string" || !/^[A-Za-z0-9_.:~+/=-]{1,512}$/.test(cursor) || cursor === after || state.historyCursors.has(cursor))) throw new Error("The history cursor is missing or repeated. Refresh the session before continuing.");
    if (append && (items.some(i => !i || typeof i.id !== "string") || after !== state.historyCursor)) throw new Error("The history page does not match the saved cursor.");
    const known = new Set(state.historyItems.map(i => i.id).filter(Boolean));
    const next = append ? [...state.historyItems] : [];
    for (const item of items) { if (!item || typeof item !== "object") throw new Error("Invalid saved history item."); if (item.id && known.has(item.id)) continue; if (item.id) known.add(item.id); next.push(item); }
    if (append && hasMore && next.length === state.historyItems.length) throw new Error("History did not advance. Refresh before continuing.");
    state.historyItems = next; state.historyMore = hasMore; state.historyCursor = hasMore ? cursor : null; state.historyLoaded = true; state.historyError = false;
    if (cursor) state.historyCursors.add(cursor);
    state.messages = messagesFromItems(next).concat(state.historyTail);
    $("history-note").hidden = false; $("history-note").textContent = page.has_more === false ? `${next.length} saved items loaded` : page.has_more === true ? `${next.length} saved items loaded / earlier history remains` : `${next.length} saved items loaded / completeness UNKNOWN`;
  }
  function savedMessages(result) {
    const page = result.items || {}; const items = Array.isArray(page) ? page : page.data || [];
    resetHistory(); historyPage(page);
    const turn = result.latest_turn; $("detail-turn").textContent = turn?.status || "Not observed";
    $("detail-usage").textContent = turn?.usage ? JSON.stringify(turn.usage) : "UNAVAILABLE";
    status(result.session?.status || "Status read");
    if (result.session?.error) notice("Session error: " + JSON.stringify(result.session.error));
    if (result.session?.required_actions?.length) notice("This session requires an application action. No local tool execution is configured. Inspect the Activity view before continuing.");
    for (const i of [...items].reverse()) if (String(i.type).endsWith("_call")) activity(i.type === "web_search_call" ? "Web search" : i.type, i.status || "unknown", "globe-2");
    renderMessages(); scrollBottom();
  }
  function olderMessages(result) {
    if (result.session_id !== state.session) throw new Error("This history page belongs to another conversation.");
    const box = $("chat-view"), top = box.scrollTop, height = box.scrollHeight;
    historyPage(result.items, true, result.after); renderMessages();
    box.scrollTo({ top: top + box.scrollHeight - height, behavior: "instant" });
  }
  function handleEvent(record) {
    if (record.type === "log") { const text = String(record.data || ""); if (text.trim()) activity("Runner", text.slice(0, 1500), "terminal"); return; }
    if (record.type !== "event") return;
    const e = record.data; if (!e || typeof e !== "object") return;
    const kind = String(e.type || "event");
    if (e.session?.id || e.session_id) { state.session = e.session?.id || e.session_id; state.newDraft = false; header(); }
    if (kind === "agent.session.turn.output_text.delta" || kind === "agent.session.turn.output_text.done") {
      const key = JSON.stringify([e.item_id, e.output_index, e.content_index]);
      const prior = state.streamParts.get(key) || "";
      state.streamParts.set(key, kind.endsWith(".delta") ? prior + (e.delta || "") : e.text || "");
      if (!state.liveMessage) { state.liveMessage = { role: "assistant", text: "", provisional: true }; appendMessage(state.liveMessage); }
      state.liveMessage.text = [...state.streamParts.values()].join("\n\n"); renderMessages(); scrollBottom(); return;
    }
    if (kind.includes("item") && e.item?.type === "web_search_call") { const item = e.item; activity("Web search", [item.status, item.action?.query || item.query || ""].filter(Boolean).join(" / "), "globe-2"); }
    else if (/turn\.(completed|failed|cancelled)$/.test(kind)) activity(kind.split(".").slice(-2).join(" "), e.turn?.error ? JSON.stringify(e.turn.error) : "", kind.endsWith("completed") ? "check" : "circle-alert");
    else if (!kind.includes("delta")) activity(kind.replace(/^agent\.session\.?/, "").replaceAll(".", " "), "", "activity");
  }
  async function poll(id, action, generation, reconcileTarget = state.activeTarget) {
    while (generation === state.pollGeneration) {
      let job;
      try { job = await api(`/api/jobs/${encodeURIComponent(id)}?after=${state.cursor}`); }
      catch (e) { notice(`Connection interrupted. The run may still be active. No message was resent. ${e.message}`); status("Connection lost", "pending"); state.disconnected = true; controls(); return; }
      state.disconnected = false; state.canCancel = !!job.can_cancel;
      state.cancellation = job.cancellation?.status || state.cancellation;
      for (const event of job.events || []) handleEvent(event);
      state.cursor = job.next_cursor ?? state.cursor;
      if (job.session_id) { state.session = job.session_id; state.newDraft = false; header(); }
      if (["queued", "running"].includes(job.status)) {
        if (state.cancellation === "requested") { status("Stop requested", "running"); $("operation-label").textContent = "Cancellation awaiting confirmation"; }
        else if (state.cancellation === "unknown") { status("Stop outcome unknown", "pending"); $("operation-label").textContent = "UNKNOWN / cancellation not confirmed"; }
        else $("operation-label").textContent = isGeneration(action) ? "Turn in progress" : action === "reconcile" ? "Reading saved provider outcome" : "Reading saved state";
        controls(); await new Promise(r => setTimeout(r, 400)); continue;
      }
      state.busy = false; state.activeJob = null; state.activeAction = null; state.canCancel = false; $("activity-status").textContent = job.status;
      if (isGeneration(action) && job.status !== "pending") clearSubmission();
      if (job.status === "completed") {
        if (action === "reconcile") {
          const result = job.result || {};
          if (!reconcileTarget || result.target_job_id !== reconcileTarget.id) throw new Error("Provider recovery returned a different target. The original submission remains unresolved.");
          if (result.outcome === "resolved" && ["completed", "cancelled", "failed"].includes(result.target_status)) {
            if (result.snapshot?.session?.id && result.snapshot.session.id !== result.session_id) throw new Error("The recovery snapshot does not match its session.");
            if (!state.submission || reconcileTarget.client_submission_id === state.submission.client_submission_id) clearSubmission();
            state.pendingTarget = null; state.disconnected = false; state.session = result.session_id || state.session; state.newDraft = !state.session;
            if (result.snapshot) savedMessages(result.snapshot);
            status(result.target_status === "completed" ? "Recovered" : result.target_status === "cancelled" ? "Cancelled" : "Recovered failure", result.target_status === "completed" ? "" : "pending");
            notice(result.detail || "The saved provider outcome was reconciled. No message was resent.");
            $("operation-label").textContent = "Provider outcome reconciled / MEASURED";
          } else {
            state.pendingTarget = reconcileTarget; state.disconnected = true; status("Outcome unknown", "pending");
            notice(result.detail || "The original provider outcome could not be uniquely verified. Nothing was resent.");
            $("operation-label").textContent = "UNKNOWN / no message resent";
          }
        }
        else if (action === "history") { olderMessages(job.result); status("History loaded"); $("operation-label").textContent = "Saved history retrieved"; }
        else if (action === "status" && job.result) { savedMessages(job.result); $("operation-label").textContent = "Session state retrieved"; }
        else {
          const text = job.result?.text;
          if (state.liveMessage) { if (text) state.liveMessage.text = text; state.liveMessage.provisional = false; }
          else if (text) appendMessage({ role: "assistant", text });
          status("Completed"); $("detail-turn").textContent = "completed"; renderMessages(); scrollBottom();
          $("operation-label").textContent = "Turn completed / MEASURED";
        }
      } else if (job.status === "cancelled" && state.cancellation === "confirmed") {
        const text = job.result?.text;
        if (state.liveMessage) { if (text) state.liveMessage.text = text; state.liveMessage.provisional = false; state.liveMessage.cancelled = true; }
        else if (text) appendMessage({ role: "assistant", text, cancelled: true });
        status("Cancelled", "cancelled"); $("detail-turn").textContent = "cancelled";
        $("operation-label").textContent = "Cancellation confirmed / MEASURED";
        notice("The provider confirmed cancellation. Any partial output is preserved."); renderMessages();
      } else {
        status(job.status === "pending" ? "Outcome unknown" : "Run failed", job.status);
        notice(job.error || "The run did not complete. Refresh the session before sending another message.");
        activity(job.status === "pending" ? "Outcome unknown" : "Run failed", job.error || "", "circle-alert");
        $("operation-label").textContent = job.status === "pending" ? "UNKNOWN / no automatic retry" : "Run failed";
        if (job.status === "pending" && state.submission) state.disconnected = true;
        if (isGeneration(action) && job.status === "pending") state.pendingTarget = job;
        if (action === "reconcile") { state.pendingTarget = reconcileTarget; state.disconnected = true; }
      }
      try { await updateState(); } catch (e) { notice(e.message); }
      controls(); renderSessions(); return;
    }
  }
  async function submitJob(action, message, extra = {}) {
    if (state.busy || (isGeneration(action) && (state.submission || state.pendingTarget))) return;
    const body = { action, ...extra }; if (message !== undefined) body.message = message; if (state.session && action !== "start") body.session_id = state.session;
    if (isGeneration(action)) {
      try { body.client_submission_id = crypto.randomUUID(); saveSubmission(body); }
      catch (e) { notice(e.message); return; }
    }
    state.busy = true; state.streamParts.clear(); state.liveMessage = null; notice(); status(action === "history" ? "Loading history" : action === "status" ? "Refreshing" : "Running", "running"); controls(); renderSessions();
    $("operation-label").textContent = isGeneration(action) ? "Submission awaiting acknowledgement" : "Reading saved state";
    if (isGeneration(action)) { appendMessage({ role: "user", text: message }); renderMessages(); scrollBottom(); }
    await dispatch(body);
  }
  async function dispatch(body) {
    try {
      const result = await api("/api/jobs", body);
      if (result.retained === false) { state.busy = false; state.disconnected = true; notice("This submission was already accepted; its local transcript has expired. It was not submitted again. Inspect the saved provider session."); controls(); return; }
      state.activeJob = result.job_id; state.activeAction = body.action; state.cursor = 0; state.pollGeneration++; state.disconnected = false; state.cancellation = null;
      $("activity-status").textContent = isGeneration(body.action) ? "Run in progress" : "Reading saved state";
      if (isGeneration(body.action)) { $("message").value = ""; sessionStorage.removeItem("szl-studio-draft"); }
      await poll(result.job_id, body.action, state.pollGeneration);
    } catch (e) {
      state.busy = false; state.disconnected = !!state.submission;
      if (body.action === "history") { state.historyError = true; status("History unavailable", "pending"); notice(e.message); $("operation-label").textContent = "History unavailable / refresh required"; }
      else if (body.action === "status") { status("Status unavailable", "pending"); notice(e.message); $("operation-label").textContent = "Provider state UNKNOWN"; }
      else { status("Request not confirmed", "pending"); notice(`${e.message} No automatic retry was made. The saved submission identity has been retained.`); $("operation-label").textContent = "UNKNOWN / no automatic retry"; }
      controls(); renderSessions();
      if (isGeneration(body.action) && state.submission) await recoverSubmission();
    }
  }
  async function recoverSubmission() {
    if (state.recovering) return;
    state.recovering = true; state.submissionMissing = false; controls();
    let jobId = state.activeJob, action = state.activeAction;
    try {
      if (state.submission && state.activeAction !== "reconcile") {
        const found = await api(`/api/submissions/${encodeURIComponent(state.submission.client_submission_id)}`);
        if (!found.retained) {
          state.busy = false;
          if (["completed", "cancelled"].includes(found.provider_terminal_status) && found.provider_terminal_status === found.status) {
            state.session = found.observed_session_id || state.submission.session_id || state.session; state.newDraft = !state.session;
            clearSubmission(); state.disconnected = false; $("message").value = ""; sessionStorage.removeItem("szl-studio-draft");
            status("Previously " + found.provider_terminal_status); notice("The saved record confirms this submission ended. Its local transcript expired; no message was resent."); header(); renderMessages();
          } else { state.disconnected = true; status("Saved record expired", "pending"); notice("This submission was already accepted. Its local transcript has expired; no retry was made. Inspect the saved provider session."); }
          return;
        }
        jobId = found.job_id; action = state.submission.action;
        $("message").value = ""; sessionStorage.removeItem("szl-studio-draft");
      }
      if (!jobId) throw new Error("No saved job identity is available.");
      state.activeJob = jobId; state.activeAction = action; state.busy = true; state.disconnected = false;
      status("Reconnected", "running"); notice(); controls();
    } catch (e) {
      state.busy = false; state.disconnected = true;
      if (e.status === 404 && state.submission) { state.submissionMissing = true; notice("No local acceptance record was found. The saved submission can be retried with the same identity."); status("Not confirmed", "pending"); }
      else { notice(`Recovery unavailable. Nothing was resent. ${e.message}`); status("Outcome unknown", "pending"); }
      return;
    } finally { state.recovering = false; controls(); renderSessions(); }
    state.pollGeneration++;
    await poll(jobId, action, state.pollGeneration);
  }
  async function retrySubmission() {
    if (!state.submission || !state.submissionMissing || state.recovering) return;
    state.submissionMissing = false; state.busy = true; state.disconnected = false; notice(); status("Submitting saved request", "running"); controls();
    await dispatch(state.submission);
  }
  async function checkOutcome() {
    if (!state.pendingTarget || state.busy || state.recovering) return;
    const target = state.pendingTarget;
    state.activeTarget = target; state.activeJob = null; state.activeAction = "reconcile"; state.busy = true; state.recovering = true; state.canCancel = false; state.cancellation = null;
    notice(); status("Checking provider", "running"); $("operation-label").textContent = "Reading saved provider outcome"; controls();
    try {
      const accepted = await api(`/api/jobs/${encodeURIComponent(target.id)}/reconcile`, {});
      state.activeJob = accepted.job_id; state.activeAction = "reconcile"; state.cursor = 0; state.pollGeneration++; state.recovering = false;
      await poll(accepted.job_id, "reconcile", state.pollGeneration, target);
    } catch (e) {
      state.busy = false; state.activeAction = null; state.pendingTarget = target; state.disconnected = true; status("Recovery unavailable", "pending"); notice(`${e.message} No message was resent.`);
      $("operation-label").textContent = "UNKNOWN / original submission unresolved";
    } finally { state.recovering = false; controls(); renderSessions(); }
  }
  async function stopRun() {
    if (!state.activeJob || !state.canCancel || state.cancellation) return;
    const id = state.activeJob;
    state.cancellation = "requested"; controls(); status("Stop requested", "running");
    $("operation-label").textContent = "Cancellation awaiting confirmation";
    activity("Stop requested", "Provider cancellation is not yet confirmed.", "square");
    try {
      const result = await api(`/api/jobs/${encodeURIComponent(id)}/cancel`, {});
      if (state.activeJob === id) state.cancellation = result.cancellation?.status || "unknown";
    } catch (e) {
      if (state.activeJob === id) { state.cancellation = "unknown"; status("Stop outcome unknown", "pending"); notice(`Cancellation was not confirmed. No automatic retry was made. ${e.message}`); }
    }
    controls();
  }
  function exportConversation() {
    const title = state.session ? titleFor(state.session) : "Conversation";
    const text = `# ${title}\n\nModel: ${state.profile.model || "gpt-6-astra"}\nSession: ${state.session || "Not started"}\nExported: ${new Date().toISOString()}\n\n` + state.messages.map(m => `## ${m.role === "user" ? "You" : "New agent"}${m.cancelled ? " (partial / cancelled)" : m.provisional ? " (provisional)" : ""}\n\n${m.text}`).join("\n\n");
    const url = URL.createObjectURL(new Blob([text], { type: "text/markdown;charset=utf-8" })); const a = element("a"); a.href = url; a.download = "szl-conversation-" + new Date().toISOString().slice(0, 10) + ".md"; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); toast("Conversation exported");
  }
  async function restoreLocalJobs(remote, cached) {
    const cutoff = cached ? Date.parse(cached.created_at) : 0;
    const recent = (remote.jobs || []).filter(j => j.session_id === state.session && ["start", "send"].includes(j.action) && !["queued", "running"].includes(j.status) && Date.parse(j.created_at) > cutoff).slice(0, 20).reverse();
    for (const summary of recent) {
      const job = await api(`/api/jobs/${encodeURIComponent(summary.id)}?after=0`);
      if (job.message) appendMessage({ role: "user", text: job.message });
      if (job.result?.text) appendMessage({ role: "assistant", text: job.result.text, provisional: !["completed", "cancelled"].includes(job.status), cancelled: job.status === "cancelled" });
      if (job.error) activity("Saved run: " + job.status, job.error, "circle-alert");
    }
    if (recent.length) { status("Saved locally"); $("operation-label").textContent = "Local history / refresh for provider state"; renderMessages(); scrollBottom(); }
  }
  function closeSidebar() { $("sidebar").classList.remove("open"); $("menu").setAttribute("aria-expanded", "false"); }
  $("new-chat").addEventListener("click", newConversation);
  $("recover-run").addEventListener("click", recoverSubmission);
  $("check-outcome").addEventListener("click", checkOutcome);
  $("load-older").addEventListener("click", () => { if (state.historyMore && state.historyCursor && !state.busy && !state.historyError) submitJob("history", undefined, { after: state.historyCursor, limit: 100 }); });
  $("retry-submission").addEventListener("click", retrySubmission);
  $("stop-run").addEventListener("click", stopRun);
  $("search").addEventListener("input", renderSessions);
  $("refresh").addEventListener("click", () => { if (state.session && !state.busy) { state.activities = []; submitJob("status"); } });
  $("load-history").addEventListener("click", () => { if (state.session && !state.busy) submitJob("status"); });
  $("export").addEventListener("click", exportConversation);
  const views = ["chat", "activity", "connections"];
  for (const name of views) {
    $(name + "-tab").addEventListener("click", () => setView(name));
    $(name + "-tab").addEventListener("keydown", e => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) { e.preventDefault(); const index = e.key === "Home" ? 0 : e.key === "End" ? views.length - 1 : (views.indexOf(name) + (e.key === "ArrowRight" ? 1 : views.length - 1)) % views.length; setView(views[index]); $(views[index] + "-tab").focus(); } });
  }
  $("cloud-check").addEventListener("click", checkCloudConnection);
  $("cloud-check").disabled = !token;
  setView("chat");
  $("details-toggle").addEventListener("click", () => { $("session-details").hidden = !$("session-details").hidden; $("details-toggle").setAttribute("aria-expanded", String(!$("session-details").hidden)); });
  $("menu").addEventListener("click", () => { const open = $("sidebar").classList.toggle("open"); $("menu").setAttribute("aria-expanded", String(open)); });
  document.addEventListener("click", e => { if (window.innerWidth <= 700 && !$("sidebar").contains(e.target) && !$("menu").contains(e.target)) closeSidebar(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeSidebar(); });
  $("message").value = sessionStorage.getItem("szl-studio-draft") || "";
  try {
    const saved = sessionStorage.getItem("szl-studio-submission");
    if (saved) {
      const body = JSON.parse(saved);
      if (!body || !/^[0-9a-f-]{36}$/i.test(body.client_submission_id) || !["start", "send"].includes(body.action) || typeof body.message !== "string") throw new Error("Invalid submission record");
      state.submission = body;
    }
  } catch { state.storageBlocked = true; notice("The saved submission record could not be read. New submissions are blocked; inspect the local job history before continuing."); }
  $("message").addEventListener("input", () => { sessionStorage.setItem("szl-studio-draft", $("message").value); controls(); });
  $("message").addEventListener("keydown", e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && !$("send").disabled) { e.preventDefault(); $("composer").requestSubmit(); } });
  $("composer").addEventListener("submit", e => { e.preventDefault(); const message = $("message").value.trim(); if (!message || state.busy || $("send").disabled) return; setView("chat"); submitJob(state.session ? "send" : "start", message); });
  for (const e of document.querySelectorAll("[data-prompt]")) e.addEventListener("click", () => { $("message").value = e.dataset.prompt; sessionStorage.setItem("szl-studio-draft", $("message").value); controls(); $("message").focus(); });
  icons(); controls();
  (async () => {
    if (!token) { status("Connection required", "pending"); notice("No local connection token. Reopen this workspace with Start-Agents.cmd on this computer."); return; }
    try {
      const remote = await updateState(); $("connection-label").textContent = "Connected locally / 127.0.0.1";
      if (!state.profile.credential_available) notice("The server has no usable API credential. Restart it with the configured encrypted credential store.");
      const running = (remote.jobs || []).find(j => ["running", "queued"].includes(j.status));
      if (running?.action === "reconcile") state.activeTarget = (remote.jobs || []).find(j => j.id === running.target_job_id) || null;
      if (state.submission && running?.action !== "reconcile") {
        state.session = state.submission.session_id || null; state.newDraft = !state.session;
        state.messages.push({ role: "user", text: state.submission.message }); header(); renderMessages();
        await recoverSubmission(); return;
      }
      const job = running;
      if (job) { state.busy = true; state.activeJob = job.id; state.activeAction = job.action; state.session = job.session_id || state.session; if (job.message) state.messages.push({ role: "user", text: job.message }); status("Reconnected", "running"); state.cursor = 0; state.pollGeneration++; header(); renderMessages(); renderSessions(); await poll(job.id, job.action, state.pollGeneration); }
      else {
        const cached = (remote.jobs || []).find(j => j.session_id === state.session && j.action === "status" && j.status === "completed");
        if (cached) { const saved = await api(`/api/jobs/${encodeURIComponent(cached.id)}?after=0`); if (saved.result) savedMessages(saved.result); status("Saved locally"); $("operation-label").textContent = "Cached session / refresh for current state"; }
        else { status("Ready"); renderMessages(); }
        await restoreLocalJobs(remote, cached);
        if (state.pendingStart) notice("A previous session start has an unknown outcome. New starts are blocked until its saved record is reconciled; no automatic retry was made.");
      }
    } catch (e) { status("Unavailable", "failed"); notice(e.message); }
  })();
})();
