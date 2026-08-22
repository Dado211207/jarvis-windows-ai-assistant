"""Where JARVIS's pronunciation data comes from, pinned and verifiable.

The lexicon shipped in `app/voice/kokoro/data/` is generated from the CMU
Pronouncing Dictionary by `scripts/build_lexicon.py`. This module records
exactly which upstream files that generation used, so the derived data can
be re-checked against its source rather than trusted.

**Acknowledgement**, as the upstream licence asks for: JARVIS's
pronunciation lexicon is derived from the CMU Pronouncing Dictionary,
created at Carnegie Mellon University. The licence text is reproduced
verbatim at `docs/licences/CMUDICT-LICENSE.txt` and ships with the
application.

**On how this is pinned.** The intent was to pin an upstream commit SHA.
This environment's network proxy blocks the GitHub API for repositories
outside the session's own scope, so a commit SHA could not be read and
would have had to be invented — which is worse than useless in a
provenance record. The pin is therefore content-addressed: the SHA-256 of
each exact file used. For verification purposes that is the stronger of
the two, since it constrains the bytes rather than a reference that could
be repointed; what it does not carry is a human-readable "which revision",
and that limitation is stated rather than papered over.

**On the licence label.** This data is *not* described here by an SPDX
identifier. The upstream text is CMU's own, and while it reads like a
two-clause BSD licence it is not literally that licence and carries an
acknowledgement request BSD does not. Labelling it with an SPDX id would
be a small convenience bought with a factual misstatement, so the
verbatim text is what ships and what is referenced.

**On the GPL wrapper.** The `cmudict` package on PyPI is
GPL-3.0-or-later. The dictionary it distributes is not. Installing that
package purely to obtain the data would pull a GPL component into the
dependency set for no reason, so the data is taken from upstream directly
and converted by a script in this repository.
"""

from dataclasses import dataclass
from typing import Tuple

UPSTREAM_REPO = "https://github.com/cmusphinx/cmudict"
LICENCE_FILE = "docs/licences/CMUDICT-LICENSE.txt"
ACKNOWLEDGEMENT = (
    "JARVIS's pronunciation lexicon is derived from the CMU Pronouncing "
    "Dictionary, created at Carnegie Mellon University."
)


@dataclass(frozen=True)
class SourceFile:
    """One upstream file, pinned by the hash of its exact contents."""

    path: str
    size_bytes: int
    sha256: str

    def url(self) -> str:
        return f"https://raw.githubusercontent.com/cmusphinx/cmudict/master/{self.path}"


SOURCE_FILES: Tuple[SourceFile, ...] = (
    SourceFile(
        path="cmudict.dict",
        size_bytes=3618488,
        sha256="81917843c7f44ce2b094ac63873c2c7a4cf802040792c455ba3ca406891c3d22",
    ),
    SourceFile(
        path="cmudict.symbols",
        size_bytes=281,
        sha256="408ccaae803641c6d7b626b6299949320c2dbca96b2220fd3fb17887b023b027",
    ),
    SourceFile(
        path="cmudict.phones",
        size_bytes=382,
        sha256="ffb588a5e55684723582c7256e1d2f9fadb130011392d9e59237c76e34c2cfd6",
    ),
)

LICENCE_SHA256 = "bd4ce8e44170a5f9f481310ca85c51de3c4f851a65e679b40e603b143bd3542a"

# The complete ARPAbet inventory the upstream dictionary uses, read from
# its own cmudict.symbols rather than from documentation. Every one of
# these must have a mapping in g2p.py, which a test enforces — an
# unmapped symbol would silently drop a sound from a word.
ARPABET_SYMBOLS: Tuple[str, ...] = (
    "AA", "AE", "AH", "AO", "AW", "AY", "B", "CH", "D", "DH",
    "EH", "ER", "EY", "F", "G", "HH", "IH", "IY", "JH", "K",
    "L", "M", "N", "NG", "OW", "OY", "P", "R", "S", "SH",
    "T", "TH", "UH", "UW", "V", "W", "Y", "Z", "ZH",
)
