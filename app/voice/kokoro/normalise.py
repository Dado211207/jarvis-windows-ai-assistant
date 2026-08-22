"""Turning what JARVIS writes into what JARVIS should say.

This module exists because of a licence constraint with a real technical
consequence. Every off-the-shelf Kokoro pipeline reaches for espeak-ng to
pronounce words a dictionary does not contain, and espeak-ng is GPL-3.0.
Without it, out-of-vocabulary text is exactly where a neural voice falls
apart — and "out of vocabulary" for an assistant means the things it says
most: file paths, acronyms, version numbers, URLs, dates.

So the fallback is not a phonemizer. It is *rewriting the text into words
that are already in the dictionary*, which is both licence-clean and, for
this content, more correct than letter-to-sound guessing would be:
"C:\\Users\\TestUser" read phonetically is noise, while "C drive, Users,
TestUser"
is what a person would say.

Everything here is pure string work — no model, no network, no
dependencies — so the cases the owner asked to be tested (names,
acronyms, dates, URLs, Windows paths) are testable directly.

What is deliberately *not* attempted: guessing the pronunciation of an
unknown ordinary word. g2p.py falls back to spelling it out, which is
honest and occasionally clumsy, rather than confidently wrong.
"""

import re
from typing import List

# Said as words, not spelled out, even though they are capitalised.
# CPU, GPU, PDF, API and URL are deliberately *not* here: they are
# spelled out, because that is how they are said. Getting this set wrong
# is audible immediately — "Cpu" and "Pdf" are not words.
_SPOKEN_ACRONYMS = {
    "NASA", "RAM", "ROM", "JARVIS", "WIFI", "PIN", "NATO", "SIM", "JPEG",
}

_UNITS = {
    "KB": "kilobytes", "MB": "megabytes", "GB": "gigabytes", "TB": "terabytes",
    "MS": "milliseconds", "HZ": "hertz", "GHZ": "gigahertz",
}

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")

_ORDINALS = {
    1: "first", 2: "second", 3: "third", 5: "fifth", 8: "eighth",
    9: "ninth", 12: "twelfth",
}

# Punctuation Kokoro's own vocabulary understands; everything else is
# stripped rather than sent through as an unpronounceable token.
KEEPABLE_PUNCTUATION = set(";:,.!?()\"' ")


def number_to_words(value: int) -> str:
    """Small non-negative integers as words. Bounded on purpose: anything
    past a few digits is read digit by digit instead, which is what a
    person does with a serial number."""
    if value < 0:
        return "minus " + number_to_words(-value)
    if value < 20:
        return _ONES[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        spoken = f"{_ONES[hundreds]} hundred"
        return spoken + (f" and {number_to_words(rest)}" if rest else "")
    if value < 1_000_000:
        thousands, rest = divmod(value, 1000)
        spoken = f"{number_to_words(thousands)} thousand"
        if not rest:
            return spoken
        # British English keeps the "and": "two thousand and five", not
        # "two thousand five". It only appears when what follows is
        # under a hundred — "two thousand one hundred" takes none.
        joiner = " and " if rest < 100 else " "
        return spoken + joiner + number_to_words(rest)
    return " ".join(_ONES[int(digit)] for digit in str(value))


def ordinal_to_words(value: int) -> str:
    if value in _ORDINALS:
        return _ORDINALS[value]
    if value < 20:
        return _ONES[value] + "th"
    tens, ones = divmod(value, 10)
    if ones == 0:
        return _TENS[tens][:-1] + "ieth"
    return f"{_TENS[tens]} {ordinal_to_words(ones)}"


def year_to_words(year: int) -> str:
    """Years the way they are actually said: "twenty twenty-six", not
    "two thousand and twenty-six"."""
    if 1100 <= year < 2000 or 2010 <= year < 3000:
        high, low = divmod(year, 100)
        if low == 0:
            return f"{number_to_words(high)} hundred"
        if low < 10:
            return f"{number_to_words(high)} oh {number_to_words(low)}"
        return f"{number_to_words(high)} {number_to_words(low)}"
    return number_to_words(year)


def _spell_out(text: str) -> str:
    """Letters separated so the dictionary matches each one. 'U' and 'R'
    are words in CMUdict; 'URL' is not."""
    return " ".join(char.upper() for char in text if char.isalnum())


def expand_dates(text: str) -> str:
    """ISO and slashed dates, spoken as dates rather than as three
    separate numbers with punctuation between them."""
    def _iso(match: re.Match) -> str:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return match.group(0)
        return f"the {ordinal_to_words(day)} of {_MONTHS[month - 1]} {year_to_words(year)}"

    text = re.sub(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", _iso, text)

    def _slashed(match: re.Match) -> str:
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return match.group(0)
        return f"the {ordinal_to_words(day)} of {_MONTHS[month - 1]} {year_to_words(year)}"

    return re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", _slashed, text)


def expand_times(text: str) -> str:
    def _time(match: re.Match) -> str:
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return match.group(0)
        if minute == 0:
            return f"{number_to_words(hour)} o'clock"
        if minute < 10:
            return f"{number_to_words(hour)} oh {number_to_words(minute)}"
        return f"{number_to_words(hour)} {number_to_words(minute)}"

    return re.sub(r"\b(\d{1,2}):(\d{2})\b", _time, text)


def expand_windows_paths(text: str) -> str:
    """`C:\\Users\\TestUser\\Documents` -> `C drive, Users, TestUser, Documents`.

    Read as a path, the way someone would say it out loud, rather than
    as a string of unpronounceable characters.
    """
    def _path(match: re.Match) -> str:
        drive = match.group(1)
        rest = match.group(2) or ""
        parts = [segment for segment in re.split(r"[\\/]+", rest) if segment]
        spoken = [f"{drive.upper()} drive"]
        spoken.extend(_speak_path_segment(segment) for segment in parts)
        return ", ".join(spoken)

    return re.sub(r"\b([A-Za-z]):([\\/][^\s,;\"']*)?", _path, text)


def _speak_path_segment(segment: str) -> str:
    """One folder or file name. A file extension is worth saying as a
    word ("dot txt"); the rest is left for the ordinary word path."""
    if "." in segment and not segment.startswith("."):
        stem, _, extension = segment.rpartition(".")
        if stem and extension.isalpha() and len(extension) <= 4:
            return f"{stem} dot {extension.lower()}"
    return segment


def expand_urls(text: str) -> str:
    """Hosts and paths spoken in the way people read them aloud, with the
    scheme dropped — nobody says "h t t p colon slash slash"."""
    def _url(match: re.Match) -> str:
        raw = match.group(0)
        without_scheme = re.sub(r"^[a-zA-Z][\w+.-]*://", "", raw)
        without_scheme = without_scheme.rstrip("/.,;:!?")
        host, _, path = without_scheme.partition("/")
        spoken = " dot ".join(part for part in host.split(".") if part)
        if path:
            # Hyphens become spaces rather than disappearing: stripping
            # them later would run the words together, and
            # "jarviswindowsaiassistant" is not a word anyone can say.
            segments = [part.replace("-", " ") for part in path.split("/") if part]
            spoken += " slash " + " slash ".join(segments)
        return spoken

    return re.sub(r"\b[a-zA-Z][\w+.-]*://\S+|\bwww\.\S+", _url, text)


def expand_acronyms(text: str) -> str:
    """Capitalised runs spelled out, unless they are said as words or are
    a unit of measurement."""
    def _acronym(match: re.Match) -> str:
        word = match.group(0)
        if word in _SPOKEN_ACRONYMS:
            return word.capitalize()
        if word in _UNITS:
            return _UNITS[word]
        return _spell_out(word)

    text = re.sub(r"\b[A-Z]{2,6}\b", _acronym, text)

    # Mixed letter/digit runs — reference IDs, build hashes, model names.
    # Spelled out, because they are identifiers rather than words and a
    # dictionary will never contain them.
    def _identifier(match: re.Match) -> str:
        token = match.group(0)
        has_letter = any(c.isalpha() for c in token)
        has_digit = any(c.isdigit() for c in token)
        if has_letter and has_digit:
            return " ".join(
                _ONES[int(c)] if c.isdigit() else c.upper() for c in token
            )
        return token

    return re.sub(r"\b[A-Za-z0-9]{4,}\b", _identifier, text)


def expand_versions(text: str) -> str:
    """`0.2.0` -> `zero point two point zero`.

    Without this the dots survive into the phoneme string as sentence
    breaks, and a version number is read as three separate sentences.
    """
    def _version(match: re.Match) -> str:
        parts = match.group(0).split(".")
        return " point ".join(number_to_words(int(part)) for part in parts)

    return re.sub(r"\b\d+(?:\.\d+){1,3}\b", _version, text)


def expand_numbers(text: str) -> str:
    """Plain integers as words; long digit runs read out one by one, the
    way a person reads a reference number."""
    def _number(match: re.Match) -> str:
        digits = match.group(0)
        if len(digits) > 6:
            return " ".join(_ONES[int(d)] for d in digits)
        return number_to_words(int(digits))

    return re.sub(r"\b\d+\b", _number, text)


def strip_unspeakable(text: str) -> str:
    """Remove what the voice has no token for, and collapse the gaps.

    Symbols people do read aloud are replaced with their words first;
    everything else goes, because a token the model cannot represent is
    worse than a short silence.
    """
    replacements = {
        "&": " and ", "@": " at ", "%": " percent ", "+": " plus ",
        "=": " equals ", "#": " number ", "*": " ", "_": " ",
        "~": " ", "|": " ", "<": " ", ">": " ", "{": " ", "}": " ",
        "[": " ", "]": " ", "/": " slash ", "\\": " ", "^": " ", "`": " ",
        "–": " ", "—": " ", "\u2018": "'", "\u2019": "'",
        "\u201c": '"', "\u201d": '"',
    }
    for symbol, spoken in replacements.items():
        text = text.replace(symbol, spoken)
    text = "".join(char for char in text if char.isalnum() or char in KEEPABLE_PUNCTUATION)
    return re.sub(r"\s+", " ", text).strip()


def normalise(text: str) -> str:
    """The whole pipeline, in the order the rules depend on.

    Order is load-bearing: paths and URLs must be handled before
    acronyms, or the drive letter in `C:\\Users` and the `.CO` in a
    hostname get spelled out as acronyms first and the structure is lost.
    Numbers come last, once dates and times have already claimed theirs.
    """
    if not isinstance(text, str) or not text.strip():
        return ""
    text = expand_urls(text)
    text = expand_windows_paths(text)
    text = expand_dates(text)
    text = expand_times(text)
    text = expand_versions(text)
    text = expand_acronyms(text)
    text = expand_numbers(text)
    return strip_unspeakable(text)


def split_sentences(text: str, max_chars: int = 400) -> List[str]:
    """Break text into chunks the model can synthesise in one pass.

    Kokoro degrades on very long inputs, and a reply that arrives as one
    unbroken block is also worse to listen to. Splits on sentence
    boundaries, then on length only if a single sentence is still too
    long to be safe.
    """
    if not text:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: List[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            chunks.append(sentence)
    return chunks
