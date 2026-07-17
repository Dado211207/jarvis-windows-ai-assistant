"use strict";

const $ = id => document.getElementById(id);

// This page's per-launch session token (see app/core/session_token.py),
// rendered server-side into the page by app/ui/routes.py as a data
// attribute (not an inline <script>, so the page needs no script-src
// 'unsafe-inline' CSP allowance) — never fetched over the network, never
// stored in a cookie/localStorage/URL. Required on every private request;
// a page from another origin has no way to read this value.
function _jarvisToken() {
  return document.body.dataset.jarvisToken || "";
}

const API = {
  async get(path) {
    const r = await fetch(path, { headers: { "X-Jarvis-Token": _jarvisToken() } });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Jarvis-Token": _jarvisToken() },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  },
  async patch(path, body) {
    const r = await fetch(path, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-Jarvis-Token": _jarvisToken() },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  },
  async del(path) {
    const r = await fetch(path, { method: "DELETE", headers: { "X-Jarvis-Token": _jarvisToken() } });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  },
};

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function setMetric(id, text) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = "metric-card-value";
}

function setBadge(id, text, type) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = `badge badge-${type}`;
}

// ── Topbar status indicators ──────────────────────────────────────────────────

function setTopbarHealth(healthy) {
  const dot   = $("topbar-health-dot");
  const label = $("topbar-health-label");
  if (!dot || !label) return;
  if (healthy) {
    dot.className   = "status-dot status-dot-ok";
    label.textContent = "healthy";
  } else {
    dot.className   = "status-dot status-dot-err";
    label.textContent = "degraded";
  }
}

function setTopbarBrain(configured) {
  const dot   = $("topbar-brain-dot");
  const label = $("topbar-brain-label");
  if (!dot || !label) return;
  if (configured) {
    dot.className   = "status-dot status-dot-ok";
    label.textContent = "Claude AI";
  } else {
    dot.className   = "status-dot status-dot-warn";
    label.textContent = "local mode";
  }
}

// ── Dashboard ────────────────────────────────────────────────────────────────

function setStatus(id, text, cssClass) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = `text-sm ${cssClass}`;
}

function setProgressBar(barId, pct) {
  const bar = $(barId);
  if (!bar) return;
  const capped = Math.min(100, Math.max(0, pct || 0));
  bar.style.width = capped + "%";
  if (capped >= 90)      bar.className = "progress-fill progress-fill-err";
  else if (capped >= 70) bar.className = "progress-fill progress-fill-warn";
  else                   bar.className = "progress-fill";
}

async function loadDashboard() {
  const loadingIds = [
    "dash-health", "dash-db", "dash-brain", "dash-tools",
    "dash-cpu", "dash-ram", "dash-uptime", "dash-tts",
    "dash-version", "dash-phase",
  ];
  loadingIds.forEach(id => {
    const el = $(id);
    if (el) { el.textContent = "…"; el.className = "metric-card-value loading"; }
  });

  try {
    const [health, root, voice, sys] = await Promise.allSettled([
      API.get("/health"),
      API.get("/"),
      API.get("/voice/status"),
      API.get("/system"),
    ]);

    if (health.status === "fulfilled") {
      const h = health.value;
      setTopbarHealth(h.healthy);
      setStatus("dash-health", h.healthy ? "OK" : "Degraded",
                h.healthy ? "text-ok" : "text-err");
      setText("dash-version", h.version || "—");
    } else {
      setStatus("dash-health", "Error", "text-err");
      setTopbarHealth(false);
    }

    if (root.status === "fulfilled") {
      const r = root.value;
      setTopbarBrain(r.brain_configured);
      setStatus("dash-db", r.db_accessible ? "Connected" : "Error",
                r.db_accessible ? "text-ok" : "text-err");
      setStatus("dash-brain", r.brain_configured ? "Claude AI" : "Local fallback",
                r.brain_configured ? "text-ok" : "text-warn");
    } else {
      ["dash-db", "dash-brain"].forEach(id => setStatus(id, "Error", "text-err"));
      setTopbarBrain(false);
    }

    if (voice.status === "fulfilled") {
      const v = voice.value;
      setStatus("dash-tts", v.tts_enabled ? "Enabled" : "Disabled",
                v.tts_enabled ? "text-ok" : "text-muted");
      const eng = $("dash-tts-engine");
      if (eng) { eng.textContent = v.tts_engine || ""; eng.className = "metric-card-sub"; }
    } else {
      setStatus("dash-tts", "Unknown", "text-warn");
    }

    if (sys.status === "fulfilled") {
      const s = sys.value;
      const cpu = s.cpu_percent != null ? s.cpu_percent : null;
      const ram = s.ram_percent != null ? s.ram_percent : null;
      setText("dash-cpu",    cpu    != null ? `${cpu}%`    : "—");
      setText("dash-ram",    ram    != null ? `${ram}%`    : "—");
      setText("dash-uptime", s.uptime         || "—");
      setText("dash-tools",  s.tools_registered != null ? `${s.tools_registered}` : "—");
      setText("dash-phase",  s.phase          || "—");
      setProgressBar("dash-cpu-bar", cpu);
      setProgressBar("dash-ram-bar", ram);
    } else {
      ["dash-cpu", "dash-ram", "dash-uptime", "dash-tools", "dash-phase"].forEach(
        id => setText(id, "—"));
    }
  } catch (e) {
    console.error("dashboard load error", e);
  }

  loadPersonality();
}

async function loadPersonality() {
  const [settings, prefs] = await Promise.allSettled([
    API.get("/settings"),
    API.get("/preferences"),
  ]);

  if (settings.status === "fulfilled") {
    const s = settings.value;
    setMetric("dash-assistant-name", s.assistant_name || "JARVIS");
    setMetric("dash-user-name", s.user_display_name ? s.user_display_name : "not set");
    const pinned = (s.pinned_commands || "").split(",").map(x => x.trim()).filter(Boolean);
    setMetric("dash-pinned", String(pinned.length));
  } else {
    ["dash-assistant-name", "dash-user-name", "dash-pinned"].forEach(id => setMetric(id, "—"));
  }

  if (prefs.status === "fulfilled" && Array.isArray(prefs.value)) {
    setMetric("dash-mem-count", String(prefs.value.length));
  } else {
    setMetric("dash-mem-count", "—");
  }
}

// ── Chat ─────────────────────────────────────────────────────────────────────

let chatEmpty = true;
let assistantName = "JARVIS";

async function loadAssistantName() {
  try {
    const s = await API.get("/settings");
    if (s && s.assistant_name) assistantName = s.assistant_name;
  } catch (e) {
    // keep default name on failure
  }
}

function addMessage(role, text, toolUsed, meta) {
  const list = $("chat-messages");
  if (!list) return;

  const empty = $("chat-empty");
  if (empty && chatEmpty) { empty.style.display = "none"; chatEmpty = false; }

  const wrap = document.createElement("div");
  wrap.className = `msg msg-${role}`;

  const roleEl = document.createElement("div");
  roleEl.className = "msg-role";
  roleEl.textContent = role === "user" ? "You" : assistantName;

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.textContent = text;

  wrap.appendChild(roleEl);
  wrap.appendChild(bubble);

  if (toolUsed) {
    const tool = document.createElement("div");
    tool.className = "msg-tool";
    tool.textContent = `tool: ${toolUsed}`;
    wrap.appendChild(tool);
  }

  // This reply came from Brain's local fallback, not a real AI answer —
  // say so plainly instead of letting it look like a genuine response.
  if (toolUsed === "brain" && meta && meta.used_api === false) {
    const notice = document.createElement("div");
    notice.className = "msg-tool text-warn";
    notice.textContent = "AI chat isn't configured yet — add an API key in Settings for a real answer.";
    wrap.appendChild(notice);
  }

  list.appendChild(wrap);
  list.scrollTop = list.scrollHeight;
}

async function sendChat() {
  const input = $("chat-input");
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;

  input.value = "";
  addMessage("user", text, null);

  const sendBtn = $("chat-send");
  if (sendBtn) sendBtn.disabled = true;

  try {
    const data = await API.post("/command", { command: text });
    if (data.requires_approval && data.pending_action_id) {
      addApprovalCard(data.pending_action_id, data);
    } else {
      const reply = typeof data.message === "string" ? data.message : JSON.stringify(data.message);
      addMessage("assistant", reply, data.tool_used || null, data.data || null);
    }
  } catch (e) {
    addMessage("assistant", "Error: " + e.message, null);
  } finally {
    if (sendBtn) sendBtn.disabled = false;
    if (input)   input.focus();
  }
}

function addApprovalCard(actionId, data) {
  const list = $("chat-messages");
  if (!list) return;

  const empty = $("chat-empty");
  if (empty && chatEmpty) { empty.style.display = "none"; chatEmpty = false; }

  const preview = (data && data.data) ? data.data : {};
  const riskLevel = preview.risk_level || "medium";

  const card = document.createElement("div");
  card.className = "msg-approval";

  const hdr = document.createElement("div");
  hdr.className = "msg-approval-header";
  hdr.textContent = "⚠ Approval Required";

  const desc = document.createElement("div");
  desc.className = "msg-approval-desc";
  desc.textContent = preview.description || data.message || "";

  const meta = document.createElement("div");
  meta.className = "msg-approval-meta";
  meta.textContent = "Tool: " + (preview.tool_name || "—") + "  •  Risk: " + riskLevel;

  const footer = document.createElement("div");
  footer.className = "msg-approval-footer";

  const confirmBtn = document.createElement("button");
  confirmBtn.className = "btn btn-primary btn-sm";
  confirmBtn.textContent = "Confirm";

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "btn btn-danger btn-sm";
  cancelBtn.textContent = "Cancel";

  const statusEl = document.createElement("span");
  statusEl.className = "msg-approval-status";

  confirmBtn.addEventListener("click", async () => {
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    try {
      const r = await API.post("/actions/" + actionId + "/confirm", {});
      statusEl.textContent = r.message;
      statusEl.className = "msg-approval-status " + (r.success ? "text-ok" : "text-err");
    } catch (e) {
      statusEl.textContent = "Error: " + e.message;
      statusEl.className = "msg-approval-status text-err";
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
    }
  });

  cancelBtn.addEventListener("click", async () => {
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    try {
      await API.post("/actions/" + actionId + "/cancel", {});
      statusEl.textContent = "Cancelled.";
      statusEl.className = "msg-approval-status text-muted";
    } catch (e) {
      statusEl.textContent = "Error: " + e.message;
      statusEl.className = "msg-approval-status text-err";
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
    }
  });

  footer.appendChild(confirmBtn);
  footer.appendChild(cancelBtn);
  footer.appendChild(statusEl);

  card.appendChild(hdr);
  card.appendChild(desc);
  card.appendChild(meta);
  card.appendChild(footer);

  list.appendChild(card);
  list.scrollTop = list.scrollHeight;
}

async function loadAiNotice() {
  const notice = $("chat-ai-notice");
  if (!notice) return;
  try {
    const r = await API.get("/");
    notice.style.display = r.brain_configured ? "none" : "";
  } catch (e) {
    // leave hidden — a health-check failure is surfaced elsewhere (topbar)
  }
}

function initChat() {
  const btn   = $("chat-send");
  const input = $("chat-input");
  loadAssistantName();
  loadAiNotice();
  if (btn)   btn.addEventListener("click", sendChat);
  if (input) input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });

  // Suggestion chip clicks
  const suggestions = document.querySelectorAll(".chat-suggestion");
  suggestions.forEach(chip => {
    chip.addEventListener("click", () => {
      if (input) {
        input.value = chip.textContent;
        input.focus();
      }
    });
  });
}

// ── Logs ─────────────────────────────────────────────────────────────────────

const LOG_STATUS_BADGE = {
  success: "badge-ok",
  ok:      "badge-ok",
  error:   "badge-err",
  failure: "badge-err",
  blocked: "badge-err",
  pending: "badge-warn",
  warning: "badge-warn",
};

async function loadLogs() {
  const tbody = $("logs-tbody");
  if (!tbody) return;

  tbody.textContent = "";
  const loading = document.createElement("tr");
  const lc = document.createElement("td");
  lc.colSpan = 4;
  lc.className = "empty";
  lc.textContent = "Loading…";
  loading.appendChild(lc);
  tbody.appendChild(loading);

  try {
    const logs = await API.get("/logs");
    tbody.textContent = "";

    const entries = Array.isArray(logs) ? logs : (logs.logs || []);
    if (!entries.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 4;
      cell.className = "empty";
      cell.textContent = "No log entries yet.";
      row.appendChild(cell);
      tbody.appendChild(row);
      return;
    }

    entries.forEach(entry => {
      const row = document.createElement("tr");

      const tdTime = document.createElement("td");
      tdTime.style.whiteSpace = "nowrap";
      tdTime.textContent = entry.created_at || "—";

      const tdCmd = document.createElement("td");
      tdCmd.textContent = entry.command || "—";

      const tdTool = document.createElement("td");
      tdTool.className = "font-mono text-xs";
      tdTool.textContent = entry.tool_name || "—";

      const tdStatus = document.createElement("td");
      const rawStatus = (entry.status || "").toLowerCase();
      if (rawStatus) {
        const badge = document.createElement("span");
        badge.className = "badge " + (LOG_STATUS_BADGE[rawStatus] || "badge-muted");
        badge.textContent = entry.status;
        tdStatus.appendChild(badge);
      } else {
        tdStatus.textContent = "—";
      }

      row.appendChild(tdTime);
      row.appendChild(tdCmd);
      row.appendChild(tdTool);
      row.appendChild(tdStatus);
      tbody.appendChild(row);
    });
  } catch (e) {
    tbody.textContent = "";
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.className = "empty";
    cell.textContent = `Error loading logs: ${e.message}`;
    row.appendChild(cell);
    tbody.appendChild(row);
  }
}

function initLogs() {
  const btn = $("logs-refresh");
  if (btn) btn.addEventListener("click", loadLogs);
  loadLogs();
}

// ── Memory (personality preferences) ──────────────────────────────────────────

let memoryCategory = "";

function renderMemoryEmpty(list, isSearch) {
  const empty = document.createElement("p");
  empty.className = "empty";
  empty.textContent = isSearch
    ? "No preferences match that search."
    : "No preferences saved yet. Try: remember that I prefer short answers";
  list.appendChild(empty);
}

function buildMemoryCard(item) {
  const card = document.createElement("div");
  card.className = "memory-item";

  const head = document.createElement("div");
  head.className = "memory-head";

  const cat = document.createElement("span");
  cat.className = "badge badge-info";
  cat.textContent = item.category || "general_preference";

  // Forget requires a visible two-step confirmation — clicking "Forget" never
  // deletes by itself. It only deletes on the second click of "Confirm delete".
  const actions = document.createElement("div");
  actions.className = "memory-actions";

  const forget = document.createElement("button");
  forget.className = "btn btn-ghost btn-sm";
  forget.textContent = "Forget";

  const confirmBtn = document.createElement("button");
  confirmBtn.className = "btn btn-danger btn-sm";
  confirmBtn.textContent = "Confirm delete";
  confirmBtn.style.display = "none";

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "btn btn-ghost btn-sm";
  cancelBtn.textContent = "Cancel";
  cancelBtn.style.display = "none";

  actions.appendChild(forget);
  actions.appendChild(confirmBtn);
  actions.appendChild(cancelBtn);

  head.appendChild(cat);
  head.appendChild(actions);

  const text = document.createElement("div");
  text.className = "memory-text";
  text.textContent = item.value || item.title || "—";

  const meta = document.createElement("div");
  meta.className = "memory-meta";
  meta.textContent = item.created_at || "";

  card.appendChild(head);
  card.appendChild(text);
  if (meta.textContent) card.appendChild(meta);

  function resetForgetState() {
    forget.style.display = "";
    confirmBtn.style.display = "none";
    cancelBtn.style.display = "none";
  }

  forget.addEventListener("click", () => {
    forget.style.display = "none";
    confirmBtn.style.display = "";
    cancelBtn.style.display = "";
  });

  cancelBtn.addEventListener("click", resetForgetState);

  confirmBtn.addEventListener("click", async () => {
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    try {
      await API.del("/preferences/" + item.id);
      if (card.parentNode) card.parentNode.removeChild(card);
      const list = $("memory-list");
      if (list && !list.querySelector(".memory-item")) renderMemoryEmpty(list, false);
    } catch (e) {
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
      confirmBtn.textContent = "Error — retry?";
    }
  });

  return card;
}

async function loadMemory() {
  const list = $("memory-list");
  if (!list) return;

  _clearEl(list);
  const loading = document.createElement("p");
  loading.className = "empty";
  loading.textContent = "Loading…";
  list.appendChild(loading);

  const input = $("memory-search");
  const q = input ? input.value.trim() : "";

  try {
    let path;
    if (q) {
      path = "/preferences/search?q=" + encodeURIComponent(q);
    } else if (memoryCategory) {
      path = "/preferences?category=" + encodeURIComponent(memoryCategory);
    } else {
      path = "/preferences";
    }
    const items = await API.get(path);

    _clearEl(list);
    if (!Array.isArray(items) || !items.length) {
      renderMemoryEmpty(list, !!q);
      return;
    }
    items.forEach(item => list.appendChild(buildMemoryCard(item)));
  } catch (e) {
    _clearEl(list);
    const err = document.createElement("p");
    err.className = "empty";
    err.textContent = `Error loading memory: ${e.message}`;
    list.appendChild(err);
  }
}

function initMemory() {
  const btn     = $("memory-search-btn");
  const refresh = $("memory-refresh-btn");
  const input   = $("memory-search");

  if (btn) btn.addEventListener("click", loadMemory);
  if (refresh) refresh.addEventListener("click", () => {
    if (input) input.value = "";
    loadMemory();
  });
  if (input) input.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); loadMemory(); }
  });

  const cats = document.querySelectorAll(".memory-cat");
  cats.forEach(chip => {
    chip.addEventListener("click", () => {
      memoryCategory = chip.getAttribute("data-category") || "";
      cats.forEach(c => c.classList.remove("active"));
      chip.classList.add("active");
      if (input) input.value = "";
      loadMemory();
    });
  });

  loadMemory();
}

// ── Voice ─────────────────────────────────────────────────────────────────────

async function refreshVoiceStatus() {
  const fields = ["tts-avail", "tts-enabled-val", "tts-engine-val"];
  fields.forEach(id => {
    const el = $(id);
    if (el) { el.textContent = "…"; el.className = "status-row-value"; }
  });

  try {
    const v = await API.get("/voice/status");

    const avail   = $("tts-avail");
    const enabled = $("tts-enabled-val");
    const engine  = $("tts-engine-val");

    if (avail) {
      const isAvail = v.tts_available || v.available || false;
      avail.textContent = isAvail ? "Yes" : "No";
      avail.className   = `status-row-value ${isAvail ? "text-ok" : "text-err"}`;
    }
    if (enabled) {
      const isEnabled = v.tts_enabled || v.enabled || false;
      enabled.textContent = isEnabled ? "Enabled" : "Disabled";
      enabled.className   = `status-row-value ${isEnabled ? "text-ok" : "text-muted"}`;
    }
    if (engine) {
      engine.textContent = v.tts_engine || v.engine || "—";
      engine.className   = "status-row-value font-mono";
    }
  } catch (e) {
    fields.forEach(id => {
      const el = $(id);
      if (el) { el.textContent = "Error"; el.className = "status-row-value text-err"; }
    });
  }
}

async function voiceCommand(cmd) {
  try {
    await API.post("/command", { command: cmd });
    await refreshVoiceStatus();
  } catch (e) {
    console.error("voice command error", e);
  }
}

async function voiceStop() {
  try {
    await API.post("/voice/stop", {});
  } catch (e) {
    console.error("voice stop error", e);
  }
}

async function loadVoiceSettings() {
  const rate = $("tts-rate-val");
  const vol  = $("tts-volume-val");
  if (!rate && !vol) return;
  try {
    const s = await API.get("/settings");
    if (rate) { rate.textContent = s.tts_rate   || "—"; rate.className = "status-row-value font-mono"; }
    if (vol)  { vol.textContent  = s.tts_volume || "—"; vol.className  = "status-row-value font-mono"; }
  } catch (e) {
    if (rate) { rate.textContent = "—"; rate.className = "status-row-value font-mono"; }
    if (vol)  { vol.textContent  = "—"; vol.className  = "status-row-value font-mono"; }
  }
}

function initVoice() {
  const on   = $("btn-speak-on");
  const off  = $("btn-speak-off");
  const test = $("btn-speak-test");
  const stop = $("btn-speak-stop");

  if (on)   on.addEventListener(  "click", () => voiceCommand("speak on"));
  if (off)  off.addEventListener( "click", () => voiceCommand("speak off"));
  if (test) test.addEventListener("click", () => voiceCommand("speak test"));
  if (stop) stop.addEventListener("click", voiceStop);

  refreshVoiceStatus();
  loadVoiceSettings();
}

// ── Actions ───────────────────────────────────────────────────────────────────

function _clearEl(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

function buildActionCard(action) {
  const riskLevel = action.risk_level || "medium";
  const card = document.createElement("div");
  card.className = "action-card risk-" + riskLevel;
  card.id = "action-" + action.id;

  const header = document.createElement("div");
  header.className = "action-card-header";

  const title = document.createElement("div");
  title.className = "action-card-title";
  title.textContent = action.action_name || action.tool_name;

  const riskMap = { high: "badge-err", medium: "badge-warn", low: "badge-info" };
  const riskBadge = document.createElement("span");
  riskBadge.className = "badge " + (riskMap[riskLevel] || "badge-info");
  riskBadge.textContent = riskLevel;

  header.appendChild(title);
  header.appendChild(riskBadge);

  const desc = document.createElement("div");
  desc.className = "action-card-desc";
  desc.textContent = action.description || "";

  const meta = document.createElement("div");
  meta.className = "action-card-meta";
  meta.textContent =
    "Command: " + (action.command || "—") +
    "  •  Tool: " + (action.tool_name || "—") +
    "  •  Created: " + (action.created_at || "—");

  const statusEl = document.createElement("div");
  statusEl.className = "action-card-status mt-2";

  const footer = document.createElement("div");
  footer.className = "action-card-footer";

  const confirmBtn = document.createElement("button");
  confirmBtn.className = "btn btn-primary btn-sm";
  confirmBtn.textContent = "Confirm";

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "btn btn-ghost btn-sm";
  cancelBtn.textContent = "Cancel";

  confirmBtn.addEventListener("click", async () => {
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    try {
      const r = await API.post("/actions/" + action.id + "/confirm", {});
      statusEl.textContent = r.message;
      statusEl.className = "action-card-status mt-2 " + (r.success ? "text-ok" : "text-err");
      card.className = "action-card risk-" + riskLevel + " resolved";
    } catch (e) {
      statusEl.textContent = "Error: " + e.message;
      statusEl.className = "action-card-status mt-2 text-err";
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
    }
  });

  cancelBtn.addEventListener("click", async () => {
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    try {
      await API.post("/actions/" + action.id + "/cancel", {});
      statusEl.textContent = "Action cancelled.";
      statusEl.className = "action-card-status mt-2 text-muted";
      card.className = "action-card risk-" + riskLevel + " resolved";
    } catch (e) {
      statusEl.textContent = "Error: " + e.message;
      statusEl.className = "action-card-status mt-2 text-err";
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
    }
  });

  footer.appendChild(confirmBtn);
  footer.appendChild(cancelBtn);
  footer.appendChild(statusEl);

  card.appendChild(header);
  card.appendChild(desc);
  card.appendChild(meta);
  card.appendChild(footer);

  return card;
}

async function loadActions() {
  const list = $("actions-list");
  if (!list) return;

  _clearEl(list);
  const loading = document.createElement("p");
  loading.className = "empty";
  loading.textContent = "Loading…";
  list.appendChild(loading);

  try {
    const actions = await API.get("/actions/pending");
    _clearEl(list);

    if (!actions.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No pending actions.";
      list.appendChild(empty);
      return;
    }

    actions.forEach(a => list.appendChild(buildActionCard(a)));
  } catch (e) {
    _clearEl(list);
    const err = document.createElement("p");
    err.className = "empty text-err";
    err.textContent = "Error loading actions: " + e.message;
    list.appendChild(err);
  }
}

function initActions() {
  const btn = $("actions-refresh");
  if (btn) btn.addEventListener("click", loadActions);
  loadActions();
}

// ── Settings ──────────────────────────────────────────────────────────────────

const SETTINGS_FIELDS = [
  "user_display_name", "assistant_name", "preferred_language",
  "preferred_response_style", "preferred_tone", "theme_mode", "compact_mode",
  "dashboard_default_page", "tts_enabled", "tts_rate", "tts_volume", "tts_voice",
  "pinned_commands", "start_with_windows",
];

function setSettingsStatus(text, ok) {
  const el = $("settings-status");
  if (!el) return;
  el.textContent = text;
  el.className = "settings-status " + (ok ? "text-ok" : "text-err");
}

function applySettingsToForm(s) {
  SETTINGS_FIELDS.forEach(key => {
    const el = $("set-" + key);
    if (el && s[key] != null) el.value = s[key];
  });
  const safety = $("settings-safety-mode");
  if (safety) safety.textContent = s.safety_mode || "on";
}

async function loadSettings() {
  try {
    const s = await API.get("/settings");
    applySettingsToForm(s);
    setSettingsStatus("", true);
  } catch (e) {
    setSettingsStatus("Error loading settings: " + e.message, false);
  }
}

async function saveSettings(ev) {
  if (ev) ev.preventDefault();
  const values = {};
  SETTINGS_FIELDS.forEach(key => {
    const el = $("set-" + key);
    if (el) values[key] = el.value;
  });

  const btn = $("settings-save");
  if (btn) btn.disabled = true;
  try {
    const r = await API.patch("/settings", { values });
    if (r.settings) applySettingsToForm(r.settings);
    if (r.success) {
      setSettingsStatus("Saved.", true);
    } else {
      const keys = Object.keys(r.errors || {});
      setSettingsStatus(keys.length ? r.errors[keys[0]] : "Some values were rejected.", false);
    }
  } catch (e) {
    setSettingsStatus("Error saving: " + e.message, false);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function initSettings() {
  const form   = $("settings-form");
  const reload = $("settings-reload");
  if (form)   form.addEventListener("submit", saveSettings);
  if (reload) reload.addEventListener("click", loadSettings);
  loadSettings();

  const updateBtn = $("update-check-btn");
  if (updateBtn) updateBtn.addEventListener("click", checkForUpdates);
  checkForUpdates();

  initProviderSection();
}

// ── AI Provider (Settings page) ─────────────────────────────────────────────
// Reuses the same backend as onboarding (/onboarding/api-key etc.) so a user
// who postponed setup during onboarding has a direct way to finish it later,
// without re-running the whole wizard.

function setProviderKeyStatus(text, kind) {
  const el = $("provider-key-status");
  if (!el) return;
  el.textContent = text;
  el.className = "onboarding-key-status" + (kind ? ` status-${kind}` : "");
}

async function loadProviderStatus() {
  try {
    const state = await API.get("/onboarding/state");
    const configured = state.api_key_status === "validated";
    setBadge("provider-status", configured ? "configured" : "not configured", configured ? "ok" : "warn");
    const maskedRow = $("provider-masked-row");
    const removeBtn = $("provider-key-remove");
    if (configured && state.api_key_masked) {
      setText("provider-masked-key", state.api_key_masked);
      if (maskedRow) maskedRow.hidden = false;
      if (removeBtn) removeBtn.hidden = false;
    } else {
      if (maskedRow) maskedRow.hidden = true;
      if (removeBtn) removeBtn.hidden = true;
    }
  } catch (e) {
    setProviderKeyStatus("Could not load provider status: " + e.message, "err");
  }
}

function initProviderSection() {
  const keyInput = $("provider-api-key");
  const keyToggle = $("provider-key-toggle");
  const saveBtn = $("provider-key-save");
  const removeBtn = $("provider-key-remove");

  if (keyToggle) {
    keyToggle.addEventListener("click", () => {
      const hidden = keyInput.type === "password";
      keyInput.type = hidden ? "text" : "password";
      keyToggle.textContent = hidden ? "Hide" : "Show";
    });
  }

  if (saveBtn) {
    saveBtn.addEventListener("click", async () => {
      const value = keyInput.value.trim();
      if (!value) {
        setProviderKeyStatus("Enter a key to save.", "err");
        return;
      }
      saveBtn.disabled = true;
      setProviderKeyStatus("Validating with Anthropic…", "busy");
      try {
        const result = await API.post("/onboarding/api-key", { api_key: value });
        if (result.success) {
          setProviderKeyStatus("Key validated and saved securely.", "ok");
          keyInput.value = "";
          loadProviderStatus();
        } else {
          setProviderKeyStatus(result.error || "Could not validate that key.", "err");
        }
      } catch (e) {
        setProviderKeyStatus("Could not reach JARVIS to validate the key.", "err");
      } finally {
        saveBtn.disabled = false;
      }
    });
  }

  if (removeBtn) {
    removeBtn.addEventListener("click", async () => {
      removeBtn.disabled = true;
      try {
        await API.del("/onboarding/api-key");
        setProviderKeyStatus("Key removed.", "ok");
        loadProviderStatus();
      } catch (e) {
        setProviderKeyStatus("Could not remove the key: " + e.message, "err");
      } finally {
        removeBtn.disabled = false;
      }
    });
  }

  loadProviderStatus();
}

function setUpdateStatus(text, ok) {
  const el = $("update-status");
  if (!el) return;
  el.textContent = text;
  el.className = "settings-status " + (ok === true ? "text-ok" : ok === false ? "text-err" : "");
}

async function checkForUpdates() {
  const btn = $("update-check-btn");
  const note = $("update-unsupported-note");
  const box = $("update-available-box");
  const link = $("update-download-link");
  if (btn) btn.disabled = true;
  setUpdateStatus("Checking…", null);
  try {
    const r = await API.get("/update/check");
    setText("update-current-version", r.current_version || "—");
    if (box) box.hidden = true;
    if (note) note.hidden = true;

    if (!r.checked) {
      setUpdateStatus("", null);
      if (note) {
        note.textContent = r.reason || "Update checking is unavailable.";
        note.hidden = false;
      }
      if (btn) btn.disabled = !r.reason || !r.reason.startsWith("Could not reach");
      return;
    }

    if (r.update_available) {
      setUpdateStatus(`A new version is available: ${r.latest_version}`, true);
      if (link) link.href = r.download_url;
      if (box) box.hidden = false;
    } else {
      setUpdateStatus("You're on the latest version.", true);
    }
  } catch (e) {
    setUpdateStatus("Could not check for updates: " + e.message, false);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  const path = window.location.pathname.replace(/\/+$/, "");

  if (path === "/ui" || path === "/ui/dashboard" || path === "") {
    loadDashboard();
  } else if (path === "/ui/chat") {
    initChat();
  } else if (path === "/ui/actions") {
    initActions();
  } else if (path === "/ui/logs") {
    initLogs();
  } else if (path === "/ui/memory") {
    initMemory();
  } else if (path === "/ui/voice") {
    initVoice();
  } else if (path === "/ui/settings") {
    initSettings();
  }
});
