"""The specialist agents on the running stack: n8n lead intake, support triage,
the read-only SEO audit, mock outbound actions and the Credit Repair isolation."""

from __future__ import annotations

import re
import uuid

import httpx

from .conftest import compose, wait_for, wait_task, worker_access_lines

AGENTS = {"supervisor", "wordpress_security", "seo_content", "social_marketing", "server_management", "communications", "monitoring",
          "credit_repair"}


def run(api, agent: str, capability: str, params: dict) -> dict:
    r = api.post(f"/api/agents/{agent}/tasks", json={"capability": capability, "params": params})
    assert r.status_code == 201, r.text
    task = r.json()["task"]
    return task if task["status"] == "awaiting_approval" else wait_task(api, task["id"])


def approve_task(api, task_id: int) -> dict:
    approval = next(a for a in api.get("/api/approvals").json()["approvals"] if a["task_id"] == task_id)
    r = api.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True, "note": "e2e"})
    assert r.status_code == 200, r.text
    return wait_task(api, task_id)


def psql(db: str, sql: str, user: str = "postgres") -> str:
    p = compose("exec", "-T", "postgres", "psql", "-U", user, "-d", db, "-tAc", sql)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def test_all_seven_agents_and_the_supervisor_are_registered(api):
    listed = api.get("/api/agents").json()["agents"]
    agents = {a["name"]: a for a in listed}
    assert set(agents) == AGENTS
    assert [a["role"] for a in listed].count("supervisor") == 1 and agents["supervisor"]["role"] == "supervisor"
    assert agents["credit_repair"]["sensitive"] is True
    assert not any(a["sensitive"] for name, a in agents.items() if name != "credit_repair")


def test_lead_from_the_n8n_webhook_to_a_mock_proposal_email(api, env):
    email = f"lead-{uuid.uuid4().hex[:8]}@example.test"
    url = f"http://127.0.0.1:{env.get('N8N_PORT', '5680')}/webhook/lead-intake"
    form = {"your-name": "E2E Prospect", "company": "E2E Dental", "email": email,
            "service": "SEO and a new website", "message": "We have a budget of $3,000 and want to start next month."}
    with httpx.Client(timeout=30, trust_env=False) as n8n:
        assert n8n.post(url, json=form).status_code == 403  # the webhook needs the shared secret
        assert n8n.post(url, json=form, headers={"X-CC-Webhook-Secret": env["N8N_WEBHOOK_SECRET"]}).status_code == 200

    def scored():
        lead = next((l for l in api.get("/api/leads").json()["leads"] if l["email"] == email), None)
        return lead if lead and lead["score"] is not None else None

    lead = wait_for(scored, what="the lead to arrive through n8n and be scored")
    assert lead["source"] == "workflow" and lead["name"] == "E2E Prospect" and lead["score"] >= 75

    draft = run(api, "communications", "draft_proposal", {"lead_id": lead["id"]})
    assert draft["status"] == "succeeded", draft
    send = run(api, "communications", "send_proposal", {"lead_id": lead["id"]})
    assert send["status"] == "awaiting_approval"  # nothing goes out without a person
    assert not any(e["recipient"] == email for e in api.get("/api/outbox").json()["entries"])
    assert approve_task(api, send["id"])["status"] == "succeeded"
    entry = next(e for e in api.get("/api/outbox").json()["entries"] if e["recipient"] == email)
    assert entry["mode"] == "mock" and entry["result"] == "recorded, not sent"


def test_support_message_is_triaged_and_a_reply_drafted(api):
    subject = f"Site down {uuid.uuid4().hex[:6]}"
    r = api.post("/api/support/tickets", json={"customer_name": "E2E Client", "customer_email": "client@example.test",
                                               "subject": subject, "body": "Our website is not loading at all."})
    ticket_id = r.json()["ticket"]["id"]

    def drafted():
        t = next(t for t in api.get("/api/support/tickets").json()["tickets"] if t["id"] == ticket_id)
        return t if t["status"] == "drafted" else None

    ticket = wait_for(drafted, what="triage and a reply draft")
    assert ticket["category"] == "outage" and ticket["priority"] == "urgent" and ticket["reply_artifact_id"]


def test_seo_audit_reads_the_local_wordpress_only(api, test_site):
    before = len(worker_access_lines())
    task = run(api, "seo_content", "audit_page", {"url": test_site["url"]})
    assert task["status"] == "succeeded", task
    assert task["result"]["data"]["score"] is not None
    lines = wait_for(lambda: (l := worker_access_lines())[before:] and l[before:], timeout=30, what="the audit in the access log")
    methods = {m.group(1) for line in lines if (m := re.search(r'"([A-Z]+) /', line))}
    assert methods == {"GET"}, methods


def test_credit_repair_data_stays_in_its_own_encrypted_database(api, env):
    me = api.get("/api/auth/me").json()["user"]
    api.patch(f"/api/users/{me['id']}", json={"can_access_sensitive": True}).raise_for_status()
    api.post("/api/credit-repair/enable", json={"acknowledge": True}).raise_for_status()
    marker = f"Synthetic Person {uuid.uuid4().hex[:6]}"
    client = api.post("/api/credit-repair/clients", json={"full_name": marker, "ssn_last4": "0000"}).json()["client"]
    assert client["reference"].startswith("CR-")

    # Encrypted at rest in the credit_repair database ...
    assert marker not in psql("credit_repair", "SELECT string_agg(full_name_enc, ',') FROM cr_clients")
    # ... absent from the main database ...
    dump = compose("exec", "-T", "postgres", "pg_dump", "-U", "postgres", "--data-only", "command_center", timeout=120)
    assert dump.returncode == 0 and marker not in dump.stdout
    # ... and the main application's database role cannot even connect to it.
    denied = compose("exec", "-T", "-e", f"PGPASSWORD={env['CC_DB_PASSWORD']}", "postgres",
                     "psql", "-h", "127.0.0.1", "-U", "command_center", "-d", "credit_repair", "-c", "SELECT 1")
    assert denied.returncode != 0 and "permission denied" in denied.stderr
    api.get(f"/api/credit-repair/clients/{client['id']}").raise_for_status()
    actions = {e["action"] for e in api.get("/api/credit-repair/access-log").json()["entries"]}
    assert {"create_client", "view_client"} <= actions
