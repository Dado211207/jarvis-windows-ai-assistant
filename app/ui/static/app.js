"use strict";

const $ = id => document.getElementById(id);

// v0.2 CSRF/mutation session token (see app/api/session.py). The server
// sets a non-HttpOnly "jarvis_session" cookie specifically so this page's
// own JS can read it and echo it back as a header — the classic
// double-submit-cookie pattern. A foreign page cannot read our cookie to
// forge a matching header, even if it could otherwise get a request to
// the API to fire at all.
function getSessionCookie() {
  const match = document.cookie.match(/(?:^|;\s*)jarvis_session=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

const API = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  },
  async post(path, body) {
    const token = getSessionCookie();
    const r = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { "X-JARVIS-Session-Token": token } : {}),
      },
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

  initPushToTalk();
}

// ── Push-to-talk (v0.2) ──────────────────────────────────────────────────────
// Optional. Text input always works whether or not this is available —
// see app/voice/stt.py. One explicit recording per press; nothing is
// captured continuously or in the background.

const PTT_STATE = { IDLE: "idle", REQUESTING: "requesting", LISTENING: "listening", TRANSCRIBING: "transcribing", ERROR: "error" };
let pttState = PTT_STATE.IDLE;
let pttRecorder = null;
let pttChunks = [];
let pttStream = null;
let pttUploadController = null;
// Sticky, independent of pttState: true once /voice/stt-status reports
// unavailable. setPttState() must keep respecting this on every call —
// it previously recomputed `disabled` from pttState alone and silently
// re-enabled the button on the very next state change (caught via real
// browser testing, not a unit test: see tests/test_playwright_e2e.py).
let pttUnavailable = false;

function setPttState(state, message) {
  pttState = state;
  const btn = $("ptt-button");
  const cancelBtn = $("ptt-cancel");
  const status = $("ptt-status");

  if (btn) {
    btn.setAttribute("aria-label",
      state === PTT_STATE.LISTENING ? "Push to talk: stop recording" : "Push to talk: start recording");
    btn.disabled = pttUnavailable || state === PTT_STATE.TRANSCRIBING || state === PTT_STATE.REQUESTING;
    btn.className = "btn " + (state === PTT_STATE.LISTENING ? "btn-danger" : "btn-ghost");
  }
  if (cancelBtn) {
    cancelBtn.hidden = !(state === PTT_STATE.LISTENING || state === PTT_STATE.TRANSCRIBING);
  }
  if (status) {
    status.textContent = message || "";
  }
}

function pttReleaseMicrophone() {
  if (pttStream) {
    pttStream.getTracks().forEach(track => track.stop());
    pttStream = null;
  }
}

async function pttStart() {
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    setPttState(PTT_STATE.ERROR, "Voice input isn't supported in this browser. Text input still works.");
    return;
  }

  setPttState(PTT_STATE.REQUESTING, "Requesting microphone permission…");
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    setPttState(PTT_STATE.ERROR, "Microphone unavailable or permission denied. Text input still works.");
    return;
  }

  pttStream = stream;
  pttChunks = [];
  try {
    pttRecorder = new MediaRecorder(stream);
  } catch (e) {
    pttReleaseMicrophone();
    setPttState(PTT_STATE.ERROR, "Could not start recording. Text input still works.");
    return;
  }

  pttRecorder.addEventListener("dataavailable", (e) => {
    if (e.data && e.data.size > 0) pttChunks.push(e.data);
  });
  pttRecorder.addEventListener("stop", pttOnRecordingStopped);

  pttRecorder.start();
  setPttState(PTT_STATE.LISTENING, "Listening… click again or press Alt+M to stop.");
}

function pttStop() {
  if (pttRecorder && pttRecorder.state !== "inactive") {
    pttRecorder.stop(); // triggers pttOnRecordingStopped via the "stop" event
  }
}

function pttCancel() {
  if (pttUploadController) {
    pttUploadController.abort();
    pttUploadController = null;
  }
  if (pttRecorder && pttRecorder.state !== "inactive") {
    pttRecorder.removeEventListener("stop", pttOnRecordingStopped);
    pttRecorder.stop();
  }
  pttReleaseMicrophone();
  pttChunks = [];
  setPttState(PTT_STATE.IDLE, "Cancelled.");
}

async function pttOnRecordingStopped() {
  pttReleaseMicrophone();
  const blob = new Blob(pttChunks, { type: "audio/webm" });
  pttChunks = [];

  if (blob.size === 0) {
    setPttState(PTT_STATE.IDLE, "No audio captured.");
    return;
  }

  setPttState(PTT_STATE.TRANSCRIBING, "Transcribing…");

  const token = getSessionCookie();
  const formData = new FormData();
  formData.append("audio", blob, "recording.webm");

  pttUploadController = new AbortController();
  try {
    const r = await fetch("/voice/transcribe", {
      method: "POST",
      headers: token ? { "X-JARVIS-Session-Token": token } : {},
      body: formData,
      signal: pttUploadController.signal,
    });
    const data = await r.json();
    pttUploadController = null;

    if (!r.ok || !data.success) {
      setPttState(PTT_STATE.ERROR, data.message || "Transcription failed. Text input still works.");
      return;
    }

    const input = $("chat-input");
    if (input) {
      input.value = data.text;
      input.focus();
    }
    setPttState(PTT_STATE.IDLE, "Heard: “" + data.text + "”");
  } catch (e) {
    pttUploadController = null;
    if (e.name !== "AbortError") {
      setPttState(PTT_STATE.ERROR, "Could not reach the server to transcribe. Text input still works.");
    }
  }
}

async function initPushToTalk() {
  const btn = $("ptt-button");
  const cancelBtn = $("ptt-cancel");
  if (!btn) return;

  btn.addEventListener("click", () => {
    if (pttState === PTT_STATE.LISTENING) {
      pttStop();
    } else if (pttState === PTT_STATE.IDLE || pttState === PTT_STATE.ERROR) {
      pttStart();
    }
  });

  if (cancelBtn) cancelBtn.addEventListener("click", pttCancel);

  document.addEventListener("keydown", (e) => {
    if (e.altKey && (e.key === "m" || e.key === "M")) {
      e.preventDefault();
      if (pttState === PTT_STATE.LISTENING) pttStop();
      else if (pttState === PTT_STATE.IDLE || pttState === PTT_STATE.ERROR) pttStart();
    }
  });

  try {
    const status = await API.get("/voice/stt-status");
    if (!status.available) {
      pttUnavailable = true;
      btn.title = "Voice input not configured: " + status.reason;
      setPttState(PTT_STATE.IDLE, "");
    }
  } catch (e) {
    // Leave the button enabled; a real attempt will surface the honest error.
  }
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

// ── Setup / first-run onboarding ─────────────────────────────────────────────

const SETUP_READINESS_FIELDS = [
  "core", "text_chat", "ai_provider", "mode",
  "stt_runtime", "speech_model", "tts", "database", "windows_automation",
];

async function checkMicrophonePresence() {
  const el = $("ready-microphone");
  if (!el) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
    el.textContent = "Not supported by this browser";
    el.className = "status-row-value text-err";
    return;
  }
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const hasMic = devices.some(d => d.kind === "audioinput");
    el.textContent = hasMic ? "Device detected" : "No microphone detected";
    el.className = `status-row-value ${hasMic ? "text-ok" : "text-err"}`;
  } catch (e) {
    el.textContent = "Could not check (browser permissions)";
    el.className = "status-row-value text-err";
  }
}

async function refreshSetupReadiness() {
  SETUP_READINESS_FIELDS.forEach(key => {
    const el = $("ready-" + key);
    if (el) { el.textContent = "…"; el.className = "status-row-value loading"; }
  });

  try {
    const r = await API.get("/onboarding/readiness");
    SETUP_READINESS_FIELDS.forEach(key => {
      const el = $("ready-" + key);
      const item = r[key];
      if (!el || !item) return;
      el.textContent = item.ready ? "Ready" : "Not ready";
      el.className = `status-row-value ${item.ready ? "text-ok" : "text-err"}`;
      el.title = item.detail;
    });

    const speechModelEl = $("setup-speech-model-status");
    if (speechModelEl && r.speech_model) {
      speechModelEl.textContent = r.speech_model.detail;
      speechModelEl.className = `status-row-value ${r.speech_model.ready ? "text-ok" : "text-err"}`;
    }
  } catch (e) {
    console.error("readiness check error", e);
  }

  checkMicrophonePresence();
}

async function refreshSetupKeyStatus() {
  const el = $("setup-key-status");
  if (!el) return;
  try {
    const r = await API.get("/settings/api-key-status");
    el.textContent = r.configured ? "Configured" : "Not configured";
    el.className = `status-row-value ${r.configured ? "text-ok" : "text-err"}`;
  } catch (e) {
    el.textContent = "Unknown";
    el.className = "status-row-value";
  }
}

function _setSetupKeyMessage(text, ok) {
  const el = $("setup-key-message");
  if (!el) return;
  el.textContent = text;
  el.className = `text-xs mt-2 ${ok ? "text-ok" : "text-err"}`;
}

// ── Guided speech-model download ────────────────────────────────────────────

const MODEL_ACTIVE_STATES = ["checking", "downloading", "verifying", "installing"];

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return "0 MB";
  const mb = bytes / (1024 * 1024);
  return mb >= 1000 ? (mb / 1024).toFixed(2) + " GB" : mb.toFixed(1) + " MB";
}

async function refreshModelPreview() {
  const errorEl = $("model-info-error");
  const startBtn = $("model-install-start");
  try {
    const info = await API.get("/onboarding/speech-model/info");
    if (!info.available) {
      if (errorEl) errorEl.textContent = info.error || "Could not check the model right now.";
      if (startBtn) startBtn.disabled = true;
      return;
    }
    if (errorEl) errorEl.textContent = "";
    if (startBtn) startBtn.disabled = false;

    setText("model-info-name", info.display_name);
    setText("model-info-source", info.source_url);
    setText("model-info-license", info.license);
    setText("model-info-size", formatBytes(info.total_size));
    setText("model-info-destination", info.destination);
    const hashedCount = info.files.filter(f => f.sha256_verified).length;
    setText(
      "model-info-checksum",
      `SHA-256 verified for ${hashedCount} of ${info.files.length} files (the model itself); ` +
      "remaining small files verified by exact download size"
    );
    setText("model-info-language-note", info.language_note);
  } catch (e) {
    console.error("model info error", e);
    if (errorEl) errorEl.textContent = "Could not check the model right now.";
  }
}

function _renderModelInstallState(state) {
  const startBtn = $("model-install-start");
  const cancelBtn = $("model-install-cancel");
  const retryBtn = $("model-install-retry");
  const progressWrap = $("model-install-progress-wrap");
  const progressBar = $("model-install-progress-bar");
  const progressText = $("model-install-progress-text");

  const active = MODEL_ACTIVE_STATES.includes(state.status);

  if (startBtn) startBtn.hidden = active || state.status === "error";
  if (cancelBtn) cancelBtn.hidden = !active;
  if (retryBtn) retryBtn.hidden = state.status !== "error";
  if (progressWrap) progressWrap.hidden = !active && state.status !== "complete";

  if (progressBar) {
    const pct = state.bytes_total > 0
      ? Math.min(100, Math.round((state.bytes_downloaded / state.bytes_total) * 100))
      : 0;
    progressBar.style.width = pct + "%";
  }
  if (progressText) {
    progressText.textContent = state.status === "downloading"
      ? `Downloading ${state.current_file}… ${formatBytes(state.bytes_downloaded)} / ${formatBytes(state.bytes_total)}`
      : (state.message || state.status);
  }

  if (state.status === "complete" || state.status === "error") {
    refreshSetupReadiness();
  }
}

let _modelPollTimer = null;

async function pollModelInstallStatus() {
  try {
    const state = await API.get("/onboarding/speech-model/install-status");
    _renderModelInstallState(state);
    _modelPollTimer = MODEL_ACTIVE_STATES.includes(state.status)
      ? setTimeout(pollModelInstallStatus, 500)
      : null;
  } catch (e) {
    console.error("model install status error", e);
    _modelPollTimer = null;
  }
}

async function startModelInstall() {
  try {
    const state = await API.post("/onboarding/speech-model/install", {});
    _renderModelInstallState(state);
    if (!_modelPollTimer) pollModelInstallStatus();
  } catch (e) {
    console.error("model install start error", e);
  }
}

async function cancelModelInstall() {
  try {
    const state = await API.post("/onboarding/speech-model/cancel", {});
    _renderModelInstallState(state);
  } catch (e) {
    console.error("model install cancel error", e);
  }
}

// ── First-run wizard ────────────────────────────────────────────────────────
// Steps live in the DOM at once and are shown/hidden with the `hidden`
// attribute, which also removes a hidden step from the accessibility tree —
// so keyboard focus can never land on a control belonging to a step that
// isn't on screen.

const WIZARD_LAST_STEP = 5;
let wizardStep = 0;

function wizardPanels() {
  return Array.from(document.querySelectorAll(".wizard-panel"));
}

function renderWizardStep() {
  const panels = wizardPanels();
  if (!panels.length) return;

  panels.forEach(panel => {
    panel.hidden = Number(panel.dataset.step) !== wizardStep;
  });

  document.querySelectorAll(".wizard-step-item").forEach(item => {
    const index = Number(item.dataset.stepLabel);
    item.classList.toggle("active", index === wizardStep);
    item.classList.toggle("done", index < wizardStep);
    if (index === wizardStep) {
      item.setAttribute("aria-current", "step");
    } else {
      item.removeAttribute("aria-current");
    }
  });

  const progress = $("wizard-progress-text");
  if (progress) progress.textContent = `Step ${wizardStep + 1} of ${WIZARD_LAST_STEP + 1}`;

  const back = $("wizard-back");
  const next = $("wizard-next");
  const finish = $("setup-continue");
  const skip = $("wizard-skip");
  const onLastStep = wizardStep === WIZARD_LAST_STEP;

  if (back) back.disabled = wizardStep === 0;
  if (next) next.hidden = onLastStep;
  if (finish) finish.hidden = !onLastStep;
  // Once the user has reached the end there is nothing left to skip.
  if (skip) skip.hidden = onLastStep;
}

function goToWizardStep(step) {
  wizardStep = Math.min(Math.max(step, 0), WIZARD_LAST_STEP);
  renderWizardStep();

  // Refresh the data a step actually shows, when it is shown — cheaper
  // than polling everything continuously, and it means a key saved on
  // step 2 is reflected by the summary on step 5.
  if (wizardStep === 2) refreshWizardProviders();
  if (wizardStep === 3) refreshSetupReadiness();
  if (wizardStep === 4) refreshWizardStartup();
  if (wizardStep === 5) refreshSetupReadiness();

  const panel = document.querySelector(`.wizard-panel[data-step="${wizardStep}"]`);
  const heading = panel ? panel.querySelector(".card-title, .page-title") : null;
  if (heading) {
    // Move focus to the new step's heading so screen-reader and keyboard
    // users are told where they are instead of being left on a button
    // that just changed meaning.
    heading.setAttribute("tabindex", "-1");
    heading.focus();
  }
}

async function refreshWizardProviders() {
  const host = $("wizard-provider-list");
  if (!host) return;
  host.textContent = "";

  let data;
  try {
    data = await API.get("/providers");
  } catch (e) {
    const p = document.createElement("p");
    p.className = "text-xs text-err";
    p.textContent = "Could not check providers. JARVIS still works without one.";
    host.appendChild(p);
    return;
  }

  (data.providers || []).forEach(provider => {
    const row = document.createElement("div");
    row.className = "status-row";

    const label = document.createElement("span");
    label.className = "status-row-label";
    label.textContent = provider.display_name;

    const value = document.createElement("span");
    value.className = `status-row-value ${provider.available ? "text-ok" : "text-muted"}`;
    value.textContent = provider.available ? "Available" : "Not detected";

    row.appendChild(label);
    row.appendChild(value);
    host.appendChild(row);

    const detail = document.createElement("p");
    detail.className = "text-xs text-muted mt-2";
    detail.textContent = provider.detail;
    host.appendChild(detail);

    if (provider.models && provider.models.length) {
      const models = document.createElement("p");
      models.className = "text-xs text-muted";
      models.textContent = `Models: ${provider.models.join(", ")}`;
      host.appendChild(models);
    }
  });
}

async function refreshWizardStartup() {
  const toggle = $("wizard-startup-toggle");
  const detail = $("wizard-startup-detail");
  if (!toggle) return;
  try {
    const r = await API.get("/settings/startup");
    toggle.checked = r.enabled;
    toggle.disabled = !r.supported;
    if (detail) detail.textContent = r.detail;
  } catch (e) {
    if (detail) detail.textContent = "Could not read the current setting.";
  }
}

async function setWizardStartup(enabled) {
  const toggle = $("wizard-startup-toggle");
  const detail = $("wizard-startup-detail");
  try {
    const r = await API.post("/settings/startup", { enabled });
    // Trust the server's reported state, not the click: if the shortcut
    // could not be created the checkbox must fall back rather than show
    // a setting that isn't real.
    if (toggle) toggle.checked = r.enabled;
    if (detail) detail.textContent = r.detail;
  } catch (e) {
    if (detail) detail.textContent = "Could not change the setting.";
    refreshWizardStartup();
  }
}

function initWizardControls() {
  const back = $("wizard-back");
  const next = $("wizard-next");
  const skip = $("wizard-skip");
  const startup = $("wizard-startup-toggle");

  if (back) back.addEventListener("click", () => goToWizardStep(wizardStep - 1));
  if (next) next.addEventListener("click", () => goToWizardStep(wizardStep + 1));
  if (skip) skip.addEventListener("click", finishSetup);
  if (startup) startup.addEventListener("change", () => setWizardStartup(startup.checked));

  goToWizardStep(0);
}

async function finishSetup() {
  try {
    await API.post("/onboarding/complete", {});
  } catch (e) {
    // Non-fatal — the dashboard is reachable either way.
  }
  window.location.href = "/ui/";
}

function initSetup() {
  initWizardControls();
  refreshSetupReadiness();
  refreshSetupKeyStatus();
  refreshModelPreview();
  pollModelInstallStatus();  // covers an install already running from a previous page load

  const saveBtn = $("setup-key-save");
  const removeBtn = $("setup-key-remove");
  const input = $("setup-key-input");
  const continueBtn = $("setup-continue");
  const modelStartBtn = $("model-install-start");
  const modelCancelBtn = $("model-install-cancel");
  const modelRetryBtn = $("model-install-retry");

  if (modelStartBtn) modelStartBtn.addEventListener("click", startModelInstall);
  if (modelCancelBtn) modelCancelBtn.addEventListener("click", cancelModelInstall);
  if (modelRetryBtn) modelRetryBtn.addEventListener("click", startModelInstall);

  if (saveBtn) saveBtn.addEventListener("click", async () => {
    const value = input ? input.value.trim() : "";
    if (!value) {
      _setSetupKeyMessage("Enter a key first.", false);
      return;
    }
    saveBtn.disabled = true;
    try {
      const r = await API.post("/settings/api-key", { api_key: value });
      _setSetupKeyMessage(r.message, r.success);
      if (r.success && input) input.value = "";
      refreshSetupKeyStatus();
      refreshSetupReadiness();
    } catch (e) {
      _setSetupKeyMessage("Could not reach the server.", false);
    } finally {
      saveBtn.disabled = false;
    }
  });

  if (removeBtn) removeBtn.addEventListener("click", async () => {
    removeBtn.disabled = true;
    try {
      const r = await API.post("/settings/api-key/remove", {});
      _setSetupKeyMessage(r.message, r.success);
      refreshSetupKeyStatus();
      refreshSetupReadiness();
    } catch (e) {
      _setSetupKeyMessage("Could not reach the server.", false);
    } finally {
      removeBtn.disabled = false;
    }
  });

  if (continueBtn) continueBtn.addEventListener("click", async () => {
    try {
      await API.post("/onboarding/complete", {});
    } catch (e) {
      // Non-fatal — the dashboard is still reachable directly either way.
    }
    window.location.href = "/ui/";
  });
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

// ── Privacy mode indicator ──────────────────────────────────────────────────
// v0.2: a clear, persistent, always-visible topbar indicator (present on
// every page, not just a settings screen) — see app/core/privacy.py.

function setPrivacyIndicator(active) {
  const dot   = $("topbar-privacy-dot");
  const label = $("topbar-privacy-label");
  if (!dot || !label) return;
  if (active) {
    dot.className = "status-dot status-dot-warn";
    label.textContent = "privacy: on";
  } else {
    dot.className = "status-dot status-dot-grey";
    label.textContent = "privacy: off";
  }
}

async function refreshPrivacyIndicator() {
  try {
    const status = await API.get("/privacy/status");
    setPrivacyIndicator(!!status.active);
  } catch (e) {
    // Leave the last-known indicator state rather than guessing.
  }
}

function handleStreamEvent(evt) {
  if (typeof evt.seq === "number" && evt.seq > wsLastSeq) wsLastSeq = evt.seq;

  if (evt.type === "runtime_state" && evt.payload) {
    setRuntimeLabel(evt.payload.to);
  }

  // Keep the privacy indicator live across every open tab/page the
  // instant it changes anywhere (chat command, another tab, voice).
  if (evt.type === "action_result" && evt.payload && evt.payload.tool_name === "set_privacy_mode") {
    refreshPrivacyIndicator();
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
  refreshPrivacyIndicator();

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
  } else if (path === "/ui/setup") {
    initSetup();
  }
});
