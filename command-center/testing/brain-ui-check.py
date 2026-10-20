#!/usr/bin/env python3
"""The JARVIS Brain page in a real browser, on a test copy with the kit's fixture imported (testing/brain-check.sh
runs it with BRAIN_UI=1, after the import and before the rollback).

    brain-ui-check.py BASE_URL ADMIN_EMAIL ADMIN_PASSWORD PLANTED_JSON

Signs in as the test copy's admin, a viewer and an operator (both created here through the API) and checks what each
one sees: search over memory and conversations, an item's source (provenance), approving and editing an item, and the
restricted-data rules (operators never see customer or quarantined items, viewers never see conversations). No page
may show a planted secret or restricted value. Prints PASS/FAIL lines; exit status 1 on any FAIL. Needs Playwright
with Chromium (PLAYWRIGHT_BROWSERS_PATH).
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import secrets
import sys
import urllib.error
import urllib.request

from playwright.sync_api import Page, sync_playwright

BASE, ADMIN_EMAIL, ADMIN_PASSWORD, PLANTED_FILE = sys.argv[1:5]
PLANTED: dict[str, str] = json.load(open(PLANTED_FILE, encoding="utf-8"))
FAILED: list[str] = []
SHOWN: list[str] = []  # planted values found on a page


def check(ok: bool, what: str, detail: object = "") -> bool:
    print(("PASS  " if ok else "FAIL  ") + what + ("" if ok else ": %s" % str(detail)[:300]))
    if not ok:
        FAILED.append(what)
    return ok


class Api:
    def __init__(self, email: str, password: str) -> None:
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        status, body = self.call("POST", "/api/auth/login", {"email": email, "password": password})
        assert status == 200, (status, body)

    def call(self, method: str, path: str, body: object = None) -> tuple[int, object]:
        req = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"content-type": "application/json", "x-requested-with": "command-center"})
        try:
            with self.opener.open(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")


def user(api: Api, email: str, role: str) -> str:
    """A user of the test copy with a fresh password (created, or its password reset on a second run)."""
    password = secrets.token_urlsafe(18)
    status, body = api.call("POST", "/api/users", {"email": email, "role": role, "password": password})
    if status == 409 or (status == 400 and "exists" in str(body)):
        uid = next(u["id"] for u in api.call("GET", "/api/users")[1]["users"] if u["email"] == email)
        status, body = api.call("PATCH", "/api/users/%d" % uid, {"password": password, "role": role, "is_active": True})
    assert status in (200, 201), (status, body)
    return password


def word(title: str) -> str:
    """The longest word of a title, to search for it."""
    return max(re.findall(r"[A-Za-z]{4,}", title) or [title], key=len)


def sign_in(page: Page, email: str, password: str) -> None:
    page.goto(BASE + "/")
    page.fill("input[type=email]", email)
    page.fill("input[type=password]", password)
    page.click("button[type=submit]")
    page.wait_for_selector("nav", timeout=20000)


def visit(page: Page, path: str, *, wait: str | None = None) -> str:
    """Opens a page of the dashboard and returns its text; every page is checked for planted values."""
    page.goto(BASE + path)
    page.wait_for_selector("h1", timeout=20000)
    if wait:
        page.get_by_text(wait, exact=False).first.wait_for(timeout=20000)
    page.wait_for_load_state("networkidle")
    html = page.content()
    shown = sorted(k for k, v in PLANTED.items() if v and v in html)
    if shown:
        SHOWN.extend(shown)
        check(False, "no planted value on %s" % path, shown)
    return page.inner_text("body")


def tabs(page: Page) -> list[str]:
    return [t.strip() for t in page.locator("[role=tab]").all_inner_texts()]


def search(page: Page, words: str) -> str:
    visit(page, "/brain?tab=search")
    page.fill("input[type=search]", words)
    page.get_by_role("button", name="Search", exact=True).click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)
    html = page.content()
    shown = sorted(k for k, v in PLANTED.items() if v and v in html)
    if shown:
        SHOWN.extend(shown)
        check(False, "no planted value in search results for %r" % words, shown)
    return page.inner_text("[role=tabpanel]")


def main() -> int:
    admin_api = Api(ADMIN_EMAIL, ADMIN_PASSWORD)
    items = admin_api.call("GET", "/api/brain/items?limit=200")[1]["items"]
    pending = next(i for i in items if i["status"] == "pending_review" and i["sensitivity"] == "general"
                   and i["kind"] in ("business", "knowledge", "project") and not i.get("needs_admin"))
    customer = next(i for i in items if i["sensitivity"] == "customer")
    quarantined = next(i for i in items if i["status"] == "quarantined")
    session_id = admin_api.call("GET", "/api/brain/sessions?limit=1")[1]["sessions"][0]["id"]
    viewer_pw = user(admin_api, "brain-ui-viewer@test.local", "viewer")
    operator_pw = user(admin_api, "brain-ui-operator@test.local", "operator")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()

        # --- The admin: everything, the source of each item, approve and edit -------------------------------------
        page = browser.new_context().new_page()
        sign_in(page, ADMIN_EMAIL, ADMIN_PASSWORD)
        visit(page, "/brain")
        check(tabs(page) == ["Overview", "Search", "Conversations", "Agent knowledge", "Skills", "Workflows", "Review",
                             "Import history"], "admin: every tab of the Brain page", tabs(page))
        found = search(page, word(pending["title"]))
        check(pending["title"] in found, "admin: search finds an imported item by a word of its title", found[:300])
        text = visit(page, "/brain?item=%d" % pending["id"], wait="Source")
        check(bool(pending.get("source_ref")) and pending["source_ref"] in text and "Source" in text,
              "admin: the item shows where it came from (%s)" % pending.get("source_ref"), text[:300])
        dialog = page.get_by_role("dialog")
        dialog.get_by_role("button", name="Approve", exact=True).click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)
        after = admin_api.call("GET", "/api/brain/items/%d" % pending["id"])[1]["item"]
        check(after["status"] == "approved", "admin: Approve in the item dialog approves it", after["status"])
        dialog.get_by_role("button", name="Edit").click()
        dialog.get_by_role("textbox", name="Text", exact=True).fill(after["body"] + "\nChecked in the browser test.")
        dialog.get_by_role("button", name="Save as a new version").click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)
        edited = admin_api.call("GET", "/api/brain/items/%d" % pending["id"])[1]["item"]
        check(edited["version"] == after["version"] + 1 and edited["status"] == "pending_review"
              and edited["body"].endswith("Checked in the browser test."),
              "admin: Edit saves a new version and sends the approved item back to review",
              (edited["version"], edited["status"]))
        page.reload()
        page.get_by_role("dialog").get_by_role("button", name="Approve", exact=True).click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)
        approved = admin_api.call("GET", "/api/brain/items/%d" % pending["id"])[1]["item"]
        check(approved["status"] == "approved", "admin: the edited version approved again", approved["status"])
        text = visit(page, "/brain?item=%d" % customer["id"], wait=customer["title"])
        check(customer["title"] in text, "admin: a customer item opens for the admin")
        text = visit(page, "/brain?tab=conversations", wait="message")
        check("message" in text.lower(), "admin: conversations are listed")  # the header is shown in capitals

        # --- A viewer: approved general memory only, no conversations ---------------------------------------------
        page = browser.new_context().new_page()
        sign_in(page, "brain-ui-viewer@test.local", viewer_pw)
        visit(page, "/brain")
        check(tabs(page) == ["Overview", "Search", "Agent knowledge", "Skills"], "viewer: no conversation, workflow, "
              "review or import tab", tabs(page))
        found = search(page, word(pending["title"]))
        check(pending["title"] in found, "viewer: search finds the approved item", found[:300])
        found = search(page, word(customer["title"]))
        check(customer["title"] not in found, "viewer: search never shows a customer item", found[:300])
        text = visit(page, "/brain?item=%d" % customer["id"])
        check(customer["title"] not in text, "viewer: a customer item does not open by its address")
        text = visit(page, "/brain?tab=conversations&session=%d" % session_id)
        check("Overview" in tabs(page) and "Conversations" not in tabs(page) and page.get_by_role("dialog").count() == 0,
              "viewer: a conversation does not open by its address")

        # --- An operator: conversations and review, never customer or quarantined items --------------------------
        page = browser.new_context().new_page()
        sign_in(page, "brain-ui-operator@test.local", operator_pw)
        visit(page, "/brain")
        check("Conversations" in tabs(page) and "Review" in tabs(page), "operator: conversation and review tabs",
              tabs(page))
        found = search(page, word(customer["title"]))
        check(customer["title"] not in found, "operator: search never shows a customer item", found[:300])
        for it, what in ((customer, "customer"), (quarantined, "quarantined")):
            text = visit(page, "/brain?item=%d" % it["id"])
            check(it["title"] not in text, "operator: a %s item does not open by its address" % what)
        text = visit(page, "/brain?tab=conversations", wait="message")
        check("message" in text.lower(), "operator: conversations are listed")
        browser.close()
    check(not SHOWN, "no planted secret or restricted value on any page visited", sorted(set(SHOWN)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
