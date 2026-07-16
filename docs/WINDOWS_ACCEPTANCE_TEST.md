# Windows Acceptance Test Checklist

Status: **every item below is UNVERIFIED.** Nothing in this document has
been run on a real Windows machine — this session has no Windows OS access
(a Linux container only), so every claim here is "should work, per code
review and CI-mocked tests," not "confirmed working." Run this checklist on
a real Windows 10/11 machine before treating the installer as trustworthy,
and update each row's status when you do.

## What you need

Just the installer artifact. Nothing else:

1. Download `JARVIS-Setup-<version>.exe` (from a CI build artifact — see
   `docs/WINDOWS_INSTALLER.md` — no public release exists yet).
2. Double-click it.
3. Follow the wizard.

You should **not** need, at any point: PowerShell, Command Prompt, a
`.bat` file, ZIP extraction, a `.env` file, Notepad, the GitHub CLI,
Python, or Node.js. If any step below asks you to use one of these, that
is itself a failure to report — not something to work around.

## Checklist

For each item: what to do, what should happen, and a place to mark it
✅ / ❌ / ⚠️ once you've actually run it.

| # | Item | How to test | Expected result | Status |
|---|---|---|---|---|
| 1 | Normal non-admin install | Double-click `JARVIS-Setup-<version>.exe` | No "Run as administrator" / UAC elevation prompt appears (or if Windows shows one anyway, it's for the installer's own signing status, not a requirement); wizard proceeds normally; installs under `%LOCALAPPDATA%\Programs\JARVIS` | ⬜ UNVERIFIED |
| 2 | Start Menu shortcut | After install, open the Start Menu and search "JARVIS" | A JARVIS shortcut appears and launches the app | ⬜ UNVERIFIED |
| 3 | Optional desktop shortcut | During install, check/leave unchecked "Create a desktop shortcut" | Unchecked by default; if checked, a desktop icon appears after install | ⬜ UNVERIFIED |
| 4 | No console window | Launch JARVIS normally (Start Menu / desktop icon) | No black terminal/console window appears or flashes persistently; only the browser dashboard opens | ⬜ UNVERIFIED |
| 5 | First-run onboarding | First launch after install | Browser opens to a welcome → privacy → API key → voice → startup wizard, not the raw dashboard | ⬜ UNVERIFIED |
| 6 | API key hidden in UI | On the API key onboarding step or Settings → AI Provider | Input is password-masked by default with a working Show/Hide toggle; page source and Diagnostics never show the full key, only a masked form like `sk-ant-...wxyz` | ⬜ UNVERIFIED |
| 7 | Provider setup success | Enter a real, valid Anthropic API key | Key validates, saves, and Chat gives real AI answers to open-ended questions afterward | ⬜ UNVERIFIED |
| 8 | Provider setup failure | Enter an invalid/malformed key, or disconnect network and try | A clear, specific error is shown (rejected key / no internet / rate limit / provider down as applicable) — never a crash, never a silent hang | ⬜ UNVERIFIED |
| 9 | Restart persistence | Close JARVIS fully, relaunch | Onboarding does not repeat; your API key, settings, and memory are all still there | ⬜ UNVERIFIED |
| 10 | Memory and Settings | Add a memory item (`remember that ...` in Chat) and change a Settings value | Both persist across a restart; changing settings never requires a restart to take effect | ⬜ UNVERIFIED |
| 11 | Single-instance behavior | With JARVIS already running, launch it again (Start Menu icon a second time) | Brings the existing browser tab/window to the front; does **not** start a second backend or open a second, independent dashboard | ⬜ UNVERIFIED |
| 12 | Normal Exit | Use whatever exit mechanism is exposed (tray icon "Exit" if present, or closing the app) | JARVIS shuts down cleanly, no error dialog | ⬜ UNVERIFIED |
| 13 | No orphan process | After Exit, check Task Manager | No `JARVIS.exe` (or `python.exe`/`uvicorn`-related) process remains running | ⬜ UNVERIFIED |
| 14 | Upgrade install | Install an older build, then run a newer `JARVIS-Setup-<version>.exe` over it | Upgrades in place (no need to uninstall first); settings/memory/API key survive the upgrade untouched | ⬜ UNVERIFIED |
| 15 | Uninstall preserving data | Windows Settings → Apps → Uninstall JARVIS | A prompt asks about deleting your data, defaulted to **No**; choosing No (or just closing/silently uninstalling) removes the app but leaves `%LOCALAPPDATA%\JARVIS\` (settings, memory, API key) intact | ⬜ UNVERIFIED |
| 16 | Reinstall detecting preserved data | After #15 (data preserved), reinstall JARVIS | First run picks up the old settings/memory/API key instead of re-running onboarding from scratch | ⬜ UNVERIFIED |
| 17 | Explicit user-data deletion | Uninstall again, this time explicitly choosing **Yes** to delete data | `%LOCALAPPDATA%\JARVIS\` is fully removed; a subsequent reinstall starts completely fresh (onboarding runs again) | ⬜ UNVERIFIED |
| 18 | SmartScreen warning status | First run of the freshly downloaded, unsigned installer | Windows SmartScreen likely shows an "unrecognized app" warning (expected — this build is unsigned, see `docs/SECURITY.md`); confirm it's the standard unsigned-publisher warning, not something worse (e.g. Defender flagging it as actual malware, which would be a real blocker to report) | ⬜ UNVERIFIED |

## If something fails

Report it plainly — which numbered item, what you saw instead of the
expected result, and (if available) the JARVIS log folder contents from
Diagnostics → Open log folder. Do not work around a failure by using
PowerShell/CMD/manual file edits "just this once" — if the normal flow
needs that, the normal flow is what's broken.
