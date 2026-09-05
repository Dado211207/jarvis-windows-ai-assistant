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

> ### A checksum belongs to one build, not to a version
>
> **Never check an installer against a hash from a different run.** Inno
> Setup embeds a build timestamp, so two builds of the *same commit*
> produce two different files with two different SHA-256 values. That is
> expected and is not a sign of tampering.
>
> The only correct comparison is the `.sha256` file that came out of the
> **same artifact** as the `.exe` you are holding. A hash written down
> from an earlier build, quoted in a chat message, or copied from another
> run's log will not match, and treating that mismatch as tampering — or
> worse, treating a match against a stale hash as proof — both get the
> reasoning backwards.
>
> These builds are **not bit-reproducible**, and nothing here claims they
> are.

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

3. **A Workspace ID** — optional, and only needed for some keys. See below.

The key is stored in **Windows Credential Manager**, never in a file in
your profile, never in the database, and never in a log. You can add,
change or remove it later from **Settings**.

### When you need a Workspace ID

Anthropic decides this by how the key was created, not by what kind of
account you have:

- A key **scoped to a single workspace** works on its own. Leave the
  Workspace ID **blank**.
- A key that can act in **more than one workspace** — a personal or
  service account key that was not tied to one workspace when it was
  created — must say which workspace each request acts in. Without it
  Anthropic rejects every request with *"anthropic-workspace-id is
  required when authenticating with an identity-linked API key"*, and
  JARVIS says so in one sentence.

#### The simplest route: scope the key, and skip this field

When you create the key in the Claude Console (**Settings → API keys →
Create key**) you can choose a single workspace for it. Anthropic:

> You can also scope the key to a specific workspace, which lets you skip
> setting a workspace ID manually in future requests.

Your **Default Workspace** counts as that workspace like any other, so
this works on an account that has never created another one. **You do not
need to create an extra workspace to use JARVIS.** A key scoped this way
needs nothing in the Workspace ID field, ever.

#### If your key is not scoped to one workspace

Then JARVIS needs the ID of the workspace you want it to act in, and where
you find it depends on which workspace that is:

- **Any workspace other than Default** — the **ID** column of
  [Settings → Workspaces](https://platform.claude.com/settings/workspaces)
  in the Claude Console. It starts with `wrkspc_`.
- **The Default Workspace** — **it is not in that table.** Anthropic
  documents this explicitly: *"List Workspaces omits the Default
  Workspace; its ID is in the `anthropic-workspace-id` response header of
  any request that runs there."* It has a normal `wrkspc_` ID; it is just
  never listed.

  To read that header, send one request with a key that already runs in
  that workspace and look at the response headers. From PowerShell, with
  your key in `$env:ANTHROPIC_API_KEY`:

  ```powershell
  $r = Invoke-WebRequest -Uri https://api.anthropic.com/v1/messages -Method POST `
    -Headers @{ "x-api-key" = $env:ANTHROPIC_API_KEY
                "anthropic-version" = "2023-06-01"
                "content-type" = "application/json" } `
    -Body '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"ok"}]}'
  $r.Headers["anthropic-workspace-id"]
  ```

  That prints the `wrkspc_…` value. If the request fails with the
  workspace-required error instead, the key is a multi-workspace key and
  the header is absent — scope a key to the workspace (above) and use that
  one, which needs no ID here at all.

#### What happens when you save

Enter the key and the Workspace ID together and press **Save**. JARVIS
tries the pair against Anthropic once before storing anything, and the
outcome decides what is kept:

| What Anthropic said | What JARVIS does |
|---|---|
| It worked | Both are saved, and the key is marked **verified** |
| The key is invalid, or the pair needs a Workspace ID | **Nothing is saved** — not the key, not the Workspace ID |
| The account has no credit | Both are saved, marked **account has no credit** |
| Nothing — offline, timed out, rate-limited | Both are saved, marked **not yet confirmed**, so you do not have to type the key again |

That last row is why "JARVIS checks it works before saving it" would be
too strong a claim: a pair that could not be checked *at all* is stored
and labelled honestly, rather than thrown away because your network was
down for ten seconds.

The Workspace ID is stored on this PC as account metadata (it identifies
a workspace; it does not authenticate anything), and like the key it
never appears in a log, a diagnostic or an API response. Removing the key
clears it too — and if it cannot be cleared, JARVIS says so rather than
reporting a clean removal.

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

## Upgrading from the v0.1 ZIP

If you used the old ZIP distribution, JARVIS tries to bring its database
— memories, chat history, the action log — into the new location on
first launch.

**What it does.** It looks for a v0.1 `data\jarvis.db`, validates it,
takes a backup, copies it into `%LOCALAPPDATA%\JARVIS\data\`, and applies
the current schema on top. Your original file is **only ever read**: it
is never moved, changed or deleted, even after a successful import. The
decision is recorded, so this happens once and a second launch cannot
duplicate anything.

**Where it looks.** v0.1 stored its database at `data\jarvis.db`
*relative to wherever you ran `JARVIS.exe` from*, so there is no single
path to check. JARVIS looks at a short, fixed list — no disk scanning:

| Location | Why |
|---|---|
| `%LOCALAPPDATA%\Programs\JARVIS\data\jarvis.db` | You installed over an extracted ZIP |
| `C:\JARVIS\JARVIS\data\jarvis.db` and `C:\JARVIS\data\jarvis.db` | The location v0.1's own QUICKSTART named |
| `%USERPROFILE%\Downloads\JARVIS\data\jarvis.db` | Where a downloaded ZIP lands by default |
| `%USERPROFILE%\Desktop\JARVIS\data\jarvis.db` | ditto |
| `%USERPROFILE%\Documents\JARVIS\data\jarvis.db` | ditto |

**If yours is somewhere else**, set `JARVIS_LEGACY_DB` to the full path
of the old `jarvis.db` before the first launch. That is checked ahead of
everything in the table.

**When it does not run — all of these leave both files untouched:**

- No v0.1 database in any of those places, and `JARVIS_LEGACY_DB` unset.
- **You already have data in this installation.** An import would have to
  either overwrite it or merge two histories; JARVIS does neither, and
  says so in the log.
- The old file is corrupt, truncated, or is some other program's
  database. It is left exactly as it is and JARVIS starts empty.
- It already ran once. The decision is remembered either way.
- You are running from source. This is a packaged-build behaviour only.

A failure here never stops JARVIS starting — the worst case is the empty
database you would have had anyway, and the old file still sitting where
it was.

---

## What JARVIS will not remember

Ask it to remember something that contains a password, an API key or a
token and it refuses, explains why, and **stores nothing**. The check
runs before the write, so the value never reaches the database — there is
nothing to purge afterwards, and nothing to find in the file.

The message names the *kind* of secret it spotted and never the value
itself, because that message is shown on screen and written to the log.

Ordinary sentences are unaffected: "remind me to change my password on
Friday" is stored, because there is no secret in it. What gets refused is
a credential-shaped string, or a credential noun with a value attached
("`api_key = …`", "my password is …").

Your Anthropic key is not affected by any of this — it goes in Settings,
which puts it in the Windows Credential Manager.

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
