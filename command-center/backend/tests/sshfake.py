"""An SSH server for tests that behaves like OpenSSH with a forced command.

It accepts one public key for one user, ignores whatever command the client
asks for, and runs the real server/cc-gate instead, as the unprivileged user
"nobody", with SSH_ORIGINAL_COMMAND set - exactly what sshd does for an
authorized_keys line with command="cc-gate". WP-CLI, cc-wp, sudo and
systemctl are small fakes that record their arguments (the real cc-wp and
cc-site-helper are tested in tests/unit/test_gate.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

import asyncssh

REPO = Path(__file__).resolve().parents[2]
GATE = REPO / "server" / "cc-gate"
HELPER = REPO / "server" / "cc-site-helper"

HARNESS = """
import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader("cc_gate", sys.argv[1])
gate = importlib.util.module_from_spec(importlib.util.spec_from_loader("cc_gate", loader))
loader.exec_module(gate)
gate.CONFIG_PATH = sys.argv[2]
gate.REQUIRE_ROOT_OWNED_CONFIG = False  # the test config lives in a temp folder
# WordPress.org's plugin information: wporg-<slug>.json files next to the config instead of the internet.
gate.WPORG_PLUGIN_INFO = "file://" + os.path.join(os.path.dirname(sys.argv[2]), "wporg-%s.json")
# The fake site (<temp folder>/wp) is not in daemon's home; the folder holding the temp folder stands in for it.
gate.site_home = lambda site: os.path.dirname(os.path.dirname(sys.argv[2]))
sys.exit(gate.main())
"""

FAKE_WP = r"""#!/bin/sh
# Fake WP-CLI: records its arguments, answers a few commands.
echo "$*" >> "$(dirname "$0")/wp-calls.log"
while [ $# -gt 0 ]; do case "$1" in --*) shift ;; *) break ;; esac; done
case "$1 $2" in
  "core version") echo "6.8.3" ;;
  "core verify-checksums") echo "Success: WordPress installation verifies against checksums." ;;
  "plugin list")
    D="$(dirname "$0")"; a=$(cat "$D/plugin-akismet" 2>/dev/null || echo active); e=$(cat "$D/plugin-evil-cloak" 2>/dev/null || echo active)
    v=$(cat "$D/version-akismet" 2>/dev/null || echo 5.3); [ "$v" = 5.3 ] && u=available || u=none
    m=""; [ -f "$D/mu-plugin" ] && m=",{\"name\":\"$(cat "$D/mu-plugin")\",\"status\":\"must-use\",\"version\":\"\",\"update\":\"none\",\"update_version\":\"\"}"
    echo "[{\"name\":\"akismet\",\"status\":\"$a\",\"version\":\"$v\",\"update\":\"$u\",\"update_version\":\"5.4\"},{\"name\":\"evil-cloak\",\"status\":\"$e\",\"version\":\"1.0\",\"update\":\"none\",\"update_version\":\"\"}$m]" ;;
  "plugin update")
    D="$(dirname "$0")"; [ -f "$D/update-fails" ] && { echo "Error: Download failed." >&2; exit 1; }
    # The version asked for (--version=X), or what an update-installs file says WordPress.org delivered instead.
    v=$(printf '%s\n' "$@" | sed -n 's/^--version=//p'); [ -f "$D/update-installs" ] && v=$(cat "$D/update-installs")
    echo "${v:-5.4}" > "$D/version-$3"; echo "Success: Updated 1 of 1 plugins." ;;
  "super-admin list")
    # A multisite network when a super-admins file (one login per line) exists; printed the way WP-CLI 2.12 does.
    D="$(dirname "$0")"; [ -f "$D/super-admins" ] || { echo "Error: This is not a multisite installation." >&2; exit 1; }
    awk 'BEGIN { printf "[" } { printf "%s{\"user_login\":\"%s\"}", (NR > 1 ? "," : ""), $0 } END { print "]" }' "$D/super-admins" ;;
  "plugin deactivate") echo inactive > "$(dirname "$0")/plugin-$3"; echo "Success: Deactivated 1 of 1 plugins." ;;
  "plugin activate") echo active > "$(dirname "$0")/plugin-$3"; echo "Success: Activated 1 of 1 plugins." ;;
  "theme list") echo '[{"name":"astra","status":"active","version":"4.0","update":"none","update_version":""}]' ;;
  "user list")
    [ -f "$(dirname "$0")/users-broken" ] && { echo "Error: Error establishing a database connection." >&2; exit 1; }
    # Users found by capability (another role with administrator powers): from a capability-users file (a JSON list).
    case "$*" in *--capability__in=*) [ -f "$(dirname "$0")/capability-users" ] && { cat "$(dirname "$0")/capability-users"; exit 0; } ;; esac
    echo '[{"ID":1,"user_login":"owner","user_registered":"2024-01-01 00:00:00"}]' ;;
  "option get") cat "$(dirname "$0")/option-$3" 2>/dev/null || echo "http://site.test" ;;
  "option update") printf '%s' "$4" > "$(dirname "$0")/option-$3"; echo "Success: Updated '$3' option." ;;
  "maintenance-mode is-active") exit 1 ;;
  "plugin get") cat "$(dirname "$0")/version-$3" 2>/dev/null || { [ "$3" = akismet ] && echo 5.3 || echo 1.0; } ;;
  *) echo "Success: $*" ;;
esac
"""

FAKE_RUNNER = r"""#!/bin/sh
# Fake cc-wp: like the real one, WP-CLI with an empty environment and no packages.
echo "$*" >> "$(dirname "$0")/runner-calls.log"
exec env -i PATH=/usr/bin:/bin "$(dirname "$0")/wp-cli" --skip-packages "$@"
"""

FAKE_HELPER = r"""#!/bin/sh
# Fake cc-site-helper: a debug.log in the fake site folder; plugin headers from update-uri-<plugin> files.
D="$(dirname "$0")"; LOG="$D/wp/wp-content/debug.log"
case "$1" in
  plugin_update_uri) echo "{\"ok\": true, \"result\": {\"found\": true, \"update_uri\": \"$(cat "$D/update-uri-$3" 2>/dev/null)\"}}" ;;
  debug_log_status) if [ -f "$LOG" ]; then echo "{\"ok\": true, \"result\": {\"exists\": true, \"bytes\": $(wc -c < "$LOG")}}"; else echo '{"ok": true, "result": {"exists": false}}'; fi ;;
  debug_log_quarantine) mv "$LOG" "$D/backups/debug-moved.log" && echo "{\"ok\": true, \"result\": {\"exists\": true, \"moved_to\": \"$D/backups/debug-moved.log\"}}" ;;
esac
"""

FAKE_SUDO = r"""#!/bin/sh
# Fake sudo: records the call, drops its options and runs the command.
echo "$*" >> "$(dirname "$0")/sudo-calls.log"
while [ $# -gt 0 ]; do case "$1" in -u) shift 2 ;; --) shift; break ;; -*) shift ;; *) break ;; esac; done
exec "$@"
"""

FAKE_SYSTEMCTL = r"""#!/bin/sh
echo "$*" >> "$(dirname "$0")/systemctl-calls.log"
case "$1" in
  is-active) [ "$2" = "httpd" ] && echo active || echo inactive ;;
  restart) exit 0 ;;
esac
"""


class FakeGateServer:
    """Start with `async with FakeGateServer(authorized_key) as srv:`; srv.port, srv.host_key, srv.config."""

    def __init__(self, authorized_openssh: str, *, allow_changes: bool = True, username: str = "ccagent") -> None:
        self.authorized = asyncssh.import_public_key(authorized_openssh)
        self.username = username
        self.dir = Path(tempfile.mkdtemp(prefix="ccgate-"))
        os.chmod(self.dir, 0o777)
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.requests: list[str | None] = []
        self.config = {
            "allow_changes": allow_changes,
            # The gate runs as "nobody"; a site must have its own user (the fake sudo does not switch users).
            "sites": {"site-one": {"path": str(self.dir / "wp"), "user": "daemon", "url": "http://site.test"}},
            "services": ["httpd", "php-fpm"],
            "restartable_services": ["httpd"],
            "wp_cli": str(self.dir / "wp-cli"),
            "wp_runner": str(self.dir / "cc-wp"),
            "systemctl": str(self.dir / "systemctl"),
            "sudo": str(self.dir / "sudo"),
            "site_helper": str(self.dir / "cc-site-helper"),
            "backup_dir": str(self.dir / "backups"),
            "log_file": str(self.dir / "gate.log"),
            "timeout_seconds": 20,
        }
        self._listener: asyncssh.SSHAcceptor | None = None

    def write_config(self) -> None:
        (self.dir / "config.json").write_text(json.dumps(self.config))

    async def __aenter__(self) -> FakeGateServer:
        for name, body in (("wp-cli", FAKE_WP), ("cc-wp", FAKE_RUNNER), ("systemctl", FAKE_SYSTEMCTL), ("sudo", FAKE_SUDO),
                           ("cc-site-helper", FAKE_HELPER), ("harness.py", HARNESS)):
            path = self.dir / name
            path.write_text(body)
            path.chmod(0o755)
        shutil.copy(GATE, self.dir / "cc-gate")
        (self.dir / "wp").mkdir()
        (self.dir / "backups").mkdir()
        (self.dir / "wporg-akismet.json").write_text('{"name": "Akismet", "version": "5.4"}')  # WordPress.org's current release
        for p in (self.dir / "wp", self.dir / "backups"):
            p.chmod(0o777)
        self.write_config()
        outer = self

        class Server(asyncssh.SSHServer):
            def begin_auth(self, username: str) -> bool:
                return True

            def password_auth_supported(self) -> bool:
                return False

            def public_key_auth_supported(self) -> bool:
                return True

            def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
                return username == outer.username and key.public_data == outer.authorized.public_data

        async def handle(process: asyncssh.SSHServerProcess) -> None:
            outer.requests.append(process.command)
            env = {"PATH": "/usr/bin:/bin", "SSH_CLIENT": "127.0.0.1 50000 22"}
            if process.command is not None:
                env["SSH_ORIGINAL_COMMAND"] = process.command
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/python3", str(outer.dir / "harness.py"), str(outer.dir / "cc-gate"), str(outer.dir / "config.json"),
                env=env, user="nobody", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd="/",
            )
            out, err = await proc.communicate()
            process.stdout.write(out.decode())
            process.stderr.write(err.decode())
            process.exit(proc.returncode or 0)

        self._listener = await asyncssh.listen(
            "127.0.0.1", 0, server_host_keys=[self.host_key], server_factory=Server, process_factory=handle,
            allow_pty=False, encoding="utf-8",
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._listener is not None:
            self._listener.close()
            await self._listener.wait_closed()
        shutil.rmtree(self.dir, ignore_errors=True)

    @property
    def port(self) -> int:
        return self._listener.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    @property
    def host_key_line(self) -> str:
        return " ".join(self.host_key.export_public_key("openssh").decode().split()[:2])

    @property
    def fingerprint(self) -> str:
        return self.host_key.get_fingerprint("sha256")

    def calls(self, name: str) -> list[str]:
        path = self.dir / f"{name}-calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def gate_log(self) -> list[dict[str, Any]]:
        path = self.dir / "gate.log"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
