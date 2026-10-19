"""Server-side WordPress work for the WordPress Security & Repair agent: WP-CLI diagnostics and repairs.

Everything runs through the server's cc-gate (see app/ssh and server/cc-gate):
a restricted, non-root account that can only run allowlisted operations, as
the site's own system user. Diagnostics are read-only. Every repair:

  1. checks the current state, and makes no change when nothing is wrong;
  2. backs up the database into the server's backup folder (or stops);
  3. makes the one change it was approved for;
  4. verifies the result on the server and over HTTP;
  5. reports what changed, and raises a critical event if the site went down,
     naming the repair that undoes it (which needs approval in turn).

Repairs are "change" risk: always held for a human approval; for production
sites (or sites on production servers), an admin must type the site's name.
Only one repair or server-side check runs per WordPress install at a time;
a task that finds its install busy goes back to the queue. Just before a
repair opens the gate, its site and server are read again and compared with
what was approved; once the gate is open, the server's own cc-gate must still
map the site key to this site's address (and to the one approved), and the
dry run refuses when the version or address it would apply is not the one
the approval names. If a repair fails or cannot confirm its result after it
sent a change to the site, the site is checked over HTTP and a critical
repair.failed event says what happened and where the backup is.

What WP-CLI reports about a compromised site can be influenced by that site's
own code (wp-config.php, must-use plugins and drop-ins run whenever WordPress
loads); core checksum verification runs before WordPress loads.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import events
from .. import tasks as task_service
from ..db import session_scope
from ..models import Server, Site
from ..ssh.client import OutcomeUnknown
from ..ssh.gateway import GateFailed, GateRefused
from .base import AgentContext, AgentResult, EventSpec, Finding, Params, Risk, capability
from .monitoring.agent import make_checker

# Seconds for a whole repair or diagnosis: room for several slow server operations
# (backup, download, verification) one after the other.
REPAIR_TIMEOUT = 3000
STATUS_RANK = {"up": 0, "degraded": 1, "down": 2}

log = logging.getLogger(__name__)


class SiteBusy(Exception):
    """Another repair or server-side check is running for this WordPress install; the worker queues the task again."""


class SiteParams(Params):
    site_id: int = Field(description="ID of a monitored site that is linked to a server")


class PluginParams(Params):
    site_id: int = Field(description="ID of a monitored site that is linked to a server")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,99}$", description="The plugin's folder name, e.g. akismet")


class MaintenanceParams(Params):
    site_id: int = Field(description="ID of a monitored site that is linked to a server")
    on: bool = Field(description="true to switch maintenance mode on, false to switch it off")


async def linked(ctx: AgentContext, site_id: int, *, refresh: bool = False) -> tuple[Site, Server]:
    """The site and its server; refresh=True reads them again rather than from this session's identity map."""
    site = await ctx.session.get(Site, site_id, populate_existing=refresh)
    if site is None:
        raise ValueError(f"no monitored site with id {site_id}")
    if not site.server_id or not site.server_site_key:
        raise ValueError(f"{site.name} is not linked to a server yet (Websites page: server and site key)")
    server = await ctx.session.get(Server, site.server_id, populate_existing=refresh)
    if server is None:
        raise ValueError(f"the server linked to {site.name} no longer exists")
    if ctx.ssh is None:
        raise RuntimeError("SSH is not available in this process")
    return site, server


async def lock_site(ctx: AgentContext, site: Site) -> None:
    """One repair or server-side check per WordPress install at a time, held until the task ends.

    Keyed on the install (server and site key), not the Site row, which several rows may share. Never waits:
    raises SiteBusy instead, so a waiting task does not hold a worker slot or use up its own time.
    """
    got = await ctx.session.scalar(text("SELECT pg_try_advisory_xact_lock(hashtext('cc-site-server-work'), hashtext(:install))"),
                                   {"install": f"{site.server_id}:{site.server_site_key}"})
    if not got:
        raise SiteBusy(f"another repair or server-side check is running for {site.name}")


async def linked_as_approved(ctx: AgentContext, site_id: int) -> tuple[Site, Server]:
    """The site and server, read again in this task's own session right before a change, and checked against the approval.

    The worker checked the target too, but in an earlier transaction; this is the check that counts.
    """
    site, server = await linked(ctx, site_id, refresh=True)
    # Computed from the rows just read (they are in the identity map now), which are the ones the gate is given.
    try:
        fresh = await site_approval_context(ctx.session, site_id)
    except ValueError as exc:
        raise task_service.TargetChanged(f"the target changed since this was requested: {exc}; reject it and request the "
                                         "change again") from exc
    task_service.check_target(ctx.approved_target or {}, fresh)
    return site, server


def site_host(url: str) -> str:
    """The host a site is reached at, for comparing two addresses of one site: lower case, without "www." or a final dot."""
    try:
        host = (urlsplit(url.strip()).hostname or "").rstrip(".")
    except ValueError:  # e.g. a malformed address in a server's cc-gate configuration: matches nothing
        return ""
    return host[4:] if host.startswith("www.") else host


def site_address(url: str) -> str:
    """Host and path of a site's address, for comparing two addresses of one site ("" without a host).

    The host as site_host gives it; the path as given, without a final "/". The scheme, port and query do not count.
    A WordPress install in a subfolder (http://site.test/blog) is a different install from the one at the root.
    """
    host = site_host(url)
    if not host:
        return ""
    try:
        path = urlsplit(url.strip()).path
    except ValueError:
        return ""
    return host + path.rstrip("/")


def same_address(a: str, b: str) -> bool:
    return bool(site_address(a)) and site_address(a) == site_address(b)


def gate_site_url(hello: dict[str, Any] | None, key: str) -> str | None:
    """The URL a server's cc-gate configuration gives a site key (from its hello answer), or None if it does not list the key."""
    entry = ((hello or {}).get("sites") or {}).get(key) if key else None
    url = entry.get("url") if isinstance(entry, dict) else None
    return url if isinstance(url, str) and url else None


def gate_link_problem(server: Server, key: str, site_url: str) -> str:
    """Why this site may not be linked to (or changed through) this server's site key, from the server's last hello; "" if fine.

    cc-gate acts on whatever its root-owned configuration holds for a key, so a key that belongs to another
    site would make a repair change a different WordPress install than the one the approval names.
    """
    gate_url = gate_site_url(server.last_hello, key)
    if gate_url is None:
        return (f"server {server.name} has not reported a site key '{key}': run Test connection on the Servers page "
                "(it reads the site keys from the server's cc-gate configuration), then link the site again")
    if not same_address(gate_url, site_url):
        return (f"the cc-gate on server {server.name} gives site key '{key}' the address {gate_url}, which is not this site "
                f"({site_url}); link the key whose address is this site's")
    return ""


async def site_approval_context(session: AsyncSession, site_id: int | None, *, lock: bool = False) -> dict[str, Any]:
    """What an approval for a change on this site is about (see BaseAgent.approval_context).

    "binding" pins the exact target: if the site is re-linked, renamed or its
    server changes (its host key re-pinned, or its cc-gate giving the site key
    another address) after the request, the approval no longer matches and the
    change is refused (tasks.decide_approval, and again by the worker).
    """
    if site_id is None:
        return {}
    site = await session.get(Site, site_id, with_for_update=lock)
    if site is None:
        raise ValueError(f"no monitored site with id {site_id}")
    server = await session.get(Server, site.server_id, with_for_update=lock) if site.server_id else None
    production = site.environment == "production" or (server is not None and server.environment == "production")
    gate = None
    if server is not None and site.server_site_key:
        problem = gate_link_problem(server, site.server_site_key, site.url)
        if problem:
            raise ValueError(problem)
        gate = {"key": site.server_site_key, "url": gate_site_url(server.last_hello, site.server_site_key)}
    target = f"site {site.name} ({site.url})"
    if server is not None:
        target += f" on server {server_label(server)}"
    if gate:
        target += f", cc-gate site key {gate['key']} ({gate['url']})"
    return {
        "target": target,
        "environment": "production" if production else site.environment,
        "production": production,
        "confirm": site.name,
        "binding": {"site_id": site.id, "name": site.name, "url": site.url, "server_site_key": site.server_site_key or "",
                    "server": server_binding(server), "gate": gate},
    }


def server_label(server: Server) -> str:
    return f"{server.name} ({server.username}@{server.host}:{server.port})"


def server_binding(server: Server | None) -> dict[str, Any] | None:
    """The exact machine: a re-pinned host key means another machine may be answering, so approvals given before no longer apply."""
    if server is None:
        return None
    return {"server_id": server.id, "name": server.name, "host": server.host, "port": server.port, "username": server.username,
            "host_key_fingerprint": server.host_key_fingerprint or ""}


def _checked_at(diag: dict[str, Any]) -> str:
    return str(diag.get("at") or "")[:16].replace("T", " ") + " UTC" if diag.get("at") else "an unknown time"


def repair_expectations(capability: str, params: dict[str, Any], site: Site) -> dict[str, Any] | None:
    """The exact version or address a repair will apply, from the site's latest server-side check (None: nothing to record).

    The approval records these, and the repair's dry run refuses when the server is about to apply anything else.
    Raises ValueError when the latest check does not show it, so the approval cannot be vague.
    """
    if capability not in ("repair_update_plugin", "repair_reinstall_plugin", "repair_reinstall_core", "repair_restore_site_url"):
        return None
    diag = site.last_diagnosis or {}
    when = _checked_at(diag)
    first = f"run the server-side check of {site.name} (wp_diagnose) first, so the approval can name the exact "
    if capability in ("repair_update_plugin", "repair_reinstall_plugin"):
        slug = params.get("slug")
        plugin = next((p for p in diag.get("plugins") or [] if isinstance(p, dict) and p.get("name") == slug), None)
        if plugin is None or not isinstance(plugin.get("version"), str):
            raise ValueError(f"plugin {slug} is not in the latest server-side check ({when}); " + first + "version")
        if capability == "repair_reinstall_plugin":
            return {"plugin": slug, "version": plugin["version"], "checked_at": diag.get("at"),
                    "summary": f"reinstall {slug} {plugin['version']}"}
        if plugin.get("update") != "available" or not plugin.get("update_version"):
            raise ValueError(f"the latest server-side check ({when}) found no update for {slug} {plugin['version']}; "
                             "run it again if one was released since")
        return {"plugin": slug, "version": plugin["version"], "update_version": str(plugin["update_version"]), "checked_at": diag.get("at"),
                "summary": f"update {slug} {plugin['version']} → {plugin['update_version']}"}
    if capability == "repair_reinstall_core":
        version = diag.get("core_version")
        if not isinstance(version, str) or not version:
            raise ValueError(f"the latest server-side check ({when}) has no WordPress version; " + first + "version")
        return {"core_version": version, "checked_at": diag.get("at"), "summary": f"reinstall WordPress {version} core files"}
    urls = diag.get("site_urls") or {}
    home = urls.get("expected")
    if not isinstance(home, str) or not home:
        raise ValueError(f"the latest server-side check ({when}) did not read home/siteurl; " + first + "addresses")
    siteurl = urls.get("expected_siteurl") or home
    return {"home": home, "siteurl": siteurl, "checked_at": diag.get("at"),
            "summary": f"set home to {home}" + (f" and siteurl to {siteurl}" if siteurl != home else " and siteurl to the same")}


def approved_expectations(ctx: AgentContext) -> dict[str, Any]:
    """What the approval says the repair applies; ValueError if it names nothing (an approval given before versions were recorded)."""
    expected = (ctx.approved_target or {}).get("expected")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("this approval does not name the exact version or address the repair applies; reject it and request "
                         "the repair again")
    return expected


# The first cc-gate that installs exactly the approved plugin version (and refuses an update without one).
PINNED_UPDATE_GATE = (1, 2, 0)


def gate_version(hello: dict[str, Any]) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in str(hello.get("gate_version", "")).split("."))
    except ValueError:
        return (0,)


async def check_gate_site(gate: Any, site: Site, approved: dict[str, Any] | None) -> dict[str, Any]:
    """The server's own answer, right before a change: its cc-gate gives the site key this site's address, and the one approved.

    Returns the gate's hello.
    """
    hello = await gate.run("hello")
    key = site.server_site_key
    url = gate_site_url(hello, key)
    approved_url = (((approved or {}).get("binding") or {}).get("gate") or {}).get("url")
    if url is None:
        raise ValueError(f"the cc-gate on {gate.server.name} no longer lists site key '{key}'; nothing was changed")
    if not same_address(url, site.url):
        raise ValueError(f"the cc-gate on {gate.server.name} gives site key '{key}' the address {url}, not {site.name}'s ({site.url}); "
                         "nothing was changed. Link the site to the key for its own address")
    if url != approved_url:
        raise ValueError(f"the cc-gate on {gate.server.name} now gives site key '{key}' the address {url} (approved: {approved_url}); "
                         "nothing was changed; request the repair again")
    return hello


# Lists the site's own code can fill with anything (WP-CLI loads it): each entry must be an object with this text field.
OBJECT_LISTS = {"plugins": "name", "themes": "name", "admins": "user_login", "capability_admins": "user_login"}


def objects(part: str, value: Any) -> list[dict[str, Any]]:
    """value, if it is a list of objects with a text OBJECT_LISTS[part]; otherwise TypeError (the part counts as not read)."""
    key = OBJECT_LISTS[part]
    if not isinstance(value, list) or not all(isinstance(x, dict) and isinstance(x.get(key), str) for x in value):
        raise TypeError(f"expected a list of objects with a text {key!r}")
    return value


DIAGNOSIS_PARTS = {
    "core_version": ("wp_core_version", lambda r: r.get("version")),
    "site_urls": ("wp_site_urls", lambda r: r),
    "plugins": ("wp_plugin_list", lambda r: objects("plugins", r.get("plugins", []))),
    "themes": ("wp_theme_list", lambda r: objects("themes", r.get("themes", []))),
    "admins": ("wp_admins", lambda r: objects("admins", r.get("admins", []))),
    "super_admins": ("wp_admins", lambda r: r.get("super_admins", [])),
    # Users of other roles with an administrator's powers; missing (not checked) before WordPress 5.9 or with an older gate.
    "capability_admins": ("wp_admins", lambda r: objects("capability_admins", r["capability_admins"])),
    "maintenance": ("wp_maintenance_status", lambda r: r),
    "debug_log": ("wp_debug_log_status", lambda r: r),
    "core_verify": ("wp_core_verify", lambda r: r),
}
# Kept from the previous diagnosis when they cannot be read, so "new since last time" still compares with something.
BASELINE_PARTS = ("admins", "super_admins", "capability_admins", "plugins")
ADMIN_PARTS = ("admins", "super_admins", "capability_admins")
HIDDEN_CODE = ("must-use", "dropin")  # loaded even with --skip-plugins


def diagnosis_findings(diag: dict[str, Any], previous: dict[str, Any] | None) -> list[Finding]:
    out: list[Finding] = []
    errors = diag.get("errors") or {}
    if errors:
        out.append(Finding(code="server_check_incomplete", severity="warning",
                           title="Server-side check incomplete; could not check: " + ", ".join(sorted(errors)),
                           detail="; ".join(f"{k}: {v}" for k, v in sorted(errors.items()))[:1000]))
    urls = diag.get("site_urls") or {}
    registered = diag.get("registered_url")
    if urls.get("expected") and registered and not same_address(str(urls["expected"]), str(registered)):
        out.append(Finding(code="gate_url_mismatch", severity="critical",
                           title=f"The server's cc-gate gives this site key the address {urls['expected']}, not the registered "
                                 f"{registered}",
                           detail="These server-side results may be about a different WordPress install. Link the site to the key "
                                  "for its own address (Websites page), or fix the key's \"url\" in the server's cc-gate configuration; "
                                  "repairs are refused until they match."))
    if urls.get("subdirectory_install_suspected"):
        # home is right, and siteurl is home plus the folder the site's path ends in: most likely the configuration, not a hack.
        out.append(Finding(code="subdirectory_install_suspected", severity="warning",
                           title=f"WordPress seems to live in its own folder (siteurl {urls.get('suspected_siteurl') or urls.get('siteurl')}, "
                                 f"home {urls.get('home')})",
                           detail="Set \"siteurl\" for this site in the server's cc-gate configuration, so the address check can tell this "
                                  "from a changed siteurl. Until then the gate does not reset it (repair_restore_site_url is refused)."))
    elif urls and not urls.get("matches"):
        expected = urls.get("expected")
        if urls.get("expected_siteurl") and urls.get("expected_siteurl") != expected:
            expected = f"{expected} and {urls.get('expected_siteurl')}"
        out.append(Finding(code="site_url_mismatch", severity="critical",
                           title=f"WordPress address options point elsewhere (home {urls.get('home')}, siteurl {urls.get('siteurl')}; "
                                 f"expected {expected})",
                           detail="Changed home/siteurl options are a classic redirect hack. Repair: repair_restore_site_url"
                                  + (" (restores home; siteurl stays, as WordPress may live in that folder: set the site's \"siteurl\" in "
                                     "the server's cc-gate configuration to have it checked)." if urls.get("suspected_siteurl") else
                                     ". If WordPress lives in its own folder, set that site's \"siteurl\" in the server's cc-gate configuration.")))
    verify = diag.get("core_verify") or {}
    if verify.get("verified") is False:
        changed, extra = verify.get("files") or [], verify.get("extra_files") or []
        out.append(Finding(code="core_modified", severity="critical",
                           title=f"WordPress core does not match the official checksums ({len(changed)} changed or missing, "
                                 f"{len(extra)} unexpected file(s))",
                           detail="Modified or extra core files usually mean malware. Repair: repair_reinstall_core (replaces changed "
                                  "files; unexpected files must be checked and removed by hand). " + "; ".join((changed + extra)[:10])[:900]))
    elif verify.get("verified") is None and verify:
        out.append(Finding(code="core_verify_unavailable", severity="info", title="Core checksums could not be checked",
                           detail=str(verify.get("detail", ""))[:300]))
    plugins = diag.get("plugins") or []
    updates = [p for p in plugins if p.get("update") == "available"]
    if updates:
        out.append(Finding(code="plugin_update_available", severity="warning",
                           title="Plugin updates available: " + ", ".join(f"{p['name']} {p.get('version')} → {p.get('update_version')}" for p in updates[:6]),
                           detail="Repair: repair_update_plugin (one plugin per approval)."))
    if diag.get("maintenance", {}).get("active"):
        out.append(Finding(code="maintenance_on", severity="warning", title="WordPress maintenance mode is on",
                           detail="Left on after an interrupted update? Repair: repair_maintenance_mode with on=false."))
    debug = diag.get("debug_log") or {}
    if debug.get("exists") and debug.get("type", "file") != "file":
        out.append(Finding(code="debug_log_not_a_file", severity="warning", title=f"wp-content/debug.log is a {debug.get('type')}, not a file",
                           detail="A link there can expose another file (e.g. wp-config.php) at /wp-content/debug.log. Check it on the server."))
    elif debug.get("exists"):
        out.append(Finding(code="debug_log_present", severity="warning", title=f"wp-content/debug.log exists ({debug.get('bytes', 0)} bytes)",
                           detail="It can leak paths and errors if served publicly. Repair: repair_quarantine_debug_log."))
    if previous:
        # Only parts the previous diagnosis really has count as a baseline (a part it could not read is missing there).
        def logins(d: dict[str, Any], parts: tuple[str, ...] = ADMIN_PARTS) -> set[str]:
            found = set(d.get("super_admins") or []) if "super_admins" in parts else set()
            for part in ("admins", "capability_admins"):
                if part in parts:
                    found |= {a.get("user_login") for a in d.get(part) or []}
            return found

        new_admins = logins(diag, tuple(part for part in ADMIN_PARTS if part in previous)) - logins(previous)
        if new_admins and "admins" not in errors:
            out.append(Finding(code="new_admin_account", severity="critical",
                               title="New administrator account(s) since the last check: " + ", ".join(sorted(new_admins)),
                               detail="Check with the site owner; attackers often add an admin account."))
        code = {p.get("name") for p in plugins if p.get("status") in HIDDEN_CODE}
        before = {p.get("name") for p in previous.get("plugins") or [] if p.get("status") in HIDDEN_CODE}
        if code - before and "plugins" in previous and "plugins" not in errors:
            out.append(Finding(code="new_mu_plugin", severity="critical",
                               title="New must-use plugin or drop-in since the last check: " + ", ".join(sorted(code - before)),
                               detail="They run on every request and cannot be switched off from wp-admin; malware likes to hide there."))
    return out


async def record_failed_diagnosis(site_id: int, exc: BaseException) -> None:
    """A server-side check that could not run at all: noted next to the last diagnosis, which stays the baseline (own transaction).

    The next diagnosis that works replaces the note.
    """
    error = "the check ran out of time" if isinstance(exc, asyncio.CancelledError) else str(exc) or exc.__class__.__name__
    try:
        async with session_scope() as s:
            await s.execute(text("SET LOCAL lock_timeout = '10s'"))  # never stuck behind a writer of that row
            site = await s.get(Site, site_id)
            if site is not None:
                site.last_diagnosis = {**(site.last_diagnosis or {}),
                                       "last_failed_attempt": {"at": datetime.now(UTC).isoformat(), "error": error[:300]}}
    except Exception as record_exc:  # noqa: BLE001 - the original failure is what the task reports
        log.warning("could not record the failed server-side check of site %s: %s", site_id, record_exc)


class ServerSideCapabilities:
    """WP-CLI diagnostics and repairs; mixed into the WordPress Security & Repair agent."""

    name: str

    async def _http_status(self, ctx: AgentContext, site: Site) -> tuple[str, str]:
        """(status, the critical problems) from a quick HTTP check."""
        report = await make_checker(ctx, None).check(site.url, deep=False)
        return report.status, "; ".join(f.title for f in report.findings if f.severity == "critical")[:300]

    async def _diagnose(self, ctx: AgentContext, site: Site, server: Server, previous: dict[str, Any] | None = None) -> dict[str, Any]:
        """Each part on its own: a part the server cannot answer is listed under "errors", the rest still counts."""
        key = site.server_site_key
        diag: dict[str, Any] = {}
        errors: dict[str, str] = {}
        answers: dict[str, Any] = {}
        async with ctx.ssh.open(ctx, server) as gate:
            for part, (op, pick) in DIAGNOSIS_PARTS.items():
                if op not in answers:
                    try:
                        answers[op] = await gate.run(op, site=key)
                    except (GateFailed, GateRefused, OutcomeUnknown) as exc:
                        answers[op] = exc
                if isinstance(answers[op], Exception):
                    errors[part] = str(answers[op])[:500]
                    continue
                try:
                    diag[part] = pick(answers[op])
                except LookupError:  # this server does not report that part: not checked, and not an error
                    continue
                except (AttributeError, TypeError) as exc:  # an answer of the wrong shape
                    errors[part] = f"unexpected answer: {exc}"[:500]
        for part in BASELINE_PARTS:
            if part in errors and previous and part in previous:
                diag[part] = previous[part]
        # A part never read stays missing (no empty placeholder): it is not a baseline to compare with.
        diag["errors"] = errors
        diag["at"] = datetime.now(UTC).isoformat()
        diag["server"] = server.name
        diag["registered_url"] = site.url
        return diag

    @capability(risk=Risk.READ, description="Server-side diagnosis through WP-CLI (read-only): core checksums, home/siteurl, plugins and "
                "updates, themes, administrator accounts, maintenance mode, debug.log.", params=SiteParams, timeout_seconds=REPAIR_TIMEOUT,
                examples=("Diagnose site 3 on its server",))
    async def wp_diagnose(self, ctx: AgentContext, params: SiteParams) -> AgentResult:
        site, server = await linked(ctx, params.site_id)
        await lock_site(ctx, site)
        previous = dict(site.last_diagnosis or {})
        previous.pop("last_failed_attempt", None)
        for part in OBJECT_LISTS:  # stored before answers were checked: no baseline
            try:
                objects(part, previous.get(part, []))
            except TypeError:
                del previous[part]
        try:
            diag = await self._diagnose(ctx, site, server, previous or None)
            findings = diagnosis_findings(diag, previous or None)
        except BaseException as exc:  # the server could not be asked at all, the time ran out, or the answers made no sense
            await asyncio.shield(record_failed_diagnosis(site.id, exc))
            raise
        diag["findings"] = [f.model_dump() for f in findings]
        site.last_diagnosis, site.last_diagnosis_at = diag, datetime.now(UTC)
        serious = [f for f in findings if f.severity != "info"]
        events = []
        if serious:
            worst = "critical" if any(f.severity == "critical" for f in serious) else "warning"
            events.append(EventSpec(kind="site.server_finding", severity=worst,
                                    title=f"{site.name} (server check): " + "; ".join(f.title for f in serious[:3]),
                                    detail={"site_id": site.id, "codes": [f.code for f in serious]}))
        summary = (f"{site.name} on {server.name}: WordPress {diag.get('core_version') or '?'}, {len(diag.get('plugins') or [])} plugin(s), "
                   f"{len(diag.get('admins') or [])} admin(s); " + ("; ".join(f.title for f in serious) if serious else "no problems found"))
        return AgentResult(summary=summary, data={"site_id": site.id, "diagnosis": diag}, findings=findings, events=events)

    async def _repair(self, ctx: AgentContext, site_id: int, title: str, steps, *, backup: bool = True,
                      may_degrade: bool = False, min_gate: tuple[int, ...] | None = None) -> AgentResult:
        """Common shape: check, backup, change (steps), confirm, HTTP verification, report.

        steps(gate, key, notes, dry) returns "nothing to do" or "change" when dry,
        and raises when the change did not take effect.
        """
        site, server = await linked(ctx, site_id)
        await lock_site(ctx, site)
        site, server = await linked_as_approved(ctx, site_id)
        key = site.server_site_key
        notes: list[str] = []
        before, _ = await self._http_status(ctx, site)
        gate_session = ctx.ssh.open(ctx, server)
        try:
            async with gate_session as gate:
                hello = await check_gate_site(gate, site, ctx.approved_target)
                if min_gate and gate_version(hello) < min_gate:
                    raise ValueError(f"cc-gate {hello.get('gate_version')} on {server.name} cannot {title} safely; install cc-gate "
                                     f"{'.'.join(map(str, min_gate))} or later on the server (install-gate.sh from this version's server "
                                     "folder, see docs/SERVER_SETUP.md); nothing was changed")
                outcome = await steps(gate, key, notes, dry=True)
                if outcome == "nothing to do":
                    return AgentResult(summary=f"{site.name}: {title} not needed ({'; '.join(notes) or 'already correct'}); nothing was changed.",
                                       data={"site_id": site.id, "changed": False})
                if backup:
                    try:
                        exported = await gate.run("wp_db_export", site=key)
                    finally:
                        gate.sent_change = False  # the backup changes nothing on the site; from here on a sent change counts
                    notes.insert(0, f"database backup: {exported.get('file')}")
                await steps(gate, key, notes, dry=False)
        except BaseException as exc:
            # Failed part-way only if a change was really sent to the site; a refused one changed nothing.
            if gate_session.sent_change and not isinstance(exc, GateRefused):
                await self._repair_failed(ctx, site, server, title, notes, exc)
            raise
        after, problems = await self._http_status(ctx, site)
        notes.append(f"site responds: {after}" + (f" (was {before})" if after != before else ""))
        findings: list[Finding] = []
        if STATUS_RANK.get(after, 2) > STATUS_RANK.get(before, 2) and not (may_degrade and after == "degraded"):
            findings.append(Finding(code="site_worse_after_repair", severity="critical",
                                    title=f"{site.name} is {after} after the repair (was {before})" + (f": {problems}" if problems else "")))
        ok = not findings
        result_events = [EventSpec(
            kind="repair.completed" if ok else "repair.site_down",
            severity="info" if ok else "critical",
            title=f"{site.name}: {title} " + ("completed" if ok else f"completed, but the site is now {after} (was {before})"),
            detail={"site_id": site.id, "server": server.name, "approved_by": ctx.approved_by, "notes": notes},
        )]
        return AgentResult(summary=f"{site.name}: {title} done (approved by {ctx.approved_by}). " + "; ".join(notes),
                           data={"site_id": site.id, "changed": True, "notes": notes}, findings=findings, events=result_events)

    async def _repair_failed(self, ctx: AgentContext, site: Site, server: Server, title: str, notes: list[str], exc: BaseException) -> None:
        """The change started but did not finish cleanly: look at the site and say so loudly (own transaction, survives the failure)."""
        status = "not checked"
        if isinstance(exc, Exception):
            try:
                async with asyncio.timeout(60):
                    status, problems = await self._http_status(ctx, site)
                    status += f" ({problems})" if problems else ""
            except Exception as check_exc:  # noqa: BLE001 - the report matters more than this check
                status = f"could not be checked ({check_exc.__class__.__name__})"
        error = f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__
        ctx.log(f"{title} failed after it started changing the site: {error}; site responds: {status}; " + "; ".join(notes), "error")

        async def emit() -> None:
            async with session_scope() as s:
                await events.emit(s, ctx.settings, kind="repair.failed", severity="critical", source=ctx.agent_name or "wordpress_security",
                                  title=f"{site.name}: {title} failed part-way; check the site (it responds: {status})",
                                  detail={"site_id": site.id, "server": server.name, "approved_by": ctx.approved_by, "error": error[:1000],
                                          "notes": notes, "site_status": status}, task_id=ctx.task_id)

        await asyncio.shield(emit())

    @capability(risk=Risk.CHANGE, description="Set the WordPress home and siteurl options back to the site's URL configured on the server "
                "(fixes the common redirect hack). Database backup first.", params=SiteParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_restore_site_url(self, ctx: AgentContext, params: SiteParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            urls = await gate.run("wp_site_urls", site=key)
            if dry:
                if urls.get("subdirectory_install_suspected"):  # only siteurl differs, and it looks like WordPress's own folder
                    raise ValueError(f"WordPress seems to live in its own folder (siteurl {urls['suspected_siteurl']}); set \"siteurl\" "
                                     "for this site in the server's cc-gate configuration, then check the site again")
                if urls.get("matches"):
                    notes.append(f"home and siteurl already {urls.get('expected')}")
                    return "nothing to do"
                expected = approved_expectations(ctx)
                applies = (urls.get("expected"), urls.get("expected_siteurl") or urls.get("expected"))
                if applies != (expected.get("home"), expected.get("siteurl")):
                    raise ValueError(f"the server would now set home {applies[0]} and siteurl {applies[1]}, but the approval names "
                                     f"home {expected.get('home')} and siteurl {expected.get('siteurl')}; nothing was changed; "
                                     "request the repair again")
                notes.append(f"was home {urls.get('home')}, siteurl {urls.get('siteurl')}")
                return "change"
            result = await gate.run("wp_set_site_urls", site=key)
            after = await gate.run("wp_site_urls", site=key)
            if after.get("matches"):
                notes.append(f"now {after.get('expected')}")
            elif result.get("siteurl_kept") and after.get("subdirectory_install_suspected"):
                # The gate undid the changed home and left a siteurl that looks like WordPress's own folder.
                notes.append(f"home now {after.get('expected')}; siteurl kept at {after.get('siteurl')} (set \"siteurl\" in the "
                             "cc-gate configuration to have it checked)")
            else:
                raise RuntimeError(f"home/siteurl still do not match after the change: {after}")
            return "done"

        return await self._repair(ctx, params.site_id, "restore home/siteurl", steps)

    @capability(risk=Risk.CHANGE, description="Deactivate one plugin (e.g. a suspicious or broken one). Database backup first; undo with "
                "repair_activate_plugin.", params=PluginParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_deactivate_plugin(self, ctx: AgentContext, params: PluginParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            plugins = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}
            plugin = plugins.get(params.slug)
            if plugin is None:
                raise ValueError(f"plugin {params.slug} is not installed")
            if dry:
                if plugin.get("status") != "active":
                    notes.append(f"{params.slug} is already {plugin.get('status')}")
                    return "nothing to do"
                return "change"
            await gate.run("wp_plugin_deactivate", site=key, slug=params.slug)
            after = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}
            status = after.get(params.slug, {}).get("status")
            if status == "active":
                raise RuntimeError(f"{params.slug} is still active after deactivating it")
            notes.append(f"{params.slug} is now {status}")
            return "done"

        return await self._repair(ctx, params.site_id, f"deactivate plugin {params.slug}", steps)

    @capability(risk=Risk.CHANGE, description="Activate one installed plugin (undoes repair_deactivate_plugin). Database backup first.",
                params=PluginParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_activate_plugin(self, ctx: AgentContext, params: PluginParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            plugins = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}
            plugin = plugins.get(params.slug)
            if plugin is None:
                raise ValueError(f"plugin {params.slug} is not installed")
            if dry:
                if plugin.get("status") == "active":
                    notes.append(f"{params.slug} is already active")
                    return "nothing to do"
                return "change"
            await gate.run("wp_plugin_activate", site=key, slug=params.slug)
            after = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}
            if after.get(params.slug, {}).get("status") != "active":
                raise RuntimeError(f"{params.slug} is {after.get(params.slug, {}).get('status')} after activating it")
            notes.append(f"{params.slug} activated")
            return "done"

        return await self._repair(ctx, params.site_id, f"activate plugin {params.slug}", steps)

    @capability(risk=Risk.CHANGE, description="Update one WordPress.org plugin to exactly the version named in the approval (the server's "
                "cc-gate refuses any other). Database backup first.", params=PluginParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_update_plugin(self, ctx: AgentContext, params: PluginParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            plugins = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}
            plugin = plugins.get(params.slug)
            if plugin is None:
                raise ValueError(f"plugin {params.slug} is not installed")
            if dry and plugin.get("update") != "available":
                notes.append(f"{params.slug} {plugin.get('version')} is up to date")
                return "nothing to do"
            # In both passes: the backup in between takes a while, and WordPress.org may publish another version.
            expected = approved_expectations(ctx)
            now = (plugin.get("version"), plugin.get("update_version") if plugin.get("update") == "available" else None)
            if now != (expected.get("version"), expected.get("update_version")):
                raise ValueError(f"{params.slug} is now {now[0]}" + (f" with {now[1]} available" if now[1] else " with no update")
                                 + f", but the approval names {expected.get('version')} → {expected.get('update_version')}; nothing was "
                                 "changed; request the update again")
            if dry:
                notes.append(f"{params.slug} {plugin.get('version')} → {plugin.get('update_version')}")
                return "change"
            # The gate installs exactly this version and fails if the plugin is any other version afterwards.
            await gate.run("wp_plugin_update", site=key, slug=params.slug, version=str(expected.get("update_version")))
            after = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}.get(params.slug)
            if after is None:
                raise RuntimeError(f"{params.slug} is no longer installed after the update")
            if after.get("version") != expected.get("update_version"):
                raise RuntimeError(f"{params.slug} is version {after.get('version')} after the update, not the approved "
                                   f"{expected.get('update_version')}")
            notes.append(f"now version {after.get('version')}, as approved")
            return "done"

        return await self._repair(ctx, params.site_id, f"update plugin {params.slug}", steps, min_gate=PINNED_UPDATE_GATE)

    @capability(risk=Risk.CHANGE, description="Reinstall one plugin's current version from WordPress.org (replaces tampered plugin files). "
                "Database backup first.", params=PluginParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_reinstall_plugin(self, ctx: AgentContext, params: PluginParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            # In both passes: the reinstall takes whichever version is installed when it runs.
            plugins = {p["name"]: p for p in (await gate.run("wp_plugin_list", site=key)).get("plugins", [])}
            if params.slug not in plugins:
                raise ValueError(f"plugin {params.slug} is not installed")
            version, approved = plugins[params.slug].get("version"), approved_expectations(ctx).get("version")
            if version != approved:
                raise ValueError(f"{params.slug} is now version {version}, but the approval names {approved}; nothing was changed; "
                                 "request the reinstall again")
            if dry:
                return "change"
            result = await gate.run("wp_plugin_reinstall", site=key, slug=params.slug)
            notes.append(f"reinstalled {params.slug} {result.get('version')}")
            return "done"

        return await self._repair(ctx, params.site_id, f"reinstall plugin {params.slug}", steps)

    @capability(risk=Risk.CHANGE, description="Reinstall the current WordPress version's core files from WordPress.org (replaces modified "
                "core files; wp-content untouched). Database backup first, checksums verified after.", params=SiteParams,
                timeout_seconds=REPAIR_TIMEOUT)
    async def repair_reinstall_core(self, ctx: AgentContext, params: SiteParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            # In both passes: the reinstall takes whichever version is installed when it runs.
            version, approved = (await gate.run("wp_core_version", site=key)).get("version"), approved_expectations(ctx).get("core_version")
            if version != approved:
                raise ValueError(f"WordPress is now version {version}, but the approval names {approved}; nothing was changed; "
                                 "request the reinstall again")
            if dry:
                return "change"
            result = await gate.run("wp_core_reinstall", site=key)
            verify = await gate.run("wp_core_verify", site=key)
            if verify.get("verified") is False:
                left = (verify.get("files") or []) + (verify.get("extra_files") or [])
                raise RuntimeError(f"reinstalled WordPress {result.get('version')}, but core still does not match the checksums "
                                   "(files that should not exist are not removed by a reinstall; check and remove them by hand): "
                                   + "; ".join(left[:10]))
            notes.append(f"reinstalled WordPress {result.get('version')}; checksums "
                         + ("verified" if verify.get("verified") else "could not be checked"))
            return "done"

        return await self._repair(ctx, params.site_id, "reinstall WordPress core", steps)

    @capability(risk=Risk.CHANGE, description="Switch WordPress maintenance mode on or off.", params=MaintenanceParams,
                timeout_seconds=REPAIR_TIMEOUT)
    async def repair_maintenance_mode(self, ctx: AgentContext, params: MaintenanceParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            active = (await gate.run("wp_maintenance_status", site=key)).get("active")
            if dry:
                if bool(active) == params.on:
                    notes.append(f"maintenance mode is already {'on' if active else 'off'}")
                    return "nothing to do"
                return "change"
            await gate.run("wp_maintenance", site=key, onoff="on" if params.on else "off")
            if bool((await gate.run("wp_maintenance_status", site=key)).get("active")) != params.on:
                raise RuntimeError(f"maintenance mode is not {'on' if params.on else 'off'} after switching it")
            notes.append(f"maintenance mode {'on' if params.on else 'off'}")
            return "done"

        return await self._repair(ctx, params.site_id, f"maintenance mode {'on' if params.on else 'off'}", steps, backup=False,
                                  may_degrade=params.on)

    @capability(risk=Risk.CHANGE, description="Flush the object cache and rewrite rules.", params=SiteParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_flush_caches(self, ctx: AgentContext, params: SiteParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            if dry:
                return "change"
            await gate.run("wp_cache_flush", site=key)
            await gate.run("wp_rewrite_flush", site=key)
            notes.append("object cache and rewrite rules flushed")
            return "done"

        return await self._repair(ctx, params.site_id, "flush caches", steps, backup=False)

    @capability(risk=Risk.CHANGE, description="Move wp-content/debug.log into the server's backup folder (nothing is deleted).",
                params=SiteParams, timeout_seconds=REPAIR_TIMEOUT)
    async def repair_quarantine_debug_log(self, ctx: AgentContext, params: SiteParams) -> AgentResult:
        async def steps(gate, key, notes, dry):
            if dry:
                status = await gate.run("wp_debug_log_status", site=key)
                if not status.get("exists"):
                    notes.append("there is no wp-content/debug.log")
                    return "nothing to do"
                if status.get("type", "file") != "file":  # the helper does not touch it either
                    notes.append(f"wp-content/debug.log is a {status.get('type')}, not a file; check it on the server")
                    return "nothing to do"
                return "change"
            moved = await gate.run("debug_log_quarantine", site=key)
            notes.append(f"debug.log moved to {moved.get('moved_to')}")
            if (await gate.run("wp_debug_log_status", site=key)).get("exists"):
                notes.append("a new debug.log was written right away: WP_DEBUG_LOG is probably on in wp-config.php")
            return "done"

        return await self._repair(ctx, params.site_id, "quarantine debug.log", steps, backup=False)
