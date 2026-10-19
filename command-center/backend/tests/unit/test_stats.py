"""The dashboard's task categories cover every capability."""

from __future__ import annotations

from app.agents.registry import all_agents
from app.api.stats import CATEGORIES, CATEGORY_OF, INTERNAL, category


def test_every_registered_capability_has_a_category():
    keys = {k for k, _ in CATEGORIES}
    missing = []
    for name, agent in all_agents().items():
        for cap in agent.describe()["capabilities"]:
            pair = (name, cap["name"])
            if pair in INTERNAL:
                continue
            if pair not in CATEGORY_OF:
                missing.append(pair)
            else:
                assert CATEGORY_OF[pair] in keys, pair
    assert not missing, f"add these to CATEGORY_OF in app/api/stats.py: {missing}"


def test_internal_supervisor_steps_are_not_counted():
    assert category("supervisor", "plan_request") is None
    assert category("supervisor", "summarize_request") is None
    assert category("supervisor", "daily_briefing") == "reports"


def test_no_stale_entries():
    registered = {(n, c["name"]) for n, a in all_agents().items() for c in a.describe()["capabilities"]}
    assert set(CATEGORY_OF) <= registered, set(CATEGORY_OF) - registered
