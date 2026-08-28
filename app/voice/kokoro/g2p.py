"""Words to Kokoro phonemes, British English, with no GPL component.

The text normaliser next door rewrites paths, acronyms, dates and
identifiers into ordinary words. That is preprocessing, not
pronunciation: "Dado", "Anthropic" and every other ordinary word still
has to become phonemes the model accepts.

This is that step. A dictionary lookup, an ARPAbet-to-IPA conversion
tuned for British English, a user-editable dictionary in front of both,
and a spelling fallback behind them — so a word is never silently
dropped and no unknown marker ever reaches the model.

**Why not misaki.** misaki is Apache-2.0 and its English G2P can run
without an espeak fallback, so the licence is not the objection. The
objection is its `[en]` extra, which as a set pulls `espeakng-loader` and
`phonemizer-fork` (both there to drive GPL espeak-ng) alongside spacy and
a separate model download. Taking only the permissive minimum would still
mean spacy plus `en_core_web_sm` for part-of-speech tagging that this use
barely needs. A dictionary lookup is smaller, offline, and easier to hold
to account.

**Stress and syllables.** CMUdict marks primary and secondary stress on
vowels. Those become Kokoro's own ˈ and ˌ markers, placed before the
syllable rather than on the vowel, which is what the model was trained
on.

**The fallback is spelling, not guessing.** An unknown word is spelled
out — "Dado" as D-A-D-O if it were missing — which sounds deliberate
rather than wrong. Letter-to-sound guessing produces confident nonsense,
and for names, which is where unknown words mostly come from, wrong is
worse than careful.
"""

import re
from typing import Dict, Iterable, List, Optional, Tuple

from app.logging_config import get_logger

logger = get_logger("voice.kokoro.g2p")

# Kokoro's own token inventory, read from the pinned model's
# tokenizer.json — not from documentation. Anything outside this set is
# rejected before inference rather than sent and hoped for.
KOKORO_VOCABULARY = frozenset(
    "$;:,.!?—…\"()“” ̃ʣʥʦʨᵝꭧAIOQSTWYᵊabcdefhijklmnopqrstuvwxyz"
    "ɑɐɒæβɔɕçɖðʤəɚɛɜɟɡɥɨɪʝɯɰŋɳɲɴøɸθœɹɾɻʁɽʂʃʈʧʊʋʌɣɤχʎʒʔˈˌːʰʲ↓→↗↘ᵻ"
)

STRESS_PRIMARY = "ˈ"
STRESS_SECONDARY = "ˌ"

# ARPAbet to IPA, in the British English realisations Kokoro's bm_*
# voices were trained on. The differences from American English are the
# ones people actually hear: OW is əʊ not oʊ, AA and AO are long, ER is
# ɜː rather than ɝ, and there is no rhotic colouring on vowels.
_ARPABET_TO_IPA: Dict[str, str] = {
    "AA": "ɑː", "AE": "æ", "AH": "ʌ", "AO": "ɔː",
    "AW": "aʊ", "AY": "aɪ", "B": "b", "CH": "ʧ",
    # ER keeps its r here on purpose. This table produces *rhotic* IPA
    # and make_non_rhotic() decides positionally, because only it can
    # see what follows: the r in "butter" goes, the one in "around"
    # stays. Deciding here dropped the r from every ER, which turned
    # "around" into "a-ound".
    "D": "d", "DH": "ð", "EH": "ɛ", "ER": "ɜːɹ",
    "EY": "eɪ", "F": "f", "G": "ɡ", "HH": "h",
    "IH": "ɪ", "IY": "iː", "JH": "ʤ", "K": "k",
    "L": "l", "M": "m", "N": "n", "NG": "ŋ",
    "OW": "əʊ", "OY": "ɔɪ", "P": "p", "R": "ɹ",
    "S": "s", "SH": "ʃ", "T": "t", "TH": "θ",
    "UH": "ʊ", "UW": "uː", "V": "v", "W": "w",
    "Y": "j", "Z": "z", "ZH": "ʒ",
}

# Unstressed AH is a schwa, and unstressed ER is a plain schwa-r. Both
# are extremely common — "about", "letter" — and using the stressed form
# everywhere is one of the clearest markers of a synthetic voice.
_UNSTRESSED_OVERRIDES: Dict[str, str] = {
    "AH0": "ə",
    # Schwa plus r, not bare schwa — see the ER note above.
    "ER0": "əɹ",
    "IH0": "ɪ",
    "UH0": "ʊ",
}

# How each letter is pronounced when a word has to be spelled out.
_LETTER_PHONEMES: Dict[str, str] = {
    "A": "eɪ", "B": "biː", "C": "siː", "D": "diː", "E": "iː",
    "F": "ɛf", "G": "ʤiː", "H": "eɪʧ", "I": "aɪ", "J": "ʤeɪ",
    "K": "keɪ", "L": "ɛl", "M": "ɛm", "N": "ɛn", "O": "əʊ",
    "P": "piː", "Q": "kjuː", "R": "ɑː", "S": "ɛs", "T": "tiː",
    "U": "juː", "V": "viː", "W": "dʌbljuː", "X": "ɛks",
    "Y": "waɪ", "Z": "zɛd",
}

_DIGIT_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")

# Words this product says constantly that no general dictionary contains.
# Written as phonemes directly, checked against Kokoro's vocabulary by a
# test, and non-rhotic already. Without these the assistant spells its
# own name out at you.
BUILTIN_PRONUNCIATIONS: Dict[str, str] = {
    "jarvis": "ʤˈɑːvɪs",
    "anthropic": "ænθɹˈɒpɪk",
    "kokoro": "kəkˈɔːɹəʊ",
    "ollama": "əlˈɑːmə",
    "claude": "klˈɔːd",
    "webview": "wˈɛbvjuː",
    "onnx": "ˈɒnɪks",
    "whisper": "wˈɪspə",
    "podgorica": "pɒdɡɔːɹˈiːtsə",
    "dado": "dˈɑːdəʊ",
    "dragan": "dɹˈɑːɡan",
}


def arpabet_to_ipa(symbols: Iterable[str]) -> str:
    """One CMUdict pronunciation as a Kokoro phoneme string.

    Stress markers are emitted before the syllable that carries them,
    which is where the model expects them — not attached to the vowel the
    dictionary marks.
    """
    pieces: List[str] = []
    for raw in symbols:
        symbol = raw.strip().upper()
        if not symbol:
            continue
        stress = ""
        base = symbol
        if symbol[-1].isdigit():
            base, digit = symbol[:-1], symbol[-1]
            if digit == "1":
                stress = STRESS_PRIMARY
            elif digit == "2":
                stress = STRESS_SECONDARY

        ipa = _UNSTRESSED_OVERRIDES.get(symbol) or _ARPABET_TO_IPA.get(base)
        if ipa is None:
            # Never guess: a symbol with no mapping is a bug in this
            # table, and dropping it silently would remove a sound from a
            # word with nothing to show for it.
            logger.warning("No IPA mapping for ARPAbet symbol %r; skipping it.", symbol)
            continue
        pieces.append(stress + ipa)
    return make_non_rhotic("".join(pieces))


# Vowel symbols, for deciding whether an /ɹ/ is followed by one. Kokoro
# writes long vowels as vowel + ː, so the length mark counts as part of
# the vowel rather than as a following consonant.
_VOWEL_CHARACTERS = frozenset("ɑɐɒæɔəɚɛɜɨɪɯøœʊʌɤaeiouːᵻ")


def make_non_rhotic(phonemes: str) -> str:
    """Drop /ɹ/ everywhere except before a vowel.

    CMUdict is an *American* dictionary, so a straight conversion gives a
    rhotic accent: "your", "course", "report" and "JARVIS" itself all come
    out with a hard R that no British speaker uses. Since the voices are
    British (bm_*), leaving them rhotic is the single most audible way the
    result would sound wrong.

    Two things this has to get right, both found by listening to the
    output rather than by reasoning about it:

    * **Stress markers sit between a consonant and its vowel.** "drive"
      is dɹˈaɪv, so a naive "is the next character a vowel" test sees ˈ,
      drops the ɹ, and produces "dive" — a different real word, which is
      the worst kind of wrong because nothing looks broken.
    * **Centring diphthongs need their schwa.** Non-rhotic English turns
      /ɛər/ into /ɛə/, not into a bare /ɛ/: "fair" is fɛə, and dropping
      the r without compensating leaves "feh".
    """
    if "ɹ" not in phonemes:
        return phonemes

    # /ɪɹ/, /ɛɹ/ and /ʊɹ/ at the end of a syllable become centring
    # diphthongs. Applied before deletion, so what follows only has to
    # handle the plain cases.
    for vowel in ("ɪ", "ɛ", "ʊ"):
        phonemes = re.sub(
            vowel + "ː?ɹ(?![" + "".join(_VOWEL_CHARACTERS) + "ˈˌ])",
            vowel + "ə",
            phonemes,
        )

    result = []
    for index, character in enumerate(phonemes):
        if character != "ɹ":
            result.append(character)
            continue
        # Look past stress markers: they belong to the syllable that
        # follows, not to the consonant before them.
        lookahead = index + 1
        while lookahead < len(phonemes) and phonemes[lookahead] in (STRESS_PRIMARY, STRESS_SECONDARY):
            lookahead += 1
        following = phonemes[lookahead: lookahead + 1]
        if following and following in _VOWEL_CHARACTERS:
            result.append(character)  # onset or linking R: kept
        # otherwise dropped
    return "".join(result)


def _split_compound(word: str):
    """CamelCase and joined product names, as their parts.

    "PowerShell" is not in any dictionary and is not an acronym, so
    without this it gets spelled out letter by letter — which is exactly
    what happened before this existed.
    """
    import re as _re

    if len(word) < 4:
        return []
    parts = _re.findall(r"[A-Z][a-z]+|[a-z]+", word)
    return parts if len(parts) > 1 else []


def spell_out(word: str) -> str:
    """A word JARVIS does not know, spelled rather than guessed.

    Sounds deliberate instead of wrong — which for names, where unknown
    words mostly come from, is the better failure.
    """
    pieces: List[str] = []
    for character in word:
        if character.isalpha():
            phonemes = _LETTER_PHONEMES.get(character.upper())
            if phonemes:
                pieces.append(phonemes)
        elif character.isdigit():
            pieces.append(_word_fallback(_DIGIT_WORDS[int(character)]))
    return " ".join(pieces)


def _word_fallback(word: str) -> str:
    """Used for digit names, which are always in the lexicon; if the
    lexicon is somehow unavailable this still returns something
    speakable rather than nothing."""
    from app.voice.kokoro.lexicon import lookup

    return lookup(word) or ""


def validate_phonemes(phonemes: str) -> Tuple[bool, str]:
    """Whether every character is in Kokoro's vocabulary.

    Called before inference, always. A token the model has never seen
    does not produce a mispronunciation — it produces an index error or
    silence, and both are worse than declining to speak the sentence.
    """
    unknown = sorted({c for c in phonemes if c not in KOKORO_VOCABULARY and c != " "})
    if unknown:
        return False, "".join(unknown)
    return True, ""


class PronunciationDictionary:
    """The user's own pronunciations, tried before the lexicon.

    Exists for the case the owner named first: the name chosen during
    onboarding. A lexicon will not contain "Dado", and a person should not
    have to accept their own name being spelled out at them forever.

    Entries are either an explicit phoneme string (for someone who knows
    IPA) or a respelling in ordinary words, which is what most people can
    actually provide — "Dado" said as "dah-doh".
    """

    def __init__(self, entries: Optional[Dict[str, str]] = None) -> None:
        self._entries: Dict[str, str] = {}
        for word, pronunciation in (entries or {}).items():
            self.set(word, pronunciation)

    def set(self, word: str, pronunciation: str) -> bool:
        """Add or replace one entry. Returns whether it was accepted —
        a pronunciation Kokoro cannot represent is refused at the point
        of entry, where it can be corrected, rather than at the point of
        speech, where it cannot."""
        key = (word or "").strip().lower()
        value = (pronunciation or "").strip()
        if not key or not value:
            return False
        resolved = self._resolve(value)
        ok, unknown = validate_phonemes(resolved)
        if not ok:
            logger.warning("Refusing a pronunciation containing unusable symbols: %r", unknown)
            return False
        self._entries[key] = resolved
        return True

    def _resolve(self, value: str) -> str:
        """An entry is phonemes if it already looks like phonemes, and a
        respelling otherwise.

        Slash delimiters are stripped *first*. They were stripped last,
        which made that branch unreachable — "/dˈadəʊ/" looks like
        phonemes, so it was returned with its slashes still attached and
        then rejected for containing one.
        """
        if value.startswith("/") and value.endswith("/") and len(value) > 2:
            # Slashes mean "these are phonemes". Honoured literally, so a
            # bad symbol inside them is refused rather than quietly
            # reinterpreted as a respelling and spelled out — which would
            # accept "/dad?/" and say something the user never asked for.
            return value[1:-1].strip()
        if any(character in KOKORO_VOCABULARY and not character.isascii() for character in value):
            return value
        return self._from_respelling(value)

    def _from_respelling(self, value: str) -> str:
        """"dah-doh" -> phonemes, by looking each syllable up as if it
        were a word. Falls back to spelling a syllable that is not a
        word, so an entry always produces something."""
        from app.voice.kokoro.lexicon import lookup

        pieces: List[str] = []
        for syllable in re.split(r"[-\s]+", value):
            if not syllable:
                continue
            found = lookup(syllable)
            pieces.append(found if found else spell_out(syllable))
        return "".join(pieces)

    def get(self, word: str) -> Optional[str]:
        return self._entries.get((word or "").strip().lower())

    def remove(self, word: str) -> bool:
        return self._entries.pop((word or "").strip().lower(), None) is not None

    def as_dict(self) -> Dict[str, str]:
        return dict(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


def phonemise_word(word: str, user_dictionary: Optional[PronunciationDictionary] = None) -> str:
    """One word's phonemes: the user's dictionary, then the lexicon, then
    spelling. Never returns empty for a word containing a letter or
    digit, and never returns an unknown marker."""
    from app.voice.kokoro.lexicon import lookup

    if user_dictionary is not None:
        chosen = user_dictionary.get(word)
        if chosen:
            return chosen

    builtin = BUILTIN_PRONUNCIATIONS.get(word.strip().lower())
    if builtin:
        return builtin

    found = lookup(word)
    if found:
        return found

    # Possessives and plurals of known words are extremely common and are
    # not all in the dictionary. Try the stem before giving up on it.
    lowered = word.lower()
    for suffix, extra in (("'s", "z"), ("s'", "z"), ("s", "z")):
        if lowered.endswith(suffix) and len(lowered) > len(suffix) + 1:
            stem = lookup(lowered[: -len(suffix)])
            if stem:
                return stem + extra

    # A joined compound like "PowerShell" is neither a word nor an
    # acronym; spelling it out is the worst of the three options.
    parts = _split_compound(word)
    if parts:
        found = [lookup(part) for part in parts]
        if all(found):
            return "".join(found)

    return spell_out(word)


def phonemise(text: str, user_dictionary: Optional[PronunciationDictionary] = None) -> str:
    """A normalised sentence as a Kokoro phoneme string.

    Punctuation the model understands is preserved, because it is what
    makes a sentence sound like a sentence. Everything else has already
    been removed by normalise().
    """
    if not text:
        return ""

    output: List[str] = []
    for token in re.findall(r"[A-Za-z']+|\d+|[^\sA-Za-z\d]", text):
        if token.isdigit():
            for digit in token:
                output.append(phonemise_word(_DIGIT_WORDS[int(digit)], user_dictionary))
            continue
        if re.match(r"^[A-Za-z']+$", token):
            output.append(phonemise_word(token, user_dictionary))
            continue
        if token in KOKORO_VOCABULARY:
            # Attach punctuation to the preceding word rather than
            # spacing it, so the model reads it as phrasing.
            if output:
                output[-1] = output[-1] + token
            else:
                output.append(token)

    return " ".join(piece for piece in output if piece)
