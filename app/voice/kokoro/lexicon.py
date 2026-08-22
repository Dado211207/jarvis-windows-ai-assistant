"""The bundled pronunciation lexicon.

126,052 words, derived from the CMU Pronouncing Dictionary and converted
to Kokoro's British-English phoneme inventory by
`scripts/build_lexicon.py`. Shipped in the repository and the installer
rather than downloaded: a voice that needs the network to pronounce
"hello" is not a local voice, and the owner's requirement is that speech
works offline once installed.

Acknowledgement, as the upstream licence asks: derived from the CMU
Pronouncing Dictionary, created at Carnegie Mellon University. The
verbatim licence is at docs/licences/CMUDICT-LICENSE.txt and ships with
the application.

Loaded lazily and once. 799 KB compressed is small enough to keep
resident and far too much to re-read per word.
"""

import gzip
import threading
from pathlib import Path
from typing import Dict, Optional

from app.logging_config import get_logger

logger = get_logger("voice.kokoro.lexicon")

DATA_FILENAME = "lexicon.txt.gz"

_lexicon: Optional[Dict[str, str]] = None
_lock = threading.Lock()


def data_path() -> Path:
    """Beside this module, so PyInstaller's collect_all picks it up with
    the package rather than needing its own --add-data rule."""
    return Path(__file__).resolve().parent / "data" / DATA_FILENAME


def _load() -> Dict[str, str]:
    path = data_path()
    entries: Dict[str, str] = {}
    try:
        with gzip.open(path, "rb") as source:
            for raw in source:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                word, _, phonemes = line.partition("\t")
                if word and phonemes:
                    entries[word] = phonemes
    except OSError:
        # A missing lexicon is a broken install, not a crash. g2p falls
        # back to spelling words out, which is worse but still speech.
        logger.error("Could not read the pronunciation lexicon at %s.", path, exc_info=True)
        return {}
    logger.info("Pronunciation lexicon loaded: %d words.", len(entries))
    return entries


def ensure_loaded() -> Dict[str, str]:
    global _lexicon
    if _lexicon is None:
        with _lock:
            if _lexicon is None:
                _lexicon = _load()
    return _lexicon


def lookup(word: str) -> Optional[str]:
    """Phonemes for one word, or None. Case-insensitive: the dictionary
    is lower-cased, and callers pass whatever the user typed."""
    if not word:
        return None
    return ensure_loaded().get(word.strip().lower())


def size() -> int:
    return len(ensure_loaded())


def is_available() -> bool:
    return size() > 0
