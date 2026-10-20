"""Who may search the web, how often and with what (plan sections 3.10, 3.14 and 10), and the research settings.

Two app_settings rows hold the controls, read without a cache before every search, so "off" takes effect at once:
- research_mode: "off" (the default when the row is absent), "mock" (a fixed fake provider, nothing leaves) or
  "live" (the private SearXNG container). An admin changes it; live needs a typed confirmation (the API's rule);
- research_settings: the daily caps (50 searches in all, 30 per person), retention days and the owner's own business
  names (data class O).
A row that cannot be read as these values refuses every search (SettingsUnreadable), never falls back to defaults.

Per agent (research_policy, seeded by 0008_research.sql with the defaults below): whether it may search, its daily
cap and the data classes its queries may hold:
  G  general public topics;
  S  software names, versions and CVE ids a person typed (the advisory checks);
  O  the owner's own business names;
  C  client businesses' names and domains (off for everyone by default).
An agent without a row may not search. Credit Repair never searches: refused here whatever its row says.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AppSetting, ResearchPolicy

MODES = ("off", "mock", "live")
MODE_KEY = "research_mode"
SETTINGS_KEY = "research_settings"
SENSITIVE_AGENT = "credit_repair"
CLASSES = ("G", "S", "O", "C")
MAX_CAP = 10_000
MAX_OWNER_TERMS = 50


class SettingsUnreadable(RuntimeError):
    pass


class PolicyRefused(Exception):
    """Research is not allowed here; code says why (never any query text)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ResearchSettings:
    mode: str = "off"
    global_daily_cap: int = 50
    person_daily_cap: int = 30
    sources_days: int = 30
    logs_days: int = 90
    usage_days: int = 730
    owner_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentPolicy:
    agent: str
    search_enabled: bool
    daily_cap: int
    classes: frozenset[str]
    blocked_domains: tuple[str, ...] = ()


def _policy(agent: str, enabled: bool, cap: int, classes: str) -> AgentPolicy:
    return AgentPolicy(agent, enabled, cap, frozenset(classes))


# Plan section 10, as approved (decision 4): SEO & Content without class C; Monitoring and Communications without
# research; Credit Repair none. The agent caps add up to more than the global cap of 50, which binds first.
DEFAULT_POLICIES: dict[str, AgentPolicy] = {p.agent: p for p in (
    _policy("supervisor", True, 20, "GSO"),
    _policy("seo_content", True, 20, "GO"),
    _policy("social_marketing", True, 10, "GO"),
    _policy("wordpress_security", True, 10, "S"),
    _policy("server_management", True, 5, "S"),
    _policy("monitoring", False, 0, ""),
    _policy("communications", False, 0, ""),
    _policy("credit_repair", False, 0, ""),
)}


def _count(value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise SettingsUnreadable("a research setting is not a whole number in range")
    return value


async def load_settings(session: AsyncSession) -> ResearchSettings:
    """The current research settings, read fresh. Raises SettingsUnreadable for any value it cannot use."""
    mode_row = await session.get(AppSetting, MODE_KEY, populate_existing=True)
    row = await session.get(AppSetting, SETTINGS_KEY, populate_existing=True)
    mode = "off" if mode_row is None else mode_row.value
    if mode not in MODES:
        raise SettingsUnreadable("the research mode is not off, mock or live")
    values = {} if row is None else row.value
    if not isinstance(values, dict):
        raise SettingsUnreadable("the research settings are not an object")
    base = ResearchSettings()
    terms = values.get("owner_terms", [])
    if not isinstance(terms, list) or len(terms) > MAX_OWNER_TERMS or not all(
            isinstance(t, str) and 0 < len(t.strip()) <= 200 for t in terms):
        raise SettingsUnreadable("the owner's business names are not a list of short texts")
    return ResearchSettings(
        mode=mode,
        global_daily_cap=_count(values.get("global_daily_cap", base.global_daily_cap), 0, MAX_CAP),
        person_daily_cap=_count(values.get("person_daily_cap", base.person_daily_cap), 0, MAX_CAP),
        sources_days=_count(values.get("sources_days", base.sources_days), 1, 3650),
        logs_days=_count(values.get("logs_days", base.logs_days), 1, 3650),
        usage_days=_count(values.get("usage_days", base.usage_days), 1, 3650),
        owner_terms=tuple(t.strip() for t in terms),
    )


def policy_from_row(row: ResearchPolicy) -> AgentPolicy:
    classes = row.data_classes
    domains = row.blocked_domains
    if (not isinstance(classes, list) or not set(classes) <= set(CLASSES) or not isinstance(domains, list)
            or not all(isinstance(d, str) for d in domains)
            or isinstance(row.search_daily_cap, bool) or not 0 <= row.search_daily_cap <= MAX_CAP):
        raise PolicyRefused("policy_unreadable")
    return AgentPolicy(row.agent, bool(row.search_enabled), row.search_daily_cap, frozenset(classes), tuple(domains))


async def search_policy(session: AsyncSession, agent: str) -> AgentPolicy:
    """The agent's policy if it may search; PolicyRefused (credit_repair, no_policy, policy_unreadable,
    search_disabled) otherwise."""
    if agent == SENSITIVE_AGENT:  # enforced in code, before its row is even read
        raise PolicyRefused("credit_repair")
    row = await session.get(ResearchPolicy, agent, populate_existing=True)
    if row is None:
        raise PolicyRefused("no_policy")
    policy = policy_from_row(row)
    if not policy.search_enabled:
        raise PolicyRefused("search_disabled")
    return policy
