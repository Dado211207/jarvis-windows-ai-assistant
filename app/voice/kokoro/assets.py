"""What JARVIS's voice is made of, where each piece comes from, and under
which licence.

Every entry here is a real, verified fact — the sizes and SHA-256 digests
were read from Hugging Face's own API for the exact revision named below,
not copied from documentation and not invented. Nothing is downloaded
without matching one of these digests.

**The licence constraint is the design constraint.** The owner's decision
was explicit: the complete distributed dependency chain stays permissive,
no GPL component is bundled or invoked, and Piper (GPL-3.0-or-later since
its rewrite) is out. That rules out the usual Kokoro plumbing too:

  - `kokoro-onnx` depends on `espeakng-loader` and `phonemizer-fork`,
    which exist to drive espeak-ng (GPL-3.0). Not used.
  - `misaki`'s English G2P is Apache-2.0 but falls back to espeak-ng for
    out-of-vocabulary words. Not used.
  - the `cmudict` *PyPI package* is GPL-3.0-or-later, even though the
    dictionary it wraps is not. Not used — see g2p.py, which reads the
    upstream CMU data (BSD-2-Clause, Carnegie Mellon University)
    directly.

So the shipped chain is: onnxruntime (MIT) + the Kokoro model and voices
(Apache-2.0) + CMU's dictionary data (BSD-2-Clause) + this project's own
code. Nothing here is copyleft.

**On the voice identity.** `bm_george` is an original voice from the
Kokoro model, released under Apache-2.0. JARVIS presents it under its own
name with its own description. It is not a clone of, and is never
described as, any particular performer's voice — the owner's instruction,
and the only defensible position for a distributed product.
"""

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# Pinned to a specific revision, not `main`: a moving reference would
# make the digests below meaningless the moment the repository changed.
MODEL_REPO = "onnx-community/Kokoro-82M-v1.0-ONNX"
MODEL_REVISION = "1939ad2a8e416c0acfeecc08a694d14ef25f2231"
MODEL_LICENCE = "Apache-2.0"
MODEL_SOURCE_URL = f"https://huggingface.co/{MODEL_REPO}"
_RESOLVE = "https://huggingface.co/{repo}/resolve/{revision}/{path}"

# The quantized build, deliberately. At 88 MB it is a third of the fp32
# model's 310 MB, runs on CPU on the machine this was reported against
# (a Ryzen 7 2700X with no GPU requirement), and the quality difference
# for speech is not what anyone would notice — the difference between
# this and the old SAPI5 voice is.
MODEL_FILENAME = "model_quantized.onnx"


@dataclass(frozen=True)
class RemoteAsset:
    """One downloadable file, with everything needed to verify it."""

    path: str          # path within the model repository
    filename: str      # what it is called on disk once installed
    size_bytes: int
    sha256: str

    def url(self) -> str:
        return _RESOLVE.format(repo=MODEL_REPO, revision=MODEL_REVISION, path=self.path)


MODEL_ASSET = RemoteAsset(
    path="onnx/model_quantized.onnx",
    filename=MODEL_FILENAME,
    size_bytes=92361116,
    sha256="fbae9257e1e05ffc727e951ef9b9c98418e6d79f1c9b6b13bd59f5c9028a1478",
)


@dataclass(frozen=True)
class Voice:
    """A selectable voice.

    `display_name` and `description` are JARVIS's own presentation of an
    Apache-2.0 voice, never a claim about whose voice it sounds like.
    """

    key: str
    display_name: str
    description: str
    asset: RemoteAsset


def _voice(key: str, display: str, description: str, sha256: str) -> Voice:
    return Voice(
        key=key,
        display_name=display,
        description=description,
        asset=RemoteAsset(
            path=f"voices/{key}.bin",
            filename=f"{key}.bin",
            # Every voice pack in this release is the same 510 KiB of
            # style vectors.
            size_bytes=522240,
            sha256=sha256,
        ),
    )


# British male voices only. The brief was a calm, measured British male
# presentation; offering the whole model's range would be a list to scroll
# rather than a choice to make.
VOICES: Tuple[Voice, ...] = (
    _voice(
        "bm_george", "George",
        "Measured and warm. JARVIS's default.",
        "c4b235a4c1f2cd3b939fed08b899ce9385638b763f7b73a59616c4fc9bd6c9bc",
    ),
    _voice(
        "bm_daniel", "Daniel",
        "Lighter and quicker, for shorter replies.",
        "6b3194bbceffb746733cbc22c8f593dd44e401a71d53895a2dca891bc595a1e8",
    ),
    _voice(
        "bm_lewis", "Lewis",
        "Deeper, with more weight on each phrase.",
        "b8f671cef828c30e66fdf0b0756a76bba58f6bb3398cbbf27058642acbcedb97",
    ),
    _voice(
        "bm_fable", "Fable",
        "Brighter and more expressive.",
        "f889083196807b4adb15e9204252165f503b8d33d3982e681c52443c49d798f1",
    ),
)

DEFAULT_VOICE_KEY = "bm_george"

VOICES_BY_KEY: Dict[str, Voice] = {voice.key: voice for voice in VOICES}


def find_voice(key: str) -> Optional[Voice]:
    return VOICES_BY_KEY.get((key or "").strip())


def resolve_voice(key: str) -> Voice:
    """The requested voice, or the default. Never raises: an unknown key
    saved by an older build must not stop JARVIS speaking."""
    return find_voice(key) or VOICES_BY_KEY[DEFAULT_VOICE_KEY]


def total_download_bytes(voice_key: str = DEFAULT_VOICE_KEY) -> int:
    """What a first-time install actually costs, so the figure shown to
    the user is computed rather than remembered."""
    return MODEL_ASSET.size_bytes + resolve_voice(voice_key).asset.size_bytes


# The manifest surfaced in the UI and in docs/THIRD_PARTY_NOTICES.md.
# Kept as data so the licence page and the download screen cannot drift
# apart from each other.
LICENCE_MANIFEST = (
    {
        "component": "Kokoro 82M (ONNX, quantized)",
        "role": "Neural speech synthesis model",
        "licence": MODEL_LICENCE,
        "source": MODEL_SOURCE_URL,
        "distributed": "Downloaded on request, verified by SHA-256",
    },
    {
        "component": "Kokoro voice packs (bm_*)",
        "role": "British male voice styles",
        "licence": MODEL_LICENCE,
        "source": MODEL_SOURCE_URL,
        "distributed": "Downloaded on request, verified by SHA-256",
    },
    {
        "component": "ONNX Runtime",
        "role": "Runs the model on the CPU",
        "licence": "MIT",
        "source": "https://github.com/microsoft/onnxruntime",
        "distributed": "Bundled in the installer",
    },
    {
        "component": "CMU Pronouncing Dictionary",
        "role": "Word to phoneme lookup",
        "licence": "BSD-2-Clause",
        "source": "https://github.com/cmusphinx/cmudict",
        "distributed": "Bundled in the installer",
    },
)
