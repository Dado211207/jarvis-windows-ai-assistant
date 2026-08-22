"""Kokoro's phoneme-to-token table, read from the pinned model.

Not from documentation and not reconstructed from an upstream script:
these are the exact contents of `tokenizer.json` at the revision named in
assets.py, whose own SHA-256 is recorded below. The published Kokoro
symbol list is longer than this and numbered differently; using it would
send this model token IDs that mean something else, and the failure would
be audible nonsense rather than an error.

The IDs are sparse (0-177 across 115 tokens) because the vocabulary is a
subset of a larger symbol space. That is why this is a table and not an
index into a string.
"""

from typing import List, Tuple

# huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX at revision
# 1939ad2a8e416c0acfeecc08a694d14ef25f2231, file tokenizer.json,
# sha256 77a02c8e164413299b4b4c403b14f8e0e1c1b727db4d46a09d6327b861060a34
TOKENIZER_SHA256 = "77a02c8e164413299b4b4c403b14f8e0e1c1b727db4d46a09d6327b861060a34"

# The padding token that opens and closes every sequence.
PAD_TOKEN = "$"

_VOCAB_PAIRS: Tuple[Tuple[str, int], ...] = (
    ('$', 0), (';', 1), (':', 2), (',', 3),
    ('.', 4), ('!', 5), ('?', 6), ('—', 9),
    ('…', 10), ('"', 11), ('(', 12), (')', 13),
    ('“', 14), ('”', 15), (' ', 16), ('̃', 17),
    ('ʣ', 18), ('ʥ', 19), ('ʦ', 20), ('ʨ', 21),
    ('ᵝ', 22), ('ꭧ', 23), ('A', 24), ('I', 25),
    ('O', 31), ('Q', 33), ('S', 35), ('T', 36),
    ('W', 39), ('Y', 41), ('ᵊ', 42), ('a', 43),
    ('b', 44), ('c', 45), ('d', 46), ('e', 47),
    ('f', 48), ('h', 50), ('i', 51), ('j', 52),
    ('k', 53), ('l', 54), ('m', 55), ('n', 56),
    ('o', 57), ('p', 58), ('q', 59), ('r', 60),
    ('s', 61), ('t', 62), ('u', 63), ('v', 64),
    ('w', 65), ('x', 66), ('y', 67), ('z', 68),
    ('ɑ', 69), ('ɐ', 70), ('ɒ', 71), ('æ', 72),
    ('β', 75), ('ɔ', 76), ('ɕ', 77), ('ç', 78),
    ('ɖ', 80), ('ð', 81), ('ʤ', 82), ('ə', 83),
    ('ɚ', 85), ('ɛ', 86), ('ɜ', 87), ('ɟ', 90),
    ('ɡ', 92), ('ɥ', 99), ('ɨ', 101), ('ɪ', 102),
    ('ʝ', 103), ('ɯ', 110), ('ɰ', 111), ('ŋ', 112),
    ('ɳ', 113), ('ɲ', 114), ('ɴ', 115), ('ø', 116),
    ('ɸ', 118), ('θ', 119), ('œ', 120), ('ɹ', 123),
    ('ɾ', 125), ('ɻ', 126), ('ʁ', 128), ('ɽ', 129),
    ('ʂ', 130), ('ʃ', 131), ('ʈ', 132), ('ʧ', 133),
    ('ʊ', 135), ('ʋ', 136), ('ʌ', 138), ('ɣ', 139),
    ('ɤ', 140), ('χ', 142), ('ʎ', 143), ('ʒ', 147),
    ('ʔ', 148), ('ˈ', 156), ('ˌ', 157), ('ː', 158),
    ('ʰ', 162), ('ʲ', 164), ('↓', 169), ('→', 171),
    ('↗', 172), ('↘', 173), ('ᵻ', 177),
)

VOCAB = dict(_VOCAB_PAIRS)
PAD_ID = VOCAB[PAD_TOKEN]

# The voice style packs hold 510 vectors, indexed by token count, so 510
# is the hard ceiling on one inference — not a tuning choice.
MAX_TOKENS = 510


def encode(phonemes: str) -> List[int]:
    """Phoneme string to model token IDs, wrapped in the pad token.

    Characters outside the vocabulary are dropped rather than mapped to
    a fallback ID: every ID in this table means a specific sound, so
    there is no spare one to mean "unknown". g2p.validate_phonemes()
    is the check that catches this before it gets here; this is the
    boundary that makes it impossible regardless.
    """
    return [PAD_ID] + [VOCAB[ch] for ch in phonemes if ch in VOCAB] + [PAD_ID]


def unsupported(phonemes: str) -> List[str]:
    """Which characters encode() would have to drop. Empty is the
    expected answer for anything g2p produced."""
    return sorted({ch for ch in phonemes if ch not in VOCAB})
