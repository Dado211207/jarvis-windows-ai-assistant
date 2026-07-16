"use strict";

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
  async post(path) {
    const r = await fetch(path, { method: "POST", headers: { "X-Jarvis-Token": _jarvisToken() } });
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  },
};

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function setBadge(id, ok, okText, badText) {
  const el = $(id);
  if (!el) return;
  el.textContent = ok ? okText : badText;
  el.className = "badge " + (ok ? "badge-ok" : "badge-err");
}

function setDiagStatus(text, ok) {
  const el = $("diag-status");
  if (!el) return;
  el.textContent = text;
  el.className = "settings-status " + (ok ? "text-ok" : "text-err");
}

let lastReport = null;

function applyReport(report) {
  lastReport = report;
  setText("diag-version", report.version);
  setText("diag-phase", report.phase);
  setText("diag-frozen", report.frozen ? "yes" : "no (running from source)");
  setText("diag-os", report.os);
  setText("diag-onboarding", report.onboarding.complete ? "yes" : "not required / not yet completed");

  setText("diag-host", report.host);
  setText("diag-port", report.actual_port != null ? String(report.actual_port) : `${report.configured_port} (configured)`);
  setText("diag-provider", report.brain.provider);
  setBadge("diag-brain", report.brain.configured, "Claude AI", "local mode");
  setText("diag-tools", String(report.tools_registered));

  setText("diag-db-path", report.paths.db_path);
  if (report.database.exists) {
    setBadge("diag-db-ok", report.database.integrity_ok, "ok", "check failed");
  } else {
    setText("diag-db-ok", "not created yet");
    $("diag-db-ok").className = "badge badge-muted";
  }
  setText("diag-log-dir", report.paths.logs_dir);
  setText("diag-secret-backend", report.secure_storage.backend);
  setBadge("diag-api-key", report.secure_storage.api_key_stored, "configured", "not configured");

  const m = report.migration;
  setText("diag-migration", m ? m.status : "not applicable");
}

function formatReportText(report) {
  const lines = [
    `JARVIS Diagnostics Report`,
    `Version: ${report.version} (${report.phase})`,
    `Installed app: ${report.frozen}`,
    `OS: ${report.os}`,
    `Python: ${report.python_version}`,
    `Onboarding complete: ${report.onboarding.complete}`,
    ``,
    `Host: ${report.host}`,
    `Configured port: ${report.configured_port}`,
    `Actual port: ${report.actual_port}`,
    `AI provider: ${report.brain.provider} (model: ${report.brain.model})`,
    `AI configured: ${report.brain.configured}`,
    `Tools registered: ${report.tools_registered}`,
    ``,
    `Database path: ${report.paths.db_path}`,
    `Database exists: ${report.database.exists}`,
    `Database integrity ok: ${report.database.integrity_ok}`,
    `Log folder: ${report.paths.logs_dir}`,
    `Secure key storage backend: ${report.secure_storage.backend}`,
    `Secure key storage available: ${report.secure_storage.available}`,
    `API key configured: ${report.secure_storage.api_key_stored}`,
    `Legacy DB migration: ${report.migration ? JSON.stringify(report.migration) : "not applicable"}`,
  ];
  return lines.join("\n");
}

async function loadDiagnostics() {
  try {
    const report = await API.get("/diagnostics");
    applyReport(report);
    setDiagStatus("", true);
  } catch (e) {
    setDiagStatus("Error loading diagnostics: " + e.message, false);
  }
}

async function copyReport() {
  if (!lastReport) return;
  const text = formatReportText(lastReport);
  try {
    await navigator.clipboard.writeText(text);
    setDiagStatus("Report copied to clipboard.", true);
  } catch (e) {
    setDiagStatus("Could not copy automatically — select and copy the report manually.", false);
  }
}

async function openLogsFolder() {
  try {
    const r = await API.post("/diagnostics/open-logs-folder");
    setDiagStatus(r.message || (r.success ? "Opened." : "Could not open log folder."), r.success);
  } catch (e) {
    setDiagStatus("Could not open log folder: " + e.message, false);
  }
}

function initDiagnostics() {
  const refresh = $("diag-refresh");
  const copy = $("diag-copy");
  const openLogs = $("diag-open-logs");
  if (refresh) refresh.addEventListener("click", loadDiagnostics);
  if (copy) copy.addEventListener("click", copyReport);
  if (openLogs) openLogs.addEventListener("click", openLogsFolder);
  loadDiagnostics();
}

initDiagnostics();
