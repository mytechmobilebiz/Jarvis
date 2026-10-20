"""Operator commands, run inside the API image:

    python -m app.cli check-config   # validate settings and secrets; exit 1 on problems
    python -m app.cli migrate        # apply pending database migrations
    python -m app.cli db-status      # list applied migrations
    python -m app.cli brain-import   # the JARVIS import (plan, run, resume, verify, rollback, status, rescan,
                                     # decisions-export, decisions-apply); brain-import --help lists them
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from typing import Any

from .config import RESEARCH_HOSTS, SEARXNG_URL, Settings, get_settings

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
    secret("CC_SECRETS_KEY", settings.secrets_key)
    if token and token == webhook:
        errors.append("CC_SERVICE_TOKEN and N8N_WEBHOOK_SECRET must be different")
    if not settings.database_url.startswith("postgresql+asyncpg://"):
        errors.append("DATABASE_URL must start with postgresql+asyncpg://")
    if settings.anthropic_api_key and settings.anthropic_api_key.get_secret_value():
        if not settings.anthropic_api_key.get_secret_value().startswith("sk-ant-"):
            warnings.append("ANTHROPIC_API_KEY does not look like an Anthropic key (sk-ant-...)")
    else:
        warnings.append("ANTHROPIC_API_KEY not set: the supervisor will plan with rules (no model)")
    if settings.searxng_url.rstrip("/") != SEARXNG_URL:
        errors.append(f"SEARXNG_URL must be {SEARXNG_URL} (the private research container) or left unset: web "
                      "research never goes to another search service")
    listed = {h.strip().lower().rstrip(".") for h in settings.monitor_allowed_private_hosts.split(",") if h.strip()}
    if listed & RESEARCH_HOSTS:
        errors.append("MONITOR_ALLOWED_PRIVATE_HOSTS must not list " + " or ".join(sorted(listed & RESEARCH_HOSTS))
                      + ": monitoring would reach the research service without its screen")
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


# --- brain-import: the JARVIS import (docs/JARVIS_BRAIN_SPEC.md section 2) --------------------------------------
# Exit codes: 0 done; 1 failed, interrupted or verification failed; 2 refused before any change (including a command
# line that does not parse). Everything printed is counts, ids, kinds and fixed labels: never message text, titles,
# file contents or the text of an unexpected exception (it could quote a row).

BRAIN_HELP = """python -m app.cli brain-import COMMAND ...

  plan      --db F --profile D [--content D] SELECTION [--report FILE] [--discovery FILE]
  run       --db F --profile D [--content D] SELECTION [--label T] [--actor T] [--report FILE] [--discovery FILE]
            [--pre-backup-sha256 H]
  resume    --run ID --db F --profile D [--content D] [--report FILE]
  verify    --run ID --db F --profile D [--content D] [--report FILE]
  rollback  --run ID --yes [--discard-reviewed] [--actor T]
  status    [--run ID]
  rescan    [--yes]                       re-clean every stored brain text with the current detectors
  decisions-export --out FILE             review decisions keyed by source_key (on the test copy)
  decisions-apply  --in FILE --run ID --actor ADMIN_EMAIL [--yes]   (after the production import)

SELECTION: --sample N [--files-sample M] | --all | --since DATE --until DATE; and --no-files.
Staged plan: --sample 5, review; --sample (about 10 %), review; then --all.
Exit codes: 0 done, 1 failed or verification failed, 2 refused before any change.
"""


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of exiting, so a bad command line is a refusal (exit 2) we print ourselves."""

    def error(self, message: str) -> Any:  # noqa: D401 - argparse's hook
        raise _UsageError(message)


class _UsageError(Exception):
    pass


def _brain_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m app.cli brain-import", add_help=False)
    sub = parser.add_subparsers(dest="command")

    def source(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", required=True)
        p.add_argument("--profile", required=True)
        p.add_argument("--content")
        p.add_argument("--report")

    def selection(p: argparse.ArgumentParser) -> None:
        p.add_argument("--sample", type=int)
        p.add_argument("--files-sample", type=int)
        p.add_argument("--all", action="store_true")
        p.add_argument("--since")
        p.add_argument("--until")
        p.add_argument("--no-files", action="store_true")
        p.add_argument("--discovery")

    p = sub.add_parser("plan", add_help=False)
    source(p)
    selection(p)
    p = sub.add_parser("run", add_help=False)
    source(p)
    selection(p)
    p.add_argument("--label", default="")
    p.add_argument("--actor")
    p.add_argument("--pre-backup-sha256", default="")
    for name in ("resume", "verify"):
        p = sub.add_parser(name, add_help=False)
        p.add_argument("--run", type=int, required=True)
        source(p)
        p.add_argument("--actor")
    p = sub.add_parser("rollback", add_help=False)
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--discard-reviewed", action="store_true")
    p.add_argument("--actor")
    p = sub.add_parser("status", add_help=False)
    p.add_argument("--run", type=int)
    p = sub.add_parser("rescan", add_help=False)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--actor")
    p = sub.add_parser("decisions-export", add_help=False)
    p.add_argument("--out", required=True)
    p = sub.add_parser("decisions-apply", add_help=False)
    p.add_argument("--in", dest="in_path", required=True)
    p.add_argument("--run", type=int, required=True)
    p.add_argument("--actor", required=True)
    p.add_argument("--yes", action="store_true")
    return parser


def _counts_line(counts: dict[str, Any]) -> str:
    return ", ".join("%s %s" % (k, v) for k, v in sorted(counts.items())) or "none"


def _print_report(report: dict[str, Any]) -> None:
    """The counts of a plan or run report (spec 2.8): no text, no titles, no file names."""
    by_action = report.get("by_action") or {}
    print("records: %d (%s)" % (sum(by_action.values()), _counts_line(by_action)))
    print("selected: " + _counts_line(report.get("totals") or {}))
    print("by type: " + _counts_line(report.get("by_type") or {}))
    if report.get("by_reason"):
        print("reasons:")
        for reason, n in sorted(report["by_reason"].items(), key=lambda kv: (-kv[1], kv[0])):
            print("  %6d  %s" % (n, reason))
    print("by agent: " + _counts_line(report.get("by_agent") or {}))
    print("by staff (inferred): " + _counts_line(report.get("by_staff") or {}))
    print("by kind: " + _counts_line(report.get("by_kind") or {}))
    for what, levels in sorted((report.get("by_sensitivity") or {}).items()):
        if levels:
            print("sensitivity of %s: %s" % (what, _counts_line(levels)))
    secrets = report.get("secrets") or {}
    if secrets.get("types"):
        print("secrets removed (rotate these kinds of credential): " + _counts_line(secrets["types"]))
        print("  found in: " + _counts_line(secrets.get("where") or {}))
    restricted = report.get("restricted") or {}
    if restricted.get("types") or restricted.get("sessions_withheld") or restricted.get("files_excluded"):
        print("restricted data (never stored): %d session(s) withheld, %d file(s) excluded; kinds: %s"
              % (restricted.get("sessions_withheld", 0), restricted.get("files_excluded", 0),
                 _counts_line(restricted.get("types") or {})))
    print("contact details masked: " + _counts_line(report.get("masked") or {}))
    print("conflicts for a person to decide: " + _counts_line(report.get("conflicts") or {}))
    drafts = report.get("drafts") or []
    if drafts:
        print("workflow drafts (never run; disabled): %d" % len(drafts))
        for d in drafts:
            print("  %s: %s%s" % (d.get("name"), d.get("compatibility"),
                                  ", enabled in JARVIS" if d.get("enabled_in_source") else ""))
    tr = report.get("time_range") or {}
    if tr.get("first"):
        print("time range: %s to %s" % (tr.get("first"), tr.get("last")))
    print("archived sessions missing from this backup: %d" % report.get("missing_from_source", 0))
    disc = report.get("discovery_check") or {}
    print("discovery summary check: " + ("passed" if disc.get("ok") else "not requested"
                                         if not disc.get("checked") else "failed"))


def _print_verification(verification: dict[str, Any]) -> None:
    for name, check in (verification.get("checks") or {}).items():
        details = {k: v for k, v in check.items() if k != "ok" and not isinstance(v, (list, dict))}
        print("  %-34s %s  %s" % (name, "ok" if check.get("ok") else "FAILED", _counts_line(details)))
        if not check.get("ok"):
            for k, v in check.items():
                if isinstance(v, (list, dict)) and v:
                    print("      %s: %s" % (k, json.dumps(v, sort_keys=True)[:2000]))


async def _brain_command(args: argparse.Namespace) -> int:
    from .brain import importer as imp
    from .db import dispose_engine

    actor = getattr(args, "actor", None) or imp.DEFAULT_ACTOR
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task is not None:
        try:  # SIGTERM (docker stop) interrupts like Ctrl+C: the run is marked interrupted and can be resumed
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        if args.command in ("plan", "run", "resume", "verify"):
            paths = imp.SourcePaths(db=args.db, profile=args.profile, content=args.content,
                                    discovery=getattr(args, "discovery", None))
        if args.command in ("plan", "run"):
            selection = imp.Selection(sample=args.sample, files_sample=args.files_sample, all=args.all,
                                      since=args.since, until=args.until, no_files=args.no_files)
        if args.command == "plan":
            report = await imp.plan(paths, selection, report_file=args.report)
            print("plan (nothing was written):")
            _print_report(report)
            return 0
        if args.command == "run":
            result = await imp.run(paths, selection, label=args.label, actor=actor, report_file=args.report,
                                   pre_backup_sha256=args.pre_backup_sha256)
            print("run %d completed:" % result["run_id"])
            _print_report(result["report"])
            print("next: brain-import verify --run %d (with the same --db and --profile)" % result["run_id"])
            return 0
        if args.command == "resume":
            result = await imp.resume(args.run, paths, report_file=args.report, actor=actor)
            print("run %d completed after resuming:" % result["run_id"])
            _print_report(result["report"])
            return 0
        if args.command == "verify":
            result = await imp.verify(args.run, paths, report_file=args.report)
            print("verification of run %d: %s" % (args.run, "passed" if result["passed"] else "FAILED"))
            _print_verification(result["verification"])
            return 0 if result["passed"] else 1
        if args.command == "rollback":
            result = await imp.rollback(args.run, actor=actor, discard_reviewed=args.discard_reviewed,
                                        confirm=args.yes)
            print("run %d rolled back:" % args.run)
            for key in ("deleted", "restored", "reverted"):
                print("  %s: %s" % (key, _counts_line(result.get(key) or {})))
            print("  sessions recomputed: %d" % result.get("recomputed_sessions", 0))
            if result.get("kept_converted_drafts"):
                print("  converted drafts kept (their disabled schedules belong to the Command Center): ids %s"
                      % ", ".join(str(i) for i in result["kept_converted_drafts"]))
            if result.get("discarded_reviewed_items"):
                print("  reviewed or edited items deleted: %d" % result["discarded_reviewed_items"])
            if result.get("promoted_items_marked"):
                print("  promoted items now marked 'source removed': %d" % result["promoted_items_marked"])
            return 0
        if args.command == "status":
            out = await imp.status(args.run)
            print("importer %s; JARVIS Brain tables %s" % (out["importer_version"],
                                                          "installed" if out["installed"] else "NOT installed"))
            if out["installed"]:
                # scripts/jarvis-import.ps1 reads this line before a production import: it must say 0.
                print("migration 0007_brain_withhold %s; withheld copies with text: %d" % (
                    "present" if out.get("migration_0007_brain_withhold") else "MISSING",
                    out.get("withheld_copies_with_text", 0)))
                if out.get("withheld_rollback_copies_with_text"):
                    print("rollback copies of withheld messages with text: %d (the next start, or rescan --yes, "
                          "blanks them)" % out["withheld_rollback_copies_with_text"])
            if out.get("marked_interrupted"):
                print("marked interrupted (their process is gone): runs %s"
                      % ", ".join(str(i) for i in out["marked_interrupted"]))
            for r in out["runs"]:
                print("run %d: %s; started %s; finished %s; heartbeat %s; checkpoint %s; records %s%s%s"
                      % (r["id"], r["status"], r["started_at"], r["finished_at"] or "-", r["heartbeat_at"],
                         _counts_line(r["checkpoint"]), r["records"] if r["records"] is not None else "-",
                         "; verified %s" % ("passed" if r["verified"] else "FAILED") if r["verified"] is not None
                         else "", "; error: %s" % r["error"] if r["error"] else ""))
            if not out["runs"]:
                print("no import runs yet")
            return 0
        if args.command == "rescan":
            out = await imp.rescan(apply=args.yes, actor=actor)
            print(("rescan applied: " if args.yes else "rescan (nothing was changed; add --yes to apply): ")
                  + _counts_line({k: v for k, v in out.items() if k != "applied"}))
            return 0
        if args.command == "decisions-export":
            out = await imp.decisions_export(args.out)
            print("decisions exported: %d (items without a source key skipped: %d; edits of customer or Credit "
                  "Repair items not exported, to redo by hand: %d); removals (withheld messages and purged items, "
                  "keys only): %d"
                  % (out["exported"], out["skipped_without_source_key"], out["edits_not_exported"], out["removals"]))
            return 0
        if args.command == "decisions-apply":
            out = await imp.decisions_apply(args.in_path, args.run, actor=args.actor, apply=args.yes)
            key = "applied" if args.yes else "would_apply"
            print("decisions %s: %d%s" % ("applied" if args.yes else "that would apply (add --yes)", out[key],
                                          "" if out["not_applied"] else "; none left over"))
            for reason, numbers in sorted(out["not_applied"].items()):
                print("  not applied (%s): decisions %s" % (reason, ", ".join(str(n) for n in numbers)))
            print("removals %s: %s" % ("applied" if args.yes else "that would apply",
                                       _counts_line(out["removals"]) if out["removals"] else "none"))
            for reason, numbers in sorted(out["removals_not_applied"].items()):
                print("  removal not applied (%s): removals %s" % (reason, ", ".join(str(n) for n in numbers)))
            return 0
        raise _UsageError("unknown command")
    except imp.Refused as exc:
        print("refused: %s%s" % (exc, " (ids: %s)" % ", ".join(str(i) for i in exc.ids) if exc.ids else ""))
        return 2
    except imp.ImportFailed as exc:
        print("failed: %s" % exc)
        return 1
    except asyncio.CancelledError:
        print("interrupted")
        return 1
    finally:
        await dispose_engine()


def brain_import(argv: list[str]) -> int:
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(BRAIN_HELP)
        return 0
    try:
        args = _brain_parser().parse_args(argv)
    except _UsageError as exc:
        print("refused: %s\n" % exc + BRAIN_HELP)
        return 2
    if args.command is None:
        print("refused: choose a command\n" + BRAIN_HELP)
        return 2
    try:
        return asyncio.run(_brain_command(args))
    except KeyboardInterrupt:
        print("interrupted")
        return 1
    except Exception as exc:  # never print the message: it could quote imported text
        print("failed: %s (the details are not shown, because they could quote imported text)"
              % exc.__class__.__name__)
        return 1


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
    if command == "brain-import":
        return brain_import(argv[2:])
    print(__doc__)
    return 0 if command == "help" else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
