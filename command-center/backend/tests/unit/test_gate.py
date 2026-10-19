"""cc-gate's request parsing and validation, and its allowlist matching the Command Center's."""

from __future__ import annotations

import importlib.util
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ssh import ops as cc_ops

from ..sshfake import FAKE_RUNNER, FAKE_SUDO, FAKE_WP, GATE, HELPER, REPO

RUNNER = REPO / "server" / "cc-wp"
INSTALLER = REPO / "server" / "install-gate.sh"
# CC_TEST_SUDO=<path> runs the sudo test against another build (e.g. 1.9.5p2, the EL9 version).
SUDO = os.environ.get("CC_TEST_SUDO", "/usr/bin/sudo")


def load(path):
    loader = SourceFileLoader(path.name.replace("-", "_"), str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


gate = load(GATE)
CONFIG = {"allow_changes": True, "sites": {"site-one": {"path": "/srv/site", "user": "daemon", "url": "https://one.test"}},
          "services": ["httpd", "php-fpm"], "restartable_services": ["httpd"]}


def test_allowlists_match():
    assert {op: (kind, tuple(args)) for op, (kind, args) in gate.OPS.items()} == {op: (o.kind, o.args) for op, o in cc_ops.OPS.items()}
    assert gate.SITE_RE.pattern == cc_ops.SITE_RE.pattern and gate.SLUG_RE.pattern == cc_ops.SLUG_RE.pattern
    assert gate.SERVICE_RE.pattern == cc_ops.SERVICE_RE.pattern and gate.VERSION_RE.pattern == cc_ops.VERSION_RE.pattern
    assert set(gate.SLOW_OPS) == {op for op, o in cc_ops.OPS.items() if o.slow}


@pytest.mark.parametrize("raw, message", [
    (None, "interactive sessions are not allowed"),
    ("", "interactive sessions are not allowed"),
    ("bash", "only 'cc-gate v1"),
    ("id; cat /etc/shadow", "only 'cc-gate v1"),
    ("scp -t /tmp", "only 'cc-gate v1"),
    ("internal-sftp", "only 'cc-gate v1"),
    ("cc-gate v2 hello", "only 'cc-gate v1"),
    ("cc-gate v1 eval 'phpinfo();'", "unknown operation"),
    ("cc-gate v1 hello extra", "takes 0 argument"),
    ("cc-gate v1 wp_plugin_list site-one; id", "takes 1 argument"),
    # 1.2.0: a plugin update names the exact version to install; "the latest" is never accepted.
    ("cc-gate v1 wp_plugin_update site-one akismet", "takes 3 argument"),
    ("cc-gate v1 wp_plugin_list 'site-one", "could not be parsed"),
    ("cc-gate v1 " + "x" * 3000, "too long"),
])
def test_requests_that_are_refused(raw, message):
    with pytest.raises(gate.Refused, match=message):
        gate.parse_request(raw)


@pytest.mark.parametrize("op, args, config_change, message", [
    ("wp_plugin_deactivate", {"site": "site-one", "slug": "akismet"}, {"allow_changes": False}, "changes are switched off"),
    ("wp_core_version", {"site": "other"}, {}, "not configured"),
    ("wp_core_version", {"site": "../etc"}, {}, "not configured"),
    ("wp_plugin_deactivate", {"site": "site-one", "slug": "akismet --skip-plugins"}, {}, "invalid plugin name"),
    ("wp_plugin_deactivate", {"site": "site-one", "slug": "../../wp-config"}, {}, "invalid plugin name"),
    ("wp_plugin_update", {"site": "site-one", "slug": "Akismet", "version": "5.4"}, {}, "invalid plugin name"),
    ("wp_plugin_update", {"site": "site-one", "slug": "akismet", "version": "latest"}, {}, "invalid plugin version"),
    ("wp_plugin_update", {"site": "site-one", "slug": "akismet", "version": "5.4 --force"}, {}, "invalid plugin version"),
    ("service_status", {"service": "sshd"}, {}, "not allowed"),
    ("service_restart", {"service": "php-fpm"}, {}, "may not be restarted"),
    ("wp_maintenance", {"site": "site-one", "onoff": "maybe"}, {}, "on or off"),
])
def test_arguments_that_are_refused(op, args, config_change, message):
    with pytest.raises(gate.Refused, match=message):
        gate.validate(op, args, {**CONFIG, **config_change})


def test_valid_requests_pass():
    assert gate.parse_request("cc-gate v1 wp_plugin_deactivate site-one akismet") == ("wp_plugin_deactivate", {"site": "site-one", "slug": "akismet"})
    gate.validate("wp_plugin_deactivate", {"site": "site-one", "slug": "akismet"}, CONFIG)
    gate.validate("service_restart", {"service": "httpd"}, CONFIG)
    # Read operations work even when changes are off.
    gate.validate("wp_plugin_list", {"site": "site-one"}, {**CONFIG, "allow_changes": False})


def test_command_center_builds_only_valid_requests():
    assert cc_ops.build_command("wp_plugin_deactivate", {"site": "site-one", "slug": "akismet"}) == "cc-gate v1 wp_plugin_deactivate site-one akismet"
    for op, args in (("eval", {}), ("hello", {"x": "1"}), ("wp_plugin_deactivate", {"site": "site-one", "slug": "a b"}),
                     ("wp_plugin_deactivate", {"site": "site-one", "slug": "$(id)"}), ("service_status", {"service": "x;y"})):
        with pytest.raises(cc_ops.InvalidRequest):
            cc_ops.build_command(op, args)


@pytest.fixture
def tmp_path():
    """A temporary folder the unprivileged "nobody" user can read (pytest's own is root-only)."""
    import shutil
    import tempfile
    from pathlib import Path

    path = Path(tempfile.mkdtemp(prefix="ccgate-test-"))
    path.chmod(0o755)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def run_gate(tmp_path, request, *, as_root=False, config=None, root_owned_check=False):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config or CONFIG))
    cfg.chmod(cfg.stat().st_mode | 0o444)  # readable by "nobody"; any extra write bits are kept
    harness = tmp_path / "harness.py"
    harness.write_text(
        "import importlib.util, sys\nfrom importlib.machinery import SourceFileLoader\n"
        "l = SourceFileLoader('g', sys.argv[1]); g = importlib.util.module_from_spec(importlib.util.spec_from_loader('g', l)); l.exec_module(g)\n"
        f"g.CONFIG_PATH = sys.argv[2]; g.REQUIRE_ROOT_OWNED_CONFIG = {root_owned_check}\nsys.exit(g.main())\n"
    )
    os.chmod(tmp_path, 0o755)
    env = {"PATH": "/usr/bin:/bin", "SSH_ORIGINAL_COMMAND": request}
    kwargs = {} if as_root else {"user": "nobody"}
    done = subprocess.run([sys.executable if as_root else "/usr/bin/python3", str(harness), str(GATE), str(cfg)],
                          capture_output=True, text=True, env=env, timeout=30, **kwargs)
    return done.returncode, json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to start the gate as another user")
def test_gate_refuses_to_run_as_root(tmp_path):
    code, answer = run_gate(tmp_path, "cc-gate v1 hello", as_root=True)
    assert code == 2 and answer["refused"] and "must not run as root" in answer["error"]


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to start the gate as another user")
def test_gate_refuses_a_config_it_could_have_changed(tmp_path):
    code, _ = run_gate(tmp_path, "cc-gate v1 hello", root_owned_check=True)
    assert code == 0  # owned by root (the test runs as root) and not writable by others
    (tmp_path / "config.json").chmod(0o666)
    code, answer = run_gate(tmp_path, "cc-gate v1 hello", root_owned_check=True)
    # run_gate rewrote the file (still 0666): writable by others is refused.
    assert code == 2 and "owned by root and not writable" in answer["error"]


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to start the gate as another user")
def test_hello_as_an_unprivileged_user(tmp_path):
    code, answer = run_gate(tmp_path, "cc-gate v1 hello")
    assert code == 0 and answer["ok"]
    result = answer["result"]
    assert result["user"] == "nobody" and result["uid"] != 0
    assert "wp_plugin_deactivate" in result["ops"] and result["sites"] == {"site-one": {"url": "https://one.test"}}
    code, answer = run_gate(tmp_path, "cc-gate v1 hello", config={**CONFIG, "allow_changes": False})
    assert "wp_plugin_deactivate" not in answer["result"]["ops"] and "wp_plugin_list" in answer["result"]["ops"]


def test_sites_never_run_as_root_or_as_the_gate_account():
    me = pwd.getpwuid(os.getuid()).pw_name
    for user, message in (("root", "not as root or the gate account"), (me, "not as root or the gate account"),
                          ("no-such-user-x", "does not exist")):
        config = {**CONFIG, "sites": {"site-one": {**CONFIG["sites"]["site-one"], "user": user}}}
        with pytest.raises(gate.Refused, match=message):
            gate.validate("wp_core_version", {"site": "site-one"}, config)


# ------------------------------------------------------------ running programs

@pytest.fixture
def budget(monkeypatch):
    def set_budget(seconds, grace=1):
        monkeypatch.setattr(gate, "STOP_GRACE_SECONDS", grace)
        gate.DEADLINE[0] = time.monotonic() + seconds
    yield set_budget
    gate.DEADLINE[0] = 0.0


def test_a_program_that_runs_out_of_time_is_asked_to_stop(tmp_path, budget):
    marker = tmp_path / "stopped"
    child = (f"import signal, sys, time; signal.signal(signal.SIGTERM, lambda *a: (open({str(marker)!r}, 'w').write('term'), "
             "sys.exit(0))); time.sleep(30)")
    budget(1)
    started = time.monotonic()
    with pytest.raises(gate.TimedOut, match="may be incomplete"):
        gate.run([sys.executable, "-c", child], {"max_output_bytes": 1000})
    assert time.monotonic() - started < 5 and marker.read_text() == "term"


def test_a_program_that_ignores_the_stop_request_is_killed_and_orphans_do_not_block(budget):
    # The shell ignores TERM, and a background child keeps the output pipes open after it is gone.
    budget(1, grace=1)
    started = time.monotonic()
    with pytest.raises(gate.TimedOut):
        gate.run(["/bin/sh", "-c", "trap '' TERM; (sleep 20) & sleep 20"], {"max_output_bytes": 1000})
    assert time.monotonic() - started < 6


def test_programs_run_in_their_own_session(budget):
    # sudo 1.8/1.9 (EL8/EL9) drops a SIGTERM sent from its own process group, so sharing the gate's would
    # mean stop() never reaches the command.
    budget(20)
    code, out, _ = gate.run([sys.executable, "-c", "import os; print(os.getsid(0), os.getpgrp())"], {"max_output_bytes": 1000})
    sid, pgrp = (int(x) for x in out.split())
    assert code == 0 and sid != os.getsid(0) and pgrp != os.getpgrp()


@pytest.mark.skipif(os.geteuid() != 0 or not os.access(SUDO, os.X_OK), reason="needs root and sudo")
def test_a_stop_request_reaches_the_command_through_sudo(tmp_path, budget, monkeypatch):
    marker = tmp_path / "stopped"
    os.chmod(tmp_path, 0o777)
    tag = "cc-gate-test-%d" % os.getpid()
    monkeypatch.setattr(gate, "CONFIG", {"sudo": SUDO})
    argv = gate.as_site_user({"user": "daemon"}, ["/bin/sh", "-c", f"trap 'echo term > {marker}; exit 0' TERM; while :; do sleep 0.1; done", tag])
    budget(1.5, grace=4)
    try:
        with pytest.raises(gate.TimedOut):
            gate.run(argv, {"max_output_bytes": 1000})
        assert marker.read_text().strip() == "term"
    finally:
        subprocess.run(["pkill", "-f", tag])


def test_output_is_capped_but_drained(budget):
    budget(20)
    code, out, err = gate.run([sys.executable, "-c", "import sys; sys.stdout.write('x' * 5000000); sys.stderr.write('y' * 5000)"],
                              {"max_output_bytes": 1000})
    assert code == 0 and out == "x" * 1000 and err == "y" * 250


def test_no_time_left_means_nothing_starts(budget):
    budget(-1)
    with pytest.raises(gate.TimedOut, match="before"):
        gate.run(["/bin/true"], {"max_output_bytes": 1000})


# ------------------------------------------------------------- answers parsed

@pytest.fixture
def site(monkeypatch):
    monkeypatch.setattr(gate, "CONFIG", {**CONFIG, "sudo": "/usr/bin/sudo", "wp_runner": "/usr/local/lib/cc-gate/cc-wp",
                                         "site_helper": "/x/cc-site-helper", "max_output_bytes": 1000})
    monkeypatch.setattr(gate, "SITE", CONFIG["sites"]["site-one"])
    monkeypatch.setattr(gate, "ARGS", {"site": "site-one"})
    answers = []
    monkeypatch.setattr(gate, "run", lambda argv, config: answers.pop(0))
    return answers


def test_wp_cli_runs_through_the_wrapper_as_the_site_user(site):
    argv = gate.wp({"path": "/srv/site", "user": "daemon"}, "core", "version")
    assert argv[:6] == ["/usr/bin/sudo", "-n", "-u", "daemon", "--", "/usr/local/lib/cc-gate/cc-wp"]
    assert "-H" not in argv and "--skip-plugins" in argv


@pytest.mark.parametrize("answer, expected", [
    ((0, "", ""), True),
    ((1, "", ""), False),
    ((1, "", "Error: Error establishing a database connection."), None),
    ((255, "PHP Fatal error: ...", ""), None),
])
def test_maintenance_status_is_only_off_when_wp_cli_says_so(site, answer, expected):
    site.append(answer)
    if expected is None:
        with pytest.raises(gate.Failed):
            gate.op_wp_maintenance_status({})
    else:
        assert gate.op_wp_maintenance_status({}) == {"active": expected}


def test_extra_core_files_fail_verification(site):
    site.append((0, "Warning: File should not exist: wp-includes/class-wp-cache-helper.php\nSuccess: WordPress installation verifies "
                 "against checksums.", ""))
    result = gate.op_wp_core_verify({})
    assert result["verified"] is False and result["extra_files"] == ["Warning: File should not exist: wp-includes/class-wp-cache-helper.php"]
    site.append((0, "Success: WordPress installation verifies against checksums.", ""))
    assert gate.op_wp_core_verify({})["verified"] is True
    site.append((1, "", "Error: RuntimeException: Failed to get url 'https://api.wordpress.org/core/checksums/1.0/?version=6.9.4': "
                 "cURL error 6: Could not resolve host"))
    assert gate.op_wp_core_verify({})["verified"] is None  # not checked is not "modified"
    site.append((1, "Warning: File doesn't verify against checksum: wp-includes/version.php\nError: WordPress installation doesn't verify "
                 "against checksums.", ""))
    assert gate.op_wp_core_verify({})["files"] == ["Warning: File doesn't verify against checksum: wp-includes/version.php"]


def test_reinstall_only_replaces_plugins_from_wordpress_org(site, monkeypatch):
    header = {"found": True, "update_uri": "https://updates.example.com/acme-forms"}
    monkeypatch.setattr(gate, "helper", lambda *a: header)
    with pytest.raises(gate.Refused, match="not a WordPress.org plugin"):
        gate.op_wp_plugin_reinstall({"slug": "acme-forms"})
    header["update_uri"] = "https://wordpress.org/plugins/acme-forms/"
    site.extend([(0, "1.0", ""), (0, "Success: Verified 0 of 1 plugins (1 skipped).",
                                  "Warning: Could not retrieve the checksums for version 1.0 of plugin acme-forms, skipping.")])
    with pytest.raises(gate.Refused, match="no checksums"):  # refused: nothing has been changed yet
        gate.op_wp_plugin_reinstall({"slug": "acme-forms"})
    header["update_uri"] = ""
    site.extend([(0, "1.0", ""), (1, "Warning: File doesn't verify against checksum: x.php", ""), (0, "Success: Installed 1 of 1 plugins.", "")])
    assert gate.op_wp_plugin_reinstall({"slug": "acme-forms"})["version"] == "1.0"
    header["found"] = False
    with pytest.raises(gate.Refused, match="not found"):
        gate.op_wp_plugin_reinstall({"slug": "acme-forms"})


def test_a_plugin_update_installs_exactly_the_approved_version(site, monkeypatch):
    header = {"found": True, "update_uri": ""}
    monkeypatch.setattr(gate, "helper", lambda *a: header)
    monkeypatch.setattr(gate, "wordpress_org_current", lambda slug: "5.4")
    calls = []
    answers = site
    monkeypatch.setattr(gate, "run", lambda argv, config: (calls.append(argv), answers.pop(0))[1])
    # installed 5.3, checksums known, update to 5.4, then the plugin reports 5.4
    site.extend([(0, "5.3", ""), (0, "Success: Verified 1 of 1 plugins.", ""), (0, "Success: Updated 1 of 1 plugins.", ""), (0, "5.4", "")])
    result = gate.op_wp_plugin_update({"slug": "akismet", "version": "5.4"})
    assert result["before"] == "5.3" and result["version"] == "5.4"
    update = next(argv for argv in calls if "update" in argv)
    assert update[-3:] == ["update", "akismet", "--version=5.4"]
    # WordPress.org delivered another version: the gate says so instead of reporting success.
    site.extend([(0, "5.3", ""), (0, "Success: Verified 1 of 1 plugins.", ""), (0, "Success: Updated 1 of 1 plugins.", ""), (0, "5.5", "")])
    with pytest.raises(gate.Failed, match="is version 5.5 after the update, not the approved 5.4"):
        gate.op_wp_plugin_update({"slug": "akismet", "version": "5.4"})


@pytest.mark.parametrize("installed, requested, current, message", [
    ("5.3", "2.5.0", "5.4", "2.5.0 is not a newer release"),  # a downgrade, e.g. to a known-vulnerable release
    ("5.3", "5.3.0", "5.4", "5.3.0 is not a newer release"),  # the same release
    ("5.4", "5.4", "5.4", "5.4 is not a newer release"),
    ("5.3", "5.3.5", "5.4", "current release of akismet is 5.4, not the requested 5.3.5"),  # an intermediate release
    ("5.3", "5.4", "5.5", "current release of akismet is 5.5, not the requested 5.4"),  # published since the approval
])
def test_a_plugin_update_installs_only_wordpress_orgs_current_release_and_only_forward(site, monkeypatch, installed, requested, current,
                                                                                       message):
    monkeypatch.setattr(gate, "helper", lambda *a: {"found": True, "update_uri": ""})
    monkeypatch.setattr(gate, "wordpress_org_current", lambda slug: current)
    calls = []
    monkeypatch.setattr(gate, "run", lambda argv, config: (calls.append(argv), site.pop(0))[1])
    site.extend([(0, installed, ""), (0, "Success: Verified 1 of 1 plugins.", "")])
    with pytest.raises(gate.Refused, match=message):
        gate.op_wp_plugin_update({"slug": "akismet", "version": requested})
    assert not any("update" in argv for argv in calls)  # refused before anything was installed


def test_releases_compare_by_their_numbers():
    assert gate.is_newer("5.10", "5.9") and gate.is_newer("5.4.1", "5.4") and gate.is_newer("6.0-beta1", "5.9.9")
    assert not gate.is_newer("5.4.0", "5.4") and not gate.is_newer("5.4-beta", "5.4") and not gate.is_newer("4.9", "5.0")
    assert cc_ops.is_newer_release("5.10", "5.9") and not cc_ops.is_newer_release("5.4.0", "5.4")
    assert not cc_ops.is_newer_release("latest", "5.3") and not cc_ops.is_newer_release("5.4", "")


def test_wordpress_orgs_current_release_is_read_from_wordpress_org(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "DEADLINE", [__import__("time").monotonic() + 30])
    monkeypatch.setattr(gate, "WPORG_PLUGIN_INFO", "file://" + str(tmp_path / "%s.json"))
    (tmp_path / "akismet.json").write_text('{"name": "Akismet", "version": "5.4"}')
    assert gate.wordpress_org_current("akismet") == "5.4"
    (tmp_path / "gone.json").write_text('{"error": "Plugin not found."}')
    with pytest.raises(gate.Refused, match="reports no current release for gone"):
        gate.wordpress_org_current("gone")
    with pytest.raises(gate.Refused, match="could not ask WordPress.org for the current release of missing"):
        gate.wordpress_org_current("missing")


def test_a_plugin_update_only_replaces_plugins_from_wordpress_org(site, monkeypatch):
    header = {"found": True, "update_uri": "https://updates.example.com/acme-forms"}
    monkeypatch.setattr(gate, "helper", lambda *a: header)
    with pytest.raises(gate.Refused, match="not a WordPress.org plugin; it is not updated from there"):
        gate.op_wp_plugin_update({"slug": "acme-forms", "version": "2.0"})
    header["update_uri"] = ""
    site.extend([(0, "1.0", ""), (0, "Success: Verified 0 of 1 plugins (1 skipped).",
                                  "Warning: Could not retrieve the checksums for version 1.0 of plugin acme-forms, skipping.")])
    with pytest.raises(gate.Refused, match="no checksums for acme-forms 1.0.*not updating"):
        gate.op_wp_plugin_update({"slug": "acme-forms", "version": "2.0"})
    assert site == []  # nothing was installed


def test_a_separate_wordpress_folder_can_be_configured(site, monkeypatch):
    monkeypatch.setattr(gate, "SITE", {**CONFIG["sites"]["site-one"], "siteurl": "https://one.test/wp"})
    site.extend([(0, "https://one.test", ""), (0, "https://one.test/wp", "")])
    assert gate.op_wp_site_urls({})["matches"] is True


@pytest.mark.parametrize("path, url, home, siteurl, configured, suspected, kept", [
    # WordPress in its own folder without "siteurl" in the configuration: restoring siteurl would break wp-admin.
    ("/home/acme/public_html/wp", "https://acme.com", "https://acme.com", "https://acme.com/wp", None, True, True),
    ("/home/acme/public_html/blog/wp/", "https://acme.com/blog/", "https://acme.com/blog", "https://acme.com/blog/wp/", None, True, True),
    # home changed too: a real mismatch (not suspected). home is restored, siteurl is not overwritten.
    ("/home/acme/public_html/wp", "https://acme.com", "http://evil.example", "https://acme.com/wp", None, False, True),
    ("/home/acme/public_html/wp", "https://acme.com", "https://acme.com", "http://evil.example/wp", None, False, False),
    ("/home/acme/public_html", "https://acme.com", "https://acme.com", "https://acme.com/wp", None, False, False),  # files not there
    ("/home/acme/public_html/mywp", "https://acme.com", "https://acme.com", "https://acme.com/wp", None, False, False),
    ("/home/acme/public_html/wp", "https://acme.com", "https://acme.com", "https://acme.com/wp?x=/wp", None, False, False),
    ("/home/acme/public_html/wp", "https://acme.com", "https://acme.com", "https://acme.com/wp", "https://acme.com", False, False),
    # A root install: its path ends in public_html too, but its web root would be the home folder or above it.
    ("/home/acme/public_html", "https://acme.com", "http://evil.example", "https://acme.com/public_html", None, False, False),
    ("/home/acme/public_html", "https://acme.com", "https://acme.com", "https://acme.com/public_html", None, False, False),
    ("/home/acme/public_html", "https://acme.com", "http://evil.example", "https://acme.com/acme/public_html", None, False, False),
    ("/home/acme/public_html", "https://acme.com", "https://acme.com", "https://acme.com/home/acme/public_html", None, False, False),
    # Not in the site user's home (/home/acme).
    ("/srv/acme/wp", "https://acme.com", "https://acme.com", "https://acme.com/wp", None, False, False),
    ("/home/acme2/public_html/wp", "https://acme.com", "https://acme.com", "https://acme.com/wp", None, False, False),
])
def test_a_probable_subdirectory_install_is_reported_and_its_siteurl_left_alone(site, monkeypatch, path, url, home, siteurl,
                                                                                configured, suspected, kept):
    monkeypatch.setattr(gate, "SITE", {"path": path, "user": "daemon", "url": url, "siteurl": configured})
    monkeypatch.setattr(gate, "site_home", lambda s: "/home/acme")
    answers = {"home": home, "siteurl": siteurl}
    calls = []

    def fake_run(argv, config):
        calls.append(argv)
        return 0, (answers[argv[-1]] if "get" in argv else "Success"), ""

    monkeypatch.setattr(gate, "run", fake_run)
    result = gate.op_wp_site_urls({})
    assert result["matches"] is False and result["subdirectory_install_suspected"] is suspected
    assert result.get("suspected_siteurl") == (siteurl.rstrip("/") if kept else None)  # also when home was changed
    calls.clear()
    if suspected:  # only siteurl differs, most likely because the configuration lacks "siteurl": nothing is written
        with pytest.raises(gate.Refused, match='WordPress appears to live in /wp .*set "siteurl" for this site') as refused:
            gate.op_wp_set_site_urls({})
        assert "(%s if it has no folder of its own)" % url.rstrip("/") in str(refused.value)
        assert not any("update" in argv for argv in calls)
    else:  # the redirect is always undone
        done = gate.op_wp_set_site_urls({})
        assert [argv[-3:] for argv in calls if "update" in argv] == [["update", "home", url.rstrip("/")]] + (
            [] if kept else [["update", "siteurl", configured or url.rstrip("/")]])
        assert done["siteurl_kept"] is kept and done["after_siteurl"] == (siteurl if kept else configured or url.rstrip("/"))


def test_the_site_users_home_comes_from_the_account_database(site, monkeypatch):
    assert gate.site_home({"user": "daemon"}) == pwd.getpwnam("daemon").pw_dir.rstrip("/")
    assert gate.site_home({"user": "no-such-user-cc"}) == ""
    monkeypatch.setattr(gate, "SITE", {"path": "/home/acme/public_html/wp", "user": "daemon", "url": "https://acme.com"})
    monkeypatch.setattr(gate, "site_home", lambda s: "")  # unknown, or "/"
    assert gate.wordpress_folder("https://acme.com/wp") is None
    monkeypatch.setattr(gate, "site_home", lambda s: "/home/acme")
    assert gate.wordpress_folder("https://acme.com/wp") == "/wp"


def admin_answers(monkeypatch, *answers):
    queue, calls = list(answers), []
    monkeypatch.setattr(gate, "run", lambda argv, config: (calls.append(argv), queue.pop(0))[1])
    return calls


def test_capability_holders_and_super_admins_are_listed_apart_from_admins(site, monkeypatch):
    owner = '{"ID":1,"user_login":"owner","user_registered":"2024-01-01 00:00:00"}'
    shadow = '{"ID":7,"user_login":"shadow","user_registered":"2026-10-01 00:00:00"}'
    calls = admin_answers(monkeypatch, (0, f"[{owner}]", ""), (0, "6.8.3", ""), (0, f"[{owner},{shadow}]", ""),
                          (0, '[{"user_login":"owner"},{"user_login":"netadmin"}]', ""))  # WP-CLI 2.12's super-admin JSON
    result = gate.op_wp_admins({})
    # A Command Center whose baseline has only "admins" must not see long-standing users of a custom role as new.
    assert [a["user_login"] for a in result["admins"]] == ["owner"]
    assert [a["user_login"] for a in result["capability_admins"]] == ["shadow"]  # not already listed (by ID)
    assert result["super_admins"] == ["owner", "netadmin"]
    assert "--capability__in=manage_options,promote_users" in calls[2] and "--role=administrator" not in calls[2]
    # Plain names are taken too; a single site has none.
    admin_answers(monkeypatch, (0, f"[{owner}]", ""), (0, "6.8.3", ""), (0, "[]", ""), (0, '["owner"]', ""))
    assert gate.op_wp_admins({})["super_admins"] == ["owner"]
    admin_answers(monkeypatch, (0, f"[{owner}]", ""), (0, "6.8.3", ""), (0, "[]", ""), (1, "", "Error: This is not a multisite installation."))
    assert gate.op_wp_admins({})["super_admins"] == []


def test_admin_capabilities_are_only_asked_from_wordpress_5_9(site, monkeypatch):
    # Before 5.9 WordPress ignores capability__in, and the query would list every user.
    calls = admin_answers(monkeypatch, (0, '[{"ID":1,"user_login":"owner"}]', ""), (0, "5.8.3", ""), (1, "", "Error: not a multisite"))
    result = gate.op_wp_admins({})
    assert [a["user_login"] for a in result["admins"]] == ["owner"]
    assert "capability_admins" not in result  # not checked, which is not the same as none (no baseline before 5.9)
    assert len(calls) == 3 and not any("--capability__in" in a for argv in calls for a in argv)
    assert gate.version_at_least("5.9", (5, 9)) and gate.version_at_least("6.10-beta1", (5, 9))
    assert not gate.version_at_least("5.8.12", (5, 9)) and not gate.version_at_least("unknown", (5, 9))


def test_super_admins_through_the_fake_servers_wp_cli(tmp_path, budget, monkeypatch):
    for name, body in (("wp-cli", FAKE_WP), ("cc-wp", FAKE_RUNNER), ("sudo", FAKE_SUDO)):
        (tmp_path / name).write_text(body)
        (tmp_path / name).chmod(0o755)
    monkeypatch.setattr(gate, "CONFIG", {**CONFIG, "sudo": str(tmp_path / "sudo"), "wp_runner": str(tmp_path / "cc-wp"),
                                         "max_output_bytes": 100000})
    monkeypatch.setattr(gate, "SITE", CONFIG["sites"]["site-one"])
    budget(20)
    result = gate.op_wp_admins({})
    assert result["super_admins"] == [] and result["capability_admins"] == []  # a single site; owner is already in "admins"
    (tmp_path / "super-admins").write_text("owner\nnetadmin\n")  # a multisite network
    assert gate.op_wp_admins({})["super_admins"] == ["owner", "netadmin"]


# ------------------------------------------------------------------ cc-wp

def run_runner(tmp_path, args, *, env=None):
    harness = tmp_path / "runner-harness.py"
    harness.write_text(
        "import importlib.util, sys\nfrom importlib.machinery import SourceFileLoader\n"
        "l = SourceFileLoader('w', sys.argv[1]); w = importlib.util.module_from_spec(importlib.util.spec_from_loader('w', l)); "
        "l.exec_module(w)\nw.CONFIG_PATH = sys.argv[2]; w.REQUIRE_ROOT_OWNED_CONFIG = False\nsys.exit(w.main(['cc-wp'] + sys.argv[3:]))\n"
    )
    return subprocess.Popen(["/usr/bin/python3", str(harness), str(RUNNER), str(tmp_path / "config.json")] + args, user="nobody",
                            env=env or {"PATH": "/usr/bin:/bin"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to start the wrapper as another user")
def test_wp_cli_gets_an_environment_the_site_cannot_influence(tmp_path):
    fake_wp = tmp_path / "wp"
    fake_wp.write_text("#!/bin/sh\nenv | sort\necho \"ARGS $*\"\npwd\n")
    fake_wp.chmod(0o755)
    (tmp_path / "config.json").write_text(json.dumps({"wp_cli": str(fake_wp), "sites": {"s": {"path": "/srv/s", "user": "nobody"}}}))
    hostile = {"PATH": "/tmp/evil:/usr/bin:/bin", "HOME": "/tmp/evil", "WP_CLI_CONFIG_PATH": "/tmp/evil.yml", "PHPRC": "/tmp/evil.ini",
               "WP_CLI_PACKAGES_DIR": "/tmp/evil"}
    proc = run_runner(tmp_path, ["--path=/srv/s", "core", "version"], env=hostile)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    env = dict(line.split("=", 1) for line in out.splitlines() if "=" in line and not line.startswith("ARGS"))
    assert "evil" not in out and "PHPRC" not in env
    assert env["HOME"].startswith("/tmp/cc-wp-")
    # Nothing WP-CLI reads lives in that home: the site user could plant a configuration or a cached download there.
    assert not [k for k, v in env.items() if k != "HOME" and v.startswith(env["HOME"])]
    assert env["WP_CLI_CONFIG_PATH"] == "/dev/null" and env["WP_CLI_CACHE_DIR"] == "/dev/null"
    assert env["WP_CLI_PACKAGES_DIR"] == "/usr/local/lib/cc-gate/no-packages"
    assert "ARGS --skip-packages --path=/srv/s core version" in out and out.splitlines()[-1] == "/"
    assert not os.path.exists(env["HOME"])  # the private home is removed afterwards
    # Only paths configured for this very user.
    proc = run_runner(tmp_path, ["--path=/srv/other", "core", "version"])
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 125 and "not a site configured for user nobody" in err


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to start the wrapper as another user")
def test_the_wrapper_passes_a_stop_request_on_to_wp_cli(tmp_path):
    marker = tmp_path / "out"
    os.chmod(tmp_path, 0o777)
    fake_wp = tmp_path / "wp"
    fake_wp.write_text(f"#!/bin/sh\ntrap 'echo term > {marker}; kill $!; exit 0' TERM\nsleep 30 >/dev/null 2>&1 &\nwait\n")
    fake_wp.chmod(0o755)
    (tmp_path / "config.json").write_text(json.dumps({"wp_cli": str(fake_wp), "sites": {"s": {"path": "/srv/s", "user": "nobody"}}}))
    proc = run_runner(tmp_path, ["--path=/srv/s", "plugin", "update", "x"])
    time.sleep(1.0)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=10)
    assert marker.read_text().strip() == "term" and proc.returncode == 0


# -------------------------------------------------------------- site helper

@pytest.fixture
def helper_site(tmp_path, monkeypatch):
    """The real site helper in this process, pretending to run as the site user "daemon"."""
    helper = load(HELPER)
    uid = pwd.getpwnam("daemon").pw_uid
    fake_os = SimpleNamespace(**{n: getattr(os, n) for n in dir(os) if not n.startswith("__")})
    fake_os.getuid = fake_os.geteuid = lambda: uid
    monkeypatch.setattr(helper, "os", fake_os)
    site_path = tmp_path / "site"
    (site_path / "wp-content" / "plugins").mkdir(parents=True)
    (tmp_path / "backups" / "site-one").mkdir(parents=True)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"sites": {"site-one": {"path": str(site_path), "user": "daemon", "url": "https://one.test"}},
                                  "backup_dir": str(tmp_path / "backups")}))
    helper.CONFIG_PATH, helper.REQUIRE_ROOT_OWNED_CONFIG = str(config), False
    return helper, site_path, tmp_path / "backups" / "site-one"


def helper_answer(helper, capsys, *args):
    code = helper.main(["cc-site-helper", *args])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_site_helper_refuses_bad_input():
    helper = load(HELPER)
    assert helper.main(["cc-site-helper", "rm", "site-one"]) == 1
    assert helper.main(["cc-site-helper", "debug_log_status", "../x"]) == 1
    assert helper.main(["cc-site-helper", "plugin_update_uri", "site-one", "../x"]) == 1
    assert helper.main(["cc-site-helper", "plugin_update_uri", "site-one"]) == 1


def test_debug_log_is_quarantined_and_a_link_is_reported_not_followed(helper_site, capsys):
    helper, site_path, backups = helper_site
    log = site_path / "wp-content" / "debug.log"
    log.write_text("PHP Warning: x\n")
    code, answer = helper_answer(helper, capsys, "debug_log_quarantine", "site-one")
    assert code == 0 and not log.exists() and Path(answer["result"]["moved_to"]).read_text() == "PHP Warning: x\n"
    log.symlink_to("/etc/hostname")
    code, answer = helper_answer(helper, capsys, "debug_log_status", "site-one")
    assert code == 0 and answer["result"]["exists"] is True and answer["result"]["type"] == "symlink"
    code, answer = helper_answer(helper, capsys, "debug_log_quarantine", "site-one")
    assert code == 1 and "not a regular file" in answer["error"] and log.is_symlink()


def test_quarantine_to_another_disk_checks_space_and_never_leaves_a_partial_copy(helper_site, monkeypatch):
    helper, site_path, backups = helper_site
    log = site_path / "wp-content" / "debug.log"
    log.write_text("x" * 1000)
    with pytest.raises(RuntimeError, match="not enough free space"):
        helper.quarantine(str(log), SimpleNamespace(st_dev=-1, st_size=10 ** 15), str(backups))
    assert log.exists() and list(backups.iterdir()) == []

    def broken_copy(src, dst):
        Path(dst).write_text("partial")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(helper.shutil, "copy2", broken_copy)
    with pytest.raises(OSError):
        helper.quarantine(str(log), SimpleNamespace(st_dev=-1, st_size=1000), str(backups))
    assert log.read_text() == "x" * 1000 and list(backups.iterdir()) == []


def test_plugin_update_uri_is_read_from_the_plugin_header(helper_site, capsys):
    helper, site_path, _ = helper_site
    plugin = site_path / "wp-content" / "plugins" / "acme-forms"
    plugin.mkdir()
    (plugin / "helpers.php").write_text("<?php // no header here\n")
    (plugin / "acme-forms.php").write_text("<?php\n/**\n * Plugin Name: Acme Forms\n * Update URI: https://updates.acme.test/forms\n */\n")
    code, answer = helper_answer(helper, capsys, "plugin_update_uri", "site-one", "acme-forms")
    assert answer["result"] == {"found": True, "file": "acme-forms.php", "update_uri": "https://updates.acme.test/forms"}
    code, answer = helper_answer(helper, capsys, "plugin_update_uri", "site-one", "missing")
    assert answer["result"] == {"found": False}


# ---------------------------------------------------------------- installer

FAKE_ID = """#!/bin/sh
# id, except that "alias" is a second name for uid 1 (daemon).
case "$1 $2" in "-u alias") echo 1; exit 0 ;; "alias ") exit 0 ;; esac
exec /usr/bin/id "$@"
"""


def sync_sites(tmp_path, sites, *args, systemctl="/usr/bin/systemctl"):
    """install-gate.sh --sync-sites under dash, from a copy whose system paths point into tmp_path."""
    text = INSTALLER.read_text()
    for name, path in (("GATE", "/usr/local/bin/cc-gate"), ("HELPER_DIR", "/usr/local/lib/cc-gate"), ("CONF_DIR", "/etc/cc-gate"),
                       ("SUDOERS", "/etc/sudoers.d/cc-gate"), ("LOG_DIR", "/var/log/cc-gate")):
        assert f"\n{name}={path}\n" in text
        text = text.replace(f"\n{name}={path}\n", f"\n{name}={tmp_path}{path}\n")
    (tmp_path / "install-gate.sh").write_text(text)
    for folder in ("etc/cc-gate", "etc/sudoers.d", "bin"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    (tmp_path / "etc/cc-gate/config.json").write_text(json.dumps({
        "sites": {key: {"path": f"/srv/{key}", "user": user, "url": f"https://{key}.test"} for key, user in sites.items()},
        "backup_dir": str(tmp_path / "backups"), "services": ["httpd"], "restartable_services": ["httpd"], "systemctl": systemctl}))
    (tmp_path / "bin/id").write_text(FAKE_ID)
    (tmp_path / "bin/id").chmod(0o755)
    done = subprocess.run(["dash", str(tmp_path / "install-gate.sh"), "--sync-sites", *args], capture_output=True, text=True,
                          env={"PATH": f"{tmp_path}/bin:/usr/sbin:/usr/bin:/sbin:/bin"}, timeout=60)
    return done, tmp_path / "etc/sudoers.d/cc-gate"


needs_installer = pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("dash") or not shutil.which("visudo"),
                                     reason="needs root, dash and visudo")


@needs_installer
def test_installer_writes_sudo_rules_for_the_configured_site_users(tmp_path):
    done, sudoers = sync_sites(tmp_path, {"one": "daemon", "two": "bin"}, "--user", "nobody")
    assert done.returncode == 0, done.stderr
    assert sudoers.read_text().splitlines()[1:] == [
        "Defaults:nobody !requiretty",
        f"nobody ALL=(bin,daemon) NOPASSWD: {tmp_path}/usr/local/lib/cc-gate/cc-wp, {tmp_path}/usr/local/lib/cc-gate/cc-site-helper",
        "nobody ALL=(root) NOPASSWD: /usr/bin/systemctl restart httpd",
    ]


@needs_installer
@pytest.mark.parametrize("site_user", ["daemon", "alias"])  # the gate account by name, or by uid under another name
def test_installer_writes_no_sudo_rules_when_a_site_runs_as_the_gate_account(tmp_path, site_user):
    # Its PHP would otherwise hold "sudo -u <every other site user> cc-wp <any WP-CLI arguments>", past cc-gate.
    (tmp_path / "etc/sudoers.d").mkdir(parents=True)
    (tmp_path / "etc/sudoers.d/cc-gate").write_text("# rules from an earlier run\n")
    done, sudoers = sync_sites(tmp_path, {"one": site_user, "two": "bin"}, "--user", "daemon")
    assert done.returncode == 1 and "site one runs as the gate account daemon" in done.stderr
    assert not sudoers.exists() and not (tmp_path / "backups/two").exists()


@needs_installer
@pytest.mark.parametrize("account", ["nobody\nnobody ALL=(ALL) NOPASSWD: ALL", "nobody\rx", "nobody\\nx"])
def test_installer_refuses_an_account_name_of_more_than_one_line(tmp_path, account):
    done, sudoers = sync_sites(tmp_path, {"one": "daemon"}, "--user", account)
    assert done.returncode == 1 and "invalid account name" in done.stderr
    assert not sudoers.exists() and not (tmp_path / "backups").exists()


@needs_installer
def test_installer_refuses_a_systemctl_path_that_would_add_sudo_rules(tmp_path):
    done, sudoers = sync_sites(tmp_path, {"one": "daemon"}, "--user", "nobody",
                               systemctl="/usr/bin/systemctl x\nnobody ALL=(ALL) NOPASSWD: /bin/sh #")
    assert done.returncode == 1 and "systemctl may only contain" in done.stderr and not sudoers.exists()


def test_a_quarantine_refused_before_anything_moved_says_so(helper_site, capsys, monkeypatch):
    helper, site_path, backups = helper_site
    (site_path / "wp-content" / "debug.log").write_text("x")
    backups.rmdir()  # no backup folder: checked before anything is moved
    code, answer = helper_answer(helper, capsys, "debug_log_quarantine", "site-one")
    assert code == 1 and answer["unchanged"] is True and "does not exist" in answer["error"]
    monkeypatch.setattr(gate, "run", lambda argv, config: (1, json.dumps(answer), ""))
    monkeypatch.setattr(gate, "CONFIG", {**CONFIG, "sudo": "/usr/bin/sudo", "site_helper": "/x/cc-site-helper", "max_output_bytes": 1000})
    with pytest.raises(gate.Refused, match="does not exist"):  # a refusal: nothing on the site changed
        gate.helper(CONFIG["sites"]["site-one"], "debug_log_quarantine", "site-one")
