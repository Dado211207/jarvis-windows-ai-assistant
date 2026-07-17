# JARVIS Quick Start

For the full walkthrough (screenshots, every step, troubleshooting), see
[`docs/USER_GUIDE.md`](docs/USER_GUIDE.md). This page is the short version.

> All data stays on your machine. Nothing is uploaded unless you add an
> Anthropic API key, and even then only your typed messages are sent.

---

## Install

Download `JARVIS-Setup-<version>.exe` from the
[latest release](https://github.com/dado211207/jarvis-windows-ai-assistant/releases)
and double-click it. No Administrator rights are needed — it installs to
your own user profile. JARVIS opens automatically when setup finishes.

*Prefer not to install anything?* `JARVIS-Portable-<version>.zip` is also
published on the same release — unzip it anywhere and double-click
`JARVIS.exe` directly. There's nothing to configure by hand either way; the
first launch walks you through setup inside JARVIS itself.

---

## First run

JARVIS opens in your browser (`127.0.0.1` only — never reachable from other
devices) and walks you through a short setup: a privacy explanation, an
optional Anthropic API key (skip it if you don't have one — JARVIS still
works for every built-in command), voice output on/off, and whether to
start JARVIS automatically with Windows.

---

## Using JARVIS

Everything happens in the browser dashboard: **Chat** for natural-language
questions and commands, **Actions** for anything that needs your
confirmation first, **Voice**, **Memory**, **Settings**, and **Diagnostics**.
A few things to try from the Chat page:

| Try typing | What happens |
|---|---|
| `status` | JARVIS version and configuration |
| `system status` | CPU, RAM, disk, battery |
| `open chrome` / `open notepad` | Launch an allowlisted app |
| `screenshot` | Capture your screen |
| `memory add <text>` | Save a note to local memory |
| `what can you do?` | Natural-language answer (needs an API key) |

## Prefer a terminal?

Advanced users and contributors can still run `JARVIS.exe --cli` for the
classic REPL, or `JARVIS.exe --api` to run just the local API
(`http://127.0.0.1:5555/docs` for the Swagger UI). Double-clicking
`JARVIS.exe` with no flags always opens the normal dashboard experience
above — that's the intended way to use JARVIS.

## Security notes

- The local API binds to `127.0.0.1` only — never reachable from other
  devices on your network.
- Your Anthropic API key, if you add one, is stored securely (Windows
  DPAPI) — never in a plaintext file. See `docs/SECURITY.md`.
- This build is currently unsigned. Windows SmartScreen may show an
  "unrecognized app" warning on first run — see `docs/SECURITY.md` for what
  to expect and why.
- The app launcher only opens an allowlisted set of applications.
