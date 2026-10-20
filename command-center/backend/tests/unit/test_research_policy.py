"""The agent matrix and settings (app/research/policy.py) and SearXNG's health rules (app/research/health.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.agents.registry import all_agents
from app.licensing import EDITIONS
from app.models import ResearchPolicy
from app.research import health, policy

NOW = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
MIGRATION = Path(__file__).resolve().parents[2] / "app" / "migrations" / "0008_research.sql"


def test_the_matrix_is_section_10_as_approved():
    p = policy.DEFAULT_POLICIES
    assert set(p) == set(all_agents())
    assert {a: (x.search_enabled, x.daily_cap, "".join(sorted(x.classes))) for a, x in p.items()} == {
        "supervisor": (True, 20, "GOS"), "seo_content": (True, 20, "GO"), "social_marketing": (True, 10, "GO"),
        "wordpress_security": (True, 10, "S"), "server_management": (True, 5, "S"),
        "monitoring": (False, 0, ""), "communications": (False, 0, ""), "credit_repair": (False, 0, "")}
    assert not any("C" in x.classes for x in p.values())  # class C is off for everyone, SEO included
    assert sum(x.daily_cap for x in p.values()) == 65  # more than the global 50, which binds first


def test_the_migration_seeds_the_same_matrix():
    sql = MIGRATION.read_text(encoding="utf-8")
    for agent, x in policy.DEFAULT_POLICIES.items():
        classes = ", ".join(f'"{c}"' for c in "GSOC" if c in x.classes)
        assert f"('{agent}', {str(x.search_enabled).upper()}, {x.daily_cap}, '[{classes}]', 'default')" in sql


async def test_credit_repair_is_refused_in_code_whatever_its_row_says():
    class NoSession:
        async def get(self, *args, **kwargs):  # never reached
            raise AssertionError("the policy row was read")

    with pytest.raises(policy.PolicyRefused) as exc:
        await policy.search_policy(NoSession(), "credit_repair")
    assert exc.value.code == "credit_repair"


async def test_agents_without_a_row_or_switched_off_are_refused():
    rows = {"seo_content": ResearchPolicy(agent="seo_content", search_enabled=True, search_daily_cap=20,
                                          data_classes=["G", "O"], blocked_domains=["spam.example"]),
            "monitoring": ResearchPolicy(agent="monitoring", search_enabled=False, search_daily_cap=0, data_classes=[],
                                         blocked_domains=[]),
            "broken": ResearchPolicy(agent="broken", search_enabled=True, search_daily_cap=5, data_classes=["X"],
                                     blocked_domains=[])}

    class Session:
        async def get(self, model, key, **kwargs):
            return rows.get(key)

    allowed = await policy.search_policy(Session(), "seo_content")
    assert allowed.classes == {"G", "O"} and allowed.blocked_domains == ("spam.example",) and allowed.daily_cap == 20
    for agent, code in (("monitoring", "search_disabled"), ("nobody", "no_policy"), ("broken", "policy_unreadable")):
        with pytest.raises(policy.PolicyRefused) as exc:
            await policy.search_policy(Session(), agent)
        assert exc.value.code == code


class SettingsSession:
    def __init__(self, mode=None, values=None) -> None:
        from app.models import AppSetting

        self.rows = {}
        if mode is not None:
            self.rows[policy.MODE_KEY] = AppSetting(key=policy.MODE_KEY, value=mode)
        if values is not None:
            self.rows[policy.SETTINGS_KEY] = AppSetting(key=policy.SETTINGS_KEY, value=values)

    async def get(self, model, key, **kwargs):
        return self.rows.get(key)


async def test_research_is_off_by_default_with_the_approved_caps():
    values = await policy.load_settings(SettingsSession())
    assert values == policy.ResearchSettings(mode="off", global_daily_cap=50, person_daily_cap=30, sources_days=30,
                                             logs_days=90, usage_days=730)


@pytest.mark.parametrize("mode, values", [
    ("on", None), (True, None), ("LIVE", None), ("live", "x"), ("mock", {"global_daily_cap": "50"}),
    ("mock", {"person_daily_cap": -1}), ("mock", {"global_daily_cap": True}), ("mock", {"owner_terms": "Acme"}),
    ("mock", {"owner_terms": [""]}), ("mock", {"logs_days": 0}),
])
async def test_settings_that_cannot_be_read_refuse(mode, values):
    with pytest.raises(policy.SettingsUnreadable):
        await policy.load_settings(SettingsSession(mode, values))


def test_the_feature_is_in_the_owners_edition_only_among_the_defaults():
    assert EDITIONS["developer"].features >= {"web_research"}
    # Not in any sellable edition until owner decision 18 and the legal review (COMMERCIALIZATION.md, B9).
    assert [code for code, edition in EDITIONS.items() if "web_research" in edition.features] == ["developer"]
    assert not EDITIONS["developer"].sellable


# --- Health ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("reported, ok", [
    ("2026.10.4+d48c4b555", True), ("2026.10.4-d48c4b555", True), ("2026.10.4+d48c4b555+dirty", True),
    ("2026.10.5+aaaaaaaaa", False), ("", False),
])
def test_the_version_is_compared_in_tag_form(reported, ok):
    assert health.version_ok(reported) is ok
    assert health.normalize_version("2026.10.4+d48c4b555+dirty") == "2026.10.4-d48c4b555"


@pytest.mark.parametrize("message, code, suspended, seconds", [
    ("timeout", "timeout", False, 5),
    ("HTTP connection error", "connection_error", False, 5),
    ("Suspended: too many requests", "too_many_requests", True, 900),
    ("access denied", "access_denied", False, 3600),
    ("Suspended: CAPTCHA", "captcha", True, 3600),
    ("HTTP protocol error", "protocol_error", False, 5),
    ("something new", "engine_error", False, 5),
])
def test_searxng_messages_become_codes_with_their_configured_pause(message, code, suspended, seconds):
    assert health.classify(message) == (code, suspended)
    assert health.pause_seconds(code) == seconds


def detail(**engines) -> dict:
    return {"healthz": "ok", "engines": engines, "recent": []}


def paused(reason: str, minutes: int) -> dict:
    return {"state": "suspended", "reason": reason, "until": (NOW + timedelta(minutes=minutes)).isoformat()}


def test_cooling_engines_are_left_out_until_their_time():
    d = detail(brave=paused("captcha", 60), duckduckgo={"state": "ok"},
               wikipedia={"state": "suspended", "reason": "timeout", "until": (NOW - timedelta(seconds=1)).isoformat()})
    assert health.available_engines(d, NOW) == ["duckduckgo", "wikipedia"]
    assert health.available_engines(d, NOW + timedelta(minutes=61)) == list(health.ENGINES)


def test_the_states_people_see():
    assert health.state(detail(), mode="off", now=NOW) == "off"
    assert health.state(detail(), mode="live", internet="offline", now=NOW) == "pc_offline"
    assert health.state({"healthz": "failed"}, mode="live", now=NOW) == "unreachable"
    assert health.state({}, mode="live", now=NOW) == "unreachable"
    assert health.state(detail(brave={"state": "ok"}), mode="live", now=NOW) == "healthy"
    assert health.state(detail(brave=paused("captcha", 5)), mode="live", now=NOW) == "degraded"
    everything = detail(**{e: paused("captcha", 5) for e in health.ENGINES})
    assert health.state(everything, mode="live", now=NOW) == "all_engines_failing"
    assert health.state({**detail(), "recent": [True, True, True]}, mode="live", now=NOW) == "all_engines_failing"


def test_the_suspension_message_names_each_reason_and_its_time():
    d = detail(duckduckgo=paused("captcha", 60), brave=paused("access_denied", 60))
    message = health.suspension_message(d, NOW)
    assert message == ("SearXNG is running, but DuckDuckGo asked for a CAPTCHA (paused for 60 minutes) and Brave "
                       "refused access (paused for 60 minutes). Results come back at 15:00 UTC. A block by Cloudflare "
                       "or reCAPTCHA can last 1 to 15 days.")
    assert "paused for 15 minutes" in health.suspension_message(detail(brave=paused("too_many_requests", 15)), NOW)
    assert "1 to 15 days" not in health.suspension_message(detail(brave=paused("too_many_requests", 15)), NOW)
    assert "paused for 15 days" in health.suspension_message(detail(brave=paused("cf_captcha", 60)), NOW)
    assert health.suspension_message(detail(), NOW) == ""
