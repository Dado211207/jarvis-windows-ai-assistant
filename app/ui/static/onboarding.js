"use strict";

// First-run onboarding wizard. Talks only to /onboarding/* on this machine's
// own loopback API. The API key is sent to the backend to validate/store but
// is never logged, never echoed back, and never written anywhere in this file.

const $ = id => document.getElementById(id);

// Server-rendered per-launch session token — see app/core/session_token.py.
function _jarvisToken() {
  return window.__JARVIS_TOKEN__ || "";
}

const API = {
  async get(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Jarvis-Token": _jarvisToken() },
      body: JSON.stringify(body || {}),
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

const panels = Array.from(document.querySelectorAll(".onboarding-panel"));
const dots = Array.from(document.querySelectorAll(".onboarding-dot"));

function showStep(step) {
  panels.forEach(p => { p.hidden = p.dataset.panel !== step; });
  const idx = dots.findIndex(d => d.dataset.step === step);
  dots.forEach((d, i) => {
    d.classList.toggle("active", i === idx);
    d.classList.toggle("done", i < idx);
  });
  API.post("/onboarding/step", { step }).catch(() => {});
}

function setKeyStatus(text, kind) {
  const el = $("ob-key-status");
  if (!el) return;
  el.textContent = text;
  el.className = "onboarding-key-status" + (kind ? ` status-${kind}` : "");
}

document.querySelectorAll("[data-next]").forEach(btn => {
  btn.addEventListener("click", () => showStep(btn.dataset.next));
});
document.querySelectorAll("[data-back]").forEach(btn => {
  btn.addEventListener("click", () => showStep(btn.dataset.back));
});

// --- API key step ---

const keyInput = $("ob-api-key");
const keyToggle = $("ob-key-toggle");

if (keyToggle) {
  keyToggle.addEventListener("click", () => {
    const hidden = keyInput.type === "password";
    keyInput.type = hidden ? "text" : "password";
    keyToggle.textContent = hidden ? "Hide" : "Show";
  });
}

const keyValidateBtn = $("ob-key-validate");
if (keyValidateBtn) {
  keyValidateBtn.addEventListener("click", async () => {
    const value = keyInput.value.trim();
    if (!value) {
      setKeyStatus("Enter a key, or use “Skip for now”.", "err");
      return;
    }
    keyValidateBtn.disabled = true;
    setKeyStatus("Validating with Anthropic…", "busy");
    try {
      const result = await API.post("/onboarding/api-key", { api_key: value });
      if (result.success) {
        setKeyStatus("Key validated and saved securely.", "ok");
        keyInput.value = "";
        setTimeout(() => showStep("voice"), 600);
      } else {
        setKeyStatus(result.error || "Could not validate that key.", "err");
      }
    } catch (e) {
      setKeyStatus("Could not reach JARVIS to validate the key.", "err");
    } finally {
      keyValidateBtn.disabled = false;
    }
  });
}

const keySkipBtn = $("ob-key-skip");
if (keySkipBtn) {
  keySkipBtn.addEventListener("click", async () => {
    try {
      await API.post("/onboarding/api-key/skip", {});
    } catch (e) { /* proceed regardless — skip is always allowed locally */ }
    showStep("voice");
  });
}

// --- Voice step ---

const voiceContinueBtn = $("ob-voice-continue");
if (voiceContinueBtn) {
  voiceContinueBtn.addEventListener("click", async () => {
    const enabled = $("ob-voice-enabled").value === "true";
    try {
      await API.post("/onboarding/voice", { enabled });
    } catch (e) { /* non-fatal — preference can be set later in Settings */ }
    showStep("startup_pref");
  });
}

// --- Startup step ---

const startupContinueBtn = $("ob-startup-continue");
if (startupContinueBtn) {
  startupContinueBtn.addEventListener("click", async () => {
    const enabled = $("ob-startup-enabled").value === "true";
    try {
      await API.post("/onboarding/startup", { enabled });
    } catch (e) { /* non-fatal — preference can be set later in Settings */ }
    showStep("finish");
  });
}

// --- Finish ---

const finishBtn = $("ob-finish");
if (finishBtn) {
  finishBtn.addEventListener("click", async () => {
    finishBtn.disabled = true;
    try {
      const result = await API.post("/onboarding/complete", {});
      if (result.success) {
        window.location.href = "/ui/";
        return;
      }
      // Required step unresolved (e.g. key step never reached) — send the
      // user back to fix it instead of silently failing to finish.
      showStep("api_key");
    } catch (e) {
      finishBtn.disabled = false;
    }
  });
}

showStep("welcome");
