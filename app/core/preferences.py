"""User preferences chosen in the app, stored beside the onboarding
marker under app_paths.config_dir().

This exists for one reason: the Ollama provider is unreachable without
it. `jarvis_ai_provider` is a pydantic setting read from the environment
or a .env file, and a person running the installed JARVIS.exe has
neither — so a provider only selectable that way is a feature that
exists in the code and not in the product.

Deliberately narrow, and it should stay that way:

**An allowlist, not a settings store.** Only STORABLE_KEYS may be
written. This is not a general mechanism for overriding configuration
from a browser; adding a key here is a decision, not a convenience.

**Never a credential.** API keys live in the OS credential store
(app/core/credentials.py), which is a different thing with different
protections. A plain JSON file in AppData is the wrong place for a
secret and this module must never become one — hence the explicit
rejection in `store()` rather than a comment asking nicely.

**Precedence: a saved preference wins over the environment.** The env
var supplies the starting default when nothing has been saved; once
somebody picks a provider in Settings, that choice holds. The
alternative — environment always wins — produces a picker that silently
does nothing on a machine where the variable happens to be set, which
is worse than not offering the control at all. Development and CI are
unaffected: neither writes a preference file.

**Never raises.** An unreadable or corrupt file means "no preferences
saved", which degrades to the configured defaults rather than taking the
app down over a settings file.
"""

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from app.core.app_paths import config_dir
from app.core.safe_traceback import describe
from app.logging_config import get_logger

logger = get_logger("core.preferences")

PREFERENCES_FILENAME = "preferences.json"

#: Serialises the whole read-modify-write below.
#:
#: **The invariant.** Once a successful `store_many()` returns, its update
#: may not be lost by a concurrent successful update to *different* keys.
#: A reader sees the complete old document or the complete new one, never
#: a partial write.
#:
#: Without it, every writer merged into its own snapshot of the whole
#: document and then replaced the entire file, so a writer that loaded
#: before another committed silently restored what that other had changed —
#: and both returned True. The credential transaction did not help: it
#: serialises credential Save/Remove/state, while this file is also written
#: by voice settings, clap settings, the preferred name, provider selection
#: and local-AI ownership, none of which enter that gate. A save could
#: therefore put a new key in Windows Credential Manager, report
#: `PairOutcome(APPLIED)`, and have the previous Workspace ID restored
#: underneath it by somebody changing their display name.
#:
#: **This lock is a leaf.** It is taken *inside* `credential_transaction`'s
#: `_gate` and `providers._runtime_downgrade_lock` — `save()` holds the
#: gate and then writes metadata; `note_runtime_failure()` holds the gate,
#: then the runtime-note lock, then writes — so nothing reached from under
#: it may call back into either. Nothing unbounded runs inside it: no
#: network, no provider call, no model work, only JSON and one file
#: replacement. `test_the_write_lock_is_a_leaf_and_can_deadlock_with_nothing`
#: enforces that structurally, because a lock-order defect that only shows
#: under load is not something one green run can disprove.
#:
#: Re-entrant deliberately. Nesting is not expected and would be a defect,
#: but this module's contract is that it never raises and never takes the
#: application down over a settings file; a self-deadlock in a preference
#: write is a worse outcome than a redundant one.
#:
#: **Process scope.** Every runtime writer runs in the server child: the
#: FastAPI routes, the voice engines, the clap settings, the credential
#: metadata path and the local-AI installer. The tray parent and the window
#: child only ever *read* (`app/launcher/gui.py` uses `get`), a single
#: instance is enforced by `app/launcher/instance_lock.py`, and
#: `--uninstall-cleanup` runs when the application has been told to stop.
#: A process-local lock is therefore sufficient today — and the temporary
#: file below is unique per write anyway, so a second process could not
#: corrupt a document even if one ever appeared.
_write_lock = threading.RLock()

# The complete set of keys this file may hold. Anything else is refused.
#
# `preferred_name` is what JARVIS calls the user. It is the only thing
# first-run asks for besides an API key, so it has to survive a restart
# without a .env file or an environment variable — the same reason
# `ai_provider` is here.
#
# `close_action` decides whether closing the window quits JARVIS or
# leaves it in the tray. It was previously an environment-only setting
# with a control on the setup screen that was wired to nothing at all.
#
# `stt_enabled` decides whether push-to-talk is offered. It was
# environment-only too, which meant the packaged app had no way to turn
# voice input on at all — while telling users to "turn it on from the
# Voice page".
STORABLE_KEYS = (
    "ai_provider", "ollama_model", "speak_replies", "preferred_name",
    "close_action", "stt_enabled",
    # Which of the neural voices JARVIS speaks with, and how fast. Both
    # are choices made in the app, so both have to survive a restart —
    # an environment variable cannot be edited by someone who installed
    # a .exe.
    "voice_key", "voice_speed",
    # Whether JARVIS is the thing that put Ollama on this machine. Not a
    # setting anyone changes: a fact the uninstaller needs, because
    # removing software somebody else installed — because JARVIS happened
    # to be uninstalled — is the wrong answer every time.
    "ollama_installed_by_jarvis",
    # The Anthropic workspace an identity-linked key acts in. Account
    # metadata, not a credential: it identifies a workspace, it does not
    # authenticate anything, and Anthropic prints it in a Console table.
    # The key itself stays in the OS credential store, where it belongs —
    # see app/core/ai/workspace.py for the full reasoning, and note that
    # "not a secret" still does not make it something an endpoint, a log
    # or a diagnostic may return.
    "anthropic_workspace_id",
    # What was last observed about the stored key: one of
    # providers.CREDENTIAL_VERIFIED / CREDENTIAL_FAILED /
    # CREDENTIAL_UNFUNDED. Absent means "never successfully checked on
    # this installation", which is what both an upgrade from an older
    # build and a check that could not complete read as — those are not
    # rejections and must never be recorded as one. A state name, never a
    # credential.
    "anthropic_key_state",
    # The optional cloud voice. Which engine was chosen, which voice, its
    # tuning, and whether the local voice may cover for it.
    #
    # The ElevenLabs API key is deliberately NOT here and never will be:
    # this file is plain JSON in AppData, and a credential belongs in the
    # Windows Credential Manager (app/core/credentials.py). store()
    # refuses anything credential-shaped, but the real protection is that
    # there is no key here for it to refuse.
    "tts_engine", "elevenlabs_voice_id", "elevenlabs_voice_name",
    "elevenlabs_settings", "elevenlabs_fallback",
    # OpenAI Speech preferences. The credential itself is deliberately
    # absent: openai_voice_key_configured is only a non-secret status bit.
    "openai_voice_key_configured", "openai_tts_model", "openai_tts_voice",
    "openai_tts_speed", "openai_tts_instructions", "openai_tts_fallback",
    # Double-clap activation (app/voice/clap.py). Four settings, all of
    # them decisions about a room: whether to listen at all (off until
    # somebody turns it on), how loud a transient has to be, and what —
    # if anything — to say when the window appears. No audio, no levels
    # and no timestamps are ever stored here or anywhere else.
    "clap_enabled", "clap_sensitivity", "clap_greet", "clap_greeting",
    # Calibrated detector overrides, clamped to app/voice/clap.py's
    # SAFE_BOUNDS before they are ever written or read back.
    "clap_tuning",
    # Which microphone this machine uses. One choice, shared by the
    # diagnostics level meter and the clap listener, so the dropdown on
    # the Voice page is not decoration. A device id is not a credential
    # and not audio; it is the same string the browser already hands out
    # to any page with microphone permission.
    "mic_device_id",
)

# What a name is allowed to be. Deliberately generous about content and
# strict about length: this is a display string, not an identifier, and
# it ends up in a system prompt.
MAX_PREFERRED_NAME_LENGTH = 40

_TRUE_VALUES = ("true", "1", "yes", "on")
_FALSE_VALUES = ("false", "0", "no", "off")


def preferences_path() -> Path:
    return config_dir() / PREFERENCES_FILENAME


def _path_or_none() -> Optional[Path]:
    """Resolving the path can itself fail — an AppData directory that
    cannot be determined, a redirected profile — and "never raises" has
    to include that, not only the read."""
    try:
        return preferences_path()
    except Exception as exc:  # noqa: BLE001
        # Described, never rendered: an OSError from resolving an AppData
        # path quotes that path, which begins with the account name.
        logger.warning("Preferences location could not be resolved. %s", describe(exc))
        return None


def load() -> Dict[str, Any]:
    """Everything saved, filtered to the allowlist. Never raises."""
    path = _path_or_none()
    if path is None:
        return {}
    try:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — a corrupt file is "nothing saved"
        # A JSONDecodeError quotes the offending document, and this file
        # holds the workspace ID. An OSError quotes the full path.
        logger.warning("Preferences file could not be read; using defaults. %s", describe(exc))
        return {}

    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in STORABLE_KEYS if key in raw}


def get(key: str) -> Optional[str]:
    """A saved string preference, or None when unset/blank/not a string."""
    value = load().get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def get_bool(key: str) -> Optional[bool]:
    """A saved boolean preference, or None when nothing was saved.

    None is distinct from False on purpose: the caller needs to know
    whether to fall back to a configured default or honour an explicit
    "off". An unrecognised value reads as None — a settings file someone
    hand-edited to "maybe" is not a decision.
    """
    raw = get(key)
    if raw is None:
        return None
    lowered = raw.lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    return None


def store(key: str, value: Optional[str]) -> bool:
    """Save (or, with value None/blank, clear) one preference.

    Returns whether it was written. A key outside the allowlist is
    refused rather than stored, so this can never quietly become a
    browser-writable settings backdoor.
    """
    return store_many({key: value})


def store_many(values: Mapping[str, Optional[str]]) -> bool:
    """Atomically persist several allowlisted preferences.

    OpenAI voice settings are one logical choice. Writing four separate
    versions could leave a half-old profile after a disk error, so the
    complete JSON is replaced only after the temporary file is durable.
    """
    unknown = [key for key in values if key not in STORABLE_KEYS]
    if unknown:
        logger.warning("Refused to store unlisted preference keys: %r", unknown)
        return False

    path = _path_or_none()
    if path is None:
        return False

    # Load, merge and replace as **one** operation. Reading outside this
    # would put the whole defect back: two writers would each merge into a
    # snapshot taken before the other committed, and the later replacement
    # would silently restore what the earlier one had changed while both
    # returned True.
    with _write_lock:
        readable, data = _load_for_update(path)
        if not readable:
            # The file is there and could not be read *this time* — a
            # transient sharing violation, a profile that briefly went
            # away. Writing now would replace a document whose contents
            # are unknown with one built from nothing, which is how a
            # single unlucky read erases every saved preference. Refusing
            # loses this update; writing loses all of them.
            return False

        for key, value in values.items():
            if value is None or not str(value).strip():
                data.pop(key, None)
            else:
                data[key] = str(value).strip()
        return _write_document(path, data)


def _load_for_update(path: Path) -> Tuple[bool, Dict[str, Any]]:
    """`(safe_to_replace, document)` for the write path. Never raises.

    `load()` answers `{}` for a file that is missing *and* for one it could
    not read, which is the right answer for a reader — "nothing is saved"
    degrades to defaults. It is the wrong answer for a writer, where the
    two differ completely: merging into `{}` and replacing the file turns
    an unreadable document into a deleted one.

    A file that does not exist yet is `(True, {})`: there is nothing to
    lose. A file whose *content* is unparseable is also safe to replace —
    every reader already sees `{}` for it, so the preferences are gone
    already and overwriting recovers rather than destroys. Only a file that
    exists and could not be opened is refused.
    """
    try:
        if not path.exists():
            return True, {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not check for the preferences file. %s", describe(exc))
        return False, {}

    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        # Described, never rendered: an OSError quotes the full path, which
        # begins with the account name.
        logger.warning(
            "The preferences file exists but could not be read; leaving it alone "
            "rather than replacing it with an incomplete one. %s", describe(exc),
        )
        return False, {}

    try:
        parsed = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        # A JSONDecodeError quotes the offending document, and this file
        # holds the workspace ID.
        logger.warning(
            "The preferences file is not valid JSON; it already reads as empty "
            "everywhere, so this write replaces it. %s", describe(exc),
        )
        return True, {}

    if not isinstance(parsed, dict):
        return True, {}
    return True, {key: parsed[key] for key in STORABLE_KEYS if key in parsed}


def _write_document(path: Path, data: Dict[str, Any]) -> bool:
    """Replace the whole document atomically. Only called under the lock.

    The temporary file is **unique per write**. It used to be one shared
    `preferences.json.tmp`, so two writers wrote and deleted the same path:
    within a process the lock now prevents that, but a name nobody else can
    collide with removes the hazard outright — including for the uninstall
    cleanup, which runs as a different process, and for a stale temporary
    left behind by a crash.
    """
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # Atomic on both platforms, which is what lets `load()` stay
        # lock-free: a reader opens either the complete old document or the
        # complete new one, never a half-written file.
        temporary.replace(path)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not write the preferences file. %s", describe(exc))
        try:
            temporary.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return False
