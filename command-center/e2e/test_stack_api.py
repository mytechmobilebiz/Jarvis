"""Milestone 1 acceptance: the running stack, through its public entry point (the dashboard's /api)."""

from __future__ import annotations

import json
import re

import httpx

from .conftest import compose, wait_for, wait_request, wait_task, worker_access_lines


def test_health_is_public_and_minimal(base_url):
    r = httpx.get(f"{base_url}/api/health", trust_env=False)
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_everything_else_requires_sign_in(base_url):
    for path in ("/api/system/status", "/api/sites", "/api/tasks", "/api/audit"):
        assert httpx.get(f"{base_url}{path}", trust_env=False).status_code == 401


def test_dashboard_sends_security_headers(base_url):
    r = httpx.get(f"{base_url}/", trust_env=False)
    assert r.status_code == 200
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert "Infinite AI Command Center" in r.text


def test_platform_status(api):
    status = api.get("/api/system/status").json()
    assert status["database"]["ok"]
    assert any(w["alive"] for w in status["workers"]), status["workers"]
    assert status["llm"]["provider"] in ("rule-based", "anthropic")


def test_agents_registered(api):
    names = {a["name"] for a in api.get("/api/agents").json()["agents"]}
    assert {"supervisor", "monitoring", "wordpress_security", "server_management"} <= names


def test_event_round_trip_through_n8n(api):
    event = api.post("/api/integrations/n8n/test").json()["event"]
    acked = wait_for(
        lambda: (e := api.get(f"/api/events/{event['id']}").json()["event"])["n8n_ack_at"] and e,
        timeout=90,
        what="n8n acknowledgement",
    )
    assert acked["forward_status"] == "sent"
    assert acked["n8n_execution_id"]


def test_wordpress_monitor_reads_local_wordpress(api, test_site):
    task = api.post(f"/api/sites/{test_site['id']}/check", json={"deep": True}).json()["task"]
    done = wait_task(api, task["id"])
    assert done["status"] == "succeeded", done["error"]
    site = api.get(f"/api/sites/{test_site['id']}").json()["site"]
    check = site["last_check"]
    assert site["last_status"] == "up"
    assert check["is_wordpress"] is True
    assert re.match(r"^\d+\.\d+", check["wp_version"])
    codes = {f["code"] for f in check["findings"]}
    assert "xmlrpc_enabled" in codes and "no_https" in codes
    assert check["details"]["site_name"] == "Command Center Test Site"


def test_monitor_only_ever_sent_get_requests(api, test_site):
    """Read-only, verified from the test WordPress's own Apache access log."""
    api.post(f"/api/sites/{test_site['id']}/check", json={"deep": True})
    lines = wait_for(lambda: len(worker_access_lines()) >= 7 and worker_access_lines(), timeout=60, what="worker requests in the access log")
    methods = {m.group(1) for line in lines if (m := re.search(r'"([A-Z]+) /', line))}
    assert methods == {"GET"}, methods
    assert any("/wp-json/wp/v2/users" in line for line in lines)


def test_supervisor_plans_and_completes_a_request(api, test_site):
    req = api.post("/api/supervisor/requests", json={"message": "Check all websites"}).json()["request"]
    detail = wait_request(api, req["id"])
    r = detail["request"]
    assert r["status"] == "completed"
    assert r["plan"]["steps"][0]["agent"] == "monitoring"
    assert "Checked" in r["summary"]
    assert all(t["status"] == "succeeded" for t in detail["tasks"])


def test_n8n_workflow_can_ask_the_supervisor(api):
    before = {r["id"] for r in api.get("/api/supervisor/requests?limit=200").json()["requests"]}
    run = compose(
        "exec", "-T", "-e", "N8N_RUNNERS_BROKER_PORT=5699", "-e", "N8N_PORT=5698",
        "n8n", "n8n", "execute", "--id", "ccDailyBrief0001",
    )
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]
    assert '"status": "success"' in run.stdout
    new = wait_for(
        lambda: [r for r in api.get("/api/supervisor/requests?limit=200").json()["requests"] if r["id"] not in before],
        timeout=30,
        what="request created by n8n",
    )
    assert new[0]["source"] == "n8n" and new[0]["requested_by"] == "service:n8n"
    detail = wait_request(api, new[0]["id"])
    assert detail["request"]["status"] == "completed"
    assert any(t["capability"] == "daily_briefing" and t["status"] == "succeeded" for t in detail["tasks"])


def test_service_token_cannot_approve_or_read(base_url, service_headers):
    with httpx.Client(base_url=base_url, headers=service_headers, trust_env=False) as svc:
        assert svc.post("/api/approvals/1/decision", json={"approve": True}).status_code == 403
        assert svc.get("/api/sites").status_code == 403
        assert svc.post("/api/integrations/n8n/ack", json={"event_id": 999999}).status_code == 404


def test_audit_log_records_actions(api):
    actions = {e["action"] for e in api.get("/api/audit?limit=500").json()["entries"]}
    assert {"auth.login", "supervisor.request", "integration.n8n_test"} <= actions


def test_n8n_editor_is_up_and_only_on_localhost(env):
    r = httpx.get(f"http://127.0.0.1:{env.get('N8N_PORT', '5680')}/healthz/readiness", trust_env=False)
    assert r.status_code == 200
    ports = compose("port", "n8n", "5678").stdout.strip()
    assert ports.startswith("127.0.0.1:"), ports
    published = json.loads(compose("ps", "--format", "json", "postgres").stdout.splitlines()[0]).get("Publishers") or []
    assert not [p for p in published if p.get("PublishedPort")], "PostgreSQL must not be published"
