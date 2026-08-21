# Installing JARVIS on Windows

This is the end-user install guide for the packaged desktop application.
If you want to work on the code instead, see the "From source" section of
`README.md` — a source checkout is a development environment, not an
installation.

---

## What you are installing

`JARVIS-Setup-v<version>-x64.exe`, a per-user [Inno Setup][inno]
installer.

| | |
|---|---|
| Administrator rights | Not required (`PrivilegesRequired=lowest`) |
| Application goes to | `%LOCALAPPDATA%\Programs\JARVIS` |
| Your data goes to | `%LOCALAPPDATA%\JARVIS` — kept separate on purpose |
| Start Menu | A "JARVIS" entry is created |
| Python | Not required; the runtime is inside the executable |
| Code signing | **None.** See [SmartScreen](#smartscreen) below |

[inno]: https://jrsoftware.org/isinfo.php

---

## Current status: release candidate

**Version `0.2.0-rc1` is a release candidate.** It is not a published
release, and this is not a marketing hedge — concretely:

- There is **no GitHub Release** and **no tag** for it.
- There is no download page, and no permanent link to give anyone.
- The build is **unsigned**.

Builds come from the **Windows Installer** workflow in GitHub Actions and
are attached to the workflow run that produced them.

### Who can download a build

Workflow-run artifacts are not public files, even though this repository
is public:

- You must be **signed in to GitHub** with **read access to this
  repository**.
- The download is served only from that workflow run's own page, behind a
  short-lived signed URL.
- Anonymous visitors browsing the public repository cannot fetch it.
- Every artifact **expires** — the installer's retention is 30 days —
  after which the run's page still exists but the file is gone.

That last point is why this document does not link to a build. Any link
written here would be dead within a month. Ask the repository owner for
the current run, or open **Actions → Windows Installer** and pick the
most recent successful run.

---

## Downloading and verifying

1. Open the **Windows Installer** workflow run you were pointed at, while
   signed in to GitHub.
2. Scroll to **Artifacts** and download **JARVIS-Windows-Installer**.
   GitHub always serves artifacts as a `.zip`.
3. Extract it. You get two files:
   - `JARVIS-Setup-v<version>-x64.exe`
   - `JARVIS-Setup-v<version>-x64.exe.sha256`
4. **Check the hash before running the installer.** In PowerShell, from
   the folder you extracted into:

   ```powershell
   Get-FileHash .\JARVIS-Setup-v0.2.0-rc1-x64.exe -Algorithm SHA256 | Format-List
   Get-Content .\JARVIS-Setup-v0.2.0-rc1-x64.exe.sha256
   ```

   The two must show the same value, ignoring case. The sidecar file is
   produced by the same build step that produced the installer, and the
   value is also printed in that step's log.

5. If they differ, **stop**. Do not run the file. Delete it and download
   it again.

### SmartScreen

The installer is unsigned, so Windows shows *"Windows protected your
PC — unrecognised publisher"*. That warning is accurate: nothing about
this file proves who built it, which is exactly why step 4 exists. If you
choose to continue, it is **More info → Run anyway**.

Signing is deliberately out of scope for v0.2. Claiming a verified
publisher this build does not have would be worse than the warning.

---

## Installing

Run the installer and follow it. Then, on first launch, JARVIS asks for:

1. **A preferred name** — what it calls you.
2. **An Anthropic API key** — optional, and only needed for conversational
   AI replies. Commands work without one.

The key is stored in **Windows Credential Manager**, never in a file in
your profile, never in the database, and never in a log. You can add,
change or remove it later from **Settings**.

Everything else is set up from inside the application, when and if you
want it:

| Feature | Where | Downloads anything? |
|---|---|---|
| Neural voice (Kokoro) | Voice page | Yes — ~93 MB, after you press the button |
| Speech recognition | Voice page | Yes — the speech model, after you press the button |
| Local AI (Ollama) | Settings → Local AI | Yes — shown in full first, including size and licence |

Nothing downloads on startup, on a status check, or as a side effect of
anything else.

---

## Uninstalling

**Settings → Apps → JARVIS → Uninstall**, or the uninstaller in the
install folder.

By default, **your data is kept**: settings, chat history, memory and any
models you downloaded stay in `%LOCALAPPDATA%\JARVIS`, so reinstalling
later picks up where you left off. The uninstaller asks whether to remove
it, and the answer defaults to **No**.

Removed either way: the application files, the Start Menu entry, the
start-with-Windows shortcut if you enabled it.

Never removed, even if you choose complete removal:

- **WebView2** and the **Visual C++ runtime** — shared Windows components
  other software may depend on.
- **Ollama and its models**, even if JARVIS installed Ollama for you.
- **`Documents\JARVIS_Notes`** — notes you wrote are yours.

Choosing complete removal additionally deletes `%LOCALAPPDATA%\JARVIS`
and the API key from Credential Manager. That step is performed by the
application itself (`JARVIS.exe --uninstall-cleanup --purge-data`),
because only it knows how the key was stored — an installer guessing at a
Credential Manager target name is how an uninstall leaves a secret behind
while reporting success.

---

## If something goes wrong

**Diagnostics** inside the application reports version, paths, database
integrity, backend status and provider status, and can copy a sanitised
report — every field is a boolean, a count or a path, never a secret.

Logs live in `%LOCALAPPDATA%\JARVIS\data\logs`. Child-process output is
redacted before it is written.

The installed application can also check itself:

```powershell
& "$env:LOCALAPPDATA\Programs\JARVIS\JARVIS.exe" --selftest
```

It prints one line per capability — speech recognition, audio decoding,
the neural voice runtime, the credential store, the native window — and
exits non-zero if a required one could not load. Add `--deep` (with both
voice models installed) to make it synthesise real audio and transcribe
it back.
