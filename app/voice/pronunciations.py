"""Custom pronunciations, saved between runs.

The case this exists for is the one the owner named: the name entered
during first run. No general dictionary contains "Dado", so without this
the assistant spells its user's name out at them, every time, forever.

Two things are stored per entry, deliberately. The **input** is what the
person typed — "dah-doh" — and is what they see if they come back to
change it; the **phonemes** are what that resolved to. Storing only the
resolved form would mean an entry could be read back but never sensibly
edited; storing only the input would mean re-resolving on every startup
and silently changing a working pronunciation if the lexicon ever
changed.

Not in app/core/preferences.py: that is an allowlist of known keys, and
its rules say so. This is open-ended user data with its own shape, so it
gets its own file.
"""

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.core.app_paths import config_dir
from app.logging_config import get_logger
from app.voice.kokoro.g2p import PronunciationDictionary

logger = get_logger("voice.pronunciations")

FILENAME = "pronunciations.json"

# A guard, not a policy: this is a pronunciation dictionary for names and
# product words, and a file with ten thousand entries in it is a bug or
# an attack rather than a user.
MAX_ENTRIES = 500
MAX_WORD_LENGTH = 64
MAX_INPUT_LENGTH = 200

_lock = threading.Lock()


def store_path() -> Path:
    return config_dir() / FILENAME


@dataclass
class Entry:
    word: str
    input: str
    phonemes: str


def _read_raw() -> Dict[str, dict]:
    path = store_path()
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A corrupt file must not stop JARVIS speaking; it is replaced on
        # the next successful write.
        logger.warning("Could not read %s; ignoring custom pronunciations.", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(word): value
        for word, value in data.items()
        if isinstance(value, dict) and isinstance(value.get("phonemes"), str)
    }


def entries() -> List[Entry]:
    """Everything saved, for the Voice page's list."""
    return sorted(
        (
            Entry(
                word=word,
                input=str(value.get("input") or ""),
                phonemes=str(value.get("phonemes") or ""),
            )
            for word, value in _read_raw().items()
        ),
        key=lambda entry: entry.word,
    )


def load() -> PronunciationDictionary:
    """The dictionary the synthesiser consults.

    Built from the stored phonemes rather than by re-resolving the
    inputs: what was accepted once should keep being what is said.
    """
    dictionary = PronunciationDictionary()
    for word, value in _read_raw().items():
        phonemes = str(value.get("phonemes") or "")
        if phonemes:
            # Slash-delimited so it is taken as phonemes and not read back
            # as a respelling of itself.
            dictionary.set(word, f"/{phonemes}/")
    return dictionary


def _write_raw(data: Dict[str, dict]) -> bool:
    path = store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the target and moved into place: an interrupted
        # write must not leave a half-written dictionary behind.
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8",
        )
        temporary.replace(path)
        return True
    except OSError:
        logger.warning("Could not save custom pronunciations to %s.", path, exc_info=True)
        # A failed write must not leave its scratch file behind. The
        # rename is what makes this atomic; if it did not happen, the
        # half-written file is litter in the user's config directory and
        # nothing will ever come back for it.
        try:
            path.with_suffix(".json.tmp").unlink(missing_ok=True)
        except OSError:
            pass
        return False


def resolve(word: str, spoken_as: str) -> Tuple[bool, str, str]:
    """What *spoken_as* would sound like, without saving it.

    Returns (accepted, phonemes, message). The Voice page uses this for
    its preview, so a person can hear an entry before committing to it.
    """
    word = (word or "").strip()
    spoken_as = (spoken_as or "").strip()
    if not word:
        return False, "", "Enter the word to change."
    if len(word) > MAX_WORD_LENGTH:
        return False, "", "That word is too long."
    if not spoken_as:
        return False, "", "Enter how it should be said."
    if len(spoken_as) > MAX_INPUT_LENGTH:
        return False, "", "That pronunciation is too long."

    probe = PronunciationDictionary()
    if not probe.set(word, spoken_as):
        return False, "", (
            "That pronunciation uses symbols this voice cannot say. Try spelling it "
            "out in syllables instead, like “dah-doh”."
        )
    return True, probe.get(word) or "", ""


def save_entry(word: str, spoken_as: str) -> Tuple[bool, str]:
    """Add or replace one pronunciation. Returns (saved, message)."""
    accepted, phonemes, message = resolve(word, spoken_as)
    if not accepted:
        return False, message

    key = word.strip().lower()
    with _lock:
        data = _read_raw()
        if key not in data and len(data) >= MAX_ENTRIES:
            return False, (
                f"There are already {MAX_ENTRIES} custom pronunciations saved. "
                "Remove one before adding another."
            )
        data[key] = {"input": spoken_as.strip(), "phonemes": phonemes}
        if not _write_raw(data):
            return False, "The pronunciation could not be saved."
    return True, f"“{word.strip()}” will be said as “{spoken_as.strip()}”."


def remove_entry(word: str) -> bool:
    key = (word or "").strip().lower()
    with _lock:
        data = _read_raw()
        if key not in data:
            return False
        del data[key]
        return _write_raw(data)


def get_entry(word: str) -> Optional[Entry]:
    key = (word or "").strip().lower()
    for entry in entries():
        if entry.word == key:
            return entry
    return None


def name_needs_pronunciation(name: str) -> bool:
    """Whether the name entered at first run would be spelled out.

    Used to offer the setting rather than to guess at it: proposing a
    pronunciation nobody asked for would be inventing how someone's name
    sounds, which is exactly the thing worth not doing.
    """
    from app.voice.kokoro import g2p
    from app.voice.kokoro.lexicon import lookup

    word = (name or "").strip().lower()
    if not word or not word.isalpha():
        return False
    if get_entry(word) is not None:
        return False
    return not (lookup(word) or g2p.BUILTIN_PRONUNCIATIONS.get(word))
