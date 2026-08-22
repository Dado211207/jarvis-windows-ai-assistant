"""Generate JARVIS's pronunciation lexicon from the CMU Pronouncing
Dictionary.

Run manually when the pinned upstream data changes; the output is checked
into the repository so the application never downloads a dictionary and
works offline from the moment it is installed.

    python scripts/build_lexicon.py path/to/cmudict.dict

Every source file is verified against the SHA-256 recorded in
app/voice/kokoro/lexicon_source.py before a single line is read — the
whole point of pinning is lost if the pin is not checked.

Acknowledgement, as the upstream licence asks: this lexicon is derived
from the CMU Pronouncing Dictionary, created at Carnegie Mellon
University. See docs/licences/CMUDICT-LICENSE.txt.
"""

import gzip
import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.voice.kokoro.g2p import arpabet_to_ipa, validate_phonemes  # noqa: E402
from app.voice.kokoro.lexicon_source import SOURCE_FILES  # noqa: E402

OUTPUT = REPO_ROOT / "app" / "voice" / "kokoro" / "data" / "lexicon.txt.gz"


def verify(path: Path) -> None:
    expected = {source.path: source for source in SOURCE_FILES}.get(path.name)
    if expected is None:
        raise SystemExit(f"{path.name} is not a pinned source file.")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected.sha256:
        raise SystemExit(
            f"{path.name} does not match its pinned SHA-256.\n"
            f"  expected {expected.sha256}\n  got      {digest}\n"
            "Refusing to build a lexicon from unverified data."
        )
    print(f"verified {path.name} ({path.stat().st_size} bytes)")


def build(dict_path: Path) -> None:
    verify(dict_path)

    entries = {}
    skipped = 0
    for line in dict_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        word, symbols = parts[0], parts[1:]
        # "word(2)" is an alternative pronunciation; the first is the one
        # people mean by default, so alternatives are dropped rather than
        # overwriting it.
        if "(" in word:
            continue
        phonemes = arpabet_to_ipa(symbols)
        ok, unknown = validate_phonemes(phonemes)
        if not ok:
            skipped += 1
            print(f"  skipping {word!r}: unusable symbols {unknown!r}")
            continue
        entries[word.lower()] = phonemes

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(f"{word}\t{phonemes}\n" for word, phonemes in sorted(entries.items()))
    with gzip.open(OUTPUT, "wb", compresslevel=9) as out:
        out.write(payload.encode("utf-8"))

    print(f"\n{len(entries)} words -> {OUTPUT}")
    print(f"{OUTPUT.stat().st_size / 1024:.0f} KB compressed")
    if skipped:
        raise SystemExit(f"{skipped} entries had unusable symbols — fix the mapping in g2p.py.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    build(Path(sys.argv[1]))
