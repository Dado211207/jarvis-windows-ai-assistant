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

// When the server refuses something it explains why (FastAPI puts that in
// `detail`). Surfacing it beats "409 Conflict", which tells a user
// nothing they can act on. Falls back to the status line when there is
// no explanation to show.
async function errorFromResponse(r) {
  try {
    const body = await r.json();
    if (body && typeof body.detail === "string" && body.detail) return new Error(body.detail);
  } catch (e) { /* not JSON — fall through to the status line */ }
  return new Error(`${r.status} ${r.statusText}`);
}

const API = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw await errorFromResponse(r);
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
    if (!r.ok) throw await errorFromResponse(r);
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
  if (!list) return null;

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

  let speakBtn = null;
  if (role === "assistant") {
    speakBtn = makeSpeakButton(() => bubble.textContent);
    wrap.appendChild(speakBtn);
  }

  if (toolUsed) {
    const tool = document.createElement("div");
    tool.className = "msg-tool";
    tool.textContent = `tool: ${toolUsed}`;
    wrap.appendChild(tool);
  }

  list.appendChild(wrap);
  list.scrollTop = list.scrollHeight;
  return speakBtn;
}

// A message bubble that grows as text streams in. Returns an appender so
// the caller never has to touch the DOM node itself.
function addStreamingMessage() {
  const list = $("chat-messages");
  if (!list) return { append: () => {}, set: () => {}, isEmpty: () => true };

  const empty = $("chat-empty");
  if (empty && chatEmpty) { empty.style.display = "none"; chatEmpty = false; }

  const wrap = document.createElement("div");
  wrap.className = "msg msg-assistant";

  const roleEl = document.createElement("div");
  roleEl.className = "msg-role";
  roleEl.textContent = "JARVIS";

  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";
  bubble.textContent = "";

  wrap.appendChild(roleEl);
  wrap.appendChild(bubble);

  // Hidden until the answer is complete: a Listen button on half an
  // answer would read half an answer.
  const speakBtn = makeSpeakButton(() => bubble.textContent);
  speakBtn.hidden = true;
  wrap.appendChild(speakBtn);

  list.appendChild(wrap);

  let buffer = "";
  return {
    append(text) {
      buffer += text;
      bubble.textContent = buffer;   // textContent only, per CLAUDE.md's XSS rule
      list.scrollTop = list.scrollHeight;
    },
    set(text) {
      buffer = text;
      bubble.textContent = buffer;
      list.scrollTop = list.scrollHeight;
    },
    isEmpty() { return buffer.length === 0; },
    text() { return buffer; },
    remove() { wrap.remove(); },
    finish() {
      if (buffer.trim()) speakBtn.hidden = false;
      return speakBtn;
    },
  };
}

// ── Speaking a reply ─────────────────────────────────────────────────────────
//
// The server decides whether to actually speak. Asking every time rather
// than caching a flag here means a page left open after speech was
// switched off elsewhere can't make JARVIS narrate on its own.
//
// Two endpoints, because they answer two different questions:
//   /voice/speak       — read this new reply automatically. Gated on the
//                        "Speak responses" setting, server-side.
//   /voice/speak-once  — read *this* message, because I just pressed its
//                        button. Not gated on that setting; pressing the
//                        button is the request.
//
// One utterance at a time. Every path stops whatever is playing before
// starting, so a reply arriving while an older one is being read does not
// produce two voices talking over each other.

const SPOKEN_REPLY_MAX_CHARS = 1000;  // matches MAX_SPEAK_LENGTH server-side
const SPEECH_POLL_MS = 700;

const SPEAK_GLYPH = "▶";   // ▶
const STOP_GLYPH  = "■";   // ■

let speakingButton = null;
let speakingPoll = null;

function paintSpeakButton(btn, speaking) {
  if (!btn) return;
  const glyph = btn.querySelector(".msg-speak-glyph");
  const label = btn.querySelector(".msg-speak-label");
  if (glyph) glyph.textContent = speaking ? STOP_GLYPH : SPEAK_GLYPH;
  if (label) label.textContent = speaking ? "Stop" : "Listen";
  btn.setAttribute("aria-label", speaking ? "Stop reading this answer aloud" : "Read this answer aloud");
  btn.setAttribute("aria-pressed", speaking ? "true" : "false");
  btn.classList.toggle("is-speaking", !!speaking);
}

function makeSpeakButton(getText) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "msg-speak";

  const glyph = document.createElement("span");
  glyph.className = "msg-speak-glyph";
  glyph.setAttribute("aria-hidden", "true");

  const label = document.createElement("span");
  label.className = "msg-speak-label";

  btn.appendChild(glyph);
  btn.appendChild(label);
  paintSpeakButton(btn, false);

  btn.addEventListener("click", () => speakOnDemand(getText(), btn));
  return btn;
}

// ── Keeping JARVIS's own voice out of the clap detector ─────────────────────
//
// Every speech path — Kokoro, a Windows natural voice, SAPI5, ElevenLabs,
// the per-message Listen button, "speak test", a spoken reply — ends up
// asking the same server whether it is still speaking. So there is one
// watcher rather than eight hooks: suspend before the audio starts, and
// resume only when /voice/speaking says it has finished. The controller
// adds its own short refractory on top, so the speaker's last click is
// not the first half of a pair.
let speechSuspendPoll = null;

function suspendForSpeech() {
  clapSuspend(CLAP_REASON.SPEAKING);
  if (speechSuspendPoll) return;
  speechSuspendPoll = setInterval(async () => {
    let speaking = false;
    try {
      const r = await API.get("/voice/speaking");
      speaking = !!(r && r.speaking);
    } catch (e) {
      speaking = false;   // an unanswerable question must not leave it suspended forever
    }
    if (!speaking) releaseSpeechSuspension();
  }, SPEECH_POLL_MS);
}

function releaseSpeechSuspension() {
  if (speechSuspendPoll) { clearInterval(speechSuspendPoll); speechSuspendPoll = null; }
  clapResume(CLAP_REASON.SPEAKING);
}

function forgetSpeaking() {
  if (speakingPoll) { clearInterval(speakingPoll); speakingPoll = null; }
  if (speakingButton) paintSpeakButton(speakingButton, false);
  speakingButton = null;
}

// Speech ends on its own and the server can only report that it *started*,
// so the button that says Stop has to check whether it is still true.
function watchSpeech(btn) {
  forgetSpeaking();
  if (!btn) return;
  speakingButton = btn;
  paintSpeakButton(btn, true);
  speakingPoll = setInterval(async () => {
    try {
      const r = await API.get("/voice/speaking");
      if (!r || !r.speaking) forgetSpeaking();
    } catch (e) {
      forgetSpeaking();   // an unanswerable question must not leave a stuck button
    }
  }, SPEECH_POLL_MS);
}

async function stopSpeech() {
  const wasSpeaking = speakingButton !== null;
  forgetSpeaking();
  releaseSpeechSuspension();
  if (!wasSpeaking) return;
  try {
    await API.post("/voice/stop", {});
  } catch (e) {
    console.warn("could not stop speech", e);
  }
}

// The speaker button on one message.
async function speakOnDemand(text, btn) {
  if (speakingButton === btn) { await stopSpeech(); return; }
  await stopSpeech();

  const trimmed = (text || "").trim();
  if (!trimmed) return;
  suspendForSpeech();
  try {
    const r = await API.post("/voice/speak-once", { text: trimmed.slice(0, SPOKEN_REPLY_MAX_CHARS) });
    if (r && r.success) {
      watchSpeech(btn);
      setChatStatus("");
    } else {
      releaseSpeechSuspension();
      // The engine's own reason and the step that fixes it — never a
      // suggestion to go and find another program.
      setChatStatus((r && r.message) || "JARVIS could not speak that.");
    }
  } catch (e) {
    releaseSpeechSuspension();
    setChatStatus("Could not speak that: " + e.message);
  }
}

// A new reply arriving, spoken only if the user asked for that.
async function speakReply(text, btn) {
  const trimmed = (text || "").trim();
  if (!trimmed) return;
  await stopSpeech();
  suspendForSpeech();
  try {
    const r = await API.post("/voice/speak", { text: trimmed.slice(0, SPOKEN_REPLY_MAX_CHARS) });
    if (r && r.success) watchSpeech(btn);
    else releaseSpeechSuspension();
  } catch (e) {
    releaseSpeechSuspension();
    // Speech is an enhancement; a failure here must never disturb the
    // conversation the user is reading.
    console.warn("could not speak the reply", e);
  }
}

let currentGenerationId = null;

function setChatBusy(busy) {
  const sendBtn = $("chat-send");
  const stopBtn = $("chat-stop");
  if (sendBtn) sendBtn.disabled = busy;
  if (stopBtn) stopBtn.hidden = !busy;
}

function setChatStatus(text) {
  const el = $("chat-status");
  if (el) el.textContent = text || "";
}

async function sendChat() {
  const input = $("chat-input");
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;

  input.value = "";
  addMessage("user", text, null);
  setChatBusy(true);
  setChatStatus("");

  try {
    await streamChat(text);
  } catch (e) {
    // Streaming was unavailable (an old browser, or a proxy that buffers
    // the body away). Fall back to the plain one-shot endpoint rather
    // than showing the user an error for something they can't act on.
    console.warn("streaming unavailable, falling back to /command", e);
    await sendChatFallback(text);
  } finally {
    currentGenerationId = null;
    setChatBusy(false);
    if (input) input.focus();
  }
}

async function streamChat(text) {
  const token = getSessionCookie();
  const res = await fetch("/chat/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-JARVIS-Session-Token": token } : {}),
    },
    body: JSON.stringify({ command: text }),
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  if (!res.body || !res.body.getReader) throw new Error("streaming unsupported");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let stream = null;
  let sawError = false;

  const handle = evt => {
    if (evt.type === "start") {
      currentGenerationId = evt.generation_id || null;
      if (evt.model) setChatStatus(`Answering with ${evt.model}.`);
    } else if (evt.type === "routed") {
      const data = evt.response || {};
      if (data.requires_approval && data.pending_action_id) {
        // Deliberately not spoken: an approval prompt is something to
        // read and decide on, not something to hear read out.
        addApprovalCard(data.pending_action_id, data);
      } else {
        const btn = addMessage("assistant", data.message || "", data.tool_used || null);
        speakReply(data.message || "", btn);
      }
    } else if (evt.type === "delta") {
      if (!stream) stream = addStreamingMessage();
      stream.append(evt.text || "");
    } else if (evt.type === "error") {
      sawError = true;
      if (!stream) stream = addStreamingMessage();
      // A partial answer plus the reason it stopped is more useful than
      // either alone, so the error is appended rather than replacing it.
      stream.append((stream.isEmpty() ? "" : "\n\n") + (evt.message || "Something went wrong."));
      const id = evt.error && evt.error.correlation_id;
      setChatStatus(id ? `Reference: ${id}` : "");
    } else if (evt.type === "done") {
      if (evt.stopped) {
        setChatStatus("Stopped.");
        if (stream && stream.isEmpty()) stream.set("Stopped before any response arrived.");
      } else if (!sawError) {
        setChatStatus("");
      }
      const btn = stream ? stream.finish() : null;
      if (!sawError && stream && !stream.isEmpty()) speakReply(stream.text(), btn);
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let split;
    while ((split = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, split).trim();
      buffer = buffer.slice(split + 2);
      if (!frame.startsWith("data:")) continue;
      try {
        handle(JSON.parse(frame.slice(5).trim()));
      } catch (parseError) {
        console.warn("unparseable chat event", parseError);
      }
    }
  }
}

async function sendChatFallback(text) {
  try {
    const data = await API.post("/command", { command: text });
    if (data.requires_approval && data.pending_action_id) {
      addApprovalCard(data.pending_action_id, data);
    } else {
      const reply = typeof data.message === "string" ? data.message : JSON.stringify(data.message);
      const btn = addMessage("assistant", reply, data.tool_used || null);
      speakReply(reply, btn);
    }
  } catch (e) {
    addMessage("assistant", "Error: " + e.message, null);
  }
}

async function stopChat() {
  setChatStatus("Stopping…");
  try {
    const r = await API.post("/chat/stop", { generation_id: currentGenerationId });
    if (!r.stopped) setChatStatus(r.message || "");
  } catch (e) {
    setChatStatus("Could not stop the response: " + e.message);
  }
}

async function resetConversation() {
  const confirmed = window.confirm(
    "Clear this chat and the stored conversation history?\n\n" +
    "This cannot be undone. Your action history and logs are not affected."
  );
  if (!confirmed) return;

  // The button that would stop it is about to be removed from the page.
  await stopSpeech();

  try {
    const r = await API.post("/conversation/reset", {});
    const list = $("chat-messages");
    if (list) {
      Array.from(list.children).forEach(node => {
        if (node.id !== "chat-empty") node.remove();
      });
    }
    const empty = $("chat-empty");
    if (empty) { empty.style.display = ""; chatEmpty = true; }
    setChatStatus(r.message || "Chat cleared.");
  } catch (e) {
    setChatStatus("Could not clear the conversation: " + e.message);
  }
}

async function refreshChatProvider() {
  const el = $("chat-provider");
  if (!el) return;
  try {
    const r = await API.get("/providers");
    const active = (r.providers || []).find(p => p.name === r.selected);
    if (active && active.available) {
      el.textContent = `AI: ${active.display_name}`;
    } else {
      // Never reads as broken: deterministic commands are the majority
      // of what this box is for and they work with no provider at all.
      el.textContent = "AI not configured — commands still work.";
    }
  } catch (e) {
    el.textContent = "Could not check the AI provider.";
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
  const stop  = $("chat-stop");
  const reset = $("chat-reset");
  if (btn)   btn.addEventListener("click", sendChat);
  if (stop)  stop.addEventListener("click", stopChat);
  if (reset) reset.addEventListener("click", resetConversation);
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

  refreshChatProvider();
  initSpeakRepliesToggle();
  initPushToTalk();
}

// The "Speak replies" switch in the chat toolbar. Reads and writes the
// same saved setting as the Voice page toggle through /voice/output, so
// the two controls can never disagree about the one flag.
async function initSpeakRepliesToggle() {
  const toggle = $("chat-speak-replies");
  const label = $("chat-speak-replies-label");
  if (!toggle) return;

  const paint = status => {
    toggle.checked = !!(status && status.tts_enabled);
    if (!label) return;
    if (status && !status.tts_available) {
      // Honest rather than encouraging: the switch really can be on
      // while nothing on this machine can make a sound.
      label.textContent = "Speak replies (no voice installed)";
    } else {
      label.textContent = "Speak replies";
    }
  };

  try {
    paint(await API.get("/voice/status"));
  } catch (e) {
    toggle.disabled = true;
    if (label) label.textContent = "Speak replies (unavailable)";
    return;
  }

  toggle.addEventListener("change", async () => {
    const wanted = toggle.checked;
    try {
      // The server returns the state actually in effect, not the
      // requested one, so a setting that could not be saved shows as
      // what it really is instead of flipping back on the next load.
      paint(await API.post("/voice/output", { enabled: wanted }));
      if (!wanted) await stopSpeech();
    } catch (e) {
      setChatStatus("Could not change that setting: " + e.message);
      try { paint(await API.get("/voice/status")); } catch (ignored) { /* leave as-is */ }
    }
  });
}

// ── Push-to-talk (v0.2) ──────────────────────────────────────────────────────
// Optional. Text input always works whether or not this is available —
// see app/voice/stt.py. One explicit recording per press; nothing is
// captured continuously or in the background.

const PTT_STATE = { IDLE: "idle", REQUESTING: "requesting", LISTENING: "listening", TRANSCRIBING: "transcribing", ERROR: "error" };
const PTT_MAX_RECORDING_MS = 60 * 1000;
let pttState = PTT_STATE.IDLE;
let pttRecorder = null;
let pttChunks = [];
let pttStream = null;
let pttUploadController = null;
let pttRecordingTimer = null;
let pttRequestGeneration = 0;
let pttStopMessage = "";
// Sticky, independent of pttState: true once /voice/stt-status reports
// unavailable. setPttState() must keep respecting this on every call —
// it previously recomputed `disabled` from pttState alone and silently
// re-enabled the button on the very next state change (caught via real
// browser testing, not a unit test: see tests/test_playwright_e2e.py).
let pttUnavailable = false;

function setPttState(state, message) {
  pttState = state;
  // Requesting, listening and transcribing all own the microphone or the
  // conversation; idle and error do not. Routing suspension through the
  // one function every path already calls is what makes cancel, abort
  // and failure release it too.
  if (state === PTT_STATE.IDLE || state === PTT_STATE.ERROR) {
    clapResume(CLAP_REASON.PTT);
  } else {
    clapSuspend(CLAP_REASON.PTT);
  }
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
    cancelBtn.hidden = !(
      state === PTT_STATE.REQUESTING
      || state === PTT_STATE.LISTENING
      || state === PTT_STATE.TRANSCRIBING
    );
  }
  if (status) {
    status.textContent = message || "";
  }
}

function pttClearRecordingTimer() {
  if (pttRecordingTimer) {
    clearTimeout(pttRecordingTimer);
    pttRecordingTimer = null;
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

  const requestGeneration = ++pttRequestGeneration;
  setPttState(PTT_STATE.REQUESTING, "Requesting microphone permission…");
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    if (requestGeneration !== pttRequestGeneration) return;
    setPttState(PTT_STATE.ERROR, "Microphone unavailable or permission denied. Text input still works.");
    return;
  }

  // getUserMedia cannot itself be aborted. If Cancel or pagehide happened
  // while its permission prompt was open, stop the late stream immediately
  // instead of reviving a recording the user already ended.
  if (requestGeneration !== pttRequestGeneration || pttState !== PTT_STATE.REQUESTING) {
    stream.getTracks().forEach(track => track.stop());
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
  pttRecorder.addEventListener("error", pttOnRecorderError);

  try {
    pttRecorder.start();
  } catch (e) {
    pttOnRecorderError();
    return;
  }
  setPttState(PTT_STATE.LISTENING, "Listening… click again or press Alt+M to stop.");
  pttRecordingTimer = setTimeout(() => {
    if (pttState === PTT_STATE.LISTENING) {
      pttStop("Maximum recording length reached. Transcribing…");
    }
  }, PTT_MAX_RECORDING_MS);
}

function pttStop(message = "") {
  pttClearRecordingTimer();
  pttStopMessage = message;
  if (pttRecorder && pttRecorder.state !== "inactive") {
    pttRecorder.stop(); // triggers pttOnRecordingStopped via the "stop" event
  }
}

function pttCancel() {
  ++pttRequestGeneration;
  pttClearRecordingTimer();
  pttStopMessage = "";
  if (pttUploadController) {
    pttUploadController.abort();
    pttUploadController = null;
  }
  if (pttRecorder) {
    pttRecorder.removeEventListener("stop", pttOnRecordingStopped);
    pttRecorder.removeEventListener("error", pttOnRecorderError);
    if (pttRecorder.state !== "inactive") pttRecorder.stop();
    pttRecorder = null;
  }
  pttReleaseMicrophone();
  pttChunks = [];
  setPttState(PTT_STATE.IDLE, "Cancelled.");
}

function pttOnRecorderError() {
  ++pttRequestGeneration;
  pttClearRecordingTimer();
  pttStopMessage = "";
  if (pttRecorder) {
    pttRecorder.removeEventListener("stop", pttOnRecordingStopped);
    pttRecorder.removeEventListener("error", pttOnRecorderError);
  }
  pttRecorder = null;
  pttReleaseMicrophone();
  pttChunks = [];
  setPttState(PTT_STATE.ERROR, "Recording stopped unexpectedly. Text input still works.");
}

async function pttOnRecordingStopped() {
  pttClearRecordingTimer();
  if (pttRecorder) pttRecorder.removeEventListener("error", pttOnRecorderError);
  pttRecorder = null;
  pttReleaseMicrophone();
  const blob = new Blob(pttChunks, { type: "audio/webm" });
  pttChunks = [];

  if (blob.size === 0) {
    pttStopMessage = "";
    setPttState(PTT_STATE.IDLE, "No audio captured.");
    return;
  }

  const stopMessage = pttStopMessage;
  pttStopMessage = "";
  setPttState(PTT_STATE.TRANSCRIBING, stopMessage || "Transcribing…");

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
      if (item.id !== undefined && item.id !== null) {
        card.appendChild(_memoryDeleteButton(item, query));
      }
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

function _memoryDeleteButton(item, query) {
  const button = document.createElement("button");
  button.className = "btn btn-ghost btn-sm memory-delete";
  button.type = "button";
  // Named, so a screen-reader user hears which memory this removes
  // rather than a row of identical "Delete" buttons.
  const preview = (item.content || "").slice(0, 40);
  button.setAttribute("aria-label", `Delete memory: ${preview}`);
  button.textContent = "Delete";

  button.addEventListener("click", async () => {
    if (!window.confirm("Delete this memory?\n\nThis cannot be undone.")) return;
    button.disabled = true;
    try {
      const r = await API.post(`/memory/${item.id}/delete`, {});
      if (r.success) {
        loadMemory(query || null);
      } else {
        _setMemoryMessage(r.message, false);
        button.disabled = false;
      }
    } catch (e) {
      _setMemoryMessage("Could not delete that memory: " + e.message, false);
      button.disabled = false;
    }
  });
  return button;
}

function _setMemoryMessage(text, ok) {
  const el = $("memory-add-message");
  if (!el) return;
  el.textContent = text || "";
  el.className = `text-xs mt-2 ${ok ? "text-ok" : "text-err"}`;
}

async function addMemoryFromPage() {
  const input = $("memory-add-input");
  if (!input) return;
  const content = input.value.trim();
  if (!content) { _setMemoryMessage("Type something to remember first.", false); return; }

  const button = $("memory-add-btn");
  if (button) button.disabled = true;
  try {
    const r = await API.post("/memory", { content });
    // Privacy mode refuses the write, and says so — the page must show
    // that refusal rather than pretending it saved.
    _setMemoryMessage(r.message, r.success);
    if (r.success) {
      input.value = "";
      loadMemory(null);
    }
  } catch (e) {
    _setMemoryMessage("Could not save that memory: " + e.message, false);
  } finally {
    if (button) button.disabled = false;
  }
}

function initMemory() {
  const btn   = $("memory-search-btn");
  const input = $("memory-search");
  const addBtn = $("memory-add-btn");
  const addInput = $("memory-add-input");

  const doSearch = () => {
    const q = input ? input.value.trim() : "";
    loadMemory(q || null);
  };

  if (btn)   btn.addEventListener("click", doSearch);
  if (input) input.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); doSearch(); }
  });

  if (addBtn) addBtn.addEventListener("click", addMemoryFromPage);
  if (addInput) addInput.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); addMemoryFromPage(); }
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
      enabled.textContent = isEnabled ? "Yes" : "No";
      enabled.className   = `status-row-value ${isEnabled ? "text-ok" : "text-muted"}`;
      const toggle = $("voice-output-toggle");
      if (toggle) toggle.checked = isEnabled;   // trust the server, not local state
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
  // Covers "speak test" and anything else on the Voice page that can
  // make a noise. Harmless for the commands that cannot: the watcher
  // releases as soon as the server says nothing is speaking.
  suspendForSpeech();
  try {
    await API.post("/command", { command: cmd });
    await refreshVoiceStatus();
  } catch (e) {
    releaseSpeechSuspension();
    console.error("voice command error", e);
  }
}

async function voiceStop() {
  releaseSpeechSuspension();
  try {
    await API.post("/voice/stop", {});
  } catch (e) {
    console.error("voice stop error", e);
  }
}

async function setVoiceOutput(enabled) {
  const message = $("voice-output-message");
  const toggle = $("voice-output-toggle");
  try {
    const r = await API.post("/voice/output", { enabled });
    // The response carries the state actually in effect, which is not
    // necessarily what was asked for — see /voice/output.
    if (toggle) toggle.checked = r.tts_enabled;
    if (message) {
      if (r.tts_enabled && !r.tts_available) {
        message.textContent = "Turned on, but no speech engine is available on this computer, so nothing will be spoken.";
        message.className = "text-xs mt-2 text-warn";
      } else {
        message.textContent = r.tts_enabled
          ? "JARVIS will speak its replies."
          : "JARVIS will stay silent.";
        message.className = "text-xs mt-2 text-muted";
      }
    }
    await refreshVoiceStatus();
  } catch (e) {
    if (toggle) toggle.checked = !enabled;   // never show a state that isn't real
    if (message) {
      message.textContent = "Could not change the setting: " + e.message;
      message.className = "text-xs mt-2 text-err";
    }
  }
}

// Voice input status, shown on the Voice page so this page describes the
// whole voice experience rather than only the half that speaks.
async function refreshVoiceInputStatus() {
  const avail = $("stt-avail");
  const model = $("stt-model");
  const detail = $("stt-detail");
  if (!avail && !model) return;

  try {
    const r = await API.get("/voice/stt-status");
    if (avail) {
      avail.textContent = r.available ? "Ready" : "Not ready";
      avail.className = `status-row-value ${r.available ? "text-ok" : "text-muted"}`;
    }
    if (detail) detail.textContent = r.reason || "";
  } catch (e) {
    if (avail) { avail.textContent = "Unknown"; avail.className = "status-row-value"; }
    if (detail) detail.textContent = "Could not check speech recognition.";
  }

  if (!model) return;
  try {
    const info = await API.get("/onboarding/readiness");
    const ready = info.speech_model && info.speech_model.ready;
    model.textContent = ready ? "Installed" : "Not installed";
    model.className = `status-row-value ${ready ? "text-ok" : "text-muted"}`;
  } catch (e) {
    model.textContent = "Unknown";
    model.className = "status-row-value";
  }
}


// ── Voice diagnostics ───────────────────────────────────────────────────────
// Reported the packaged app said "Speech runtime — Not ready" and push-to-talk
// did nothing, with no way to find out why. The answers live in two places and
// neither can see the other's: the server knows about the engine and the model
// on disk, the browser knows about microphone permission, devices and level.
// This panel shows both, separately, so a failure names itself.
//
// Nothing here records, uploads or stores audio. The level meter reads the
// live stream and discards each sample as it goes; the stream is stopped when
// the test ends.

const DIAG_TEST_DURATION_MS = 5000;
let diagLevelStream = null;
let diagLevelRaf = null;
let diagAudioContext = null;

function _setDiag(id, text, tone) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = `status-row-value${tone ? " " + tone : ""}`;
}

// The ten states from app/voice/input_state.py, in plain words. Three of
// them — the microphone permission ones and "no input device" — can only
// be known in the browser, so they are decided here and overlaid on the
// server's answer: a refused microphone matters more than a model that
// is present and unused.
const VOICE_INPUT_LABELS = {
  disabled: "Switched off",
  permission_not_requested: "Microphone not allowed yet",
  permission_denied: "Microphone permission denied",
  no_input_device: "No microphone found",
  runtime_missing: "Speech engine missing",
  model_missing: "Speech model not downloaded",
  downloading: "Downloading the speech model",
  verifying: "Checking the speech model",
  ready: "Ready",
  transcription_failed: "The last recording failed",
};

let browserVoiceState = null;   // set by the permission/device checks below

function renderVoiceInputState(server) {
  // The browser's own findings win: being unable to hear is a more
  // immediate problem than anything on disk.
  const state = browserVoiceState || (server && server.state) || "";
  const ready = state === "ready";
  const busy = state === "downloading" || state === "verifying";

  _setDiag("diag-state", VOICE_INPUT_LABELS[state] || "Unknown",
           ready ? "text-ok" : (busy ? "text-muted" : "text-warn"));

  const detail = $("diag-state-detail");
  if (detail) {
    detail.textContent = browserVoiceState
      ? BROWSER_STATE_DETAIL[browserVoiceState] || ""
      : ((server && server.last_failure) || (server && server.reason) || "");
  }
  setText("diag-next-step", browserVoiceState
    ? BROWSER_STATE_NEXT[browserVoiceState] || ""
    : ((server && server.next_step) || ""));

  const wrap = $("diag-progress-wrap");
  if (wrap) wrap.hidden = !busy;
  const bar = $("diag-progress-bar");
  if (bar) bar.style.width = ((server && server.percent) || 0) + "%";
}

const BROWSER_STATE_DETAIL = {
  permission_not_requested:
    "Windows and this window have not been asked for the microphone yet. Nothing is listening.",
  permission_denied:
    "The microphone was refused for JARVIS, so no recording can start.",
  no_input_device:
    "No microphone is connected to this computer, or Windows is not reporting one.",
};

const BROWSER_STATE_NEXT = {
  permission_not_requested:
    "Press Test microphone — Windows will ask once, and you can say no.",
  permission_denied:
    "Allow the microphone for JARVIS in Windows privacy settings, then press Run diagnostics again.",
  no_input_device:
    "Plug in a microphone or headset and press Run diagnostics again. Typing works normally either way.",
};

async function refreshVoiceDiagnostics() {
  let diagnostics = null;
  try {
    const r = await API.get("/voice/diagnostics");
    diagnostics = r;
    _setDiag("diag-runtime", r.runtime_ready ? "Installed" : "Not installed",
             r.runtime_ready ? "text-ok" : "text-err");
    const runtimeEl = $("diag-runtime");
    if (runtimeEl) runtimeEl.title = r.runtime_detail || "";

    _setDiag("diag-model", r.model_ready ? "Installed" : "Not installed",
             r.model_ready ? "text-ok" : "text-muted");
    const modelEl = $("diag-model");
    if (modelEl) modelEl.title = r.model_detail || "";

    const pathEl = $("diag-model-path");
    if (pathEl) {
      pathEl.textContent = r.model_path || "No model installed";
      pathEl.className = "status-row-value font-mono";
    }

    const toggle = $("voice-input-toggle");
    if (toggle) toggle.checked = r.enabled;
  } catch (e) {
    _setDiag("diag-runtime", "Unknown");
    _setDiag("diag-model", "Unknown");
  }

  browserVoiceState = null;
  await refreshMicrophonePermission();
  await refreshInputDevices();
  renderVoiceInputState(diagnostics);
  await refreshSpeechOutputDiagnostics();
}

// The speaking half. Somebody whose voice is not working comes to the
// diagnostics panel; before this, half the answer was on a different
// card further up the page, and two of these four rows did not exist
// anywhere.
async function refreshSpeechOutputDiagnostics() {
  if (!$("diag-out-engine")) return;

  try {
    const s = await API.get("/voice/engine-status");
    _setDiag("diag-out-engine", s.available ? s.active_engine_name : "No working voice",
             s.available ? "text-ok" : "text-err");
    _setDiag("diag-out-enabled", s.speaks_replies ? "On" : "Off",
             s.speaks_replies ? "text-ok" : "text-muted");
  } catch (e) {
    _setDiag("diag-out-engine", "Unknown");
    _setDiag("diag-out-enabled", "Unknown");
  }

  try {
    const c = await API.get("/voice/cloud");
    let text = "Not set up";
    let tone = "text-muted";
    if (c.blocked_by_privacy) {
      text = "Blocked by privacy mode";
      tone = "text-err";
    } else if (!c.key_configured) {
      text = "No API key";
    } else if (!c.voice_id) {
      text = "Key saved, no voice chosen";
      tone = "text-err";
    } else {
      text = c.selected ? `In use — ${c.voice_name || c.voice_id}` : "Ready, but not selected";
      tone = c.selected ? "text-ok" : "text-muted";
    }
    _setDiag("diag-out-cloud", text, tone);
  } catch (e) {
    _setDiag("diag-out-cloud", "Unknown");
  }

  try {
    const k = await API.get("/voice/clap");
    let text = "Off";
    let tone = "text-muted";
    if (k.privacy_blocking && k.enabled) {
      text = "Blocked by privacy mode";
      tone = "text-err";
    } else if (k.enabled && clapListening()) {
      text = "Listening";
      tone = "text-ok";
    } else if (k.enabled) {
      // Switched on but nothing is running: the microphone could not be
      // opened. Saying "on" here would be the kind of accurate-but-
      // useless row this whole panel exists to replace.
      text = "On, but the microphone could not be opened";
      tone = "text-err";
    }
    _setDiag("diag-clap", text, tone);
  } catch (e) {
    _setDiag("diag-clap", "Unknown");
  }
}

async function refreshMicrophonePermission() {
  const el = $("diag-mic-permission");
  if (!el) return;
  if (!navigator.permissions || !navigator.permissions.query) {
    // Not every engine implements the Permissions API for microphone.
    // Saying "unknown until you test" is honest; guessing "granted" is not.
    _setDiag("diag-mic-permission", "Unknown until tested", "text-muted");
    return;
  }
  try {
    const status = await navigator.permissions.query({ name: "microphone" });
    const label = { granted: "Granted", denied: "Denied", prompt: "Not asked yet" }[status.state] || status.state;
    _setDiag("diag-mic-permission", label,
             status.state === "granted" ? "text-ok" : (status.state === "denied" ? "text-err" : "text-muted"));
    if (status.state === "denied") browserVoiceState = "permission_denied";
    else if (status.state === "prompt") browserVoiceState = "permission_not_requested";
  } catch (e) {
    _setDiag("diag-mic-permission", "Unknown until tested", "text-muted");
  }
}

async function refreshInputDevices() {
  const summary = $("diag-mic-device");
  const select = $("diag-device-select");
  if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) {
    _setDiag("diag-mic-device", "Not supported by this browser", "text-err");
    return;
  }
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const inputs = devices.filter(d => d.kind === "audioinput");
    if (!inputs.length) {
      _setDiag("diag-mic-device", "No microphone detected", "text-err");
      // Overrides a permission state: there is nothing to grant access to.
      browserVoiceState = "no_input_device";
      return;
    }
    // Labels are empty until permission has been granted at least once —
    // that is a browser privacy rule, not a fault, so say so rather than
    // showing a list of blanks.
    const named = inputs.filter(d => d.label);
    _setDiag(
      "diag-mic-device",
      named.length ? named[0].label : `${inputs.length} detected (names hidden until permission is granted)`,
      "text-ok",
    );

    if (select) {
      // The saved choice wins over whatever this dropdown happens to be
      // showing: the list is rebuilt whenever devices change, and the
      // preference is the thing that survived the restart.
      let chosen = select.value;
      try {
        const saved = (await API.get("/voice/clap")).device_id;
        if (saved) chosen = saved;
      } catch (e) { /* keep whatever is on screen */ }

      while (select.options.length > 1) select.remove(1);
      inputs.forEach((device, index) => {
        const option = document.createElement("option");
        option.value = device.deviceId;
        option.textContent = device.label || `Microphone ${index + 1}`;
        select.appendChild(option);
      });
      // A saved device that is no longer plugged in must not silently
      // read as "selected" — fall the dropdown back to the default entry
      // so the screen matches what would actually open.
      const known = Array.prototype.some.call(select.options, o => o.value === chosen);
      select.value = known ? chosen : "";
      const missing = $("diag-device-missing");
      if (missing) {
        missing.textContent = (chosen && !known)
          ? "The microphone you chose is not connected. JARVIS will use the system default until it comes back."
          : "";
      }
    }
  } catch (e) {
    _setDiag("diag-mic-device", "Could not list devices", "text-err");
  }
}

function stopMicrophoneTest() {
  clapResume(CLAP_REASON.MIC_TEST);
  if (diagLevelRaf) { cancelAnimationFrame(diagLevelRaf); diagLevelRaf = null; }
  if (diagLevelStream) {
    diagLevelStream.getTracks().forEach(track => track.stop());
    diagLevelStream = null;
  }
  if (diagAudioContext) {
    try { diagAudioContext.close(); } catch (e) { /* already closed */ }
    diagAudioContext = null;
  }
  const bar = $("diag-level-bar");
  if (bar) bar.style.width = "0%";
}

async function testMicrophone() {
  const message = $("diag-test-message");
  const button = $("diag-test-mic");
  const select = $("diag-device-select");

  stopMicrophoneTest();
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.AudioContext) {
    if (message) message.textContent = "This browser cannot open a microphone.";
    _setDiag("diag-last-result", "Not supported", "text-err");
    return;
  }

  if (button) button.disabled = true;
  if (message) message.textContent = "Asking for microphone permission…";
  clapSuspend(CLAP_REASON.MIC_TEST);

  const deviceId = select && select.value ? select.value : null;
  const constraints = { audio: deviceId ? { deviceId: { exact: deviceId } } : true };

  try {
    diagLevelStream = await navigator.mediaDevices.getUserMedia(constraints);
  } catch (e) {
    // Releases CLAP_REASON.MIC_TEST as well as tidying up. Returning
    // without this left the clap listener suspended until the page was
    // closed — a microphone that could not be opened for a five-second
    // level test is no reason to stop listening for claps for good.
    stopMicrophoneTest();
    if (button) button.disabled = false;
    // Name the actual cause: a denied prompt and an absent device need
    // different things from the user.
    const denied = e && (e.name === "NotAllowedError" || e.name === "SecurityError");
    const missing = e && (e.name === "NotFoundError" || e.name === "OverconstrainedError");
    const text = denied
      ? "Microphone permission was denied. Allow it for JARVIS in Windows Settings › Privacy › Microphone, then test again."
      : (missing ? "No microphone was found on this computer." : "The microphone could not be opened.");
    if (message) message.textContent = text;
    _setDiag("diag-last-result", denied ? "Permission denied" : "Failed", "text-err");
    refreshMicrophonePermission();
    return;
  }

  // Device labels only become readable after permission is granted.
  refreshMicrophonePermission();
  refreshInputDevices();

  diagAudioContext = new AudioContext();
  const source = diagAudioContext.createMediaStreamSource(diagLevelStream);
  const analyser = diagAudioContext.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);

  const samples = new Uint8Array(analyser.frequencyBinCount);
  const bar = $("diag-level-bar");
  const startedAt = Date.now();
  let peak = 0;

  if (message) message.textContent = "Listening for a few seconds — say something.";

  function tick() {
    if (!diagAudioContext) return;
    analyser.getByteTimeDomainData(samples);
    let sum = 0;
    for (let i = 0; i < samples.length; i++) {
      const centred = (samples[i] - 128) / 128;
      sum += centred * centred;
    }
    const level = Math.min(1, Math.sqrt(sum / samples.length) * 3);
    peak = Math.max(peak, level);
    if (bar) bar.style.width = Math.round(level * 100) + "%";

    if (Date.now() - startedAt >= DIAG_TEST_DURATION_MS) {
      stopMicrophoneTest();
      if (button) button.disabled = false;
      const heard = peak > 0.02;
      if (message) {
        message.textContent = heard
          ? "Microphone is working — sound was detected."
          : "The microphone opened but no sound was detected. Check it isn't muted or set to the wrong device.";
      }
      _setDiag("diag-last-result", heard ? "Microphone working" : "Opened, but silent",
               heard ? "text-ok" : "text-err");
      return;
    }
    diagLevelRaf = requestAnimationFrame(tick);
  }
  tick();
}

async function setVoiceInputEnabled(enabled) {
  const message = $("voice-input-message");
  try {
    const r = await API.post("/voice/input-enabled", { enabled });
    const toggle = $("voice-input-toggle");
    if (toggle) toggle.checked = r.enabled;  // trust the server, not the click
    if (message) {
      message.textContent = r.enabled ? r.reason : "Push-to-talk is switched off.";
      message.className = `text-xs mt-2 ${r.available ? "text-ok" : "text-muted"}`;
    }
    refreshVoiceInputStatus();
    refreshVoiceDiagnostics();
  } catch (e) {
    if (message) {
      message.textContent = "Could not change the setting.";
      message.className = "text-xs mt-2 text-err";
    }
  }
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
    // The panels above report whether push-to-talk is usable. A finished
    // install has to update them, or they keep saying the model is
    // missing.
    refreshVoiceInputStatus();
    refreshVoiceDiagnostics();
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

// ── Home / Overview ─────────────────────────────────────────────────────────
// Every panel here has an explicit empty state and an explicit failure
// state. A dashboard that silently shows nothing is indistinguishable
// from one that is broken, which is the failure mode this avoids.

function _setOverviewValue(id, text, tone) {
  const el = $(id);
  if (!el) return;
  el.textContent = text;
  el.className = `metric-card-value${tone ? " " + tone : ""}`;
}

async function refreshOverviewProvider() {
  try {
    const data = await API.get("/providers");
    const available = (data.providers || []).filter(p => p.available);
    if (available.length) {
      _setOverviewValue("dash-provider", available.map(p => p.display_name).join(", "), "text-ok");
      _setOverviewValue("dash-provider-sub", "");
    } else {
      _setOverviewValue("dash-provider", "Local only", "text-muted");
    }
    const sub = $("dash-provider-sub");
    if (sub) {
      sub.textContent = available.length
        ? "Natural-language chat is available."
        : "Commands work; add a provider in Settings for conversational replies.";
    }
  } catch (e) {
    _setOverviewValue("dash-provider", "Unavailable", "text-err");
  }
}

async function refreshOverviewVoice() {
  try {
    const r = await API.get("/voice/stt-status");
    _setOverviewValue("dash-voice-input", r.available ? "Ready" : "Not set up", r.available ? "text-ok" : "text-muted");
    const sub = $("dash-voice-input-sub");
    if (sub) sub.textContent = r.available ? "Push-to-talk only — never always listening." : r.reason;
  } catch (e) {
    _setOverviewValue("dash-voice-input", "Unavailable", "text-err");
  }
}

async function refreshOverviewPrivacy() {
  try {
    const r = await API.get("/privacy/status");
    _setOverviewValue("dash-privacy", r.active ? "On" : "Off", r.active ? "text-ok" : "text-muted");
  } catch (e) {
    _setOverviewValue("dash-privacy", "Unknown", "text-err");
  }
}

async function refreshOverviewApprovals() {
  const host = $("dash-pending-approvals");
  if (!host) return;
  host.textContent = "";
  try {
    const actions = await API.get("/actions/pending");
    const pending = Array.isArray(actions) ? actions : (actions.actions || []);
    if (!pending.length) {
      const p = document.createElement("p");
      p.className = "text-xs text-muted";
      p.textContent = "Nothing is waiting for approval.";
      host.appendChild(p);
      return;
    }
    pending.forEach(action => {
      const row = document.createElement("div");
      row.className = "status-row";
      const label = document.createElement("span");
      label.className = "status-row-label";
      label.textContent = action.tool_name || "Action";
      const value = document.createElement("span");
      value.className = "status-row-value text-warn";
      value.textContent = "Waiting for approval";
      row.appendChild(label);
      row.appendChild(value);
      host.appendChild(row);
    });
    const link = document.createElement("p");
    link.className = "text-xs mt-2";
    const a = document.createElement("a");
    a.href = "/ui/actions";
    a.textContent = `Review ${pending.length} pending action(s)`;
    link.appendChild(a);
    host.appendChild(link);
  } catch (e) {
    const p = document.createElement("p");
    p.className = "text-xs text-err";
    p.textContent = "Could not check pending approvals.";
    host.appendChild(p);
  }
}

async function refreshOverviewRecentActions() {
  const host = $("dash-recent-actions");
  if (!host) return;
  host.textContent = "";
  try {
    const logs = await API.get("/logs?limit=5");
    if (!logs.length) {
      const p = document.createElement("p");
      p.className = "text-xs text-muted";
      p.textContent = "No actions yet. Anything JARVIS does will appear here.";
      host.appendChild(p);
      return;
    }
    logs.forEach(entry => {
      const row = document.createElement("div");
      row.className = "status-row";
      const label = document.createElement("span");
      label.className = "status-row-label";
      label.textContent = entry.tool_name || entry.command || "Action";
      const value = document.createElement("span");
      const ok = (entry.status || "").toLowerCase() === "success";
      value.className = `status-row-value ${ok ? "text-ok" : "text-muted"}`;
      value.textContent = entry.status || "";
      row.appendChild(label);
      row.appendChild(value);
      host.appendChild(row);
    });
  } catch (e) {
    const p = document.createElement("p");
    p.className = "text-xs text-err";
    p.textContent = "Could not load recent actions.";
    host.appendChild(p);
  }
}

async function refreshOverviewRuntimeState() {
  // Seeded from the topbar's current label so the card is correct
  // immediately on load rather than waiting for the first WS event.
  const label = $("topbar-runtime-label");
  _setOverviewValue("dash-runtime-state", (label && label.textContent) ? label.textContent : "standby");
}

function refreshOverview() {
  refreshOverviewRuntimeState();
  refreshOverviewProvider();
  refreshOverviewVoice();
  refreshOverviewPrivacy();
  refreshOverviewApprovals();
  refreshOverviewRecentActions();
}

// ── Settings page ───────────────────────────────────────────────────────────

async function refreshSettingsProviders() {
  const host = $("settings-provider-list");
  if (!host) return;
  host.textContent = "";
  try {
    const data = await API.get("/providers");
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
    });
    populateProviderPicker(data);
  } catch (e) {
    const p = document.createElement("p");
    p.className = "text-xs text-err";
    p.textContent = "Could not reach the server.";
    host.appendChild(p);
  }
}

// Only providers actually detected are offerable — an unavailable one is
// listed as unselectable rather than hidden, so a user who expected it
// can see it was looked for and not found.
function populateProviderPicker(data) {
  const picker = $("settings-provider-select");
  if (!picker) return;

  picker.textContent = "";
  (data.providers || []).forEach(provider => {
    const option = document.createElement("option");
    option.value = provider.name;
    option.textContent = provider.available
      ? provider.display_name
      : `${provider.display_name} — not detected`;
    option.disabled = !provider.available;
    if (provider.name === data.selected) option.selected = true;
    picker.appendChild(option);
  });

  picker.onchange = () => syncOllamaModelPicker(data);
  syncOllamaModelPicker(data);
}

function syncOllamaModelPicker(data) {
  const picker = $("settings-provider-select");
  const models = $("settings-ollama-model");
  const label  = $("settings-ollama-model-label");
  if (!picker || !models || !label) return;

  const chosen = (data.providers || []).find(p => p.name === picker.value);
  const isLocal = !!chosen && chosen.kind === "local";
  models.hidden = !isLocal;
  label.hidden = !isLocal;
  if (!isLocal) return;

  models.textContent = "";
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = "Automatic (first installed model)";
  models.appendChild(auto);

  (chosen.models || []).forEach(name => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    if (name === data.selected_model) option.selected = true;
    models.appendChild(option);
  });
}

async function saveProviderSelection() {
  const picker = $("settings-provider-select");
  const models = $("settings-ollama-model");
  const message = $("settings-provider-message");
  if (!picker) return;

  const setMessage = (text, ok) => {
    if (!message) return;
    message.textContent = text;
    message.className = `text-xs mt-2 ${ok ? "text-ok" : "text-err"}`;
  };

  try {
    const body = { provider: picker.value, model: models && !models.hidden ? models.value : "" };
    const data = await API.post("/providers/select", body);
    const active = (data.providers || []).find(p => p.name === data.selected);
    setMessage(`Chat now uses ${active ? active.display_name : data.selected}.`, true);
    populateProviderPicker(data);
  } catch (e) {
    // The server refuses a provider it could not detect, and says why —
    // that reason is more useful than a generic failure.
    setMessage(e.message, false);
  }
}

async function refreshSettingsKeyStatus() {
  const el = $("settings-key-status");
  if (!el) return;
  try {
    const r = await API.get("/settings/api-key-status");
    el.textContent = r.configured ? "Configured" : "Not configured";
    el.className = `status-row-value ${r.configured ? "text-ok" : "text-muted"}`;
  } catch (e) {
    el.textContent = "Unknown";
    el.className = "status-row-value";
  }
}

async function refreshSettingsStartup() {
  const toggle = $("settings-startup-toggle");
  const detail = $("settings-startup-detail");
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

async function refreshSettingsPrivacy() {
  const el = $("settings-privacy-status");
  const toggle = $("settings-privacy-toggle");
  if (!el && !toggle) return;
  try {
    const r = await API.get("/privacy/status");
    if (el) {
      el.textContent = r.active ? "On" : "Off";
      el.className = r.active ? "text-ok" : "text-muted";
    }
    if (toggle) toggle.checked = r.active;   // trust the server, not local state
  } catch (e) {
    if (el) { el.textContent = "Unknown"; el.className = "text-muted"; }
  }
}

// Toggling goes through POST /command, not a dedicated write endpoint:
// that path is already protected, already writes an audit record, and
// already publishes the WebSocket event the topbar indicator listens
// for. A second way to change the same state would have to reproduce
// all three — see app/api/routes.py's own note on this.
async function setPrivacyMode(active) {
  const toggle = $("settings-privacy-toggle");
  try {
    await API.post("/command", { command: active ? "privacy mode on" : "privacy mode off" });
  } catch (e) {
    if (toggle) toggle.checked = !active;   // never show a state that isn't real
  }
  await refreshSettingsPrivacy();
  refreshPrivacyIndicator();
  await applyPrivacyToClap();
}

async function refreshStoredData() {
  const host = $("settings-stored-data");
  if (!host) return;
  host.textContent = "";
  try {
    const data = await API.get("/privacy/data");
    (data.items || []).forEach(item => {
      const row = document.createElement("div");
      row.className = "status-row";

      const label = document.createElement("span");
      label.className = "status-row-label";
      label.textContent = item.label;

      const value = document.createElement("span");
      value.className = "status-row-value";
      value.textContent = String(item.count);

      row.appendChild(label);
      row.appendChild(value);
      host.appendChild(row);

      const detail = document.createElement("p");
      detail.className = "text-xs text-muted mt-2";
      detail.textContent = item.detail;
      host.appendChild(detail);
    });

    const note = document.createElement("p");
    note.className = "text-xs text-muted mt-2";
    // Stated rather than omitted: "your data stays local" is not the
    // same claim as "your data is protected".
    note.textContent = data.encrypted
      ? "Stored encrypted."
      : "Stored unencrypted in a plain database file on this computer.";
    host.appendChild(note);
  } catch (e) {
    const p = document.createElement("p");
    p.className = "text-xs text-err";
    p.textContent = "Could not read what is stored.";
    host.appendChild(p);
  }
}

async function refreshSettingsPaths() {
  const host = $("settings-paths");
  if (!host) return;
  try {
    const data = await API.get("/diagnostics");
    const locations = (data.sections || []).find(s => s.title === "Locations");
    host.textContent = "";
    if (!locations) return;
    locations.items.forEach(item => {
      const row = document.createElement("div");
      row.className = "status-row";
      const label = document.createElement("span");
      label.className = "status-row-label";
      label.textContent = item.label;
      const value = document.createElement("span");
      value.className = "status-row-value font-mono text-xs";
      value.textContent = item.value;
      row.appendChild(label);
      row.appendChild(value);
      host.appendChild(row);
    });
  } catch (e) {
    host.textContent = "";
    const p = document.createElement("p");
    p.className = "text-xs text-err";
    p.textContent = "Could not load locations.";
    host.appendChild(p);
  }
}

function _setSettingsKeyMessage(text, ok) {
  const el = $("settings-key-message");
  if (!el) return;
  el.textContent = text;
  el.className = `text-xs mt-2 ${ok ? "text-ok" : "text-err"}`;
}

// ── Local AI ─────────────────────────────────────────────────────────────────
//
// Ten states, ten next steps. The buttons that appear are the ones that
// apply right now — offering "Start Ollama" when nothing is installed is
// how the old single-message version wasted people's time.
//
// The consent panel is shown before a download, never during or after:
// its whole job is to be read while there is still a decision to make.

const LOCAL_AI_POLL_MS = 1500;
let localAIPoll = null;

function renderLocalAIPlan(plan) {
  setText("plan-what", "Ollama, then the " + plan.model + " model (" + plan.model_download + ")");
  setText("plan-url", plan.url);
  setText("plan-publisher", plan.publisher);
  setText("plan-licence", plan.licence);
  setText("plan-size", plan.approximate_size + " for Ollama, " + plan.model_download + " for the model");
  setText("plan-verification", plan.verification);
  setText("plan-ownership", plan.installs);

  const disk = $("plan-disk");
  if (disk) {
    if (plan.enough_disk === false) {
      disk.textContent = "There may not be enough free disk space for this. " + plan.hardware;
      disk.className = "text-xs mt-2 text-err";
    } else if (plan.enough_disk === null || plan.enough_disk === undefined) {
      disk.textContent = "Free disk space could not be read on this computer. " + plan.hardware;
      disk.className = "text-xs mt-2 text-warn";
    } else {
      disk.textContent = plan.hardware;
      disk.className = "text-xs mt-2 text-muted";
    }
  }
}

async function refreshLocalAIPlan() {
  const panel = $("local-ai-plan");
  if (!panel) return;
  try {
    renderLocalAIPlan(await API.get("/local-ai/plan"));
  } catch (e) {
    console.warn("local ai plan unavailable", e);
  }
}

function renderLocalAI(data) {
  setText("local-ai-headline", data.headline);
  const headline = $("local-ai-headline");
  if (headline) headline.className = "status-row-value " + (data.usable ? "text-ok" : "text-warn");

  setText("local-ai-detail", data.detail);
  setText("local-ai-next", data.next_step);
  setText("local-ai-hardware", data.hardware || "—");
  setText("local-ai-recommended", data.recommended_model + " (" + data.recommended_download + ")");

  let why = data.recommended_why;
  if (data.memory_gb) why += " This computer has about " + data.memory_gb + " GB of memory.";
  setText("local-ai-why", why);

  const needsSetup = data.status === "not_installed";
  const needsModel = data.status === "running_no_models";
  const failed = data.status === "failed";

  const show = (id, visible) => { const el = $(id); if (el) el.hidden = !visible; };
  show("local-ai-plan", (needsSetup || needsModel) && !data.busy);
  show("local-ai-setup", needsSetup && !data.busy);
  show("local-ai-pull", needsModel && !data.busy);
  show("local-ai-start", data.can_start && !data.busy);
  show("local-ai-retry", failed);
  show("local-ai-cancel", data.busy);
  show("local-ai-test", data.usable && !data.busy);
  show("local-ai-progress-wrap", data.busy);
  show("local-ai-download", !data.installed && !data.busy);

  const download = $("local-ai-download");
  if (download) download.href = data.download_url;

  const bar = $("local-ai-progress-bar");
  if (bar) bar.style.width = (data.percent || 0) + "%";
  setText("local-ai-progress-text", data.busy ? data.detail : "");

  // A five-minute download with no visible state is indistinguishable
  // from a hang, so the page follows it rather than waiting to be asked.
  if (data.busy && !localAIPoll) {
    localAIPoll = setInterval(refreshLocalAI, LOCAL_AI_POLL_MS);
  } else if (!data.busy && localAIPoll) {
    clearInterval(localAIPoll);
    localAIPoll = null;
  }
}

async function refreshLocalAI() {
  try {
    renderLocalAI(await API.get("/local-ai/status"));
  } catch (e) {
    console.error("local ai status error", e);
  }
}

// Downloading and installing somebody else's software. The plan panel is
// on screen and has already said what this fetches, from where, and how
// it is checked before it runs.
async function setUpLocalAI() {
  const button = $("local-ai-setup");
  if (button) button.disabled = true;
  _setLocalAIMessage("Starting…", "text-muted");
  try {
    const r = await API.post("/local-ai/install", {});
    _setLocalAIMessage(r.message, r.started ? "text-ok" : "text-warn");
    renderLocalAI(r.status);
  } catch (e) {
    _setLocalAIMessage("Local AI setup could not start. " + e.message, "text-err");
  } finally {
    if (button) button.disabled = false;
  }
}

async function pullLocalAIModel() {
  const button = $("local-ai-pull");
  if (button) button.disabled = true;
  _setLocalAIMessage("Starting the download…", "text-muted");
  try {
    const r = await API.post("/local-ai/pull", { model: "" });
    _setLocalAIMessage(r.message, r.started ? "text-ok" : "text-warn");
    renderLocalAI(r.status);
  } catch (e) {
    _setLocalAIMessage("The download could not start. " + e.message, "text-err");
  } finally {
    if (button) button.disabled = false;
  }
}

// One Cancel button for whichever job is running: the user is cancelling
// "this", not choosing between two internal endpoints.
async function cancelLocalAI() {
  _setLocalAIMessage("Stopping…", "text-muted");
  try {
    await API.post("/local-ai/install/cancel", {});
    await API.post("/local-ai/pull/cancel", {});
  } catch (e) {
    _setLocalAIMessage("Could not stop it: " + e.message, "text-err");
  }
  await refreshLocalAI();
}

// Retry after a failure. Which job failed decides what "again" means.
async function retryLocalAI() {
  const status = await API.get("/local-ai/status");
  if (status.installed) {
    await pullLocalAIModel();
  } else {
    await setUpLocalAI();
  }
}

function _setLocalAIMessage(text, kind) {
  const el = $("local-ai-message");
  if (!el) return;
  el.textContent = text;
  el.className = "text-xs mt-2 " + (kind || "text-muted");
}

async function startLocalAI() {
  const button = $("local-ai-start");
  if (button) button.disabled = true;
  _setLocalAIMessage("Starting Ollama…", "text-muted");
  try {
    const r = await API.post("/local-ai/start", {});
    _setLocalAIMessage(r.message, r.started ? "text-ok" : "text-warn");
    renderLocalAI(r.status);
  } catch (e) {
    _setLocalAIMessage("Ollama could not be started. " + e.message, "text-err");
  } finally {
    if (button) button.disabled = false;
  }
}

async function testLocalAI() {
  const button = $("local-ai-test");
  if (button) button.disabled = true;
  // Honest about the wait: the first answer after installing a model can
  // take a while, and a silent button looks broken.
  _setLocalAIMessage("Asking the model to answer… the first one can be slow.", "text-muted");
  try {
    const r = await API.post("/local-ai/verify", {});
    _setLocalAIMessage(r.message, r.ok ? "text-ok" : "text-warn");
    await refreshLocalAI();
  } catch (e) {
    _setLocalAIMessage("The test could not be completed. " + e.message, "text-err");
  } finally {
    if (button) button.disabled = false;
  }
}

function initSettings() {
  const localStart = $("local-ai-start");
  const localTest = $("local-ai-test");
  const localRefresh = $("local-ai-refresh");
  const localSetup = $("local-ai-setup");
  const localPull = $("local-ai-pull");
  const localCancel = $("local-ai-cancel");
  const localRetry = $("local-ai-retry");
  if (localStart) localStart.addEventListener("click", startLocalAI);
  if (localTest) localTest.addEventListener("click", testLocalAI);
  if (localRefresh) localRefresh.addEventListener("click", refreshLocalAI);
  if (localSetup) localSetup.addEventListener("click", setUpLocalAI);
  if (localPull) localPull.addEventListener("click", pullLocalAIModel);
  if (localCancel) localCancel.addEventListener("click", cancelLocalAI);
  if (localRetry) localRetry.addEventListener("click", retryLocalAI);
  refreshLocalAIPlan();
  refreshLocalAI();

  refreshSettingsProviders();
  refreshSettingsKeyStatus();
  refreshSettingsStartup();
  refreshSettingsCloseAction();
  loadPreferredNameInto("settings-name-input");
  refreshSettingsPrivacy();
  refreshSettingsPaths();
  refreshStoredData();

  const saveBtn = $("settings-key-save");
  const removeBtn = $("settings-key-remove");
  const input = $("settings-key-input");
  const startup = $("settings-startup-toggle");
  const providerSave = $("settings-provider-save");
  const privacyToggle = $("settings-privacy-toggle");

  if (privacyToggle) privacyToggle.addEventListener("change", () => setPrivacyMode(privacyToggle.checked));

  if (providerSave) providerSave.addEventListener("click", saveProviderSelection);

  // The same verified save the first-run screen uses, so a key rejected
  // during setup and a key rejected in Settings say the same thing.
  if (saveBtn) saveBtn.addEventListener("click", async () => {
    await saveApiKeyFrom("settings-key-input", "settings-key-save", _setSettingsKeyMessage);
    refreshSettingsKeyStatus();
    refreshSettingsProviders();
  });

  if (removeBtn) removeBtn.addEventListener("click", async () => {
    removeBtn.disabled = true;
    try {
      const r = await API.post("/settings/api-key/remove", {});
      _setSettingsKeyMessage(r.message, r.success);
      refreshSettingsKeyStatus();
      refreshSettingsProviders();
    } catch (e) {
      _setSettingsKeyMessage("Could not reach the server.", false);
    } finally {
      removeBtn.disabled = false;
    }
  });

  if (startup) startup.addEventListener("change", async () => {
    const detail = $("settings-startup-detail");
    try {
      const r = await API.post("/settings/startup", { enabled: startup.checked });
      startup.checked = r.enabled;  // trust the server, not the click
      if (detail) detail.textContent = r.detail;
    } catch (e) {
      if (detail) detail.textContent = "Could not change the setting.";
      refreshSettingsStartup();
    }
  });

  const nameSave = $("settings-name-save");
  if (nameSave) nameSave.addEventListener("click", async () => {
    const message = $("settings-name-message");
    const ok = await savePreferredName("settings-name-input");
    if (message) {
      message.textContent = ok ? "Saved." : "Could not save that right now.";
      message.className = `text-xs mt-2 ${ok ? "text-ok" : "text-err"}`;
    }
  });

  const closeAction = $("settings-close-action");
  if (closeAction) closeAction.addEventListener("change", async () => {
    const detail = $("settings-close-action-detail");
    try {
      const r = await API.post("/settings/close-action", { close_action: closeAction.value });
      closeAction.value = r.close_action;  // trust the server, not the click
      if (detail) detail.textContent = r.detail;
    } catch (e) {
      if (detail) detail.textContent = "Could not change the setting.";
      refreshSettingsCloseAction();
    }
  });
}

async function refreshSettingsCloseAction() {
  const select = $("settings-close-action");
  const detail = $("settings-close-action-detail");
  if (!select) return;
  try {
    const r = await API.get("/settings/close-action");
    select.value = r.close_action;
    if (detail) detail.textContent = r.detail;
  } catch (e) {
    if (detail) detail.textContent = "Could not read the current setting.";
  }
}

// ── Diagnostics page ────────────────────────────────────────────────────────

let diagnosticsReportText = "";

async function refreshDiagnostics() {
  const host = $("diagnostics-sections");
  if (!host) return;
  try {
    const data = await API.get("/diagnostics");
    diagnosticsReportText = data.text || "";
    host.textContent = "";
    (data.sections || []).forEach(section => {
      const card = document.createElement("div");
      card.className = "card mb-4";

      const title = document.createElement("div");
      title.className = "card-title";
      title.textContent = section.title;
      card.appendChild(title);

      const body = document.createElement("div");
      body.className = "mt-3";
      section.items.forEach(item => {
        const row = document.createElement("div");
        row.className = "status-row";
        const label = document.createElement("span");
        label.className = "status-row-label";
        label.textContent = item.label;
        const value = document.createElement("span");
        value.className = "status-row-value font-mono text-xs";
        value.textContent = item.value;
        row.appendChild(label);
        row.appendChild(value);
        body.appendChild(row);
      });
      card.appendChild(body);
      host.appendChild(card);
    });
  } catch (e) {
    host.textContent = "";
    const card = document.createElement("div");
    card.className = "card";
    const p = document.createElement("p");
    p.className = "text-xs text-err";
    p.textContent = "Could not collect diagnostics. Is JARVIS still running?";
    card.appendChild(p);
    host.appendChild(card);
  }
}

async function refreshAbout() {
  const version = $("about-version");
  const build = $("about-build");
  if (!version && !build) return;
  try {
    const r = await API.get("/about");
    if (version) { version.textContent = r.version; version.className = "status-row-value font-mono"; }
    if (build) {
      build.textContent = r.packaged ? `${r.build} (installed)` : `${r.build} (running from source)`;
      build.className = "status-row-value";
    }
  } catch (e) {
    if (version) { version.textContent = "Unknown"; version.className = "status-row-value"; }
    if (build) { build.textContent = "Unknown"; build.className = "status-row-value"; }
  }
}

// Opens the releases page through the existing safe URL opener, so it
// goes to the system browser (correct inside the native desktop window)
// and through the same scheme validation as any other link JARVIS opens.
async function checkForUpdates() {
  const message = $("about-update-message");
  const setMessage = (text, ok) => {
    if (!message) return;
    message.textContent = text;
    message.className = `text-xs mt-2 ${ok ? "text-muted" : "text-err"}`;
  };

  try {
    const about = await API.get("/about");
    await API.post("/command", { command: `open website ${about.releases_url}` });
    setMessage(`Opened the releases page. You have ${about.version}.`, true);
  } catch (e) {
    setMessage("Could not open the releases page: " + e.message, false);
  }
}

async function refreshNotices() {
  const target = $("about-notices");
  if (!target) return;
  try {
    const r = await API.get("/about/notices");
    target.textContent = r.available
      ? r.text
      : "The notices file could not be found in this installation.";
  } catch (e) {
    target.textContent = "The notices could not be loaded.";
  }
}

function initDiagnostics() {
  refreshDiagnostics();
  refreshAbout();
  refreshNotices();

  const updateBtn = $("about-check-updates");
  if (updateBtn) updateBtn.addEventListener("click", checkForUpdates);

  const copyBtn = $("diagnostics-copy");
  const refreshBtn = $("diagnostics-refresh");
  const message = $("diagnostics-copy-message");

  if (refreshBtn) refreshBtn.addEventListener("click", refreshDiagnostics);

  if (copyBtn) copyBtn.addEventListener("click", async () => {
    if (!diagnosticsReportText) { return; }
    try {
      await navigator.clipboard.writeText(diagnosticsReportText);
      if (message) { message.textContent = "Report copied to the clipboard."; message.className = "text-xs mt-2 text-ok"; }
    } catch (e) {
      // Clipboard access can be denied; say so rather than silently failing.
      if (message) { message.textContent = "Could not copy automatically — select the text above instead."; message.className = "text-xs mt-2 text-err"; }
    }
  });
}

// ── First run ───────────────────────────────────────────────────────────────
// Two questions and a button.
//
// This replaced a six-step wizard (welcome, privacy, provider discovery,
// speech-model install, preferences, readiness summary). Real hardware
// testing found it exposed setup work a normal user should never see, and
// most of it duplicated Settings — which is where all of it now lives.
// Nothing here blocks getting started: both fields are optional and "Skip
// for now" is a first-class choice, not a warning.

function _setSetupKeyMessage(text, ok) {
  const el = $("setup-key-message");
  if (!el) return;
  el.textContent = text;
  el.className = `text-xs mt-2 ${ok ? "text-ok" : "text-err"}`;
}

async function refreshSetupKeyStatus() {
  const el = $("setup-key-status");
  if (!el) return;
  try {
    const r = await API.get("/settings/api-key-status");
    el.textContent = r.configured ? "Configured" : "Not configured yet";
    el.className = `status-row-value ${r.configured ? "text-ok" : "text-muted"}`;
  } catch (e) {
    el.textContent = "Unknown";
    el.className = "status-row-value";
  }
}

async function loadPreferredNameInto(inputId) {
  const input = $(inputId);
  if (!input) return;
  try {
    const r = await API.get("/settings/preferred-name");
    input.value = r.name || "";
  } catch (e) {
    // A name we could not read is not worth interrupting anyone over.
  }
}

async function savePreferredName(inputId) {
  const input = $(inputId);
  if (!input) return true;
  try {
    const r = await API.post("/settings/preferred-name", { name: input.value.trim() });
    // Show what was actually stored — the server sanitises it, and a
    // field that keeps showing something different would be a lie.
    input.value = r.name || "";
    return true;
  } catch (e) {
    return false;
  }
}

// Saving the key is deliberately a real round trip that verifies it, so
// this can take a couple of seconds. The button says so rather than
// looking frozen.
async function saveApiKeyFrom(inputId, buttonId, setMessage) {
  const input = $(inputId);
  const button = $(buttonId);
  const value = input ? input.value.trim() : "";
  if (!value) {
    setMessage("Enter a key first.", false);
    return false;
  }

  const originalLabel = button ? button.textContent : "";
  if (button) { button.disabled = true; button.textContent = "Checking…"; }
  try {
    const r = await API.post("/settings/api-key", { api_key: value });
    setMessage(r.message, r.success);
    // Cleared only when it was actually stored: leaving a rejected key in
    // the box is what lets someone fix a typo instead of retyping it.
    if (r.stored && input) input.value = "";
    return r.success;
  } catch (e) {
    setMessage("Could not reach JARVIS's local service.", false);
    return false;
  } finally {
    if (button) { button.disabled = false; button.textContent = originalLabel; }
  }
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
  refreshSetupKeyStatus();
  loadPreferredNameInto("setup-name-input");

  const continueBtn = $("setup-continue");
  const skipBtn = $("setup-skip");
  const keyInput = $("setup-key-input");

  if (skipBtn) skipBtn.addEventListener("click", finishSetup);

  if (continueBtn) continueBtn.addEventListener("click", async () => {
    await savePreferredName("setup-name-input");

    const typedKey = keyInput ? keyInput.value.trim() : "";
    if (typedKey) {
      const ok = await saveApiKeyFrom("setup-key-input", "setup-continue", _setSetupKeyMessage);
      await refreshSetupKeyStatus();
      // A rejected key keeps the user here, where they can fix it — but
      // only a rejection does. Being offline or rate-limited during setup
      // still stores the key and lets them through.
      const status = $("setup-key-status");
      const stillUnconfigured = status && status.textContent === "Not configured yet";
      if (!ok && stillUnconfigured) return;
    }
    finishSetup();
  });

  if (keyInput) keyInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && continueBtn) continueBtn.click();
  });
}

// ── JARVIS's voice: engines, installation, pronunciation ─────────────────────
//
// Every string that reaches the page goes in with textContent. None of
// this builds markup from server data.

let nvInstallTimer = null;

function nvFormatMB(bytes) {
  return (bytes / (1024 * 1024)).toFixed(0) + " MB";
}

function nvRenderEngines(data) {
  const list = $("nv-engine-list");
  if (!list) return;
  _clearEl(list);

  data.engines.forEach((engine) => {
    const row = document.createElement("div");
    row.className = "status-row";

    const label = document.createElement("span");
    label.className = "status-row-label";
    label.textContent = engine.name;

    const value = document.createElement("span");
    value.className = "status-row-value";
    // The mark says which one is speaking; the detail says why the
    // others are not. Both matter — a list of ticks and crosses with no
    // reasons is what made the reported failure impossible to act on.
    const badge = document.createElement("span");
    badge.className = "badge " + (engine.active ? "badge-ok" : engine.available ? "badge-info" : "badge-warn");
    badge.textContent = engine.active ? "in use" : engine.available ? "ready" : "unavailable";

    const detail = document.createElement("div");
    detail.className = "text-xs text-muted";
    detail.textContent = engine.detail;

    value.appendChild(badge);
    row.appendChild(label);
    row.appendChild(value);
    list.appendChild(row);
    list.appendChild(detail);
  });
}

function nvRenderVoices(data) {
  const select = $("nv-voice-select");
  if (!select) return;
  _clearEl(select);

  data.voices.forEach((voice) => {
    const option = document.createElement("option");
    option.value = voice.key;
    option.textContent = voice.installed
      ? voice.display_name
      : `${voice.display_name} — not installed`;
    if (voice.key === data.voice_key) option.selected = true;
    select.appendChild(option);
  });

  const chosen = data.voices.find((voice) => voice.key === data.voice_key);
  setText("nv-voice-description", chosen ? chosen.description : "");
}

async function refreshNeuralVoice() {
  try {
    const data = await API.get("/voice/engine-status");

    setText("nv-active-engine", data.active_engine_name);
    const active = $("nv-active-engine");
    if (active) active.className = "status-row-value " + (data.available ? "text-ok" : "text-warn");

    nvRenderEngines(data);
    nvRenderVoices(data);

    const speed = $("nv-speed");
    if (speed) speed.value = data.speed.toFixed(2);

    const installCard = $("nv-install-card");
    if (installCard) installCard.hidden = data.model_installed;
    if (!data.model_installed) await refreshVoiceInstallPreview(data.voice_key);
  } catch (e) {
    console.error("voice engine status error", e);
  }
}

async function refreshVoiceInstallPreview(voiceKey) {
  try {
    const info = await API.get("/voice/install-preview?voice_key=" + encodeURIComponent(voiceKey));
    setText("nv-install-size", nvFormatMB(info.download_bytes));
    setText("nv-install-source", info.source);
    setText("nv-install-licence", info.licence);
    setText("nv-install-destination", info.destination);
  } catch (e) {
    console.error("voice install preview error", e);
  }
}

function nvApplyInstallState(state) {
  const wrap = $("nv-progress-wrap");
  const bar = $("nv-progress-bar");
  const text = $("nv-progress-text");
  const startBtn = $("nv-install-start");
  const cancelBtn = $("nv-install-cancel");
  const retryBtn = $("nv-install-retry");

  const busy = state.running || state.status === "downloading" ||
               state.status === "verifying" || state.status === "installing";

  if (wrap) wrap.hidden = !busy && state.status !== "error";
  if (bar) bar.style.width = state.percent + "%";
  if (startBtn) startBtn.hidden = busy;
  if (cancelBtn) cancelBtn.hidden = !busy;
  if (retryBtn) retryBtn.hidden = state.status !== "error";

  if (text) {
    if (busy && state.bytes_total > 0) {
      text.textContent = `${state.status} ${state.current_file} — ` +
        `${nvFormatMB(state.bytes_downloaded)} of ${nvFormatMB(state.bytes_total)} (${state.percent}%)`;
    } else {
      text.textContent = state.message || "";
    }
    text.className = "text-xs mt-2 " + (state.status === "error" ? "text-err" : "text-muted");
  }

  return busy;
}

async function pollVoiceInstallStatus() {
  try {
    const state = await API.get("/voice/install-status");
    const busy = nvApplyInstallState(state);

    if (busy) {
      if (nvInstallTimer) clearTimeout(nvInstallTimer);
      nvInstallTimer = setTimeout(pollVoiceInstallStatus, 500);
    } else {
      if (nvInstallTimer) { clearTimeout(nvInstallTimer); nvInstallTimer = null; }
      if (state.status === "complete") await refreshNeuralVoice();
    }
  } catch (e) {
    console.error("voice install status error", e);
  }
}

async function startVoiceInstall() {
  const select = $("nv-voice-select");
  try {
    const state = await API.post("/voice/install", { voice_key: select ? select.value : undefined });
    nvApplyInstallState(state);
    pollVoiceInstallStatus();
  } catch (e) {
    setText("nv-message", "The voice could not be installed. " + e.message);
  }
}

async function cancelVoiceInstall() {
  try {
    nvApplyInstallState(await API.post("/voice/install/cancel", {}));
  } catch (e) {
    console.error("voice install cancel error", e);
  }
}

async function testNeuralVoice() {
  suspendForSpeech();
  const select = $("nv-voice-select");
  const message = $("nv-message");
  try {
    const r = await API.post("/voice/test", { voice_key: select ? select.value : undefined });
    if (message) {
      message.textContent = r.success ? "Speaking a sample…" : r.message;
      message.className = "text-xs mt-2 " + (r.success ? "text-muted" : "text-warn");
    }
  } catch (e) {
    if (message) {
      message.textContent = "The voice could not be tested. " + e.message;
      message.className = "text-xs mt-2 text-err";
    }
  }
}

async function selectNeuralVoice(key) {
  try {
    nvRenderVoices(await API.post("/voice/select", { voice_key: key }));
    await refreshNeuralVoice();
  } catch (e) {
    console.error("voice select error", e);
  }
}

async function setNeuralSpeed(value) {
  try {
    await API.post("/voice/speed", { speed: parseFloat(value) });
  } catch (e) {
    console.error("voice speed error", e);
  }
}

// ── Pronunciations ───────────────────────────────────────────────────────────

function nvRenderPronunciations(data) {
  const list = $("pron-list");
  if (list) {
    _clearEl(list);
    if (!data.entries.length) {
      const empty = document.createElement("p");
      empty.className = "text-xs text-muted";
      empty.textContent = "No custom pronunciations saved.";
      list.appendChild(empty);
    }
    data.entries.forEach((entry) => {
      const row = document.createElement("div");
      row.className = "status-row";

      const label = document.createElement("span");
      label.className = "status-row-label";
      label.textContent = entry.word;

      const value = document.createElement("span");
      value.className = "status-row-value";

      const said = document.createElement("span");
      said.className = "text-xs text-muted";
      said.textContent = entry.input;

      const remove = document.createElement("button");
      remove.className = "btn btn-ghost";
      remove.type = "button";
      remove.textContent = "Remove";
      remove.addEventListener("click", () => removePronunciation(entry.word));

      value.appendChild(said);
      value.appendChild(remove);
      row.appendChild(label);
      row.appendChild(value);
      list.appendChild(row);
    });
  }

  const prompt = $("nv-name-prompt");
  if (prompt) {
    prompt.hidden = !data.name_needs_pronunciation;
    if (data.name_needs_pronunciation) {
      // Offered, never guessed: proposing a pronunciation nobody asked
      // for would be inventing how someone's name sounds.
      prompt.textContent =
        `JARVIS does not know how to say “${data.preferred_name}”, so it will spell it out. ` +
        "You can teach it below.";
      prompt.className = "text-xs mt-2 text-warn";
    }
  }
}

async function refreshPronunciations() {
  try {
    nvRenderPronunciations(await API.get("/voice/pronunciations"));
  } catch (e) {
    console.error("pronunciations error", e);
  }
}

async function previewPronunciation() {
  suspendForSpeech();
  const word = $("pron-word");
  const spoken = $("pron-spoken");
  const message = $("pron-message");
  if (!word || !spoken) return;
  try {
    const r = await API.post("/voice/pronunciations/preview", {
      word: word.value, spoken_as: spoken.value,
    });
    if (message) {
      message.textContent = r.message;
      message.className = "text-xs mt-2 " + (r.success ? "text-muted" : "text-warn");
    }
    if (r.success) {
      await API.post("/voice/test", { text: word.value });
    }
  } catch (e) {
    if (message) {
      message.textContent = "That could not be checked. " + e.message;
      message.className = "text-xs mt-2 text-err";
    }
  }
}

async function savePronunciation() {
  const word = $("pron-word");
  const spoken = $("pron-spoken");
  const message = $("pron-message");
  if (!word || !spoken) return;
  try {
    const data = await API.post("/voice/pronunciations", {
      word: word.value, spoken_as: spoken.value,
    });
    const saved = data.entries.some((entry) => entry.word === word.value.trim().toLowerCase());
    if (message) {
      message.textContent = saved
        ? `JARVIS will say “${word.value.trim()}” that way.`
        : "That pronunciation could not be saved — try spelling it in syllables, like “dah-doh”.";
      message.className = "text-xs mt-2 " + (saved ? "text-ok" : "text-warn");
    }
    if (saved) { word.value = ""; spoken.value = ""; }
    nvRenderPronunciations(data);
  } catch (e) {
    if (message) {
      message.textContent = "That could not be saved. " + e.message;
      message.className = "text-xs mt-2 text-err";
    }
  }
}

async function removePronunciation(word) {
  try {
    nvRenderPronunciations(await API.post("/voice/pronunciations/remove", { word, spoken_as: "" }));
  } catch (e) {
    console.error("pronunciation remove error", e);
  }
}

// ── The optional cloud voice (ElevenLabs) ───────────────────────────────────
//
// The key is write-only from here: it can be typed, saved, replaced and
// deleted, and there is no endpoint that gives it back. Everything this
// code ever learns about it is a boolean.

let cloudVoices = [];

function _cloudMessage(text, tone) {
  const el = $("el-message");
  if (!el) return;
  el.textContent = text;
  el.className = `text-xs mt-2 ${tone || "text-muted"}`;
}

function renderCloudStatus(s) {
  if (!s || !$("cloud-voice-card")) return;

  _setDiag("el-detail", s.detail, s.blocked_by_privacy ? "text-err" : (s.key_configured ? "text-ok" : ""));
  _setDiag("el-key-state", s.key_configured ? "Saved in the Windows Credential Manager" : "Not set",
           s.key_configured ? "text-ok" : "");
  _setDiag("el-voice-state", s.voice_name || s.voice_id || "None chosen", s.voice_id ? "text-ok" : "");

  const engine = $("el-engine");
  if (engine) engine.value = s.selected ? "elevenlabs" : "";

  const idLine = $("el-voice-id");
  if (idLine) idLine.textContent = s.voice_id ? `Voice ID ${s.voice_id}` : "";

  const settings = s.settings || {};
  _setSlider("el-stability", settings.stability);
  _setSlider("el-similarity", settings.similarity_boost);
  _setSlider("el-style", settings.style);
  _setSlider("el-speed", settings.speed);
  const boost = $("el-boost");
  if (boost) boost.checked = settings.use_speaker_boost !== false;
  const fallback = $("el-fallback");
  if (fallback) fallback.checked = !!s.fallback_allowed;

  const phrase = $("el-test-phrase");
  if (phrase) phrase.textContent = `Test phrase: “${s.test_phrase}”`;

  const note = $("el-fallback-note");
  if (note) {
    note.textContent = s.last_fallback
      ? `Last time the cloud voice could not speak: ${s.last_fallback}`
      : "";
  }

  // A key with no voice chosen cannot speak, and neither can privacy mode.
  const ready = s.key_configured && !!s.voice_id && !s.blocked_by_privacy;
  const testBtn = $("el-test");
  if (testBtn) testBtn.disabled = !ready;
}

function _setSlider(id, value) {
  const el = $(id);
  if (!el || typeof value !== "number") return;
  el.value = String(value);
  const label = $(id + "-value");
  if (label) label.textContent = value.toFixed(2);
}

function _sliderValue(id) {
  const el = $(id);
  return el ? Number(el.value) : undefined;
}

async function refreshCloudVoice() {
  if (!$("cloud-voice-card")) return;
  try {
    renderCloudStatus(await API.get("/voice/cloud"));
  } catch (e) {
    _cloudMessage("Could not read the cloud voice settings.", "text-err");
  }
}

async function _cloudAction(path, body, whenBusy) {
  _cloudMessage(whenBusy || "Working…", "text-muted");
  try {
    const r = await API.post(path, body || {});
    renderCloudStatus(r.status);
    _cloudMessage(r.message, r.success ? "text-ok" : "text-err");
    return r;
  } catch (e) {
    _cloudMessage("That did not work. " + (e.message || ""), "text-err");
    return null;
  }
}

async function saveCloudKey() {
  const field = $("el-key");
  const key = field ? field.value.trim() : "";
  if (!key) {
    _cloudMessage("Paste your ElevenLabs API key first.", "text-err");
    return;
  }
  const r = await _cloudAction("/voice/cloud/key", { api_key: key }, "Saving the key…");
  // Clear the field either way: a key left sitting in an input is a key
  // on screen, and it is never read back from the server to refill it.
  if (field) field.value = "";
  if (r && r.success) await refreshCloudVoices();
}

async function refreshCloudVoices() {
  const select = $("el-voice");
  if (!select) return;
  _cloudMessage("Asking ElevenLabs which voices this account can use…", "text-muted");
  try {
    const r = await API.post("/voice/cloud/voices", {});
    cloudVoices = r.voices || [];
    _clearEl(select);
    for (const voice of cloudVoices) {
      const option = document.createElement("option");
      option.value = voice.voice_id;
      option.textContent = voice.category ? `${voice.name} — ${voice.category}` : voice.name;
      select.appendChild(option);
    }
    _cloudMessage(r.message, r.success ? "text-ok" : "text-err");
  } catch (e) {
    _cloudMessage("Could not load the voice list. " + (e.message || ""), "text-err");
  }
}

async function saveCloudVoice() {
  const select = $("el-voice");
  if (!select || !select.value) {
    _cloudMessage("Load the voices and choose one first.", "text-err");
    return;
  }
  const chosen = cloudVoices.find(v => v.voice_id === select.value);
  await _cloudAction(
    "/voice/cloud/select-voice",
    { voice_id: select.value, voice_name: chosen ? chosen.name : "" },
    "Saving your choice…",
  );
}

function saveCloudSettings() {
  return _cloudAction("/voice/cloud/settings", {
    settings: {
      stability: _sliderValue("el-stability"),
      similarity_boost: _sliderValue("el-similarity"),
      style: _sliderValue("el-style"),
      speed: _sliderValue("el-speed"),
      use_speaker_boost: !!($("el-boost") && $("el-boost").checked),
    },
  }, "Saving…");
}

function initCloudVoice() {
  if (!$("cloud-voice-card")) return;

  const on = (id, event, handler) => {
    const el = $(id);
    if (el) el.addEventListener(event, handler);
  };

  on("el-key-save", "click", saveCloudKey);
  on("el-key-check", "click", () => _cloudAction("/voice/cloud/validate", {}, "Checking the key…"));
  on("el-key-delete", "click", () => _cloudAction("/voice/cloud/key/delete", {}, "Removing the key…"));
  on("el-voices-refresh", "click", refreshCloudVoices);
  on("el-voice-save", "click", saveCloudVoice);
  on("el-settings-save", "click", saveCloudSettings);
  on("el-settings-reset", "click", () => _cloudAction("/voice/cloud/settings/reset", {}, "Resetting…"));
  on("el-test", "click", () => {
    suspendForSpeech();
    return _cloudAction("/voice/cloud/test", {}, "Speaking the test phrase…");
  });
  on("el-stop", "click", voiceStop);
  on("el-engine", "change", () => _cloudAction("/voice/engine", { engine: $("el-engine").value }, "Switching…"));
  on("el-fallback", "change", () =>
    _cloudAction("/voice/cloud/fallback", { allowed: $("el-fallback").checked }, "Saving…"));

  // Live numbers beside each slider, so a value is readable before it is saved.
  for (const id of ["el-stability", "el-similarity", "el-style", "el-speed"]) {
    on(id, "input", () => {
      const label = $(id + "-value");
      if (label) label.textContent = Number($(id).value).toFixed(2);
    });
  }

  refreshCloudVoice();
}

// ── Double-clap activation ──────────────────────────────────────────────────
//
// The state machine lives in app/ui/static/clap-controller.js, which owns
// the microphone. This half is wiring: it tells the controller what the
// server says, tells the server what the controller is actually doing,
// and paints the result.
//
// It runs on every page rather than only the Voice page, because the
// point of the feature is to work while JARVIS is minimised — and it is
// only ever started when the stored setting says so, which is off until
// somebody turns it on.

// Suspension reasons. One per exclusive user of the microphone or the
// speakers, so two overlapping ones cannot resume the listener early.
const CLAP_REASON = {
  SPEAKING: "speaking",
  PTT: "push-to-talk",
  MIC_TEST: "microphone-test",
  CALIBRATING: "calibrating",
};

let clapLastReported = "";
let clapSafeBounds = {};
let clapProposed = null;

function clapListening() {
  return !!(window.ClapController && ClapController.isListening());
}

function clapState() {
  return window.ClapController ? ClapController.state() : "disabled";
}

// Every exclusive audio operation goes through these two, never through
// a bare start/stop, so a missed release cannot leave the microphone off
// for good and a double release cannot turn it on early.
function clapSuspend(reason) {
  if (window.ClapController) ClapController.suspend(reason);
}

function clapResume(reason) {
  if (window.ClapController) ClapController.resume(reason);
}

// Runs an async operation with the listener suspended, releasing the
// reason on success, failure and cancellation alike.
async function withClapSuspended(reason, fn) {
  clapSuspend(reason);
  try {
    return await fn();
  } finally {
    clapResume(reason);
  }
}

// The tray asks the server what the listener is doing; the server only
// knows because of this.
//
// Sent on every change *and* re-sent on a timer, because
// app/voice/clap.py deliberately stops believing a report older than
// LISTENER_FRESH_SECONDS. That staleness is what stops a closed or
// crashed tab leaving a false "On" behind — but it also means a page
// that sits in one state has to keep saying so, or the tray would decay
// to "Microphone unavailable" while the microphone is plainly open. The
// interval is comfortably shorter than the window it has to stay inside.
const CLAP_HEARTBEAT_MS = 8000;
let clapHeartbeat = null;

async function sendClapListenerState(state) {
  clapLastReported = state;
  try {
    await API.post("/voice/clap/listener", { state });
  } catch (e) { /* the tray falls back to "unavailable" on its own */ }
}

function reportClapListenerState(snapshot) {
  if (snapshot.state !== clapLastReported) sendClapListenerState(snapshot.state);
  if (!clapHeartbeat) {
    clapHeartbeat = setInterval(() => sendClapListenerState(clapState()), CLAP_HEARTBEAT_MS);
  }
}

function stopClapHeartbeat() {
  if (clapHeartbeat) {
    clearInterval(clapHeartbeat);
    clapHeartbeat = null;
  }
}

function renderClapState() {
  const el = $("clap-state");
  if (!el) return;
  const labels = {
    "listening": "Listening for claps",
    "starting": "Starting…",
    "suspended": "Temporarily paused",
    "calibrating": "Calibrating",
    "privacy-blocked": "Paused by privacy mode",
    "microphone-unavailable": "Microphone unavailable",
    "stopping": "Stopping…",
    "error": "Error",
    "disabled": "Off",
  };
  const state = clapState();
  el.textContent = labels[state] || "Off";
  el.className = `status-row-value${state === "listening" ? " text-ok" : (
    state === "privacy-blocked" || state === "microphone-unavailable" || state === "error" ? " text-err" : "")}`;

  // What is actually open, which is not always what was chosen.
  const active = $("clap-active-device");
  if (active) {
    if (!clapListening()) {
      active.textContent = "";
    } else if (ClapController.usingFallback()) {
      active.textContent = "The chosen microphone was unavailable, so JARVIS is using the system default.";
      active.className = "text-xs mt-2 text-err";
    } else {
      active.textContent = "Using the selected microphone.";
      active.className = "text-xs mt-2 text-muted";
    }
  }
}

function onClapControllerChange(snapshot) {
  renderClapState();
  reportClapListenerState(snapshot);
}

async function onClapPair() {
  try {
    const r = await API.post("/voice/clap/activate", {});
    // The Voice page, if it happens to be the page that is open, says
    // what happened. Every other page stays silent — a window arriving
    // is the feedback.
    const message = $("clap-message");
    if (message) {
      message.textContent = r.message;
      message.className = `text-xs mt-2 ${r.accepted ? "text-ok" : "text-muted"}`;
    }
    if (!r.accepted && (r.reason === "disabled" || r.reason === "privacy_mode")) {
      // The server and this page disagree about the settings. The server
      // is right; re-read rather than guess.
      refreshClap(false);
    }
  } catch (e) {
    // A failed activation is not worth telling anyone about; the next
    // clap will try again.
  }
}

function renderClapStatus(s) {
  if (!s) return;
  const toggle = $("clap-toggle");
  if (toggle) toggle.checked = !!s.enabled;
  const sensitivity = $("clap-sensitivity");
  if (sensitivity) sensitivity.value = s.sensitivity;
  const greet = $("clap-greet");
  if (greet) greet.checked = !!s.greet;
  const greeting = $("clap-greeting");
  if (greeting && document.activeElement !== greeting) greeting.value = s.greeting || "";
  clapSafeBounds = s.safe_bounds || {};

  const calibrated = $("clap-calibrated");
  if (calibrated) {
    calibrated.textContent = s.calibrated
      ? "Calibrated for this room."
      : "Using the standard settings for the chosen sensitivity.";
  }
  const reset = $("clap-cal-reset");
  if (reset) reset.disabled = !s.calibrated;

  renderClapState();

  const message = $("clap-message");
  if (message && s.privacy_blocking && s.enabled) {
    message.textContent = "Privacy mode is on, so JARVIS is not listening for claps.";
    message.className = "text-xs mt-2 text-err";
  }
}

// Hands the server's settings to the controller, which decides whether
// that means start, stop, restart or nothing.
async function applyClapSetting(status, restart) {
  if (window.ClapController) {
    await ClapController.configure({
      enabled: status.enabled,
      privacyBlocked: status.privacy_blocking,
      detector: status.detector,
      deviceId: status.device_id || "",
      forceRestart: !!restart,
    });
  }
  renderClapStatus(status);
}

async function refreshClap(restart) {
  try {
    await applyClapSetting(await API.get("/voice/clap"), restart);
  } catch (e) { /* a status read that fails leaves the listener as it is */ }
}

// Privacy mode changed somewhere — Settings, a chat command, another
// tab, the tray. The microphone stops here and now; the settings re-read
// afterwards only decides whether anything should start again.
//
// This is the defect this pass exists to fix: the old code refreshed the
// privacy *indicator* on this event and left the microphone open.
async function applyPrivacyToClap() {
  let active = true;   // unreadable privacy state is treated as "on"
  try {
    const r = await API.get("/privacy/status");
    active = !!r.active;
  } catch (e) { /* keep the safe reading */ }
  if (window.ClapController) await ClapController.setPrivacyBlocked(active);
  if (!active) await refreshClap(false);
  renderClapState();
}

async function setClapEnabled(enabled) {
  const message = $("clap-message");
  try {
    const status = await API.post("/voice/clap/enabled", { enabled });
    await applyClapSetting(status, true);
    if (message && enabled && !clapListening() && !status.privacy_blocking) {
      message.textContent = "The microphone could not be opened, so nothing is listening. "
        + "Check the microphone permission in Voice diagnostics below.";
      message.className = "text-xs mt-2 text-err";
    } else if (message && !enabled) {
      message.textContent = "Switched off. Nothing is listening.";
      message.className = "text-xs mt-2 text-muted";
    }
  } catch (e) {
    if (message) {
      message.textContent = "Could not change the setting.";
      message.className = "text-xs mt-2 text-err";
    }
  }
}

async function saveClapSettings(restart, extra) {
  const sensitivity = $("clap-sensitivity");
  const greet = $("clap-greet");
  const greeting = $("clap-greeting");
  const body = {
    sensitivity: sensitivity ? sensitivity.value : null,
    greet: greet ? greet.checked : null,
    greeting: greeting ? greeting.value : null,
  };
  Object.assign(body, extra || {});
  try {
    const status = await API.post("/voice/clap/settings", body);
    await applyClapSetting(status, restart);
    return status;
  } catch (e) {
    return null;
  }
}

// The shared microphone choice. Saving it restarts the listener onto the
// new device — the controller stops the old stream before opening the
// new one, so there is never a moment with two.
async function setSharedMicrophone(deviceId) {
  await saveClapSettings(true, { device_id: deviceId || "" });
}

// ── Calibration ─────────────────────────────────────────────────────────────
//
// Bounded, explicitly started, and entirely local. The onset scalars the
// worklet reports during a session are read here and thrown away; nothing
// posts them anywhere, and a test asserts that.

function clapCalMessage(text, tone) {
  const el = $("clap-cal-message");
  if (!el) return;
  el.textContent = text;
  el.className = `text-xs mt-2 ${tone || "text-muted"}`;
}

function describeOnsets(onsets, detector) {
  // Turns two measured onsets into something a person can act on.
  const lines = [];
  if (!onsets.length) return ["No clap was detected. Try clapping harder, or closer to the microphone."];
  lines.push("First clap detected.");
  const first = onsets[0];
  if (first.peak < (detector.absMin || 0.035)) lines.push("It was very quiet.");
  if (first.peak > 0.95) lines.push("It was loud enough to clip — try a little further from the microphone.");
  if (onsets.length < 2) {
    lines.push("No second clap arrived in time. Clap twice, about a quarter of a second apart.");
    return lines;
  }
  const gap = onsets[1].gap;
  lines.push(`Second clap detected ${Math.round(gap * 1000)} ms later.`);
  if (gap < (detector.minGap || 0.12)) lines.push("That was faster than the current setting allows.");
  if (gap > (detector.maxGap || 0.7)) lines.push("That was slower than the current setting allows.");
  return lines;
}

function proposeTuning(onsets) {
  // Conservative: set the loudness floor below the quieter of the two
  // claps with margin, and widen the gap window around what was actually
  // clapped. The server clamps all of it to SAFE_BOUNDS regardless.
  const peaks = onsets.map(o => o.peak).filter(p => typeof p === "number" && p > 0);
  const gap = onsets.length >= 2 ? onsets[1].gap : null;
  const proposal = {};
  if (peaks.length) proposal.absMin = Math.max(0.008, Math.min(...peaks) * 0.45);
  if (gap && gap > 0) {
    proposal.minGap = Math.max(0.08, gap * 0.5);
    proposal.maxGap = Math.min(1.2, gap * 1.8);
  }
  return proposal;
}

function renderProposal(proposal) {
  const el = $("clap-cal-proposal");
  if (!el) return;
  if (!proposal || !Object.keys(proposal).length) {
    el.textContent = "";
    return;
  }
  const parts = [];
  if (proposal.absMin !== undefined) parts.push(`minimum loudness ${proposal.absMin.toFixed(3)}`);
  if (proposal.minGap !== undefined) parts.push(`fastest gap ${Math.round(proposal.minGap * 1000)} ms`);
  if (proposal.maxGap !== undefined) parts.push(`slowest gap ${Math.round(proposal.maxGap * 1000)} ms`);
  el.textContent = `Proposed: ${parts.join(", ")}. Nothing is saved until you press Save.`;
}

async function startClapCalibration() {
  if (!window.ClapController) return;
  const startBtn = $("clap-cal-start");
  const saveBtn = $("clap-cal-save");
  const cancelBtn = $("clap-cal-cancel");
  clapProposed = null;
  renderProposal(null);
  if (saveBtn) saveBtn.disabled = true;

  let detector = {};
  try {
    detector = (await API.get("/voice/clap")).detector || {};
  } catch (e) { /* fall back to whatever the worklet defaults to */ }

  clapSuspend(CLAP_REASON.CALIBRATING);
  const result = await ClapController.startCalibration({
    onOnset: (onsets) => {
      clapCalMessage(describeOnsets(onsets, detector).join(" "), "text-muted");
    },
    onFinish: (outcome, onsets) => {
      if (startBtn) startBtn.disabled = false;
      if (cancelBtn) cancelBtn.disabled = true;
      clapResume(CLAP_REASON.CALIBRATING);
      if (outcome === "cancelled") {
        clapCalMessage("Calibration cancelled. The microphone was released.", "text-muted");
        return;
      }
      if (outcome === "timeout" && onsets.length < 2) {
        clapCalMessage(describeOnsets(onsets, detector).join(" ")
          + " Calibration stopped and the microphone was released.", "text-err");
        return;
      }
      clapProposed = proposeTuning(onsets);
      clapCalMessage(describeOnsets(onsets, detector).join(" ") + " Double clap accepted.", "text-ok");
      renderProposal(clapProposed);
      if (saveBtn) saveBtn.disabled = !Object.keys(clapProposed).length;
    },
  });

  if (!result.ok) {
    clapResume(CLAP_REASON.CALIBRATING);
    if (startBtn) startBtn.disabled = false;
    clapCalMessage(result.reason === "privacy"
      ? "Privacy mode is on, so the microphone was not opened."
      : "The microphone could not be opened for calibration.", "text-err");
    return;
  }
  if (startBtn) startBtn.disabled = true;
  if (cancelBtn) cancelBtn.disabled = false;
  clapCalMessage("Listening — clap twice, about a quarter of a second apart.", "text-muted");
}

function cancelClapCalibration() {
  if (window.ClapController) ClapController.stopCalibration();
}

async function saveClapCalibration() {
  if (!clapProposed) return;
  const status = await saveClapSettings(true, { tuning: clapProposed });
  if (status) {
    clapProposed = null;
    renderProposal(null);
    const saveBtn = $("clap-cal-save");
    if (saveBtn) saveBtn.disabled = true;
    clapCalMessage("Saved. JARVIS is using the calibrated settings.", "text-ok");
  } else {
    clapCalMessage("Could not save the calibrated settings.", "text-err");
  }
}

async function resetClapCalibration() {
  const status = await saveClapSettings(true, { tuning: {} });
  clapProposed = null;
  renderProposal(null);
  const saveBtn = $("clap-cal-save");
  if (saveBtn) saveBtn.disabled = true;
  clapCalMessage(status ? "Reset to the standard settings." : "Could not reset the settings.",
                 status ? "text-ok" : "text-err");
}

function initClapControls() {
  const toggle = $("clap-toggle");
  const sensitivity = $("clap-sensitivity");
  const greet = $("clap-greet");
  const save = $("clap-greeting-save");
  if (toggle) toggle.addEventListener("change", () => setClapEnabled(toggle.checked));
  if (sensitivity) sensitivity.addEventListener("change", () => saveClapSettings(true));
  if (greet) greet.addEventListener("change", () => saveClapSettings(false));
  if (save) save.addEventListener("click", () => saveClapSettings(false));

  const calStart = $("clap-cal-start");
  const calCancel = $("clap-cal-cancel");
  const calSave = $("clap-cal-save");
  const calReset = $("clap-cal-reset");
  if (calStart) calStart.addEventListener("click", startClapCalibration);
  if (calCancel) calCancel.addEventListener("click", cancelClapCalibration);
  if (calSave) calSave.addEventListener("click", saveClapCalibration);
  if (calReset) calReset.addEventListener("click", resetClapCalibration);
}

function initVoice() {
  const test   = $("btn-speak-test");
  const stop   = $("btn-speak-stop");
  const toggle = $("voice-output-toggle");

  if (test)   test.addEventListener("click", () => voiceCommand("speak test"));
  if (stop)   stop.addEventListener("click", voiceStop);
  if (toggle) toggle.addEventListener("change", () => setVoiceOutput(toggle.checked));

  // The speech-model installer moved here from the first-run wizard —
  // Voice is where someone comes when they want voice, and it is entirely
  // optional.
  const modelStartBtn = $("model-install-start");
  const modelCancelBtn = $("model-install-cancel");
  const modelRetryBtn = $("model-install-retry");
  if (modelStartBtn) modelStartBtn.addEventListener("click", startModelInstall);
  if (modelCancelBtn) modelCancelBtn.addEventListener("click", cancelModelInstall);
  if (modelRetryBtn) modelRetryBtn.addEventListener("click", startModelInstall);

  const inputToggle = $("voice-input-toggle");
  if (inputToggle) inputToggle.addEventListener("change", () => setVoiceInputEnabled(inputToggle.checked));

  // The microphone dropdown is the one shared choice: the level meter
  // and the clap listener both use it, and it is saved server-side so it
  // survives a restart. It used to move nothing but this page's own test.
  const deviceSelect = $("diag-device-select");
  if (deviceSelect) {
    deviceSelect.addEventListener("change", () => setSharedMicrophone(deviceSelect.value));
  }

  const testMic = $("diag-test-mic");
  const refreshDiag = $("diag-refresh");
  if (testMic) testMic.addEventListener("click", testMicrophone);
  if (refreshDiag) refreshDiag.addEventListener("click", refreshVoiceDiagnostics);
  // An open microphone must not survive navigating away from the page.
  window.addEventListener("pagehide", stopMicrophoneTest);

  // The neural voice: engines, installation, and how names are said.
  const nvTest = $("nv-test");
  const nvStop = $("nv-stop");
  const nvSelect = $("nv-voice-select");
  const nvSpeed = $("nv-speed");
  const nvStart = $("nv-install-start");
  const nvCancel = $("nv-install-cancel");
  const nvRetry = $("nv-install-retry");
  if (nvTest) nvTest.addEventListener("click", testNeuralVoice);
  if (nvStop) nvStop.addEventListener("click", voiceStop);
  if (nvSelect) nvSelect.addEventListener("change", () => selectNeuralVoice(nvSelect.value));
  if (nvSpeed) nvSpeed.addEventListener("change", () => setNeuralSpeed(nvSpeed.value));
  if (nvStart) nvStart.addEventListener("click", startVoiceInstall);
  if (nvCancel) nvCancel.addEventListener("click", cancelVoiceInstall);
  if (nvRetry) nvRetry.addEventListener("click", startVoiceInstall);

  const pronPreview = $("pron-preview");
  const pronSave = $("pron-save");
  if (pronPreview) pronPreview.addEventListener("click", previewPronunciation);
  if (pronSave) pronSave.addEventListener("click", savePronunciation);

  initCloudVoice();
  initClapControls();

  refreshVoiceStatus();
  refreshVoiceInputStatus();
  refreshVoiceDiagnostics();
  refreshModelPreview();
  pollModelInstallStatus();  // covers an install already running from a previous page load
  refreshNeuralVoice();
  refreshPronunciations();
  pollVoiceInstallStatus();  // likewise, for a voice install already in flight
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

// ── Action history ──────────────────────────────────────────────────────────
// The persisted audit trail, which has been written since v0.2 and until
// now had nowhere to be read. Inputs arrive already redacted (see
// app/core/redaction.py) — nothing here re-masks them, because the raw
// values never reached the database.

const HISTORY_STATUS_BADGE = {
  succeeded:        "badge-ok",
  failed:           "badge-err",
  blocked:          "badge-err",
  cancelled:        "badge-muted",
  expired:          "badge-muted",
  pending_approval: "badge-warn",
  approved:         "badge-info",
  executing:        "badge-info",
  proposed:         "badge-muted",
};

const HISTORY_STATUS_LABEL = {
  succeeded:        "Succeeded",
  failed:           "Failed",
  blocked:          "Blocked",
  cancelled:        "Cancelled",
  expired:          "Expired",
  pending_approval: "Waiting for approval",
  approved:         "Approved",
  executing:        "Running",
  proposed:         "Proposed",
};

function _historyRow(cells) {
  const row = document.createElement("tr");
  cells.forEach(build => row.appendChild(build()));
  return row;
}

function _cell(text, className) {
  const td = document.createElement("td");
  if (className) td.className = className;
  td.textContent = text;
  return td;
}

function _historyMessage(text) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = 5;
  cell.className = "empty";
  cell.textContent = text;
  row.appendChild(cell);
  return row;
}

function _formatWhen(iso) {
  if (!iso) return "—";
  const when = new Date(iso);
  return isNaN(when.getTime()) ? iso : when.toLocaleString();
}

function _historyDetail(entry) {
  // Prefer what happened; fall back to why it was refused, then to the
  // redacted input. An empty cell would leave the user guessing.
  if (entry.result_summary) return entry.result_summary;
  if (entry.status === "blocked" && entry.policy_reason) return entry.policy_reason;
  const summary = entry.input_summary || {};
  const keys = Object.keys(summary);
  if (keys.length) return keys.map(k => `${k}: ${summary[k]}`).join(", ");
  return "—";
}

async function loadActionHistory() {
  const tbody = $("history-tbody");
  if (!tbody) return;

  const filter = $("history-filter");
  const status = filter ? filter.value : "";

  tbody.textContent = "";
  tbody.appendChild(_historyMessage("Loading…"));

  let data;
  try {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    data = await API.get(`/actions/history${query}`);
  } catch (e) {
    tbody.textContent = "";
    tbody.appendChild(_historyMessage("Could not load the action history."));
    return;
  }

  const entries = data.entries || [];
  tbody.textContent = "";

  const count = $("history-count");
  if (count) {
    if (!data.total) {
      count.textContent = "Nothing has been recorded yet.";
    } else if (status) {
      count.textContent = `${entries.length} of ${data.total} recorded action(s) match this filter.`;
    } else if (entries.length < data.total) {
      count.textContent = `Showing the ${entries.length} most recent of ${data.total} recorded actions.`;
    } else {
      count.textContent = `${data.total} recorded action(s) on this computer.`;
    }
  }

  if (!entries.length) {
    tbody.appendChild(_historyMessage(
      status ? "No recorded actions match this filter." : "No actions have been recorded yet."
    ));
    return;
  }

  entries.forEach(entry => {
    const row = _historyRow([
      () => _cell(_formatWhen(entry.created_at)),
      () => _cell(entry.tool_name || "—", "font-mono text-xs"),
      () => _cell(entry.risk || "—", "text-xs"),
      () => {
        const td = document.createElement("td");
        const badge = document.createElement("span");
        badge.className = "badge " + (HISTORY_STATUS_BADGE[entry.status] || "badge-muted");
        badge.textContent = HISTORY_STATUS_LABEL[entry.status] || entry.status;
        td.appendChild(badge);
        return td;
      },
      () => _cell(_historyDetail(entry), "text-xs"),
    ]);
    tbody.appendChild(row);
  });
}

function initActions() {
  const btn = $("actions-refresh");
  if (btn) btn.addEventListener("click", loadActions);
  loadActions();

  const historyRefresh = $("history-refresh");
  const historyFilter = $("history-filter");
  if (historyRefresh) historyRefresh.addEventListener("click", loadActionHistory);
  if (historyFilter) historyFilter.addEventListener("change", loadActionHistory);
  loadActionHistory();
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
    // Home's "What JARVIS is doing" card reads the same source as the
    // topbar, so the two can never disagree.
    _setOverviewValue("dash-runtime-state", evt.payload.to || "standby");
  }

  // Keep Home's attention panel honest the moment an approval appears or
  // resolves anywhere — a stale "nothing waiting" is worse than a delay.
  if (evt.type === "action_approval_changed" || evt.type === "action_result") {
    if ($("dash-pending-approvals")) {
      refreshOverviewApprovals();
      refreshOverviewRecentActions();
    }
  }

  // Keep the privacy indicator live across every open tab/page the
  // instant it changes anywhere (chat command, another tab, voice).
  if (evt.type === "action_result" && evt.payload && evt.payload.tool_name === "set_privacy_mode") {
    refreshPrivacyIndicator();
    // And release the microphone. Repainting the indicator while the
    // clap listener kept running is the defect this line replaces:
    // privacy mode has to mean the capture stops, not that a label
    // changed colour.
    applyPrivacyToClap();
  }

  // Keep the Actions page's pending list live when an action anywhere
  // (voice, another tab, chat) changes approval state or finishes running.
  if (evt.type === "action_approval_changed" || evt.type === "action_result") {
    if ($("actions-list")) loadActions();
    // The history is the record of what just happened, so a stale one is
    // wrong in exactly the moment someone is watching it.
    if ($("history-tbody")) loadActionHistory();
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

  // Every page, not just Voice: the point of clap activation is that it
  // works while JARVIS is minimised, whatever page happens to be open.
  // refreshClap() starts nothing unless the stored setting says to, and
  // that setting is off until somebody turns it on.
  if (window.ClapController) {
    ClapController.onClapPair(onClapPair);
    ClapController.onChange(onClapControllerChange);
  }
  refreshClap(false);
  // Navigating away, closing the tab and quitting all end the same way:
  // every track stopped and every context closed before this page goes.
  window.addEventListener("pagehide", () => {
    pttCancel();
    if (window.ClapController) ClapController.setQuitting();
    // The heartbeat stops with the page, which is what makes the
    // server's staleness window mean something.
    stopClapHeartbeat();
  });
  // …and the other half. A page restored from the back/forward cache
  // never runs DOMContentLoaded again, so without this, Back into the
  // Voice page would be a page that had already declared itself gone.
  window.addEventListener("pageshow", (evt) => {
    if (!evt.persisted) return;
    if (window.ClapController) ClapController.setRestored();
    clapLastReported = "";
    refreshClap(true);
  });

  const path = window.location.pathname.replace(/\/+$/, "");

  if (path === "/ui" || path === "/ui/dashboard" || path === "") {
    loadDashboard();
    refreshOverview();
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
  } else if (path === "/ui/diagnostics") {
    initDiagnostics();
  } else if (path === "/ui/setup") {
    initSetup();
  }
});
