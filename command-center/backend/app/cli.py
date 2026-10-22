"""Operator commands, run inside the API image:

    python -m app.cli check-config   # validate settings and secrets; exit 1 on problems
    python -m app.cli migrate        # apply pending database migrations
    python -m app.cli db-status      # list applied migrations
    python -m app.cli brain-import   # the JARVIS import (plan, run, settle, resume, verify, rollback, status, rescan,
                                     # repair, fingerprints, decisions-export, decisions-apply); brain-import --help
                                     # lists them
    python -m app.cli restore-carry  # restore.ps1: users' access and the audit log across a restore (export, apply)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from typing import Any

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
            [--pre-backup-sha256 H] [--decisions FILE --actor ADMIN_EMAIL]
            --decisions: a version 5 decisions file from the test copy; its withholds, releases and purges apply
            first, then the import replays the decisions taken on this very backup (the same SHA-256)
  settle    --backup F [--actor T] [--report FILE]   finish the withholds taken on this JARVIS backup (0.6.2): its
            conversations with a withheld message are decided once, by provenance; every related row is held for
            review with its evidence; an import of this very file then replays the result (decisions-export
            refuses until every withhold is finished and every hold it found is kept or released)
  resume    --run ID --db F --profile D [--content D] [--report FILE]
  verify    --run ID --db F --profile D [--content D] [--report FILE]
  rollback  --run ID --yes [--discard-reviewed] [--actor T]
  status    [--run ID]
  rescan    [--yes]                       re-clean every stored brain text with the current detectors
  repair    [--yes] [--actor T]           withhold every readable row that is the same message as a withheld one
                                          (without --yes: what is pending, nothing is changed)
  fingerprints --forget-other-key [--yes] forget purge fingerprints made with another database password
  decisions-export --out FILE [--removals-only]   review decisions keyed by source_key (on the test copy);
                                          --removals-only: only withholds, purges, releases and version
                                          redactions (restore.ps1)
  decisions-apply  --in FILE --run ID --actor ADMIN_EMAIL [--yes]   (after an import: version 3 and 4 files;
                                          a version 5 file goes with the import itself: run --decisions)
  decisions-apply  --in FILE --removals-only [--actor ADMIN_EMAIL] [--yes]   withholds, purges and releases only
                                          (restore.ps1, after a restore; the actor is "system (restore)")
  legacy-reset --export FILE [--yes] [--actor T] [--decisions V3FILE --actor ADMIN_EMAIL]
            JARVIS Brain data of Command Center 0.6.0 or 0.6.1 is locked (hidden from every reader; imports
            refused): binds each withhold to its exact JARVIS row where it can, writes every decision to FILE (a
            version 5 decisions file), rolls the legacy import runs back and lifts the lock; then import the same
            JARVIS backup again and run settle. --decisions: a version 3 file of 0.6.0 or 0.6.1 (decisions-apply
            refuses it), applied first. Without --yes: what it would do, nothing is changed

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
    p.add_argument("--decisions")
    p = sub.add_parser("settle", add_help=False)
    p.add_argument("--backup", required=True)
    p.add_argument("--profile")  # accepted and not needed: Settle reads the conversations only
    p.add_argument("--content")
    p.add_argument("--report")
    p.add_argument("--actor")
    p.add_argument("--yes", action="store_true")  # accepted: Settle changes only what the decisions make it change
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
    p = sub.add_parser("repair", add_help=False)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--actor")
    p = sub.add_parser("fingerprints", add_help=False)
    p.add_argument("--forget-other-key", action="store_true", required=True)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--actor")
    p = sub.add_parser("decisions-export", add_help=False)
    p.add_argument("--out", required=True)
    p.add_argument("--removals-only", action="store_true")
    p = sub.add_parser("decisions-apply", add_help=False)
    p.add_argument("--in", dest="in_path", required=True)
    p.add_argument("--run", type=int)
    p.add_argument("--actor")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--removals-only", action="store_true")
    p = sub.add_parser("legacy-reset", add_help=False)
    p.add_argument("--export", required=True)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--actor")
    p.add_argument("--decisions")
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
    if report.get("withheld_same_message") or report.get("held_for_review"):
        print("stored without text as the same message as a withheld one: %s; held for an admin's review: %s"
              % (_counts_line(report.get("withheld_same_message") or {}),
                 _counts_line(report.get("held_for_review") or {})))
    later_backup = report.get("later_backup") or {}
    if later_backup:
        # 0.6.2 plan 5.6 (jarvis-import -Mode Plan prints this before anything is imported): per withheld conversation
        # (its archive session id), what this backup does there. Counts only.
        print("withheld conversations in this backup (a later backup: what is new waits for an admin):")
        for conversation, counts in sorted(later_backup.items()):
            print("  conversation %s: %s" % (conversation, _counts_line(counts)))
    if report.get("frozen_sessions_skipped"):
        print("conversations an admin stopped importing (left out): %d" % report["frozen_sessions_skipped"])
    tripwire = report.get("tripwire") or {}
    if tripwire.get("inside_family") or tripwire.get("outside_family"):
        print("tripwire (plan 5.5): rows with a withheld text held in withheld conversations: %d (verify fails: an "
              "admin looks); readable rows with a withheld text outside them, listed for admins: %d"
              % (tripwire.get("inside_family", 0), tripwire.get("outside_family", 0)))
    if report.get("key_reassigned") or report.get("keys_adopted") or report.get("purged_same_text"):
        print("keys JARVIS gave to another row (left out): %d; stored rows re-keyed: %d; purged items back under "
              "another key (left out): %d" % (report.get("key_reassigned", 0), report.get("keys_adopted", 0),
                                              report.get("purged_same_text", 0)))
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
            if args.decisions and not args.actor:
                raise imp.Refused("run --decisions needs --actor ADMIN_EMAIL (recorded as the reviewer of each "
                                  "decision)")
            result = await imp.run(paths, selection, label=args.label, actor=actor, report_file=args.report,
                                   pre_backup_sha256=args.pre_backup_sha256, decisions=args.decisions)
            if result.get("decisions") is not None:
                out = result["decisions"]
                for warning in out["warnings"]:
                    print("WARNING: " + warning)
                print("decisions applied before the import: " + _counts_line(out["before_import"]))
                print("review decisions on imported items applied: %d%s" % (
                    out["items_applied"], "" if out["items_not_applied"] else "; none left over"))
                for reason, numbers in sorted(out["items_not_applied"].items()):
                    print("  not applied (%s): decisions %s" % (reason, ", ".join(str(n) for n in numbers)))
            print("run %d completed:" % result["run_id"])
            _print_report(result["report"])
            print("next: brain-import verify --run %d (with the same --db and --profile)" % result["run_id"])
            return 0
        if args.command == "settle":
            result = await imp.settle(imp.SourcePaths(db=args.backup, profile=args.profile or "",
                                                      content=args.content), actor=actor, report_file=args.report)
            for label, n in sorted((result.get("waiting") or {}).items()):
                print("decisions waiting on another JARVIS backup (%s): %d" % (label, n))
            if result.get("run_id") is None:
                print("nothing to finish: every withhold on this JARVIS backup is finished")
                return 0
            done = (result.get("report") or {}).get("settle") or {}
            print("run %d finished %d decision(s): %s" % (result["run_id"], result.get("decisions", 0),
                                                         _counts_line(done)))
            _print_report(result["report"])
            print("next: review what it holds (JARVIS Brain page, Held for review), then export the decisions")
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
                if "migration_0008_brain_withhold_identity" in out:
                    # scripts/jarvis-import.ps1 reads this line too (0.6.2).
                    print("migration 0008_brain_withhold_identity %s; identity repair %s; readable rows matching a "
                          "withhold: %d; held for review: %d; withheld rows with a text fingerprint: %d; rows blanked "
                          "by the database guard: %d; quotes of removed sources: %d; purge fingerprints made with "
                          "another key: %d" % (
                              "present" if out["migration_0008_brain_withhold_identity"] else "MISSING",
                              "done" if out.get("identity_repair_done") else "not done",
                              out.get("readable_rows_matching_a_withhold", 0), out.get("held_for_review", 0),
                              out.get("withheld_rows_with_a_text_fingerprint", 0),
                              out.get("rows_blanked_by_the_database_guard", 0),
                              out.get("quotes_of_removed_sources", 0),
                              out.get("purge_fingerprints_made_with_another_key", 0)))
            if out.get("legacy_lock"):
                # 0.6.2 plan 8.1 (scripts/jarvis-import.ps1 and brain-precheck.ps1 show this line)
                print("legacy JARVIS Brain data (Command Center 0.6.0 or 0.6.1): LOCKED, hidden from every reader, "
                      "imports refused; import runs %s. Run brain-import legacy-reset --export FILE --yes "
                      "(jarvis-import -Mode LegacyReset)" % (", ".join(str(i) for i in out.get("legacy_runs") or [])
                                                             or "none (withholds made before 0.6.2 only)"))
            if out.get("frozen_conversations") or out.get("last_tripwire"):
                # 0.6.2 plan 5.3, 5.5: what a later backup left out, and where the newest run found a withheld text
                tw = out.get("last_tripwire") or {}
                print("later backups: conversations an admin stopped importing: %d; tripwire of run %s: rows held in "
                      "withheld conversations %d, readable rows outside them %d%s" % (
                          out.get("frozen_conversations", 0), tw.get("run", "-"), tw.get("inside_family", 0),
                          tw.get("outside_family", 0), "" if not tw.get("outside") else " (archive session/message: %s)"
                          % ", ".join("%s/%s" % (w.get("session"), w.get("message")) for w in tw["outside"][:20])))
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
        if args.command == "repair":
            out = await imp.repair(apply=args.yes, actor=actor)
            if args.yes:
                print("identity repair done: " + _counts_line({k: v for k, v in out["repair"].items()
                                                               if isinstance(v, int) and not isinstance(v, bool)}))
            else:
                print("identity repair pending (nothing was changed; add --yes to repair): " + _counts_line(
                    {k: v for k, v in out["pending"].items() if not isinstance(v, (dict, list))}))
            return 0
        if args.command == "fingerprints":
            out = await imp.forget_fingerprints(apply=args.yes, actor=actor)
            print("purge fingerprints made with another key: %d (%s)" % (
                out["fingerprints_made_with_another_key"],
                "forgotten; the purges stay, by their keys" if args.yes else "nothing was changed; add --yes"))
            return 0
        if args.command == "decisions-export":
            out = await imp.decisions_export(args.out, removals_only=args.removals_only)
            entries = _entry_keys(args.out)
            if entries:  # review 2, S17: say what in the file is derived from text
                print("note: %d purged memory entr%s in this file: each key carries a 64-bit digest of the entry's "
                      "text; keep the file like a backup and delete it once it is applied"
                      % (entries, "y is" if entries == 1 else "ies are"))
            if args.removals_only:
                # scripts/restore.ps1 reads this line (tests/unit/test_brain_ops.py runs its pattern against it).
                print("removals exported (withheld messages and purged items, keys and JARVIS ids only): %d; "
                      "releases: %d; purges by item id: %d; purge fingerprints: %d; version redactions: %d"
                      % (out["removals"], out["releases"], out.get("purged_items", 0), out.get("purge_fingerprints", 0),
                         out.get("redactions", 0)))
                return 0
            print("decisions exported: %d (items without a source key skipped: %d; edits of customer or Credit "
                  "Repair items not exported, to redo by hand: %d); removals (withheld messages and purged items, "
                  "keys and JARVIS ids only): %d; releases: %d"
                  % (out["exported"], out["skipped_without_source_key"], out["edits_not_exported"], out["removals"],
                     out["releases"]))
            return 0
        if args.command == "decisions-apply":
            if not args.removals_only and (args.run is None or not args.actor):
                raise imp.Refused("decisions-apply needs --run ID and --actor ADMIN_EMAIL (or --removals-only)")
            out = await imp.decisions_apply(args.in_path, args.run, actor=args.actor, apply=args.yes,
                                            removals_only=args.removals_only)
            if not args.removals_only:
                key = "applied" if args.yes else "would_apply"
                print("decisions %s: %d%s" % ("applied" if args.yes else "that would apply (add --yes)", out[key],
                                              "" if out["not_applied"] else "; none left over"))
                for reason, numbers in sorted(out["not_applied"].items()):
                    print("  not applied (%s): decisions %s" % (reason, ", ".join(str(n) for n in numbers)))
            # scripts/restore.ps1 reads the removals line (tests/unit/test_brain_ops.py runs its pattern against it).
            print("removals %s: %s" % ("applied" if args.yes else "that would apply",
                                       _counts_line(out["removals"]) if out["removals"] else "none"))
            for reason, numbers in sorted(out["removals_not_applied"].items()):
                print("  removal not applied (%s): removals %s" % (reason, ", ".join(str(n) for n in numbers)))
            # scripts/restore.ps1 reads every "not applied" line: a removal it could not apply again fails the restore.
            if out.get("purged_items"):
                print("purges by item id: " + _counts_line(out["purged_items"]))
            for reason, numbers in sorted((out.get("purged_items_not_applied") or {}).items()):
                print("  purge not applied (%s): purged items %s" % (reason, ", ".join(str(n) for n in numbers)))
            if out.get("purge_fingerprints"):
                print("purge fingerprints: " + _counts_line(out["purge_fingerprints"]))
            if out.get("redactions"):  # restore.ps1's file: the version redactions made after the backup (rv3-dur-3)
                print("version redactions: " + _counts_line(out["redactions"]))
            for reason, numbers in sorted((out.get("redactions_not_applied") or {}).items()):
                print("  redaction not applied (%s): redactions %s" % (reason, ", ".join(str(n) for n in numbers)))
            if out["releases"] or out["releases_not_applied"]:
                print("releases %s: %s" % ("applied" if args.yes else "that would apply",
                                           _counts_line(out["releases"]) if out["releases"] else "none"))
            for reason, numbers in sorted(out["releases_not_applied"].items()):
                print("  release not applied (%s): releases %s" % (reason, ", ".join(str(n) for n in numbers)))
            return 0
        if args.command == "legacy-reset":
            out = await imp.legacy_reset(args.export, actor=actor, confirm=args.yes, decisions=args.decisions)
            if out.get("version_3_file") is not None:
                print("version 3 decisions applied first: " + _counts_line(out["version_3_file"]))
            print("legacy reset done: import runs rolled back: %s; withholds bound to their exact JARVIS row: %d; "
                  "withholds kept by their key only (no stored row to bind): %d; reviewed or edited items deleted "
                  "(their review decisions are in the file): %d" % (
                      ", ".join(str(i) for i in out["runs_rolled_back"]) or "none",
                      out.get("withholds bound to their exact JARVIS row", 0),
                      out.get("withholds kept by their key only (no stored row to bind)", 0),
                      out.get("reviewed_items_deleted", 0)))
            print("decisions file (version 5): " + _counts_line(out.get("decisions_file") or {}))
            print("the legacy lock is lifted. Next: import the same JARVIS backup again (brain-import run ... --all; "
                  "add --decisions <this file> --actor <admin email> to apply its review decisions), then finish "
                  "the decisions on it: brain-import settle --backup <that backup>")
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


# --- restore-carry: what restore.ps1 keeps across a restore besides the brain's removals (app/restore_carry.py) ----
RESTORE_CARRY_HELP = """python -m app.cli restore-carry COMMAND ...

  export --out FILE        the users' access state and the audit log (restore.ps1, before the restore)
  apply  --in FILE [--yes] after pg_restore, before the services start: the audit entries of the undone period put back,
                           the database migrated, deactivations, revoked permissions and lowered roles made after the
                           backup applied again, every sign-in ended; everything else the restore reverts is listed

Exit codes: 0 done, 1 failed, 2 refused before any change.
"""


def _entry_keys(path: str) -> int:
    """How many removals in a decisions file are purged memory entries (their keys carry a digest of the text)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return 0
    removals = data.get("removals") if isinstance(data, dict) else None
    return sum(1 for r in removals or [] if isinstance(r, dict) and "#entry-" in str(r.get("source_key") or ""))


def restore_carry(argv: list[str]) -> int:
    from . import restore_carry as rc
    from .db import dispose_engine

    parser = _Parser(prog="restore-carry", add_help=False)
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("export", add_help=False)
    p.add_argument("--out", required=True)
    p = sub.add_parser("apply", add_help=False)
    p.add_argument("--in", dest="in_path", required=True)
    p.add_argument("--yes", action="store_true")
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(RESTORE_CARRY_HELP)
        return 0
    try:
        args = parser.parse_args(argv)
    except _UsageError as exc:
        print("refused: %s\n" % exc + RESTORE_CARRY_HELP)
        return 2

    async def go() -> int:
        try:
            if args.command == "export":
                out = await rc.export(args.out)
                print("access and audit exported: users %d, audit entries %d" % (out["users"], out["audit_entries"]))
                return 0
            out = await rc.apply(args.in_path, apply=args.yes)
            done = "" if args.yes else " (nothing was changed; add --yes)"
            # scripts/restore.ps1 prints these lines and repeats the "reverted:" and "not applied again:" ones at the end.
            if out["audit_not_kept"]:
                print("audit entries made after the backup NOT put back (%s): they stay in the file%s"
                      % (out["audit_not_kept"], done))
            else:
                print("audit entries made after the backup, put back: %d%s" % (out["audit_kept"], done))
            if out["access_not_carried"]:
                print("access: %s" % out["access_not_carried"])
            print("access changes made after the backup, applied again: %d%s" % (len(out["kept"]), done))
            for line in out["kept"]:
                print("  kept: " + line)
            for line in out["skipped"]:
                print("  not applied again: " + line)
            print("user changes the restore reverts (redo them on the Users page if still wanted): %d"
                  % len(out["reverted"]))
            for line in out["reverted"]:
                print("  reverted: " + line)
            print("sign-in sessions ended: %d (everyone signs in again)%s" % (out["sessions_ended"], done))
            return 0
        except rc.Refused as exc:
            print("refused: %s" % exc)
            return 2
        finally:
            await dispose_engine()

    try:
        return asyncio.run(go())
    except Exception as exc:  # the message could name a user's data: the class only
        print("failed: %s (the details are not shown)" % exc.__class__.__name__)
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
    if command == "restore-carry":
        return restore_carry(argv[2:])
    print(__doc__)
    return 0 if command == "help" else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
