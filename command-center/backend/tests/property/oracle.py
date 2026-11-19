"""The 0.6.2 contract's oracles (RECOMMENDATION.md sections 3 and 5.1), checked against the simulator's ground truth.

Oracle A, decision backup: withhold, Settle, a scripted review that releases exactly the rows the ground truth calls
independent, export, live replay. A1 no readable row of the family carries a withheld message's words; A2 every
certain member derives from a withheld message; A3 every hold's evidence equals a recomputation; A4 rows outside the
family equal a no-withhold import; A5 the test copy and live agree on every row (C1), and no withheld text is readable
on live at any moment of that import.

Oracle B, later backup: more Hermes operations, a later backup X imported on live. B1 no stored family row gained text
or readability; B2 no readable family row carries a withheld message's words; B3 every new certain row has an exact
identity, a copy identity or an absorbed uid of a certain stone (and derives from a withheld message); B4 rows outside
the family equal a plain import; B5 every new readable family row equals a readable stored row of the family; B6 the
one-click 'release rows with no relation', when offered, never releases a row derived from a withheld message and is
refused when the backup lacks a withheld text of that family; B7 re-importing X, a rollback with a re-import and a
restore with a re-import change nothing.

The family is computed the way plan 5.1 defines it, from what the Command Center could see: the sessions bound by
stones, the sticky closure over every copy-kind link any imported backup showed, X's own links and markers, X's sessions
with a row carrying or absorbing a stone uid, and X's sessions with copied history (role, call and time of a stored row
or stone of the family, at or before the session's start). Ground truth (which logical message a row derives from)
never comes from the row's text."""

from __future__ import annotations

import copy
import dataclasses
import functools
import hashlib
import json
import random
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import cc_adapter as cc
from . import hermes_sim as hs

# --- evidence (plan 4.6) -----------------------------------------------------------------------------------------

TEXT_RELATIONS = ("identical", "restated", "contains", "part_of", "cut", "not_checked")
# option C codes (D3): exact relation codes when the hold lists them (rebuild R5)
EXACT_CODES = frozenset(TEXT_RELATIONS) | {"same_uid", "same_call", "same_time", "reused_row", "stale", "orphan",
                                           "not_in_backup", "uncertain", "later_withhold", "other_not_compared",
                                           "none", "differs"}
NOT_COMPARED_C = frozenset({"not_in_backup", "later_withhold", "other_not_compared"})
_DISCORD = re.compile(r"^\[Triggering message id: `[^`]*` — use as `message_id` for reply/react/pin via the discord "
                      r"tools\.\]\n\n")
_REPLY = re.compile(r'^\[Replying to(?: your previous message)?: "(.*?)"\]\n\n', re.S)
# a pointer that starts a turn Hermes's fold merged in (prev + '\n\n' + msg) is that turn's own wrapper (deviation 258)
_REPLY_ANY = re.compile(r'(?:\A|\n\n)\[Replying to(?: your previous message)?: "(.*?)"\]\n\n', re.S)


def message_lms(sim: hs.Sim, tag: hs.Tag) -> set[int]:
    """The withheld message for A1/A2/B2 (plan 5.1: rows that derive from W): the words the withheld row carries
    that are its own logical message or what that message re-sends or copies (its lineage). A fold, or a summary that
    carries a restated request, also carries the words of the message it absorbed: that message is its own, not a
    copy of the withheld one, and plan 4.3 holds no plain row that is a part of the withheld text (only a reply quote,
    owner Q8 (a)); so a readable original of an absorbed part is not a leak of the withheld message (deviation 249;
    listed as a residual in the integration report). For any other row this is exactly the words it carries."""
    return {tag.origin} | (set(tag.carries) & sim.lineage(tag.origin))


def norm(text: str | None) -> str:
    return " ".join((text or "").split())


def peel(text: str) -> tuple[str, str | None, bool]:
    """Hermes's wrappers off a row's text: the Discord note, the reply pointer (its quote returned), the replay
    header. Returns (body, quote, restated)."""
    body = _DISCORD.sub("", text or "", count=1)
    quote = None
    m = _REPLY.match(body)
    if m:
        quote, body = m.group(1), body[m.end():]
    restated = False
    if hs.REPLAY_HEADER in body:
        # the restated request keeps its own wrappers after the header (Hermes: HEADER + "\n" + the request; deviation
        # 280): its Discord note, then its reply pointer
        body = _DISCORD.sub("", body.rsplit(hs.REPLAY_HEADER, 1)[1].lstrip(), count=1)
        m = _REPLY.match(body)
        if m:
            quote, body = (quote if quote is not None else m.group(1)), body[m.end():]
        restated = True
    return body.strip(), quote, restated


def reply_quotes(text: str | None) -> list[str]:
    """Every reply pointer's quote that starts the text (after the Discord note), a turn a fold merged into it, or a
    request restated after the replay header (deviation 280)."""
    body = _DISCORD.sub("", text or "", count=1)
    out = [m.group(1) for m in _REPLY_ANY.finditer(body)]
    for part in body.split(hs.REPLAY_HEADER)[1:]:
        m = _REPLY.match(_DISCORD.sub("", part.lstrip(), count=1))
        if m:
            out.append(m.group(1))
    return out


def text_relations(h: str, w: str) -> set[str]:
    """The relations of a held row's text h to a withheld text w, computed by comparison after the peel."""
    out: set[str] = set()
    hn, wn = norm(h), norm(w)
    if not wn:
        return out
    body, quote, restated = peel(h)
    wbody = norm(peel(w)[0])
    if hn == wn or norm(body) == wbody:
        out.add("identical")
        # one side is the other after the replay header, or the held row is the withheld text restated after the
        # replay header (plan 4.3 'W restated after the replay header'; true also when W itself is such a
        # restatement and the held row is its copy: integrator, deviation 277)
        if restated or (hn != wn and hs.REPLAY_HEADER in (w or "")):
            out.add("restated")
    if len(wbody) >= 16 and wbody in norm(body or h):  # true of an identical or restated row too
        out.add("contains")
    quotes = ([quote] if quote is not None else []) + reply_quotes(h)
    if any(len(q) >= 16 and norm(q) and norm(q) in wbody for q in quotes):  # as JARVIS wrote it (deviation 273)
        out.add("part_of")
    # used for holds inside a withheld family only: a row whose own text is part of W (deviation 274)
    if len(norm(body)) >= 16 and norm(body) != wbody and norm(body) in wbody:
        out.add("part_of")
    return out


# --- option C readings (rebuild R5, RECON-39..): O-CI fold, O-D4 generic-v1, the derived copies -----------------------
# Written from the contract (owner decisions O-CI, O-D4; the round-3 derived readings as the deviation log states them),
# not from the product code. The demanded side (CORE readings) is what a hold must show and a carrier must be held for;
# quote spans of Hermes's f-string summaries, the cut-per-line of a truncated preview and the short-withheld-text word
# sequence are admitted only on the side of what a hold MAY show (span_relations / allowed): they are never demanded.

# Default_Ignorable_Code_Point (Unicode DerivedCoreProperties): 4,174 code points
IGNORABLE_RANGES = ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
                    (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x3164, 0x3164),
                    (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
                    (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))
_IGN = dict.fromkeys(c for a, b in IGNORABLE_RANGES for c in range(a, b + 1))
_WORDS = re.compile(r"\w+")
MIN_REL = 16  # characters (fflat): contains, a cut beginning, a part


def fold(text: str | None) -> str:
    """O-CI: default-ignorable code points removed, NFKC, casefold, Turkish dotted/dotless i read as i."""
    t = (text or "").translate(_IGN)
    t = unicodedata.normalize("NFKC", t).replace("İ", "i").replace("ı", "i")
    t = t.casefold().replace("i̇", "i")
    return unicodedata.normalize("NFKC", t).translate(_IGN)


def fflat(text: str | None) -> str:
    return " ".join(fold(text).split())


def fwords(text: str | None) -> str:
    """fold(), words only (punctuation and '> ' quote markers gone)."""
    return " ".join(_WORDS.findall(fold(text)))


# O-D4, list generic-v1 (the contract's list; test_oracle_c.py checks it equals the product's)
GENERIC_V1 = frozenset(" ".join(_WORDS.findall(p)) for p in (
    "hello", "hi", "hey", "hello there", "hi there", "hey there", "good morning", "good afternoon", "good evening",
    "good night", "thanks", "thank you", "thanks a lot", "thank you very much", "thank you so much", "many thanks",
    "thanks so much", "thanks very much", "you are welcome", "you're welcome", "no problem", "ok", "okay", "ok thanks",
    "okay thanks", "ok thank you", "yes", "no", "yes please", "no thanks", "no thank you", "sure", "great", "perfect",
    "got it", "sounds good", "please", "bye", "goodbye", "see you", "see you later", "have a nice day",
    "have a great day", "how are you", "how are you doing", "what's up", "can you help me", "please help me",
    "hallo", "guten morgen", "guten tag", "guten abend", "gute nacht", "danke", "danke schön", "danke sehr",
    "vielen dank", "bitte", "bitte schön", "ja", "nein", "ja bitte", "nein danke", "tschüss", "wie geht es dir",
    "merhaba", "teşekkürler", "teşekkür ederim", "çok teşekkür ederim", "günaydın", "iyi akşamlar", "evet", "hayır",
    "bonjour", "bonsoir", "merci", "merci beaucoup", "oui", "non", "salut", "hola", "gracias", "muchas gracias",
    "buenos días", "buenas noches", "sí", "ciao", "grazie", "grazie mille"))
_GENERIC_URL = re.compile(r"(?:(?:https?|ftp)\s*:\s*/\s*/\s*)?(?:www\s*\.\s*)?[a-z0-9-]+(?:\s*\.\s*[a-z0-9-]+)+\s*/?\s*"
                          r"(?:(?:search\s*\?\s*q\s*=|index\s*\.\s*html?|wiki\s*/|en\s*/)\s*)?")


def generic(piece: str | None, *, whole: bool = False) -> bool:
    """O-D4: a piece shorter than 16 characters (unless the whole withheld text), or one on generic-v1."""
    f = fflat(piece)
    if not f or (not whole and len(f) < MIN_REL):
        return True
    words = " ".join(_WORDS.findall(f))
    return not words or words in GENERIC_V1 or bool(_GENERIC_URL.fullmatch(f.strip(" .,;:!?'\"")))


# The decode cap: the same caps as the product's canonical decode (owner rule 'at the cap fail closed, the same in the
# oracle'; test_oracle_c.py checks the constants and the capped verdict on the same inputs)
CAP_STEPS, CAP_DEPTH, CAP_BUDGET, CAP_TEXT, CAP_STRINGS = 64, 32, 4_000_000, 2_000_000, 20_000
DECODE_CAP = True  # a mutant that turns the cap off must be killed (test_oracle_c.py)


@dataclass(frozen=True)
class Rd:
    """One reading of a field: unit (compared whole), lead (W would start there), cut_end (cut at its end without a
    mark), span (a quote span of a Hermes f-string summary: admitted only on the may-show side)."""
    text: str
    unit: bool = False
    lead: bool = False
    cut_end: bool = False
    span: bool = False


_QUOTE_LINE = re.compile(r"(?m)^[ \t]*(?:>[ \t]?)+")
_PRUNE = re.compile(r"\[[\w.:/-]+\]( [A-Za-z_][\w-]*=.*?) \([\d,]+ chars result\)", re.S)
_PRUNE_K = re.compile(r"(?:^| )([A-Za-z_][\w-]*)=")
PRUNE_WIDTH = 40  # str(v)[:40] (context_compressor._summarize_tool_result_unguarded)
_CUT_MARK = re.compile(r"…|\.\.\.|\[(?:output |content |message )?truncated|\((?:output )?truncated|<truncated", re.I)
_TRUNC = re.compile(r"\[truncated|truncated: tool response|</persisted-output>|\n\.\.\.(?:\n|\Z)", re.I)
_OPEN = re.compile(r"""(?:\A|[:\[{(,=]\s?)(['"])""")
_DOC = re.compile(r'\A\s*(?:\{.*\}|\[.*\]|".*")\s*\Z', re.S)


def quote_spans(text: str) -> list[Rd]:
    """The quote spans of Hermes's f-string tool summaries (written from the spec text: the argument between the
    summary's opening quote and the closing one its fixed tail follows, across apostrophes and line breaks;
    vision_analyze cut at 50 characters, delegate_task at 57 + '...'), each line of a span as well. Admitted only on
    the side of what a hold may show (round 3, consolidation oracle change 1)."""
    out: list[Rd] = []
    if "] " not in text:
        return out
    for rx, width in ((r"\[vision_analyze\] '(.*?)' \([\d,]+ chars\)", 50),
                      (r"\[web_search\] query='(.*?)' \([\d,]+ chars result\)", 0),
                      (r"\[search_files\] \S+ search for '(.*?)' in .*? -> \S+ matches", 0),
                      (r"\[delegate_task\] '(.*?)' \([\d,]+ chars result\)", 0),
                      (r"\[execute_code\] `(.*?)` \(\d+ lines output\)", 0),
                      (r"\[terminal\] ran `(.*?)` -> exit ", 0),
                      (r"\[clarify\] user responded: (.*)", 0)):
        for m in re.finditer(rx, text, re.S):
            span = m.group(1)
            cut = bool(width) and len(span) >= width
            if span.endswith("..."):
                span, cut = span[:-3], True
            out.append(Rd(span, unit=True, lead=True, cut_end=cut, span=True))
            if "\n" in span:
                out.extend(Rd(ln, unit=True, lead=i == 0, span=True) for i, ln in enumerate(span.split("\n")) if ln)
    return out


def _cheap_readings(text: str) -> list[Rd]:
    out: list[Rd] = []
    if _QUOTE_LINE.search(text):  # fqw: the text without '> ' markers, and each quoted block whole
        out.append(Rd(_QUOTE_LINE.sub("", text)))
        block: list[str] = []
        for line in text.splitlines() + [""]:
            if _QUOTE_LINE.match(line):
                block.append(_QUOTE_LINE.sub("", line, count=1))
            elif block:
                out.append(Rd("\n".join(block), unit=True, lead=True))
                block = []
    out += quote_spans(text)
    for m in _PRUNE.finditer(text):  # the generic prune line: each value at 40 characters
        args = m.group(1)
        keys = list(_PRUNE_K.finditer(args))
        for k, key in enumerate(keys):
            end = keys[k + 1].start() if k + 1 < len(keys) else len(args)
            win = args[key.end():key.end() + PRUNE_WIDTH]
            out.append(Rd(win, lead=True, cut_end=len(win) >= PRUNE_WIDTH))
            val = args[key.end():end]
            out.append(Rd(val, unit=True, lead=True, cut_end=len(val) >= PRUNE_WIDTH))
    return out


def unescape(text: str) -> str:
    """JSON and Python escapes read back, tolerant of a cut text (a cut escape and a lone surrogate dropped)."""
    out, i, n = [], 0, len(text)
    simple = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "a": "\a", "v": "\v", "0": "\0", '"': '"',
              "'": "'", "\\": "\\", "/": "/"}
    while i < n:
        c = text[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        if i + 1 >= n:
            break
        e = text[i + 1]
        width = {"u": 4, "x": 2, "U": 8}.get(e)
        if not width:
            out.append(simple.get(e, e))
            i += 2
            continue
        digits = text[i + 2:i + 2 + width]
        if len(digits) < width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
            part = re.match(r"[0-9a-fA-F]*", digits)
            i += 2 + (part.end() if part else 0)
            continue
        code, i = int(digits, 16), i + 2 + width
        if e == "u" and 0xD800 <= code <= 0xDBFF:
            low = re.match(r"\\u([dD][c-fC-F][0-9a-fA-F]{2})", text[i:i + 6])
            if not low:
                continue
            code, i = 0x10000 + ((code - 0xD800) << 10) + (int(low.group(1), 16) - 0xDC00), i + 6
        if 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
            continue
        out.append(chr(code))
    return "".join(out)


def _literals(text: str) -> list[Rd]:
    out: list[Rd] = []
    i, n, count = 0, len(text), 0
    while i < n and count < CAP_STRINGS:
        found = _OPEN.search(text, i)
        if not found:
            break
        q, j = found.group(1), found.end()
        start = j
        while j < n and text[j] != q:
            j += 2 if text[j] == "\\" else 1
        body = text[start:min(j, n)]
        if j >= n:
            out.append(Rd(unescape(body), unit=True, lead=True, cut_end=True))
            break
        if "\\" in body:
            out.append(Rd(unescape(body), unit=True, lead=True))
        count += 1
        i = j + 1
    return out


def _open_literal(text: str) -> bool:
    i, n = 0, len(text)
    while i < n:
        found = _OPEN.search(text, i)
        if not found:
            return False
        q, j = found.group(1), found.end()
        while j < n and text[j] != q:
            j += 2 if text[j] == "\\" else 1
        if j >= n:
            return True
        i = j + 1
    return False


class _CapHit(Exception):
    pass


def _strings(value: Any) -> list[Rd]:
    out: list[Rd] = []
    stack = [value]
    while stack and len(out) < CAP_STRINGS:
        v = stack.pop()
        if isinstance(v, str):
            out.append(Rd(v, unit=True, lead=True))
        elif isinstance(v, dict):
            for k, x in v.items():
                out.append(Rd(str(k), unit=True, lead=True))
                stack.append(x)
        elif isinstance(v, list):
            stack.extend(v)
    return out


def _decode_steps(text: str) -> list[Any]:
    rest: list[Any] = []
    if "\\" in text:
        rest.append(lambda: [Rd(unescape(text))])
    if _OPEN.search(text) and ("\\" in text or _open_literal(text)):
        rest.append(lambda: _literals(text))
    if _DOC.match(text) and (text.lstrip()[:1] != '"' or "\\" in text):
        def parse() -> list[Rd]:
            try:
                value = json.loads(text)
            except RecursionError as exc:
                raise _CapHit from exc
            except ValueError:
                return [x for fn in rest for x in fn()]
            return _strings(value)
        return [parse]
    return rest


def readings(text: str | None) -> tuple[list[Rd], bool]:
    rds, capped = _readings_cached(text or "") if len(text or "") <= 50_000 else _readings(text or "")
    return list(rds), capped


@functools.lru_cache(maxsize=8192)
def _readings_cached(text: str) -> tuple[tuple[Rd, ...], bool]:
    rds, capped = _readings(text)
    return tuple(rds), capped


def _readings(text: str | None) -> tuple[list[Rd], bool]:
    """Every reading of a field (the field first) and whether a cap stopped the decode (fail-closed: not_checked).
    Iterative, with the product's caps: CAP_STEPS decode steps, CAP_DEPTH nested decodes, CAP_BUDGET characters of
    readings, CAP_TEXT for the field."""
    text = text or ""
    if DECODE_CAP and len(text) > CAP_TEXT:
        return [Rd(text, unit=True, lead=True)], True
    out: list[Rd] = []
    seen: set[Rd] = set()
    queue: list[tuple[Rd, int]] = [(Rd(text, unit=True, lead=True), 0)]
    steps, budget, capped = 0, CAP_BUDGET, False
    while queue:
        r, depth = queue.pop(0)
        if r in seen or not r.text:
            continue
        seen.add(r)
        budget -= len(r.text)
        if DECODE_CAP and budget < 0:
            capped = True
            break
        out.append(r)
        queue.extend((x, depth) for x in _cheap_readings(r.text) if x.text != r.text)
        steps_here = _decode_steps(r.text)
        if not steps_here:
            continue
        if DECODE_CAP and depth >= CAP_DEPTH:
            capped = True
            continue
        for fn in steps_here:
            steps += 1
            if DECODE_CAP and steps > CAP_STEPS:
                capped = True
                break
            try:
                found = fn()
            except (_CapHit, RecursionError, MemoryError):
                capped = True
                continue
            queue.extend((x, depth + 1) for x in found if x.text != r.text or x.cut_end != r.cut_end)
        if DECODE_CAP and steps > CAP_STEPS:
            break
    return out, capped


def _suffix_prefix(tail: str, w: str) -> int:
    """The longest suffix of tail that is a prefix of w."""
    tail = tail[-len(w):] if w else ""
    for k in range(min(len(tail), len(w)), 0, -1):
        if tail.endswith(w[:k]):
            return k
    return 0


def _peeled(text: str) -> str:
    return peel(text)[0] or text


@dataclass(frozen=True)
class WC:
    w: str  # fflat of W's authored text
    whole: str  # fflat of W as written
    words: str  # fwords of W's authored text
    generic: bool  # O-D4: W itself on generic-v1


def wc(withheld: str) -> WC | None:
    body = _peeled(withheld)
    w = fflat(body) or fflat(withheld)
    if not w:
        return None
    return WC(w, fflat(withheld), fwords(body) or fwords(withheld), generic(body or withheld, whole=True))


CUT_PER_LINE = True  # admitted (may show) only; a mutant that demands it is a loosening of nothing (see self-tests)


def _rd_relations(r: Rd, wf: WC, *, group: bool, carrier: bool, allowed: bool) -> set[str]:
    out: set[str] = set()
    f = fflat(r.text)
    if not f:
        return out
    if r.unit and (f in (wf.w, wf.whole) or (wf.words and fwords(r.text) == wf.words)):
        out.add("identical")
    if len(wf.w) >= MIN_REL and wf.w in f:
        out.add("contains")
    if allowed and group and len(wf.words) < MIN_REL and wf.words and f" {wf.words} " in f" {fwords(r.text)} ":
        out.add("contains")  # a short W's words as a sequence inside a group: ambiguous (may show, never demanded)
    if len(wf.w) < MIN_REL:
        return out
    if (r.unit or r.lead) and len(f) < len(wf.w) and wf.w.startswith(f) and not generic(f):
        out.add("cut")
    points = [m.start() for m in _CUT_MARK.finditer(r.text)] + ([len(r.text)] if r.cut_end else [])
    if allowed and CUT_PER_LINE and _TRUNC.search(r.text):
        points += [m.start() for m in re.finditer(r"\n", r.text)]
    for p in points:
        k = _suffix_prefix(fflat(r.text[:p]), wf.w)
        if MIN_REL <= k < len(wf.w) and not generic(wf.w[:k]):
            out.add("cut")
    if group and r.unit and len(f) >= MIN_REL and f != wf.w and f in wf.w and not generic(f):
        out.add("part_of")
    if "[Replying to" in r.text:
        for q in reply_quotes(r.text):
            if len(q) >= MIN_REL and fflat(q) and fflat(q) in wf.w and not (carrier and generic(q, whole=True)):
                out.add("part_of")
    return out


def field_relations(held: str, w: str, *, group: bool, carrier: bool = False, allowed: bool = False) -> set[str]:
    """The relations of one field of a held row to a withheld text under fold and every reading: CORE readings only
    (demanded), or with the spans and the may-show readings too (allowed=True)."""
    wf = wc(w)
    if wf is None or not norm(held):
        return set()
    out: set[str] = set()
    body, quote, again = peel(held)
    whole = fflat(held)
    if whole == wf.whole or (wf.w and fflat(body) == wf.w):
        out.add("identical")
        if whole != wf.whole and (again or hs.REPLAY_HEADER in w):
            out.add("restated")
    rds, capped = readings(held)
    if capped:
        out.add("not_checked")
    for r in rds:
        if r.span and not allowed:
            continue
        out |= _rd_relations(r, wf, group=group, carrier=carrier, allowed=allowed)
    if quote is not None and len(quote) >= MIN_REL and fflat(quote) in wf.w:
        out.add("part_of")
    if group and len(fflat(body)) >= MIN_REL and fflat(body) != wf.w and fflat(body) in wf.w:
        out.add("part_of")
    if carrier and wf.generic:
        out -= {"identical", "contains", "restated"}  # O-D4: a generic W is no carrier
    return out


def relations_c(fields: list[str], w: str, *, group: bool, carrier: bool = False) -> tuple[set[str], set[str]]:
    """(demanded, allowed) over every field of a row: demanded from the core readings; allowed adds the spans and the
    may-show readings (span_relations)."""
    dem, alw = set(), set()
    for f in fields:
        if not f:
            continue
        dem |= field_relations(f, w, group=group, carrier=carrier)
        alw |= field_relations(f, w, group=group, carrier=carrier, allowed=True)
    return dem, alw | dem


def span_relations(fields: list[str], w: str, *, group: bool) -> set[str]:
    """What the quote spans alone relate (round 3 consolidation oracle change 1: admitted, never demanded)."""
    wf = wc(w)
    out: set[str] = set()
    if wf is None:
        return out
    for f in fields:
        for r in quote_spans(f or ""):
            out |= _rd_relations(r, wf, group=group, carrier=False, allowed=True)
    return out


def version_fields(v: hs.Version) -> list[str]:
    return [x for x in (v.content, v.calls) if x]


def row_fields(r: dict[str, Any]) -> list[str]:
    calls = r.get("tool_calls")
    if calls and not isinstance(calls, str):
        calls = json.dumps(calls, ensure_ascii=False, sort_keys=True)
    return [x for x in (r.get("content") or "", calls or "") if x]


_CC_MARK = re.compile(r"\[REDACTED:[A-Za-z0-9_ -]+\]|\[email\]|\[phone\]")


def cc_redacted_of(stored: str | None, released: str | None) -> bool:
    """A stored released row that equals the released text with only the Command Center's own marks
    ([REDACTED:<kind>], [email], [phone]) in place of spans: the readable pieces between the marks appear, in order,
    in the released text, and the first and last pieces anchor at its ends (round 3 consolidation oracle change 2)."""
    s, r = stored or "", released or ""
    if norm(s) == norm(r):
        return True
    if not _CC_MARK.search(s):
        return False
    pieces = _CC_MARK.split(s)
    pos = 0
    for i, piece in enumerate(pieces):
        if not piece:
            continue
        at = r.find(piece, pos)
        if at < 0 or (i == 0 and at != 0):
            return False
        pos = at + len(piece)
    return not pieces[-1] or r.endswith(pieces[-1])


_CODES = (("not_in_backup", r"not in (this|the) backup|not_in_backup|not in backup"),
          ("identical", r"identical|same text|same words"),
          ("restated", r"restat"),
          ("contains", r"contain"),
          ("part_of", r"part of|part_of|reply quote|quote"),
          ("same_uid", r"message id|message_uid|same uid|same_uid"),
          ("same_call", r"tool call|tool_call|same_call"),
          ("same_time", r"same time|same_time|same second"),
          ("reused_row", r"row number|reused|reused_row"),
          ("stale", r"stale"),
          ("orphan", r"orphan"),
          ("differs", r"differ"),
          ("none", r"^none$|no relation"))


def reported_relations(hold: dict[str, Any]) -> tuple[set[str], list[str]]:
    texts: list[str] = []
    rel = hold.get("relations")
    if isinstance(rel, list) and rel and all(isinstance(x, str) and x in EXACT_CODES for x in rel):
        # option C: the hold lists its exact codes (D3); the words shown are read as before as well (rebuild R5)
        exact = set(rel)
    else:
        exact = set()

    def walk(x: Any) -> None:
        if isinstance(x, str):
            texts.append(x)
        elif isinstance(x, (list, tuple)):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            for y in x.values():
                walk(y)
    for key in ("relation", "relations", "relation_codes", "codes", "evidence", "compare"):
        if key in hold:
            walk(hold[key])
    out: set[str] = set(exact)
    for t in texts:
        tl = t.lower().strip()
        for code, pat in _CODES:
            if re.search(pat, tl):
                out.add(code)
    return out, texts


# --- ground truth for a stored row ---------------------------------------------------------------------------------


def tkey(t: float | None) -> float | None:
    """A time as a comparison key, at the microsecond the Command Center stores (source.py: datetime.fromtimestamp,
    microseconds). Milliseconds were used before; a time at a half millisecond rounded one way as JARVIS's float and
    the other way as the stored microsecond value, so the same row did not match itself (integrator, deviation 276)."""
    return None if t is None else round(t, 6)


def epoch(at: Any) -> float | None:
    if at in (None, ""):
        return None
    if isinstance(at, (int, float)):
        return float(at)
    try:
        return datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def versions_for(sim: hs.Sim, row: dict[str, Any]) -> list[hs.Version]:
    rid = str(row.get("source_message_id") or "")
    if not rid.isdigit():
        return []
    cands = sim.versions_of(row["sid"], int(rid))
    at = epoch(row.get("at"))
    uid = row.get("source_uid") or ""
    out = [v for v in cands if v.role == row.get("role") and (at is None or abs(v.ts - at) < 2e-3)
           and (not uid or not v.uid or v.uid == uid)]
    if len(out) > 1 and norm(row.get("content")):
        exact = [v for v in out if norm(v.content) == norm(row.get("content"))]
        out = exact or out
    return out or cands


def backup_versions(run: "Run", row: dict[str, Any], snap: hs.Snapshot) -> list[hs.Version]:
    """The row's version in the backup a hold's evidence was computed on (deviation 241): a held row is blank, so
    versions_for cannot tell its versions apart by text and would compare every text the row ever had."""
    vs = versions_for(run.sim, row)
    rid = str(row.get("source_message_id") or "")
    v = snap.rows.get((row["sid"], int(rid))) if rid.isdigit() else None
    if v is None and rid.isdigit():
        # a stale stored row (this backup no longer holds it): the text the Command Center stored for it is the one
        # the newest imported backup that held it brought (deviation 270)
        for older in reversed(run.imported):
            if (row["sid"], int(rid)) in older.rows:
                v = older.rows[(row["sid"], int(rid))]
                break
    if v is not None and any(x is v or (x.role, tkey(x.ts), x.uid, x.content) == (v.role, tkey(v.ts), v.uid,
                                                                                    v.content) for x in vs):
        return [v]
    return vs


def content_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


@dataclass
class Withhold:
    cc_id: int
    key: str
    sid: str
    row: int
    text: str
    lms: set[int]  # the logical message the withheld row is a version or copy of, and what that copies (message_lms)
    origin: int
    role: str = ""  # the withheld row's own identity when withheld (deviation 245)
    at: float | None = None
    uid: str = ""
    absorbed: tuple[str, ...] = ()  # the uids the withheld row had absorbed (a withheld fold; deviation 275)


@dataclass
class Run:
    seed: int
    sim: hs.Sim
    profile: str
    content: str
    api: cc.Api
    rng: random.Random
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    withholds: list[Withhold] = field(default_factory=list)
    imported: list[hs.Snapshot] = field(default_factory=list)  # every backup this installation imported
    runs: list[int] = field(default_factory=list)
    # option C (rebuild R5): the texts each released row may hold (source key or 'id:<archive id>' -> texts), and the
    # rows stored when each withhold was made (its text was in hand then: the full scan compared them)
    released: dict[str, list[str]] = field(default_factory=dict)
    at_withhold: dict[str, set[str]] = field(default_factory=dict)

    def bad(self, prop: str, msg: str) -> None:
        self.problems.append("%s seed %d: %s" % (prop, self.seed, msg))

    @property
    def wlms(self) -> set[int]:
        out: set[int] = set()
        for w in self.withholds:
            out |= w.lms
        return out

    def derives(self, versions: list[hs.Version]) -> bool:
        return any(self.sim.derives(v.tag, self.wlms) for v in versions)

    def leaks(self, versions: list[hs.Version]) -> bool:
        return any(self.sim.leaks(v.tag, self.wlms) for v in versions)

    async def import_(self, snap: hs.Snapshot, **kw: Any) -> dict[str, Any]:
        out = await cc.import_backup(snap.path, self.profile, self.content, **kw)
        self.imported.append(snap)
        if out.get("run_id"):
            self.runs.append(out["run_id"])
        return out


# --- the family (plan 5.1) -----------------------------------------------------------------------------------------


def family(run: Run, current: hs.Snapshot, stored: list[dict[str, Any]], stones: list[dict[str, Any]]) -> set[str]:
    links: set[tuple[str, str]] = set()
    for snap in run.imported + [current]:
        for child, parent, how in snap.edges:
            if how in hs.COPY_LINKS:
                links.add((child, parent))
    fam = {w.sid for w in run.withholds} | {s.get("source_session_id") for s in stones if s.get("source_session_id")}
    stone_uids = {s.get("message_uid") for s in stones if s.get("message_uid") and s.get("review") != "pending"}
    for w in run.withholds:
        for snap in run.imported:
            v = snap.rows.get((w.sid, w.row))
            if v is not None and v.uid and v.tag.origin == w.origin:
                stone_uids.add(v.uid)
    while True:
        before = set(fam)
        for child, parent in links:
            if child in fam or parent in fam:
                fam |= {child, parent}
        keys = set()
        for r in stored:
            if r["sid"] in fam:
                keys.add((r.get("role"), r.get("tool_call_id") or "", tkey(epoch(r.get("at")) or 0.0)))
        for s in stones:
            if s.get("source_session_id") in fam and s.get("source_at"):
                keys.add((s.get("role"), s.get("tool_call_id") or "", tkey(epoch(s.get("source_at")) or 0.0)))
        for sid, sess in current.sessions.items():
            if sid in fam:
                continue
            for (rsid, _), v in current.rows.items():
                if rsid != sid:
                    continue
                if (v.uid and v.uid in stone_uids) or (set(v.absorbed) & stone_uids):
                    fam.add(sid)
                    break
                if v.ts <= (sess.get("started_at") or 0) + 1e-6 and (v.role, v.call or "", tkey(v.ts)) in keys:
                    fam.add(sid)
                    break
        if fam == before:
            return fam


def per_row_carrier(stone: dict[str, Any]) -> bool:
    """Option C (RECON-27): a pending carrier hold, held per row on its own (i22 made no such stone: D-26)."""
    return stone.get("via") == CARRIER_VIA and stone.get("review") == "pending"


def carrier_only_sids(run: Run, current: hs.Snapshot, stored: list[dict[str, Any]], stones: list[dict[str, Any]],
                      fam: set[str], group: set[str] = frozenset()) -> set[str]:
    """Rebuild S3 (RECON-58, D-45): the conversations of the family `fam` that are in it only because option C's
    pending per-row carrier holds bind them: i22's family computed without those stones (every stone i22 made, every
    link, uid and copied history still counts) does not reach them, and they are no group conversation (the ground
    truth's or the Command Center's own) either."""
    i22 = family(run, current, stored, [s for s in stones if not per_row_carrier(s)])
    return set(fam) - i22 - set(group)


def decision_family(run: Run, w: Withhold, current: hs.Snapshot, st: dict[str, Any]) -> set[str]:
    """The family (5.1) of one decision: family() started from this withhold and its own group's stones only."""
    proxy = copy.copy(run)
    proxy.withholds = [w]
    stones = [s for s in st["stones"] if w.key in (s.get("source_key"), s.get("anchor_key"))]
    return family(proxy, current, st["rows"], stones)


# --- stored state -------------------------------------------------------------------------------------------------


async def state() -> dict[str, Any]:
    rows = await cc.rows()
    stones = await cc.stones()
    by_key = {s["source_key"]: s for s in stones}
    return {"rows": rows, "stones": stones, "stone_by_key": by_key}


def released_pending(row: dict[str, Any], st: dict[str, Any]) -> bool:
    """An admin released this held row; its text comes back at the next import of a backup holding it (owner Q6:
    nothing computed from it is stored), so until then it is still withheld, with no stone (integrator, deviation
    240)."""
    flag = (row.get("flags") or {}).get("withheld_by_admin") or {}
    return bool(row.get("withheld")) and bool(flag.get("released_at")) and row.get("source_key") not in st[
        "stone_by_key"]


def hide_title_choice(run: "Run", row: dict[str, Any], snap: hs.Snapshot, carries: set[int]) -> dict[str, Any]:
    """The admin's 'hide the conversation title' choice in the withhold dialog (C6 lists it among the admin's
    decisions; the dialog shows the conversation's title): ticked when the title shown derives from the message being
    withheld, else the dialog's default (deviation 246)."""
    tag = snap.title_tags.get(row["sid"])
    if norm(row.get("session_title")) and tag is not None and run.sim.lineage(tag) & set(carries):
        return {"hide_title": True}
    return {}


def check_released_blank(run: "Run", prop: str, st: dict[str, Any], where: str) -> None:
    """Until the next import, a released row stays hidden and holds no text (fail-closed; deviation 240)."""
    for r in st["rows"]:
        if released_pending(r, st) and norm(r.get("content")):
            run.bad(prop, "%s: released row %s (%s:%s) holds text before the next import" % (
                where, r["id"], r["sid"], r.get("source_message_id")))


def admin_withheld(row: dict[str, Any], st: dict[str, Any]) -> bool:
    return bool(row.get("withheld")) and ("withheld_by_admin" in (row.get("flags") or {})
                                          or row.get("source_key") in st["stone_by_key"])


def row_state(row: dict[str, Any], st: dict[str, Any]) -> tuple:
    stone = st["stone_by_key"].get(row.get("source_key"))
    flag = (row.get("flags") or {}).get("withheld_by_admin") or {}
    review = stone.get("review") if stone else flag.get("review", "-")
    via = stone.get("via") if stone else flag.get("via", "-")
    return (bool(row.get("withheld")), review, via, content_hash(row.get("content")))


def by_key(st: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["source_key"]: r for r in st["rows"]}


def compare_states(run: Run, prop: str, what: str, want: dict[str, Any], got: dict[str, Any],
                   keys: set[str] | None = None) -> None:
    a, b = by_key(want), by_key(got)
    keys = set(a) | set(b) if keys is None else keys
    diffs = []
    for k in sorted(keys):
        ra, rb = a.get(k), b.get(k)
        sa = row_state(ra, want) if ra else None
        sb = row_state(rb, got) if rb else None
        if sa != sb:
            diffs.append("%s: %s -> %s" % (k, sa, sb))
    if diffs:
        run.bad(prop, "%s: %d rows differ: %s" % (what, len(diffs), "; ".join(diffs[:6])))


# --- the checks ---------------------------------------------------------------------------------------------------


def compare_c1(run: Run, prop: str, tc: dict[str, Any], live: dict[str, Any], plain_tc: dict[str, Any],
               plain_live: dict[str, Any], decision: hs.Snapshot) -> None:
    """C1: every JARVIS row live stores has the test copy's state, and live stores every row of the decision backup
    the test copy stores. The test copy may have imported earlier backups first; where a plain import of the test
    copy's backups and a plain import of the decision backup alone already store a row differently (the importer's
    own history handling, with no decision at all), that difference is not C1's: it is excused only when both sides
    are exactly the plain imports' (readable with the plain content, or not stored on either live side), and noted
    (deviation 250)."""
    a, b, pa, pb = by_key(tc), by_key(live), by_key(plain_tc), by_key(plain_live)

    def plain(r: dict[str, Any] | None) -> tuple | None:
        return None if r is None else (bool(r.get("withheld")), content_hash(r.get("content")))
    diffs, excused = [], []
    keys = set(b) | {k for k, r in a.items() if (r["sid"], int(r["source_message_id"])) in decision.rows}
    for k in sorted(keys):
        ra, rb = a.get(k), b.get(k)
        sa = row_state(ra, tc) if ra else None
        sb = row_state(rb, live) if rb else None
        if sa == sb:
            continue
        same_as_plain = plain(pa.get(k)) != plain(pb.get(k)) and (
            (rb is None and pb.get(k) is None and (ra is None or plain(ra) == plain(pa.get(k))
                                                    or bool(ra.get("withheld"))))
            or (ra is not None and rb is not None and not ra.get("withheld") and not rb.get("withheld")
                and plain(ra) == plain(pa.get(k)) and plain(rb) == plain(pb.get(k))))
        if not same_as_plain and released_waiting_shown(run, ra, tc, rb, decision):
            excused.append("%s: released on the test copy, waiting for a text the decision backup no longer holds; "
                           "live shows the released text (S2, RECON-57)" % k)
            continue
        (excused if same_as_plain else diffs).append("%s: %s -> %s" % (k, sa, sb))
    if excused:
        run.notes.append("%s: %d rows differ exactly as the plain imports do (no decision involved): %s" % (
            prop, len(excused), "; ".join(excused[:4])))
    if diffs:
        run.bad(prop, "test copy vs live: %d rows differ: %s" % (len(diffs), "; ".join(diffs[:6])))


def released_waiting_shown(run: Run, ra: dict[str, Any] | None, tc: dict[str, Any], rb: dict[str, Any] | None,
                           decision: hs.Snapshot) -> bool:
    """C1 for a released row (stabilisation S2, RECON-57): the admin released this exact row on the test copy, and it
    waits there for its text (released_pending: withheld, released, no stone) because the decision backup no longer
    holds the row (the test copy's next import of it cannot bring the text back: a stale stored row); live, which
    stored the row from an earlier backup, shows it readable with exactly the text released for that very test-copy
    row (its key or id: run.released, the content of the backup the hold was decided on). Both say 'released'. Never
    when the decision backup holds the row (the test copy must have its text back), when the test copy's row is held
    or not released, or when live shows any other text."""
    if ra is None or rb is None or rb.get("withheld") or not released_pending(ra, tc):
        return False
    rid = str(ra.get("source_message_id") or "")
    if not rid.isdigit() or (ra["sid"], int(rid)) in decision.rows or rb.get("sid") != ra.get("sid") \
            or str(rb.get("source_message_id") or "") != rid:
        return False
    texts = run.released.get(ra["source_key"]) or run.released.get("id:%s" % ra["id"]) or []
    return bool(norm(rb.get("content"))) and any(cc_redacted_of(rb.get("content"), t) for t in texts)


def check_no_readable_leak(run: Run, prop: str, st: dict[str, Any], fam: set[str], snap: hs.Snapshot,
                           where: str) -> None:
    for r in st["rows"]:
        if r["sid"] not in fam or r.get("withheld") or not norm(r.get("content")):
            continue
        vs = versions_for(run.sim, r)
        if run.leaks(vs):
            run.bad(prop, "%s: readable row %s (%s:%s %s) carries withheld words: %r" % (
                where, r["id"], r["sid"], r.get("source_message_id"), r.get("role"), (r.get("content") or "")[:70]))
    # titles of family sessions (C4): a title derived from a withheld message must not be readable
    seen = set()
    for r in st["rows"]:
        sid = r["sid"]
        if sid in seen or sid not in fam:
            continue
        seen.add(sid)
        tag = snap.title_tags.get(sid)
        title = norm(r.get("session_title"))
        if tag is None or not title:
            continue
        if run.sim.lineage(tag) & run.wlms:
            src_title = norm((snap.sessions.get(sid) or {}).get("title"))
            if src_title and src_title[:20] in title:
                run.bad(prop, "%s: session %s title readable although derived from a withheld message: %r" % (
                    where, sid, title[:60]))


def check_certain_derive(run: Run, prop: str, st: dict[str, Any], listed: set[int], fam: set[str] | None,
                         where: str, only: set[str] | None = None) -> None:
    for r in st["rows"]:
        if not admin_withheld(r, st) or r["id"] in listed or released_pending(r, st):
            continue
        if only is not None and r["source_key"] not in only:
            continue
        stone = st["stone_by_key"].get(r["source_key"])
        if stone is not None and stone.get("review") == "pending":
            run.bad(prop, "%s: row %s has a pending stone but is not listed among the holds" % (where, r["id"]))
            continue
        vs = versions_for(run.sim, r)
        if not vs:
            run.notes.append("%s: no ground truth for certain row %s (%s:%s)" % (where, r["id"], r["sid"],
                                                                                  r.get("source_message_id")))
            continue
        if not run.derives(vs):
            run.bad(prop, "%s: row %s (%s:%s %s) withheld for good (review %r, via %r) but derives from no "
                          "withheld message" % (where, r["id"], r["sid"], r.get("source_message_id"), r.get("role"),
                                                stone.get("review") if stone else None,
                                                stone.get("via") if stone else None))


def w_text_in(run: Run, w: Withhold, snap: hs.Snapshot, st: dict[str, Any] | None = None) -> str | None:
    """The withheld text as this backup holds it, found where the plan says the Command Center finds it (4.6, 5.3 R4,
    5.5, owner Q6; deviation 245): the withheld JARVIS row itself (session, row, role, time, and uid when both have
    one), else a Hermes copy identity of it (the same uid, time and role), else the JARVIS row of a certain member of
    its decision that holds the very words (A2 checks those derive from it). None when the backup holds none of them:
    a copy that only text, or a tool call and time without a uid, links to it does not count, nor does a row that
    merged another message into W's words (owner decision A, deviation 292), and with no original left the evidence
    is 'the withheld text is not in this backup' (5.3 R4). An edit JARVIS made in place to the
    withheld row (a prune) is what the backup holds: edits inside a withheld family are out of contract (section 3)."""
    if not norm(w.text):
        return None
    for (sid, row), v in snap.rows.items():
        if (sid, row, v.role, tkey(v.ts)) == (w.sid, int(w.row), w.role, tkey(w.at)) and not (
                w.uid and v.uid and w.uid != v.uid) and norm(v.content) != norm(hs.STUB) and not (
                set(v.absorbed) - set(w.absorbed)):
            return v.content
    if w.uid:  # Hermes's prune placeholder is not the withheld text; its archived twin is (deviation 265)
        piece = None
        for v in snap.rows.values():
            if v.uid == w.uid and (tkey(v.ts), v.role) == (tkey(w.at), w.role) and norm(v.content) != norm(hs.STUB) \
                    and not set(v.absorbed) - set(w.absorbed):  # a later fold of W holds more than W (deviation 275)
                if set(w.absorbed) - set(v.absorbed) - {w.uid}:
                    # D-48 (rebuild S4, 13626): a copy made before Hermes folded another message into W holds W's
                    # first part only; a copy that holds W whole is W's text here, the piece only when none does
                    piece = piece if piece is not None else v.content
                    continue
                return v.content
        if piece is not None:
            return piece
    rows, copies = set(), set()
    for s in (st or {}).get("stones", []):
        if s.get("review") == "pending" or w.key not in (s.get("source_key"), s.get("anchor_key")):
            continue
        if s.get("review") == "kept" and "identical" not in (s.get("relations") or []):
            continue  # a kept hold is known to hold the withheld words only when its evidence said so (C3)
        at = tkey(epoch(s.get("source_at")))
        if str(s.get("source_row_id") or "").isdigit():
            rows.add((s.get("source_session_id"), int(s["source_row_id"]), s.get("role"), at))
        if s.get("message_uid"):
            copies.add((s["message_uid"], at, s.get("role")))
    wbody = norm(peel(w.text)[0])
    for (sid, row), v in snap.rows.items():
        # the very words: equal, or 'identical' as plan 4.6 defines it (the same authored text inside Hermes's
        # wrappers, e.g. W restated after the replay header under W's own uid), as the member's evidence said
        # (fixer, deviation 288)
        same = norm(v.content) == norm(w.text) or (bool(wbody) and norm(peel(v.content)[0]) == wbody)
        if not same or norm(v.content) == norm(hs.STUB) or not run.sim.leaks(v.tag, w.lms):
            continue
        if set(v.absorbed) - set(w.absorbed):
            # owner decision A (deviation 292): W's words count as held by this backup only as W's own row or an
            # untouched copy; inside a row that merged another message (Hermes's summary carrier of W, a fold)
            # the evidence is 'nothing to compare' and the one-click release is refused (fail-closed)
            continue
        if (sid, row, v.role, tkey(v.ts)) in rows or (v.uid and (v.uid, tkey(v.ts), v.role) in copies):
            return v.content
    return None


def w_piece(w: Withhold, snap: hs.Snapshot, wt: str | None) -> bool:
    """D-47 (rebuild S4, RECON-64): the withheld row as this backup holds it (its own row, or its uid copy) absorbed
    fewer messages than when it was withheld (a withheld fold whose copy here lost a message it had folded in): the
    text in hand is a piece of what the admin withheld. A hold may then say 'not in this backup' for it (the rest of
    the withheld text is not here); its relations to the piece are admitted as before, and 'none' is still judged
    against the text as withheld (O5)."""
    if wt is None or not w.absorbed:
        return False
    for (sid, row), v in snap.rows.items():
        if norm(v.content) != norm(wt):
            continue
        own = (sid, row, v.role, tkey(v.ts)) == (w.sid, int(w.row), w.role, tkey(w.at))
        copy = bool(w.uid) and v.uid == w.uid and (tkey(v.ts), v.role) == (tkey(w.at), w.role)
        if own or copy:
            return bool(set(w.absorbed) - set(v.absorbed) - {w.uid})
    return False


def w_text_present(run: Run, w: Withhold, snap: hs.Snapshot, st: dict[str, Any] | None = None) -> bool:
    return w_text_in(run, w, snap, st) is not None




NA_EVERY_TEXT = True  # O5 (N-A): 'none' against every withheld text (a mutant 'own only' must be killed)
LATER_UNDECIDED = True  # O9: 'later_withhold' is undecided (a mutant reading it as decided must be killed)


def check_evidence(run: Run, prop: str, holds: list[dict[str, Any]], st: dict[str, Any], snap: hs.Snapshot,
                   where: str) -> None:
    """A3 / C3-evidence / O4: every hold's evidence matches a recomputation. Option C (rebuild R5): a hold shows its
    relations to every withheld text in hand (group_relations), read under fold and every demanded reading; what it
    shows must be admitted (computed, or a quote span or other may-show reading of SOME withheld text), and it must
    show a demanded relation to its own withheld text when there is one. 'none' needs every withheld text in hand and
    no relation to any (O5, N-A); 'later_withhold' is undecided (O9); 'other_not_compared' and 'not_in_backup' say
    what was not compared."""
    rows = {r["id"]: r for r in st["rows"]}
    comps = {w.key: decision_family(run, w, snap, st) for w in run.withholds}
    group = truth_group(run, snap, st) if run.withholds else set()
    for h in holds:
        rid = h.get("message")
        r = rows.get(rid)
        if r is None:
            continue
        rep, raw = reported_relations(h)
        if not rep:
            run.bad(prop, "%s: hold %s shows no evidence (fields %s)" % (where, rid, sorted(h)))
            continue
        vs = backup_versions(run, r, snap)
        if not vs:
            continue
        vs = vs + [x for x in (stored_form(v) for v in vs) if x is not None]  # D-49
        stone = st["stone_by_key"].get(r["source_key"]) or {}
        decided = stone.get("decided_on") or ""
        if decided and snap.sha256 and decided != snap.sha256:
            # a hold decided on another backup (an earlier decision's, untouched by this Settle) carries the
            # evidence computed on that backup (plan 4.4, 4.6; deviation 248): recomputed there, not here
            continue
        anchor = stone.get("anchor_key") or ""
        own = [w for w in run.withholds if w.key == anchor] or run.withholds
        fam_ws = [w for w in run.withholds if r["sid"] in comps[w.key]]
        in_group = r["sid"] in group or r["sid"] in fam_ws_sids(run, fam_ws)
        ws = own + [w for w in run.withholds if w not in own]
        rep_text = rep & set(TEXT_RELATIONS)
        allowed_all: set[str] = set()
        per_w: list[tuple[Withhold, str | None, list[tuple[str, set[str]]]]] = []
        for w in ws:
            wt = w_text_in(run, w, snap, st)
            views = [w.text] if wt is None else [wt] + ([w.text] if norm(wt) != norm(w.text) else [])
            done = []
            for view in views:
                computed, allowed = set(), set()
                for v in vs:
                    computed |= text_relations(v.content, view)
                    dem, alw = relations_c(version_fields(v), view, group=in_group, carrier=not in_group)
                    computed |= dem
                    allowed |= alw
                    if not in_group and family_part_of(r, w, comps, stone):
                        allowed |= relations_c(version_fields(v), view, group=True)[1]
                allowed |= computed
                allowed_all |= allowed
                done.append((view, computed))
            per_w.append((w, wt, done))
        ok = False
        why = []
        undecided = LATER_UNDECIDED and "later_withhold" in rep
        for w, wt, done in per_w:
            if w not in own:
                continue
            for view, computed in done:
                if rep & {"differs", "none"} and (computed or undecided):
                    honest = False
                elif undecided or "other_not_compared" in rep:
                    honest = rep_text <= allowed_all and (undecided or not computed or bool(rep_text & computed))
                elif wt is None:
                    honest = ("not_in_backup" in rep and rep_text <= allowed_all) or (not rep_text and not computed) \
                        or (bool(rep_text) and rep_text <= allowed_all)
                else:
                    honest = rep_text <= allowed_all and (not computed or bool(rep_text & computed)) \
                        and ("not_in_backup" not in rep or w_piece(w, snap, wt))
                if honest:
                    ok = True
                why.append("%scomputed %s; held %r vs withheld %r" % ("W absent, " if wt is None else "",
                                                                      sorted(computed), vs[-1].content[:90],
                                                                      view[:90]))
        if ok and "none" in rep:
            # O5 (N-A): 'none' gates the one-click release: every withheld text in hand, none related
            for w, wt, done in per_w:
                if not NA_EVERY_TEXT and w not in own:
                    continue
                if wt is None:
                    # a withheld text this backup does not hold (as the oracle finds it): compared nowhere here; the
                    # product's own 'other_not_compared' / 'not_in_backup' states are its integration tests' (RECON-47)
                    continue
                related = set()
                for view, computed in done:
                    related |= computed
                if related:
                    ok = False
                    why.append("'none' but %s to the withheld %r" % (sorted(related), (wt or "")[:60]))
        if not ok:
            run.bad(prop, "%s: hold %s (%s:%s %s) evidence %s %r does not match the recomputation (%s) [anchor %s, "
                          "via %r, own %s]" % (
                where, rid, r["sid"], r.get("source_message_id"), r.get("role"), sorted(rep), raw[:3],
                "; ".join(why), anchor, stone.get("via"), ["%s:%s" % (w.sid, w.row) for w in own]))


CC_CUT_NOTE = "\n[\u2026 cut: the full text is in the JARVIS backup]"
CC_STORED = {"tool": 4_000}  # spec: tool results are stored cut to 4,000 characters (user/assistant: 50,000)


def stored_form(v: hs.Version) -> hs.Version | None:
    """D-49 (rebuild S4, 13658): the text the Command Center stores for a backup row the contract cuts (a tool result
    longer than 4,000 characters, user and assistant rows longer than 50,000; at a space near the limit, with the
    cut note): a hold's evidence computed on the stored row is the evidence of that text. None when nothing is cut.
    Used as one more version of the row in check_evidence: a relation of the stored text is admitted and meets the
    demand; a hold that shows no relation ('none') while the full text has one is still reported."""
    limit = CC_STORED.get(v.role, 50_000)
    text = v.content or ""
    if len(text) <= limit:
        return None
    room = max(0, limit - len(CC_CUT_NOTE))
    head = text[:room]
    space = max(head.rfind(" "), head.rfind("\n"))
    if space > room - 400:
        head = head[:space]
    return dataclasses.replace(v, content=head.rstrip() + CC_CUT_NOTE)


def family_part_of(row: dict[str, Any], w: Withhold, comps: dict[str, set[str]], stone: dict[str, Any]) -> bool:
    """D-46 (rebuild S4, RECON-63): a hold of a row of this decision's family (plan 5.1, i22's family: the sessions
    its stones bind and their copy links) outside the ground-truth group, held by the family's own rules (Settle's
    'found when finishing decisions', a later backup's 'new in a later backup'; never a per-row carrier hold), may show
    what the family's readings give (the group reading): 'part_of' where a value of it is part of the withheld text,
    'contains' where a withheld text of fewer than 16 characters of words appears as a word sequence in it, and
    'identical' / 'contains' of a generic withheld text, which a carrier reading drops (O-D4; 13880: W 'Sounds good.'
    restated, a session_search read's value): the product computes those holds with i22's family rules
    (identity.family_relations, as i22's oracle admitted for every family hold: deviation 274), now read per value.
    Admitted only (never demanded); a carrier hold, a row outside the family and a relation no value gives are still
    reported (test_oracle_family_part_of_s4.py)."""
    return row.get("sid") in comps.get(w.key, set()) and (stone.get("via") or "") != CARRIER_VIA


def fam_ws_sids(run: Run, ws: list[Withhold]) -> set[str]:
    return {w.sid for w in ws}


async def scripted_review(run: Run, holds: list[dict[str, Any]], st: dict[str, Any], where: str) -> list[int]:
    """Release exactly the holds the ground truth calls independent; keep the rest. Returns released row ids."""
    rows = {r["id"]: r for r in st["rows"]}
    released = []
    for h in holds:
        rid = h.get("message")
        r = rows.get(rid)
        if r is None:
            continue
        vs = versions_for(run.sim, r)
        if vs and not run.derives(vs):
            resp = await run.api.release(rid)
            untyped = getattr(run.api, "untyped", None)
            if untyped is not None and untyped.status_code < 400 and h.get("confirm_required"):
                # O6 (B6): a hold one click could not release is released per row only with the typed word
                run.bad("O6", "%s: hold %s (confirm required, relations %s) was released without the typed word" % (
                    where, rid, h.get("relations")))
            if resp.status_code >= 400:
                run.bad("review", "%s: release of independent hold %s refused: %s %s" % (
                    where, rid, resp.status_code, resp.text[:200]))
            else:
                released.append(rid)
                # the content the release covers (C3: the exact row and its content): its version in the backup the
                # hold was decided on (else the newest imported one)
                stone = st["stone_by_key"].get(r["source_key"]) or {}
                by_sha = {snap.sha256: snap for snap in run.imported}
                decided = by_sha.get(stone.get("decided_on") or "") or (run.imported[-1] if run.imported else None)
                texts = [v.content for v in backup_versions(run, r, decided)] if decided is not None else []
                for key in (r["source_key"], "id:%s" % r["id"]):
                    run.released.setdefault(key, []).extend(t for t in texts if t not in run.released[key])
        else:
            resp = await run.api.keep(rid)
            if resp.status_code >= 400:
                run.bad("review", "%s: keep of hold %s refused: %s %s" % (where, rid, resp.status_code,
                                                                           resp.text[:200]))
    return released


def check_watch(run: Run, prop: str, records: list[dict[str, Any]], where: str, final: dict[str, Any],
                fam: set[str], tc: dict[str, Any], fam_tc: set[str]) -> None:
    """C1 at any moment: no withheld text is ever committed readable on live during the import with the decisions
    file. Every record is judged unless its row is provably a plain import's readable row on both installations: in
    no family (live's or the test copy's: A1's scope), readable on live when the import ends, and readable (or not
    stored) on the test copy. Such a row is a verbatim copy in a conversation with no recorded link (plan 5.4, owner
    Q8 (b); section 8 residual, e.g. a rotation whose parent JARVIS deleted before any import saw it): C1 requires it
    to end readable exactly as on the test copy, so it is necessarily committed readable at some moment and a check
    that flags it cannot be met by any implementation of the contract. A4 judges it against a plain import. A row
    that ends withheld on either side, a family row, and a row the import wrote and then removed are all judged
    (integrator deviation 279; fixer deviation 284: the test copy's state is used too, fail-closed)."""
    live_rows = {r["id"]: r for r in final["rows"]}
    tc_rows = {(r["sid"], str(r.get("source_message_id"))): r for r in tc["rows"]}
    for rec in records:
        if rec["withheld"] or not norm(rec.get("content")):
            continue
        end = live_rows.get(rec["id"])
        mine = tc_rows.get((rec["sid"], str(rec.get("source_message_id"))))
        plain = (rec["sid"] not in fam and rec["sid"] not in fam_tc and end is not None and not end.get("withheld")
                 and (mine is None or not mine.get("withheld")))
        if plain and norm(rec.get("content")) == norm(end.get("content")):
            continue
        vs = versions_for(run.sim, rec)
        if run.leaks(vs):
            run.bad(prop, "%s: row %s (%s:%s) was committed readable with withheld words during the import" % (
                where, rec["id"], rec["sid"], rec["source_message_id"]))


def is_reply_quote_hold(row: dict[str, Any], st: dict[str, Any], run: Run) -> bool:
    """Owner Q8(a) default: a reply quoting a withheld message is held even outside a withheld conversation."""
    stone = st["stone_by_key"].get(row.get("source_key"))
    if not (row.get("withheld") and stone and stone.get("review") == "pending"):
        return False
    for v in versions_for(run.sim, row):
        for quote in reply_quotes(v.content):
            if quote and any(norm(quote) and norm(quote) in norm(w.text) and len(quote) >= 16 for w in run.withholds):
                return True
    return False


CARRIER_EXCUSE = True  # narrowings (rebuild R5): each has a self-test with a mutant that loosens it
KEPT_CARRIER_EXCUSE = True
REKEYED = True


def compare_outside(run: Run, prop: str, mine: dict[str, Any], plain: dict[str, Any], fam: set[str],
                    where: str, last: hs.Snapshot | None = None) -> None:
    a, b = by_key(mine), by_key(plain)
    diffs, waiting = [], []
    for k in sorted(set(a) | set(b)):
        ra, rb = a.get(k), b.get(k)
        sid = (ra or rb)["sid"]
        if sid in fam:
            continue
        if ra is not None and is_reply_quote_hold(ra, mine, run):
            continue
        # option C (rebuild R5): a carrier outside every group held on its own (an admitted reading relates it to a
        # withheld text), and a kept carrier whose own row once carried a withheld text (round 3 'kept_carrier')
        if ra is not None and (CARRIER_EXCUSE and is_carrier_hold(ra, mine, run)
                               or KEPT_CARRIER_EXCUSE and kept_carrier(ra, mine, run)):
            continue
        sa = (bool(ra.get("withheld")), content_hash(ra.get("content"))) if ra else None
        sb = (bool(rb.get("withheld")), content_hash(rb.get("content"))) if rb else None
        if sa != sb and released_waiting_outside(run, ra, mine, rb, last):
            waiting.append(k)
            continue
        if sa != sb:
            diffs.append("%s (%s) %s vs plain %s" % (k, sid, sa, sb))
    if waiting:
        run.notes.append("%s: %s: %d rows outside the family the admin released wait for their text (no backup "
                         "imported since holds them; S4, D-50): %s" % (prop, where, len(waiting),
                                                                      "; ".join(waiting[:4])))
    if diffs:
        run.bad(prop, "%s: %d rows outside the family differ from a plain import: %s" % (
            where, len(diffs), "; ".join(diffs[:5])))


def released_waiting_outside(run: Run, ra: dict[str, Any] | None, mine: dict[str, Any], rb: dict[str, Any] | None,
                             last: hs.Snapshot | None) -> bool:
    """A4 / B4 (stabilisation S4, D-50, as RECON-57's C1 excuse): a row outside the family the admin released (a
    carrier hold, typed or one click) waits for its text (released_pending: withheld, released, no stone) because the
    installation's last backup no longer holds that JARVIS row (the next import cannot bring its text back, and the
    Command Center never kept the text: owner question 6); the plain import shows it readable with exactly the text
    released for that very row (run.released). Never when the last backup holds the row, when the row is held or not
    released, or when the plain import shows any other text."""
    if last is None or ra is None or rb is None or rb.get("withheld") or not released_pending(ra, mine):
        return False
    rid = str(ra.get("source_message_id") or "")
    if not rid.isdigit() or (ra["sid"], int(rid)) in last.rows:
        return False
    texts = run.released.get(ra["source_key"]) or run.released.get("id:%s" % ra["id"]) or []
    return bool(norm(rb.get("content"))) and any(cc_redacted_of(rb.get("content"), t) for t in texts)


def check_stored_unchanged(run: Run, prop: str, pre: dict[str, Any], post: dict[str, Any], fam: set[str],
                           where: str, snap: hs.Snapshot | None = None, group: set[str] = frozenset(),
                           carrier_only: set[str] = frozenset()) -> None:
    """B1 over i22's family; option C (rebuild R5) adds the group's conversations for every check but the
    disappearance of a row (Hermes rewinds rows of any conversation; i22 promised to keep only the family's).

    `carrier_only` (rebuild S3, D-45): conversations of `fam` that only option C's pending per-row carrier holds bind
    (carrier_only_sids: no group conversation, no stone i22 made, no link, uid or copied history of i22's family).
    i22 made no stone there, so i22's promise never covered their readable rows: those follow a plain import
    (RECON-27, carriers outside the groups are held per row) and are reported only when their new text carries a
    withheld message's words (ground truth). Every withheld stored row there is still checked as a family row."""
    a, b = by_key(pre), by_key(post)
    for k, ra in a.items():
        if ra["sid"] not in fam and ra["sid"] not in group:
            continue
        rb = b.get(k)
        if (ra["sid"] in carrier_only and ra["sid"] not in group and not ra.get("withheld")
                and not released_pending(ra, pre)):
            if rb is not None and not rb.get("withheld") and norm(rb.get("content")) != norm(ra.get("content")) \
                    and run.leaks(versions_for(run.sim, rb)):
                run.bad(prop, "%s: stored row %s (%s) of a conversation a carrier hold binds changed to a text "
                              "carrying withheld words: %r" % (where, k, ra["sid"], (rb.get("content") or "")[:40]))
            continue
        if rb is None and REKEYED:
            rb = rekeyed(ra, post, snap)  # round 3 (13560): the same archive row under its uid key
        if rb is None and ra["sid"] not in fam:
            continue
        if rb is None:
            run.bad(prop, "%s: stored family row %s (%s) disappeared" % (where, k, ra["sid"]))
            continue
        if released_pending(ra, pre):  # C2(a): the admin released it; its text comes back now (deviation 240)
            continue
        if ra.get("withheld") and not rb.get("withheld"):
            run.bad(prop, "%s: stored family row %s (%s) became readable" % (where, k, ra["sid"]))
        elif not rb.get("withheld") and norm(rb.get("content")) != norm(ra.get("content")):
            run.bad(prop, "%s: stored family row %s (%s) changed its text %r -> %r" % (
                where, k, ra["sid"], (ra.get("content") or "")[:40], (rb.get("content") or "")[:40]))
        elif not norm(ra.get("content")) and norm(rb.get("content")):
            run.bad(prop, "%s: stored family row %s (%s) gained text" % (where, k, ra["sid"]))


ANY_TIME = "any time"


def certain_stone_identities(st: dict[str, Any]) -> tuple[set, set, set]:
    exact, copy, uids = set(), set(), set()
    for s in st["stones"]:
        if s.get("review") == "pending":
            continue
        # a stone with no time is the exact JARVIS row at any time, as the 0008 trigger matches it (t_at IS NULL): a
        # recorded duplicate with no row of its own at the withhold (plan 4.5 member) (integrator, deviation 281)
        at = tkey(epoch(s.get("source_at"))) if s.get("source_at") else ANY_TIME
        exact.add((s.get("source_session_id"), str(s.get("source_row_id")), s.get("role"), s.get("tool_call_id") or "",
                   at))
        if s.get("message_uid"):
            copy.add((s.get("message_uid"), at, s.get("role"), s.get("tool_call_id") or ""))
            uids.add(s.get("message_uid"))
    return exact, copy, uids


def check_new_certain(run: Run, prop: str, pre: dict[str, Any], post: dict[str, Any], listed: set[int],
                      fam: set[str], where: str) -> None:
    before = by_key(pre)
    exact, copy, uids = certain_stone_identities(post)
    for r in post["rows"]:
        if r["sid"] not in fam or not admin_withheld(r, post) or r["id"] in listed:
            continue
        old = before.get(r["source_key"])
        if old is not None and old.get("withheld"):
            continue
        vs = versions_for(run.sim, r)
        if not vs:
            continue
        if not run.derives(vs):
            run.bad(prop, "%s: new certain row %s (%s:%s) derives from no withheld message" % (
                where, r["id"], r["sid"], r.get("source_message_id")))
            continue
        ok = False
        for v in vs:
            at = tkey(v.ts)
            if (v.sid, str(v.row), v.role, v.call or "", at) in exact or (
                    v.sid, str(v.row), v.role, v.call or "", ANY_TIME) in exact:
                ok = True
            if v.uid and (v.uid, at, v.role, v.call or "") in copy:
                ok = True
            if set(v.absorbed) & uids:
                ok = True
        if not ok:
            run.bad(prop, "%s: new certain row %s (%s:%s %s) has no exact, copy or absorbed identity of a certain "
                          "stone" % (where, r["id"], r["sid"], r.get("source_message_id"), r.get("role")))


def check_safe_copies(run: Run, prop: str, pre: dict[str, Any], post: dict[str, Any], fam: set[str],
                      where: str, carrier_only: set[str] = frozenset()) -> None:
    """B5. `carrier_only` (rebuild S3, D-45): a new readable row of a conversation only option C's pending per-row
    carrier holds bind (no stone i22 made: i22's family never held it) follows a plain import; it is reported when
    it carries a withheld message's words (ground truth). Its rows are not safe copies of the family's either."""
    before = by_key(pre)
    readable = set()
    for r in pre["rows"]:
        if r["sid"] in fam and r["sid"] not in carrier_only and not r.get("withheld"):
            readable.add((r.get("role"), r.get("tool_call_id") or "", tkey(epoch(r.get("at")) or 0.0),
                          norm(r.get("content"))))
    for r in post["rows"]:
        if r["sid"] not in fam or r.get("withheld") or r["source_key"] in before:
            continue
        if r["sid"] in carrier_only:
            if norm(r.get("content")) and run.leaks(versions_for(run.sim, r)):
                run.bad(prop, "%s: new row %s (%s:%s %s) of a conversation a carrier hold binds is readable and "
                              "carries withheld words: %r" % (where, r["id"], r["sid"], r.get("source_message_id"),
                                                             r.get("role"), (r.get("content") or "")[:60]))
            continue
        k = (r.get("role"), r.get("tool_call_id") or "", tkey(epoch(r.get("at")) or 0.0), norm(r.get("content")))
        if k not in readable:
            run.bad(prop, "%s: new family row %s (%s:%s %s) is readable but equals no readable stored row of the "
                          "family: %r" % (where, r["id"], r["sid"], r.get("source_message_id"), r.get("role"),
                                         (r.get("content") or "")[:60]))


# --- option C: the group ground truth and the checks O1-O11 (rebuild R5, RECON-39) --------------------------------
# O1  every conversation of a withheld message's group (the ground truth of the links any imported backup showed:
#     continuation, branch and reset both ways; a delegated sub-agent and a markerless child from the parent down only,
#     O-D1; a row carrying or absorbing a certain stone's uid; copied history for a conversation no backup gave a
#     parent) is recorded as a member or uncertain
# O2  every stored row of such a conversation is held, unless the admin released that exact row and it still holds the
#     released text (released_ok, cc_redacted_of)
# O3  (B9) no readable row outside the groups carries a withheld text the Command Center had in hand by a demanded
#     reading (fold, generic-v1, prune line, fwords, decode cap: not_checked)
# O4  (A3, C3-evidence) every hold's evidence is honest: what it shows is admitted (spans only on the may-show side)
#     and it shows a demanded relation when there is one
# O5  (N-A) 'none' only with every withheld text in hand and none related
# O6  (B6, D3) one click only for exactly 'none', a user or assistant row, the plain form, no uncertain link, no later
#     withhold; an untyped per-row release of anything else is refused
# O7  a released row holds no text until the next import, then exactly the released text (released_ok)
# O8  (N-B) a released row that carries another withheld text is held again
# O9  'later_withhold' is undecided: never 'none', never one click
# O10 (B1, B7) imports, re-imports, rollbacks and restores never make a held row readable (rekeyed: the same archive
#     row under the uid the backup gives it is the same row)
# O11 a group conversation's title is hidden; a title outside the groups that carries a withheld text is hidden

GROUP_VIAS_C = ("held with its conversation: a withheld conversation group",
                "held with its conversation: an uncertain link to a withheld conversation")
CARRIER_VIA = "carries the withheld text"
DOWN_ONLY = frozenset({"delegated", "child"})  # O-D1: from the parent down only (the parent side is pending: B, D)


def visible_links(run: Run, current: hs.Snapshot) -> set[tuple[str, str, str]]:
    """(child, parent, kind) of every link an imported backup (or the one in hand) showed (its parent_session_id or a
    marker naming the parent)."""
    out: set[tuple[str, str, str]] = set()
    for snap in run.imported + [current]:
        for child, (parent, kind) in snap.links.items():
            out.add((child, parent, kind))
    return out


def group_seeds(run: Run, st: dict[str, Any]) -> tuple[set[str], set[str]]:
    """The conversations and uids certain stones bind (the withheld rows, their certain members, a kept hold; never
    a pending hold nor a hold kept only with its conversation)."""
    sids = {w.sid for w in run.withholds}
    uids = {w.uid for w in run.withholds if w.uid}
    keys = {w.key for w in run.withholds}
    for s in st["stones"]:
        if s.get("review") == "pending" or s.get("via") in GROUP_VIAS_C:
            continue
        if s.get("anchor_key") not in keys and s.get("source_key") not in keys:
            continue
        if s.get("source_session_id"):
            sids.add(s["source_session_id"])
        if s.get("message_uid"):
            uids.add(s["message_uid"])
    return sids, uids


def truth_group(run: Run, current: hs.Snapshot, st: dict[str, Any]) -> set[str]:
    """O1's demand: the conversations every withheld message's group must hold (ground truth over the visible links,
    O-D1 directions, shared uids, copied history of a conversation no imported backup gave a parent)."""
    links = visible_links(run, current)
    seeds, uids = group_seeds(run, st)
    parented = {c for c, _, _ in links} | {sid for snap in run.imported + [current]
                                          for sid, s in snap.sessions.items() if s.get("parent_session_id")}
    out = set(seeds)
    snaps = run.imported + [current]
    while True:
        before = set(out)
        for child, parent, kind in links:
            if parent in out:
                out.add(child)
            elif child in out and kind not in DOWN_ONLY:
                out.add(parent)
        for snap in snaps:
            for (sid, _), v in snap.rows.items():
                if sid not in out and ((v.uid and v.uid in uids) or set(v.absorbed) & uids):
                    out.add(sid)
        keys = set()
        for snap in snaps:
            for (sid, _), v in snap.rows.items():
                if sid in out and v.ts is not None:
                    keys.add((v.role, v.call or "", tkey(v.ts)))
        for snap in snaps:
            for sid, sess in snap.sessions.items():
                if sid in out or sid in parented:
                    continue
                start = sess.get("started_at") or 0
                if any(rsid == sid and v.ts <= start + 1e-6 and (v.role, v.call or "", tkey(v.ts)) in keys
                       for (rsid, _), v in snap.rows.items()):
                    out.add(sid)
        if out == before:
            return out


def uncertain_doubt(run: Run, current: hs.Snapshot, sid: str) -> bool:
    """The contract's uncertain state is justified for this conversation: a parent that no imported backup holds, two
    parents, or a link only by copied-looking rows (O-D1, RECON-3)."""
    known = {x for snap in run.imported + [current] for x in snap.sessions}
    claims = set()
    for snap in run.imported + [current]:
        s = snap.sessions.get(sid)
        if s is None:
            continue
        if s.get("parent_session_id"):
            claims.add(s["parent_session_id"])
        cfg = json.loads(s.get("model_config") or "{}") or {}
        claims |= {v for k, v in cfg.items() if k.startswith("_") and k.endswith("_from") and isinstance(v, str)}
    return bool(claims - known) or len(claims) > 1 or not claims


async def groups_state() -> dict[str, Any]:
    recorded = await cc.withheld_sessions()
    return {"members": {r["source_session_id"] for r in recorded},
            "uncertain": {r["source_session_id"] for r in recorded if r.get("state") == "uncertain"},
            "hidden_titles": await cc.hidden_titles()}


def released_ok(run: Run, r: dict[str, Any]) -> bool:
    """O7: an admin released this exact row, and it holds the text released (with only the Command Center's own marks
    in place of spans: cc_redacted_of)."""
    texts = run.released.get(r["source_key"]) or run.released.get("id:%s" % r["id"])
    if not texts:
        return False
    return any(cc_redacted_of(r.get("content"), t) for t in texts)


def demanded_for(run: Run, fields: list[str], w: "Withhold", snap: hs.Snapshot, st: dict[str, Any], *,
                 group: bool) -> set[str]:
    """The relations a row's fields have with a withheld text by the demanded readings (as withheld, and as the
    backup holds it)."""
    out: set[str] = set()
    views = {w.text}
    wt = w_text_in(run, w, snap, st)
    if wt is not None:
        views.add(wt)
    for view in views:
        out |= relations_c(fields, view, group=group, carrier=not group)[0]
    return out


def check_c(run: Run, where: str, st: dict[str, Any], gs: dict[str, Any], snap: hs.Snapshot,
            holds: list[dict[str, Any]] | None = None) -> set[str]:
    """O1, O2, O3, O6, O8, O9, O11 on one state. Returns the ground-truth group."""
    group = truth_group(run, snap, st)
    if not run.withholds:
        return group
    stored_sids = {r["sid"] for r in st["rows"]}
    for sid in sorted(group & stored_sids):
        if sid not in gs["members"]:
            run.bad("O1", "%s: conversation %s of a withheld message's group is not recorded (member or uncertain)"
                    % (where, sid))
    for r in st["rows"]:
        fields = row_fields(r)
        if r.get("withheld") or not fields:
            continue
        if norm(r.get("content")) == norm(hs.STUB) and len(fields) == 1:
            continue  # Hermes's prune placeholder carries nothing
        vs = versions_for(run.sim, r)
        if r["sid"] in group or r["sid"] in gs["uncertain"]:
            if not released_ok(run, r):
                run.bad("O2", "%s: row %s (%s:%s %s) of group conversation %s is readable and was never released "
                              "as it is: %r" % (where, r["id"], r["sid"], r.get("source_message_id"), r.get("role"),
                                                r["sid"], (r.get("content") or "")[:60]))
                continue
        hit = []
        for w in run.withholds:
            if not any(run.sim.leaks(v.tag, w.lms) for v in vs):
                continue
            in_hand = (w_text_in(run, w, snap, st) is not None or r["source_key"] in run.at_withhold.get(w.key, ()))
            rel = demanded_for(run, fields, w, snap, st, group=r["sid"] in group) - {"none"}
            if rel and in_hand:
                hit.append((w, rel))
            elif rel:
                run.notes.append("%s: row %s carries %s:%s by %s while its text was never in hand here (13639 "
                                 "shape)" % (where, r["id"], w.sid, w.row, sorted(rel)))
        for w, rel in hit:
            if not NB_CHECK and released_ok(run, r):
                continue
            prop = "O8" if released_ok(run, r) else ("O3" if r["sid"] not in group else "O2")
            run.bad(prop, "%s: readable row %s (%s:%s %s) carries the withheld %s:%s (%s): %r" % (
                where, r["id"], r["sid"], r.get("source_message_id"), r.get("role"), w.sid, w.row, sorted(rel),
                (r.get("content") or "")[:70]))
    # O11: titles
    seen: set[str] = set()
    for r in st["rows"]:
        sid = r["sid"]
        title = norm(r.get("session_title"))
        if sid in seen or not title:
            continue
        seen.add(sid)
        if sid in group:
            run.bad("O11", "%s: group conversation %s shows its title %r" % (where, sid, title[:40]))
            continue
        tag = snap.title_tags.get(sid)
        if tag is not None and run.sim.lineage(tag) & run.wlms:
            for w in run.withholds:
                if run.sim.lineage(tag) & w.lms and relations_c([title], w.text, group=False, carrier=True)[0]:
                    run.bad("O11", "%s: title of %s carries the withheld %s:%s: %r" % (where, sid, w.sid, w.row,
                                                                                         title[:40]))
                    break
    if holds is not None:
        check_one_click(run, where, holds, st, snap, gs)
    run.notes.append("check_c %s: group %d, recorded %d (uncertain %d), carrier holds %d" % (
        where, len(group), len(gs["members"]), len(gs["uncertain"]),
        sum(1 for x in st["stones"] if x.get("via") == CARRIER_VIA)))
    return group


ONE_CLICK_ROLES = ("user", "assistant")
NB_CHECK = True  # O8 (N-B): a released row is compared with every other withheld text (mutant rel3:oracle_nb_off)


def check_one_click(run: Run, where: str, holds: list[dict[str, Any]], st: dict[str, Any], snap: hs.Snapshot,
                    gs: dict[str, Any]) -> None:
    """O6, O9: a hold offered for one click is exactly 'none', a user or assistant row, the plain form, no uncertain
    link, no later withhold; and its 'none' holds against every withheld text (O5)."""
    rows = {r["id"]: r for r in st["rows"]}
    for h in holds:
        if not h.get("one_click"):
            continue
        r = rows.get(h.get("message"))
        rel = h.get("relations") if isinstance(h.get("relations"), list) else None
        why = []
        if rel is not None and list(rel) != ["none"]:
            why.append("relations %s" % rel)
        if (h.get("role") or (r or {}).get("role")) not in ONE_CLICK_ROLES:
            why.append("role %s" % h.get("role"))
        if h.get("form") != "plain":
            why.append("form %r" % h.get("form"))
        if h.get("uncertain") or (r is not None and r["sid"] in gs["uncertain"]):
            why.append("uncertain link")
        if h.get("later_withhold"):
            why.append("a later withhold")
        if why:
            run.bad("O6", "%s: hold %s is offered for one click: %s" % (where, h.get("message"), "; ".join(why)))


def is_carrier_hold(row: dict[str, Any], st: dict[str, Any], run: Run) -> bool:
    """Option C (RECON-27): a carrier outside every group held on its own: a pending or kept CARRIER_HOLD whose row
    (some version of it) relates to a withheld text by an admitted reading, or could not be read in full."""
    stone = st["stone_by_key"].get(row.get("source_key"))
    if not (row.get("withheld") and stone and stone.get("via") == CARRIER_VIA):
        return False
    for v in versions_for(run.sim, row):
        for w in run.withholds:
            if relations_c(version_fields(v), w.text, group=False, carrier=True)[1]:
                return True
    return False


def kept_carrier(row: dict[str, Any], st: dict[str, Any], run: Run) -> bool:
    """Round 3 (step 2) excuse for A4/B4: the row's own key has a kept carrier hold, and one of that exact row's own
    versions carried a withheld text (Hermes later rewrote it; live keeps holding it while a plain import shows it)."""
    stone = st["stone_by_key"].get(row.get("source_key"))
    if not (stone and stone.get("via") == CARRIER_VIA and stone.get("review") == "kept"):
        return False
    rid = str(row.get("source_message_id") or "")
    if not rid.isdigit():
        return False
    return any(run.leaks([v]) for v in run.sim.versions_of(row["sid"], int(rid)))


def rekeyed(row: dict[str, Any], post: dict[str, Any], snap: hs.Snapshot | None) -> dict[str, Any] | None:
    """Round 3 (step 3, 13560): a stored row whose key moved to the uid key the backup gives that very JARVIS row
    (the same archive row id; the backup's row at that session and row id has that uid). None otherwise."""
    if snap is None:
        return None
    for rb in post["rows"]:
        if rb["id"] != row["id"] or rb["source_key"] == row["source_key"]:
            continue
        rid = str(row.get("source_message_id") or "")
        v = snap.rows.get((row["sid"], int(rid))) if rid.isdigit() else None
        if v is not None and v.uid and rb["source_key"] == "jarvis:message:uid:" + v.uid:
            return rb
    return None
