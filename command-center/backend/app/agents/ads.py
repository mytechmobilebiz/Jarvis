"""Ads & Analytics: performance reports and budget recommendations; spend changes only after approval (mock)."""

from __future__ import annotations

from collections import defaultdict

from pydantic import Field

from .base import AgentContext, AgentResult, ArtifactSpec, BaseAgent, Finding, Intent, Params, Risk, capability


class ReportParams(Params):
    days: int = Field(7, ge=1, le=90)


class BudgetParams(Params):
    campaign_id: str = Field(max_length=64)
    daily_budget: float = Field(gt=0, le=10_000)
    reason: str = Field(min_length=5, max_length=500)


class AdsAgent(BaseAgent):
    name = "ads"
    title = "Ads & Analytics"
    description = ("Reports ad performance (spend, conversions, ROAS, CPA), flags waste and anomalies, and proposes budget moves. "
                   "Budget changes are financial actions: approval required, and mock in this build. Performance data is sample data until an ad account is connected.")
    integrations = ("ads",)

    def intents(self) -> list[Intent]:
        return [Intent("ads_report", (r"\b(ads?|advertising|campaigns?|roas|cpa|ad spend|ppc)\b",), lambda text, urls: {"days": 7}, weight=2)]

    @capability(risk=Risk.READ, description="Ad performance report with ROAS, CPA and anomalies.", params=ReportParams,
                examples=("How are our ads doing this week?",))
    async def ads_report(self, ctx: AgentContext, params: ReportParams) -> AgentResult:
        rows = ctx.integrations.get("ads").insights(params.days)  # type: ignore[attr-defined]
        per: dict[str, dict] = defaultdict(lambda: {"spend": 0.0, "clicks": 0, "conversions": 0, "revenue": 0.0, "days": []})
        for r in rows:
            c = per[r["campaign_id"]]
            c["name"] = r["campaign"]
            for k in ("spend", "clicks", "conversions", "revenue"):
                c[k] += r[k]
            c["days"].append(r)
        findings: list[Finding] = []
        lines = [f"# Ad performance, last {params.days} days", "", "**SAMPLE DATA**: no ad account is connected; these figures are synthetic.", "",
                 "| Campaign | Spend | Clicks | Conv. | Revenue | ROAS | CPA |", "|---|---|---|---|---|---|---|"]
        total_spend = total_rev = 0.0
        for cid, c in sorted(per.items()):
            roas = c["revenue"] / c["spend"] if c["spend"] else 0.0
            cpa = c["spend"] / c["conversions"] if c["conversions"] else None
            c["roas"], c["cpa"] = round(roas, 2), (round(cpa, 2) if cpa else None)
            total_spend += c["spend"]
            total_rev += c["revenue"]
            lines.append(f"| {c['name']} | ${c['spend']:,.0f} | {c['clicks']} | {c['conversions']} | ${c['revenue']:,.0f} | {roas:.2f} | {'$%.0f' % cpa if cpa else '-'} |")
            if c["conversions"] == 0 and c["spend"] > 100:
                findings.append(Finding(code="no_conversions", severity="warning", title=f"{c['name']}: ${c['spend']:,.0f} spent with no conversions"))
            elif roas < 1:
                findings.append(Finding(code="low_roas", severity="warning", title=f"{c['name']}: ROAS {roas:.2f} (spending more than it returns)"))
            days = sorted(c["days"], key=lambda d: d["date"])
            if len(days) >= 3:
                avg = sum(d["spend"] for d in days[:-1]) / (len(days) - 1)
                if days[-1]["spend"] > 1.5 * avg:
                    findings.append(Finding(code="spend_spike", severity="info", title=f"{c['name']}: yesterday's spend ${days[-1]['spend']:,.0f} vs ${avg:,.0f} average"))
        overall = total_rev / total_spend if total_spend else 0.0
        lines += ["", f"Overall: ${total_spend:,.0f} spent, ${total_rev:,.0f} revenue, ROAS {overall:.2f}.", "", "## Flags"]
        lines += [f"- {f.title}" for f in findings] or ["- None"]
        lines += ["", "Budget changes are proposals only: each needs your approval and is not applied in this build."]
        return AgentResult(
            summary=f"Ads (sample data, {params.days} days): ${total_spend:,.0f} spend, ROAS {overall:.2f}, {len(findings)} flag(s)",
            data={"sample_data": True, "campaigns": {k: {kk: v for kk, v in c.items() if kk != "days"} for k, c in per.items()},
                  "total_spend": total_spend, "total_revenue": total_rev, "roas": round(overall, 2)},
            findings=findings,
            artifacts=[ArtifactSpec(kind="ads_report", title=f"Ad performance ({params.days} days, sample data)", content="\n".join(lines),
                                    data={"sample_data": True})],
        )

    @capability(risk=Risk.FINANCIAL, description="Change a campaign's daily budget (financial: approval required; mock in this build).", params=BudgetParams)
    async def change_budget(self, ctx: AgentContext, params: BudgetParams) -> AgentResult:
        known = {cid for cid, _ in ctx.integrations.get("ads").CAMPAIGNS}  # type: ignore[attr-defined]
        if params.campaign_id not in known:
            raise ValueError(f"unknown campaign '{params.campaign_id}'")
        entry = await ctx.integrations.get("ads").set_daily_budget(  # type: ignore[attr-defined]
            ctx, self.name, campaign_id=params.campaign_id, amount=params.daily_budget, reason=params.reason)
        return AgentResult(summary=f"Budget change for {params.campaign_id} to ${params.daily_budget:,.2f}/day recorded as outbox #{entry.id} (mock: not applied)",
                           data={"outbox_id": entry.id})
