# Work log — pre-install audit and hardening pass

A public, tracked handoff record. Not authorization, and not cross-session memory.
Code and current owner instructions take precedence if this becomes stale. No
secrets, personal data, absolute local paths, usernames or signed artifact URLs.

- Branch: `claude/pre-install-audit-hardening`
- Base: `main` at `22e419da530b18d37f9d9aa5416aed5dc3b28894` (verified live before
  any change; unchanged by this pass)
- Purpose: a final pre-install audit before the owner runs JARVIS on a real
  Windows 11 PC. **Nothing here has been installed, merged, tagged, released or
  deployed.**

## Closed out: PR #17

PR [#17](https://github.com/Dado211207/jarvis-windows-ai-assistant/pull/17) was
squash-merged into `main` as `22e419d` on 2026-08-30. It repaired four installer
defects (ISPP directive, preview `plan_id` contract, and two path-gate holes that
let the installer job **skip** while the check reported success). Its post-merge
runs on `22e419d` were all green at attempt 1 — CI `33301544905`, Windows Build
`33301544914`, Windows Installer `33301544877` with job `99230569556` executing
all 19 steps and ending `ALL CLEAN-INSTALL CHECKS PASSED`.

The `msedge.exe` survivor from run `33251235787` **remains unreproduced and
unfixed.** It did not recur in 42 diagnostic iterations (runs `33272738623`,
`33297310832`; 126 cleanup passes over 1540 processes, zero survivors) nor in any
installer run since. The opt-in diagnostics scaffolding is deliberately still in
place — `scripts/diagnose_survivor_flake.py`, the `survivor_iterations` input and
its two steps in `.github/workflows/windows-build.yml`, and the diagnostic fields
in `app/launcher/process_tree.py`. Remove it only once the question is answered.
The classification to apply if a survivor is reproduced is unchanged:

- `already_gone` → the observation path is the cause; the narrow correction is a
  final identity-aware verification before declaring a survivor;
- `still_alive` with the same identity → Edge genuinely survived
  `TerminateProcess`; stop, that needs a different design;
- `pid_reused` or `inaccessible` → stop and report; do not apply the
  `already_gone` correction.

## This pass: what was found

### 1. `ready` does not prove the dashboard rendered — real, pre-existing, **not fixed**

`GET /desktop/ready` composes four *process* facts. None is evidence that
WebView2 painted anything:

- `window_alive` is answered by the window child's IPC pump, which starts as soon
  as `webview_window.current_window()` is non-None. `create_and_run()` publishes
  that object from `webview.create_window()`, which returns **before**
  `webview.start()` is called — so READY can precede the native window's own
  creation, never mind its navigation.
- The obvious fix does not work. In pywebview 6.2.1's edgechromium backend,
  `EdgeChrome.on_navigation_completed(self, sender, _)` discards the
  `NavigationCompletedEventArgs` without reading `IsSuccess`, then calls
  `inject_pywebview()` regardless; `webview/util.py` sets `events.loaded` both at
  the end of the injection and inside its own `except` handler. **A WebView2
  error page fires `loaded` exactly like a healthy dashboard.** Wiring it would
  add a false signal, not remove one.

No behavioural correction was implemented. A real fix has to prove rendering *at
the page* — an in-page beacon, or an `evaluate_js` probe for a known element,
driven by the code that owns the window, in the spirit of
`app/coding/browser_qa.py`. That is a new mechanism on the startup critical path
which cannot be executed in a Linux container (pywebview is Windows-only) and
would reach the owner's first install untested. Instead the limitation is now
stated in `app/launcher/desktop_ready.py`, pinned by
`test_ready_is_four_process_facts_and_claims_nothing_about_rendering`, and listed
as a manual check in `docs/physical-pc-checklist.md` item 4.

### 2. WebView2 bootstrapper download — determined **acceptable**, no change

`packaging/jarvis.iss::EnsureWebView2()` downloads Microsoft's Evergreen
Bootstrapper and runs it without a project-side hash or Authenticode check.
Determined against primary Microsoft documentation (*Distribute your app and the
WebView2 Runtime*): this is Microsoft's own documented online-only workflow,
step for step — inspect the `pv` regkey at the three documented locations, treat
`0.0.0.0`/empty as not installed, fetch the bootstrapper from the permanent
fwlink, run `MicrosoftEdgeWebview2Setup.exe /silent /install`. Microsoft
prescribes **no** hash or signature step, and:

- a SHA-256 pin is actively wrong here — the file behind the evergreen link is
  *meant* to change, so a pin would fail on the first Microsoft update;
- Inno's `DownloadTemporaryFileWithISSigVerify` verifies an ISSig signature this
  project would have to produce, not Microsoft's Authenticode one;
- `DownloadTemporaryFile` does validate TLS (it rejects expired and self-signed
  certificates);
- Windows 11 — the owner's target — ships the Evergreen Runtime, so
  `WebView2Installed()` returns true and this path never executes there.

A `WinVerifyTrust` `DLLImport` in Pascal Script was considered and rejected: the
nested `WINTRUST_DATA`/`WINTRUST_FILE_INFO` marshalling cannot be exercised on
Linux or in CI (the runners already have the runtime, so `EnsureWebView2()`
returns early there too), and a subtly wrong struct layout can return a **false
pass** — worse than an honest absence of a check.

What was unpinned is now pinned:
`test_setup_only_ever_downloads_over_https_from_a_microsoft_host` holds that
setup performs exactly one download, over HTTPS, from a Microsoft host, with no
plain-HTTP URL anywhere in the script.

### 3. `SECURITY.md` was stale — corrected

It described secrets as living "only in `.env`". The packaged product keeps every
API key in Windows Credential Manager through `app/core/credentials.py`
(Anthropic, ElevenLabs and OpenAI, each its own entry), with the
`ANTHROPIC_API_KEY` environment variable taking precedence when set because
development and CI depend on it. The file now also records that an ordinary
uninstall **keeps** credentials and that removal is the explicit `--purge-data`
choice.

### 4. A test compelled `PROJECT_STATE.md` to stay stale — corrected

Found by the correction in finding 3's neighbourhood: the first full gate on
this branch came back `1 failed, 3044 passed`.
`test_provenance_and_project_state_are_explicit_about_the_limits` asserted the
literal string `"Draft, open, unmerged"` in `docs/ai/PROJECT_STATE.md`. That is
a **transient** status. Once PR #17 was squash-merged, the only way to keep the
test green was to go on describing a merged pull request as an open draft — the
test enforced exactly the staleness it was meant to guard against, and it would
have done so again on every future merge.

It now asserts the durable property instead: an explicit `- State:` line, and no
pull request named anywhere in the file without a word saying where it stands
(`merged`/`open`/`closed`/`draft`). Proven in both directions — it passes on the
corrected file, and fails with a named line when a PR is mentioned bare or the
`State:` line is removed. The three other assertions in that test
(`not authorization`, `Real microphone`, the no-tag/release/merge constraint)
are untouched. Pre-existing defect, surfaced by this pass.

## Verified and found sound (no change needed)

| Area | Evidence |
| --- | --- |
| Loopback binding | `test_the_api_binds_to_loopback_by_default`, `test_nothing_binds_to_all_interfaces`, `test_the_bind_all_exemption_cannot_bind_anything` |
| Session-token protection | `test_every_mutating_endpoint_requires_the_session_token`, `test_sensitive_personal_gets_require_the_session_token` |
| WebSocket is read-only | `test_the_websocket_stream_cannot_run_a_command` |
| No shell, no dynamic exec, no pickle | `test_no_subprocess_call_uses_a_shell`, `test_no_dynamic_code_execution`, `test_nothing_deserialises_untrusted_pickles` |
| Approvals cannot be bypassed | `test_no_approval_required_tool_can_be_executed_directly` |
| Clipboard stays SENSITIVE and read-only | `test_the_clipboard_tool_is_permanently_approval_required`, `test_there_is_no_clipboard_writing_or_monitoring` |
| Redaction before persistence | `test_tool_inputs_are_redacted_before_they_are_persisted` |
| One memory INSERT | single site in `db/database.py` |
| Temporary microphone file | `POST /voice/transcribe` uses `mkstemp` + `try/finally` → `unlink(missing_ok=True)` on success *and* failure; logs only `success` and a character count |
| Speech-model integrity | SHA-256 verified for LFS-tracked blobs, size-only for the four small config files — and `model_installer.py` says so rather than implying all four are hash-verified |
| No silent model download | STT refuses to fetch a model without an explicit setting or a Setup-page action |
| Ollama is loopback-only | `providers._ollama_base_url()` is a constant with no environment override |
| Coding Workspace Git safety | `gitsafe.FORBIDDEN_VERBS` covers `reset`, `checkout`, `clean`, `stash`, `rebase`, `push` |

## Known gaps

- **Visible rendering is unverified by automation** (finding 1). Manual item.
- The diagnostics JSONL records no route or iteration label, so route attribution
  is inferred from record structure, not read directly.
- The 14 Windows-only skips in the installer build's suite are not enumerated —
  that build runs `pytest` without `-rs`.
- `main` has no branch protection. Noted, deliberately unchanged.
- No workflow declares a `permissions:` block, so `GITHUB_TOKEN` takes the
  repository default. Actions are pinned to mutable major tags (`@v4`, `@v5`),
  not commit SHAs. There is no dependency hash lock and no SBOM. All four are
  public-release hardening, not blockers for a supervised private install, and
  none was changed in this pass.
- The installer is unsigned; SmartScreen will warn. Accurate, and the owner's
  call.

## Next action

1. Independent review of this branch.
2. If accepted, the owner performs the real-PC acceptance in
   `docs/physical-pc-checklist.md`, starting with item 4 (the window must
   actually show the dashboard).

Do not tag, release, deploy, or install JARVIS on the owner's PC without a
current explicit instruction.
