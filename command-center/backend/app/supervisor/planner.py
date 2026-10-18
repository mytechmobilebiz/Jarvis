"""Turning an operator's request into a plan of agent tasks.

Two planners produce the same Plan shape:

* LLMPlanner asks Claude for a plan constrained by a JSON schema whose action
  field is an enum of the real capabilities, then re-validates every step's
  parameters against the capability's own model. Anything invalid is dropped
  with a note, never "repaired" by guessing.
* RulePlanner routes by each agent's declared Intents. It is deterministic,
  needs no API key, and is the fallback whenever the model is unavailable,
  over budget, refuses, or returns something unusable.

Neither planner decides about approvals; the task layer does that from the
capability's risk when the plan is turned into tasks.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..agents.base import BaseAgent, extract_urls
from ..llm.base import LLMError, LLMProvider

log = logging.getLogger(__name__)

MAX_STEPS = 8
HELP_RE = re.compile(r"^\s*(help|\?|what can you do|what do you do|capabilities|list (your )?(agents|capabilities))\b", re.I)


@dataclass
class PlanStep:
    agent: str
    capability: str
    params: dict[str, Any]
    rationale: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "capability": self.capability, "params": self.params, "rationale": self.rationale}


@dataclass
class Plan:
    planner: str
    reply: str
    steps: list[PlanStep] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "planner": self.planner,
            "reply": self.reply,
            "steps": [s.as_dict() for s in self.steps],
            "notes": self.notes,
        }


def validate_step(agents: dict[str, BaseAgent], agent: str, capability: str, params: Any) -> tuple[PlanStep | None, str]:
    a = agents.get(agent)
    if a is None:
        return None, f"dropped step for unknown or disabled agent '{agent}'"
    cap = a.capabilities().get(capability)
    if cap is None:
        return None, f"dropped step: {agent} has no capability '{capability}'"
    if not isinstance(params, dict):
        return None, f"dropped {agent}.{capability}: parameters were not an object"
    try:
        clean = cap.params_model.model_validate(params).model_dump(mode="json")
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        return None, f"dropped {agent}.{capability}: invalid parameter {'.'.join(map(str, first['loc']))}: {first['msg']}"
    return PlanStep(agent=agent, capability=capability, params=clean), ""


def capability_catalog(agents: dict[str, BaseAgent]) -> str:
    lines = []
    for agent in agents.values():
        lines.append(f"## {agent.name} — {agent.title}\n{agent.description}")
        for cap in agent.capabilities().values():
            if agent.name == "supervisor" and cap.name in ("plan_request", "summarize_request"):
                continue
            schema = cap.params_model.model_json_schema()
            props = {
                k: {kk: vv for kk, vv in v.items() if kk in ("type", "description", "default", "enum")}
                for k, v in schema.get("properties", {}).items()
            }
            lines.append(
                f"- {agent.name}.{cap.name} [risk: {cap.risk.value}"
                f"{', needs human approval' if cap.risk.requires_approval else ''}]: {cap.description}\n"
                f"  params: {json.dumps(props, separators=(',', ':'))}; required: {schema.get('required', [])}"
            )
    return "\n".join(lines)


def help_reply(agents: dict[str, BaseAgent]) -> str:
    lines = ["Here is what the team can do. Ask in plain language, for example:"]
    for agent in agents.values():
        examples = [e for c in agent.capabilities().values() for e in c.examples][:2]
        if examples:
            lines.append(f"- {agent.title}: " + " / ".join(f'"{e}"' for e in examples))
    lines.append("Anything that publishes, sends, spends or deletes is held for your approval first.")
    return "\n".join(lines)


class RulePlanner:
    name = "rule-based"

    def plan(self, message: str, agents: dict[str, BaseAgent]) -> Plan:
        if HELP_RE.search(message):
            return Plan(planner=self.name, reply=help_reply(agents))
        urls = extract_urls(message)
        candidates: list[tuple[int, BaseAgent, str, list[dict[str, Any]]]] = []
        for agent in agents.values():
            for intent in agent.intents():
                score = intent.matches(message)
                if not score:
                    continue
                built = intent.build(message, urls)
                if built is None:
                    continue
                param_sets = built if isinstance(built, list) else [built]
                candidates.append((score, agent, intent.capability, param_sets))
        if not candidates:
            return Plan(
                planner=self.name,
                reply="I could not match that request to an agent capability. " + help_reply(agents),
            )
        best = max(c[0] for c in candidates)
        chosen: dict[str, tuple[int, BaseAgent, str, list[dict[str, Any]]]] = {}
        for cand in candidates:
            score, agent = cand[0], cand[1]
            if score < best:
                continue
            if agent.name not in chosen or score > chosen[agent.name][0]:
                chosen[agent.name] = cand
        plan = Plan(planner=self.name, reply="")
        for score, agent, cap_name, param_sets in chosen.values():
            for params in param_sets:
                step, note = validate_step(agents, agent.name, cap_name, params)
                if step is None:
                    plan.notes.append(note)
                    continue
                step.rationale = f"matched {agent.title} keywords (score {score})"
                plan.steps.append(step)
        plan.steps = plan.steps[:MAX_STEPS]
        names = ", ".join(f"{s.agent}.{s.capability}" for s in plan.steps)
        plan.reply = f"Routing this to: {names}." if plan.steps else "Nothing to run for that request."
        return plan


PLANNER_SYSTEM = """You are the supervisor of Infinite AI Command Center, the private automation platform \
of Infinite Marketing, a web design and digital marketing agency. You turn the operator's request into a \
short plan of tasks for specialist agents.

Rules:
- Use only the capabilities listed below, with parameters that match their schemas. Never invent agents, \
capabilities or parameters.
- Use the fewest steps that fully answer the request (at most {max_steps}). Steps run in parallel.
- If the request is a question you can answer from the catalog itself, or is not something the agents can do, \
return no steps and answer in "reply".
- Some capabilities need human approval. Include them only when the operator clearly asked for that action; \
the platform will hold them for approval. Say so in "reply".
- "reply" is one or two plain sentences to the operator saying what you are doing.
- Put each step's parameters in "params_json" as a JSON object string, e.g. "{{\\"url\\": \\"https://example.com\\"}}".
- Monitored sites are listed with their ids; prefer the site id capability when the operator names one.

Capability catalog:
{catalog}

Monitored sites (id, name, url):
{sites}
"""


def plan_schema(actions: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": actions},
                        "params_json": {"type": "string"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["action", "params_json", "rationale"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["reply", "steps"],
        "additionalProperties": False,
    }


class LLMPlanner:
    def __init__(self, llm: LLMProvider) -> None:
        self.llm = llm
        self.name = llm.name

    async def plan(self, message: str, agents: dict[str, BaseAgent], sites: list[tuple[int, str, str]]) -> Plan:
        actions = [
            f"{a.name}.{c.name}"
            for a in agents.values()
            for c in a.capabilities().values()
            if not (a.name == "supervisor" and c.name in ("plan_request", "summarize_request"))
        ]
        system = PLANNER_SYSTEM.format(
            max_steps=MAX_STEPS,
            catalog=capability_catalog(agents),
            sites="\n".join(f"{i}, {n}, {u}" for i, n, u in sites[:100]) or "(none yet)",
        )
        data = await self.llm.json(
            purpose="plan",
            system=system,
            prompt=f"Operator request:\n<request>\n{message}\n</request>",
            schema=plan_schema(actions),
        )
        plan = Plan(planner=self.name, reply=str(data.get("reply", "")).strip())
        for raw in data.get("steps", [])[:MAX_STEPS]:
            action = str(raw.get("action", ""))
            agent, _, cap = action.partition(".")
            try:
                params = json.loads(raw.get("params_json") or "{}")
            except json.JSONDecodeError:
                plan.notes.append(f"dropped {action}: parameters were not valid JSON")
                continue
            step, note = validate_step(agents, agent, cap, params)
            if step is None:
                plan.notes.append(note)
                continue
            step.rationale = str(raw.get("rationale", ""))[:500]
            plan.steps.append(step)
        if not plan.reply:
            plan.reply = "Working on it." if plan.steps else "There is nothing for the agents to run for that."
        return plan


async def make_plan(
    message: str,
    agents: dict[str, BaseAgent],
    llm: LLMProvider,
    sites: list[tuple[int, str, str]],
) -> Plan:
    """Plan with the LLM when one is configured; otherwise, or on any failure, by rules."""
    if llm.available:
        try:
            plan = await LLMPlanner(llm).plan(message, agents, sites)
            if plan.steps or not plan.notes:
                return plan
            fallback = RulePlanner().plan(message, agents)
            fallback.notes = [*plan.notes, "the model's plan had no valid steps; used the rule-based planner"]
            return fallback
        except LLMError as exc:
            log.warning("LLM planning failed, falling back to rules: %s", exc)
            plan = RulePlanner().plan(message, agents)
            plan.notes.append(f"model unavailable ({exc}); used the rule-based planner")
            return plan
    return RulePlanner().plan(message, agents)
