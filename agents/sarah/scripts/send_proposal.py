"""Fill the Infinite Marketing proposal for one lead, print it to PDF and email it.

Sarah runs this once per lead instead of assembling the proposal by hand, so
every proposal goes out the same way: all five fields filled, a real PDF,
real line breaks, a proper sender name, a Message-ID on the sender's domain
and, for leads who emailed us, a reply in their own thread.

    python send_proposal.py --name Daniel --company "Peachtree Plumbing" --email daniel@example.com
    python send_proposal.py ... --reply-to-message-id "<abc@mail.gmail.com>" --reply-subject "Web Design"
    python send_proposal.py ... --dry-run      # build the PDF and the email, send nothing

Paths are relative to the Jarvis profile (this file lives in <profile>/scripts/).
The SMTP password is EMAIL_PASSWORD from <profile>/.env, the same value the
himalaya password helper reads. Inside the Codex sandbox the connection goes
through the HTTP proxy named in HTTPS_PROXY, because direct connections are
blocked there.
"""

import argparse
import base64
import datetime
import html
import json
import os
import re
import shutil
import smtplib
import socket
import ssl
import subprocess
import sys
import tempfile
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from urllib.parse import urlparse

PROFILE = Path(__file__).resolve().parent.parent
TEMPLATE = PROFILE / "knowledge" / "sarah" / "proposal" / "proposal-template.html"
OUT_DIR = PROFILE / "workspace" / "proposals"
SENT_LOG = OUT_DIR / "sent-log.jsonl"

SENDER_NAME = "Jerome Hall - Infinite Marketing"
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465
VALID_DAYS = 30
MIN_PDF_BYTES = 200_000

SUBJECT = "Website Design Proposal for {company} - Infinite Marketing"
BODY = """Hi {name},

Thank you for your interest in Infinite Marketing. Attached is your website design proposal for {company}.

It includes our website packages, AI chatbot and voice options, and monthly website care plans. To get started, choose your package and any services, then sign and return the proposal.

This proposal is valid until {valid_until}. If you have any questions, or would like a live demo of our AI chatbots, call me at 678-935-6401 or reply to this email.

Best regards,
Jerome Hall
Infinite Marketing
infinitemarketing.co | 678-935-6401
"""

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


class Failure(Exception):
    pass


def fail(message):
    raise Failure(message)


def long_date(day):
    return f"{day:%B} {day.day}, {day.year}"


def clean_field(label, value, limit):
    value = " ".join((value or "").split())
    if not value:
        fail(f"{label} is empty")
    if len(value) > limit:
        fail(f"{label} is longer than {limit} characters")
    return value


def read_env_file():
    values = {}
    env_file = PROFILE / ".env"
    if not env_file.is_file():
        return values
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return values


def safe_file_part(text):
    part = re.sub(r"[^A-Za-z0-9 _-]+", "", text).strip()
    return re.sub(r"\s+", "-", part)[:60] or "Client"


def already_sent(email):
    if not SENT_LOG.is_file():
        return None
    for line in SENT_LOG.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("email", "").lower() == email.lower() and entry.get("status") == "sent":
            return entry
    return None


def fill_template(name, company, email, today, valid_until):
    if not TEMPLATE.is_file():
        fail(f"template not found: {TEMPLATE}")
    page = TEMPLATE.read_text(encoding="utf-8")
    values = {
        "CLIENT_NAME": name,
        "CLIENT_COMPANY": company,
        "CLIENT_EMAIL": email,
        "PROPOSAL_DATE": long_date(today),
        "VALID_UNTIL": long_date(valid_until),
    }
    for key, value in values.items():
        if "{{" + key + "}}" not in page:
            fail(f"template has no {{{{{key}}}}} field")
        page = page.replace("{{" + key + "}}", html.escape(value))
    if "{{" in page:
        fail("template still has an unfilled {{...}} field")
    return page


def find_edge():
    override = os.environ.get("EDGE_PATH")
    if override:
        return override
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("msedge")
    if found:
        return found
    fail("Microsoft Edge (msedge.exe) was not found")


def print_pdf(html_path, pdf_path):
    edge = find_edge()
    if pdf_path.exists():
        pdf_path.unlink()
    with tempfile.TemporaryDirectory(dir=OUT_DIR, prefix="edge-", ignore_cleanup_errors=True) as profile_dir:
        command = [
            edge,
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--no-pdf-header-footer",
            f"--user-data-dir={profile_dir}",
            f"--print-to-pdf={pdf_path}",
            html_path.resolve().as_uri(),
        ]
        if os.environ.get("EDGE_NO_SANDBOX") == "1":
            command.insert(1, "--no-sandbox")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            fail("Edge did not finish printing the PDF within 3 minutes")
    if not pdf_path.is_file():
        detail = (result.stderr or result.stdout or "").strip()[-600:]
        fail(f"Edge did not create the PDF (exit {result.returncode}): {detail}")
    data = pdf_path.read_bytes()
    if not data.startswith(b"%PDF") or len(data) < MIN_PDF_BYTES:
        fail(f"the PDF looks wrong ({len(data)} bytes)")
    return data


def build_message(sender, name, company, email, valid_until, pdf_name, pdf_bytes, reply_id, reply_subject):
    message = EmailMessage()
    message["From"] = formataddr((SENDER_NAME, sender))
    message["To"] = email
    if reply_id:
        subject = reply_subject or SUBJECT.format(company=company)
        if not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        message["Subject"] = subject
        message["In-Reply-To"] = reply_id
        message["References"] = reply_id
    else:
        message["Subject"] = SUBJECT.format(company=company)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[1])
    message.set_content(BODY.format(name=name, company=company, valid_until=long_date(valid_until)))
    message.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=pdf_name)
    return message


def proxy_for(host):
    url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not url:
        return None
    no_proxy = (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",")
    for entry in (item.strip().lstrip(".") for item in no_proxy):
        if entry and (host == entry or host.endswith("." + entry)):
            return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return parsed


def open_tunnel(proxy, host, port, timeout):
    sock = socket.create_connection((proxy.hostname, proxy.port or 80), timeout=timeout)
    request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    if proxy.username:
        token = base64.b64encode(f"{proxy.username}:{proxy.password or ''}".encode()).decode()
        request += f"Proxy-Authorization: Basic {token}\r\n"
    sock.sendall((request + "\r\n").encode())
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
        if len(response) > 65536:
            break
    status = response.split(b"\r\n", 1)[0].decode(errors="replace")
    if not re.match(r"^HTTP/1\.[01] 200", status):
        sock.close()
        fail(f"the network proxy refused {host}:{port}: {status or 'no answer'}")
    return sock


class TunnelSMTP(smtplib.SMTP_SSL):
    """SMTP over implicit TLS, reached through an HTTP CONNECT proxy."""

    def __init__(self, proxy, *args, **kwargs):
        self._proxy = proxy
        super().__init__(*args, **kwargs)

    def _get_socket(self, host, port, timeout):
        raw = open_tunnel(self._proxy, host, port, timeout)
        return self.context.wrap_socket(raw, server_hostname=host)


def send(message, sender, password, host, port):
    context = ssl.create_default_context()
    proxy = proxy_for(host)
    if proxy:
        client = TunnelSMTP(proxy, host, port, context=context, timeout=60)
    else:
        client = smtplib.SMTP_SSL(host, port, context=context, timeout=60)
    try:
        client.login(sender, password)
        client.send_message(message)
    finally:
        try:
            client.quit()
        except Exception:
            pass


def log_entry(entry):
    with SENT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def run(args):
    name = clean_field("--name", args.name, 80)
    company = clean_field("--company", args.company, 120)
    email = clean_field("--email", args.email, 254)
    if not EMAIL_RE.match(email):
        fail(f"{email!r} is not a valid email address")
    reply_id = (args.reply_to_message_id or "").strip()
    if reply_id and not re.match(r"^<[^<>\s]+@[^<>\s]+>$", reply_id):
        fail("--reply-to-message-id must look like <something@domain>")
    reply_subject = " ".join((args.reply_subject or "").split())[:200]

    if not args.dry_run and not args.force:
        previous = already_sent(email)
        if previous:
            fail(f"a proposal was already sent to {email} on {previous.get('sent_at')}; use --force to send again")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.date.today()
    valid_until = today + datetime.timedelta(days=VALID_DAYS)
    stem = f"Infinite-Marketing-Proposal-{safe_file_part(company)}"
    html_path = OUT_DIR / f"{stem}.html"
    pdf_path = OUT_DIR / f"{stem}.pdf"

    html_path.write_text(fill_template(name, company, email, today, valid_until), encoding="utf-8")
    pdf_bytes = print_pdf(html_path, pdf_path)

    env = read_env_file()
    sender = os.environ.get("EMAIL_ADDRESS") or env.get("EMAIL_ADDRESS") or "infinitemarketingsocial@gmail.com"
    message = build_message(sender, name, company, email, valid_until, pdf_path.name, pdf_bytes, reply_id, reply_subject)

    result = {
        "to": email,
        "subject": message["Subject"],
        "pdf": str(pdf_path),
        "pdf_bytes": len(pdf_bytes),
        "message_id": message["Message-ID"],
        "valid_until": long_date(valid_until),
    }
    if args.dry_run:
        eml_path = OUT_DIR / f"{stem}.eml"
        eml_path.write_bytes(bytes(message))
        result.update(status="dry-run", eml=str(eml_path))
        return result

    password = os.environ.get("EMAIL_PASSWORD") or env.get("EMAIL_PASSWORD")
    if not password:
        fail("EMAIL_PASSWORD is missing from the profile .env")
    host = os.environ.get("EMAIL_SMTP_HOST") or env.get("EMAIL_SMTP_HOST") or DEFAULT_SMTP_HOST
    port = int(os.environ.get("EMAIL_SMTP_PORT") or env.get("EMAIL_SMTP_PORT") or DEFAULT_SMTP_PORT)
    if port != 465:
        fail(f"only SMTP over implicit TLS on port 465 is supported, not port {port}")
    send(message, sender, password, host, port)
    result.update(status="sent", sent_at=datetime.datetime.now().isoformat(timespec="seconds"))
    log_entry({k: result[k] for k in ("status", "sent_at", "to", "message_id", "pdf")} | {"email": email, "company": company})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--name", required=True, help="the lead's first name")
    parser.add_argument("--company", required=True, help="the business name the lead gave")
    parser.add_argument("--email", required=True, help="the lead's email address")
    parser.add_argument("--reply-to-message-id", help="Message-ID of the lead's email, to reply in their thread")
    parser.add_argument("--reply-subject", help="subject of the lead's email, used with --reply-to-message-id")
    parser.add_argument("--dry-run", action="store_true", help="build the PDF and the email but do not send")
    parser.add_argument("--force", action="store_true", help="send even if this address already got a proposal")
    args = parser.parse_args()
    try:
        result = run(args)
    except Failure as error:
        print(json.dumps({"status": "failed", "error": str(error)}))
        return 1
    except (OSError, smtplib.SMTPException) as error:
        print(json.dumps({"status": "failed", "error": f"{type(error).__name__}: {error}"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
