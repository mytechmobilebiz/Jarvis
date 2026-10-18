"""Pure logic of the specialist agents."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.agents.credit_repair import letter_text
from app.agents.sales import fill_template, score
from app.agents.seo import PageParser, audit_findings
from app.agents.support import triage
from app.config import Settings
from app.credit_repair.store import business_days_after
from app.integrations.mocks import MockAdsPlatform
from app.models import Lead


def lead(**kw) -> Lead:
    base = dict(name="Crystal", company="Joyful Journey Home Care", email="crystal@joyful.example", phone="555-0100",
                website="joyful.example", service_interest="website redesign", message="We need a new website with online booking, budget around $1,000, launch this month.")
    return Lead(**{**base, **kw})


def test_strong_lead_scores_high_with_reasons():
    value, reasons, spam = score(lead())
    assert not spam and value >= 90
    assert any("budget" in r for r in reasons) and any("timeline" in r for r in reasons)


def test_thin_lead_scores_low():
    value, _, spam = score(Lead(name="x", company="", email="nope", phone="", website="", service_interest="", message="hi"))
    assert not spam and value == 30


@pytest.mark.parametrize("message", ["We offer guest post and backlink packages", "a http://a.x http://b.x http://c.x links"])
def test_spam_is_detected(message):
    value, reasons, spam = score(lead(message=message, service_interest=""))
    assert spam and value == 0


def test_proposal_fill_escapes_form_input():
    html = fill_template("<p>{{CLIENT_NAME}} at {{CLIENT_COMPANY}} until {{VALID_UNTIL}}</p>",
                         lead(name='<script>alert(1)</script>'), datetime(2026, 10, 18, tzinfo=UTC))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "November 17, 2026" in html


@pytest.mark.parametrize(
    "subject,body,category,priority",
    [
        ("Website down!", "Our site shows a white screen since this morning", "outage", "urgent"),
        ("Hacked?", "There are spam links on our homepage", "security", "urgent"),
        ("Invoice question", "I was charged twice this month", "billing", "high"),
        ("Small change", "Please update the phone number on the contact page", "change_request", "normal"),
        ("Thanks", "Great work on the new site!", "general", "normal"),
    ],
)
def test_support_triage(subject, body, category, priority):
    t = triage(subject, body)
    assert (t["category"], t["priority"]) == (category, priority)


def test_frustration_raises_priority():
    t = triage("Still waiting", "This is unacceptable, I asked for the photo change again last week")
    assert t["sentiment"] == "negative" and t["priority"] == "high"


def test_seo_findings():
    p = PageParser()
    p.feed("<html><head><title>Hi</title><meta name='robots' content='noindex'></head><body><img src=a.png><p>short</p></body></html>")
    findings, score_ = audit_findings(p, 200, "http://x.example/")
    codes = {f.code: f.severity for f in findings}
    assert codes["noindex"] == "critical" and codes["title_length"] == "warning" and codes["no_https"] == "warning"
    assert "no_h1" in codes and "img_alt" in codes and score_ < 60


def test_good_page_scores_well():
    words = " ".join(["word"] * 400)
    p = PageParser()
    p.feed(f"""<html lang="en"><head><title>Web Design for Home Care Agencies | Acme</title>
    <meta name="description" content="We build fast, accessible websites for home care agencies that turn visitors into calls.">
    <meta name="viewport" content="width=device-width"><meta property="og:title" content="x">
    <link rel="canonical" href="https://acme.example/home-care"><script type="application/ld+json">{{}}</script></head>
    <body><h1>Home care websites</h1><img src="a.png" alt="team"><p>{words}</p></body></html>""")
    findings, score_ = audit_findings(p, 200, "https://acme.example/home-care")
    assert score_ >= 95 and not [f for f in findings if f.severity != "info"]


def test_ads_sample_data_is_deterministic_and_labelled():
    ads = MockAdsPlatform(Settings())
    a = ads.insights(7, today=date(2026, 10, 18))
    assert a == ads.insights(7, today=date(2026, 10, 18))
    assert len(a) == 21 and all(r["sample_data"] for r in a)


def test_business_days_skip_weekends():
    assert business_days_after(date(2026, 10, 16), 3) == date(2026, 10, 21)  # Friday -> Wednesday
    assert business_days_after(date(2026, 10, 19), 3) == date(2026, 10, 22)


def test_dispute_letter_content():
    text = letter_text({"full_name": "Pat Example", "address": "1 Test Way\nAtlanta, GA 30301", "dob": "1990-01-01", "ssn_last4": "1234"},
                       "experian", [{"creditor": "Example Bank", "account_last4": "9876", "reason": "not_mine", "detail": ""}],
                       datetime(2026, 10, 18, tzinfo=UTC))
    assert "Experian\nP.O. Box 4500" in text and "1681i" in text
    assert "Example Bank, account ending 9876: This account does not belong to me." in text
    assert "SSN (last 4): 1234" in text
