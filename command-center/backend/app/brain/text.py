"""Cleaning text before it is stored as memory or archive, and the rule-based labels used to answer questions.

The order matters:
1. NUL characters and broken Unicode are removed (PostgreSQL rejects them).
2. Reasoning blocks (<think>…</think>, <reasoning>, <thought> and the forms Hermes uses) are removed.
3. Secrets are redacted: the detectors of the owner's kit, then the stricter rules here for conversations and notes
   (secrets written inside sentences, passwords on command lines, positional literals in scripts, secret URL
   parameters and webhook addresses, long random tokens, whole configuration dumps). JSON tool output is cleaned value
   by value, each value judged with its key ({"ssn": "..."}, {"pin": 4821}, {"phone": 5551234567}).
4. Restricted data (SSNs, card and bank details, dates of birth, a client's credit file) is detected: a record
   holding it keeps no text at all.
5. E-mail addresses, phone numbers and WhatsApp ids are masked as [email] and [phone].

Detection is pattern-based, so it is a safety net, not a guarantee: anything that reaches a person or an agent is
also gated by review and by sensitivity. Nothing here calls an AI model or the network.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from . import jarvis_kit as kit

# --- Restricted data: never stored -----------------------------------------------------------------------------
# A client's credit file and identity numbers. The words "credit repair" or "credit score" alone (the service and its
# marketing) are not restricted; a score with a number, bureau names, letters, derogatory items are.
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
RESTRICTED_PATTERNS: dict[str, re.Pattern[str]] = {
    "us_ssn": re.compile(
        r"(?i)(?<![0-9])(?!000|666|9[0-9]{2})[0-9]{3}-(?!00)[0-9]{2}-(?!0000)[0-9]{4}(?![0-9])"
        r"|(?:\b(?:ssn|social security(?: number| no\.?| #)?|social)\b|(?<!\w)ss ?#)[^0-9\n]{0,30}"
        r"[0-9]{3}[ .\-]?[0-9]{2}[ .\-]?[0-9]{4}(?![0-9])"
        r"|\blast (?:four|4)(?: digits)? of (?:the |my |his |her |their )?(?:ssn|social)\b"),
    "payment_card": kit.PERSONAL_PATTERNS["payment_card"],
    "date_of_birth": re.compile(
        r"(?i)\b(?:date of birth|d\.o\.b\.?|dob|birth ?date)\b"
        r"|(?:\b|_)(?:date[_\-]?of[_\-]?birth|birth[_\-]date)[\"']?\s*[:=,]?\s*[\"']?[0-9]"
        r"|\b(?:born(?: on)?|birthday)\b\W{0,12}(?:[0-9]{1,2}[/.\-][0-9]{1,2}[/.\-][0-9]{2,4}"
        r"|" + _MONTH + r" [0-9]{1,2}(?:st|nd|rd|th)?,? [0-9]{4}|[0-9]{1,2} " + _MONTH + r",? [0-9]{4})"),
    "bank_details": re.compile(
        r"(?i)\b(?:routing number|account number|iban|swift code|bank account)\b"
        r"|\b(?:acct|account|a/c)\s*(?:#|no\.?|number|num)?\s*:?\s*[0-9][0-9 \-]{5,20}[0-9]"
        r"|\b(?:aba|routing|rtn)\s*(?:#|no\.?|number)?\s*:?\s*[0-9]{9}\b"
        r"|(?:\b|_)(?:bank[_\-]?)?(?:acct|account)[_\-]?(?:number|no|num|nr)?[\"']?\s*[:=,]\s*[\"']?[0-9][0-9 \-]{4,20}[0-9]"
        r"|(?:\b|_)(?:aba|routing|rtn)[_\-]?(?:number|no|num|nr)?[\"']?\s*[:=,]\s*[\"']?[0-9]{9}\b"),
    "credit_file": re.compile(
        r"(?i)\b(?:experian|equifax|trans ?union|credit reports?|credit files?|credit history|dispute letters?"
        r"|disputes? (?:with|to) (?:the )?(?:bureaus?|creditors?)|609 letters?|goodwill letters?"
        r"|(?:debt )?validation (?:letters?|requests?)|pay[- ]for[- ]delete|fcra|fdcpa|charge[- ]?offs?"
        r"|collections? (?:accounts?|agency|agencies)|tradelines?|hard inquir(?:y|ies)|derogator(?:y|ies)"
        r"|repossessions?|bankruptc(?:y|ies)|late payments?|credit utili[sz]ation"
        r"|(?:credit |fico |vantage ?)?score(?: (?:is|of|at|was|went (?:up|down) to)| ?:)\s*[3-8][0-9]{2}\b"
        r"|\b(?:credit|fico|vantage ?|beacon)[ _-]?scores?\b[^0-9\n]{0,15}[3-8][0-9]{2}\b"
        r"|\bfico ?[0-9]{1,2}\b[^0-9\n]{0,10}[3-8][0-9]{2}\b|(?-i:\b(?:TU|EQ|EX)\b)[ :]{0,3}[3-8][0-9]{2}\b"
        r"|\b[3-8][0-9]{2}\s+(?:credit|fico|vantage|beacon)\s+scores?\b"
        r"|\b(?:tu|eq|ex)(?: and | ?/ ?| & )(?:tu|eq|ex)\b)"),
}
RESTRICTED_TYPES = tuple(RESTRICTED_PATTERNS)

# --- Customer contact data: masked ----------------------------------------------------------------------------
MASKS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("phone_number", re.compile(r"(?i)\b[0-9]{8,15}@(?:s\.whatsapp\.net|c\.us|g\.us)\b"), "[phone]"),
    ("email_address", kit.PERSONAL_PATTERNS["email_address"], "[email]"),
    ("phone_number", re.compile(r"(?<![\w+])\+[0-9][0-9 \-().]{8,18}[0-9](?![0-9])"), "[phone]"),
    ("phone_number", kit.PERSONAL_PATTERNS["phone_number"], "[phone]"),
)
PHONE_CONTEXT = re.compile(
    r"(?i)\b(?:phone|tel|telephone|call|text|sms|whatsapp|wa|mobile|cell|contact|number|ph)\b[^0-9\n]{0,15}"
    r"(?P<v>[0-9][0-9 \-().]{8,16}[0-9])(?![0-9])")
STAND_INS = {"email_address": "[email]", "phone_number": "[phone]"}

# --- Secrets beyond the kit's file-oriented rules -----------------------------------------------------------------
SECRET_WORD = re.compile(
    r"(?i)(?<![/.@\w-])(?:passwords?|passwd|pass|pw|pwd|passcode|pass ?phrase|creds|credentials?|login|log-?in details"
    r"|api[ _-]?keys?|access[ _-]?keys?|secret(?: key)?s?|tokens?|app[ -]?passwords?|private keys?)\b")
SEPARATOR = re.compile(r"(?i)^\s*(?:(?:is|was|are|were|should be|will be|=|:|->|=>|-)\s*)+")
FILLER = {"for", "the", "a", "an", "my", "our", "your", "his", "her", "their", "of", "to", "on", "at", "in", "with",
          "account", "is", "was", "are", "new", "old", "same", "and", "also", "still", "now", "here", "it"}
AS_PASSWORD = re.compile(
    r"(?i)\b(?:use|set|try|used|chose|picked)\s+[\"'`]?(?P<v>[^\s\"'`]{6,128})[\"'`]?\s+as\s+"
    r"(?:the|my|your|a|our|his|her)?\s*(?:new\s+)?(?:password|passcode|pin|pass ?phrase)\b")
GMAIL_SPACED = re.compile(
    r"(?i)\bapp[ -]?passwords?\b[^\n]{0,30}?(?P<v>(?<![A-Za-z0-9])[a-z]{4} [a-z]{4} [a-z]{4} [a-z]{4}(?![A-Za-z0-9]))")
PIN = re.compile(
    r"(?i)\b(?:pin(?: code)?|passcode|security code|verification code|one[- ]time code|otp|2fa code)\b\W{0,12}"
    r"(?:is\s+)?(?P<v>[0-9]{4,8})\b")
URL_SECRET = re.compile(
    r"(?i)[?&#](?:code|token|access_token|id_token|refresh_token|auth|authorization|key|apikey|api_key|sig"
    r"|signature|secret|password|pass|pwd|session|sessionid|sid|otp|magic|login_token|reset_token|ticket)"
    r"=(?P<v>[^\s&#\"'<>]{6,})")
CODE_SECRET = re.compile(
    r"ConvertTo-SecureString\s+(?:-String\s+)?[\"'](?P<a>[^\"'\n]{4,})[\"']"
    r"|\.login\(\s*[^,()\n]{1,100},\s*[\"'](?P<b>[^\"'\n]{4,})[\"']"
    r"|\bauth\s*=\s*\(\s*[\"'][^\"'\n]*[\"']\s*,\s*[\"'](?P<c>[^\"'\n]{4,})[\"']"
    r"|PSCredential\([^,\n]{1,100},\s*\(?\s*ConvertTo-SecureString\s+[\"'](?P<d>[^\"'\n]{4,})[\"']")
# Passwords on command lines and in environment prefixes (JARVIS ran WordPress and server work in its terminal).
_ARG = r"(?:\"[^\"\n]+\"|'[^'\n]+'|[^\s\"'|;&]+)"
CLI_SECRET = re.compile(
    r"(?i:\b(?:mysql|mysqldump|mysqladmin|mariadb|mariadb-dump)\b)[^\n|;&]*?\s-p(?P<a>[^\s\"']{2,}|\"[^\"\n]+\"|'[^'\n]+')"
    r"|(?<![\w-])--(?:[\w-]*[_-])?(?i:password|passwd|pass|pwd|secret|token|api-?key)(?:=|[ \t]+)(?P<b>" + _ARG + ")"
    r"|(?i:\bsshpass)\s+-p\s*(?P<c>" + _ARG + ")"
    r"|(?i:\b(?:curl|wget|https?|httpie)\b)[^\n]*?\s(?:-u|--user)[ \t]+[\"']?[^\s:\"']+:(?P<d>[^\s\"']+)"
    r"|(?<![\w$%{])[A-Z][A-Z0-9_]*(?:PASSWORD|PASSWD|PASS|PWD|SECRET|TOKEN)[A-Z0-9_]*=(?P<e>" + _ARG + ")"
    r"|(?i:\becho)\s+(?P<f>" + _ARG + r")\s*\|\s*sudo\s+(?:-\w+\s+)*-S\b"
    r"|(?i:\bhtpasswd)\s+-[a-zA-Z]*b[a-zA-Z]*\s+\S+\s+\S+\s+(?P<g>" + _ARG + ")"
    r"|(?i:\bwp\s+config\s+set)\s+[\"']?\w*(?i:password|key|salt|secret)\w*[\"']?\s+(?P<h>" + _ARG + ")"
    # PowerShell (-Password, -Pwd), "-u <user> -p <password>" (psexec, mongo, docker login), redis-cli -a, smbclient -U
    # user%password, lftp -u user,password, net use /user:<u> <password>, Set-Item env:<NAME> <value>.
    r"|(?<![\w-])-(?i:password|passwd|pwd)(?:[ \t]+|:)(?P<i>" + _ARG + ")"
    r"|\s-u[ \t]+[^\s-][^\s]*[ \t]+-p[ \t]+(?P<j>" + _ARG + ")"
    r"|(?i:\bredis-cli\b)[^\n]*?\s-a[ \t]+(?P<k>" + _ARG + ")"
    r"|\s-U[ \t]+[^\s%]+%(?P<l>[^\s\"']+)"
    r"|(?i:\blftp\b)[^\n]*?\s-u[ \t]+[^\s,]+,(?P<m>[^\s\"']+)"
    r"|(?i:\bnet[ \t]+use\b)[^\n]*?/user:\S+[ \t]+(?P<n>" + _ARG + ")"
    r"|(?i:\bset-item[ \t]+env:)\w*(?i:pass|pwd|token|secret|key)\w*[ \t]+(?:-value[ \t]+)?(?P<o>" + _ARG + ")")
CLI_GROUPS = "abcdefghijklmno"
WEBHOOK_URL = re.compile(
    r"(?i)https?://(?:hook\.[a-z0-9]+\.make\.com|hooks\.zapier\.com/hooks/catch|[^\s/\"'<>]+/webhook(?:-test)?)"
    r"/(?P<v>[A-Za-z0-9][^\s\"'<>?#]{7,})")
TOKEN = re.compile(r"(?<![A-Za-z0-9+/_\-])[A-Za-z0-9+/_\-]{32,}={0,2}(?![A-Za-z0-9+/_\-])")
ENV_LINE = re.compile(r"(?m)^\s*(?:export\s+|set\s+)?[A-Z][A-Z0-9_]{2,}\s*=\s*\S")
_REASONING = r"think|thinking|reasoning|thought|reasoning_scratchpad|思考|反思|推理|推敲"
THINK_BLOCK = re.compile(r"<(%s)\b[^>]*>.*?(?:</\1\s*>|\Z)|◁think▷.*?(?:◁/think▷|\Z)" % _REASONING,
                         re.IGNORECASE | re.DOTALL)
# A closing tag with no opening one: the model's reasoning started the message (the opening tag was in the prompt).
THINK_ORPHAN = re.compile(r"\A.*?(?:</(?:%s)\s*>|◁/think▷)" % _REASONING, re.IGNORECASE | re.DOTALL)
SPACE = re.compile(r"\s+")
REDACTED = "[REDACTED:%s]"


@dataclass
class Cleaned:
    text: str
    secret_types: dict[str, int] = field(default_factory=dict)
    restricted: list[str] = field(default_factory=list)
    masked: dict[str, int] = field(default_factory=dict)
    think_removed: int = 0

    @property
    def withheld(self) -> bool:
        return bool(self.restricted)

    @property
    def sensitivity(self) -> str:
        if self.restricted:
            return "restricted"
        return "customer" if self.masked else "general"

    def flags(self) -> dict[str, object]:
        out: dict[str, object] = {}
        if self.secret_types:
            out["secrets_redacted"] = dict(self.secret_types)
        if self.restricted:
            out["restricted"] = list(self.restricted)
        if self.masked:
            out["masked"] = dict(self.masked)
        if self.think_removed:
            out["reasoning_removed"] = self.think_removed
        return out

    def merge(self, other: Cleaned) -> None:
        """Adds another cleaned piece's findings (for a record made of several texts)."""
        for k, v in other.secret_types.items():
            self.secret_types[k] = self.secret_types.get(k, 0) + v
        for k, v in other.masked.items():
            self.masked[k] = self.masked.get(k, 0) + v
        self.restricted = sorted(set(self.restricted) | set(other.restricted))
        self.think_removed += other.think_removed


SENSITIVITY_ORDER = {"general": 0, "customer": 1, "restricted": 2}


def most_sensitive(*levels: str) -> str:
    return max(levels or ("general",), key=lambda s: SENSITIVITY_ORDER.get(s, 2))


def sanitize(text: str) -> str:
    """No NUL characters and no lone surrogates: PostgreSQL TEXT and JSONB reject both."""
    text = text.replace("\x00", "")
    return text.encode("utf-8", "replace").decode("utf-8") if any("\ud800" <= c <= "\udfff" for c in text) else text


def strip_think(text: str) -> tuple[str, int]:
    """Removes reasoning blocks (<think>, <reasoning>, <thought> and the like, an unclosed one to the end, and
    everything before a closing tag that has no opening one): private reasoning is never kept."""
    text, n = THINK_BLOCK.subn("", text)
    text, orphan = THINK_ORPHAN.subn("", text, count=1)
    return text, n + orphan


def _entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def _replace_spans(text: str, spans: list[tuple[int, int, str]]) -> tuple[str, dict[str, int]]:
    found: dict[str, int] = {}
    out, last = [], 0
    for start, end, name in sorted(spans):
        if start < last:
            continue
        out += [text[last:start], REDACTED % name]
        last = end
        found[name] = found.get(name, 0) + 1
    return ("".join(out) + text[last:], found) if found else (text, {})


def _already(value: str) -> bool:
    return (value.startswith("[REDACTED") or value in ("[email]", "[phone]")
            or bool(kit.PLACEHOLDER_VALUE.fullmatch(value)))


CONTACT = "\x00"


def _contacts_as_one_word(window: str) -> str:
    """The window with each phone number and e-mail address turned into one word of the same length, so it is skipped
    as a whole: cleaning before and after masking then sees the same words."""
    for _name, rx, _stand_in in MASKS:
        window = rx.sub(lambda c: CONTACT * len(c.group(0)), window)
    return PHONE_CONTEXT.sub(lambda c: c.group(0)[: c.start("v") - c.start()] + CONTACT * len(c.group("v"))
                             if 10 <= len(re.sub(r"[^0-9]", "", c.group("v"))) <= 15 else c.group(0), window)


def _secrets_after_word(text: str, m: re.Match[str]) -> list[tuple[int, int]]:
    """The secret-looking word that follows a password word: after 'is', ':' or '=' if present, else the first word
    with a digit or a symbol among the next few words ("root password Xy7!kLp9"). After a login or credentials word,
    every such word among them: the user name and the password both follow it ("login: 4045550199 / Sup3rPass!")."""
    pair = m.group(0).lower().startswith(("login", "log-in", "creds", "credential"))
    line_end = text.find("\n", m.end())
    window_end = min(m.end() + 80, len(text) if line_end < 0 else line_end)
    window = _contacts_as_one_word(text[m.end():window_end])
    sep = SEPARATOR.match(window)
    pos = sep.end() if sep else 0
    seen = 0
    found: list[tuple[int, int]] = []
    for w in re.finditer(r"[\"'`]?(?P<w>[^\s\"'`]+)[\"'`]?", window[pos:]):
        word = w.group("w").rstrip(".,;)…")  # "…" is where one_line() cut a summary, not part of a word
        if word.lower() in FILLER or word in ("/", "|", "-", ":"):
            continue
        if len(word) > 1 and word.endswith(":"):  # a label before the value ("creds for wp-admin: <value>")
            continue
        if word.strip(CONTACT) == "":  # a phone number or an address (a login), masked later: not counted
            continue
        seen += 1
        if seen > 3 or word.startswith("[REDACTED"):  # a marker: the value was removed already
            break
        if _already(word) or kit.PERSONAL_PATTERNS["email_address"].fullmatch(word):  # a contact, masked later
            continue
        # Only a word with a digit or a symbol: "cPanel", "set", "vault" and other prose stay.
        if not kit.looks_secret(word) or not re.search(r"[0-9]|[^A-Za-z0-9]", word):
            continue
        at = m.end() + pos + w.start("w")
        found.append((at, at + len(word)))
        if not pair:
            break
    return found


def _chat_secrets(text: str) -> tuple[str, dict[str, int]]:
    spans: list[tuple[int, int, str]] = []
    # The kit's assignment rule without its "start of a line" condition (DB_PASSWORD=… in a sentence).
    for _m, (s, e) in kit.matches(kit.SECRET_PATTERNS["credential_assignment"], "credential_assignment", text, "chat"):
        value = text[s:e]
        if not _already(value) and kit.looks_secret(value):
            spans.append((s, e, "credential_assignment"))
    for m in SECRET_WORD.finditer(text):
        spans += [(*span, "credential_assignment") for span in _secrets_after_word(text, m)]
    for rx, name in ((AS_PASSWORD, "credential_assignment"), (GMAIL_SPACED, "gmail_app_password"), (PIN, "pin"),
                     (URL_SECRET, "url_secret_parameter")):
        for m in rx.finditer(text):
            if not _already(m.group("v")):
                spans.append((m.start("v"), m.end("v"), name))
    for m in CODE_SECRET.finditer(text):
        group = next(g for g in "abcd" if m.group(g) is not None)
        if not _already(m.group(group)):  # cleaning cleaned text again finds nothing new (verify, rescan)
            spans.append((m.start(group), m.end(group), "credential_assignment"))
    for m in CLI_SECRET.finditer(text):
        group = next(g for g in CLI_GROUPS if m.group(g) is not None)
        value = m.group(group).strip("\"'")
        line_start = text.rfind("\n", 0, m.start()) + 1
        if group == "j" and re.search(r"(?i)\b(?:mysql|mysqldump|mariadb)\b", text[line_start:m.start()]):
            continue  # mysql's "-p <word>" is the database name: its password is attached (-pSecret)
        if len(value) >= 4 and not _already(value) and not value.startswith(("$", "%", "<", "{{")):
            spans.append((m.start(group), m.end(group), "credential_assignment"))
    for m in WEBHOOK_URL.finditer(text):
        if not _already(m.group("v")):
            spans.append((m.start("v"), m.end("v"), "webhook_url"))
    for m in TOKEN.finditer(text):
        tok = m.group(0)
        mixed = re.search(r"[a-z]", tok) and re.search(r"[A-Z]", tok) and re.search(r"[0-9]", tok)
        if mixed and _entropy(tok) >= 3.5:
            spans.append((m.start(), m.end(), "high_entropy_token"))
    return _replace_spans(text, spans)


def _clean_string(text: str, kind: str, out: Cleaned) -> str:
    """One string through steps 2-5; findings are added to out. Returns "" when restricted data is found."""
    text, think = strip_think(text)
    out.think_removed += think
    base_kind = "text" if kind == "chat" else kind
    secrets = kit.count(kit.SECRET_PATTERNS, text, base_kind)
    if secrets:
        text, _ = kit.redact(text, base_kind)
    if kind in ("chat", "code", "shell"):
        if len(ENV_LINE.findall(text)) >= 3:
            secrets["config_dump"] = secrets.get("config_dump", 0) + 1
            text = "[withheld: a configuration dump (setting names and values) was here]"
        else:
            text, more = _chat_secrets(text)
            for k, v in more.items():
                secrets[k] = secrets.get(k, 0) + v
    for k, v in secrets.items():
        out.secret_types[k] = out.secret_types.get(k, 0) + v
    restricted = sorted({name for name, rx in RESTRICTED_PATTERNS.items()
                         if any(True for _ in kit.matches(rx, name, text, "text"))} | set(_table_restricted(text)))
    if restricted:
        out.restricted = sorted(set(out.restricted) | set(restricted))
        return ""
    for name, stand_in in STAND_INS.items():  # already masked text stays customer data
        n = text.count(stand_in)
        if n:
            out.masked[name] = out.masked.get(name, 0) + n
    for name, rx, stand_in in MASKS:
        text, n = rx.subn(stand_in, text)
        if n:
            out.masked[name] = out.masked.get(name, 0) + n

    def ctx_phone(m: re.Match[str]) -> str:
        digits = re.sub(r"[^0-9]", "", m.group("v"))
        if not 10 <= len(digits) <= 15:
            return m.group(0)
        out.masked["phone_number"] = out.masked.get("phone_number", 0) + 1
        return m.group(0)[: m.start("v") - m.start()] + "[phone]"

    return PHONE_CONTEXT.sub(ctx_phone, text)


# A JSON key that says its value is a secret ({"password": "..."}): cleaned value by value, the value alone would not
# show it. The names follow the kit's credential_assignment rule.
SECRET_KEY = re.compile(
    r"(?i:^(?:[A-Za-z0-9]{1,32}[_\-]){0,4}(?:password|passwd|pwd|pass|passphrase|passcode|secret|api[_\-]?key|apikey"
    r"|token|access[_\-]?key|client[_\-]?secret|(?:secret|private|auth|encryption|signing|master|server|app|license)"
    r"[_\-]?key|credentials?|cookie|authorization|pin)$)"
    r"|^_?[a-z][a-z0-9]*(?:Password|Passwd|Pass|Secret|Token|ApiKey|AccessKey|SecretKey|PrivateKey)$"
    r"|^_?(?:api|access|secret|private|client|auth)Key$")


def _digits(value: str) -> str:
    return re.sub(r"[^0-9]", "", value)


# A JSON key that names restricted data, with what its value must look like to be that data: {"ssn": "123456789"} and
# {"credit_score": 612} hold no context word in the value itself. The value checks keep counts ({"us_ssn": 2} in a
# report, {"pin": 1} in flags) from counting.
_DATE = re.compile(r"[0-9]{1,4}[/.\- ][0-9]{1,2}[/.\- ][0-9]{1,4}|(?i:\b" + _MONTH + r")|^[0-9]{6,8}$")
RESTRICTED_KEYS: tuple[tuple[str, re.Pattern[str], object], ...] = (
    ("us_ssn", re.compile(r"(?:^| )(?:ssn|ss ?#|social security(?: number| no| num)?|itin)$"),
     lambda v: len(_digits(v)) == 9),
    ("date_of_birth", re.compile(r"(?:^| )(?:dob|date of birth|birth ?date|birth ?day|born(?: on)?)$"),
     lambda v: bool(_DATE.search(v.strip()))),
    ("bank_details", re.compile(r"(?:^| )(?:account (?:number|no|num)|acct(?: number| no| num)?|routing(?: number| no)?"
                                r"|aba(?: number)?|rtn|iban|bank account(?: number)?)$"),
     lambda v: len(_digits(v)) >= 6),
    ("payment_card", re.compile(r"(?:^| )(?:card (?:number|no|num)|cc (?:number|num|no)|(?:credit|debit) card(?: number)?"
                                r"|pan)$"),
     lambda v: 13 <= len(_digits(v)) <= 19),
    ("payment_card", re.compile(r"(?:^| )(?:cvv2?|cvc2?|card security code)$"),
     lambda v: bool(re.fullmatch(r"\s*[0-9]{3,4}\s*", v))),
    ("credit_file", re.compile(r"(?:^| )(?:credit score|fico(?: score)?|vantage ?score|beacon score)$"),
     lambda v: bool(re.fullmatch(r"\s*[3-8][0-9]{2}\s*", v))),
)
PHONE_KEY = re.compile(r"(?:^| )(?:phone|tel|telephone|mobile|cell|cellphone|whatsapp|sms|fax)(?: number| no| num)?$")


def _key_words(key: str) -> str:
    """'date_of_birth', 'dateOfBirth' and 'Date-Of-Birth' all read 'date of birth'."""
    key = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", key)
    return " ".join(w for w in re.split(r"[^a-z0-9#]+", key.lower()) if w)


def _scalar(v: object) -> str | None:
    """The text of a JSON value that can hold data on its own (not a bool, null, list or object)."""
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        return None
    text = str(v).strip()
    return None if not text or _already(text) else text


def _by_key(k: str, v: object, out: Cleaned, parent: str = "") -> tuple[bool, object]:
    """(handled, value) for a value its key says is a secret, restricted data or a phone number. The parent key counts
    too: {"credit": {"score": 612}} is a credit score."""
    text = _scalar(v)
    if text is None:
        return False, v
    k = k.strip()
    if SECRET_KEY.match(k.replace(" ", "_")) and (len(text) >= 4 if isinstance(v, str) else len(_digits(text)) >= 4):
        out.secret_types["credential_assignment"] = out.secret_types.get("credential_assignment", 0) + 1
        return True, REDACTED % "credential_assignment"
    words = _key_words(k)
    for name, rx, looks in RESTRICTED_KEYS:
        if (rx.search(words) or rx.search(f"{parent} {words}".strip())) and looks(text):  # type: ignore[operator]
            out.restricted = sorted(set(out.restricted) | {name})
            return True, ""
    if PHONE_KEY.search(words) and re.fullmatch(r"\+?[0-9 ().\-]{10,20}", text) and 10 <= len(_digits(text)) <= 15:
        out.masked["phone_number"] = out.masked.get("phone_number", 0) + 1
        return True, "[phone]"
    return False, v


LINE_NUMBER = re.compile(r"^[ \t]*[0-9]{1,7}[|:\t\u2192]")  # "     3|text", as Hermes's read_file shows a file


def _unnumbered(text: str) -> str:
    """A file as a tool showed it with line numbers, without them (the text as it is when the lines are not numbered)."""
    lines = text.splitlines()
    if not any(line.strip() for line in lines) or not all(LINE_NUMBER.match(line) for line in lines if line.strip()):
        return text
    return "\n".join(LINE_NUMBER.sub("", line, count=1) for line in lines)


def _parse_json(text: str) -> dict[str, Any] | list[Any] | None:
    """The JSON object or array a text holds (also a line-numbered JSON file), else None."""
    stripped = _unnumbered(text.strip()).strip()
    if stripped[:1] not in ("{", "[") or len(stripped) > 2_000_000:
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _table_restricted(text: str) -> list[str]:
    """Restricted columns of a CSV, TSV or Markdown table: a header that names the data and a row that holds it
    ("name,ssn" then "Jane,123456789")."""
    lines = [line for line in _unnumbered(text).splitlines() if line.strip()][:500]
    if len(lines) < 2:
        return []
    for sep in (",", "\t", ";", "|"):
        header = [_key_words(h.strip().strip("\"'")) for h in lines[0].split(sep)]
        if len(header) < 2:
            continue
        found: set[str] = set()
        for i, words in enumerate(header):
            for name, rx, looks in RESTRICTED_KEYS:
                if words and rx.search(words) and any(
                        i < len(cells) and looks(cells[i].strip().strip("\"'"))  # type: ignore[operator]
                        for cells in (row.split(sep) for row in lines[1:])):
                    found.add(name)
        if found:
            return sorted(found)
    return []


def _clean_json(value: object, kind: str, out: Cleaned, parent: str = "") -> object:
    if isinstance(value, str):
        nested = _parse_json(value) if ("{" in value or "[" in value) else None
        if nested is not None:  # JSON inside a value (a file a tool read): cleaned key by key as well
            return json.dumps(_clean_json(nested, kind, out, parent), ensure_ascii=False)
        return _clean_string(sanitize(value), kind, out)
    if isinstance(value, list):
        return [_clean_json(v, kind, out, parent) for v in value]
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for k, v in value.items():
            key = _clean_string(sanitize(str(k)), kind, out)  # keys can be addresses or numbers too
            if key in cleaned:
                n = 2
                while "%s (%d)" % (key, n) in cleaned:
                    n += 1
                key = "%s (%d)" % (key, n)
            handled, by_key = _by_key(str(k), v, out, parent)
            cleaned[key] = by_key if handled else _clean_json(v, kind, out, _key_words(str(k)))
        return cleaned
    return value


def clean(text: str, kind: str = "chat") -> Cleaned:
    """The text as it may be stored.

    kind: "chat" (conversations, notes, Markdown and text files: the strictest), or the kit's file kind for files
    whose layout matters ("code", "shell", "config"; code and shell still get the in-sentence rules).
    JSON text (a tool result) is cleaned value by value and re-serialised.
    """
    out = Cleaned("")
    text = sanitize(text or "")
    if "{" in text or "[" in text:
        parsed = _parse_json(text)
        if parsed is not None:
            cleaned = _clean_json(parsed, kind, out)
            if out.restricted:
                return Cleaned("", out.secret_types, out.restricted, {}, out.think_removed)
            out.text = json.dumps(cleaned, ensure_ascii=False)
            return out
    if kind == "chat" and "\\n" in text and "\n" not in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n")  # escaped output: read it as lines
    cleaned_text = _clean_string(text, kind, out)
    if out.restricted:
        return Cleaned("", out.secret_types, out.restricted, {}, out.think_removed)
    out.text = cleaned_text
    return out


def clean_path(rel: str) -> Cleaned:
    """A relative file path as it may be shown (a source_ref, a fallback title). Each part is cleaned on its own, so
    an ordinary long path is not taken for a random token; restricted data is looked for in the whole path too
    ("ssn/078051120.md"). Text is "" when the path holds restricted data."""
    out = Cleaned("", restricted=list(clean(rel, "chat").restricted))
    parts = []
    for part in (rel or "").split("/"):
        c = clean(part, "chat")
        out.merge(c)
        parts.append(c.text)
    if not out.restricted:
        out.text = "/".join(parts)
    return out


def instruction_flags(text: str) -> list[str]:
    """Instruction-like text (override attempts, approval bypasses, autonomous actions, shell commands)."""
    return sorted(kit.count(kit.INSTRUCTION_PATTERNS, text))


def topics(text: str) -> list[str]:
    return sorted(kit.count(kit.TOPIC_PATTERNS, text))


def content_hash(*parts: str) -> str:
    """Hash for de-duplication: whitespace-insensitive, case-sensitive."""
    joined = "\x1f".join(SPACE.sub(" ", p or "").strip() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def key_digest(value: str) -> str:
    """32 characters of SHA-256 in lower-case base32 (a-z, 2-7): unlike hex, never a run of digits a detector could
    read as a card or phone number, so a key holding one passes verify's scan."""
    raw = hashlib.sha256((value or "").encode("utf-8", "replace")).digest()
    return base64.b32encode(raw).decode("ascii").lower()[:32]


def key_part(value: str) -> str:
    """Source text as it may stand in a source key: as it is, or "sha256-" + its key_digest() when cleaning would
    change it (a secret, restricted data, an e-mail address or a phone number) or it is not one printable line.

    Keys are stored unmasked (import records, conflicts, tombstones), so they never hold what a text could not. A
    text a later detector starts to find something in gets a new key: the safe direction."""
    if not value:
        return ""
    if value.isprintable():
        cleaned = clean(value, "chat")
        if cleaned.text == value and not (cleaned.secret_types or cleaned.restricted or cleaned.masked):
            return value
    return "sha256-" + key_digest(value)


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + "\n[… cut: the full text is in the JARVIS backup]", True


def one_line(text: str, limit: int = 200) -> str:
    text = SPACE.sub(" ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --- Labels: what a message says JARVIS did, what went wrong, what was decided ---------------------------------
# Rule-based and deliberately simple: every label is shown with its source, as "matched by keywords".

PROBLEM = re.compile(
    r"(?i)\b(?:error|errors|failed|failure|fails|exception|traceback|timed? ?out|time-?out|could ?n[o']t|cannot|"
    r"can't|unable to|blocked|blocker|refused|denied|unauthori[sz]ed|forbidden|not wired|went wrong|crash(?:ed)?|"
    r"broken|bounced|rate.?limit(?:ed)?|[45]\d\d (?:error|status))\b")
ACCOMPLISHMENT = re.compile(
    r"(?i)\b(?:(?:i|we|jarvis|sarah|scout|ava|bobby|tom)\s+(?:have\s+|'ve\s+)?(?:sent|published|posted|scheduled|"
    r"created|fixed|resolved|deployed|updated|delivered|saved|wrote|drafted|generated|completed|finished|replied|"
    r"booked|launched|added|set up|configured)|"
    r"(?:done|completed|finished|sent|published|posted|scheduled|delivered|fixed|resolved|deployed)\s*[.!:]|"
    r"successfully\s+\w+|is now (?:live|done|up|fixed))\b")
DECISION_USER = re.compile(
    r"(?i)\b(?:approve[ds]?|go ahead|yes,? (?:do|send|post|publish|go)|let'?s (?:go|do|use|try|switch|stop|keep)|"
    r"decided?|decision|from now on|(?:always|never|don'?t|do not) (?:send|post|publish|reply|email|use|run|ask|"
    r"spend|pay|refund|delete)|stop (?:doing|sending|posting|running)|cancel (?:it|that|the)|hold off|instead of)\b")
DECISION_ASSISTANT = re.compile(
    r"(?i)\b(?:we decided|you decided|decision:|agreed to|going with|will now|from now on)\b")
ACTION_TOOL = re.compile(r"(?i)(?:send|post|publish|create|schedule|update|write|reply|upload|deploy|pause|shift)")


def labels_for(role: str, content: str, *, tool_names: list[str] | None = None, tool_error: bool = False) -> list[str]:
    """accomplishment / problem / decision labels for one message (an empty list for most)."""
    out: list[str] = []
    text = content or ""
    if role == "tool":
        if tool_error or PROBLEM.search(text[:2000]):
            out.append("problem")
        return out
    if role == "user":
        if DECISION_USER.search(text[:2000]):
            out.append("decision")
        return out
    if role == "assistant":
        if ACCOMPLISHMENT.search(text) or any(ACTION_TOOL.search(n) for n in tool_names or ()):
            out.append("accomplishment")
        if PROBLEM.search(text):
            out.append("problem")
        if DECISION_ASSISTANT.search(text):
            out.append("decision")
    return out


def excerpt(text: str, terms: list[str], width: int = 240) -> str:
    """A window of text around the first search term found, on one line."""
    flat = SPACE.sub(" ", text or "").strip()
    lower = flat.lower()
    for term in terms:
        i = lower.find(term.lower())
        if i >= 0:
            start = max(0, i - width // 3)
            piece = flat[start:start + width]
            return ("…" if start else "") + piece + ("…" if start + width < len(flat) else "")
    return one_line(flat, width)
