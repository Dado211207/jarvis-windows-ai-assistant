"use strict";

const $ = id => document.getElementById(id);

// Server-rendered per-launch session token — see app/core/session_token.py.
// Delivered as a data attribute on <body>, not an inline <script>, so this
// page needs no script-src 'unsafe-inline' CSP allowance.
function _jarvisToken() {
  return document.body.dataset.jarvisToken || "";
}

const API = {
  async get(path) {
    const r = await fetch(path, { headers: { "X-Jarvis-Token": _jarvisToken() } });
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
  try {
    // Redacted server-side (app.core.diagnostics.get_report_text) — never
    // built from lastReport client-side, so the copy-safe rendering has
    // exactly one implementation, and it's the one with test coverage.
    const { text } = await API.get("/diagnostics/report-text");
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
