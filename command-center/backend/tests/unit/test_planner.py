from __future__ import annotations

import json

import pytest

from app.agents.base import extract_urls
from app.agents.registry import all_agents
from app.supervisor.planner import LLMPlanner, RulePlanner, make_plan, plan_schema

from ..fakes import FakeLLM


@pytest.fixture
def agents():
    return all_agents()


def steps(plan):
    return [(s.agent, s.capability, s.params) for s in plan.steps]


def test_extract_urls():
    assert extract_urls("check example.com and https://foo.org/bar, then www.baz.co.uk.") == [
        "https://foo.org/bar",
        "https://example.com",
        "https://www.baz.co.uk",
    ]
    assert extract_urls("email jane@example.com about it") == []
    assert extract_urls("no domains here, e.g. none") == []


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Check all websites", [("wordpress_monitor", "check_all_sites", {"deep": False})]),
        ("Run a security scan on every site", [("wordpress_monitor", "check_all_sites", {"deep": True})]),
        ("Is example.com up?", [("wordpress_monitor", "check_url", {"url": "https://example.com", "deep": True})]),
        ("Prepare the daily briefing", [("supervisor", "daily_briefing", {"hours": 24})]),
        ("Is the system healthy?", [("supervisor", "health_review", {})]),
    ],
)
def test_rule_routing(agents, message, expected):
    plan = RulePlanner().plan(message, agents)
    assert steps(plan) == expected
    assert plan.planner == "rule-based"


def test_rule_routing_multiple_urls(agents):
    plan = RulePlanner().plan("check acme.com and https://beta.org", agents)
    assert [s.params["url"] for s in plan.steps] == ["https://beta.org", "https://acme.com"]


def test_help_and_unknown_requests_produce_no_tasks(agents):
    help_plan = RulePlanner().plan("help", agents)
    assert help_plan.steps == [] and "approval" in help_plan.reply
    unknown = RulePlanner().plan("bake me a cake", agents)
    assert unknown.steps == [] and "could not match" in unknown.reply


async def test_llm_plan_is_validated(agents):
    llm = FakeLLM(
        json_responses=[
            {
                "reply": "Checking the site.",
                "steps": [
                    {"action": "wordpress_monitor.check_url", "params_json": json.dumps({"url": "acme.com"}), "rationale": "asked"},
                    {"action": "wordpress_monitor.check_site", "params_json": json.dumps({"site_id": "x"}), "rationale": "bad"},
                    {"action": "wordpress_monitor.check_url", "params_json": "{not json", "rationale": "bad"},
                    {"action": "ghost.agent", "params_json": "{}", "rationale": "bad"},
                ],
            }
        ]
    )
    plan = await LLMPlanner(llm).plan("check acme.com", agents, [(1, "Acme", "https://acme.com")])
    assert steps(plan) == [("wordpress_monitor", "check_url", {"url": "https://acme.com", "deep": True})]
    assert len(plan.notes) == 3
    assert plan.planner == "fake:test-model"
    call = llm.calls[0]
    assert "1, Acme, https://acme.com" in call["system"]
    assert "<request>\ncheck acme.com\n</request>" in call["prompt"]
    enum = call["schema"]["properties"]["steps"]["items"]["properties"]["action"]["enum"]
    assert "wordpress_monitor.check_url" in enum
    assert "supervisor.plan_request" not in enum  # internal capabilities are not offered


async def test_llm_answer_without_steps_is_kept(agents):
    llm = FakeLLM(json_responses=[{"reply": "There are two agents.", "steps": []}])
    plan = await make_plan("how many agents are there?", agents, llm, [])
    assert plan.steps == [] and plan.reply == "There are two agents."


async def test_llm_outage_falls_back_to_rules(agents):
    plan = await make_plan("Check all websites", agents, FakeLLM(fail=True), [])
    assert plan.planner == "rule-based"
    assert steps(plan) == [("wordpress_monitor", "check_all_sites", {"deep": False})]
    assert any("model unavailable" in n for n in plan.notes)


async def test_llm_plan_with_only_invalid_steps_falls_back(agents):
    llm = FakeLLM(json_responses=[{"reply": "ok", "steps": [{"action": "wordpress_monitor.check_site", "params_json": "{}", "rationale": ""}]}])
    plan = await make_plan("Check all websites", agents, llm, [])
    assert plan.planner == "rule-based"
    assert steps(plan) == [("wordpress_monitor", "check_all_sites", {"deep": False})]


def test_plan_schema_is_strict():
    schema = plan_schema(["a.b"])
    assert schema["additionalProperties"] is False
    assert schema["properties"]["steps"]["items"]["additionalProperties"] is False
