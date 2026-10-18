"""The dashboard in a real browser (Playwright / Chromium)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from .conftest import wait_task

SHOTS = Path(os.environ.get("CC_SCREENSHOT_DIR", Path(__file__).parent / "screenshots"))


@pytest.fixture
def signed_in(page: Page, base_url, env, test_site):
    page.goto(base_url)
    page.get_by_label("Email").fill(env["ADMIN_EMAIL"])
    page.get_by_label("Password").fill(env["ADMIN_PASSWORD"])
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    return page


def nav(page: Page, label: str) -> None:
    """Open a page from the sidebar menu (pages can contain links with the same name)."""
    page.locator(".sidebar nav").get_by_role("link", name=label, exact=True).click()


def shot(page: Page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=True)


def test_wrong_password_is_refused(page: Page, base_url, env):
    page.goto(base_url)
    page.get_by_label("Email").fill(env["ADMIN_EMAIL"])
    page.get_by_label("Password").fill("definitely-not-the-password")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_contain_text("wrong email or password")


def test_overview(signed_in: Page):
    page = signed_in
    expect(page.get_by_text("Websites", exact=True).first).to_be_visible()
    expect(page.get_by_text("Platform")).to_be_visible()
    expect(page.locator(".kv").get_by_text("connected").first).to_be_visible()
    shot(page, "01-overview")


def test_supervisor_console(signed_in: Page):
    page = signed_in
    nav(page, "Supervisor")
    page.get_by_role("button", name="Check all websites").click()
    page.get_by_role("button", name="Send").click()
    expect(page.get_by_text("Routing this to: monitoring.check_all_sites.")).to_be_visible(timeout=30_000)
    expect(page.locator(".summary")).to_contain_text("Checked", timeout=90_000)
    shot(page, "02-supervisor")


def test_websites_page_and_site_detail(signed_in: Page, api, test_site):
    task = api.post(f"/api/sites/{test_site['id']}/check", json={"deep": True}).json()["task"]
    wait_task(api, task["id"])
    page = signed_in
    nav(page, "Websites")
    row = page.get_by_role("row", name="Local test WordPress")
    expect(row).to_be_visible()
    expect(row.locator(".pill").first).to_have_text("Up")
    shot(page, "03-websites")
    row.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("WordPress")
    expect(dialog).to_contain_text("XML-RPC is enabled", timeout=30_000)
    shot(page, "04-site-detail")


def test_n8n_test_event_from_integrations_page(signed_in: Page):
    page = signed_in
    nav(page, "Integrations")
    page.get_by_role("button", name="Send test event through n8n").click()
    expect(page.get_by_text("acknowledged by n8n")).to_be_visible(timeout=90_000)
    shot(page, "05-integrations")


def test_agents_and_approvals_pages(signed_in: Page):
    page = signed_in
    nav(page, "Agents")
    expect(page.get_by_role("heading", name="The seven agents")).to_be_visible()
    for title in ("WordPress Security & Repair", "SEO & Content", "Social Media & Marketing (Postonomous)", "VPS & Server Management (CWP/SSH)",
                  "Email & Customer Communications", "Website & Infrastructure Monitoring", "Credit Repair & Dispute Management"):
        expect(page.locator(".card-head h2").filter(has_text=title).first).to_be_visible()
    shot(page, "06-agents")
    nav(page, "Approvals")
    expect(page.get_by_role("heading", name="Approvals")).to_be_visible()
    nav(page, "Drafts & reports")
    expect(page.get_by_role("heading", name="Drafts & reports")).to_be_visible()


def test_sign_out(signed_in: Page):
    page = signed_in
    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_role("button", name="Sign in")).to_be_visible()


def test_settings_page_stores_key_without_showing_it(signed_in: Page, api):
    page = signed_in
    nav(page, "Settings")
    expect(page.get_by_text("Developer (internal, not for sale)")).to_be_visible()
    fake = "sk-ant-e2e-placeholder-not-a-real-key-9z9z"
    page.get_by_label("New Anthropic API key").fill(fake)
    page.get_by_role("button", name="Save key").click()
    expect(page.get_by_text("set …9z9z")).to_be_visible()
    assert fake not in page.content()
    assert fake not in api.get("/api/settings").text
    shot(page, "07-settings")
    # Remove the placeholder straight away so no AI call is ever attempted with it.
    page.once("dialog", lambda d: d.accept())
    page.get_by_role("button", name="Remove").click()
    expect(page.get_by_text("not set")).to_be_visible()


def test_leads_support_outbox_and_credit_repair_pages(signed_in: Page, api):
    page = signed_in
    nav(page, "Leads")
    expect(page.get_by_role("heading", name="Leads")).to_be_visible()
    page.get_by_label("name", exact=True).fill("UI Prospect")
    page.get_by_label("company", exact=True).fill("UI Test Bakery")
    page.get_by_label("email", exact=True).fill("ui-prospect@example.test")
    page.get_by_role("button", name="Add lead").click()
    row = page.get_by_role("row").filter(has_text="UI Test Bakery").first
    expect(row).to_be_visible()
    expect(row.locator("td").nth(1)).to_have_text(re.compile(r"^\d+$"), timeout=30_000)  # scored by the Sales agent
    shot(page, "08-leads")

    nav(page, "Client support")
    expect(page.get_by_role("heading", name="Client support")).to_be_visible()
    shot(page, "09-support")

    nav(page, "Outbox")
    expect(page.get_by_role("heading", name="Outbox")).to_be_visible()
    expect(page.get_by_text("Nothing here left the machine.")).to_be_visible()
    shot(page, "10-outbox")

    nav(page, "Credit Repair")
    expect(page.get_by_role("heading", name="Credit Repair")).to_be_visible()
    expect(page.get_by_text("isolated module")).to_be_visible()
    shot(page, "11-credit-repair")


def test_servers_page_and_server_side_site_detail(signed_in: Page, api):
    if not any(s["name"] == "wp-repair-test" for s in api.get("/api/servers").json()["servers"]):
        pytest.skip("needs the wp-repair server from test_ssh_repairs.py")
    page = signed_in
    nav(page, "Servers")
    expect(page.get_by_role("heading", name="Servers", exact=True)).to_be_visible()
    expect(page.get_by_label("authorized_keys line")).to_contain_text('command="/usr/local/bin/cc-gate",restrict')
    page.get_by_role("row").filter(has_text="wp-repair-test").first.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Command audit trail")
    expect(dialog).to_contain_text("cc-gate 1.1.0 as ccagent")
    shot(page, "12-servers")
    page.keyboard.press("Escape")
    nav(page, "Websites")
    page.get_by_role("row").filter(has_text="Repair test site").first.click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Server-side (WP-CLI)")
    expect(dialog.get_by_role("button", name="Restore home/siteurl")).to_be_visible()
    shot(page, "13-site-repairs")
