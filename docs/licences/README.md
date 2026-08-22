# Third-party licence texts, verbatim

Each file here is the upstream licence exactly as published, copied
without edit. They are kept in the repository, bundled by the installer,
and reachable from the running application's About page so the obligation
travels with the product rather than with a link.

| File | Component | Pinned by |
|---|---|---|
| `CMUDICT-LICENSE.txt` | CMU Pronouncing Dictionary (data) | SHA-256 of each data file — see `app/voice/kokoro/lexicon_source.py` |

## Acknowledgement

The CMU licence asks for acknowledgement of the dictionary's origin.
JARVIS's pronunciation lexicon is derived from the CMU Pronouncing
Dictionary, created at Carnegie Mellon University. That acknowledgement
appears in the application's About page, in
`docs/THIRD_PARTY_NOTICES.md`, and in the source of the module that uses
it.

## What is deliberately not here

No GPL-licensed component is bundled, imported, invoked, or downloaded by
the distributed product. That includes packages whose *wrapper* is GPL
even when the data they carry is not — see `app/voice/kokoro/assets.py`
for the specific cases and how each was resolved.
