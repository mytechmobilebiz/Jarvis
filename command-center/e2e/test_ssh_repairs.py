"""SSH and WordPress repairs on the running stack, against the DISPOSABLE wp-repair server.

wp-repair runs a real OpenSSH server with cc-gate installed by install-gate.sh
(restricted account "ccagent", forced command, validated sudo rules), real
WP-CLI and a throwaway WordPress. Nothing here touches any other site.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re

import asyncssh
import pytest

from .conftest import compose, wait_for, wait_task

SITE_URL = "http://wp-repair:8080"


def sh(*cmd: str) -> str:
    done = compose("exec", "-T", "wp-repair", *cmd)
    assert done.returncode == 0, done.stderr
    return done.stdout


def tamper(scenario: str) -> None:
    sh("cc-test-tamper", scenario)


def run(api, agent: str, capability: str, params: dict) -> dict:
    r = api.post(f"/api/agents/{agent}/tasks", json={"capability": capability, "params": params})
    assert r.status_code == 201, r.text
    task = r.json()["task"]
    return task if task["status"] == "awaiting_approval" else wait_task(api, task["id"], timeout=300)


def approve(api, task: dict, confirm: str = "") -> dict:
    assert task["status"] == "awaiting_approval", task
    approval = next(a for a in api.get("/api/approvals").json()["approvals"] if a["task_id"] == task["id"])
    r = api.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True, "confirm": confirm})
    assert r.status_code == 200, r.text
    return wait_task(api, task["id"], timeout=300)


@pytest.fixture(scope="module")
def cc_key(api) -> dict:
    r = api.post("/api/ssh/key", json={})
    if r.status_code == 409:
        return api.get("/api/ssh/key").json()["key"]
    assert r.status_code == 201, r.text
    return r.json()["key"]


@pytest.fixture(scope="module")
def server(api, cc_key) -> dict:
    sh("cc-test-authorize", cc_key["openssh"])  # what the owner does on a real server
    existing = next((s for s in api.get("/api/servers").json()["servers"] if s["name"] == "wp-repair-test"), None)
    if existing is None:
        r = api.post("/api/servers", json={"name": "wp-repair-test", "host": "wp-repair", "port": 22, "username": "ccagent", "environment": "test"})
        assert r.status_code == 201, r.text
        existing = r.json()["server"]
    scan = api.post(f"/api/servers/{existing['id']}/host-key/scan").json()
    on_server = sh("ssh-keygen", "-lf", "/etc/ssh/ssh_host_ed25519_key.pub").split()[1]
    assert scan["fingerprint"] == on_server  # the comparison a person makes before trusting
    r = api.post(f"/api/servers/{existing['id']}/host-key/trust", json={"fingerprint": scan["fingerprint"]})
    assert r.status_code == 200, r.text
    return r.json()["server"]


@pytest.fixture(scope="module")
def site(api, server) -> dict:
    tamper("reset")
    # As an admin does before linking a site: the connection test records the site keys the server's cc-gate offers.
    hello = run(api, "server_management", "test_connection", {"server_id": server["id"]})
    assert hello["status"] == "succeeded", hello["error"]
    existing = next((s for s in api.get("/api/sites").json()["sites"] if s["url"] == SITE_URL), None)
    if existing is None:
        r = api.post("/api/sites", json={"name": "Repair test site", "url": SITE_URL, "environment": "test"})
        assert r.status_code == 201, r.text
        existing = r.json()["site"]
    r = api.patch(f"/api/sites/{existing['id']}", json={"server_id": server["id"], "server_site_key": "wp-repair", "environment": "test"})
    assert r.status_code == 200, r.text
    yield r.json()["site"]
    tamper("reset")


def test_connection_over_real_openssh(api, server):
    task = run(api, "server_management", "test_connection", {"server_id": server["id"]})
    assert task["status"] == "succeeded", task["error"]
    hello = task["result"]["data"]["hello"]
    assert hello["user"] == "ccagent" and hello["uid"] != 0 and hello["allow_changes"] is True
    assert hello["sites"] == {"wp-repair": {"url": SITE_URL}}
    overview = run(api, "server_management", "server_overview", {"server_id": server["id"]})
    assert overview["status"] == "succeeded" and overview["result"]["data"]["disks"]


def test_the_key_can_do_nothing_but_run_cc_gate(cc_key):
    """Probe the restrictions with a throwaway key installed exactly like the Command Center's."""
    probe = asyncssh.generate_private_key("ssh-ed25519", comment="probe")
    sh("cc-test-authorize", probe.export_public_key("openssh").decode().strip())

    async def probes() -> dict:
        found: dict = {}
        opts = {"username": "ccagent", "client_keys": [probe], "known_hosts": None, "config": [], "agent_path": None}
        async with asyncssh.connect("127.0.0.1", 2222, **opts) as conn:
            found["hello"] = (await conn.run("cc-gate v1 hello", check=False)).exit_status
            found["arbitrary"] = (await conn.run("id; cat /etc/shadow", check=False)).stdout
            found["shell"] = (await conn.run(check=False)).stdout
            found["sudo"] = (await conn.run("sudo -n -l", check=False)).stdout
            found["eval"] = (await conn.run("cc-gate v1 wp_plugin_deactivate wp-repair 'akismet --exec=phpinfo();'", check=False)).stdout
            # 1.2.0: an update must name the approved version; "the latest" is not an option.
            found["unpinned"] = (await conn.run("cc-gate v1 wp_plugin_update wp-repair akismet", check=False)).stdout
            try:
                await conn.open_connection("wp-repair-db", 3306)
                found["tunnel"] = "opened"
            except asyncssh.ChannelOpenError:
                found["tunnel"] = "refused"
            try:
                async with conn.start_sftp_client():
                    found["sftp"] = "opened"
            except (asyncssh.Error, OSError):
                found["sftp"] = "refused"
        for user, kwargs in (("root", {"client_keys": [probe]}), ("ccagent", {"client_keys": None, "password": "x",
                                                                                "preferred_auth": ["password", "keyboard-interactive"]})):
            try:
                async with asyncssh.connect("127.0.0.1", 2222, username=user, known_hosts=None, config=[], agent_path=None, **kwargs):
                    found[f"login_{user}"] = "allowed"
            except asyncssh.PermissionDenied:
                found[f"login_{user}"] = "refused"
        return found

    try:
        # Its own thread: the browser tests leave an event loop running on this one.
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            found = pool.submit(lambda: asyncio.run(probes())).result(timeout=120)
    finally:
        sh("cc-test-authorize", cc_key["openssh"])
    assert found["hello"] == 0
    assert "only 'cc-gate v1" in found["arbitrary"] and "root:" not in found["arbitrary"]
    assert "interactive sessions are not allowed" in found["shell"]
    assert "only 'cc-gate v1" in found["sudo"]
    assert "takes 2 argument" in found["eval"] or "invalid plugin name" in found["eval"]
    assert found["tunnel"] == "refused" and found["sftp"] == "refused"
    assert "takes 3 argument" in found["unpinned"]
    assert found["login_root"] == "refused" and found["login_ccagent"] == "refused"


def test_cloaking_malware_is_found_and_the_plugin_deactivated(api, site):
    tamper("cloak")
    scan = run(api, "wordpress_security", "security_scan", {"site_id": site["id"]})
    codes = {f["code"]: f["severity"] for f in scan["result"]["findings"]}
    assert codes.get("conditional_redirect") == "critical" and codes.get("obfuscated_script") == "warning"
    diag = run(api, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
    plugins = {p["name"]: p["status"] for p in diag["result"]["data"]["diagnosis"]["plugins"]}
    assert plugins["cc-test-cloak"] == "active"

    task = run(api, "wordpress_security", "repair_deactivate_plugin", {"site_id": site["id"], "slug": "cc-test-cloak"})
    done = approve(api, task)
    assert done["status"] == "succeeded", done["error"]
    assert "database backup: /var/lib/cc-gate/backups/wp-repair/" in done["summary"] and "now inactive" in done["summary"]
    rescan = run(api, "wordpress_security", "security_scan", {"site_id": site["id"]})
    assert "conditional_redirect" not in {f["code"] for f in rescan["result"]["findings"]}


def test_a_plugin_that_is_not_from_wordpress_org_is_never_reinstalled_from_there(api, site):
    tamper("cloak")  # a local plugin WordPress.org has never heard of
    # The approval names the plugin's version as a recent diagnosis found it.
    assert run(api, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})["status"] == "succeeded"
    done = approve(api, run(api, "wordpress_security", "repair_reinstall_plugin", {"site_id": site["id"], "slug": "cc-test-cloak"}))
    assert done["status"] == "failed" and "no checksums" in done["error"], done["error"]
    assert "cc-test-cloak.php" in sh("ls", "/var/www/html/wp-content/plugins/cc-test-cloak/")  # untouched
    tamper("reset")


def test_wp_cli_runs_with_a_clean_environment_as_the_site_user(api, site):
    # A WP-CLI config in the site user's home runs code before every command (what malware on the site could plant).
    home = sh("sh", "-c", "getent passwd www-data | cut -d: -f6").strip()
    sh("sh", "-c", f"mkdir -p {home}/.wp-cli && printf 'require:\\n  - {home}/.wp-cli/evil.php\\n' > {home}/.wp-cli/config.yml "
       f"&& echo '<?php file_put_contents(\"/tmp/cc-wp-poisoned\", \"x\");' > {home}/.wp-cli/evil.php && chown -R www-data {home}/.wp-cli")
    poisoned = "test -e /tmp/cc-wp-poisoned && echo poisoned || echo clean"
    try:
        # Plain WP-CLI as that user does load it ...
        sh("su", "-s", "/bin/sh", "-c", "cd /var/www/html && wp option get home", "www-data")
        assert sh("sh", "-c", poisoned).strip() == "poisoned"
        sh("rm", "-f", "/tmp/cc-wp-poisoned")
        # ... the gate's WP-CLI does not.
        diag = run(api, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
        assert diag["status"] == "succeeded", diag["error"]
        assert sh("sh", "-c", poisoned).strip() == "clean"
        assert not diag["result"]["data"]["diagnosis"]["errors"]
    finally:
        sh("rm", "-rf", f"{home}/.wp-cli", "/tmp/cc-wp-poisoned")


def test_hijacked_site_address_is_found_and_repaired(api, site):
    tamper("redirect")  # sets WordPress's home option to http://evil.invalid
    check = run(api, "monitoring", "check_site", {"site_id": site["id"], "deep": False})
    assert check["result"]["data"]["report"]["status"] == "down"
    # WordPress's own canonical redirect then sends visitors to a broken address.
    assert "after redirecting to http://wp-repair/" in check["summary"]
    diag = run(api, "wordpress_security", "wp_diagnose", {"site_id": site["id"]})
    codes = {f["code"]: f["severity"] for f in diag["result"]["findings"]}
    assert codes.get("site_url_mismatch") == "critical", codes
    done = approve(api, run(api, "wordpress_security", "repair_restore_site_url", {"site_id": site["id"]}))
    assert done["status"] == "succeeded", done["error"]
    assert "was home http://evil.invalid" in done["summary"] and "site responds: up" in done["summary"]
    home = sh("su", "-s", "/bin/sh", "-c", "cd /var/www/html && wp option get home", "www-data").strip()
    assert home == SITE_URL


def test_public_debug_log_is_quarantined(api, site):
    tamper("debug-log")
    scan = run(api, "wordpress_security", "security_scan", {"site_id": site["id"]})
    assert "debug_log_present" in {f["code"] for f in scan["result"]["findings"]}
    done = approve(api, run(api, "wordpress_security", "repair_quarantine_debug_log", {"site_id": site["id"]}))
    assert done["status"] == "succeeded", done["error"]
    assert "moved to /var/lib/cc-gate/backups/wp-repair/debug-" in done["summary"]
    listing = sh("ls", "/var/lib/cc-gate/backups/wp-repair/")
    assert re.search(r"debug-\d{8}T\d{6}Z\.log", listing) and re.search(r"wp-repair-\d{8}T\d{6}Z-[0-9a-f]{8}\.sql", listing)
    assert "debug.log" not in sh("ls", "/var/www/html/wp-content/")


def test_service_restart_goes_through_the_sudo_rule(api, server):
    done = approve(api, run(api, "server_management", "restart_service", {"server_id": server["id"], "service": "php-web"}))
    assert done["status"] == "succeeded", done["error"]
    assert "restart php-web" in sh("cat", "/var/log/cc-test-systemctl.log")  # the shim refuses unless it runs as root


def test_production_needs_the_typed_name(api, server, site):
    api.patch(f"/api/servers/{server['id']}", json={"environment": "production"}).raise_for_status()
    try:
        task = run(api, "wordpress_security", "repair_flush_caches", {"site_id": site["id"]})
        approval = next(a for a in api.get("/api/approvals").json()["approvals"] if a["task_id"] == task["id"])
        assert approval["details"]["production"] is True
        r = api.post(f"/api/approvals/{approval['id']}/decision", json={"approve": True})
        assert r.status_code == 400
        done = approve(api, task, confirm=site["name"])
        assert done["status"] == "succeeded", done["error"]
    finally:
        # Out of production needs the server's name typed, as in the dashboard.
        api.patch(f"/api/servers/{server['id']}", json={"environment": "test", "confirm": server["name"]}).raise_for_status()


def test_every_command_is_audited_on_both_sides(api, server):
    commands = api.get(f"/api/servers/{server['id']}/commands?limit=1000").json()["commands"]
    changes = [c for c in commands if c["kind"] == "change"]
    assert changes and all(c["approved_by"] for c in changes)
    gate_log = [line for line in sh("cat", "/var/log/cc-gate/gate.log").splitlines() if line.strip()]
    logged_ops = [re.search(r'"op": "([a-z_]+)"', line).group(1) for line in gate_log if '"op": "' in line]
    for op in {c["op"] for c in commands if c["ok"]}:
        assert op in logged_ops


def test_an_impostor_host_key_is_refused(api, cc_key):
    r = api.post("/api/servers", json={"name": "impostor-test", "host": "wp-repair", "port": 22, "username": "ccagent", "environment": "test"})
    server = r.json()["server"] if r.status_code == 201 else next(s for s in api.get("/api/servers").json()["servers"] if s["name"] == "impostor-test")
    api.patch(f"/api/servers/{server['id']}", json={"enabled": True}).raise_for_status()  # left disabled by an earlier run
    fake = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh").decode()
    # From the second run on this replaces the key an earlier run pinned, which needs the server's name typed.
    api.post(f"/api/servers/{server['id']}/host-key/trust", json={"host_key": fake, "confirm": server["name"]}).raise_for_status()
    before = sh("wc", "-l", "/var/log/cc-gate/gate.log").split()[0]
    task = run(api, "server_management", "test_connection", {"server_id": server["id"]})
    assert task["status"] == "failed" and "different host key" in task["error"]
    assert sh("wc", "-l", "/var/log/cc-gate/gate.log").split()[0] == before  # nothing reached the gate
    wait_for(lambda: any(e["kind"] == "server.host_key_mismatch" for e in api.get("/api/events").json()["events"]), timeout=10)
    api.patch(f"/api/servers/{server['id']}", json={"enabled": False}).raise_for_status()
