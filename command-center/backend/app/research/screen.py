"""The outbound screen: every research query is checked before anything is stored or sent (plan section 5, T1).

The steps run on the canonical form (app/research/canon.py) and its views; the first that finds something refuses
the query with a reason code. The code is all that is ever recorded about a refused query. The Credit Repair match
index is asked first, for every query, and its answer is used at step 8, right after the leads and tickets: a name
there and one in the leads or tickets give the same code whatever else the query holds, at the same cost. The
archive phrase lookup (9) runs last.

 1. search_syntax: a word starting with "!", "?", ":" or "<" (SearXNG reads these as engine, language and timeout
    switches, and "!!" as a redirect);
 2. encoded_data: long hex, base32 or base64-like tokens, percent or backslash escapes;
 3. restricted_data: SSNs, card and bank details, dates of birth, also as spaced or spelled-out digits;
 4. credit_topic: a client's credit file and every credit topic (FCRA, the bureaus, disputes), on the look-alike
    skeleton as well: owner decision 5 blocks them for every agent; also anything shaped like a Credit Repair client's
    reference ("CR-0001"), whether or not that client exists;
 5. secret: API-key shapes, bearer tokens, secret URL parameters, JWTs, long random tokens (app/brain/text.clean()),
    and any of the Command Center's own secret values, also with its punctuation or spacing changed;
 6. contact_detail: e-mail addresses and phone numbers, also spelled out ("jane at example dot com"), encoded
    ("jane%40example.com") or joined with underscores or dashes, and local numbers ("555-0199", "call 555 0199");
 7. personal_record: a lead's or a ticket's name (two of its words, anywhere in the query and in either order), a
    lead's company (two words or more besides common ones like "LLC"), website domain or e-mail domain (its label also
    as separate words, "Smith Dental" for smithdental.com), phone number (also its local number), a ticket's e-mail
    domain (common mail providers aside); names with an apostrophe are read with and without it ("O'Neil", "ONeil").
    The lead or ticket id is kept with the refusal, so a poisoned lead record shows up, and is shown only to people
    with the sensitive-data permission (app/api/research.py);
 8. personal_record too: the Credit Repair match index (app/credit_repair/blind_index.py) answered "match", read with
    the same name rules as step 7. The same code, with no id. Every signed-in user can read the leads and tickets, so
    a refusal for a name found in neither still points to the Credit Repair records: app/research/intake.py limits
    how often one person can try that;
 9. matches_private_text: a 6-word run of the query appears as a phrase in the JARVIS archive or in memory
    (owner decision 7);
10. internal_host / file_path: IP addresses, localhost and private names, the stack's container names, registered
    servers and the hosts in this installation's settings, file paths;
11. client_name: a registered site's name, client or domain, unless the agent's policy allows class C;
    owner_name: the owner's own business names, unless the policy allows class O;
    software_version: version numbers and CVE ids, unless the policy allows class S;
    class_not_allowed: an agent allowed class S only (software advisories) asked something else;
12. (DeepSeek's stricter class G rule: no DeepSeek in this release);
13. blocked_domain: a domain the agent's policy blocks;
14. instruction-like text is flagged, never refused.

Step 9 compares 6-word runs of the query as typed and with its punctuation read as spaces ("gate-code").

Detection is pattern- and list-based: paraphrased private text and names found in no store are not caught. Every
query that passes stays visible in the Research Center.
"""

from __future__ import annotations

import ipaddress
import math
import os
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import permutations
from urllib.parse import unquote, urlsplit

from sqlalchemy import func, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import customer_settings
from ..brain import jarvis_kit as kit
from ..brain import text as brain_text
from ..config import Settings
from ..models import ArchiveMessage, Lead, MemoryItem, SecretValue, Server, Site, SupportTicket
from .canon import (
    NAME_PARTICLES,
    STOP_WORDS,
    Canonical,
    digit_runs,
    fold,
    phone_numbers,
    skeleton,
    spelled_out,
    tokens,
    unapostrophe,
)

# --- Steps 1-6: patterns ----------------------------------------------------------------------------------------

SYNTAX_PREFIXES = ("!", "?", ":", "<")
ENCODED = (
    # hex: 16 or more hex digits with letters and digits, or colon-separated bytes (a fingerprint, a MAC address)
    re.compile(r"(?<![0-9A-Za-z])(?:0x)?(?=[0-9A-Fa-f]*[0-9])(?=[0-9A-Fa-f]*[A-Fa-f])[0-9A-Fa-f]{16,}(?![0-9A-Za-z])"),
    re.compile(r"(?i)(?<![0-9a-z])(?:[0-9a-f]{2}[:\-]){7,}[0-9a-f]{2}(?![0-9a-z])"),
    # base32 (an authenticator secret): one letter case, at least two of the digits 2-7
    re.compile(r"(?<![A-Za-z0-9])(?=(?:[A-Z2-7]*[2-7]){2})[A-Z2-7]{16,}={0,6}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])(?=(?:[a-z2-7]*[2-7]){2})[a-z2-7]{16,}={0,6}(?![A-Za-z0-9])"),
    # percent, backslash and HTML escapes, named ones too ("&commat;")
    re.compile(r"(?:%[0-9A-Fa-f]{2}){2,}|\\x[0-9A-Fa-f]{2}|\\u[0-9A-Fa-f]{4}|&#x?[0-9A-Fa-f]+;"),
    re.compile(r"&[A-Za-z][A-Za-z0-9]{1,31};"),
)
BASE64_LIKE = re.compile(r"(?<![A-Za-z0-9+/_\-])[A-Za-z0-9+/_\-]{20,}={0,2}(?![A-Za-z0-9+/_\-])")
CREDIT_WORDS = (
    brain_text.RESTRICTED_PATTERNS["credit_file"],
    kit.PERSONAL_PATTERNS["credit_repair_terms"],
    re.compile(r"(?i)\b(?:credit (?:bureaus?|repair|reporting|disputes?|freezes?|monitoring|files?|reports?|scores?)"
               r"|croa|fdcpa|vantage ?scores?|equifax|experian|trans ?union|fico)\b"),
)
# The same words with the spaces taken out, for words spelled letter by letter.
CREDIT_COMPACT = re.compile(r"equifax|experian|transunion|fcra|fdcpa|creditrepair|creditreport|creditscore|creditbureau")
# A Credit Repair client's reference as the app writes it (CR-0001) and as it is typed ("cr0001", "CR 0012"), refused
# whether or not that client exists; "CR2032" (a battery) is no reference.
CR_REFERENCE = re.compile(r"(?<![a-z0-9])cr(?:[\-_.#:/]+[0-9]{4,}|\s*0[0-9]{3,})(?![0-9])")
EMAIL = kit.PERSONAL_PATTERNS["email_address"]
# A local phone number written with a dash or a dot ("555-0199"), not part of a longer dotted or dashed number.
LOCAL_PHONE = re.compile(r"(?<![0-9.\-])[0-9]{3}[.\-][0-9]{4}(?![0-9]|[.\-][0-9])")
# Words that make a seven-digit number a phone number, however it is written ("call 555 0199").
PHONE_WORD = re.compile(r"\b(?:call|phone|telephone|tel|cell|fax|sms|whatsapp|area code)\b")
# Underscores and dashes that join the words of an address written out ("jane_at_example_dot_com").
_WORD_JOINERS = re.compile(r"[_\-]+")
_QUOTES = re.compile(r"[\"'`]")  # around the at and dot of an address written out ("jane 'at' example 'dot' com")

# --- Steps 7-11: names, domains, hosts --------------------------------------------------------------------------

DOMAIN = re.compile(r"(?<![a-z0-9@.\-])((?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?![a-z0-9\-])")
# Mail providers and platforms many people share: their domains say nothing about one lead or ticket.
COMMON_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "pm.me", "gmx.com", "gmx.net",
    "mail.com", "zoho.com", "yandex.com", "comcast.net", "att.net", "verizon.net", "sbcglobal.net", "bellsouth.net",
    "cox.net", "charter.net", "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "tiktok.com",
    "youtube.com", "google.com", "sites.google.com", "business.site", "yelp.com", "wix.com", "wixsite.com",
    "squarespace.com", "wordpress.com", "godaddysites.com", "weebly.com", "shopify.com", "myshopify.com", "linktr.ee",
    "example.com", "example.org", "example.net",
})
INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp", ".intranet", ".localdomain",
                     ".home.arpa")
# The stack's own containers and tools, as they appear in addresses and Docker names.
_SERVICES = r"(?:postgres|db-init|api|worker|dashboard|n8n|searxng|research-fetch)"
SERVICE_ADDRESS = re.compile(
    r"(?i)(?<![\w.\-])" + _SERVICES + r":[0-9]{2,5}\b"  # searxng:8080
    r"|\bhttps?://" + _SERVICES + r"(?=[:/\s]|$)"  # http://n8n/..., not https://api.example.com
    r"|\b(?:infinite-command-center|cc-brain-test|cc-test-pg|cc\.brain-import|cc-gate)\b")
IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9]|\.[0-9])")
FILE_PATH = re.compile(
    r"(?:^|(?<=\s))(?:~|\.{1,2})?/[\w.\-]+/"  # /etc/passwd, ~/x/y, ./a/b
    r"|(?:^|(?<=\s))~[/\\]"
    r"|(?<![A-Za-z0-9])[A-Za-z]:[\\/]"  # C:\, D:/
    r"|\\\\[\w.\-]+|[\w.\-]+\\[\w.\-]+"  # \\server\share, scripts\start.ps1
    r"|%[A-Za-z_]+%")
# A dotted number (a price or a percentage aside) or a v-number: "WordPress 6.9", "PHP 8.2.1", "v3".
VERSION = re.compile(r"(?i)(?<![\w.$\u20ac\u00a3])(?:v[0-9]+|[0-9]+(?:\.[0-9]+){1,3})(?:[-.]?(?:alpha|beta|rc|p)[0-9]*)?"
                     r"(?![\w.%])")
CVE = re.compile(r"(?i)\bcve-[0-9]{4}-[0-9]{4,}\b")
# Steps 7 and 8: a lead's, a ticket's or a Credit Repair client's name or address.
PERSONAL_RECORD = "personal_record"
MIN_LABEL = 6  # a domain's first label stands for its business on its own from this length ("acmeplumbing")
MAX_NAME_WORDS = 6  # a person's name is matched by any two of its first six words
PHRASE_WORDS = 6  # owner decision 7: 6-word runs are compared with the archive and memory


@dataclass(frozen=True)
class Mark:
    """Something a query must not name: its phrases (word sequences) and domains, a person's name as pairs of its words
    (matched wherever they are in the query), its domains' labels written as one word (matched also when typed as
    words), phone numbers, and what a match is reported as."""

    code: str
    ref: str = ""
    phrases: tuple[tuple[str, ...], ...] = ()
    domains: tuple[str, ...] = ()
    people: tuple[frozenset[str], ...] = ()
    numbers: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()


@dataclass
class Stores:
    """What the screen compares a query with, read from the database and the settings (load_stores)."""

    secrets: tuple[str, ...] = ()
    leads: list[Mark] = field(default_factory=list)  # leads and tickets
    internal: list[Mark] = field(default_factory=list)
    clients: list[Mark] = field(default_factory=list)
    owner: list[Mark] = field(default_factory=list)


@dataclass
class Verdict:
    code: str = ""  # "" when the query may be sent
    ref: str = ""  # lead:<id> or ticket:<id> when one caused the refusal
    flags: list[str] = field(default_factory=list)
    classes: frozenset[str] = frozenset({"G"})

    @property
    def blocked(self) -> bool:
        return bool(self.code)


# --- Helpers ----------------------------------------------------------------------------------------------------


def _entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _luhn(digits: str) -> bool:
    return kit.luhn(digits)


def _words(skel: str) -> tuple[str, ...]:
    return tuple(t for t in tokens(skel) if len(t) > 1 and t not in STOP_WORDS)


def phrase_of(text: str) -> tuple[str, ...]:
    """The words of a stored name as a query would show them: look-alikes folded, initials and common words left out."""
    return _words(fold(text))


def phrases_of(text: str) -> tuple[tuple[str, ...], ...]:
    """phrase_of(), also with apostrophes inside words dropped: "O'Neil" reads "neil" and "oneil", as a query may
    write it either way."""
    folded = fold(text)
    return tuple(dict.fromkeys((_words(folded), _words(unapostrophe(folded)))))


_HOST = re.compile(r"^(?:[a-z][a-z0-9+.\-]*://)?(?:[^@/?#\s]*@)?([^/:?#\s]+)")
# Second-level labels under which a country code's domains are registered ("acme.co.uk", "acme.com.au").
_SECOND_LEVEL = frozenset({"co", "com", "net", "org", "gov", "ac", "edu"})


def domain_of(value: str) -> str:
    """The host of a URL or an e-mail address, without "www.", lower case; "" when there is none. Never raises: a
    stored address that is not a valid URL (from the public lead form, say) is read as far as it goes."""
    value = (value or "").strip().lower()
    if "@" in value and "://" not in value:
        host = value.rsplit("@", 1)[1]
    else:
        try:
            host = urlsplit(value if "://" in value else "https://" + value).hostname or ""
        except ValueError:
            m = _HOST.match(value)
            host = m.group(1) if m else ""
    host = fold(host).strip(".")
    return host[4:] if host.startswith("www.") else host


def registered_domain(host: str) -> str:
    """'shop.acme.co.uk' -> 'acme.co.uk', 'shop.acme.com' -> 'acme.com' (no public-suffix list: the common cases)."""
    parts = host.split(".")
    keep = 3 if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL else 2
    return ".".join(parts[-keep:])


def domain_mark(code: str, ref: str, value: str) -> Mark | None:
    """A business's domain and its first label ("acme-plumbing.com" also as "acme plumbing"); None for shared domains."""
    host = domain_of(value)
    if not host or "." not in host:
        return None
    domain = registered_domain(host)
    if host in COMMON_DOMAINS or domain in COMMON_DOMAINS:
        return None
    label = domain.split(".", 1)[0]
    words = tuple(w for w in label.split("-") if w)
    phrases = (words,) if len(words) >= 2 or len(label) >= MIN_LABEL else ()
    together = "".join(words)
    labels = (together,) if phrases and len(together) >= MIN_LABEL else ()  # "smithdental", also as "Smith Dental"
    return Mark(code, ref, phrases=phrases, domains=tuple(dict.fromkeys((host, domain))), labels=labels)


def name_mark(code: str, ref: str, *names: str, domains: Iterable[str] = (), person: str = "",
              phones: Iterable[str] = ()) -> Mark | None:
    """Names of two words or more (common words aside), the given domains and phone numbers; None when nothing is left
    to match.

    A person's name also matches as any two of its words, in either order and wherever they are in the query ("Sacha
    Coe", "Coe, Sacha", "Sacha M Coe" for "Sacha Marie Coe", and "Sacha Marie Coe" or "Coe reviews Sacha" for "Sacha
    Coe"; two name particles such as "van der" are no name). A name of three words or more also matches as its first
    word with any other ("Acme Plumbing", "Acme Heating" for "Acme Plumbing & Heating LLC"), not as two common words
    from its middle. Each also matches written as one word ("SachaCoe": _View.has_phrase), and with an apostrophe
    inside a word dropped ("ONeil" for "O'Neil")."""
    found_names = {words for n in (*names, person) if n for words in phrases_of(n)}
    for words in [words for n in names if n for words in phrases_of(n)]:
        if len(words) >= 3:
            found_names.update((words[0], w) for w in words[1:])
    people: set[frozenset[str]] = set()
    for own in (phrases_of(person) if person else ()):
        pairs = [pair for pair in permutations(own[:MAX_NAME_WORDS], 2) if not set(pair) <= NAME_PARTICLES]
        found_names.update(pairs)
        people.update(frozenset(pair) for pair in pairs if pair[0] != pair[1])
    phrases = tuple(sorted(p for p in found_names if len(p) >= 2))
    parts = [m for m in (domain_mark(code, ref, d) for d in domains if d) if m]
    phrases += tuple(p for m in parts for p in m.phrases)
    found = tuple(d for m in parts for d in m.domains)
    labels = tuple(dict.fromkeys(label for m in parts for label in m.labels))
    numbers = tuple(dict.fromkeys(n for p in phones if p for n in phone_numbers(re.sub(r"[^0-9]", "", p))))
    if not (phrases or found or numbers):
        return None
    return Mark(code, ref, phrases=phrases, domains=found, people=tuple(sorted(people, key=sorted)), numbers=numbers,
                labels=labels)


@dataclass(frozen=True)
class _View:
    """The parts of a query the list rules compare with: its words (also with apostrophes inside words dropped), its
    words spelled together, its domains."""

    words: tuple[str, ...]
    joined_words: frozenset[str]
    domains: frozenset[str]
    pairs: frozenset[tuple[str, ...]] = frozenset()  # neighbouring words, for the many two-word names
    wordset: frozenset[str] = frozenset()  # every word, for a person's two name words with others between them
    sequences: tuple[tuple[str, ...], ...] = ()  # the words as read with and without inner apostrophes
    run_together: frozenset[str] = frozenset()  # also two or three neighbouring words as one, for domain labels

    @classmethod
    def of(cls, c: Canonical) -> _View:
        sequences = tuple(dict.fromkeys(_words(form) for form in (c.skeleton, unapostrophe(c.skeleton))))
        joined: set[str] = set()
        run_together: set[str] = set()
        for form in dict.fromkeys((c.joined, unapostrophe(c.joined))):
            parts = tokens(form)
            joined.update(parts)
            # A domain's one-word label typed as words ("Smith Dental" for smithdental.com). Only labels are compared
            # with these: a person's name is read as the Credit Repair index reads it, word by word.
            run_together.update("".join(parts[i:i + n]) for n in (2, 3) for i in range(len(parts) - n + 1))
        hosts: set[str] = set()
        for view in (c.skeleton, spelled_out(c.skeleton)):
            for m in DOMAIN.finditer(view):
                host = m.group(1).strip(".")
                host = host[4:] if host.startswith("www.") else host
                parts = host.split(".")
                hosts.update(".".join(parts[i:]) for i in range(len(parts) - 1))  # a.b.example.com, b.example.com, ...
        return cls(words=sequences[0], joined_words=frozenset(joined), domains=frozenset(hosts),
                   pairs=frozenset(p for words in sequences for p in zip(words, words[1:])),
                   wordset=frozenset(w for words in sequences for w in words) | frozenset(joined), sequences=sequences,
                   run_together=frozenset(joined | run_together))

    def has_phrase(self, phrase: tuple[str, ...]) -> bool:
        n = len(phrase)
        if n == 2:
            if phrase in self.pairs:
                return True
        elif n and any(words[i:i + n] == phrase for words in self.sequences for i in range(len(words) - n + 1)):
            return True
        together = "".join(phrase)
        return len(together) >= MIN_LABEL and together in self.joined_words

    def names(self, mark: Mark) -> bool:
        return (any(d in self.domains for d in mark.domains) or any(self.has_phrase(p) for p in mark.phrases)
                or any(p <= self.wordset for p in mark.people) or any(x in self.run_together for x in mark.labels))


# --- The pure steps (no database) -------------------------------------------------------------------------------


def _syntax(c: Canonical) -> bool:
    return any(w.startswith(SYNTAX_PREFIXES) for w in c.words)


def _encoded(c: Canonical) -> bool:
    if any(rx.search(c.text) for rx in ENCODED):
        return True
    for m in BASE64_LIKE.finditer(c.text):
        tok = m.group(0).rstrip("=")
        mixed = re.search(r"[a-z]", tok) and re.search(r"[A-Z]", tok) and re.search(r"[0-9]", tok)
        if mixed and _entropy(tok) >= 3.5:
            return True
    return any(len(run) > 19 for run in _runs(c))


def _runs(c: Canonical) -> tuple[str, ...]:
    """The query's numbers seen whole, CVE ids aside ("CVE-2026-12345" is no 9-digit SSN)."""
    return digit_runs(skeleton(CVE.sub(" ", c.text))) if CVE.search(c.text) else c.digit_runs


def _digits_restricted(c: Canonical) -> bool:
    """A number shaped like an SSN (9 digits) or a card number (13-19 digits passing the Luhn check)."""
    return any(len(run) == 9 or (13 <= len(run) <= 19 and _luhn(run)) for run in _runs(c))


def _secret_value(c: Canonical, secrets: Iterable[str]) -> bool:
    """The Command Center's own secret values, exactly, without their punctuation, or a long word cut from one."""
    compact = _compact(c.text) + " " + _compact(c.joined)
    long_words = [w for w in re.findall(r"\S{12,}", c.text)]
    for value in secrets:
        if len(value) < 8:
            continue
        if value in c.text or (len(_compact(value)) >= 8 and _compact(value) in compact):
            return True
        if any(w in value for w in long_words):
            return True
    return False


def _contact(c: Canonical) -> bool:
    loose = _WORD_JOINERS.sub(" ", c.skeleton)  # "jane_at_example_dot_com"
    decoded = unquote(c.skeleton)  # "jane%40example.com"
    unquoted = _QUOTES.sub(" ", c.skeleton)  # "jane 'at' example 'dot' com"
    for view in (c.text, c.skeleton, spelled_out(c.skeleton), spelled_out(c.joined), spelled_out(loose), decoded,
                 spelled_out(decoded), spelled_out(unquoted)):
        if EMAIL.search(view):
            return True
    runs = _runs(c)
    if any(10 <= len(run) <= 15 for run in runs) or LOCAL_PHONE.search(c.text):
        return True
    return bool(PHONE_WORD.search(c.skeleton)) and any(7 <= len(run) <= 9 for run in runs)


def _internal(c: Canonical, view: _View, marks: list[Mark]) -> str:
    if FILE_PATH.search(c.text):
        return "file_path"
    if SERVICE_ADDRESS.search(c.text) or "localhost" in view.words:
        return "internal_host"
    for m in (m for view in (c.text, spelled_out(c.skeleton)) for m in IPV4.finditer(view)):  # also "10[.]0[.]0[.]5"
        try:
            ipaddress.IPv4Address(m.group(0))
        except ValueError:
            continue
        return "internal_host"
    for word in c.words:
        candidate = word.strip("[](),;")
        if candidate.count(":") >= 2:
            try:
                ipaddress.IPv6Address(candidate.split("%", 1)[0])
            except ValueError:
                continue
            return "internal_host"
    if any(d == s.lstrip(".") or d.endswith(s) for d in view.domains for s in INTERNAL_SUFFIXES):
        return "internal_host"
    return "internal_host" if any(view.names(m) for m in marks) else ""


def screen_query(c: Canonical, *, classes: frozenset[str], blocked_domains: Iterable[str] = (),
                 stores: Stores | None = None, credit_repair_match: bool = False) -> Verdict:
    """Every step but the archive phrase check (9). credit_repair_match is the Credit Repair index's answer for this
    query (step 8), asked by screen() before any step runs."""
    stores = stores or Stores()
    flags = ["instruction_like"] if brain_text.instruction_flags(c.text) else []
    if _syntax(c):
        return Verdict("search_syntax")
    if _encoded(c):
        return Verdict("encoded_data")
    cleaned = brain_text.clean(c.text, "chat")
    if set(cleaned.restricted) - {"credit_file"} or _digits_restricted(c):
        return Verdict("restricted_data")
    if "credit_file" in cleaned.restricted or any(rx.search(v) for rx in CREDIT_WORDS for v in (c.skeleton, c.joined)) \
            or CREDIT_COMPACT.search(_compact(c.joined)) or any(CR_REFERENCE.search(v) for v in (c.skeleton, c.joined)):
        return Verdict("credit_topic")
    if cleaned.secret_types or _secret_value(c, stores.secrets):
        return Verdict("secret")
    if cleaned.masked or _contact(c):
        return Verdict("contact_detail")
    view = _View.of(c)
    numbers = {n for run in _runs(c) for n in phone_numbers(run)}
    for mark in stores.leads:
        if view.names(mark) or not numbers.isdisjoint(mark.numbers):
            return Verdict(mark.code, mark.ref)
    if credit_repair_match:  # the same code, at the same step, as a lead's or a ticket's name
        return Verdict(PERSONAL_RECORD)
    internal = _internal(c, view, stores.internal)
    if internal:
        return Verdict(internal)
    used = {"G"}
    if VERSION.search(c.text) or CVE.search(c.text):
        used.add("S")
    if any(view.names(m) for m in stores.owner):
        used.add("O")
    if any(view.names(m) for m in stores.clients):
        used.add("C")
    if "C" in used and "C" not in classes:
        return Verdict("client_name")
    if "O" in used and "O" not in classes:
        return Verdict("owner_name")
    if "S" in used and "S" not in classes:
        return Verdict("software_version")
    if "G" not in classes and not (used - {"G"}):
        return Verdict("class_not_allowed")
    blocked = {domain_of(d) for d in blocked_domains if d}
    if any(d in view.domains for d in blocked if d):
        return Verdict("blocked_domain")
    return Verdict(flags=flags, classes=frozenset(used))


# --- The database steps -----------------------------------------------------------------------------------------

SECRET_ENV = re.compile(r"(?i)(?:secret|token|password|passwd|api_?key|_key)$")


def deployment_secrets(settings: Settings) -> list[str]:
    """Every secret value this process holds: SecretStr settings, database passwords, secret-named variables."""
    values: list[str] = []
    for name in type(settings).model_fields:
        value = getattr(settings, name)
        if hasattr(value, "get_secret_value"):
            values.append(value.get_secret_value())
    for url in (settings.database_url, settings.credit_repair_database_url):
        try:
            password = urlsplit(url).password if url else None
        except ValueError:
            password = None
        if password:
            values.append(password)
    values += [v for k, v in os.environ.items() if SECRET_ENV.search(k)]
    return [v for v in values if v and len(v) >= 8]


def _settings_hosts(settings: Settings) -> list[str]:
    hosts = set(settings.allowed_private_hosts) | set(settings.ssh_private_hosts)
    for url in (settings.database_url, settings.credit_repair_database_url, settings.n8n_base_url,
                settings.n8n_editor_url, settings.workflow_webhook_url):
        try:
            host = urlsplit(url).hostname if url else None
        except ValueError:
            host = None
        if host:
            hosts.add(host)
    return sorted(hosts)


def _host_mark(code: str, host: str) -> Mark | None:
    """A host name with a domain. IP addresses are refused anyway, and one-word names ("postgres", the stack's own
    services) only in address form (SERVICE_ADDRESS): the bare word is an ordinary search term."""
    host = fold(host).strip(".")
    if "." not in host:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        return Mark(code, domains=(host,))


async def load_stores(session: AsyncSession, settings: Settings, owner_terms: Iterable[str] = ()) -> Stores:
    """Reads what the screen compares with. Only names, domains and hosts; values never leave this process."""
    from ..secrets import get_secret

    stores = Stores()
    secrets = deployment_secrets(settings)
    for name in (await session.scalars(select(SecretValue.name))).all():
        value = await get_secret(session, settings, name)
        if value and len(value) >= 8:
            secrets.append(value)
    stores.secrets = tuple(secrets)

    for row in (await session.execute(select(Lead.id, Lead.name, Lead.company, Lead.website, Lead.email, Lead.phone)
                                      .order_by(Lead.id))).all():
        mark = name_mark(PERSONAL_RECORD, f"lead:{row.id}", row.company, person=row.name,
                         domains=(row.website, row.email), phones=(row.phone,))
        if mark:
            stores.leads.append(mark)
    for row in (await session.execute(select(SupportTicket.id, SupportTicket.customer_name, SupportTicket.customer_email)
                                      .order_by(SupportTicket.id))).all():
        mark = name_mark(PERSONAL_RECORD, f"ticket:{row.id}", person=row.customer_name,
                         domains=(row.customer_email,))
        if mark:
            stores.leads.append(mark)

    for row in (await session.execute(select(Site.name, Site.client, Site.url))).all():
        mark = name_mark("client_name", "", row.name, row.client, domains=(row.url,))
        if mark:
            stores.clients.append(mark)

    hosts = [*_settings_hosts(settings), *(await session.scalars(select(Server.host))).all()]
    stores.internal = [m for m in (_host_mark("internal_host", h) for h in hosts) if m]

    business = (await customer_settings.load(session, settings)).get("business_name")
    for term in [business if isinstance(business, str) else "", *owner_terms]:
        mark = name_mark("owner_name", "", term, domains=(term,) if "." in term else ())
        if mark is None and len(phrase_of(term)) == 1 and len(phrase_of(term)[0]) >= MIN_LABEL:
            mark = Mark("owner_name", phrases=(phrase_of(term),))
        if mark:
            stores.owner.append(mark)
    return stores


def phrase_windows(c: Canonical) -> list[str]:
    """The query's 6-word runs: of its words as typed (they match hyphenated words as the archive holds them), and of
    its word tokens, punctuation read as spaces ("storage-unit-gate-code-changes-every-Friday")."""
    windows: list[str] = []
    for words in (c.words, re.findall(r"[^\W_]+", c.text)):
        windows += [" ".join(words[i:i + PHRASE_WORDS]) for i in range(len(words) - PHRASE_WORDS + 1)]
    return list(dict.fromkeys(windows))


async def matches_private_text(session: AsyncSession, c: Canonical) -> bool:
    """A 6-word run of the query, as a phrase, in an archived JARVIS message or a memory item (any status).

    Runs that are mostly common words (fewer than three words PostgreSQL keeps) are not compared: they would match
    ordinary sentences anywhere.
    """
    windows = phrase_windows(c)
    if not windows:
        return False
    kept = (await session.scalars(  # three lexemes and the two links between them: 5 nodes
        text("SELECT w FROM unnest(CAST(:windows AS text[])) AS w "
             "WHERE numnode(phraseto_tsquery('english', w)) >= 5"), {"windows": windows})).all()
    if not kept:
        return False
    queries = [func.phraseto_tsquery("english", w) for w in kept]
    for column in (ArchiveMessage.search, MemoryItem.search):
        found = await session.scalar(select(literal(1)).select_from(column.class_)
                                     .where(or_(*(column.op("@@")(q) for q in queries))).limit(1))
        if found:
            return True
    return False


async def screen(session: AsyncSession, settings: Settings, c: Canonical, *, classes: frozenset[str],
                 blocked_domains: Iterable[str] = (), owner_terms: Iterable[str] = ()) -> Verdict:
    """Every step. Raises on any error: the caller refuses the query then (fail closed)."""
    from ..credit_repair import blind_index

    # Asked for every query, before any step: a Credit Repair name and a lead's then cost the same and give the same
    # code whatever else the query holds.
    credit_repair_match = await blind_index.matches(settings, blind_index.query_candidates(c.text))
    stores = await load_stores(session, settings, owner_terms)
    verdict = screen_query(c, classes=classes, blocked_domains=blocked_domains, stores=stores,
                           credit_repair_match=credit_repair_match)
    if verdict.blocked:
        return verdict
    if await matches_private_text(session, c):
        return Verdict("matches_private_text")
    return verdict
