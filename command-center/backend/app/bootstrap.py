"""First-run setup shared by the API and the worker: migrations, admin user, schedules."""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from .config import Settings
from .db import run_migrations, session_scope
from .models import Schedule, User
from .security import hash_password

log = logging.getLogger(__name__)

# Agent routines. Cross-system workflows (the morning briefing, lead intake,
# alert routing) live in n8n instead; see n8n/workflows/.
DEFAULT_SCHEDULES = [
    {
        "name": "wordpress-uptime-sweep",
        "description": "Uptime, TLS and redirect check of every monitored site (no exposure probes).",
        "agent": "monitoring",
        "capability": "check_all_sites",
        "params": {"deep": False},
        "cron": "*/15 * * * *",
        "enabled": True,
    },
    {
        "name": "wordpress-security-scan",
        "description": "Nightly security scan of every site: exposures, suspicious redirects and cloaking, injected content.",
        "agent": "wordpress_security",
        "capability": "security_scan_all",
        "params": {},
        "cron": "17 3 * * *",
        "enabled": True,
    },
    {
        "name": "supervisor-health-review",
        "description": "Hourly review of workers, failing agents, overdue approvals and n8n delivery.",
        "agent": "supervisor",
        "capability": "health_review",
        "params": {},
        "cron": "7 * * * *",
        "enabled": True,
    },
]


async def ensure_admin(settings: Settings) -> bool:
    """Create the first admin from ADMIN_EMAIL / ADMIN_PASSWORD when there are no users."""
    async with session_scope() as session:
        count = await session.scalar(select(func.count()).select_from(User))
        if count:
            return False
        if not settings.admin_password or not settings.admin_password.get_secret_value():
            log.warning("no users exist and ADMIN_PASSWORD is not set: nobody can sign in yet")
            return False
        session.add(
            User(
                email=settings.admin_email.lower(),
                display_name="Administrator",
                password_hash=hash_password(settings.admin_password.get_secret_value()),
                role="admin",
            )
        )
        log.info("created admin user %s", settings.admin_email)
        return True


async def ensure_schedules(extra: list[dict] | None = None) -> None:
    async with session_scope() as session:
        existing = set((await session.scalars(select(Schedule.name))).all())
        for spec in [*DEFAULT_SCHEDULES, *(extra or [])]:
            if spec["name"] not in existing:
                session.add(Schedule(**spec))


async def check_withheld_copies() -> None:
    """Empties the memory items promoted from a withheld message that still hold text, blanks the rollback copies of
    withheld messages that still hold it (what a database 0.6.0 left behind: its withholds kept them,
    app/brain/archive.py), and removes the quotes of withheld, deleted or emptied sources that tasks kept
    (quotes.refresh_quotes). Skipped, never waited for, while an import, a rollback or a rescan holds the lock, or a
    task quoting the brain holds the quote lock (the next start, or rescan --yes, does it); a failure is logged by its
    class only and never stops the start-up, and brain-import status keeps counting what is left
    (withheld_copies_with_text)."""
    from .brain import archive, importer, quotes

    try:
        async with session_scope() as session:
            if not await importer.try_xact_lock(session, importer.GLOBAL_LOCK):
                log.info("withheld-copies check skipped: an import, rollback or rescan is running")
                return
            # Held from here (the sweep and refresh_quotes take it again at once): never waits for a running recall.
            if not await quotes.lock_quotes(session, wait=False):
                log.info("withheld-copies check skipped: a task is writing a history answer")
                return
            emptied = await archive.sweep_withheld_copies(session, actor="system (start-up check)")
            blanked = await archive.sweep_withheld_records(session)
            scrubbed = await quotes.refresh_quotes(session)
            if emptied or blanked or scrubbed:
                log.info("withheld-copies check: %d promoted memory item(s) emptied or quarantined again, %d rollback "
                         "copies blanked, %d task result(s) lost a quote", len(emptied), blanked, scrubbed)
    except Exception as exc:  # noqa: BLE001 - never stops the start-up; the class only, never the text
        log.warning("withheld-copies check failed: %s", exc.__class__.__name__)


async def bootstrap(settings: Settings) -> None:
    applied = await run_migrations()
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    await check_withheld_copies()
    await ensure_admin(settings)
    from .credit_repair import store as credit_repair_store

    if credit_repair_store.configured(settings):
        cr_applied = await credit_repair_store.migrate(settings)
        if cr_applied:
            log.info("applied credit repair migrations: %s", ", ".join(cr_applied))
    from .agents.registry import all_agents

    extra = [s for a in all_agents().values() for s in getattr(a, "default_schedules", [])]
    await ensure_schedules(extra)
