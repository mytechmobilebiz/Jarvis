"""cc-gate's request parsing and validation, and its allowlist matching the Command Center's."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader

import pytest

from app.ssh import ops as cc_ops

from ..sshfake import GATE, HELPER


def load(path):
    loader = SourceFileLoader(path.name.replace("-", "_"), str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


gate = load(GATE)
CONFIG = {"allow_changes": True, "sites": {"site-one": {"path": "/srv/site", "user": "siteuser", "url": "https://one.test"}},
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


def test_site_helper_refuses_bad_input():
    helper = load(HELPER)
    assert helper.main(["cc-site-helper", "rm", "site-one"]) == 1
    assert helper.main(["cc-site-helper", "debug_log_status", "../x"]) == 1
