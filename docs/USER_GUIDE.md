# JARVIS User Guide

This is the end-user guide: install, first run, everyday use, Settings,
Diagnostics, and uninstall. For how the installer is built or what's still
in progress, see [`WINDOWS_INSTALLER.md`](WINDOWS_INSTALLER.md). For the
security model, see [`SECURITY.md`](SECURITY.md).

---

## Installing

1. Download `JARVIS-Setup-<version>.exe` from the
   [latest release](https://github.com/dado211207/jarvis-windows-ai-assistant/releases).
2. Double-click it. You'll see a normal Windows setup wizard — **no
   Administrator prompt appears**, because JARVIS installs into your own
   user profile, not `Program Files`.
3. Click through the wizard. A desktop shortcut is optional (unchecked by
   default); a Start Menu entry is always created.
4. When setup finishes, JARVIS launches automatically.

Prefer not to install anything? `JARVIS-Portable-<version>.zip` is
published alongside the installer on the same release — unzip it anywhere
and double-click `JARVIS.exe`. Either way, there is nothing to configure by
hand before your first launch.

## First run

The first time JARVIS starts, it opens in your default browser at an
address on `127.0.0.1` — never reachable from any other device — and walks
you through a short setup:

1. **Welcome** — what JARVIS is.
2. **Privacy** — plain-language explanation of what stays local and what
   (if anything) is ever sent to Anthropic.
3. **API key (optional)** — paste an Anthropic API key to enable
   natural-language chat, or click **Skip for now**. Every built-in command
   (system status, app launcher, screenshots, memory, notes) works either
   way; only open-ended AI conversation needs a key. If you add one, it's
   validated against Anthropic before being saved, and it's never stored as
   plain text — see [`SECURITY.md`](SECURITY.md). If you skip it, JARVIS
   says so plainly on the finish screen and in Chat ("AI chat not
   configured") instead of implying it's fully set up — you haven't lost
   anything by skipping, and nothing repeats the wizard on you for it.
4. **Voice** — turn on/off spoken replies (local text-to-speech; JARVIS
   never listens through your microphone).
5. **Startup** — whether JARVIS should start automatically when you sign in
   to Windows.
6. **Finish** — JARVIS opens the normal dashboard. If you added a key, it
   confirms AI chat is ready; if you skipped it, it says so and points you
   at Settings.

Skipped setup and want AI chat later? Open **Settings → AI Provider** any
time — paste your key there and it's validated and saved the same way, no
need to redo the wizard.

If setup doesn't finish (you close the window, lose your connection while
validating a key, etc.), JARVIS simply shows the wizard again next time you
open it — nothing is left half-configured.

## Everyday use

Launch JARVIS from the Start Menu, your desktop (if you added the
shortcut), or Windows Search. The dashboard has these pages:

| Page | What it's for |
|---|---|
| Dashboard | At-a-glance health: CPU/RAM, whether AI is configured, uptime |
| Chat | Type commands or questions — try `status`, `system status`, `open notepad`, `screenshot`, `memory add <text>`, or ask anything |
| Actions | Review, confirm, or cancel any action that needed your approval first |
| Voice | Turn spoken replies on/off, test it, stop it |
| Memory | Browse, search, or forget things you've explicitly asked JARVIS to remember |
| Settings | Your name, assistant name, language, tone, theme, voice settings, startup preference, and "Check for updates" |
| Diagnostics | Version, paths, database health, and an "open log folder" / "copy sanitized report" button for troubleshooting |
| Help | Quick command reference |

If you start JARVIS while it's already running, it brings the existing
window to the front instead of opening a second copy.

## Settings

Everything in Settings is a local preference stored in your own database —
nothing is ever inferred or saved without you asking. Your API key is never
shown or editable from the Settings page as plain text; if you need to
replace or remove it, see [`SECURITY.md`](SECURITY.md) for how it's stored.
`Safety mode` is always on and cannot be turned off from Settings.

## Diagnostics

Open the **Diagnostics** page if something isn't working or you're
reporting a bug. It shows your JARVIS version, install type, OS, database
integrity, whether the AI backend is configured, and where your logs live —
with a one-click **Open log folder** button and a **Copy sanitized report**
button that copies all of the above (never your API key, never `.env`
contents) to your clipboard for a bug report.

## Updates

Settings shows your current version and a **Check for updates** button.
JARVIS never downloads or installs an update automatically — at most it
tells you a newer version exists and links to the release page for you to
download and run yourself, the same way you installed originally.

## Uninstalling

Use **Windows Settings → Apps → Installed apps** (or **Add or Remove
Programs**) and uninstall JARVIS like any other app.

During uninstall you'll be asked once whether to also delete your JARVIS
data (settings, personality memory, conversation history, logs, and your
stored API key). **The default is "No"** — your data is kept so a future
reinstall picks up where you left off. Only click "Yes" if you want it
gone permanently; that action cannot be undone.

## A note on Windows SmartScreen

This build is currently unsigned. The first time you run the installer (or
JARVIS.exe from the portable ZIP), Windows SmartScreen may show an
"Windows protected your PC" warning. This is expected for any unsigned app
from a new publisher — click **More info**, then **Run anyway**. See
[`SECURITY.md`](SECURITY.md) for why, and what changes once a signed build
is available.
