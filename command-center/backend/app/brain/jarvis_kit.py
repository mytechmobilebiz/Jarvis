#!/usr/bin/env python3
"""JARVIS migration kit: read-only discovery and an encrypted backup of a JARVIS (Hermes Agent) installation.

jarvis-kit.ps1 runs this inside a Docker container with no network, a read-only root filesystem and the JARVIS
folders mounted read-only, so the kit can neither change them nor send anything anywhere. The code itself also
only opens source files for reading, never follows links, and opens the database backup read-only and immutable
behind an SQLite authorizer that allows nothing but reading.

    discover --profile DIR --content DIR --db FILE --out DIR [--sample N | --full] [--check]
        An inventory without contents. discovery-summary.md and .json hold counts and structure only and are
        meant to be shareable; discovery-detail.json adds file paths and per-file findings and stays with the
        owner (file names can name customers).
    backup --profile DIR --content DIR --out DIR                 (passphrase on stdin)
        An encrypted archive (AES-256-GCM, key from the passphrase with scrypt) of the profile and content,
        without secret files, databases, logs, session transcripts, caches, programs and temporary files.
        Secret-looking strings in the copied text files are replaced by [REDACTED:<type>].
    verify --archive FILE [--extract DIR]                        (passphrase on stdin)
        Decrypts the archive, checks every file against its manifest and, if asked, extracts it into an empty
        folder.

Nothing found is ever written out as a value: secrets, personal data, message text, titles and reasoning only
ever appear as counts. Exit status: 0 done, 1 failed, 2 refused (nothing was read or written).
"""

import argparse
import datetime
import difflib
import functools
import hashlib
import io
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import statistics
import sys
import tarfile
import urllib.parse

KIT_VERSION = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
TEXT_SCAN_LIMIT = 4 * 1024 * 1024          # larger text files are counted and hashed, not scanned
TEXT_DIFF_LIMIT = 512 * 1024               # larger files are not compared line by line with their template
BACKUP_FILE_LIMIT = 100 * 1024 * 1024      # larger files are left out of the backup (counted in its report)
VALUE_SCAN_LIMIT = 200_000                 # characters of one database value that are scanned
DEFAULT_SAMPLE = 500
CHUNK = 1 << 20
MAGIC = b"CCJB1\n"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 17, 8, 1
MIN_PASSPHRASE = 12


class Refused(Exception):
    """A safety check failed before anything was read or written."""


class KitError(ValueError):
    """A failure whose message the kit wrote itself (so it holds no data from the installation)."""


# --- What the kit looks for. Only the number of matches is ever reported. -----------------------------------------

SECRET_PATTERNS = {
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openrouter_key": re.compile(r"sk-or-[A-Za-z0-9_\-]{20,}"),
    "openai_style_key": re.compile(r"(?<![A-Za-z0-9])sk-(?!ant-|or-)(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    "stripe_key": re.compile(r"(?<![A-Za-z0-9])(?:(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{24,})"),
    "github_token": re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
    "slack_token": re.compile(r"(?<![A-Za-z0-9])(?:xox[abposre]|xapp)-[A-Za-z0-9\-]{10,}"),
    # Keys made of letters and digits only: one touching "+" or "/" is a piece of base64 data (an embedded font, an
    # image), not a key.
    "aws_access_key": re.compile(r"(?<![A-Za-z0-9+/])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9+/])"),
    "google_api_key": re.compile(r"(?<![A-Za-z0-9+/])AIza[0-9A-Za-z_\-]{35}(?![A-Za-z0-9+/])"),
    "meta_access_token": re.compile(r"(?<![A-Za-z0-9+/])EAA[A-Za-z0-9]{50,}(?![A-Za-z0-9+/])"),
    "telegram_bot_token": re.compile(r"(?<![0-9])[0-9]{8,10}:AA[A-Za-z0-9_\-]{33}"),
    # A key block only when key material follows its first line (a real line break, or \n inside a JSON or .env
    # string): code or prose that merely names the header is left alone. It ends at its END line, else at the first
    # blank line.
    # (Indented in YAML, Markdown or code, or quoted with "> ", and with \n or \r\n written out in JSON.)
    "private_key": re.compile(
        r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[ \t]*(?:\\r)?(?:\\n|\r?\n)"
        r"(?:[ \t>]*[A-Za-z\-]+:[^\n\\]*(?:\\r)?(?:\\n|\r?\n))*"
        r"(?:[ \t>]*(?:\\r)?(?:\\n|\r?\n))?[ \t>]*[A-Za-z0-9+/=]{40,}[\s\S]*?"
        r"(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|(?=\r?\n[ \t]*\r?\n)|\Z)"),
    "jwt": re.compile(r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    "google_oauth_secret": re.compile(r"(?<![A-Za-z0-9])GOCSPX-[A-Za-z0-9_\-]{20,}"),
    "webhook_url": re.compile(
        r"(?i)https://(?:hooks\.slack\.com/(?:services|workflows|triggers)/[A-Za-z0-9/_\-]{20,}"
        r"|(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_\-]{20,})"),
    "bearer_token": re.compile(r"(?i)\bbearer[ \t]+(?P<v>[A-Za-z0-9._~+/\-]{20,}=*)"),
    # Header lines in any notation, including PowerShell hashtables (Authorization = "Basic ...").
    "basic_auth": re.compile(
        r"(?i)\bauthorization[\"']?[ \t]*[:=][ \t]*[\"']?basic[ \t]+(?P<v>[A-Za-z0-9+/]{8,}={0,2})"),
    # Postiz's public API, among others, takes the key itself as the Authorization header, without a scheme.
    "bare_authorization": re.compile(
        r"(?i)\bauthorization[\"']?[ \t]*[:=][ \t]*(?!(?:[\"'])?(?:basic|bearer|token|digest)\b)"
        r"(?:\"(?P<q1>[A-Za-z0-9._~+/\-]{20,}=*)\"|'(?P<q2>[A-Za-z0-9._~+/\-]{20,}=*)'"
        r"|(?P<v>[A-Za-z0-9._~+/\-]{20,}=*))"),
    "url_with_credentials": re.compile(r"\b[a-z][a-z0-9+.\-]{0,20}://[^\s/:@]{1,256}:(?P<v>[^\s/@]{1,256})@"),
    "url_secret_parameter": re.compile(
        r"(?i)[?&](?:api[_\-]?key|key|token|access[_\-]?token|secret|sig|signature)=(?P<v>[^\s&#\"']{8,})"),
    # MCP servers such as Postiz take the key as the last part of the URL path (JARVIS's playbooks/postiz.md).
    "url_path_secret": re.compile(
        r"(?i)\b[a-z][a-z0-9+.\-]{0,20}://[^\s/]{1,256}(?:/[^\s/?#]{1,256}){0,8}?/mcp/(?P<v>[A-Za-z0-9_\-]{16,})"
        r"(?![A-Za-z0-9_\-])"),
    # Every pair of a Cookie or Set-Cookie header (the value must hold name=value pairs, so a sentence about cookies
    # or code passing a variable is not one), and curl's -b / --cookie argument.
    "cookie": re.compile(
        r"(?i)\b(?:set-)?cookie[\"']?[ \t]*[:=][ \t]*"
        r"(?:\"(?P<q1>[^\"\n]{0,2048}=[^\"\n]{3,2048})\"|'(?P<q2>[^'\n]{0,2048}=[^'\n]{3,2048})'"
        r"|(?P<v>[^\s\"'{}()=;,]{1,256}=[^\r\n\"'{}()]{3,2048}))"),
    "cookie_argument": re.compile(
        r"(?<![A-Za-z0-9_\-])(?:-b|--cookie)[ \t]+(?:\"(?P<q1>[^\"\n]*=[^\"\n]*)\"|'(?P<q2>[^'\n]*=[^'\n]*)'"
        r"|(?P<v>[^\s=\"']+=[^\s\"']+))"),
    # WordPress application passwords: six groups of four letters and digits (checked for mixed case and digits).
    "wordpress_app_password": re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z0-9]{4} ){5}[A-Za-z0-9]{4}(?![A-Za-z0-9])"),
    # Google app passwords (four groups of four letters) given after a password word and a colon or equals sign.
    "gmail_app_password": re.compile(
        r"(?i)\b(?:app[ _\-]?password|password|passcode|pass)\b[^\n:=]{0,40}[:=][ \t]*[\"']?"
        r"(?P<v>(?<![A-Za-z0-9])[a-z]{4} [a-z]{4} [a-z]{4} [a-z]{4}(?![A-Za-z0-9]))"),
    # Windows commands that store a setting: setx NAME value, [Environment]::SetEnvironmentVariable("NAME", "value").
    "env_command": re.compile(
        r"(?i)(?:\bsetx[ \t]+[A-Za-z_][A-Za-z0-9_]*(?:pass|pwd|secret|token|key)[A-Za-z0-9_]*[ \t]+"
        r"|SetEnvironmentVariable\([ \t]*[\"'][A-Za-z0-9_]*(?:pass|pwd|secret|token|key)[A-Za-z0-9_]*[\"']"
        r"[ \t]*,[ \t]*)"
        r"(?:\"(?P<q1>[^\"\n]{4,512})\"|'(?P<q2>[^'\n]{4,512})'|(?P<v>[^\s\"',)]{4,512}))"),
    # Last, so that a value one of the specific patterns above already found is not counted twice. The name may be
    # SMTP_PASSWORD, smtp-pass, SECRET_KEY, apiKey or refreshToken (not inside a shell ${...}). A quoted value is
    # taken whole. An unquoted one is a single word here; how far it really goes, and whether it counts, depends on
    # the kind of file (see matches).
    "credential_assignment": re.compile(
        r"(?<![A-Za-z0-9_])(?<!\$\{)(?:(?i:(?:[A-Za-z0-9]{1,32}[_\-]){0,4}"
        r"(?:password|passwd|pwd|pass|passphrase|secret|api[_\-]?key|token|access[_\-]?key|client[_\-]?secret"
        r"|(?:secret|private|auth|encryption|signing|master|server|app|license)[_\-]?key))"
        r"|(?-i:_?[a-z][a-z0-9]*(?:Password|Passwd|Pass|Secret|Token)|_?(?:api|access|secret|private|client|auth)Key"
        r"|_?[a-z][a-z0-9]*(?:ApiKey|AccessKey|SecretKey|PrivateKey)))"
        r"[\"']?[ \t]*[:=][ \t]*"
        r"(?:\"(?P<q1>[^\"\n]{4,4096})\"|'(?P<q2>[^'\n]{4,4096})'|`(?P<q3>[^`\n]{4,512})`"
        r"|(?P<v>[^\s\"'`,;{}\[\]()<>|*][^\s\"'`,;{}\[\]|]{0,511}))"),
}
VALUE_GROUPS = ("v", "q1", "q2", "q3")
# Values that only show where a secret would go (templates, documentation, code reading a setting), not a secret.
# The whole value must have one of these shapes: a real password that merely starts like one is still a secret.
PLACEHOLDER_VALUE = re.compile(
    r"(?i)\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$\{\{[^{}\n]{1,64}\}\}|%[A-Za-z_][A-Za-z0-9_]*%|<[^<>\n]{1,64}>"
    r"|\{\{[^{}\n]{1,64}\}\}|\{[A-Za-z_][A-Za-z0-9_.\[\]'\"]*\}|%(?:\([A-Za-z_][A-Za-z0-9_]*\))?[sd]"
    r"|\$\([^\n]*\)|\$\{[^}\n]*\}|`[^`\n]*`|\$\{?[0-9@*#?$!]\}?|\$env:[A-Za-z_][A-Za-z0-9_]*|\$(?:null|true|false)"
    r"|\[[A-Za-z_ \-]{1,40}\]|\[REDACTED:[a-z_]+\]|your[-_ ][a-z0-9_ \-]*|x{3,}|\*{3,}|\.{3,}"
    r"|example(?:[-_.][a-z0-9_.\-]*)?|changeme|placeholder|replace(?:[-_ ][a-z_ \-]*)?|none|null|true|false|required"
    r"|optional|string|redacted|(?:os\.environ|process\.env|env)\b.*")
# How an unquoted "name = value" is read depends on the kind of file:
# - code: an expression (os.environ.get(...)), never a value; quoted templates ("Token %s", f"{user}") are left too;
# - shell: a literal NAME=value word, unless it is a reference ($VAR, %VAR%, `cmd`);
# - config: "key: value" at the start of a line takes the rest of the line (it may hold spaces);
# - text (notes, Markdown, JSON, messages): a single word, at the start of a line or after a JSON bracket, comma or
#   quote, and only if it looks like a secret (so "Password: never send it by email" stays).
CODE_EXTS = {".py", ".ps1", ".psm1", ".js", ".mjs", ".cjs", ".ts", ".rb", ".php"}
SHELL_EXTS = {".sh", ".bash", ".zsh", ".bat", ".cmd", ".command"}
CONFIG_EXTS = {".yaml", ".yml", ".ini", ".cfg", ".conf", ".toml", ".properties", ".env"}
ASSIGNMENT_PREFIX = re.compile(r"[ \t]*(?:export[ \t]+|set[ \t]+\"?|-[ \t]+|>[ \t]*)?[\"']?")
QUOTED_TEMPLATE = re.compile(r"\{|%[sd(]|\$\{|\s$")

PERSONAL_PATTERNS = {
    "email_address": re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,24}\b"),
    "phone_number": re.compile(
        r"(?<![0-9A-Za-z])(?:\+?1[ .\-]?)?\(?[2-9][0-9]{2}\)?[ .\-]?[0-9]{3}[ .\-][0-9]{4}(?![0-9])"),
    "us_ssn": re.compile(r"(?<![0-9])(?!000|666|9[0-9]{2})[0-9]{3}-(?!00)[0-9]{2}-(?!0000)[0-9]{4}(?![0-9])"),
    "payment_card": re.compile(r"(?<![0-9])(?:[0-9][ \-]?){12,18}[0-9](?![0-9])"),
    "date_of_birth": re.compile(r"(?i)\b(?:date of birth|d\.o\.b\.?|dob|birth ?date)\b"),
    "bank_details": re.compile(r"(?i)\b(?:routing number|account number|iban|swift code|bank account)\b"),
    "credit_repair_terms": re.compile(
        r"(?i)\b(?:experian|equifax|transunion|credit report|credit score|fico|dispute letter|fcra|charge[- ]?off"
        r"|collections? account|credit repair)\b"),
}
# Text that would act as an instruction or an authorization if it were imported as one.
INSTRUCTION_PATTERNS = {
    "override_attempt": re.compile(
        r"(?i)\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|earlier|all|your|system)\b"
        r"[^.\n]{0,20}\b(?:instructions?|rules?|prompts?|guidelines?)\b"),
    "approval_bypass": re.compile(
        r"(?i)\b(?:without (?:asking|approval|permission|confirmation)|no approval|never needs? approval|pre-?approved"
        r"|auto-?approve|don'?t ask|do not ask|skip (?:the )?approval)\b"),
    "autonomous_action": re.compile(
        r"(?i)\b(?:send|reply|email|publish|post|deploy|push|pay|refund|transfer|delete)\b[^.\n]{0,30}"
        r"\b(?:automatically|immediately|on your own|unattended)\b"),
    "shell_command": re.compile(
        r"(?im)^[ \t]*(?:\$[ \t]|>[ \t])?"
        r"(?:sudo|curl|wget|rm[ \t]+-rf|powershell|pwsh|cmd(?:\.exe)?[ \t]+/c|bash[ \t]+-c"
        r"|ssh|scp|python3?[ \t]|node[ \t]|git[ \t]+push|gh[ \t]+pr)\b"),
}
TOPIC_PATTERNS = {
    "leads_and_proposals": re.compile(r"(?i)\b(?:leads?|prospects?|quotes?|proposals?|pricing)\b"),
    "support": re.compile(r"(?i)\b(?:support|refunds?|complaints?|tickets?|faq)\b"),
    "email": re.compile(r"(?i)\b(?:e-?mails?|inbox|smtp|imap)\b"),
    "advertising": re.compile(r"(?i)\b(?:roas|cpa|ad ?spend|campaigns?|meta ads|facebook ads|ad sets?)\b"),
    "social_and_content": re.compile(
        r"(?i)\b(?:captions?|hooks?|reels?|tiktok|instagram|linkedin|postiz|hashtags?|blog)\b"),
    "seo": re.compile(r"(?i)\b(?:seo|keywords?|backlinks?|serp|rankings?)\b"),
    "websites_and_hosting": re.compile(
        r"(?i)\b(?:wordpress|websites?|domains?|dns|ssl|hosting|cwp|cpanel|plugins?|uptime)\b"),
    "development": re.compile(r"(?i)\b(?:git|github|pull requests?|deploy|repository|commits?)\b"),
    "finance": re.compile(r"(?i)\b(?:revenue|invoices?|stripe|payments?|mrr)\b"),
    "credit_repair": re.compile(r"(?i)\b(?:credit repair|credit report|disputes?|experian|equifax|transunion|fico)\b"),
}
PLACEHOLDER_MARKERS = re.compile(r"REPLACE ME|EXAMPLE|<placeholder>|\{\{[A-Z_]+\}\}|\bTODO\b|\bTBD\b|(?i:lorem ipsum)")

SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:/@+\- ]{1,64}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
CRON_SESSION = re.compile(r"^cron_(.+?)_[0-9]{8}_[0-9]{6}$")
SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
CRON_EXPR = re.compile(r"^[0-9A-Za-z*/,\-?#LW ]{1,64}$")
REASONING_COLUMN = re.compile(r"(?i)(?:^|_)(?:reasoning|thinking|thoughts?|scratchpad|chain_of_thought|cot)(?:_|$)")
ID_COLUMN = re.compile(r"(?i)(?:^id$|_id$|^rowid$|^uuid$)")
# Random identifiers (Hermes's message_uid, tool_call_uids and the like): not text to scan.
NON_CONTENT_COLUMN = re.compile(r"(?i)(?:^|_)(?:uids?|uuids?|hash|sha256|checksum)$")
DUPLICATE_COLUMNS = ("content", "tool_calls", "tool_name")
ENUM_COLUMNS = {"role", "source", "model", "tool_name", "finish_reason", "end_reason", "platform", "status", "kind",
                "type", "archived", "active", "compacted", "billing_provider", "provider"}
TIME_COLUMNS = {"timestamp", "started_at", "ended_at", "created_at", "updated_at", "last_active", "time", "ts"}

TEMPLATE_MCP_SERVERS = {"hud", "postiz", "meta-ads", "meta_ads"}  # the servers JARVIS and its playbooks set up

# --- Files ---------------------------------------------------------------------------------------------------------

SECRET_NAMES = {".env", ".envrc", "auth.json", "local-api.json", "credentials.json", ".credentials.json", "token.json",
                "tokens.json", "creds.json", ".netrc", "_netrc", ".git-credentials", ".pgpass", "id_rsa", "id_dsa",
                "id_ecdsa", "id_ed25519"}
SECRET_EXTS = {".pem", ".key", ".p12", ".pfx", ".kdbx", ".keystore", ".jks", ".ppk", ".gpg"}
SECRET_DIRS = {"secret", "secrets", "credential", "credentials", ".ssh", ".gnupg"}
# Messaging bridges keep a linked device's full account keys in a session folder (WhatsApp: platforms/whatsapp/session
# or whatsapp/session in the profile); whoever has them can use the account.
BRIDGE_DIRS = {"whatsapp", "platforms", "telegram", "signal", "baileys"}
SSH_KEY_NAME = re.compile(r"^id_(?:rsa|dsa|ecdsa|ed25519)(?:[_.\-][a-z0-9_.\-]*)?$")
# Credential stores Hermes keeps in a profile (its own list of profile credential paths), relative to the profile:
# whole folders, and files at the top.
HERMES_SECRET_DIRS = {"mcp-tokens", "vault", "browser-profile", "browser-profiles", "browser_profiles", "browser_auth",
                      "bot-desktop", "chrome-debug", "pairing", "proxy", "home", "matrix", "weixin"}
HERMES_SECRET_FILES = {"npmrc", ".npmrc", ".op.env", ".anthropic_oauth.json", "slack_tokens.json", "honcho.json",
                       "mem0.json", "webhook_subscriptions.json", "teams_pipeline_store.json", ".copilot_jwt.json"}
# Browser profile stores (saved logins, autofill, the cookie key), wherever they are.
BROWSER_STORE = re.compile(r"^(?:login data|web data|local state|cookies)(?:[-. ].*)?$")
DATABASE_EXTS = {".db", ".sqlite", ".sqlite3", ".db3"}
PROGRAM_EXTS = {".exe", ".dll", ".so", ".dylib", ".pyd", ".msi", ".node", ".wasm", ".bin", ".com", ".scr", ".sys"}
CACHE_DIRS = {"__pycache__", ".cache", "cache", "caches", "node_modules", ".venv", "venv", "site-packages",
              ".pytest_cache", ".mypy_cache", ".ruff_cache", "gpucache", "code cache"}
TEMP_DIRS = {"tmp", "temp", ".tmp", "sandbox", "sandboxes", ".sandbox"}
TEMP_EXTS = {".tmp", ".temp", ".swp", ".swo", ".bak", ".old", ".part", ".crdownload", ".lock", ".orig", ".save",
             ".backup", ".rej"}
TEXT_EXTS = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".py", ".ps1",
             ".sh", ".bat", ".cmd", ".js", ".mjs", ".cjs", ".ts", ".html", ".htm", ".css", ".csv", ".xml", ".eml",
             ".log", ".command", ".sql", ".svg"}
# Categories whose files are instructions, knowledge or automation: compared with the JARVIS templates.
REVIEW_CATEGORIES = ("instructions", "configuration", "knowledge", "skill", "schedule", "script", "plugin_or_hook",
                     "memory")
# What the encrypted backup leaves out (the database is backed up separately; the others are never copied).
BACKUP_EXCLUDED = {"secret", "database", "log", "session_transcript", "cache", "program", "temporary", "demo",
                   "template_demo"}
WORKSPACE_KNOWN = {"reports", "briefs", "proposals", "metrics"}


def classify(area, rel):
    """The category of a file, from its path relative to the profile or content folder."""
    lower = rel.lower().split("/")
    name, dirs = lower[-1], lower[:-1]
    ext = os.path.splitext(name)[1]
    if (name in SECRET_NAMES or name.startswith(".env") or name.endswith(".env") or ext in SECRET_EXTS
            or (SSH_KEY_NAME.match(name) and not name.endswith(".pub"))
            or any(word in name for word in ("secret", "credential", "cookie"))
            or any(d in SECRET_DIRS or d.startswith("auth_info") for d in dirs)
            or (area == "profile" and dirs and dirs[0] in HERMES_SECRET_DIRS)
            or (area == "profile" and dirs[:2] == ["platforms", "pairing"])
            or (area == "profile" and dirs[:1] == ["runtime"] and name == "photon-sidecar.json")
            or (area == "profile" and not dirs
                and (name in HERMES_SECRET_FILES or re.match(r"^google_.*token.*", name)))
            or BROWSER_STORE.match(name)
            # Hermes's own backups are full copies of the profile, .env and sessions included.
            or (area == "profile" and dirs[:1] == ["backups"] and ext in (".zip", ".tar", ".gz", ".tgz"))
            or (any(d in ("session", "sessions") for d in dirs) and any(d in BRIDGE_DIRS for d in dirs))):
        return "secret"
    if ext in DATABASE_EXTS or re.search(r"\.(?:db|sqlite3?)-(?:wal|shm|journal)$", name):
        return "database"
    if any(d in CACHE_DIRS for d in dirs) or ext in (".pyc", ".pyo"):
        return "cache"
    if ext in PROGRAM_EXTS:
        return "program"
    # Includes copies such as config.yaml.malformed-backup, which JARVIS's setup leaves next to the original.
    if (any(d in TEMP_DIRS for d in dirs) or ext in TEMP_EXTS or name.endswith("~") or name.startswith(".~")
            or ".malformed-backup" in name or re.search(r"\.bak[0-9]*$", name)):
        return "temporary"
    top = dirs[0] if dirs else ""
    if top == "logs" or ext == ".log":
        return "log"
    if area == "content":
        if not dirs:
            return "setup_state" if ext == ".json" else "other"
        return {"agents": "template_agents", "playbooks": "template_playbooks", "demo": "template_demo",
                "examples": "template_demo"}.get(top, "other")
    if top == "sessions":
        return "session_transcript"
    if top in ("memories", "memory") or (not dirs and name in ("memory.md", "user.md")):
        return "memory"
    folder_category = {"skills": "skill", "cron": "schedule", "plugins": "plugin_or_hook", "hooks": "plugin_or_hook",
                       "knowledge": "knowledge", "workspace": "workspace", "scripts": "script", "demo": "demo"}
    if top in folder_category:
        return folder_category[top]
    if not dirs and name in ("soul.md", "agents.md", "operations.md", ".hermes.md"):
        return "instructions"
    if not dirs and name in ("config.yaml", "config.yml", "distribution.yaml"):
        return "configuration"
    if not dirs and ext == ".json":
        return "setup_state"
    if ext in (".py", ".ps1", ".sh", ".bat", ".cmd", ".command", ".js", ".mjs"):
        return "script"
    return "other"


def walk(root):
    """(relative path, full path, lstat) for every regular file under root, and the links and special files skipped.

    Links are never followed: a link could lead out of the folder the owner named."""
    files, skipped = [], []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            if os.path.islink(full) or _is_junction(full):
                skipped.append({"path": _rel(root, full), "kind": "link to a folder (not followed)"})
                dirnames.remove(d)
        for f in sorted(filenames):
            full = os.path.join(dirpath, f)
            try:
                st = os.lstat(full)
            except FileNotFoundError:  # removed by JARVIS since the folder was listed
                continue
            if stat.S_ISLNK(st.st_mode):
                skipped.append({"path": _rel(root, full), "kind": "link (not followed)"})
            elif not stat.S_ISREG(st.st_mode):
                skipped.append({"path": _rel(root, full), "kind": "special file"})
            else:
                files.append((_rel(root, full), full, st))
    return files, skipped


def _is_junction(path):
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


def _rel(root, full):
    return os.path.relpath(full, root).replace(os.sep, "/")


def read_file(full, size):
    """sha256 of the file and, unless it is larger than TEXT_SCAN_LIMIT, its bytes (read once, read-only)."""
    digest, kept, keep = hashlib.sha256(), bytearray(), size <= TEXT_SCAN_LIMIT
    with open(full, "rb") as f:
        while True:
            block = f.read(CHUNK)
            if not block:
                break
            digest.update(block)
            if keep:
                kept += block
    return digest.hexdigest(), (bytes(kept) if keep and len(kept) <= TEXT_SCAN_LIMIT else None)


# Documents and media: never decoded as text (a stray match could corrupt them), counted as not scanned.
BINARY_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".zip", ".gz", ".7z", ".rar",
               ".docx", ".xlsx", ".pptx", ".odt", ".wav", ".mp3", ".mp4", ".m4a", ".ogg", ".webm", ".mov", ".woff",
               ".woff2", ".ttf", ".otf"}
BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))


def text_encoding(rel, raw):
    """(encoding, byte-order mark) when raw is text, judged by its content (Windows PowerShell 5.1 writes UTF-16),
    or None for binary data."""
    if os.path.splitext(rel.lower())[1] in BINARY_EXTS:
        return None
    for bom, encoding in BOMS:
        if raw.startswith(bom):
            return encoding, bom
    head = raw[:8192]
    half = len(head) // 2
    if half >= 2 and head[1::2].count(0) >= 0.9 * half and head[0::2].count(0) < 0.1 * half:
        return "utf-16-le", b""
    return None if b"\0" in head else ("utf-8", b"")


def as_text(rel, raw):
    """The text of a file with LF line endings, or None if it is not text."""
    found = text_encoding(rel, raw)
    if found is None:
        return None
    encoding, bom = found
    try:
        text = raw[len(bom):].decode(encoding, errors="replace" if encoding == "utf-8" else "strict")
    except UnicodeDecodeError:
        return None
    return text.replace("\r\n", "\n")


def normalize(data):
    """Bytes as compared with the templates: no byte-order mark, LF line endings (Windows checkouts use CRLF)."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.replace(b"\r\n", b"\n")


def decode(data):
    return normalize(data).decode("utf-8", errors="replace")


def luhn(digits):
    total, double = 0, False
    for ch in reversed(digits):
        n = ord(ch) - 48
        if double:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        double = not double
    return total % 10 == 0


def value_group(m):
    """The group holding a match's secret value (the rest of the match only says where it is), or None."""
    for group in VALUE_GROUPS:
        if group in m.re.groupindex and m.group(group) is not None:
            return group
    return None


def file_kind(rel):
    """How a file's unquoted values are read (see CODE_EXTS): code, shell, config or text."""
    name = os.path.basename(rel.lower())
    ext = os.path.splitext(name)[1]
    if ext in CODE_EXTS:
        return "code"
    if ext in SHELL_EXTS:
        return "shell"
    if ext in CONFIG_EXTS or name.startswith(".env"):
        return "config"
    return "text"


def looks_secret(value):
    """A single word that could be a password or key: long enough, and not a plain lower-case word."""
    return len(value) >= 6 and bool(re.search(r"[0-9]", value) or re.search(r"[^A-Za-z0-9]", value)
                                    or re.search(r"[A-Z]", value[1:]))


def matches(rx, name, text, kind="text"):
    """(match, value span) for each match of one pattern that counts: placeholders, invalid card numbers, code and
    ordinary prose do not."""
    found = []
    for m in rx.finditer(text):
        if name == "payment_card" and not luhn(re.sub(r"[ \-]", "", m.group(0))):
            continue
        if name == "wordpress_app_password" and not (re.search(r"[0-9]", m.group(0)) and re.search(r"[A-Z]", m.group(0))
                                                     and re.search(r"[a-z]", m.group(0))):
            continue
        group = value_group(m)
        value = m.group(group).strip() if group else ""
        span = m.span(group) if group else m.span()
        if group and PLACEHOLDER_VALUE.fullmatch(value):
            continue
        if kind == "code" and ((group == "v" and name in ("credential_assignment", "bare_authorization"))
                               or (group in ("q1", "q2", "q3") and QUOTED_TEMPLATE.search(m.group(group)))):
            continue
        if name == "credential_assignment" and group == "v":
            line_start = text.rfind("\n", 0, m.start()) + 1
            prefix = text[line_start:m.start()]
            at_line_start = bool(ASSIGNMENT_PREFIX.fullmatch(prefix))
            if kind == "shell" and value[:1] in ("$", "%", "`"):
                continue
            if kind == "config" and at_line_start:
                end = text.find("\n", span[0])
                rest = text[span[0]:len(text) if end < 0 else end]
                rest = re.split(r"[ \t]+#", rest, maxsplit=1)[0].rstrip()
                if PLACEHOLDER_VALUE.fullmatch(rest.strip()):
                    continue
                span = (span[0], span[0] + len(rest))
            elif kind in ("text", "config"):
                json_like = prefix.rstrip().endswith(("{", ",", '"', "'", "`"))
                if not (at_line_start or json_like) or not looks_secret(value):
                    continue
        found.append((m, span))
    return found


def count(patterns, text, kind="text"):
    """Matches per pattern; a match overlapping one already counted (by an earlier pattern) is not counted again."""
    result, taken = {}, []
    for name, rx in patterns.items():
        n = 0
        for m, span in matches(rx, name, text, kind):
            start, end = min(m.start(), span[0]), max(m.end(), span[1])
            if any(start < e and s < end for s, e in taken):
                continue
            taken.append((start, end))
            n += 1
        if n:
            result[name] = n
    return result


def lines_of(patterns, text, kind="text"):
    """Line numbers with at least one match (for the owner's detail report: where to look, not what is there)."""
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    found = set()
    for name, rx in patterns.items():
        for m, _span in matches(rx, name, text, kind):
            lo, hi = 0, len(starts) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if starts[mid] <= m.start():
                    lo = mid
                else:
                    hi = mid - 1
            found.add(lo + 1)
    return sorted(found)[:50]


def redact(text, kind="text"):
    """Text with every secret-looking value replaced by [REDACTED:<type>], and the number of replacements."""
    total = 0
    for name, rx in SECRET_PATTERNS.items():
        pieces, last, n = [], 0, 0
        for _m, (start, end) in matches(rx, name, text, kind):
            if start < last:
                continue
            pieces.append(text[last:start])
            pieces.append("[REDACTED:%s]" % name)
            last, n = end, n + 1
        if n:
            pieces.append(text[last:])
            text, total = "".join(pieces), total + n
    return text, total


PERSONAL_IN_LABELS = {k: PERSONAL_PATTERNS[k] for k in ("email_address", "phone_number", "us_ssn", "payment_card")}
PERSONAL_IN_LABELS["phone_digits"] = re.compile(r"(?<![0-9A-Za-z])\+?[0-9]{7,15}(?![0-9])")


def label(value, other="(other value)"):
    """A short value that is safe to report (no secret and no personal data in it), else a stand-in."""
    if value is None:
        return "(none)"
    value = str(value)
    ok = SAFE_LABEL.match(value) and not count(SECRET_PATTERNS, value) and not count(PERSONAL_IN_LABELS, value)
    return value if ok else other


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- Templates (what JARVIS 1.7.4 shipped and what the repository holds now) ---------------------------------------

def load_templates(path=None):
    path = path or os.path.join(HERE, "templates.json")
    with open(path, "rb") as f:
        templates = json.load(f)
    by_hash = {}
    for repo_path, revisions in templates["files"].items():
        for revision, info in revisions.items():
            if isinstance(info, dict) and "sha256" in info:
                by_hash.setdefault(info["sha256"], []).append((repo_path, revision))
    templates["by_hash"] = by_hash
    return templates


def template_candidates(area, rel, templates):
    """Repository paths a profile or content file was made from (setup composes the six roles into one profile)."""
    files = templates["files"]
    if area == "content":
        return [rel] if rel in files else []
    parts = rel.split("/")
    if len(parts) == 1:
        found = ["agents/jarvis/" + rel]
    elif parts[0] == "knowledge":
        found = ["agents/jarvis/knowledge/" + "/".join(parts[1:])]
        if len(parts) > 2:
            found.insert(0, "agents/%s/knowledge/%s" % (parts[1], "/".join(parts[2:])))
    elif parts == ["cron", "jobs.json"]:
        found = []  # written by Hermes when setup registers the jobs; compared job by job instead
    elif parts[0] in ("skills", "scripts", "cron"):
        tail = "/".join(parts)
        found = sorted(p for p in files if p.startswith("agents/") and p.split("/", 2)[-1] == tail)
    elif parts[0] == "demo":
        found = [rel]
    else:
        found = []
    return [p for p in found if p in files]


def compare_with_templates(area, rel, normalized_hash, text, templates):
    """"identical" or "modified" only for a file at a template's own place; a copy elsewhere (perhaps under a name the
    owner chose) is "same content as a template", so that its path is never taken for a template path."""
    candidates = template_candidates(area, rel, templates)
    elsewhere = []
    for repo_path, revision in templates["by_hash"].get(normalized_hash, []):
        if repo_path in candidates:
            return {"template": "identical", "template_source": "%s@%s" % (repo_path, revision)}
        elsewhere.append("%s@%s" % (repo_path, revision))
    if not candidates:
        if elsewhere:
            return {"template": "same content as a template", "template_source": elsewhere[0]}
        return {"template": "not from a template"}
    result = {"template": "modified", "template_source": candidates[0]}
    revisions = templates["files"][candidates[0]]
    if text is not None and len(text) <= TEXT_DIFF_LIMIT:
        best = None
        for revision, info in revisions.items():
            if isinstance(info, dict) and "text" in info:
                diff = list(difflib.unified_diff(info["text"].splitlines(), text.splitlines(), n=0, lineterm=""))
                added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
                removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
                if best is None or added + removed < best[1] + best[2]:
                    best = (revision, added, removed)
        if best:
            result.update(template_source="%s@%s" % (candidates[0], best[0]), lines_added=best[1],
                          lines_removed=best[2])
    return result


# --- Structured files: names and shapes only -----------------------------------------------------------------------

ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def closes(text, quote):
    """True if text holds the closing quote (a backslash escapes inside double quotes, as in dotenv)."""
    i = 0
    while i < len(text):
        if quote == '"' and text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return True
        i += 1
    return False


def env_names(text):
    """The setting names in a .env file. Lines inside a quoted value spanning several lines (a PEM key, say) and
    inside an unquoted key block are values, not names, and are skipped."""
    names, empty, quote, in_block, value_lines, after_empty = [], 0, None, False, 0, False
    for line in text.splitlines():
        follows_empty, after_empty = after_empty, False
        if quote:
            if closes(line, quote):
                quote = None
            continue
        if in_block:
            if "-----END " in line:
                in_block = False
            continue
        m = ENV_LINE.match(line)
        if not m:
            if "-----BEGIN " in line:
                in_block = True
            continue
        value = m.group(2).strip()
        name = m.group(1)
        # A pasted value on its own line (base64 padding makes it look like NAME=) is not a name: random mixed case
        # with digits, or a padded line right after an empty NAME=.
        looks_random = re.search(r"[a-z]", name) and re.search(r"[A-Z]", name) and re.search(r"[0-9]", name)
        if looks_random or (follows_empty and set(value) <= {"="} and " " not in line.strip()):
            value_lines += 1
            continue
        if ENV_NAME.match(name):
            names.append(name)
            if value in ("", '""', "''"):
                empty += 1
                after_empty = True
        if value[:1] in ("'", '"') and not closes(value[1:], value[0]):
            quote = value[0]
        elif "-----BEGIN " in value and "-----END " not in value:
            in_block = True
    return {"variables": sorted(set(names)), "empty_values": empty, "value_like_lines": value_lines}


# Key names of credential files that say what is stored without saying whose it is. Any other key (it could be a
# client, an account, a domain or a number) is shown as "(key)" in the shareable summary.
SHARED_KEY_NAMES = {
    "providers", "provider", "accounts", "account", "active_provider", "default", "version", "type", "plan",
    "access_token", "refresh_token", "id_token", "token", "tokens", "token_type", "expires_at", "expires_in", "expiry",
    "scope", "scopes", "client_id", "client_secret", "client_email", "api_key", "key", "keys", "secret", "password",
    "user", "username", "email", "installed", "web", "project_id", "auth_uri", "token_uri", "redirect_uris",
    "private_key", "private_key_id", "auth_provider_x509_cert_url", "client_x509_cert_url", "universe_domain",
    "anthropic", "openai", "openrouter", "nous", "codex", "codex-cli", "claude", "claude-cli", "gemini", "google",
    "github", "slack", "discord", "telegram", "whatsapp", "signal", "email_provider", "stripe", "meta", "postiz",
    "youtube", "revenuecat", "noisekey", "signedidentitykey", "signedprekey", "registrationid", "advsecretkey", "me",
}


def json_key_paths(text, depth=3):
    """Key paths of a credential file, never values: "keys" (personal data and secrets hidden) for the owner's detail
    report, and "shared_keys" (only known setting names; anything else is "(key)") for the summary."""
    try:
        data = json.loads(text)
    except ValueError:
        return {"keys": [], "shared_keys": [], "valid_json": False}
    paths, shared = [], []

    def visit(node, prefix, shared_prefix, level):
        if isinstance(node, dict) and level < depth:
            for key in sorted(node, key=str):
                name = label(key, "(key)")
                shared_name = str(key) if str(key).lower() in SHARED_KEY_NAMES else "(key)"
                path = prefix + "." + name if prefix else name
                shared_path = shared_prefix + "." + shared_name if shared_prefix else shared_name
                paths.append(path)
                if shared_path not in shared:
                    shared.append(shared_path)
                visit(node[key], path, shared_path, level + 1)
    visit(data, "", "", 0)
    return {"keys": paths[:200], "shared_keys": shared[:100], "valid_json": True}


CONFIG_VALUE_KEYS = {"model", "model.default", "model.provider", "provider", "terminal.backend", "stt.provider",
                     "stt.enabled", "tts.provider", "memory.provider", "memory.memory_enabled",
                     "memory.user_profile_enabled", "approvals.mode", "approvals.cron_mode", "agent.max_turns",
                     "agent.disabled_toolsets", "group_sessions_per_user", "unauthorized_dm_behavior"}


def yaml_keys(text):
    """Key paths of a YAML file (two levels), and values only for a short list of harmless settings."""
    keys, values, stack = [], {}, []
    block_indent = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None:
            if indent > block_indent:
                continue
            block_indent = None
        m = re.match(r"^(\s*)([A-Za-z0-9_.\-]+):(?:\s+(.*))?$", line)
        if not m:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, label(m.group(2), "(key)")))  # a key can be a client's name or number
        path = ".".join(k for _, k in stack)
        if len(stack) <= 3 and SAFE_LABEL.match(path.replace("(key)", "key")):
            keys.append(path)
        value = (m.group(3) or "").split(" #")[0].strip().strip("\"'")
        if value in ("|", ">", "|-", ">-"):
            block_indent = indent
        elif path in CONFIG_VALUE_KEYS and value and SAFE_LABEL.match(value) and not count(SECRET_PATTERNS, value):
            values[path] = value
    names = sorted(set(keys))
    servers = sorted({k.split(".")[1] for k in names if k.startswith("mcp_servers.") and k.count(".") >= 1})
    return {"keys": names[:300], "values": values, "mcp_servers": servers}


def frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        return {}
    fields = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z0-9_\-]+):\s*(.*)$", line)
        if km:
            fields[km.group(1)] = km.group(2).strip().strip("\"'")
    return fields


def memory_entries(text):
    """Hermes keeps one memory per entry, separated by a line holding only the section sign."""
    if "§" in text:
        return len([e for e in re.split(r"\n?§\n?", text) if e.strip()])
    return len([line for line in text.splitlines() if re.match(r"^\s*(?:[-*]|\d+\.)\s+\S", line)])


def cron_jobs(text, template_jobs, template_skills):
    try:
        data = json.loads(text)
    except ValueError:
        return {"valid_json": False}
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return {"valid_json": True, "jobs": []}
    out = []
    for n, job in enumerate(j for j in jobs if isinstance(j, dict)):
        name = job.get("name") if isinstance(job.get("name"), str) else ""
        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        expr = schedule.get("expr") or job.get("schedule_expr") or job.get("cron") or ""
        expr = expr if isinstance(expr, str) and CRON_EXPR.match(expr) else ("(other)" if expr else "")
        deliver = job.get("deliver")
        deliver = label(deliver.split(":")[0]) if isinstance(deliver, str) and deliver else "(none)"
        skills = job.get("skills") if isinstance(job.get("skills"), list) else []
        prompt = job.get("prompt") if isinstance(job.get("prompt"), str) else ""
        entry = {
            "template_job": name in template_jobs,
            "name": name if name in template_jobs else "custom job %d" % (n + 1),
            "name_in_detail": label(name),
            "id": job.get("id") if isinstance(job.get("id"), str) and SAFE_JOB_ID.match(job["id"]) else None,
            "schedule": expr,
            "enabled": job.get("enabled") if isinstance(job.get("enabled"), bool) else None,
            "deliver": deliver,
            "skills": [s if s in template_skills else "custom skill" for s in skills if isinstance(s, str)],
            "prompt_characters": len(prompt),
            "prompt_differs_from_template": (name in template_jobs and prompt != template_jobs[name].get("prompt")),
            "other_fields": sorted(k for k in job if isinstance(k, str) and SAFE_LABEL.match(k))[:40],
        }
        out.append(entry)
    return {"valid_json": True, "jobs": out}


# --- Discovery of files --------------------------------------------------------------------------------------------

def inventory(area, root, templates):
    files, skipped = walk(root)
    records = []
    template_jobs = templates.get("cron_jobs", {})
    template_skills = set(templates.get("skill_names", []))
    for rel, full, st in files:
        category = classify(area, rel)
        rec = {"area": area, "path": rel, "size": st.st_size, "modified": iso(st.st_mtime), "category": category}
        name = rel.rsplit("/", 1)[-1].lower()
        if category == "secret":
            # Names only, for planning the separate credential migration; values are never kept.
            if name.startswith(".env") or name.endswith(".env") or name.endswith(".json"):
                try:
                    _, raw = read_file(full, st.st_size)
                except OSError:
                    raw = None
                text = as_text(rel, raw) if raw is not None else None
                if text is not None:
                    rec.update(json_key_paths(text) if name.endswith(".json") else env_names(text))
            records.append(rec)
            continue
        if category in ("database", "cache", "program", "temporary"):
            records.append(rec)
            continue
        try:
            digest, raw = read_file(full, st.st_size)
        except OSError:  # removed or locked by JARVIS since the folder was listed
            rec["scanned"] = False
            rec["not_scanned_because"] = "changed, removed or unreadable during the run"
            records.append(rec)
            continue
        rec["sha256"] = digest
        text = as_text(rel, raw) if raw is not None else None
        if text is None:
            rec["scanned"] = False
            too_large = "larger than %d MB" % (TEXT_SCAN_LIMIT // 2 ** 20)
            rec["not_scanned_because"] = too_large if raw is None else "not text"
            if category in REVIEW_CATEGORIES or area == "content":
                rec.update(compare_with_templates(area, rel, digest, None, templates))
            records.append(rec)
            continue
        kind = file_kind(rel)
        rec["text"] = True
        rec["lines"] = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        rec["secret_like"] = count(SECRET_PATTERNS, text, kind)
        rec["personal_data"] = count(PERSONAL_PATTERNS, text)
        rec["instruction_like"] = count(INSTRUCTION_PATTERNS, text)
        if rec["secret_like"]:
            rec["secret_like_lines"] = lines_of(SECRET_PATTERNS, text, kind)
        if category in REVIEW_CATEGORIES or area == "content":
            normalized_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            rec.update(compare_with_templates(area, rel, normalized_hash, text, templates))
            rec["placeholders"] = len(PLACEHOLDER_MARKERS.findall(text))
        if category == "memory":
            rec["entries"] = memory_entries(text)
        if category == "skill" and name == "skill.md":
            fields = frontmatter(text)
            rec["skill"] = rel.split("/")[1] if rel.count("/") >= 2 else rel
            rec["frontmatter_keys"] = sorted(k for k in fields if SAFE_LABEL.match(k))
        if category == "schedule" and name.endswith(".json"):
            rec["cron"] = cron_jobs(text, template_jobs, template_skills)
            if rel == "cron/jobs.json":
                rec["template"] = "compared job by job"
        if category == "configuration" and name.endswith((".yaml", ".yml")):
            rec["config"] = yaml_keys(text)
        if area == "content" and rel == "jarvis-content.json":
            try:
                rec["content_version"] = label(json.loads(text).get("version"))
            except (ValueError, AttributeError):
                pass
        records.append(rec)
    return records, skipped


def snapshot(roots):
    state = {}
    for area, root in roots.items():
        for rel, _, st in walk(root)[0]:
            state[(area, rel)] = (st.st_size, st.st_mtime_ns)
    return state


def compare_snapshots(before, after, records):
    categories = {(r["area"], r["path"]): r["category"] for r in records}
    changed = {"modified": {}, "added": 0, "removed": 0}
    for key, value in before.items():
        if key not in after:
            changed["removed"] += 1
        elif after[key] != value:
            cat = categories.get(key, "other")
            changed["modified"][cat] = changed["modified"].get(cat, 0) + 1
    changed["added"] = sum(1 for key in after if key not in before)
    return changed


# --- The database backup -------------------------------------------------------------------------------------------

# data_version is read by SQLite's full-text index itself.
READ_PRAGMAS = {"page_size", "page_count", "freelist_count", "user_version", "application_id", "encoding",
                "schema_version", "auto_vacuum", "journal_mode", "quick_check", "data_version"}
TABLE_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "foreign_key_list"}


def open_backup(path):
    """The backup, opened so that nothing can be written: read-only, immutable, and an authorizer allowing reads."""
    with open(path, "rb") as f:
        if f.read(16) != b"SQLite format 3\x00":
            raise Refused("%s is not an SQLite database" % os.path.basename(path))
    uri = "file:%s?mode=ro&immutable=1" % urllib.parse.quote(os.path.abspath(path))
    con = sqlite3.connect(uri, uri=True)
    # Damaged text must not stop the run, and Python's error for it would quote the whole value.
    con.text_factory = lambda value: value.decode("utf-8", errors="replace")
    con.execute("PRAGMA query_only = ON")

    def authorizer(action, arg1, arg2, _db, _trigger):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA:
            pragma = (arg1 or "").lower()
            if (pragma in READ_PRAGMAS and arg2 is None) or pragma in TABLE_PRAGMAS:
                return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    con.set_authorizer(authorizer)
    return con


def q(name):
    return '"%s"' % name.replace('"', '""')


def to_date(value):
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return iso(seconds)[:10]
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return None


def month_expr(col):
    c = q(col)
    return ("CASE WHEN typeof(%s) IN ('integer','real') THEN strftime('%%Y-%%m', CASE WHEN %s > 100000000000 "
            "THEN %s / 1000.0 ELSE %s END, 'unixepoch') ELSE substr(%s, 1, 7) END" % (c, c, c, c, c))


def distribution(values):
    if not values:
        return {}
    values = sorted(values)
    return {"min": values[0], "median": statistics.median(values), "p90": values[int(0.9 * (len(values) - 1))],
            "max": values[-1]}


def analyze_database(path, sample, full, check, servers=frozenset(TEMPLATE_MCP_SERVERS)):
    st = os.stat(path)
    progress("Reading the database backup (%s)..." % size(st.st_size))
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
    report = {"file": os.path.basename(path), "bytes": st.st_size, "sha256": digest.hexdigest(),
              "sqlite_library": sqlite3.sqlite_version,
              "files_next_to_it": [s for s in ("-wal", "-shm", "-journal") if os.path.exists(path + s)]}
    if report["files_next_to_it"]:
        report["warning"] = ("a -wal or -journal file sits next to the backup: the backup may not hold every "
                             "change; check how it was made")
    con = open_backup(path)
    try:
        report["pragmas"] = {p: con.execute("PRAGMA %s" % p).fetchone()[0]
                             for p in ("page_size", "page_count", "freelist_count", "user_version", "application_id",
                                       "encoding", "schema_version", "auto_vacuum")}
        if check:
            progress("Checking the database backup (quick_check)...")
            rows = con.execute("PRAGMA quick_check").fetchall()
            report["quick_check"] = "ok" if rows == [("ok",)] else "%d problem(s)" % len(rows)
        schema = con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        virtual = {name for typ, name, _, sql in schema
                   if typ == "table" and (sql or "").upper().startswith("CREATE VIRTUAL TABLE")}
        shadow = {name for typ, name, _, _ in schema if typ == "table"
                  and any(name.startswith(v + "_") for v in virtual)}
        tables = {}
        for typ, name, _, _ in schema:
            if typ != "table" or name.startswith("sqlite_"):
                continue
            progress("Counting table %s..." % label(name, "(a table)"))
            tables[name] = table_report(con, name, virtual=name in virtual, shadow=name in shadow, servers=servers,
                                        extras=message_extras if name == "messages" else None)
        report["tables"] = tables
        report["indexes"] = sorted(name for typ, name, _, _ in schema if typ == "index")
        report["triggers"] = sorted(name for typ, name, _, _ in schema if typ == "trigger")
        report["views"] = sorted(name for typ, name, _, _ in schema if typ == "view")
        report["full_text_tables"] = sorted(virtual)
        if "sessions" in tables:
            report["sessions"] = sessions_report(con, tables["sessions"]["columns"])
        if "messages" in tables:
            report["messages"] = messages_report(con, tables["messages"],
                                                 "sessions" in tables and "id" in tables["sessions"]["columns"])
        report["content_scan"] = {}
        for table in ("messages", "sessions"):
            if table in tables and not tables[table].get("error"):
                report["content_scan"][table] = scan_table(con, table, tables[table], sample, full)
        reasoning = {t: [c for c in info.get("columns", {}) if REASONING_COLUMN.search(c)]
                     for t, info in tables.items()}
        report["reasoning_columns"] = {t: cols for t, cols in reasoning.items() if cols}
    finally:
        con.close()
    return report


def progress(message):
    """A line on the console while a long step runs (counts only, never content)."""
    print(message, file=sys.stderr, flush=True)


# Identifier columns whose number of distinct values is worth a (cheap, indexed) count.
DISTINCT_COLUMNS = {"session_id", "user_id", "parent_session_id", "chat_id"}


def table_report(con, name, virtual, shadow, servers=frozenset(TEMPLATE_MCP_SERVERS), extras=None):
    """Structure and counts of one table, in as few passes as possible: the backup is read through Docker's file
    sharing, where every pass over the message text is slow. extras gives names and SQL expressions (for the table's
    columns) summed in the first pass."""
    out = {"kind": "full-text index" if virtual else ("full-text index storage" if shadow else "table")}
    try:
        info = con.execute("PRAGMA table_info(%s)" % q(name)).fetchall()
    except sqlite3.Error as exc:
        out["error"] = "could not be opened (%s)" % exc.__class__.__name__
        out["columns"] = {}
        return out
    out["columns"] = {row[1]: (row[2] or "").upper() for row in info}
    if virtual:
        # Counting a full-text index reads every message again; its document-size table has one row per document.
        try:
            out["rows"] = con.execute("SELECT count(*) FROM %s" % q(name + "_docsize")).fetchone()[0]
        except sqlite3.Error:
            out["rows"] = "not counted"
        return out
    try:
        if shadow or not info:
            out["rows"] = con.execute("SELECT count(*) FROM %s" % q(name)).fetchone()[0]
            return out
        parts, plan = ["count(*)"], [(None, "rows")]
        for column, ctype in out["columns"].items():
            texty = ctype == "" or any(t in ctype for t in ("TEXT", "CHAR", "CLOB", "JSON"))
            parts.append("count(%s)" % q(column))
            plan.append((column, "non_null"))
            if texty:
                parts += ["sum(length(%s))" % q(column), "max(length(%s))" % q(column)]
                plan += [(column, "total_characters"), (column, "longest")]
        for key, expr in (extras(out["columns"]) if extras else {}).items():
            parts.append("total(%s)" % expr)
            plan.append(("(extra)", key))
        values = con.execute("SELECT %s FROM %s" % (", ".join(parts), q(name))).fetchone()
    except sqlite3.Error as exc:
        out["error"] = "could not be read (%s)" % exc.__class__.__name__
        return out
    stats = {}
    for (column, key), value in zip(plan, values, strict=True):
        if column is None:
            out["rows"] = value
        elif column == "(extra)":
            out.setdefault("extra", {})[key] = int(value or 0)
        else:
            stats.setdefault(column, {})[key] = value or 0
    for column in stats:
        if REASONING_COLUMN.search(column):
            stats[column]["reasoning"] = "counted only; never read or exported"
    out["column_stats"] = stats
    out["values"] = {}
    enums = [c for c in out["columns"] if c.lower() in ENUM_COLUMNS]
    times = [c for c in out["columns"] if c.lower() in TIME_COLUMNS]
    if enums or times:
        # One grouped pass for every value list and every month list of the table.
        select = [q(c) for c in enums] + [month_expr(c) for c in times]
        aggregates = ["count(*)"] + ["min(%s)" % q(c) for c in times] + ["max(%s)" % q(c) for c in times]
        rows = con.execute("SELECT %s, %s FROM %s GROUP BY %s" % (
            ", ".join(select), ", ".join(aggregates), q(name),
            ", ".join(str(i + 1) for i in range(len(select))))).fetchall()
        width = len(select)
        for i, column in enumerate(enums):
            raw = {}
            for row in rows:
                raw[row[i]] = raw.get(row[i], 0) + row[width]
            merged = {}
            for value, n in sorted(raw.items(), key=lambda kv: -kv[1])[:50]:
                key = tool_label(value, servers) if column.lower() == "tool_name" else label(value)
                merged[key] = merged.get(key, 0) + n
            out["values"][column] = merged
            out["values"][column + " (distinct)"] = sum(1 for v in raw if v is not None)
        for j, column in enumerate(times):
            months, low, high = {}, None, None
            for row in rows:
                month = row[len(enums) + j]
                if month is None:
                    continue
                month = month if isinstance(month, str) and re.match(r"^\d{4}-\d{2}$", month) else "(other)"
                months[month] = months.get(month, 0) + row[width]
                # Compared as dates: a column can mix numbers (epoch seconds) and text.
                for edge in (to_date(row[width + 1 + j]), to_date(row[width + 1 + len(times) + j])):
                    if edge is not None:
                        low = edge if low is None or edge < low else low
                        high = edge if high is None or edge > high else high
            out.setdefault("time", {})[column] = {"first": low, "last": high, "by_month": dict(sorted(months.items()))}
    for column in out["columns"]:
        if column.lower() in DISTINCT_COLUMNS:
            out["values"][column + " (distinct)"] = one(con, "SELECT count(DISTINCT %s) FROM %s" % (q(column), q(name)))
    return out


def one(con, sql):
    try:
        return con.execute(sql).fetchone()[0]
    except sqlite3.Error as exc:
        return "not available (%s)" % exc.__class__.__name__


def sessions_report(con, columns):
    out = {"total": one(con, "SELECT count(*) FROM sessions")}
    if "archived" in columns:
        out["archived"] = one(con, "SELECT count(*) FROM sessions WHERE archived = 1")
    if "parent_session_id" in columns:
        out["with_parent_session"] = one(con, "SELECT count(*) FROM sessions WHERE parent_session_id IS NOT NULL "
                                              "AND parent_session_id != ''")
        if "end_reason" in columns:
            for reason, key in (("compression", "continuations_after_compression"), ("branched", "branches")):
                out[key] = one(con, "SELECT count(*) FROM sessions s JOIN sessions p ON p.id = s.parent_session_id "
                                    "WHERE p.end_reason = '%s'" % reason)
    if "model_config" in columns:
        for key, field in (("delegated", "_delegate_from"), ("branched_from_model_config", "_branched_from")):
            out[key] = one(con, "SELECT count(*) FROM sessions WHERE json_valid(model_config) "
                                "AND json_extract(model_config, '$.%s') IS NOT NULL" % field)
    if "id" in columns:
        jobs, cron = {}, 0
        for (session_id,) in con.execute("SELECT id FROM sessions WHERE id GLOB 'cron_*'"):
            m = CRON_SESSION.match(str(session_id))
            if m:
                cron += 1
                job = m.group(1) if SAFE_JOB_ID.match(m.group(1)) else "(other)"
                jobs[job] = jobs.get(job, 0) + 1
        out["scheduled_job_sessions"] = cron
        out["scheduled_job_sessions_by_job_id"] = dict(sorted(jobs.items(), key=lambda kv: -kv[1])[:50])
        out["other_sessions"] = (out["total"] - cron) if isinstance(out["total"], int) else None
    for column in ("message_count", "tool_call_count", "estimated_cost_usd", "actual_cost_usd", "input_tokens",
                   "output_tokens"):
        if column in columns:
            out["sum_" + column] = one(con, "SELECT total(%s) FROM sessions" % q(column))
    return out


def message_extras(columns):
    """Counts of the messages table, summed in its statistics pass instead of one pass each."""
    extras = {}
    if "active" in columns:
        # Hermes: compaction archives the original turns (active=0, compacted=1) and adds the summary as live rows;
        # rewound or edited messages and superseded copies are active=0, compacted=0 and hidden.
        extras["live"] = "active = 1"
        if "compacted" in columns:
            extras["archived_by_compaction"] = "active = 0 AND compacted = 1"
            extras["rewound_or_superseded"] = "active = 0 AND (compacted = 0 OR compacted IS NULL)"
    if "tool_calls" in columns:
        extras["with_tool_calls"] = "tool_calls IS NOT NULL AND trim(tool_calls) NOT IN ('', '[]', 'null')"
    if "role" in columns:
        extras["tool_results"] = "role = 'tool'"
        conversational = ["role IN ('user', 'assistant')", "content IS NOT NULL", "trim(content) != ''"]
        if "tool_calls" in columns:
            conversational.append("(tool_calls IS NULL OR trim(tool_calls) IN ('', '[]', 'null'))")
        if "tool_name" in columns:
            conversational.append("(tool_name IS NULL OR tool_name = '')")
        extras["conversational"] = " AND ".join(conversational)
    if "content" in columns:
        extras["empty_content"] = "content IS NULL OR trim(content) = ''"
        extras["content_with_think_tags"] = "instr(content, '<think>') > 0"
    return {key: "CASE WHEN %s THEN 1 ELSE 0 END" % expr for key, expr in extras.items()}


def messages_report(con, info, has_sessions):
    columns = info["columns"]
    out = dict(info.get("extra", {}), total=info.get("rows"))
    if "active" in columns and "compacted" in columns:
        out["by_active_compacted"] = {"active=1": out.get("live", 0),
                                      "active=0 compacted=1": out.get("archived_by_compaction", 0),
                                      "active=0 compacted=0": out.get("rewound_or_superseded", 0)}
    if "session_id" in columns:
        sizes = [n for (n,) in con.execute("SELECT count(*) FROM messages GROUP BY session_id")]
        out["sessions_with_messages"] = len(sizes)
        out["messages_per_session"] = distribution(sizes)
        if has_sessions:
            out["without_a_session"] = one(con, "SELECT count(*) FROM messages m WHERE NOT EXISTS "
                                                "(SELECT 1 FROM sessions s WHERE s.id = m.session_id)")
            out["sessions_without_messages"] = one(con, "SELECT count(*) FROM sessions s WHERE NOT EXISTS "
                                                        "(SELECT 1 FROM messages m WHERE m.session_id = s.id)")
    return out


def scan_table(con, table, info, sample, full):
    """Pattern counts over the text of a sample of rows (or all of them): never the text itself."""
    columns = info.get("columns", {})
    total = info.get("rows", 0)
    scanned_columns = [c for c, t in columns.items()
                       if (t == "" or any(x in t for x in ("TEXT", "CHAR", "CLOB", "JSON")))
                       and not REASONING_COLUMN.search(c) and not ID_COLUMN.search(c) and c.lower() not in ENUM_COLUMNS
                       and c.lower() not in TIME_COLUMNS and not NON_CONTENT_COLUMN.search(c)]
    if not scanned_columns or not total:
        return {"rows_scanned": 0, "columns_scanned": scanned_columns}
    role = "role" if "role" in columns else "NULL"
    select = "SELECT %s, %s FROM %s" % (q(role) if role != "NULL" else role, ", ".join(q(c) for c in scanned_columns),
                                       q(table))
    step = 1 if full or total <= sample else max(1, total // sample)
    try:
        sql = select + ("" if step == 1 else " WHERE rowid %% %d = 0" % step) + ("" if full else " LIMIT %d" % sample)
        cursor = con.execute(sql)
    except sqlite3.OperationalError:
        cursor = con.execute(select + ("" if full else " LIMIT %d" % sample))
    out = {"mode": "all rows" if full or total <= sample else "sample", "columns_scanned": scanned_columns,
           "rows_scanned": 0,
           "rows_with_secret_like": 0, "secret_like": {}, "rows_with_personal_data": 0, "personal_data": {},
           "instruction_like_by_role": {}, "topics": {}, "duplicate_rows_in_scan": 0, "values_cut_for_scanning": 0}
    # Two rows are duplicates when their role and content agree, whatever their identifiers.
    duplicate_at = [i + 1 for i, c in enumerate(scanned_columns) if c.lower() in DUPLICATE_COLUMNS]
    expected = total if out["mode"] == "all rows" else min(sample, total)
    progress("Scanning %d %s rows for counts..." % (expected, label(table, "table")))
    seen = set()
    for row in cursor:
        out["rows_scanned"] += 1
        if out["rows_scanned"] % 10000 == 0:
            progress("  %d of %d rows scanned" % (out["rows_scanned"], expected))
        role_value = label(row[0])
        key = hashlib.sha256(repr((role_value,) + tuple(row[i] for i in duplicate_at)).encode(
            "utf-8", errors="replace")).digest()
        if duplicate_at and any(row[i] for i in duplicate_at):
            if key in seen:
                out["duplicate_rows_in_scan"] += 1
            seen.add(key)
        texts = []
        for value in row[1:]:
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            if isinstance(value, str) and value:
                if len(value) > VALUE_SCAN_LIMIT:
                    out["values_cut_for_scanning"] += 1
                    value = value[:VALUE_SCAN_LIMIT]
                texts.append(value)
        text = "\n".join(texts)
        secrets = count(SECRET_PATTERNS, text)
        personal = count(PERSONAL_PATTERNS, text)
        for found, total_key, rows_key in ((secrets, "secret_like", "rows_with_secret_like"),
                                           (personal, "personal_data", "rows_with_personal_data")):
            if found:
                out[rows_key] += 1
                for k, n in found.items():
                    out[total_key][k] = out[total_key].get(k, 0) + n
        for k, n in count(INSTRUCTION_PATTERNS, text).items():
            by_role = out["instruction_like_by_role"].setdefault(role_value, {})
            by_role[k] = by_role.get(k, 0) + n
        for topic, rx in TOPIC_PATTERNS.items():
            if rx.search(text):
                out["topics"][topic] = out["topics"].get(topic, 0) + 1
    return out


# --- Reports -------------------------------------------------------------------------------------------------------

def add(totals, key, found):
    for k, n in (found or {}).items():
        totals.setdefault(key, {})
        totals[key][k] = totals[key].get(k, 0) + n


def summarize_area(records, area):
    rows = [r for r in records if r["area"] == area]
    by_category, findings, comparison = {}, {}, {}
    for r in rows:
        c = by_category.setdefault(r["category"], {"files": 0, "bytes": 0})
        c["files"] += 1
        c["bytes"] += r["size"]
        for key in ("secret_like", "personal_data", "instruction_like"):
            if r.get(key):
                add(findings.setdefault(r["category"], {}), key, r[key])
        if "template" in r:
            comparison[r["template"]] = comparison.get(r["template"], 0) + 1
    return {"files": len(rows), "bytes": sum(r["size"] for r in rows), "by_category": by_category,
            "findings_by_category": findings, "template_comparison": comparison}


SHARED_SECRET_NAMES = {".env", ".envrc", "auth.json", "local-api.json", "credentials.json", ".credentials.json",
                       "token.json", "tokens.json", "creds.json", ".netrc", "_netrc", ".git-credentials", ".pgpass"}
KNOWN_TOPS = ("knowledge", "skills", "scripts", "plugins", "hooks", "cron", "memories", "workspace", "platforms",
              "whatsapp")
# Words setting names are made of in JARVIS's templates and Hermes; a .env name made of anything else (a client's
# name, say) is counted in the summary instead of shown.
COMMON_ENV_PARTS = {"API", "KEY", "KEYS", "TOKEN", "SECRET", "PASSWORD", "USER", "USERS", "ID", "URL", "HOST", "PORT",
                    "ENABLED", "ALLOWED", "HOME", "CHANNEL", "ACCOUNT", "PROJECT", "ACCESS", "APP", "BOT", "OWNER",
                    "MEMBER", "ADDRESS", "MODEL", "DEFAULT", "BASE", "PROVIDER", "DEBUG", "LOG", "LEVEL", "MODE", "DIR",
                    "PATH", "TIMEOUT", "REGION", "ORG", "ORGANIZATION", "NAME", "EMAIL", "PHONE", "NUMBER", "CLIENT",
                    "WHATSAPP", "TELEGRAM", "DISCORD", "SIGNAL", "GITHUB", "GITLAB", "CLAUDE", "CODEX", "GOOGLE",
                    "GEMINI", "NOUS", "GROQ", "MISTRAL", "DEEPSEEK", "XAI", "FAL", "ELEVENLABS", "FIRECRAWL", "TAVILY",
                    "EXA", "BROWSERBASE", "HONCHO", "MEM0", "NOTION", "LINEAR", "TWILIO", "SENDGRID", "MAILGUN",
                    "WORDPRESS", "WP", "CPANEL", "CWP", "SSH", "FTP", "DB", "DATABASE", "REDIS", "POSTGRES", "AWS",
                    "AZURE", "GCP", "CLOUDFLARE", "HUD", "STT", "TTS", "VOICE", "WEBHOOK", "SIGNING", "VERIFY"}


# Setting words of Hermes's config.yaml beyond those in JARVIS's own templates (templates.json adds those).
COMMON_CONFIG_WORDS = {
    "model", "default", "provider", "base_url", "api_mode", "api_key_env", "approvals", "mode", "cron_mode", "agent",
    "max_turns", "toolsets", "disabled_toolsets", "terminal", "backend", "cwd", "timeout", "stt", "tts", "enabled",
    "voice", "memory", "memory_enabled", "user_profile_enabled", "provider_config", "mcp_servers", "url", "command",
    "args", "env", "headers", "quick_commands", "type", "platforms", "whatsapp", "slack", "telegram", "discord",
    "signal", "email", "gateway", "display", "compression", "threshold", "delegation", "browser", "security",
    "redact_secrets", "skills", "plugins", "hooks", "cron", "logging", "level", "auxiliary", "vision", "web",
    "checkpoints", "context", "personality", "streaming", "tools", "allowlist", "denylist", "fallback_model",
    "smart_model_routing", "session_reset", "chats", "channels", "home_channel", "require_mention", "max_tokens",
    "temperature", "reasoning_effort", "show_reasoning", "honcho", "mem0", "external",
}


def shared_config(config, templates, counters):
    """config.yaml for the shareable summary: key words of Hermes and the templates only, other names as "(key)" and
    MCP servers other than the templates' own numbered (an owner may name one after a client)."""
    words = {w.lower() for w in templates.get("config_key_words", [])} | COMMON_CONFIG_WORDS
    servers = set(templates.get("mcp_servers", [])) | TEMPLATE_MCP_SERVERS
    numbers = {}

    def server(name):
        if name in servers:
            return name
        if name not in numbers:
            counters["mcp"] = counters.get("mcp", 0) + 1
            numbers[name] = "mcp server %d" % counters["mcp"]
        return numbers[name]
    keys = []
    for key in config["keys"]:
        parts = key.split(".")
        shown = [server(part) if i == 1 and parts[0] == "mcp_servers" else (part if part.lower() in words else "(key)")
                 for i, part in enumerate(parts)]
        joined = ".".join(shown)
        if joined not in keys:
            keys.append(joined)
    return {"keys": keys, "values": config["values"], "mcp_servers": [server(n) for n in config["mcp_servers"]]}


# Hermes's built-in tools (fixed names); a plugin's or MCP server's tools are named by whoever installed them.
HERMES_TOOLS = {"terminal", "process", "read_file", "write_file", "patch", "search_files", "web_search", "web_extract",
                "vision_analyze", "image_generate", "skills_list", "skill_view", "skill_manage", "memory",
                "session_search", "todo", "clarify", "execute_code", "delegate_task", "cronjob", "send_message",
                "text_to_speech", "mixture_of_agents", "web_fetch"}


def tool_label(value, servers):
    """A tool name for the summary: Hermes's own tools and those of JARVIS's MCP servers by name, others grouped."""
    if value is None:
        return "(none)"
    if not isinstance(value, str):
        return "(other tool)"
    if value.startswith("mcp_"):
        known = any(value.startswith("mcp_%s_" % s.replace("-", "_")) for s in servers)
        return label(value) if known else "(tool of another mcp server)"
    if value in HERMES_TOOLS or value.startswith("browser_"):
        return label(value)
    return "(other tool)"


def env_name_shared(name, templates):
    parts = set(templates.get("env_name_parts", [])) | COMMON_ENV_PARTS
    return all(part in parts for part in name.upper().split("_") if part)


def summary_label(r, counters):
    """A path that is safe to share: a template's own path, else a numbered placeholder."""
    if r.get("template") in ("identical", "modified") and label(r["path"]) == r["path"]:
        return r["path"]
    top = r["path"].split("/")[0] if "/" in r["path"] else ""
    if r["path"].lower() in ("soul.md", "agents.md", "operations.md", ".hermes.md", "config.yaml",
                             "memories/memory.md", "memories/user.md", "cron/jobs.json"):
        return r["path"]
    counters[r["category"]] = counters.get(r["category"], 0) + 1
    return "custom %s file %d%s" % (r["category"], counters[r["category"]], " in %s/" % top if top in (
        "knowledge", "skills", "scripts", "plugins", "hooks", "cron", "memories") else "")


def secret_label(r, counters):
    """A credential file's name only when it is one of the standard names at the top of the folder."""
    path = r["path"]
    name = path.rsplit("/", 1)[-1]
    if "/" not in path and name.lower() in SHARED_SECRET_NAMES:
        return path
    counters["secret"] = counters.get("secret", 0) + 1
    top = path.split("/")[0] if "/" in path else ""
    lower = name.lower()
    kind = name if lower in SHARED_SECRET_NAMES else (".env file" if lower.startswith(".env") or lower.endswith(".env")
                                                      else "")
    return "secret file %d%s%s" % (counters["secret"], " (%s)" % kind if kind else "",
                                   " in %s/" % top if top in KNOWN_TOPS else "")


def build_summary(records, skipped, database, changed, args_info, templates):
    profile = [r for r in records if r["area"] == "profile"]
    counters, review = {}, []
    for r in profile:
        if r["category"] not in REVIEW_CATEGORIES:
            continue
        item = {"file": summary_label(r, counters), "category": r["category"], "bytes": r["size"]}
        for key in ("template", "template_source", "lines_added", "lines_removed", "placeholders", "entries",
                    "secret_like", "personal_data", "instruction_like", "frontmatter_keys"):
            if r.get(key) not in (None, {}, []):
                item[key] = r[key]
        if r.get("template") == "not from a template":
            item.pop("frontmatter_keys", None)
        review.append(item)
    secrets, secret_counters, bridges = [], {}, {}
    for r in profile + [r for r in records if r["area"] == "content"]:
        if r["category"] != "secret":
            continue
        item = {"file": secret_label(r, secret_counters), "area": r["area"]}
        if "variables" in r:
            shown = [v for v in r["variables"] if env_name_shared(v, templates)]
            item["variables"] = shown
            item["other_variables"] = len(r["variables"]) - len(shown)
            item["empty_values"] = r.get("empty_values", 0)
        if "shared_keys" in r:
            item["keys"] = r["shared_keys"]
        secrets.append(item)
        parts = r["path"].lower().split("/")
        if any(p in ("session", "sessions") for p in parts[:-1]):
            for bridge in ("whatsapp", "telegram", "signal"):
                if bridge in parts:
                    bridges[bridge] = bridges.get(bridge, 0) + 1
    skills = {"total": 0, "identical_to_template": [], "modified_template": [], "other_skills": 0}
    template_skills = set(templates.get("skill_names", []))
    for r in profile:
        if r["category"] == "skill" and r.get("skill") and r["path"].lower().endswith("skill.md"):
            skills["total"] += 1
            if r.get("template") == "identical" and r["skill"] in template_skills:
                skills["identical_to_template"].append(r["skill"])
            elif r.get("template") == "modified" and r["skill"] in template_skills:
                skills["modified_template"].append(r["skill"])
            else:
                skills["other_skills"] += 1
    schedules = [dict((k, v) for k, v in job.items() if k != "name_in_detail")
                 for r in profile if r.get("cron") for job in r["cron"].get("jobs", [])]
    config = next((shared_config(r["config"], templates, {}) for r in profile
                   if r.get("config") and r["path"].lower() == "config.yaml"), None)
    workspace = {}
    for r in profile:
        if r["category"] == "workspace":
            sub = r["path"].split("/")[1] if r["path"].count("/") >= 2 else "(top level)"
            sub = sub if sub in WORKSPACE_KNOWN or sub == "(top level)" else "other folders"
            w = workspace.setdefault(sub, {"files": 0, "bytes": 0})
            w["files"] += 1
            w["bytes"] += r["size"]
    content_marker = next((r.get("content_version") for r in records if r.get("content_version")), None)
    return {
        "kit_version": KIT_VERSION,
        "generated_at": now(),
        "run": args_info,
        "read_only_check": {
            "source_files_changed_during_the_run": changed,
            "note": ("The kit opens nothing for writing; any change listed here was made by JARVIS itself "
                     "(it may be running) or by another program."),
        },
        "profile": summarize_area(records, "profile"),
        "content": dict(summarize_area(records, "content"), jarvis_content_version=content_marker),
        "skipped_links_and_special_files": len(skipped),
        "files_to_review": review,
        "secret_files": secrets,
        "linked_device_sessions": {bridge: "present (%d files; never copied)" % n for bridge, n in bridges.items()},
        "skills": skills,
        "scheduled_jobs": schedules,
        "config_yaml": config,
        "workspace": workspace,
        "database_backup": database,
    }


def md_table(rows, headers):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in rows:
        out.append("| " + " | ".join(str(c).replace("|", "/") for c in row) + " |")
    return "\n".join(out)


def size(n):
    return "%.1f MB" % (n / 1e6) if n >= 1e6 else "%d KB" % max(1, round(n / 1e3)) if n else "0 KB"


def fmt(found):
    return ", ".join("%s %s" % (k, n) for k, n in sorted(found.items())) if found else ""


def render_summary(s):
    lines = ["# JARVIS discovery summary", "",
             "Kit %s, %s. Counts and structure only: no message text, no file contents, no secret values, "
             "no reasoning. File names that are not JARVIS template names are replaced by numbers; "
             "discovery-detail.json (keep it on your PC) has them." % (s["kit_version"], s["generated_at"]), ""]
    changed = s["read_only_check"]["source_files_changed_during_the_run"]
    lines += ["## Read-only check", "",
              "Files changed while the kit ran: %d modified (%s), %d added, %d removed. %s" % (
                  sum(changed["modified"].values()), fmt(changed["modified"]) or "none", changed["added"],
                  changed["removed"], s["read_only_check"]["note"]), ""]
    for area in ("profile", "content"):
        a = s[area]
        lines += ["## %s folder: %d files, %s" % (area.capitalize(), a["files"], size(a["bytes"])), ""]
        lines.append(md_table([(c, v["files"], size(v["bytes"])) for c, v in sorted(a["by_category"].items())],
                              ["Category", "Files", "Size"]))
        if a["template_comparison"]:
            lines += ["", "Compared with the JARVIS templates: " + fmt(a["template_comparison"]) + "."]
        if a["findings_by_category"]:
            lines += ["", md_table([(c, fmt(f.get("secret_like", {})), fmt(f.get("personal_data", {})),
                                     fmt(f.get("instruction_like", {})))
                                    for c, f in sorted(a["findings_by_category"].items())],
                                   ["Category", "Secret-like strings", "Personal data", "Instruction-like text"])]
        lines.append("")
    if s.get("content", {}).get("jarvis_content_version"):
        lines += ["jarvis-content version: %s" % s["content"]["jarvis_content_version"], ""]
    lines += ["## Instructions, knowledge, skills and automation", ""]
    lines.append(md_table([(r["file"], r["category"], r.get("template", ""), r.get("template_source", ""),
                            ("+%s/-%s" % (r["lines_added"], r["lines_removed"])) if "lines_added" in r else "",
                            r.get("placeholders", ""), fmt(r.get("secret_like", {})), fmt(r.get("personal_data", {})),
                            fmt(r.get("instruction_like", {}))) for r in s["files_to_review"]],
                          ["File", "Category", "Template", "Template source", "Lines vs template", "Placeholders",
                           "Secret-like", "Personal data", "Instruction-like"]))
    sk = s["skills"]
    lines += ["", "Skills: %d. Identical to a template: %s. Changed from a template: %s. Other skills (names in the "
              "detail report): %d." % (sk["total"], ", ".join(sk["identical_to_template"]) or "none",
                                       ", ".join(sk["modified_template"]) or "none", sk["other_skills"]), ""]
    lines += ["## Scheduled jobs", ""]
    if s["scheduled_jobs"]:
        lines.append(md_table([(j["name"], j["schedule"], j["enabled"], j["deliver"], ", ".join(j["skills"]),
                                j["prompt_characters"], j["prompt_differs_from_template"])
                               for j in s["scheduled_jobs"]],
                              ["Job", "Schedule", "Enabled", "Delivers to", "Skills", "Prompt characters",
                               "Prompt changed from template"]))
    else:
        lines.append("No scheduled-job file found.")
    lines += ["", "## Secret files (names of settings only, never values)", ""]
    for item in s["secret_files"]:
        detail = list(item.get("variables") or item.get("keys") or [])
        if item.get("other_variables"):
            detail.append("%d other setting name(s)" % item["other_variables"])
        lines.append("- %s (%s): %s" % (item["file"], item["area"], ", ".join(detail) if detail else "not read"))
    for bridge, state in sorted(s.get("linked_device_sessions", {}).items()):
        lines.append("- %s linked-device session: %s. It gives full access to the account: unlink it in the app "
                     "when JARVIS is retired, and pair the Command Center separately." % (bridge, state))
    if s["config_yaml"]:
        c = s["config_yaml"]
        lines += ["", "config.yaml settings: " + ", ".join("%s=%s" % kv for kv in sorted(c["values"].items())),
                  "MCP servers (names): " + (", ".join(c["mcp_servers"]) or "none")]
    if s["workspace"]:
        lines += ["", "Workspace: " + ", ".join("%s %d file(s)" % (k, v["files"])
                                                for k, v in sorted(s["workspace"].items()))]
    db = s["database_backup"]
    lines += ["", "## Database backup", ""]
    if not db:
        lines.append("Not analysed.")
    else:
        lines.append("%s, %s, sha256 %s, read with SQLite %s." % (db["file"], size(db["bytes"]), db["sha256"],
                                                                db["sqlite_library"]))
        if db.get("warning"):
            lines.append("**Warning:** " + db["warning"])
        if db.get("quick_check"):
            lines.append("quick_check: %s" % db["quick_check"])
        lines += ["", md_table([(t, i["kind"], i.get("rows", i.get("error")), len(i.get("columns", {})))
                                for t, i in sorted(db["tables"].items())], ["Table", "Kind", "Rows", "Columns"])]
        for key in ("sessions", "messages"):
            if db.get(key):
                lines += ["", "**%s**" % key.capitalize(), ""]
                lines += ["- %s: %s" % (k.replace("_", " "), json.dumps(v, sort_keys=True) if isinstance(v, dict)
                                        else v) for k, v in sorted(db[key].items())]
        for table, info in sorted(db["tables"].items()):
            if info.get("values") or info.get("time"):
                lines += ["", "**%s values:** %s" % (table, json.dumps(info.get("values", {}), sort_keys=True))]
                for col, t in sorted(info.get("time", {}).items()):
                    lines.append("%s.%s from %s to %s; by month: %s" % (table, col, t["first"], t["last"],
                                                                       fmt(t["by_month"])))
        if db.get("reasoning_columns"):
            lines += ["", "Reasoning columns (counted, never read): " + json.dumps(db["reasoning_columns"])]
        for table, scan in sorted(db.get("content_scan", {}).items()):
            lines += ["", "**Pattern counts in %s (%s, %d rows):** secret-like in %d rows (%s); personal data in %d "
                      "rows (%s); instruction-like by role: %s; topics: %s; duplicate rows: %d." % (
                          table, scan.get("mode", ""), scan["rows_scanned"], scan.get("rows_with_secret_like", 0),
                          fmt(scan.get("secret_like", {})), scan.get("rows_with_personal_data", 0),
                          fmt(scan.get("personal_data", {})), json.dumps(scan.get("instruction_like_by_role", {})),
                          fmt(scan.get("topics", {})), scan.get("duplicate_rows_in_scan", 0))]
    return "\n".join(lines) + "\n"


# --- Commands ------------------------------------------------------------------------------------------------------

def inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def check_out_folder(out, sources):
    for source in sources:
        if source and inside(out, source):
            raise Refused("the output folder is inside a JARVIS folder; choose a folder outside it")
    if not os.path.isdir(out):
        raise Refused("the output folder %s does not exist" % out)
    if os.listdir(out):
        raise Refused("the output folder is not empty; nothing is overwritten")


def write_new(path, data):
    with open(path, "x", encoding="utf-8") as f:
        f.write(data)


def cmd_discover(args):
    for folder in (args.profile, args.content):
        if not os.path.isdir(folder):
            raise Refused("%s is not a folder" % folder)
    if args.db:
        if not os.path.isfile(args.db):
            raise Refused("the database backup %s does not exist" % args.db)
        if inside(args.db, args.profile) or inside(args.db, args.content):
            raise Refused("use the verified backup of the database, not the live one inside the JARVIS folders")
        with open(args.db, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                raise Refused("%s is not an SQLite database" % os.path.basename(args.db))
    check_out_folder(args.out, (args.profile, args.content))
    templates = load_templates(args.templates)
    roots = {"profile": args.profile, "content": args.content}
    before = snapshot(roots)
    records, skipped = [], []
    for area, root in roots.items():
        found, skip = inventory(area, root, templates)
        records += found
        skipped += [dict(s, area=area) for s in skip]
    database = analyze_database(args.db, args.sample, args.full, args.check) if args.db else None
    changed = compare_snapshots(before, snapshot(roots), records)
    run = {"sample": "all rows" if args.full else args.sample, "quick_check": args.check,
           "templates": templates.get("generated_from", {})}
    summary = build_summary(records, skipped, database, changed, run, templates)
    write_new(os.path.join(args.out, "discovery-summary.json"), json.dumps(summary, indent=2, sort_keys=True))
    write_new(os.path.join(args.out, "discovery-summary.md"), render_summary(summary))
    detail = {"kit_version": KIT_VERSION, "generated_at": summary["generated_at"],
              "warning": "Keep this file on your PC: file names can name customers. It holds no contents and no "
                         "secret values.",
              "files": records, "skipped": skipped}
    write_new(os.path.join(args.out, "discovery-detail.json"), json.dumps(detail, indent=2, sort_keys=True))
    print("Discovery done: %d files in the profile, %d in the content folder%s." % (
        summary["profile"]["files"], summary["content"]["files"],
        "; database %s analysed" % database["file"] if database else ""))
    return 0


def read_passphrase():
    try:
        line = sys.stdin.buffer.readline().decode("ascii").rstrip("\r\n")
    except UnicodeDecodeError:
        line = ""
    if len(line) < MIN_PASSPHRASE or not re.match(r"^[\x20-\x7e]+$", line):
        raise Refused("the passphrase must be at least %d printable ASCII characters" % MIN_PASSPHRASE)
    return line.encode("ascii")


def derive_key(passphrase, salt, n, r, p):
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase)


class EncryptingWriter:
    """Chunked AES-256-GCM: each chunk's nonce and associated data carry its number and whether it is the last,
    so chunks cannot be reordered, dropped or cut off without the archive failing to decrypt."""

    def __init__(self, f, key, header, prefix):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.f, self.aead, self.header, self.prefix = f, AESGCM(key), header, prefix
        self.buffer, self.index = bytearray(), 0
        f.write(header)

    def write(self, data):
        self.buffer += data
        while len(self.buffer) > CHUNK:
            self._emit(bytes(self.buffer[:CHUNK]), False)
            del self.buffer[:CHUNK]
        return len(data)

    def _emit(self, data, final):
        flag = b"\x01" if final else b"\x00"
        nonce = self.prefix + self.index.to_bytes(4, "big")
        sealed = self.aead.encrypt(nonce, data, self.header + self.index.to_bytes(8, "big") + flag)
        self.f.write(len(sealed).to_bytes(4, "big") + flag + sealed)
        self.index += 1

    def finish(self):
        self._emit(bytes(self.buffer), True)
        self.buffer = bytearray()


class DecryptingReader(io.RawIOBase):
    def __init__(self, f, passphrase):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        header = f.read(len(MAGIC) + 16 + 12 + 8)
        if len(header) != len(MAGIC) + 36 or not header.startswith(MAGIC):
            raise KitError("not a JARVIS kit archive")
        salt = header[len(MAGIC):len(MAGIC) + 16]
        n, r, p = (int.from_bytes(header[len(MAGIC) + 16 + 4 * i:len(MAGIC) + 20 + 4 * i], "big") for i in range(3))
        if n > 2 ** 20 or r > 16 or p > 4:
            raise KitError("unexpected key-derivation settings")
        self.f, self.header, self.prefix = f, header, header[-8:]
        self.aead = AESGCM(derive_key(passphrase, salt, n, r, p))
        self.index, self.pending, self.done = 0, b"", False

    def readable(self):
        return True

    def _next(self):
        record = self.f.read(5)
        if len(record) < 5:
            raise KitError("the archive is cut off")
        length, flag = int.from_bytes(record[:4], "big"), record[4:5]
        if length > CHUNK + 16 or flag not in (b"\x00", b"\x01"):
            raise KitError("the archive is damaged")
        sealed = self.f.read(length)
        if len(sealed) != length:
            raise KitError("the archive is cut off")
        from cryptography.exceptions import InvalidTag
        try:
            data = self.aead.decrypt(self.prefix + self.index.to_bytes(4, "big"), sealed,
                                     self.header + self.index.to_bytes(8, "big") + flag)
        except InvalidTag:
            raise KitError("wrong passphrase, or the archive was changed") from None
        self.index += 1
        if flag == b"\x01":
            self.done = True
            if self.f.read(1):
                raise KitError("unexpected data after the end of the archive")
        return data

    def readinto(self, buffer):
        while not self.pending and not self.done:
            self.pending = self._next()
        n = min(len(buffer), len(self.pending))
        buffer[:n] = self.pending[:n]
        self.pending = self.pending[n:]
        return n


def backup_plan(area, root):
    """Which files go into the backup, and why each of the others does not."""
    files, skipped = walk(root)
    plan, left_out = [], {}
    for rel, full, st in files:
        category = classify(area, rel)
        reason = None
        if category in BACKUP_EXCLUDED:
            reason = category
        elif st.st_size > BACKUP_FILE_LIMIT:
            reason = "larger than %d MB" % (BACKUP_FILE_LIMIT // (1024 * 1024))
        if reason:
            left_out[reason] = left_out.get(reason, 0) + 1
        else:
            plan.append((rel, full, st, category))
    left_out["links and special files"] = len(skipped)
    return plan, {k: v for k, v in left_out.items() if v}


def backup_entry(area, rel, full, st, category, report):
    """The archive bytes of one file and its manifest entry, or None if it changed or vanished while being read.

    A file is taken only if it did not change while it was read (tried three times; JARVIS may be writing it), then
    scanned by its content (whatever its name or encoding) and redacted. A file that cannot be scanned (a PDF, an
    image) is counted as such in the report."""
    raw = None
    for _attempt in range(3):
        try:
            with open(full, "rb") as src:
                before = os.fstat(src.fileno())
                data = src.read(BACKUP_FILE_LIMIT + 1)
                after = os.fstat(src.fileno())
        except OSError:
            return None
        if len(data) > BACKUP_FILE_LIMIT:
            return None
        if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) and len(data) == after.st_size:
            raw, st = data, after
            break
    if raw is None:  # still being written: left out rather than stored half-written
        return None
    entry = {"path": "%s/%s" % (area, rel), "category": category, "modified": iso(st.st_mtime)}
    found = text_encoding(rel, raw)
    text = None
    if found is not None:
        encoding, bom = found
        errors = "surrogateescape" if encoding == "utf-8" else "strict"
        try:
            text = raw[len(bom):].decode(encoding, errors=errors)
        except UnicodeDecodeError:
            text = None
    if text is None:
        reason = ("document or media file" if os.path.splitext(rel.lower())[1] in BINARY_EXTS
                  else "binary or undecodable")
        entry["scanned"] = False
        report["included_not_scanned"][reason] = report["included_not_scanned"].get(reason, 0) + 1
    else:
        redacted, n = redact(text, file_kind(rel))
        if n:
            raw = bom + redacted.encode(encoding, errors=errors)
            entry["redactions"] = n
            report["redacted_files"] += 1
            report["redactions"] += n
    entry["size"] = len(raw)
    entry["sha256"] = hashlib.sha256(raw).hexdigest()
    return raw, entry


def cmd_backup(args):
    for folder in (args.profile, args.content):
        if not os.path.isdir(folder):
            raise Refused("%s is not a folder" % folder)
    check_out_folder(args.out, (args.profile, args.content))
    passphrase = read_passphrase()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = "jarvis-profile-%s.ccjb" % stamp
    salt, prefix = os.urandom(16), os.urandom(8)
    header = (MAGIC + salt + SCRYPT_N.to_bytes(4, "big") + SCRYPT_R.to_bytes(4, "big") + SCRYPT_P.to_bytes(4, "big")
              + prefix)
    key = derive_key(passphrase, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    manifest = {"kit_version": KIT_VERSION, "created_at": now(), "files": []}
    report = {"kit_version": KIT_VERSION, "created_at": manifest["created_at"], "archive": name, "included": {},
              "left_out": {}, "included_not_scanned": {}, "redacted_files": 0, "redactions": 0, "bytes_included": 0}
    path = os.path.join(args.out, name)
    partial = path + ".partial"  # renamed only once verified: a killed run cannot leave a finished-looking archive
    try:
        with open(partial, "xb") as f:
            writer = EncryptingWriter(f, key, header, prefix)
            with tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                for area, root in (("profile", args.profile), ("content", args.content)):
                    plan, left_out = backup_plan(area, root)
                    for reason, n in left_out.items():
                        report["left_out"]["%s: %s" % (area, reason)] = n
                    for rel, full, st, category in plan:
                        made = backup_entry(area, rel, full, st, category, report)
                        if made is None:
                            key_name = "%s: changing, removed or unreadable during the backup" % area
                            report["left_out"][key_name] = report["left_out"].get(key_name, 0) + 1
                            continue
                        raw, entry = made
                        info = tarfile.TarInfo(entry["path"])
                        mtime = datetime.datetime.strptime(entry["modified"], "%Y-%m-%dT%H:%M:%SZ")
                        info.size, info.mode = len(raw), 0o644
                        info.mtime = int(mtime.replace(tzinfo=datetime.timezone.utc).timestamp())
                        tar.addfile(info, io.BytesIO(raw))
                        manifest["files"].append(entry)
                        report["included"][category] = report["included"].get(category, 0) + 1
                        report["bytes_included"] += len(raw)
                data = json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8")
                info = tarfile.TarInfo("MANIFEST.json")
                info.size, info.mtime, info.mode = len(data), int(datetime.datetime.now().timestamp()), 0o644
                tar.addfile(info, io.BytesIO(data))
            writer.finish()
        with open(partial, "rb") as f:
            verified = verify_stream(f, passphrase, None)
        if verified["problems"]:
            raise KitError("the new archive did not verify (%s); it was deleted" % verified["problems"][0])
        os.replace(partial, path)
    except BaseException:
        # Never leave a partial or unverified archive behind.
        if os.path.exists(partial):
            os.remove(partial)
        raise
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(functools.partial(f.read, CHUNK), b""):
            digest.update(block)
    report["archive_sha256"] = digest.hexdigest()
    report["files_included"] = len(manifest["files"])
    write_new(path + ".sha256", "%s  %s\n" % (report["archive_sha256"], name))
    write_new(os.path.join(args.out, "backup-report.json"), json.dumps(report, indent=2, sort_keys=True))
    # The per-file list (paths can name customers): for the owner only, like discovery-detail.json.
    write_new(os.path.join(args.out, "backup-files.json"), json.dumps(
        {"warning": "Keep this file on your PC: file names can name customers. It lists every file in the backup, "
                    "which were redacted and which could not be scanned.", "files": manifest["files"]},
        indent=1, sort_keys=True))
    unscanned = sum(report["included_not_scanned"].values())
    print("Backup written and verified: %s (%d files, %d redactions%s)." % (
        name, report["files_included"], report["redactions"],
        ", %d not scannable (PDFs, images and the like; see backup-files.json)" % unscanned if unscanned else ""))
    return 0


def safe_member(name):
    parts = name.split("/")
    return (name and not name.startswith("/") and "\\" not in name and ".." not in parts and "" not in parts
            and not re.match(r"^[A-Za-z]:", name))


def verify_stream(f, passphrase, extract_to):
    reader = io.BufferedReader(DecryptingReader(f, passphrase), buffer_size=CHUNK)
    found, manifest, problems = {}, None, []
    with tarfile.open(fileobj=reader, mode="r|") as tar:
        for member in tar:
            if not member.isfile() or not safe_member(member.name):
                problems.append("unexpected entry in the archive")
                continue
            source = tar.extractfile(member)
            is_manifest = member.name == "MANIFEST.json"
            digest, chunks, target = hashlib.sha256(), [], None
            if extract_to and not is_manifest:
                out_path = os.path.join(extract_to, *member.name.split("/"))
                os.makedirs(os.path.dirname(out_path), exist_ok=True)
                target = open(out_path, "xb")
            try:
                for block in iter(functools.partial(source.read, CHUNK), b""):
                    digest.update(block)
                    if target:
                        target.write(block)
                    if is_manifest:
                        chunks.append(block)
            finally:
                if target:
                    target.close()
            if target:
                try:
                    os.utime(out_path, (member.mtime, member.mtime))
                except OSError:
                    pass  # not allowed on some shared folders (Docker Desktop on Windows): the time is a nicety
            if is_manifest and extract_to:
                # Which files were redacted or not scanned, next to the restored files (for the owner only).
                write_new(os.path.join(extract_to, "backup-files.json"), b"".join(chunks).decode("utf-8"))
            if is_manifest:
                manifest = json.loads(b"".join(chunks).decode("utf-8"))
            else:
                found[member.name] = (digest.hexdigest(), member.size)
    if manifest is None:
        problems.append("the archive has no manifest")
    else:
        expected = {e["path"]: (e["sha256"], e["size"]) for e in manifest["files"]}
        if expected != found:
            problems.append("the files do not match the manifest (%d expected, %d found)" % (len(expected), len(found)))
    return {"files": len(found), "problems": problems, "created_at": manifest and manifest.get("created_at")}


def cmd_verify(args):
    if not os.path.isfile(args.archive):
        raise Refused("%s does not exist" % args.archive)
    if args.extract:
        if not os.path.isdir(args.extract):
            raise Refused("extract only into an existing, empty folder")
        for leftover in os.listdir(args.extract):  # the staging folder of a run that was stopped
            if re.match(r"^\.incomplete-[0-9a-f]{8}$", leftover):
                shutil.rmtree(os.path.join(args.extract, leftover))
        if os.listdir(args.extract):
            raise Refused("extract only into an existing, empty folder")
    passphrase = read_passphrase()
    # A restore goes into a staging folder first and is moved into place only once every file has verified, so a
    # damaged archive or a wrong passphrase never leaves a half-restored folder that looks complete.
    staging = os.path.join(args.extract, ".incomplete-%s" % os.urandom(4).hex()) if args.extract else None
    if staging:
        os.mkdir(staging)
    try:
        with open(args.archive, "rb") as f:
            result = verify_stream(f, passphrase, staging)
        if staging and not result["problems"]:
            moved = []
            try:
                for entry in os.listdir(staging):
                    os.replace(os.path.join(staging, entry), os.path.join(args.extract, entry))
                    moved.append(entry)
            except BaseException:
                # The folder was empty before: take back what was moved, so no partial restore is left.
                for entry in moved:
                    target = os.path.join(args.extract, entry)
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    else:
                        os.remove(target)
                raise
    finally:
        if staging and os.path.exists(staging):
            shutil.rmtree(staging)
    if result["problems"]:
        print("NOT VERIFIED: " + "; ".join(result["problems"]) + ("; nothing was restored" if staging else ""))
        return 1
    print("Verified: %d files, created %s%s." % (result["files"], result["created_at"],
                                                 ", extracted" if args.extract else ""))
    return 0


def main(argv=None):
    # docker stop and Docker Desktop shutting down send SIGTERM: end like an interruption, so clean-up runs.
    signal.signal(signal.SIGTERM, lambda _sig, _frame: sys.exit(143))
    parser = argparse.ArgumentParser(description="JARVIS migration kit (read-only)")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--profile", required=True)
    d.add_argument("--content", required=True)
    d.add_argument("--db")
    d.add_argument("--out", required=True)
    d.add_argument("--sample", type=int, default=DEFAULT_SAMPLE)
    d.add_argument("--full", action="store_true", help="scan every row instead of a sample")
    d.add_argument("--check", action="store_true", help="also run SQLite's quick_check on the backup")
    d.add_argument("--templates", help=argparse.SUPPRESS)
    b = sub.add_parser("backup")
    b.add_argument("--profile", required=True)
    b.add_argument("--content", required=True)
    b.add_argument("--out", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--archive", required=True)
    v.add_argument("--extract")
    args = parser.parse_args(argv)
    try:
        return {"discover": cmd_discover, "backup": cmd_backup, "verify": cmd_verify}[args.command](args)
    except Refused as exc:
        print("REFUSED: %s. Nothing was written." % exc, file=sys.stderr)
        return 2
    except KitError as exc:
        print("FAILED: %s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - its text could quote a file or a database value: never print it
        print("FAILED: an unexpected %s; nothing in the JARVIS folders was changed. Run the command again; if it "
              "fails the same way, send this line." % exc.__class__.__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
