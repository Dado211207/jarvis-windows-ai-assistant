"""Words to Kokoro phonemes, with no GPL component anywhere in the path.

The normaliser handles paths, acronyms and dates. This file covers what
it deliberately does not: arbitrary English words, names, and everything
out of vocabulary — which still has to become phonemes the pinned model
accepts.

Every case here was chosen because it was wrong at some point while this
was being written. British English is where most of them live: the source
dictionary is American, and converting it naively produces an accent that
is audibly not the one the bm_* voices were trained on.
"""

import pytest

from app.voice.kokoro.g2p import (
    BUILTIN_PRONUNCIATIONS,
    KOKORO_VOCABULARY,
    PronunciationDictionary,
    arpabet_to_ipa,
    make_non_rhotic,
    phonemise,
    phonemise_word,
    spell_out,
    validate_phonemes,
)
from app.voice.kokoro.lexicon import lookup, size
from app.voice.kokoro.lexicon_source import ARPABET_SYMBOLS
from app.voice.kokoro.normalise import normalise


def spoken(text: str) -> str:
    return phonemise(normalise(text))


# ---------------------------------------------------------------------------
# The lexicon exists and is complete
# ---------------------------------------------------------------------------

def test_the_lexicon_is_bundled_and_substantial():
    """Shipped, not downloaded: a voice that needs the network to say
    "hello" is not a local voice."""
    assert size() > 100_000


def test_every_arpabet_symbol_has_a_mapping():
    """An unmapped symbol silently removes a sound from a word. The
    inventory is read from the upstream data's own symbol file.

    Checked against the mapping table rather than by converting each
    symbol alone, because R alone legitimately converts to nothing: a
    non-rhotic accent has no /r/ with no vowel after it, which is the
    whole point of make_non_rhotic.
    """
    from app.voice.kokoro.g2p import _ARPABET_TO_IPA

    missing = [symbol for symbol in ARPABET_SYMBOLS if symbol not in _ARPABET_TO_IPA]
    assert missing == [], f"no IPA mapping for {missing}"

    for symbol in ARPABET_SYMBOLS:
        ok, unknown = validate_phonemes(_ARPABET_TO_IPA[symbol])
        assert ok, f"{symbol} maps to {unknown!r}, which the model has never seen"


def test_a_lone_r_converts_to_nothing_and_that_is_correct():
    """Guards the exemption above from hiding a real regression: R must
    still be mapped, and must still survive before a vowel."""
    from app.voice.kokoro.g2p import _ARPABET_TO_IPA

    assert _ARPABET_TO_IPA["R"] == "ɹ"
    assert arpabet_to_ipa(["R", "AA1"]) == "ɹˈɑː"


def test_every_lexicon_entry_is_inside_the_model_vocabulary():
    """Sampled across the whole lexicon rather than the first few
    entries, because the conversion is per-symbol and a bad mapping would
    hide anywhere."""
    from app.voice.kokoro.lexicon import ensure_loaded

    entries = ensure_loaded()
    for index, (word, phonemes) in enumerate(entries.items()):
        if index % 500:
            continue
        ok, unknown = validate_phonemes(phonemes)
        assert ok, f"{word!r} contains {unknown!r}, which the model has never seen"


# ---------------------------------------------------------------------------
# British English
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("word,expected", [
    ("car", "kˈɑː"),          # no final r
    ("your", "jˈɔː"),
    ("weather", "wˈɛðə"),     # schwa, not schwa-r
    ("bird", "bˈɜːd"),
    ("report", "ɹiːpˈɔːt"),
])
def test_the_accent_is_non_rhotic(word, expected):
    """The source dictionary is American. Leaving it rhotic is the single
    most audible way a British voice can sound wrong."""
    assert lookup(word) == expected


@pytest.mark.parametrize("word,expected", [
    ("drive", "dɹˈaɪv"),      # cluster, not "dive"
    ("around", "əɹˈaʊnd"),    # onset r before a stressed vowel
    ("hurry", "hˈɜːɹiː"),     # intervocalic r
    ("very", "vˈɛɹiː"),
])
def test_r_is_kept_wherever_it_is_actually_pronounced(word, expected):
    """Two real defects live here. A naive "is the next character a
    vowel" test sees the stress marker in dɹˈaɪv and turns "drive" into
    "dive" — a different real word, so nothing looks broken. And CMUdict
    writes "around" as ER0 AW1 N D, so an override that mapped ER0 to a
    bare schwa produced "a-ound"."""
    assert lookup(word) == expected


@pytest.mark.parametrize("word,expected", [
    ("fair", "fˈɛə"),
    ("pure", "pjˈʊə"),
])
def test_centring_diphthongs_keep_their_schwa(word, expected):
    """Non-rhotic English turns /ɛər/ into /ɛə/, not into a bare /ɛ/.
    Dropping the r without compensating leaves "feh"."""
    assert lookup(word) == expected


def test_make_non_rhotic_is_positional_not_a_blanket_deletion():
    assert make_non_rhotic("fˈɑːɹ") == "fˈɑː"
    assert make_non_rhotic("əɹˈaʊnd") == "əɹˈaʊnd"


# ---------------------------------------------------------------------------
# The words the owner named
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Dado", "Dragan", "JARVIS", "Anthropic", "Kokoro", "API", "URL",
    "CPU", "GPU", "Windows", "PowerShell",
    r"C:\Users\TestUser\Documents\report.pdf",
    "2026-08-09", "14:30", "0.2.0",
    "Really? Yes, of course; obviously!",
])
def test_every_named_case_produces_valid_phonemes(text):
    phonemes = spoken(text)

    assert phonemes.strip(), f"{text!r} produced nothing at all"
    ok, unknown = validate_phonemes(phonemes)
    assert ok, f"{text!r} produced {unknown!r}, which the model has never seen"


@pytest.mark.parametrize("name", ["Dado", "Dragan", "JARVIS", "Anthropic", "Kokoro"])
def test_names_this_product_says_constantly_are_not_spelled_out(name):
    """Without built-in pronunciations the assistant spells its own name
    at you, and a user hears their own name letter by letter forever."""
    assert spoken(name) != spell_out(name)


def test_powershell_is_not_spelled_out():
    """Not a word and not an acronym, so it fell through to spelling —
    "P-O-W-E-R-S-H-E-L-L" — until compound splitting existed."""
    phonemes = spoken("PowerShell")

    assert phonemes == "pˈaʊəʃˈɛl"


def test_every_builtin_pronunciation_is_usable():
    for word, phonemes in BUILTIN_PRONUNCIATIONS.items():
        ok, unknown = validate_phonemes(phonemes)
        assert ok, f"the built-in for {word!r} contains {unknown!r}"


# ---------------------------------------------------------------------------
# Nothing is ever silent, and no unknown marker escapes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("word", [
    "Zxcvbnm", "Qwertyuiop", "Blorptastic", "Siobhan", "Nguyen",
    "Grzegorz", "Xiuying", "Ozymandias",
])
def test_an_unknown_word_is_spelled_rather_than_dropped(word):
    """A silent word is the worst outcome: the sentence changes meaning
    and nothing indicates it happened."""
    phonemes = phonemise_word(word)

    assert phonemes.strip(), f"{word!r} would be silent"
    ok, unknown = validate_phonemes(phonemes)
    assert ok, f"{word!r} produced {unknown!r}"


def test_no_unknown_marker_ever_reaches_the_model():
    """Some pipelines emit ❓ for a word they cannot pronounce. Kokoro has
    no token for it, so it would be an index error or silence."""
    for text in ("Blorptastic", "❓", "🎉 party", "\x00\x01"):
        phonemes = spoken(text)
        assert "❓" not in phonemes
        ok, _ = validate_phonemes(phonemes)
        assert ok


def test_a_realistic_assistant_reply_converts_cleanly():
    corpus = [
        "Good morning. Your system is running normally.",
        "The CPU is at 32 percent and memory usage is 6 gigabytes.",
        "I've saved that note to your Documents folder.",
        "That action needs your approval before I can run it.",
        "I couldn't reach the provider. Local commands still work.",
        "Privacy mode is on, so nothing from this conversation is being saved.",
        "There are three pending approvals and one failed action.",
        "Opening Chrome now, sir.",
        "The battery is at 87 percent and charging.",
        "I don't have information about that. Would you like me to search?",
    ]
    for sentence in corpus:
        phonemes = spoken(sentence)
        assert phonemes.strip()
        ok, unknown = validate_phonemes(phonemes)
        assert ok, f"{sentence!r} produced {unknown!r}"


def test_word_count_is_preserved_through_conversion():
    """A dropped word is silent, and silence is indistinguishable from a
    sentence that never contained it."""
    text = "The quick brown fox jumps over the lazy dog"
    assert len(spoken(text).split()) == len(text.split())


# ---------------------------------------------------------------------------
# Rejecting bad phonemes before inference
# ---------------------------------------------------------------------------

def test_validation_rejects_symbols_the_model_has_never_seen():
    ok, unknown = validate_phonemes("hɛləʊ ❓ wɜːld")

    assert ok is False
    assert "❓" in unknown


def test_validation_accepts_the_whole_model_vocabulary():
    ok, _ = validate_phonemes("".join(sorted(KOKORO_VOCABULARY)))
    assert ok is True


# ---------------------------------------------------------------------------
# The user's own dictionary
# ---------------------------------------------------------------------------

def test_a_user_pronunciation_wins_over_everything():
    """The case this exists for: the name chosen during onboarding."""
    dictionary = PronunciationDictionary()
    assert dictionary.set("Dado", "/dˈadəʊ/") is True

    assert phonemise_word("Dado", dictionary) == "dˈadəʊ"


def test_a_respelling_is_accepted_from_someone_who_does_not_know_ipa():
    dictionary = PronunciationDictionary()
    assert dictionary.set("Dado", "dah-doh") is True

    phonemes = phonemise_word("Dado", dictionary)
    assert phonemes.strip()
    ok, _ = validate_phonemes(phonemes)
    assert ok


def test_an_unusable_pronunciation_is_refused_where_it_can_be_corrected():
    """Refused at entry, not at the point of speech — a wrong entry
    should fail in Settings, where it can be fixed."""
    dictionary = PronunciationDictionary()

    assert dictionary.set("Dado", "/dad❓/") is False
    assert dictionary.get("Dado") is None


def test_entries_are_case_insensitive_and_removable():
    dictionary = PronunciationDictionary()
    dictionary.set("Dado", "dah-doh")

    assert dictionary.get("DADO") is not None
    assert dictionary.remove("dado") is True
    assert dictionary.get("Dado") is None


@pytest.mark.parametrize("word,pronunciation", [("", "x"), ("x", ""), (None, None)])
def test_an_empty_entry_is_refused(word, pronunciation):
    assert PronunciationDictionary().set(word, pronunciation) is False
