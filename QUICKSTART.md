# JARVIS Quick Start

> **v0.2.0-rc1 — release candidate.** Not a published release: there is
> no GitHub Release, no tag, and the build is unsigned. Everything stays
> on your machine except the chat messages you send to whichever AI
> provider you configure.

Installing it: **[`docs/WINDOWS_INSTALLER.md`](docs/WINDOWS_INSTALLER.md)**,
which also covers verifying the download's checksum. This guide is about
using it once it is installed.

---

## Step 1 — Launch it

Start Menu → **JARVIS**.

A native window opens, and a JARVIS icon appears in the system tray.
Closing the window leaves it running in the tray; **Quit JARVIS** from
the tray menu shuts it down properly. The tray menu also has **Restart**,
and can open the interface in your normal browser if you prefer that.

Only one copy runs at a time. Launching it again brings the existing
window to the front rather than starting a second one.

---

## Step 1b — Coming from the v0.1 ZIP?

Your old memories and history come across automatically on first launch,
if JARVIS can find them. Your original file is only ever read — never
moved, changed or deleted.

It looks in a short fixed list of locations (the old ZIP had no fixed
install path), and does nothing at all if this installation already has
data in it. Full detail, including how to point it at a database
elsewhere: **[`docs/WINDOWS_INSTALLER.md`](docs/WINDOWS_INSTALLER.md)**.

---

## Step 2 — First run

Two questions, once:

1. **What should JARVIS call you?**
2. **An Anthropic API key** — optional. Skip it and everything except
   conversational AI replies still works.

The key goes into **Windows Credential Manager**. Not a file in your
profile, not the database, not a log. Change or remove it later in
**Settings**.

---

## Step 3 — Try it

Type into **Chat**. Recognised commands run directly; anything else goes
to the AI provider you configured.

| Try | What happens |
|---|---|
| `help` | Everything JARVIS can do |
| `system status` | CPU, RAM, disk, battery |
| `open notepad` | Launches Notepad (allowlisted apps only) |
| `create note shopping list` | Writes to `Documents\JARVIS_Notes` |
| `list notes` / `read note <name>` | Reads them back |
| `remember that I prefer dark roast` | Saves to memory, explicitly |
| `what do you remember` | Shows what it kept |
| `lock my computer` | Locks the workstation |
| `read my clipboard` | **Asks first** — see step 5 |
| `answer me with your voice` | Speaks the last reply aloud |

The interface has ten pages in the sidebar: **Home**, **Chat**,
**Actions**, **Voice**, **Logs**, **Memory**, **Help**, **Settings**,
**Diagnostics** and **Setup**.

---

## Step 4 — Voice

**Voice** page. Nothing is downloaded until you press a button, and each
button says what it will fetch and how big it is first.

**Speaking.** Install the neural voice (Kokoro, ~93 MB, British male by
default) and JARVIS can talk. Then:

- **Speak replies** — a switch in the chat toolbar, off until you turn it
  on. It survives restarts.
- **Per-message** — every JARVIS message has a speaker button, with stop
  and replay. One utterance at a time; starting a new one stops the last.
- **By asking** — "answer me with your voice", "say that again", "read
  this aloud" all work, whether or not the switch is on.

Approval prompts are never read aloud. They are for reading and deciding.

**Listening.** Install the speech model and push-to-talk becomes
available: hold the button, speak, release. There is **no wake word and
no always-listening mode**, and there never will be — see `CLAUDE.md`.
The microphone is only ever open while you are holding the button.

---

## Step 5 — Actions that need approval

Some actions stop and ask. Clearing logs, clearing memory and reading the
clipboard all show you exactly what will happen and wait for you to
confirm or cancel.

- Nothing approval-gated can run without an explicit confirmation.
- Cancelled means cancelled — it cannot be re-confirmed afterwards.
- Pending requests expire after 10 minutes, and are forgotten on restart.
- **Actions** shows the history of what ran, what was refused and when.

---

## Step 6 — Local AI (optional)

**Settings → Local AI**, if you would rather run a model on your own
machine than send messages to Anthropic.

JARVIS shows you the whole plan before anything is fetched: the source,
the publisher, the licence, the download size and how much free space
this machine has. Nothing downloads until you press the button.

If Ollama is not installed, JARVIS can install it — downloading Ollama's
own installer over HTTPS from Ollama's own domain, verifying its
Authenticode signature actually names Ollama, and deleting the file
rather than running it if anything does not check out. There is no
"continue anyway". Ollama's installer then runs visibly, not silently.

An Ollama you already had is used as it is, never reinstalled over — and
never removed by JARVIS's uninstaller, even if JARVIS installed it.

---

## Step 7 — Privacy

- **Privacy mode** (tray menu or Settings) stops conversation history
  being replayed to the provider — each message goes alone.
- **Memory is explicit, and refuses secrets.** JARVIS saves what you ask
  it to save — except a password, API key or token, which it declines and
  does not store. The check runs before the write, so the value never
  reaches the database at all. The Memory page shows everything that was
  saved, with per-item delete.
- **Diagnostics** copies a sanitised report you can safely paste
  somewhere — every field is a boolean, a count or a path.
- The local server binds to `127.0.0.1` only. Other devices on your
  network cannot reach it.
- There is no automatic update check. The only outbound request JARVIS
  makes on its own is your chat message to the provider you chose.

**Never stored anywhere:** your API key in plaintext, clipboard history,
recorded audio, screen recordings, or keystrokes.

---

## Step 8 — Uninstalling

**Settings → Apps → JARVIS → Uninstall.**

Your data is kept by default — settings, history, memory and downloaded
models — so reinstalling later resumes where you left off. The
uninstaller asks whether to remove it and defaults to **No**.

Never removed, even if you ask for complete removal: WebView2, the
Visual C++ runtime, Ollama and its models, and your notes in
`Documents\JARVIS_Notes`.

---

## If something looks wrong

**Diagnostics** first — it reports paths, database integrity, backend and
provider status. Logs are in `%LOCALAPPDATA%\JARVIS\data\logs`.

To ask the installed application to check itself:

```powershell
& "$env:LOCALAPPDATA\Programs\JARVIS\JARVIS.exe" --selftest
```

One line per capability, and a non-zero exit code if a required one could
not load.
