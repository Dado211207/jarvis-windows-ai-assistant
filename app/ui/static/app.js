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

// ── Dashboard ────────────────────────────────────────────────────────────────

function setStatus(id, text, cssClass) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = `text-sm ${cssClass}`;
}

async function loadDashboard() {
  const loadingIds = [
    "dash-health", "dash-db", "dash-brain", "dash-tools",
    "dash-cpu", "dash-ram", "dash-uptime", "dash-tts",
    "dash-version", "dash-phase",
  ];
  loadingIds.forEach(id => {
    const el = $(id);
    if (el) { el.textContent = "Loading…"; el.className = "text-sm loading"; }
  });

  try {
    const [health, voice, sys] = await Promise.allSettled([
      API.get("/health"),
      API.get("/voice/status"),
      API.get("/system"),
    ]);

    if (health.status === "fulfilled") {
      const h = health.value;
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
    }

    if (voice.status === "fulfilled") {
      const v = voice.value;
      setStatus("dash-tts", v.tts_enabled ? "Enabled" : "Disabled",
                v.tts_enabled ? "text-ok" : "text-muted");
      const eng = $("dash-tts-engine");
      if (eng) eng.textContent = v.tts_engine || "";
    } else {
      setStatus("dash-tts", "Unknown", "text-warn");
    }

    if (sys.status === "fulfilled") {
      const s = sys.value;
      setText("dash-cpu",    s.cpu_percent    != null ? `${s.cpu_percent}%`    : "—");
      setText("dash-ram",    s.ram_percent    != null ? `${s.ram_percent}%`    : "—");
      setText("dash-uptime", s.uptime         || "—");
      setText("dash-tools",  s.tools_registered != null ? `${s.tools_registered} registered` : "—");
      setText("dash-phase",  s.phase          || "—");
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
    const reply = typeof data.message === "string" ? data.message : JSON.stringify(data.message);
    addMessage("assistant", reply, data.tool_used || null);
  } catch (e) {
    addMessage("assistant", `Error: ${e.message}`, null);
  } finally {
    if (sendBtn) sendBtn.disabled = false;
    if (input)   input.focus();
  }
}

function initChat() {
  const btn   = $("chat-send");
  const input = $("chat-input");
  if (btn)   btn.addEventListener("click", sendChat);
  if (input) input.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
}

// ── Logs ─────────────────────────────────────────────────────────────────────

async function loadLogs() {
  const tbody = $("logs-tbody");
  if (!tbody) return;

  const loading = document.createElement("tr");
  const lc = document.createElement("td");
  lc.colSpan = 4;
  lc.className = "empty";
  lc.textContent = "Loading…";
  loading.appendChild(lc);
  tbody.textContent = "";
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

      const cells = [
        entry.created_at || "—",
        entry.command    || "—",
        entry.tool_name  || "—",
        entry.status     || "—",
      ];

      cells.forEach(val => {
        const td = document.createElement("td");
        td.textContent = val;
        row.appendChild(td);
      });

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
  loading.className = "text-sm text-muted";
  loading.style.textAlign = "center";
  loading.style.padding = "1.5rem 0";
  loading.textContent = "Loading…";
  list.appendChild(loading);

  try {
    const path = query ? `/memory/search?q=${encodeURIComponent(query)}` : "/memory";
    const data  = await API.get(path);
    const items = Array.isArray(data) ? data : (data.memories || data.results || []);

    list.textContent = "";
    if (!items.length) {
      const empty = document.createElement("p");
      empty.className = "text-sm text-muted";
      empty.style.textAlign = "center";
      empty.style.padding = "1.5rem 0";
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
    err.className = "text-sm text-muted";
    err.style.textAlign = "center";
    err.style.padding = "1.5rem 0";
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
  fields.forEach(id => { const el = $(id); if (el) { el.textContent = "…"; el.className = "text-sm"; } });

  try {
    const v = await API.get("/voice/status");

    const avail = $(  "tts-avail");
    const enabled = $("tts-enabled-val");
    const engine  = $("tts-engine-val");

    if (avail) {
      avail.textContent = v.available ? "Yes" : "No";
      avail.className   = `text-sm ${v.available ? "text-ok" : "text-err"}`;
    }
    if (enabled) {
      enabled.textContent = v.enabled ? "Enabled" : "Disabled";
      enabled.className   = `text-sm ${v.enabled ? "text-ok" : "text-muted"}`;
    }
    if (engine) {
      engine.textContent = v.engine || "—";
      engine.className   = "text-sm";
    }
  } catch (e) {
    fields.forEach(id => {
      const el = $(id);
      if (el) { el.textContent = "Error"; el.className = "text-sm text-err"; }
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

// ── Bootstrap ─────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  // Detect current page from URL and initialise its logic
  const path = window.location.pathname.replace(/\/+$/, "");

  if (path === "/ui" || path === "/ui/dashboard" || path === "") {
    loadDashboard();
  } else if (path === "/ui/chat") {
    initChat();
  } else if (path === "/ui/logs") {
    initLogs();
  } else if (path === "/ui/memory") {
    initMemory();
  } else if (path === "/ui/voice") {
    initVoice();
  }
});
