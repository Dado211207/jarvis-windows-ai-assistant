# JARVIS Quick Start Guide

> Alpha build for local testing. All data stays on your machine.

---

## Step 1 — Extract the ZIP

Unzip `JARVIS-Windows-v0.1.0-alpha.zip` into a folder, e.g. `C:\JARVIS\`.

You should see:

```
JARVIS\
  JARVIS.exe
  START_JARVIS.bat
  START_JARVIS_API.bat
  SETUP_ENV.bat
  QUICKSTART.md
  README.md
  .env.example
  _internal\
```

---

## Step 2 — First-time setup (optional — needed for AI responses)

Double-click **`SETUP_ENV.bat`**.

This creates a `.env` file from the included `.env.example` template.

Then open `.env` in Notepad and add your Anthropic API key:

```
ANTHROPIC_API_KEY=sk-ant-...
```

> Your key stays on your machine only. It is never included in the ZIP
> and is only sent to the Anthropic API when you send a message.

JARVIS works without an API key. Deterministic commands (system status,
memory, app launcher, screenshot) always work. Only unrecognised
natural-language queries require the key.

---

## Step 3 — Start JARVIS

Double-click **`START_JARVIS.bat`**

Or open a terminal in the JARVIS folder and run:

```bat
JARVIS.exe
```

You will see:

```
============================================================
 JARVIS -- Personal Windows AI Assistant
 Type 'help' to list commands, 'exit' to quit.
============================================================

jarvis>
```

---

## Step 4 — Try these commands

| Command | What it does |
|---|---|
| `help` | List all available commands |
| `status` | Show JARVIS version and config |
| `system status` | CPU, RAM, disk, battery |
| `open chrome` | Launch Chrome |
| `open notepad` | Launch Notepad |
| `screenshot` | Capture your screen |
| `memory add <text>` | Save a note to local memory |
| `memory search <query>` | Search saved notes |
| `what can you do?` | AI natural-language answer (needs API key) |
| `explain CPU usage in one sentence` | AI answer (needs API key) |
| `exit` | Quit JARVIS |

---

## Step 5 — Local API (optional)

Double-click **`START_JARVIS_API.bat`**

Then open **http://127.0.0.1:5555/docs** in your browser for the
interactive Swagger UI.

The API runs on `127.0.0.1:5555` only and is never accessible from
other devices on your network.

---

---

## Step 6 — Enable voice output (optional)

Open `.env` in Notepad and set:

```
JARVIS_TTS_ENABLED=true
```

Then restart JARVIS. Use these voice commands:

| Command | What it does |
|---|---|
| `speak on` | Enable voice for this session |
| `speak off` | Disable voice |
| `speak test` | Hear a test phrase |
| `stop speaking` | Stop current speech |

> Voice is **local and offline** — no API key required.
> This is text-to-speech **output only** — no microphone or wake word.

---

## Security notes

- The API key is **never** included in the ZIP or any release asset.
- JARVIS runs entirely on your machine.
- The local API binds to `127.0.0.1:5555` only.
- This is an **unsigned alpha build**. Windows SmartScreen may warn on
  first launch — click **More info** then **Run anyway**.
- The app launcher uses a strict allowlist — only approved apps can be
  launched through JARVIS.
