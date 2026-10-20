"""The canonical form of a research query (plan section 5, T1 step 0).

One function turns what a person typed into the text that is screened and then sent, unchanged: the screen never
sees one string and the search engine another. Longer queries are refused, never cut.

The form: NFKC; format and other invisible characters removed (zero-width, bidi controls, soft hyphen, variation
selectors); Unicode dashes and spaces as ASCII; whitespace collapsed. Refused: more than MAX_QUERY characters, a word
that mixes scripts (a Cyrillic "е" inside a Latin word), and digits that are not ASCII.

Beside it, the views the screen's word rules read (app/research/screen.py):
- skeleton: ASCII where a look-alike exists (Cyrillic "еquifax" reads "equifax"), accents removed, lower case;
- joined: the skeleton with words spelled letter by letter joined up ("e q u i f a x" reads "equifax");
- digit runs: numbers written with spaces or any other punctuation between their digits ("404·555·0199",
  "404[555]0199"), with separator words ("404 dash 555 dash 0199"), as words, tens words included ("four zero four
  ...", "01 ninety nine", "double five"), or with the letters o, l and i for 0 and 1 next to a digit ("4O4-555-O199"),
  seen whole: all read 4045550199. A list of numbers reads as one too ("2023, 2024, 2025" as 202320242025): the
  screen then refuses it as a phone number, which fails closed;
- spelled_out(): addresses written in words read as addresses ("jane at example dot com").

Nothing here reads the database or the network.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_QUERY = 128

# Default-ignorable code points that are not format characters (category Cf is removed as a whole): combining
# grapheme joiner, Hangul fillers, Mongolian and other variation selectors, shorthand format controls.
_IGNORABLE = re.compile(
    "[͏ᅟᅠ឴឵᠋-᠏ㅤ︀-️ﾠ￰-￸"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]")
_REMOVED_CATEGORIES = {"Cf", "Co", "Cs", "Cn"}  # format, private use, surrogates, unassigned
_DASHES = {"−", "⁃", "﹣"}  # minus sign, hyphen bullet, small hyphen-minus (not category Pd)
_SPACE = re.compile(r"\s+")

# Letters that look like ASCII letters in common fonts. Accented Latin letters need no entry: NFKD removes accents.
CONFUSABLES = str.maketrans({
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "ѕ": "s", "і": "i", "ї": "i", "ј": "j", "ԁ": "d", "ԛ": "q", "ԝ": "w", "ӏ": "l", "һ": "h",
    "А": "A", "В": "B", "Е": "E", "Ё": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "У": "Y", "Х": "X", "Ѕ": "S", "І": "I", "Ї": "I", "Ј": "J", "Ԁ": "D", "Ԛ": "Q", "Ԝ": "W", "Ӏ": "I",
    # Greek
    "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "ω": "w",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P",
    "Τ": "T", "Υ": "Y", "Χ": "X",
    # Armenian and Latin letters NFKD leaves alone
    "օ": "o", "ս": "u", "ı": "i", "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "ħ": "h", "ɑ": "a",
    "ɡ": "g", "ɩ": "i", "ʏ": "y", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    # Typographic quotes
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"',
})
# Scripts that are written together in one word (Japanese mixes Han and kana, Korean Hangul and Han).
_SCRIPT_GROUPS = {"CJK": "HAN", "HIRAGANA": "HAN", "KATAKANA": "HAN", "HANGUL": "HAN", "BOPOMOFO": "HAN",
                  "IDEOGRAPHIC": "HAN"}

NUMBER_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "nine": "9"}
# Words that write two digits ("nineteen") or the first of two ("ninety": "ninety nine" is 99, "ninety" alone 90).
TENS_WORDS = {"ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
              "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19", "twenty": "2", "thirty": "3",
              "forty": "4", "fifty": "5", "sixty": "6", "seventy": "7", "eighty": "8", "ninety": "9", "hundred": "00"}
_REPEAT_WORDS = {"double": 2, "triple": 3}  # "double five" is 55
# Any character but a letter or a digit may sit between the digits of one written number ("404.555.0199",
# "(404) 555-0199", "404·555·0199", "404[555]0199"); these words do so inside a number ("404 dash 555 dot 0199").
_RUN_SEPARATOR_WORDS = frozenset({"dash", "hyphen", "minus", "dot", "point", "period", "comma", "slash"})
_RUN_TOKEN = re.compile(r"[a-z]+|[0-9]+|.", re.S)
# Letters written for 0 and 1 ("4O4", "Ol99"): read as digits next to a digit or a number word.
_LOOKALIKE_DIGITS = re.compile(r"[oli]+")
_AS_DIGITS = str.maketrans("oli", "011")
# Three or more single letters or digits, one separator apart: a word spelled out ("e q u i f a x", "j.a.n.e").
_SPELLED = re.compile(r"(?<![a-z0-9])(?:[a-z0-9][\s.\-_*/]{1,3}){2,}[a-z0-9](?![a-z0-9])")
_TOKEN = re.compile(r"[a-z0-9]+")
_INNER_APOSTROPHE = re.compile(r"(?<=[a-z])'(?=[a-z])")
_FULL_STOPS = {"\u3002", "\uff61", "\ufe12"}  # ideographic full stops: a dot in a domain name (IDNA reads them so)
# "jane at example dot com", "jane (at) example [dot] com", "jane @ example . com", "jane(a)example d0t com"
_AT = re.compile(r"\s*[(\[{<]\s*(?:at|@)\s*[)\]}>]\s*|\s*\(\s*a\s*\)\s*|\s+at\s+|\s*@\s*")
_DOT = re.compile(r"\s*[(\[{<]\s*(?:dot|d0t|\.)\s*[)\]}>]\s*|\s+(?:dot|d0t|period)\s+|\s+\.\s*"
                  r"|\s*\.\s+(?=[a-z]{2,24}\b)")
# Particles of a person's name ("Jane van der Berg"): two of them together name nobody ("van der Waals").
NAME_PARTICLES = frozenset({"van", "der", "den", "de", "la", "le", "von", "del", "della", "da", "di", "du", "des",
                            "dos", "das", "ten", "ter", "el", "al", "bin", "ibn"})
# Words a name shares with ordinary searches (a business's "LLC", a person's "Dr"): never enough to match on their own.
# The research screen and the Credit Repair match index leave out the same words.
STOP_WORDS = frozenset({
    "the", "a", "an", "and", "of", "for", "in", "on", "at", "to", "by", "with", "&",
    "inc", "llc", "ltd", "llp", "pllc", "pc", "co", "corp", "corporation", "company", "companies", "group", "holdings",
    "services", "service", "solutions", "enterprises", "partners", "associates", "international", "global", "usa",
    "us", "mr", "mrs", "ms", "miss", "dr", "jr", "sr", "ii", "iii", "iv",
})


class QueryRefused(ValueError):
    """The query cannot be screened as it is; code is the reason (never the text)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Canonical:
    text: str  # what is screened and sent
    skeleton: str
    joined: str
    digit_runs: tuple[str, ...]

    @property
    def words(self) -> list[str]:
        return self.text.split(" ")


def strip_invisible(text: str) -> str:
    """Without format, private-use, unassigned and other invisible characters (control characters stay)."""
    text = _IGNORABLE.sub("", text)
    return "".join(ch for ch in text if unicodedata.category(ch) not in _REMOVED_CATEGORIES)


def _ascii_marks(text: str) -> str:
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Pd" or ch in _DASHES:
            out.append("-")
        elif ch in _FULL_STOPS:
            out.append(".")
        elif cat in ("Zs", "Zl", "Zp", "Cc"):
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def normal_form(text: str) -> str:
    """The canonical text without the refusals: also used for the stored names and addresses a query is compared with."""
    text = strip_invisible(text or "")
    text = strip_invisible(unicodedata.normalize("NFKC", text))
    return _SPACE.sub(" ", _ascii_marks(text)).strip()


def _script(ch: str) -> str | None:
    if not ch.isalpha() or unicodedata.category(ch) == "Lm":
        return None
    first = unicodedata.name(ch, "UNKNOWN").split(" ", 1)[0]
    return _SCRIPT_GROUPS.get(first, first)


def skeleton(text: str) -> str:
    """ASCII where a look-alike or an unaccented letter exists, lower case; other characters are kept."""
    out = []
    for ch in text.translate(CONFUSABLES):
        if ch.isascii():
            out.append(ch)
            continue
        bare = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        out.append(bare if bare and bare.isascii() else ch)
    return "".join(out).casefold()


def fold(text: str) -> str:
    """The skeleton of any stored text (a lead's name, a client's domain), for comparing it with a query."""
    return skeleton(normal_form(text))


def tokens(skel: str) -> list[str]:
    return _TOKEN.findall(skel)


def unapostrophe(skel: str) -> str:
    """A skeleton with apostrophes inside words dropped: "o'neil" as "oneil", the way it is often typed."""
    return _INNER_APOSTROPHE.sub("", skel)


def phone_numbers(digits: str) -> tuple[str, ...]:
    """The forms a phone number is compared in: its last ten digits and the local number (the last seven), or a
    number of 7-9 digits as it is; none for a shorter one."""
    if len(digits) >= 10:
        return digits[-10:], digits[-7:]
    return (digits,) if len(digits) >= 7 else ()


def join_spelled(skel: str) -> str:
    return _SPELLED.sub(lambda m: re.sub(r"[^a-z0-9]", "", m.group(0)), skel)


def spelled_out(skel: str) -> str:
    """An address written out in words read as one: 'jane at example dot com' as 'jane@example.com'."""
    return _DOT.sub(".", _AT.sub("@", skel))


def digit_runs(skel: str) -> tuple[str, ...]:
    """Every number in the text with its separators and spelled-out digits removed, longest form first."""
    words = [t for t in _RUN_TOKEN.findall(skel) if t.isalnum()]  # every other character only separates digits
    values = [t if t.isdigit() else NUMBER_WORDS.get(t) or TENS_WORDS.get(t) for t in words]
    changed = True
    while changed:  # "4O4", "Ol99", "4 o l": look-alike letters next to a digit, or next to one read so
        changed = False
        for i, word in enumerate(words):
            if values[i] is None and _LOOKALIKE_DIGITS.fullmatch(word) and any(
                    0 <= j < len(words) and values[j] is not None for j in (i - 1, i + 1)):
                values[i], changed = word.translate(_AS_DIGITS), True
    runs: list[str] = []
    current: list[str] = []
    tens = False  # the last word was "twenty" ... "ninety": a single digit after it completes it, else it ends in 0
    repeat = 1
    for word, value in zip(words, values):
        if value is None:
            if word in _REPEAT_WORDS:
                repeat = _REPEAT_WORDS[word]
            elif not (current and word in _RUN_SEPARATOR_WORDS):
                if current:
                    runs.append("".join(current) + ("0" if tens else ""))
                current, tens, repeat = [], False, 1
            continue
        if tens and not (len(value) == 1 and word not in TENS_WORDS):
            current.append("0")
        current.append(value[0] * (repeat - 1) + value)
        tens, repeat = word in TENS_WORDS and len(value) == 1, 1
    if current:
        runs.append("".join(current) + ("0" if tens else ""))
    return tuple(runs)


def canonical(raw: object) -> Canonical:
    """The canonical query, or QueryRefused (empty_query, too_long, mixed_script, non_ascii_digits)."""
    if not isinstance(raw, str):
        raise QueryRefused("empty_query")
    text = normal_form(raw)
    if not text:
        raise QueryRefused("empty_query")
    if len(text) > MAX_QUERY:
        raise QueryRefused("too_long")
    if any(unicodedata.category(ch) == "Nd" and not ch.isascii() for ch in text):
        raise QueryRefused("non_ascii_digits")
    for word in text.split(" "):
        if len({s for s in map(_script, word) if s}) > 1:
            raise QueryRefused("mixed_script")
    skel = skeleton(text)
    return Canonical(text=text, skeleton=skel, joined=join_spelled(skel), digit_runs=digit_runs(skel))
