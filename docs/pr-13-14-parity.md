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

> **Updated.** The two gaps this audit found — the secret guard and the
> v0.1 data migration — have since been implemented. The table and the
> sections below reflect that; the original findings are kept because
> what was missing, and why it mattered, is the useful part of an audit.

| Capability | Status in #15 | Detail |
|---|---|---|
| Persistent settings | **Implemented differently, and narrower** | See below |
| Personality / preferences memory | **Implemented differently** | See below |
| Secret guard | **Now present** | `app/core/secret_guard.py`, see below |
| Installer | **Supersedes fully** | `packaging/jarvis.iss` replaces `installer/JARVIS.iss` |
| Onboarding | **Supersedes fully** | `app/core/onboarding.py` + `/ui/setup` |
| DPAPI / keyring credential storage | **Supersedes fully** | `app/core/credentials.py` |
| AppData paths | **Supersedes fully** | `app/core/app_paths.py` replaces `app/core/paths.py` |
| Data migration from an alpha install | **Now present** | `app/core/legacy_migration.py`, see below |
| Diagnostics | **Supersedes fully** | `app/core/diagnostics.py`, eight sections |
| Startup-with-Windows option | **Supersedes fully** | `app/launcher/startup_shortcut.py` |
| Uninstall data preservation / removal | **Supersedes fully** | `app/core/ownership.py` + the `.iss` `usUninstall` hook |
| Windows installer smoke testing | **Supersedes fully** | `scripts/test_clean_install.py`, eight phases |

---

## The four that needed more than a yes or no

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

### Secret guard — was missing, now ported

**The finding.** PR #13's `secret_guard.py` scans every value written to
settings or memory and refuses anything credential-shaped. PR #15 had no
equivalent: `add_memory()` stored what it was given, so `memory add my
key is sk-ant-…` was persisted verbatim, in plain text, in a file that
lives on the user's disk until they delete it.

`app/core/redaction.py` was not the answer and still is not: it redacts
**tool inputs** headed for a log line, the `action_lifecycle` audit trail
or a WebSocket event, and never runs on the memory write path. Both
exist; neither replaces the other.

**What was done.** `app/core/secret_guard.py`, adapted rather than
cherry-picked. Enforced in two places: `app/core/memory.py::add_memory()`
returns a readable refusal, and `db/database.py::Database.add_memory()` —
the only place a memory row is ever inserted — raises `SecretRejected`,
so a caller that forgets to check cannot quietly write a credential. A
test greps `app/` and `db/` to prove that insert is still the only one.

**One deliberate change from #13.** That version rejected any sentence
merely *containing* a credential noun, so "remind me to change my
password on Friday" could not be saved. The trade-off was stated and
defensible, but it makes the feature annoying in the common case where
no secret is present. The port splits the decision: a credential-shaped
string is always refused; a credential noun *with a value attached* is
refused; a bare mention is allowed. The residual risk — a sentence that
conveys a credential with no assignment structure at all — is stated in
the module docstring rather than papered over.

**The value is never echoed.** `find_secret()` returns a label, never
the matched text, and so does `SecretRejected`. A guard that quotes what
it caught puts the secret in the API response, the event stream and the
log.

### Data migration — was missing, now ported

**The finding.** PR #14's `migration.py` carries an alpha-era ZIP
install's `data\jarvis.db` into the AppData layout. PR #15 had nothing
equivalent — `db/migrations.py` is schema creation only — so someone
upgrading from the v0.1 ZIP got a fresh empty database while their old
one sat in the ZIP folder, untouched but unread.

**What was done.** `app/core/legacy_migration.py`, called from
`brain.initialise()` immediately before `create_tables()`, because once
an empty database exists at the destination there is nothing left to
migrate into.

**Two deliberate changes from #14.**

1. **#14 looked in the wrong place.** Its `legacy_db_candidates()`
   returned `installed_program_dir() / "data" / "jarvis.db"` — the *v0.2
   install directory*, which is not where a v0.1 ZIP was ever extracted,
   so in the real upgrade case it would have found nothing. The honest
   answer is that v0.1 declared `jarvis_db_path = "data/jarvis.db"`,
   relative to the working directory, and the ZIP could be extracted
   anywhere. This port checks a bounded list of the locations v0.1's own
   QUICKSTART named plus the default extraction folders, and accepts
   `JARVIS_LEGACY_DB` for anyone whose copy is elsewhere. No globbing, no
   `os.walk`, no disk scan — enforced by a test.
2. **#14 skipped on the destination merely existing.** That means it
   would refuse in the ordinary case, because `create_tables()` creates
   the destination on first launch. This port distinguishes an empty
   schema (safe to replace) from a database with rows in it (never
   touched).

Everything else is #14's design, which was sound: read-only source, a
backup first, copy-to-temp-then-rename, integrity checked before and
after, current schema applied afterwards, and a marker so the decision is
made once. One addition: the whole entry point is wrapped so it cannot
raise, because it runs on the startup path of a windowed build with no
console, where an unhandled exception becomes a modal dialog nobody can
dismiss.

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

Both gaps this audit found are now closed, so **PR #15 can be described
as fully superseding #13 and #14** — with one caveat, stated rather than
buried.

**The caveat.** #13's presentation and personality settings — assistant
name, language, response style, tone, theme, compact mode, default page,
pinned commands — genuinely do not exist in #15, and are not planned. If
"superseded" is meant to imply "nothing is lost", that is the list of
things that are. They are a deliberate exclusion, not an oversight: v0.2
is an infrastructure and packaging milestone, and those are v0.3 product
features that can be built on top of `preferences.py` whenever they are
wanted.

Everything else #13 and #14 do is present here, or present in a form
that is stronger:

- the secret guard, now enforced at the single insert as well as at the
  handler, and calibrated to stop refusing ordinary sentences;
- the v0.1 migration, now looking where a v0.1 install actually is and
  distinguishing an empty destination from somebody's data;
- the installer, onboarding, credential storage, AppData paths,
  diagnostics, startup shortcut, uninstall behaviour and packaged
  acceptance testing, each superseded outright.

**So: close #13 and #14 as superseded once #15 is merged** — not before,
and with the exclusion above written into whatever closes them, so the
decision to drop those eight settings is recorded as a decision rather
than lost as an accident.
