"""Cleaning text before it is stored as memory or archive, and the rule-based labels used to answer questions.

The order matters:
1. NUL characters and broken Unicode are removed (PostgreSQL rejects them).
2. Reasoning blocks (<think>…</think>) are removed.
3. Secrets are redacted: the detectors of the owner's kit, then the stricter rules here for conversations and notes
   (secrets written inside sentences, positional literals in scripts, secret URL parameters, long random tokens,
   whole configuration dumps). JSON tool output is cleaned value by value.
4. Restricted data (SSNs, card and bank details, dates of birth, a client's credit file) is detected: a record
   holding it keeps no text at all.
5. E-mail addresses, phone numbers and WhatsApp ids are masked as [email] and [phone].

Detection is pattern-based, so it is a safety net, not a guarantee: anything that reaches a person or an agent is
also gated by review and by sensitivity. Nothing here calls an AI model or the network.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field

from . import jarvis_kit as kit

# --- Restricted data: never stored -----------------------------------------------------------------------------
# A client's credit file and identity numbers. The words "credit repair" or "credit score" alone (the service and its
# marketing) are not restricted; a score with a number, bureau names, letters, derogatory items are.
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
RESTRICTED_PATTERNS: dict[str, re.Pattern[str]] = {
    "us_ssn": re.compile(
        r"(?i)(?<![0-9])(?!000|666|9[0-9]{2})[0-9]{3}-(?!00)[0-9]{2}-(?!0000)[0-9]{4}(?![0-9])"
        r"|\b(?:ssn|ss#|social security(?: number| no\.?| #)?|social)\b[^0-9\n]{0,30}"
        r"[0-9]{3}[ .\-]?[0-9]{2}[ .\-]?[0-9]{4}(?![0-9])"
        r"|\blast (?:four|4)(?: digits)? of (?:the |my |his |her |their )?(?:ssn|social)\b"),
    "payment_card": kit.PERSONAL_PATTERNS["payment_card"],
    "date_of_birth": re.compile(
        r"(?i)\b(?:date of birth|d\.o\.b\.?|dob|birth ?date)\b"
        r"|\b(?:born(?: on)?|birthday)\b\W{0,12}(?:[0-9]{1,2}[/.\-][0-9]{1,2}[/.\-][0-9]{2,4}"
        r"|" + _MONTH + r" [0-9]{1,2}(?:st|nd|rd|th)?,? [0-9]{4}|[0-9]{1,2} " + _MONTH + r",? [0-9]{4})"),
    "bank_details": re.compile(
        r"(?i)\b(?:routing number|account number|iban|swift code|bank account)\b"
        r"|\b(?:acct|account|a/c)\s*(?:#|no\.?|number|num)?\s*:?\s*[0-9][0-9 \-]{5,20}[0-9]"
        r"|\b(?:aba|routing|rtn)\s*(?:#|no\.?|number)?\s*:?\s*[0-9]{9}\b"),
    "credit_file": re.compile(
        r"(?i)\b(?:experian|equifax|trans ?union|credit reports?|credit files?|credit history|dispute letters?"
        r"|disputes? (?:with|to) (?:the )?(?:bureaus?|creditors?)|609 letters?|goodwill letters?"
        r"|(?:debt )?validation (?:letters?|requests?)|pay[- ]for[- ]delete|fcra|fdcpa|charge[- ]?offs?"
        r"|collections? (?:accounts?|agency|agencies)|tradelines?|hard inquir(?:y|ies)|derogator(?:y|ies)"
        r"|repossessions?|bankruptc(?:y|ies)|late payments?|credit utili[sz]ation"
        r"|(?:credit |fico |vantage ?)?score (?:is|of|at|was|went (?:up|down) to|:)\s*[3-8][0-9]{2}\b"
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
TOKEN = re.compile(r"(?<![A-Za-z0-9+/_\-])[A-Za-z0-9+/_\-]{32,}={0,2}(?![A-Za-z0-9+/_\-])")
ENV_LINE = re.compile(r"(?m)^\s*(?:export\s+|set\s+)?[A-Z][A-Z0-9_]{2,}\s*=\s*\S")
THINK_BLOCK = re.compile(r"<think(?:ing)?>.*?(?:</think(?:ing)?>|\Z)", re.IGNORECASE | re.DOTALL)
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
    """Removes <think>…</think> blocks (and an unclosed one to the end): private reasoning is never kept."""
    n = len(THINK_BLOCK.findall(text))
    return (THINK_BLOCK.sub("", text), n) if n else (text, 0)


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


def _secret_after_word(text: str, m: re.Match[str]) -> tuple[int, int] | None:
    """The secret-looking word that follows a password word: after 'is', ':' or '=' if present, else the first word
    with a digit or a symbol among the next few words ("root password Xy7!kLp9")."""
    line_end = text.find("\n", m.end())
    window_end = min(m.end() + 80, len(text) if line_end < 0 else line_end)
    window = text[m.end():window_end]
    sep = SEPARATOR.match(window)
    pos = sep.end() if sep else 0
    seen = 0
    for w in re.finditer(r"[\"'`]?(?P<w>[^\s\"'`]+)[\"'`]?", window[pos:]):
        word = w.group("w").rstrip(".,;)…")  # "…" is where one_line() cut a summary, not part of a word
        if word.lower() in FILLER or word in ("/", "|", "-", ":"):
            continue
        if len(word) > 1 and word.endswith(":"):  # a label before the value ("creds for wp-admin: <value>")
            continue
        seen += 1
        if seen > 3:
            return None
        # Only a word with a digit or a symbol: "cPanel", "set", "vault" and other prose stay.
        if _already(word) or not kit.looks_secret(word) or not re.search(r"[0-9]|[^A-Za-z0-9]", word):
            continue
        start = m.end() + pos + w.start("w")
        return start, start + len(word)
    return None


def _chat_secrets(text: str) -> tuple[str, dict[str, int]]:
    spans: list[tuple[int, int, str]] = []
    # The kit's assignment rule without its "start of a line" condition (DB_PASSWORD=… in a sentence).
    for _m, (s, e) in kit.matches(kit.SECRET_PATTERNS["credential_assignment"], "credential_assignment", text, "chat"):
        value = text[s:e]
        if not _already(value) and kit.looks_secret(value):
            spans.append((s, e, "credential_assignment"))
    for m in SECRET_WORD.finditer(text):
        span = _secret_after_word(text, m)
        if span:
            spans.append((*span, "credential_assignment"))
    for rx, name in ((AS_PASSWORD, "credential_assignment"), (GMAIL_SPACED, "gmail_app_password"), (PIN, "pin"),
                     (URL_SECRET, "url_secret_parameter")):
        for m in rx.finditer(text):
            if not _already(m.group("v")):
                spans.append((m.start("v"), m.end("v"), name))
    for m in CODE_SECRET.finditer(text):
        group = next(g for g in "abcd" if m.group(g) is not None)
        if not _already(m.group(group)):  # cleaning cleaned text again finds nothing new (verify, rescan)
            spans.append((m.start(group), m.end(group), "credential_assignment"))
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
    restricted = sorted(name for name, rx in RESTRICTED_PATTERNS.items()
                        if any(True for _ in kit.matches(rx, name, text, "text")))
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


def _clean_json(value: object, kind: str, out: Cleaned) -> object:
    if isinstance(value, str):
        return _clean_string(sanitize(value), kind, out)
    if isinstance(value, list):
        return [_clean_json(v, kind, out) for v in value]
    if isinstance(value, dict):
        cleaned: dict[str, object] = {}
        for k, v in value.items():
            key = _clean_string(sanitize(str(k)), kind, out)  # keys can be addresses or numbers too
            if key in cleaned:
                n = 2
                while "%s (%d)" % (key, n) in cleaned:
                    n += 1
                key = "%s (%d)" % (key, n)
            if (isinstance(v, str) and SECRET_KEY.match(str(k)) and len(v.strip()) >= 4
                    and not _already(v.strip())):
                out.secret_types["credential_assignment"] = out.secret_types.get("credential_assignment", 0) + 1
                cleaned[key] = REDACTED % "credential_assignment"
            else:
                cleaned[key] = _clean_json(v, kind, out)
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
    stripped = text.lstrip()
    if stripped[:1] in ("{", "[") and len(text) <= 2_000_000:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
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


def instruction_flags(text: str) -> list[str]:
    """Instruction-like text (override attempts, approval bypasses, autonomous actions, shell commands)."""
    return sorted(kit.count(kit.INSTRUCTION_PATTERNS, text))


def topics(text: str) -> list[str]:
    return sorted(kit.count(kit.TOPIC_PATTERNS, text))


def content_hash(*parts: str) -> str:
    """Hash for de-duplication: whitespace-insensitive, case-sensitive."""
    joined = "\x1f".join(SPACE.sub(" ", p or "").strip() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


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
