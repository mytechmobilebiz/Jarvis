"""The dashboard in a real browser (Playwright / Chromium)."""

from __future__ import annotations

import os
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
    page.get_by_role("link", name="Supervisor", exact=True).click()
    page.get_by_role("button", name="Check all websites").click()
    page.get_by_role("button", name="Send").click()
    expect(page.get_by_text("Routing this to: wordpress_monitor.check_all_sites.")).to_be_visible(timeout=30_000)
    expect(page.locator(".summary")).to_contain_text("Checked", timeout=90_000)
    shot(page, "02-supervisor")


def test_websites_page_and_site_detail(signed_in: Page, api, test_site):
    task = api.post(f"/api/sites/{test_site['id']}/check", json={"deep": True}).json()["task"]
    wait_task(api, task["id"])
    page = signed_in
    page.get_by_role("link", name="Websites", exact=True).click()
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
    page.get_by_role("link", name="Integrations", exact=True).click()
    page.get_by_role("button", name="Send test event through n8n").click()
    expect(page.get_by_text("acknowledged by n8n")).to_be_visible(timeout=90_000)
    shot(page, "05-integrations")


def test_agents_and_approvals_pages(signed_in: Page):
    page = signed_in
    page.get_by_role("link", name="Agents", exact=True).click()
    expect(page.get_by_role("heading", name="WordPress Monitor")).to_be_visible()
    shot(page, "06-agents")
    page.get_by_role("link", name="Approvals", exact=True).click()
    expect(page.get_by_role("heading", name="Approvals")).to_be_visible()
    page.get_by_role("link", name="Drafts & reports", exact=True).click()
    expect(page.get_by_role("heading", name="Drafts & reports")).to_be_visible()


def test_sign_out(signed_in: Page):
    page = signed_in
    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_role("button", name="Sign in")).to_be_visible()


def test_settings_page_stores_key_without_showing_it(signed_in: Page, api):
    page = signed_in
    page.get_by_role("link", name="Settings", exact=True).click()
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
