"""cc-gate's request parsing and validation, and its allowlist matching the Command Center's."""

from __future__ import annotations

import importlib.util
import json
import os
import pwd
import signal
import subprocess
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ssh import ops as cc_ops

from ..sshfake import GATE, HELPER, REPO

RUNNER = REPO / "server" / "cc-wp"


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
    assert gate.SERVICE_RE.pattern == cc_ops.SERVICE_RE.pattern
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
    ("wp_plugin_update", {"site": "site-one", "slug": "Akismet"}, {}, "invalid plugin name"),
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


def test_reinstall_only_replaces_plugins_from_wordpress_org(site, monkeypatch):
    header = {"found": True, "update_uri": "https://updates.example.com/acme-forms"}
    monkeypatch.setattr(gate, "helper", lambda *a: header)
    with pytest.raises(gate.Refused, match="not a WordPress.org plugin"):
        gate.op_wp_plugin_reinstall({"slug": "acme-forms"})
    header["update_uri"] = "https://wordpress.org/plugins/acme-forms/"
    site.extend([(0, "1.0", ""), (1, "", "Warning: Could not retrieve the checksums for version 1.0 of plugin acme-forms, skipping.")])
    with pytest.raises(gate.Failed, match="no checksums"):
        gate.op_wp_plugin_reinstall({"slug": "acme-forms"})
    header["update_uri"] = ""
    site.extend([(0, "1.0", ""), (1, "Warning: File doesn't verify against checksum: x.php", ""), (0, "Success: Installed 1 of 1 plugins.", "")])
    assert gate.op_wp_plugin_reinstall({"slug": "acme-forms"})["version"] == "1.0"
    header["found"] = False
    with pytest.raises(gate.Failed, match="not found"):
        gate.op_wp_plugin_reinstall({"slug": "acme-forms"})


def test_a_separate_wordpress_folder_can_be_configured(site, monkeypatch):
    monkeypatch.setattr(gate, "SITE", {**CONFIG["sites"]["site-one"], "siteurl": "https://one.test/wp"})
    site.extend([(0, "https://one.test", ""), (0, "https://one.test/wp", "")])
    assert gate.op_wp_site_urls({})["matches"] is True


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
    assert env["HOME"].startswith("/tmp/cc-wp-") and env["WP_CLI_CONFIG_PATH"].startswith(env["HOME"])
    assert env["WP_CLI_PACKAGES_DIR"].startswith(env["HOME"]) and env["WP_CLI_CACHE_DIR"].startswith(env["HOME"])
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
