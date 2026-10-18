"""Operator commands, run inside the API image:

    python -m app.cli check-config   # validate settings and secrets; exit 1 on problems
    python -m app.cli migrate        # apply pending database migrations
    python -m app.cli db-status      # list applied migrations
"""

from __future__ import annotations

import asyncio
import sys

from .config import Settings, get_settings

MIN_SECRET = 24
PLACEHOLDERS = ("REPLACE", "CHANGEME", "changeme", "example", "password")


def config_problems(settings: Settings) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    def secret(name: str, value, minimum: int = MIN_SECRET) -> str:
        raw = value.get_secret_value() if value is not None else ""
        if not raw:
            errors.append(f"{name} is not set")
        elif len(raw) < minimum:
            errors.append(f"{name} is shorter than {minimum} characters")
        elif any(p in raw for p in PLACEHOLDERS):
            errors.append(f"{name} still contains a placeholder value")
        return raw

    if "@" not in settings.admin_email:
        errors.append("ADMIN_EMAIL is not an email address")
    secret("ADMIN_PASSWORD", settings.admin_password, 12)
    token = secret("CC_SERVICE_TOKEN", settings.service_token)
    webhook = secret("N8N_WEBHOOK_SECRET", settings.n8n_webhook_secret)
    if token and token == webhook:
        errors.append("CC_SERVICE_TOKEN and N8N_WEBHOOK_SECRET must be different")
    if not settings.database_url.startswith("postgresql+asyncpg://"):
        errors.append("DATABASE_URL must start with postgresql+asyncpg://")
    if settings.anthropic_api_key and settings.anthropic_api_key.get_secret_value():
        if not settings.anthropic_api_key.get_secret_value().startswith("sk-ant-"):
            warnings.append("ANTHROPIC_API_KEY does not look like an Anthropic key (sk-ant-...)")
    else:
        warnings.append("ANTHROPIC_API_KEY not set: the supervisor will plan with rules (no model)")
    if settings.environment == "production" and settings.cookie_secure is False:
        warnings.append("COOKIE_SECURE=false: fine for http://localhost, set true behind HTTPS")
    return errors, warnings


async def _migrate() -> None:
    from .db import dispose_engine, run_migrations

    applied = await run_migrations()
    print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    await dispose_engine()


async def _db_status() -> None:
    from sqlalchemy import text

    from .db import dispose_engine, get_engine

    async with get_engine().connect() as conn:
        rows = (await conn.execute(text("SELECT version, applied_at FROM schema_migrations ORDER BY version"))).all()
    for version, at in rows:
        print(f"{version}  {at:%Y-%m-%d %H:%M:%S %Z}")
    await dispose_engine()


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "help"
    if command == "check-config":
        errors, warnings = config_problems(get_settings())
        for w in warnings:
            print(f"warning: {w}")
        for e in errors:
            print(f"ERROR: {e}")
        print("configuration OK" if not errors else f"{len(errors)} configuration error(s)")
        return 1 if errors else 0
    if command == "migrate":
        asyncio.run(_migrate())
        return 0
    if command == "db-status":
        asyncio.run(_db_status())
        return 0
    print(__doc__)
    return 0 if command == "help" else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
