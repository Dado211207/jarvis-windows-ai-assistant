"use strict";

const $ = id => document.getElementById(id);

const API = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  },
};

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
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
    const [health, voice, sys] = await Promise.allSettled([
      API.get("/health"),
      API.get("/voice/status"),
      API.get("/system"),
    ]);

    if (health.status === "fulfilled") {
      const h = health.value;
      setTopbarHealth(h.healthy);
      setTopbarBrain(h.brain_configured);
      setStatus("dash-health", h.healthy ? "OK" : "Degraded",
                h.healthy ? "text-ok" : "text-err");
      setStatus("dash-db", h.db_accessible ? "Connected" : "Error",
                h.db_accessible ? "text-ok" : "text-err");
      setStatus("dash-brain", h.brain_configured ? "Claude AI" : "Local fallback",
                h.brain_configured ? "text-ok" : "text-warn");
      setText("dash-version", h.version || "—");
    } else {
      ["dash-health", "dash-db", "dash-brain"].forEach(
        id => setStatus(id, "Error", "text-err"));
      setTopbarHealth(false);
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
}

// ── Chat ─────────────────────────────────────────────────────────────────────

let chatEmpty = true;

function addMessage(role, text, toolUsed) {
  const list = $("chat-messages");
  if (!list) return;

  const empty = $("chat-empty");
  if (empty && chatEmpty) { empty.style.display = "none"; chatEmpty = false; }

  const wrap = document.createElement("div");
  wrap.className = `msg msg-${role}`;

  const roleEl = document.createElement("div");
  roleEl.className = "msg-role";
  roleEl.textContent = role === "user" ? "You" : "JARVIS";

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
      addMessage("assistant", reply, data.tool_used || null);
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

function initChat() {
  const btn   = $("chat-send");
  const input = $("chat-input");
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

// ── Memory ───────────────────────────────────────────────────────────────────

async function loadMemory(query) {
  const list = $("memory-list");
  if (!list) return;

  list.textContent = "";
  const loading = document.createElement("p");
  loading.className = "empty";
  loading.textContent = "Loading…";
  list.appendChild(loading);

  try {
    const path = query ? `/memory/search?q=${encodeURIComponent(query)}` : "/memory";
    const data  = await API.get(path);
    const items = Array.isArray(data) ? data : (data.memories || data.results || []);

    list.textContent = "";
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = query ? "No memories match that query." : "No memories saved yet.";
      list.appendChild(empty);
      return;
    }

    items.forEach(item => {
      const card = document.createElement("div");
      card.className = "memory-item";

      const text = document.createElement("div");
      text.className = "memory-text";
      text.textContent = item.content || item.text || item.memory || "—";

      const meta = document.createElement("div");
      meta.className = "memory-meta";
      meta.textContent = item.timestamp || item.created_at || "";

      card.appendChild(text);
      if (meta.textContent) card.appendChild(meta);
      list.appendChild(card);
    });
  } catch (e) {
    list.textContent = "";
    const err = document.createElement("p");
    err.className = "empty";
    err.textContent = `Error loading memory: ${e.message}`;
    list.appendChild(err);
  }
}

function initMemory() {
  const btn   = $("memory-search-btn");
  const input = $("memory-search");

  const doSearch = () => {
    const q = input ? input.value.trim() : "";
    loadMemory(q || null);
  };

  if (btn)   btn.addEventListener("click", doSearch);
  if (input) input.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); doSearch(); }
  });

  loadMemory(null);
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

// ── Live event stream (WebSocket) ───────────────────────────────────────────
// Read-only: reflects runtime state and action lifecycle events in real
// time. Never sends commands — command submission stays on the existing
// REST endpoints above. Reconnects with bounded exponential backoff and
// always shows connected/reconnecting/offline rather than failing silently.

const RUNTIME_BADGE = {
  booting:            "badge-muted",
  standby:            "badge-muted",
  listening:          "badge-info",
  transcribing:       "badge-info",
  thinking:           "badge-info",
  awaiting_approval:  "badge-warn",
  executing:          "badge-warn",
  speaking:           "badge-info",
  error:              "badge-err",
  offline:            "badge-err",
};

const WS_RECONNECT_MAX_DELAY_MS = 30000;
let wsReconnectDelayMs = 1000;
let wsLastSeq = 0;

function setWsStatus(state) {
  const dot   = $("topbar-ws-dot");
  const label = $("topbar-ws-label");
  if (!dot || !label) return;
  if (state === "connected") {
    dot.className = "status-dot status-dot-ok";
    label.textContent = "live";
  } else if (state === "reconnecting") {
    dot.className = "status-dot status-dot-warn";
    label.textContent = "reconnecting";
  } else {
    dot.className = "status-dot status-dot-err";
    label.textContent = "offline";
  }
}

function setRuntimeLabel(state) {
  const el = $("topbar-runtime-label");
  if (!el || !state) return;
  el.textContent = state.replace(/_/g, " ");
  el.className = "badge " + (RUNTIME_BADGE[state] || "badge-muted");
}

function handleStreamEvent(evt) {
  if (typeof evt.seq === "number" && evt.seq > wsLastSeq) wsLastSeq = evt.seq;

  if (evt.type === "runtime_state" && evt.payload) {
    setRuntimeLabel(evt.payload.to);
  }

  // Keep the Actions page's pending list live when an action anywhere
  // (voice, another tab, chat) changes approval state or finishes running.
  if (evt.type === "action_approval_changed" || evt.type === "action_result") {
    if ($("actions-list")) loadActions();
  }
}

function scheduleReconnect() {
  setTimeout(connectEventStream, wsReconnectDelayMs);
  wsReconnectDelayMs = Math.min(wsReconnectDelayMs * 2, WS_RECONNECT_MAX_DELAY_MS);
}

function connectEventStream() {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const since = wsLastSeq ? `?since=${wsLastSeq}` : "";
  const url = `${proto}//${window.location.host}/ws/events${since}`;

  let socket;
  try {
    socket = new WebSocket(url);
  } catch (e) {
    setWsStatus("offline");
    scheduleReconnect();
    return;
  }

  socket.addEventListener("open", () => {
    wsReconnectDelayMs = 1000;
    setWsStatus("connected");
  });

  socket.addEventListener("message", (msg) => {
    try {
      handleStreamEvent(JSON.parse(msg.data));
    } catch (e) {
      console.error("event stream: could not parse message", e);
    }
  });

  socket.addEventListener("close", () => {
    setWsStatus("reconnecting");
    scheduleReconnect();
  });

  socket.addEventListener("error", () => {
    // "close" always follows "error" for a WebSocket; let close() alone
    // own reconnect scheduling so a drop is only scheduled once.
    socket.close();
  });
}

// ── Bootstrap ─────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  connectEventStream();

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
  }
});
