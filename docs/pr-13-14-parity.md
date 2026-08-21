# What PR #15 does and does not carry over from PR #13 and PR #14

Two earlier draft PRs are still open and unmerged:

| PR | Branch | Base | Scope |
|---|---|---|---|
| [#13](https://github.com/Dado211207/jarvis-windows-ai-assistant/pull/13) | `feat/persistent-settings-memory` | `main` | Persistent settings & personality memory (v0.1.7-alpha) |
| [#14](https://github.com/Dado211207/jarvis-windows-ai-assistant/pull/14) | `feat/windows-installer-onboarding` | **`feat/persistent-settings-memory`** | Windows installer + first-run onboarding, stacked on #13 |

PR #15 was developed from `main`, not from either of them, so **commit
ancestry proves nothing about feature parity**. What follows comes from
reading both implementations and both test suites.

Neither PR is merged or closed by this pass.

---

## Parity table

| Capability | Status in #15 | Detail |
|---|---|---|
| Persistent settings | **Implemented differently, and narrower** | See below |
| Personality / preferences memory | **Implemented differently** | See below |
| Secret guard | **Missing** | See below |
| Installer | **Supersedes fully** | `packaging/jarvis.iss` replaces `installer/JARVIS.iss` |
| Onboarding | **Supersedes fully** | `app/core/onboarding.py` + `/ui/setup` |
| DPAPI / keyring credential storage | **Supersedes fully** | `app/core/credentials.py` |
| AppData paths | **Supersedes fully** | `app/core/app_paths.py` replaces `app/core/paths.py` |
| Data migration from an alpha install | **Missing** | See below |
| Diagnostics | **Supersedes fully** | `app/core/diagnostics.py`, eight sections |
| Startup-with-Windows option | **Supersedes fully** | `app/launcher/startup_shortcut.py` |
| Uninstall data preservation / removal | **Supersedes fully** | `app/core/ownership.py` + the `.iss` `usUninstall` hook |
| Windows installer smoke testing | **Supersedes fully** | `scripts/test_clean_install.py`, seven phases |

---

## The three that are not simple wins

### Persistent settings — narrower on purpose, and narrower than intended

PR #13 stores settings in a SQLite `settings` table with roughly fifteen
allowlisted fields: your name, assistant name, language, response style,
tone, theme, compact mode, default page, TTS enabled/rate/volume/voice,
pinned commands, and a `safety_mode` that is locked on and can never be
disabled.

PR #15 stores them in `app/core/preferences.py` — a JSON file in AppData
behind an allowlist of nine keys:

```
ai_provider, ollama_model, speak_replies, preferred_name,
close_action, stt_enabled, voice_key, voice_speed,
ollama_installed_by_jarvis
```

Every key #15 has, it has for a concrete reason: something a person can
change in the installed application that must survive a restart. The
overlap with #13 is `preferred_name` and the voice settings.

**Not carried over:** assistant name, language, response style, tone,
theme, compact mode, default page, pinned commands.

Those are real features and #15 does not have them. They are presentation
and personality preferences rather than v0.2 infrastructure, which is why
they were not rebuilt — but "supersedes" would be the wrong word.

`safety_mode` has no equivalent because there is nothing to lock: in #15
the approval gate is not a setting. `app/core/policy.py::evaluate()`
decides from a tool's declared `RiskLevel`, there is no switch anywhere
that turns it off, and `tests/test_security_invariants.py` asserts that
every approval-required tool refuses `registry.execute()`. A locked
boolean is a weaker guarantee than an absent one.

### Personality memory — same idea, different shape

PR #13 adds a `preferences` SQLite table with title, value, category
(profile/style/voice/ui/command/general_preference), source,
`is_sensitive` and timestamps, driven by `remember that …`, `save
preference …`, `what do you remember`, `search memory …`, `forget …` and
an approval-gated `clear memory`.

PR #15 has a `memories` table and three tools — `add_memory`,
`search_memory`, and `clear_memory` behind the approval gate — reached by
`memory add …` and `memory search …`, plus a Memory page with per-item
delete and `GET /privacy/data`.

Both are explicit-only: neither infers anything from ordinary
conversation. What #15 lacks is #13's **categorisation** and its more
natural phrasings — `remember that I prefer dark roast` is not a route in
#15, though `memory add …` does the same thing. #15 adds privacy-mode
gating that #13 does not have: while privacy mode is on, a memory write
is refused rather than performed quietly.

### Secret guard — genuinely missing

PR #13's `app/core/secret_guard.py` scans **every value written to
settings or memory** and refuses to persist anything shaped like a
credential: `sk-ant-`, `sk-`, GitHub, Netlify, AWS, Google and Slack
tokens, private keys, bearer tokens, and `password=`/`token=`/`secret=`
assignments.

PR #15 has no equivalent. `app/core/memory.py::add_memory()` stores what
it is given.

`app/core/redaction.py` is a different mechanism for a different problem
— it redacts **tool inputs** before they reach a log line, the
`action_lifecycle` audit trail or a WebSocket event. It does not run on
the memory write path.

So in #15, someone who types `memory add my key is sk-ant-...` gets
exactly that stored in the database in plaintext. `preferences.py`
refuses to store a credential, but only because its allowlist has no key
that could hold one — not because it inspects values.

**This is a real gap, and #13 has the better answer.** It is recorded
here rather than fixed, because this pass is finishing the v0.2 release
candidate and not adding to it.

### Data migration — genuinely missing

PR #14's `app/core/migration.py` performs a one-time migration of an
alpha-era ZIP install's `data\jarvis.db` into the new AppData layout:
never overwrites an existing destination, never deletes the legacy
source, backs it up first, atomic copy, integrity-checked before and
after. 222 lines of tests.

PR #15 has nothing equivalent. `db/migrations.py` is schema creation
only. Someone who used a v0.1 alpha ZIP and then installs v0.2 gets a
fresh, empty database; their old one is still sitting in the ZIP folder,
untouched but unread.

How much this matters depends on whether anyone actually ran the alpha
ZIP long enough to accumulate memories worth keeping. It is a smaller
gap than the secret guard, but it is a gap, and #14 has the answer.

---

## Things #14 has that #15 deliberately excludes

- **`app/core/update_check.py` and a "Check for updates" button.** #15
  has no automatic update check at all, by design: the only outbound
  request the application makes is a chat message to the provider the
  user configured. "Check for updates" opens the releases page in the
  browser. See `app/api/routes.py` and `docs/THREAT_MODEL.md`.
- **`scripts/secret_scan.py` and `installer/scan_artifacts.ps1`.** #15
  enforces the same property differently, through
  `tests/test_security_invariants.py` walking the assembled application
  and the source tree, and through licence-policy checks that inspect
  the real packaged tree in the installer job.

---

## Recommendation

If PR #15 merges, #13 and #14 should be closed as superseded — **but not
silently**, and not before the two gaps above are tracked as their own
work:

1. **The secret guard.** Port `app/core/secret_guard.py` and apply it to
   the memory write path. This is the one worth doing.
2. **Alpha data migration.** Port `app/core/migration.py`, or decide
   explicitly that v0.1 alpha data is not worth carrying and say so in
   the release notes.

Closing them without recording those would lose two implemented,
tested features that nothing in #15 replaces.
