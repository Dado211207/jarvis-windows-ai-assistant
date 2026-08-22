"""Write one WAV per installed voice, so a voice can be chosen by ear.

The default voice was picked from a description. That is not a way to
choose how a product sounds, and this exists so the choice can be made
by listening instead — and re-made whenever the pipeline changes, since
the normaliser and the G2P affect the result as much as the voice pack
does.

Runs the real path end to end: text, normalisation, grapheme-to-phoneme,
tokens, the model, WAV. No shortcuts, so what comes out is what a user
would actually hear.

    python scripts/make_voice_samples.py [output_dir]

Voices that are not installed are skipped by name rather than silently:
a missing voice is a thing to go and install, not an empty directory to
puzzle over.
"""

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.voice import audio  # noqa: E402
from app.voice.kokoro import assets, engine as kokoro_engine, install  # noqa: E402

# Long enough to hear rhythm and intonation rather than one word, and it
# says what the product actually says rather than a stock sentence.
SAMPLE_TEXT = (
    "Good evening. This is the voice JARVIS will use when it speaks to you. "
    "All systems are online, and the local model is running on this computer."
)

DEFAULT_OUTPUT_DIR = REPO_ROOT / "packaging" / "dist" / "voice-samples"


def main() -> int:
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    if not kokoro_engine.runtime_available():
        print("ONNX Runtime is not available, so no samples can be produced.")
        return 1

    written = 0
    for voice in assets.VOICES:
        if not install.file_is_good(voice.asset):
            print(f"{voice.key:12} not installed — skipped")
            continue

        started = time.monotonic()
        samples = kokoro_engine.engine.synthesise_all(SAMPLE_TEXT, voice_key=voice.key)
        elapsed = time.monotonic() - started

        if samples.size == 0:
            print(f"{voice.key:12} produced no audio")
            continue

        path = output_dir / f"{voice.key}-{voice.display_name}.wav"
        audio.write_wav(path, samples, kokoro_engine.SAMPLE_RATE)
        seconds = samples.size / kokoro_engine.SAMPLE_RATE
        print(
            f"{voice.key:12} {seconds:5.2f}s of audio in {elapsed:5.2f}s "
            f"(real-time factor {elapsed / seconds:4.2f})  ->  {path}"
        )
        written += 1

    if not written:
        print(f"\nNo voices are installed. Install one from the Voice page ({install.install_dir()}).")
        return 1

    print(f"\n{written} sample(s) written to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
