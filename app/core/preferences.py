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
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.app_paths import config_dir
from app.logging_config import get_logger

logger = get_logger("core.preferences")

PREFERENCES_FILENAME = "preferences.json"

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
    except Exception:  # noqa: BLE001
        logger.warning("Preferences location could not be resolved.", exc_info=True)
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
    except Exception:  # noqa: BLE001 — a corrupt file is "nothing saved"
        logger.warning("Preferences file could not be read; using defaults.", exc_info=True)
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
    if key not in STORABLE_KEYS:
        logger.warning("Refused to store an unlisted preference key: %r", key)
        return False

    data = load()
    if value is None or not str(value).strip():
        data.pop(key, None)
    else:
        data[key] = str(value).strip()

    path = _path_or_none()
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        logger.warning("Could not write the preferences file.", exc_info=True)
        return False
