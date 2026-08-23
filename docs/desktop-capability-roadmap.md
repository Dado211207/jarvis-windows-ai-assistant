# JARVIS desktop capability roadmap

**Status: documentation only.** Nothing in this file is implemented by the
pass that added it, except where a row says "Shipped". It exists so that
"could JARVIS do X?" has an answer with a security model attached, rather
than being re-argued from scratch each time.

Every capability below carries the same seven fields, because those are
the fields that decide whether something is buildable rather than merely
desirable:

| Field | Why it is here |
|---|---|
| **Status** | Shipped / Partial / Planned / Deliberately excluded. |
| **Prerequisites** | What has to exist first. Most of these are not "time". |
| **Security model** | What the boundary is. "Be careful" is not one. |
| **Approval** | Automatic, per-action, or per-session. |
| **External account** | Whether the user must sign up for something. |
| **Test strategy** | How it would be proved, given no test may touch a real account. |
| **Why not now** | The specific reason, not "out of scope". |

A capability with no security model is not a capability that is waiting
for time; it is one that has not been designed.

---

## 1. Coding and software work

### 1.1 Coding Workspace — read, edit, run, preview

- **Status:** Shipped (this pass). See `docs/coding-workspace-architecture.md`.
- **Prerequisites:** Met.
- **Security model:** A single containment gate (`app/coding/workspace.py`),
  a closed proposal schema, a three-tier command policy, and process-tree
  ownership. Project content is untrusted input throughout.
- **Approval:** Per-action for installs, deletions, undeclared commands
  and commits. Automatic for reads and the project's own declared scripts.
- **External account:** None. An Anthropic key or a local Ollama model,
  the same as ordinary chat.
- **Test strategy:** Fixture projects built at test time; a scripted
  provider; injection payloads the model obeys.
- **Why not now:** n/a.

### 1.2 Pushing, pull requests, merging

- **Status:** Deliberately excluded in this version.
- **Prerequisites:** A credential story that does not put a token anywhere
  JARVIS can read it back; a review UI that shows what would be published
  *before* it is; and a way to prove the diff the user approved is the
  diff that gets pushed.
- **Security model:** Would need the credential to live in Windows
  Credential Manager (like the Anthropic and ElevenLabs keys), never in a
  remote URL, never in a log, and to be used by a single module.
- **Approval:** Per-action, every time, naming the remote and the branch.
- **External account:** GitHub (or another forge).
- **Test strategy:** A local bare repository as the remote. No test may
  ever reach github.com.
- **Why not now:** Publishing is irreversible in a way local work is not.
  A mistaken local commit is `git reset` away; a mistaken push is in
  somebody else's clone. The gap between "JARVIS edited a file" and
  "JARVIS published to a repository other people pull from" is the
  largest single step in this document, and it should not be taken in the
  same pass that introduced the editing.

### 1.3 Deployment

- **Status:** Deliberately excluded.
- **Prerequisites:** 1.2, plus a rollback story.
- **Security model:** Undesigned.
- **Approval:** Would be per-action.
- **External account:** A hosting provider.
- **Test strategy:** Undesigned — every real deployment target is a
  production service, which §19-style rules forbid tests from touching.
- **Why not now:** A deployment is a push with an audience. Same reasoning
  as 1.2, more so.

### 1.4 Remote repository cloning

- **Status:** Deliberately excluded.
- **Prerequisites:** A rule for which hosts may be cloned from, and an
  answer to "the clone is now untrusted content on your disk".
- **Security model:** The cloned repository would be Zone 3 (untrusted)
  from the moment it lands, which the existing design already handles —
  but the *fetch* is a network operation to a host named by a model or a
  URL the user pasted, which the existing design deliberately has no
  precedent for.
- **Approval:** Per-action, naming the host.
- **External account:** None necessarily.
- **Test strategy:** A local path as the source.
- **Why not now:** `git clone <url>` where the url comes from anywhere but
  a person typing it is a remote-content-fetch primitive, and this
  codebase has exactly one of those (`app/core/safe_fetch.py`) built
  under much narrower rules.

---

## 2. Research and the browser

### 2.1 Browser checks against the owned preview

- **Status:** Shipped (this pass), with a real gap — see below.
- **Prerequisites:** Met in a source checkout.
- **Security model:** `browser_qa.run_checks` takes a `PreviewSession`,
  not a URL. There is no parameter through which any origin but the owned
  loopback preview can be named.
- **Approval:** Automatic — it is a read of a page JARVIS itself started.
- **External account:** None.
- **Test strategy:** A real server, a real page with planted defects, a
  real browser.
- **Gap:** Playwright and its Chromium are test dependencies, not runtime
  ones. The packaged Windows build cannot run these checks and reports
  that it cannot, rather than reporting zero problems. Shipping them
  means adding roughly 150 MB of browser to the installer, which is a
  packaging decision, not a coding one.

### 2.2 General web browsing / research

- **Status:** Deliberately excluded.
- **Prerequisites:** A content-trust model. Everything fetched is
  attacker-controlled text entering a model's context.
- **Security model:** Would extend the untrusted-content envelope already
  used for project files, but the *reachable surface* is the entire
  internet rather than one folder, and no allowlist meaningfully bounds
  "research".
- **Approval:** Per-session at minimum; per-domain more likely.
- **External account:** A search API, or a headless browser.
- **Test strategy:** A local fixture server only.
- **Why not now:** The injection defence in this codebase is structural —
  a malicious README cannot name a tool because the schema forbids it.
  That property holds regardless of content, which is exactly why it is
  the right defence. But general browsing multiplies the volume of
  hostile content by several orders of magnitude, and it introduces
  exfiltration: a model that has read the user's project and can also
  make a network request has a channel. Closing that needs its own
  design.

### 2.3 Fetching a specific URL the user typed

- **Status:** Partial — `app/core/safe_fetch.py` exists for JARVIS's own
  narrow uses (update checks, model downloads) with host pinning and no
  redirects.
- **Prerequisites:** A decision about whether a model may ever supply the
  URL. Currently it may not.
- **Security model:** Host-pinned HTTPS, `follow_redirects=False`, bounded
  body, content-type checked.
- **Approval:** Per-action.
- **External account:** None.
- **Test strategy:** A local server; the existing `test_safe_fetch.py`.
- **Why not now:** The primitive exists; exposing it to a model is 2.2's
  problem in miniature.

---

## 3. Documents and media

### 3.1 Reading a document the user points at (PDF, DOCX, XLSX)

- **Status:** Planned.
- **Prerequisites:** A parsing dependency for each format, each of which
  is a new attack surface (malformed PDFs are a classic), and a
  containment rule for where documents may be read from.
- **Security model:** The Coding Workspace boundary generalises: a chosen
  folder, canonicalised, protected paths excluded. Parsing would run
  in-process, so a parser vulnerability is a JARVIS vulnerability —
  which argues for well-maintained libraries and bounded input sizes.
- **Approval:** Per-folder, once, like adding a coding project.
- **External account:** None.
- **Test strategy:** Fixture documents, including malformed ones.
- **Why not now:** Three new dependencies and a licence review each. The
  boundary work it depends on is done; the dependency work is not.

### 3.2 Writing documents

- **Status:** Planned. `create_note` already writes Markdown to
  `Documents\JARVIS_Notes` under Phase 6 rules.
- **Prerequisites:** 3.1's containment generalisation.
- **Security model:** Same as 3.1, plus: never overwrite without showing
  a diff, which the editing engine already does.
- **Approval:** Per-action for anything outside `JARVIS_Notes`.
- **External account:** None.
- **Test strategy:** tmp_path, byte comparison.
- **Why not now:** Follows 3.1.

### 3.3 Images and video

- **Status:** Deliberately excluded for input; not designed for output.
- **Prerequisites:** A vision-capable provider path, and a rule about
  what a screenshot may contain.
- **Security model:** Undesigned. Note that `docs/` and this file's
  Safety section forbid continuous screen capture outright, and that
  remains true.
- **Approval:** Per-action.
- **External account:** Depends on the provider.
- **Test strategy:** Fixture images.
- **Why not now:** No design.

---

## 4. Personal productivity

### 4.1 Calendar, email, messaging

- **Status:** Deliberately excluded.
- **Prerequisites:** OAuth against a real provider, token storage, and a
  refresh story — all in an application whose current credential surface
  is two API keys in Windows Credential Manager.
- **Security model:** Would need per-scope consent, and a hard rule that
  reading is separate from sending. CLAUDE.md already forbids "email
  sending without approval flow".
- **Approval:** Per-account for reading; per-message for sending, always.
- **External account:** Google / Microsoft.
- **Test strategy:** A fake OAuth server and recorded fixtures. No test
  may reach a real mailbox.
- **Why not now:** An assistant that can read your mail and also read
  untrusted web content is an exfiltration path with a nice UI. The
  ordering matters: this should come after 2.2 is solved, not before.

### 4.2 Local notes, reminders, memory

- **Status:** Shipped. `app/core/memory.py`, `create_note`, `read_note`.
- **Security model:** `app/core/secret_guard.py` refuses a memory that
  contains a credential, checked twice, before the write.
- **Why not now:** n/a.

---

## 5. Windows automation

### 5.1 Launching applications, opening folders and URLs

- **Status:** Shipped (Phase 6). Allowlisted executables, a hardcoded
  folder map, and URL schemes checked before `https://` is prepended.

### 5.2 Locking the session

- **Status:** Shipped (Phase 7), and deliberately the *only* session
  action that will ever exist. Sign out, restart, sleep and shut down all
  end running programs and can lose unsaved work in other applications;
  locking cannot. `tests/test_phase7_actions.py` asserts nothing else was
  added.

### 5.3 Arbitrary UI automation (clicking, typing into other apps)

- **Status:** Deliberately excluded, permanently, absent a separate
  design review.
- **Security model:** There isn't one. Synthetic input is indistinguishable
  from the user's own to every other application on the machine, including
  the ones holding their bank session. A model with a keyboard is not a
  bounded capability.
- **Why not now:** See above. This is not a "later" item.

### 5.4 Registry, services, scheduled tasks

- **Status:** Deliberately excluded. `reg`, `sc`, `schtasks` and their
  neighbours are in `commands.BLOCKED_PROGRAMS` and are not approvable.

---

## 6. Mobile

**Not implemented, and not implementable from this codebase as it stands.**
JARVIS is a Windows desktop application: a tray process, a native
WebView2 window, `winsound`, SAPI5, Windows Credential Manager,
Authenticode verification, and an Inno Setup installer. None of that has
a mobile equivalent.

`docs/mobile-architecture.md` (written in an earlier pass) sets out the
only shape that would work: the desktop stays the system of record, and a
mobile client is a *view* onto it over an authenticated channel the user
explicitly opens — not a second copy of the assistant.

- **Prerequisites:** A remote-access story. Today the API binds to
  127.0.0.1 and every read endpoint is unauthenticated *because* of that.
  Exposing it to a phone means authenticating all ~50 read endpoints
  first. That is the work, and it is substantial.
- **Security model:** Undesigned beyond "loopback stops being the
  boundary, so something else has to become one".
- **Approval:** Per-device pairing.
- **External account:** None if paired directly; a relay otherwise, which
  is a much larger question.
- **Test strategy:** Undesigned.
- **Why not now:** The single sentence in CLAUDE.md's Safety rules — "API
  stays local. FastAPI binds to 127.0.0.1 only" — is load-bearing for the
  entire security posture of this product. Mobile is not a feature to be
  added to that; it is a decision to replace it.

---

## What this document is not

It is not a commitment to build any of it, and it is not a schedule.
Several rows exist specifically to record that something was considered
and refused, so that a future pass does not spend a day rediscovering
why 5.3 is a bad idea.
