"""Hourly website watchdog for the owner's sites, with no AI involved.

Runs as a Hermes no-agent cron job: whatever it prints is delivered to the
owner word for word, and when it prints nothing the run stays silent. It only
speaks when a site's state changes (a new problem, a different problem, or a
recovery), so a site that stays down is reported once, not every hour.

The sites come from <profile>/knowledge/websites.md: one domain or URL per
line; anything that is not a domain or URL is ignored, so the file can carry
notes. State between runs lives in <profile>/workspace/website-check-state.json.

    python check_websites.py            # normal run: print changes only
    python check_websites.py --report   # print the current state of every site
"""

import concurrent.futures
import datetime
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROFILE = Path(__file__).resolve().parent.parent
SITES_FILE = PROFILE / "knowledge" / "websites.md"
STATE_FILE = PROFILE / "workspace" / "website-check-state.json"

TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)
# Words on an otherwise "working" page that mean the site is not really up.
PAGE_PROBLEMS = [
    ("account suspended", "shows an Account Suspended page from the web host"),
    ("account has been suspended", "shows an Account Suspended page from the web host"),
    ("error establishing a database connection", "shows a WordPress database error"),
    ("there has been a critical error on this website", "shows a WordPress critical error"),
    ("this domain has expired", "shows a domain-expired page"),
    ("domain is for sale", "shows a domain-for-sale page"),
    ("buy this domain", "shows a domain-for-sale page"),
]
PAGE_NOTES = [
    ("coming soon", "shows a Coming Soon page"),
    ("under construction", "shows an Under Construction page"),
    ("maintenance mode", "is in maintenance mode"),
]
WARN = "\u26a0\ufe0f"
OK_MARK = "\u2705"
DOMAIN_RE = re.compile(r"^(?:https?://)?([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)(?::\d+)?(/\S*)?$")


def read_sites():
    if not SITES_FILE.is_file():
        return None
    sites = []
    for raw in SITES_FILE.read_text(encoding="utf-8-sig").splitlines():
        token = raw.strip().lstrip("-*0123456789.) \t").strip().strip("`<>")
        match = DOMAIN_RE.match(token)
        if not match or "." not in match.group(1):
            continue
        url = token if token.startswith(("http://", "https://")) else "https://" + token
        if url not in sites:
            sites.append(url)
    return sites


def label(url):
    return re.sub(r"^https?://", "", url).rstrip("/")


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))
    with opener.open(request, timeout=TIMEOUT) as response:
        return response.status, response.read(400_000).decode("utf-8", "replace")


def diagnose(url):
    """Return None when the site looks fine, else a short description of the problem."""
    try:
        status, body = fetch(url)
    except urllib.error.HTTPError as error:
        body = ""
        try:
            body = error.read(200_000).decode("utf-8", "replace").lower()
        except Exception:
            pass
        if error.code in (403, 429, 503) and ("just a moment" in body or "cloudflare" in body or "captcha" in body):
            return None  # bot protection answered; the site itself is up
        for needle, problem in PAGE_PROBLEMS:
            if needle in body:
                return problem
        return f"answers with an error (HTTP {error.code})"
    except urllib.error.URLError as error:
        reason = error.reason
        if isinstance(reason, socket.gaierror):
            return "domain does not resolve (DNS) - the domain may have expired"
        if isinstance(reason, ssl.SSLCertVerificationError):
            return f"SSL certificate problem: {reason.verify_message or reason}"
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return f"no response within {TIMEOUT} seconds"
        if isinstance(reason, ConnectionRefusedError):
            return "refuses connections (the server may be down)"
        if isinstance(reason, ConnectionResetError):
            return "drops the connection (the server may be down or blocking)"
        return f"cannot be reached ({reason})"
    except (socket.timeout, TimeoutError):
        return f"no response within {TIMEOUT} seconds"
    except ssl.SSLError as error:
        return f"SSL problem: {error}"
    except (ConnectionError, OSError) as error:
        return f"cannot be reached ({error})"
    text = body.lower()
    for needle, problem in PAGE_PROBLEMS:
        if needle in text:
            return problem
    for needle, note in PAGE_NOTES:
        if needle in text:
            return note
    return None


def check(url):
    first = diagnose(url)
    if first is None:
        return None
    second = diagnose(url)  # one retry so a passing hiccup does not cause an alert
    return second


def network_is_down():
    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("CHECK_SKIP_NETWORK_TEST"):
        return False  # behind a proxy (e.g. the Codex sandbox) a direct probe proves nothing
    for host in ("www.google.com", "www.cloudflare.com"):
        try:
            socket.create_connection((host, 443), timeout=10).close()
            return False
        except OSError:
            continue
    return True


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")


def main():
    report = "--report" in sys.argv[1:]
    sites = read_sites()
    if sites is None:
        print(f"Website check: the site list {SITES_FILE} is missing.")
        return 0
    if not sites:
        print(f"Website check: no domains found in {SITES_FILE}.")
        return 0
    if network_is_down():
        return 0  # this computer is offline (just woke up, Wi-Fi down); say nothing, try next hour

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip(sites, pool.map(check, sites)))

    previous = load_state()
    first_run = not previous
    lines = []
    for url in sites:
        now, before = results[url], previous.get(url)
        if report:
            lines.append(f"{WARN if now else OK_MARK} {label(url)}: {now or 'OK'}")
        elif now and now != before:
            lines.append(f"{WARN} {label(url)}: {now}")
        elif before and not now and not first_run:
            lines.append(f"{OK_MARK} {label(url)} is OK again (was: {before})")
    save_state({url: problem for url, problem in results.items() if problem})

    if lines:
        stamp = datetime.datetime.now().strftime("%b %d %I:%M %p").replace(" 0", " ")
        title = "Website report" if report else "Website check"
        ok = sum(1 for problem in results.values() if not problem)
        print(f"{title} ({stamp}) - {ok} of {len(sites)} sites OK")
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
