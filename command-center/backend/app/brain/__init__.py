"""JARVIS Brain: persistent memory for the agents, and the import of JARVIS's knowledge and history.

Imported text is reference data. It never becomes an instruction, an approval or a running workflow:
approvals are decided only by app/tasks.py from each capability's risk, and a workflow draft becomes a
schedule (created disabled) only through an admin's action.

jarvis_kit.py and templates.json in this package are byte-for-byte copies of migration/jarvis/ (the read-only
kit the owner runs on the PC); tests/unit/test_brain_text.py checks that they stay identical.
"""

from __future__ import annotations

# The format of what the importer writes. Bumped when the meaning of a stored field changes.
IMPORTER_VERSION = "1.1.0"

KINDS: dict[str, str] = {
    "business": "Business memory",
    "knowledge": "Shared knowledge",
    "agent_note": "Agent note",
    "instruction": "Operating instruction (reference)",
    "skill": "Skill reference",
    "workflow": "Workflow reference",
    "project": "Project and task context",
    "decision": "Decision",
    "accomplishment": "Accomplishment",
    "problem": "Problem",
}
STATUSES = ("pending_review", "approved", "rejected", "archived")
SENSITIVITIES = ("general", "customer")
HISTORY_LABELS = ("accomplishment", "problem", "decision")

# The Command Center's agents, as installed (app/agents/registry.py), in sidebar order.
AGENTS: dict[str, str] = {
    "supervisor": "AI Supervisor",
    "wordpress_security": "WordPress Security & Repair",
    "seo_content": "SEO & Content",
    "social_marketing": "Social Media & Marketing",
    "server_management": "VPS & Server Management",
    "communications": "Email & Customer Communications",
    "monitoring": "Website & Infrastructure Monitoring",
    "credit_repair": "Credit Repair & Dispute Management",
}

# JARVIS's six staff and the Command Center agent each one maps to. Tom (development) has no counterpart:
# his material is kept for the Supervisor only.
STAFF = ("jarvis", "sarah", "scout", "tom", "ava", "bobby")
STAFF_AGENT: dict[str, str] = {
    "jarvis": "supervisor",
    "sarah": "communications",
    "scout": "seo_content",
    "ava": "social_marketing",
    "bobby": "social_marketing",
    "tom": "supervisor",
}

# JARVIS 1.7.4's skills: (staff, Command Center agent, matching capabilities, compatibility, note).
# compatibility: "mapped" (a capability does the same job), "partial" (one exists, but something JARVIS did
# is missing or now needs approval), "reference_only" (no counterpart; kept as text).
SKILLS: dict[str, tuple[str, str, tuple[str, ...], str, str]] = {
    "morning-brief": ("jarvis", "supervisor", ("daily_briefing",), "partial",
                      "The daily briefing covers sites, alerts, tasks and approvals; revenue and targets are not "
                      "connected. Delivery to Slack is not built."),
    "post-as": ("jarvis", "supervisor", (), "reference_only", "Slack posting is not built in the Command Center."),
    "triage-inbox": ("sarah", "communications", ("triage_ticket", "draft_reply", "send_reply"), "partial",
                     "No mailbox reading yet. Every reply needs approval (JARVIS sent them without asking)."),
    "support-metrics": ("sarah", "communications", ("support_overview",), "partial",
                        "Counts come from the Command Center's own support tickets."),
    "dig": ("scout", "seo_content", ("keyword_brief", "audit_page"), "partial",
            "Research is limited to public pages read through the read-only web client."),
    "weekly-sweep": ("scout", "seo_content", ("keyword_brief",), "partial",
                     "No YouTube, X or Reddit collection; JARVIS's key-in-URL request is not reused."),
    "plan-before-code": ("tom", "supervisor", (), "reference_only", "No development agent in the Command Center."),
    "coding-lane": ("tom", "supervisor", (), "reference_only",
                    "No coding agent; pushing branches and opening pull requests is not built."),
    "roas-report": ("bobby", "social_marketing", ("ads_report",), "mapped",
                    "Sample data until a Meta Ads integration is connected."),
    "ad-actions": ("bobby", "social_marketing", ("change_budget",), "mapped",
                   "Budget changes are financial: each one needs approval in the Command Center, never a Slack reply."),
    "write-hooks": ("ava", "social_marketing", ("draft_social_posts",), "mapped", "Drafts only; nothing is published."),
    "schedule-posts": ("ava", "social_marketing", ("publish_draft",), "partial",
                       "Publishing needs approval per post; scheduling through Postiz is not built."),
}

# JARVIS's scheduled jobs (by Hermes job name): the Command Center capability a draft would run, its parameters,
# and an existing Command Center automation that already does the same job (a draft must not duplicate it).
JOBS: dict[str, dict[str, object]] = {
    "morning-brief": {"staff": "jarvis", "agent": "supervisor", "capability": "daily_briefing", "params": {"hours": 24},
                      "compatibility": "partial",
                      "overlaps": "The n8n workflow cc-daily-briefing already asks the Supervisor for the daily "
                                  "briefing on weekdays at 07:52."},
    "triage-inbox": {"staff": "sarah", "agent": "communications", "capability": "support_overview", "params": {},
                     "compatibility": "reference_only",
                     "note": "Mailbox reading is not built: there is no inbox to triage on a schedule."},
    "weekly-sweep": {"staff": "scout", "agent": "seo_content", "capability": "keyword_brief", "params": {},
                     "compatibility": "partial",
                     "note": "keyword_brief needs a topic: set one before converting this draft."},
    "roas-report": {"staff": "bobby", "agent": "social_marketing", "capability": "ads_report", "params": {},
                    "compatibility": "mapped"},
    "write-hooks": {"staff": "ava", "agent": "social_marketing", "capability": "draft_social_posts", "params": {},
                    "compatibility": "partial",
                    "note": "draft_social_posts needs a topic: set one before converting this draft."},
}


def staff_of_path(rel: str) -> str:
    """The staff member a profile path belongs to: knowledge/<role>/..., else Jarvis."""
    parts = rel.replace("\\", "/").split("/")
    if len(parts) > 2 and parts[0] == "knowledge" and parts[1] in STAFF:
        return parts[1]
    return "jarvis"
