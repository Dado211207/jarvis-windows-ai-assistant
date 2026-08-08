# Desktop shell: pywebview in-process shutdown is not reliable

Status: **open blocker for the release candidate.** Recorded here so the
evidence is not lost between sessions.

## What was built

`app/launcher/webview_window.py` gives JARVIS a real native desktop
window (pywebview 6.2.1 → WinForms → WebView2), replacing the previous
"open the dashboard in the default browser" behaviour, with the browser
kept as an explicit tray fallback. That part works: on `windows-latest`
CI the installed `JARVIS.exe` launches, the window opens (WebView2's own
`msedge` child processes are visible in the runner's orphan cleanup),
`/health`, the dashboard, the SQLite database and single-instance
detection all pass.

## The problem

**A graceful `taskkill` (no `/F`) does not reliably terminate the
process.** This is the mechanism Windows itself uses for "End task", and
the mechanism Inno Setup's `CloseApplications=yes` relies on to close a
running JARVIS before an upgrade or uninstall. When it fails, JARVIS
keeps running invisibly and an upgrade cannot close it automatically.

## Why we know it is nondeterministic, not just broken

Three consecutive CI runs on **identical application code**:

| Commit | Code difference | `Windows Installer` job |
|---|---|---|
| `d109d7c` | — | ✅ pass (`ALL CLEAN-INSTALL CHECKS PASSED`) |
| `9a8ca9c` | **empty commit** — message only, zero change | ❌ fail (Phase A.9) |
| `5a5bee7` | **docs only** — adds this file, no code touched | ❌ fail (Phase A.9) |

`git diff d109d7c 9a8ca9c` is empty, and `5a5bee7` changes nothing under
`app/`, `scripts/` or `packaging/`. Same installer, same test, differing
outcomes.

So this is a race, not a deterministic defect — which is worse for a
release candidate than a consistent failure, because a green run proves
nothing on its own. Note the direction: **it mostly fails.** One pass in
three is the outlier, not a 50/50 coin flip, so `d109d7c`'s green run
should be read as luck rather than as evidence the mitigations landed.

All three failures share one signature: the trace ends at
`tray PumpMessages() starting` (plus the second instance's own startup
lines from Phase A.8) and stops. No shutdown message is ever traced.

## What the boot trace proves

`app/launcher/tray.py`'s `wnd_proc` traces an allowlist of
shutdown-related messages (`WM_CLOSE`, `WM_DESTROY`, `WM_QUIT`,
`WM_QUERYENDSESSION`, `WM_ENDSESSION`), and
`webview_window.py`'s own `on_closing` traces every invocation. On the
failing run, after `tray PumpMessages() starting`, the trace shows:

- no `tray wnd_proc received ...` line for **any** of those messages
- no `webview on_closing` line
- no `tray do_quit() starting` line

So this is not a handler misbehaving. **Nothing in the process received a
close request at all.** That distinction is exactly what the tracing was
added to establish, and it rules out the whole class of "our shutdown
logic has a bug" explanations.

## Root cause (structural, verified against pywebview's source)

Verified by installing pywebview 6.2.1 and reading
`webview/platforms/winforms.py` directly:

- `create_window()` calls `signal.signal(SIGINT, ...)`, which CPython
  only permits on the main thread — so `webview.start()` **must** own the
  process's main thread.
- It then runs the entire WinForms `Application.Run()` loop on its own
  .NET STA thread, leaving the calling Python thread parked in
  `while thread.IsAlive: thread.Join(500)`.

Consequence: with pywebview in the process, **no window belongs to the
main Python thread**. The tray's hidden window had to move to a
background thread to make room. Before this pass, that tray window was on
the main thread and graceful `taskkill` worked reliably across many green
CI runs — the correlation is exact.

The most consistent reading of the evidence is that Windows' graceful
close targets a single window (whichever it resolves as the process's
main window), and which one it resolves — the tray's hidden window or the
WebView2 form, the latter possibly still initialising — varies run to
run. That last step is inference; the parts above it are directly
observed.

## Mitigations already in place (necessary, not sufficient)

- `WM_QUERYENDSESSION` is now handled, so a Windows logoff/shutdown can
  close JARVIS cleanly instead of only being able to kill it.
- `webview_window.force_exit_after()` guarantees the process dies within
  a bounded grace period **once a shutdown is actually underway**.

Neither helps when no close request is delivered in the first place,
which is precisely the failing case.

## Recommended fix: split the GUI into its own process

Keep the parent `JARVIS.exe` as server + tray with the tray's message
loop back on the **main thread** — the exact configuration that was
reliably green — and run pywebview in a child process
(`JARVIS.exe --window`) that owns its own main thread. The parent regains
proven graceful-close behaviour; the child's fragile native GUI loop is
isolated, and killing/restarting it never risks the server.

The alternative, if a second process is judged not worth it, is to drop
the embedded window and return to the browser-based UI — which the
milestone spec itself allows, having made the native window conditional
on it packaging *reliably*.

## Do not do

- Do not "fix" this by force-killing (`taskkill /F`) in
  `scripts/test_clean_install.py`. That would hide a real product defect:
  a user's "End task" and the installer's upgrade-time close would still
  fail on a real machine.
- Do not treat a single green `Windows Installer` run as proof this is
  resolved. Given the demonstrated flakiness, only repeated consecutive
  green runs mean anything here.
