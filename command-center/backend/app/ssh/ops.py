"""The operations cc-gate accepts, as the Command Center knows them.

This table must match OPS in server/cc-gate exactly (a test compares them).
The gate enforces it on the server; the Command Center enforces it again
before connecting, so a malformed request never leaves the building.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Literal

Kind = Literal["read", "change"]

SITE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9@._-]{0,63}$")
ARG_PATTERNS = {"site": SITE_RE, "slug": SLUG_RE, "service": SERVICE_RE, "onoff": re.compile(r"^(on|off)$")}


@dataclass(frozen=True)
class Op:
    kind: Kind
    args: tuple[str, ...]
    description: str
    slow: bool = False


OPS: dict[str, Op] = {
    "hello": Op("read", (), "Gate version, account, configured sites and services"),
    "system_overview": Op("read", (), "Uptime, load, memory and disk space"),
    "service_status": Op("read", ("service",), "Whether an allowlisted service is running"),
    "wp_core_version": Op("read", ("site",), "WordPress version (WP-CLI)"),
    "wp_core_verify": Op("read", ("site",), "Verify core files against WordPress.org checksums", slow=True),
    "wp_plugin_list": Op("read", ("site",), "Plugins with versions and available updates"),
    "wp_theme_list": Op("read", ("site",), "Themes with versions and available updates"),
    "wp_admins": Op("read", ("site",), "Administrator accounts"),
    "wp_site_urls": Op("read", ("site",), "home and siteurl options compared with the registered URL"),
    "wp_maintenance_status": Op("read", ("site",), "Whether maintenance mode is on"),
    "wp_debug_log_status": Op("read", ("site",), "Whether wp-content/debug.log exists"),
    "wp_db_export": Op("change", ("site",), "Database backup into the server's backup folder", slow=True),
    "wp_set_site_urls": Op("change", ("site",), "Set home and siteurl back to the URL configured on the server"),
    "wp_plugin_deactivate": Op("change", ("site", "slug"), "Deactivate a plugin"),
    "wp_plugin_activate": Op("change", ("site", "slug"), "Activate a plugin"),
    "wp_plugin_update": Op("change", ("site", "slug"), "Update a plugin from WordPress.org", slow=True),
    "wp_plugin_reinstall": Op("change", ("site", "slug"), "Reinstall a plugin's current version from WordPress.org", slow=True),
    "wp_core_reinstall": Op("change", ("site",), "Reinstall the current WordPress version's core files", slow=True),
    "wp_maintenance": Op("change", ("site", "onoff"), "Switch maintenance mode on or off"),
    "wp_cache_flush": Op("change", ("site",), "Flush the object cache"),
    "wp_rewrite_flush": Op("change", ("site",), "Flush rewrite rules"),
    "debug_log_quarantine": Op("change", ("site",), "Move wp-content/debug.log into the backup folder"),
    "service_restart": Op("change", ("service",), "Restart an allowlisted service"),
}


class InvalidRequest(ValueError):
    pass


def build_command(op: str, args: dict[str, str]) -> str:
    """The exact request string sent over SSH, after validation."""
    spec = OPS.get(op)
    if spec is None:
        raise InvalidRequest(f"unknown gate operation {op!r}")
    if set(args) != set(spec.args):
        raise InvalidRequest(f"{op} takes {', '.join(spec.args) or 'no arguments'}")
    values = []
    for name in spec.args:
        value = str(args[name])
        if not ARG_PATTERNS[name].match(value):
            raise InvalidRequest(f"invalid {name} {value!r}")
        values.append(value)
    return " ".join(shlex.quote(t) for t in ["cc-gate", "v1", op, *values])
