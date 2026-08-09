"""Pronunciation without a GPL phonemizer.

The owner's decision rules out espeak-ng, which is what every off-the-shelf
Kokoro pipeline uses to pronounce words a dictionary does not contain.
That constraint lands hardest on exactly the text an assistant says most:
file paths, acronyms, URLs, dates, reference numbers.

The answer is to rewrite those into words a dictionary already has, and
this file is where that is held to account. Each case is written as what a
person would actually say out loud, because "correct" here has no other
definition.

Everything is pure string work — no model, no network, no audio — so
these run anywhere and prove the licence-clean path really covers the
cases the owner asked about.
"""

import pytest

from app.voice.kokoro.normalise import (
    normalise,
    number_to_words,
    ordinal_to_words,
    split_sentences,
    year_to_words,
)


# ---------------------------------------------------------------------------
# Windows paths
# ---------------------------------------------------------------------------

def test_a_windows_path_is_read_the_way_a_person_reads_it():
    spoken = normalise(r"Open C:\Users\Dado\Documents\report.pdf")

    assert spoken == "Open C drive, Users, Dado, Documents, report dot pdf"
    assert "\\" not in spoken


def test_a_bare_drive_still_makes_sense():
    assert "C drive" in normalise("Check C:\\ for space")


def test_a_path_never_leaks_an_unspeakable_character():
    spoken = normalise(r"C:\Program Files\JARVIS\app_data\jarvis.db")

    for character in "\\:_":
        assert character not in spoken


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

def test_a_url_drops_its_scheme_and_spells_the_host():
    spoken = normalise("Check https://github.com/Dado211207/jarvis-windows-ai-assistant now")

    assert "github dot com" in spoken
    assert "http" not in spoken.lower()


def test_hyphenated_url_segments_do_not_run_together():
    """Stripping hyphens instead of spacing them produced
    "jarviswindowsaiassistant", which is not a word anyone can say."""
    spoken = normalise("https://example.com/jarvis-windows-ai-assistant")

    assert "jarvis windows ai assistant" in spoken
    assert "jarviswindowsaiassistant" not in spoken


def test_a_bare_www_host_is_still_recognised():
    assert "www dot example dot co dot uk" in normalise("Visit www.example.co.uk today")


# ---------------------------------------------------------------------------
# Acronyms
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("acronym,expected", [
    ("CPU", "C P U"),
    ("API", "A P I"),
    ("URL", "U R L"),
    ("PDF", "P D F"),
    ("FBI", "F B I"),
])
def test_letter_acronyms_are_spelled_out(acronym, expected):
    """Said as letters, because that is how they are said. Getting this
    wrong is audible immediately — "Cpu" is not a word."""
    assert expected in normalise(f"The {acronym} is fine")


@pytest.mark.parametrize("acronym", ["NASA", "RAM", "JARVIS"])
def test_acronyms_that_are_words_are_not_spelled_out(acronym):
    spoken = normalise(f"{acronym} is here")

    assert " ".join(acronym) not in spoken


def test_units_become_the_words_they_stand_for():
    spoken = normalise("Free space is 8 GB and the file is 120 MB")

    assert "gigabytes" in spoken
    assert "megabytes" in spoken


def test_a_reference_id_is_spelled_out_rather_than_guessed():
    """A build hash is an identifier, not a word; no dictionary will ever
    contain it, and reading it out is what a person does."""
    spoken = normalise("Reference 0260d261 was logged")

    assert "zero two six zero D two six one" in spoken


# ---------------------------------------------------------------------------
# Dates, times and numbers
# ---------------------------------------------------------------------------

def test_an_iso_date_is_spoken_as_a_date():
    assert normalise("Due 2026-08-09.") == "Due the ninth of August twenty twenty six."


def test_a_slashed_date_is_spoken_as_a_date():
    assert "the ninth of August" in normalise("Logged on 09/08/2026")


def test_something_that_only_looks_like_a_date_is_left_alone():
    """Month 99 is not a month. Guessing would be worse than reading the
    numbers."""
    spoken = normalise("Version 2026-99-01")

    assert "of" not in spoken.split()


def test_a_time_is_spoken_as_a_time():
    assert "fourteen thirty" in normalise("Meeting at 14:30")
    assert "o'clock" in normalise("Meeting at 15:00")


@pytest.mark.parametrize("value,expected", [
    (0, "zero"), (7, "seven"), (13, "thirteen"), (40, "forty"),
    (45, "forty five"), (100, "one hundred"), (365, "three hundred and sixty five"),
    (2000, "two thousand"),
    (2005, "two thousand and five"),
    (2100, "two thousand one hundred"),
])
def test_numbers_read_as_words(value, expected):
    assert number_to_words(value) == expected


def test_a_long_digit_run_is_read_digit_by_digit():
    """What a person does with a serial number."""
    assert number_to_words(12345678) == "one two three four five six seven eight"


@pytest.mark.parametrize("value,expected", [
    (1, "first"), (2, "second"), (3, "third"), (9, "ninth"),
    (11, "eleventh"), (20, "twentieth"), (21, "twenty first"),
])
def test_ordinals(value, expected):
    assert ordinal_to_words(value) == expected


@pytest.mark.parametrize("year,expected", [
    (2026, "twenty twenty six"),
    (1999, "nineteen ninety nine"),
    # 2000-2009 are read the long way in British English; only from 2010
    # does "twenty ten" take over.
    (2005, "two thousand and five"),
    (1900, "nineteen hundred"),
])
def test_years_are_said_the_way_people_say_them(year, expected):
    assert year_to_words(year) == expected


# ---------------------------------------------------------------------------
# Ordinary text is left alone
# ---------------------------------------------------------------------------

def test_a_normal_sentence_passes_through_unchanged():
    text = "My name is Dado and I live in Podgorica."

    assert normalise(text) == text


def test_names_are_never_mangled():
    """A preferred name is the one word JARVIS says most often."""
    for name in ("Dado", "Siobhan", "Xu", "O'Brien"):
        assert name.replace("'", "'") in normalise(f"Hello {name}, how are you?")


def test_sentence_punctuation_survives():
    """Kokoro's own vocabulary contains these, and they are what make a
    sentence sound like a sentence rather than a list."""
    spoken = normalise("Really? Yes, of course; obviously!")

    for mark in "?,;!":
        assert mark in spoken


# ---------------------------------------------------------------------------
# Nothing unspeakable reaches the model
# ---------------------------------------------------------------------------

def test_no_character_outside_the_model_vocabulary_survives():
    """A token the model cannot represent is worse than a short silence."""
    from app.voice.kokoro.normalise import KEEPABLE_PUNCTUATION

    hostile = "Weird ~ stuff | with <brackets> {and} [more] ^ backticks ` and emoji 🎉"
    spoken = normalise(hostile)

    for character in spoken:
        assert character.isalnum() or character in KEEPABLE_PUNCTUATION, (
            f"{character!r} would reach the model as an unknown token"
        )


def test_symbols_people_read_aloud_become_words():
    spoken = normalise("50% & more @ home")

    assert "percent" in spoken
    assert "and" in spoken
    assert "at" in spoken


@pytest.mark.parametrize("value", ["", "   ", None, 42])
def test_nothing_to_say_is_not_an_error(value):
    assert normalise(value) == ""


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def test_text_is_split_on_sentence_boundaries():
    chunks = split_sentences("First one. Second one! Third one?")

    assert chunks == ["First one.", "Second one!", "Third one?"]


def test_a_single_overlong_sentence_is_still_broken_up():
    """Kokoro degrades on very long inputs, so length wins when a
    sentence boundary never arrives."""
    chunks = split_sentences("word " * 300, max_chars=100)

    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)


def test_chunking_loses_no_words():
    text = "The quick brown fox jumps. Over the lazy dog repeatedly and at length."
    chunks = split_sentences(text, max_chars=20)

    assert " ".join(chunks).split() == text.split()


def test_empty_text_produces_no_chunks():
    assert split_sentences("") == []
