"""What restore.ps1 carries across a restore besides the JARVIS Brain's removals (0.6.2): who may sign in and what
they may read, and the audit log.

A restore replaces the whole command_center database with an older dump. Without this, it would also undo every access
decision an admin made after that backup: an operator whose sensitive-data permission was revoked would have it again,
a deactivated user would be active again (and that user's old sign-in cookie would work again, because the
deactivation's session deletion is undone too), and the audit entries of the undone period would be gone.

    export (before the restore): the users' access state (id, e-mail, creation time, role, active, sensitive-data
           permission, a fingerprint of the password hash, never the hash) and the audit log, to a JSON file in
           backups\\.
    apply  (after pg_restore, before anything serves):
      1. the audit entries newer than the restored backup are put back, with their own ids and times, when the backup is
         an earlier state of this very database (its newest audit entry is in the file, unchanged);
      2. the database is migrated (an older dump has an older schema);
      3. when the restored users are this database's own (same id and creation time), every NARROWING access change
         made after the backup is applied again: a deactivation, a revoked sensitive-data permission, a lowered role.
         Never one that would leave no active admin. A backup of another installation (a move to another PC) is left
         as it is;
      4. every sign-in session is ended: everyone signs in again;
      5. everything else the restore reverts is listed, to redo by hand: users created after the backup, a
         reactivation, a granted permission, a raised role, a password changed after the backup (the older password
         works again).

Everything printed is e-mail addresses, roles and counts; the file holds no password hash and no secret.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from . import audit
from .db import run_migrations, sessionmaker
from .models import AuditLog, Session, User

FORMAT = "command-center-restore-carry"
VERSION = 1
ACTOR = "system (restore)"
RANK = {"viewer": 0, "operator": 1, "admin": 2}


class Refused(Exception):
    """Nothing was changed; the message is safe to show."""


# restore.ps1's mark in the database (brain_maintenance, review 2 S18): written right after pg_restore, with a part for
# each file to apply again ("access": restore-carry apply, "removals": decisions-apply --removals-only); each command
# clears its own part once it has applied it, and the mark goes when no part is left. While it is there the api and the
# worker refuse to start (bootstrap.bootstrap): whoever starts them, from whichever folder, never serves restored data
# without the decisions made after the backup.
PENDING = "restore_reapply_pending"
PARTS = ("access", "removals")


class RestorePending(RuntimeError):
    """The restored database waits for what the restore must not undo: nothing may serve it yet."""


async def pending_restore(s: Any) -> dict[str, Any] | None:
    """The mark's detail (the parts still to apply, the files' names, the backup folder's name), or None."""
    if await s.scalar(text("SELECT to_regclass('public.brain_maintenance')")) is None:
        return None
    detail = await s.scalar(text("SELECT detail::text FROM brain_maintenance WHERE name = :n"), {"n": PENDING})
    if detail is None:
        return None
    try:
        out = json.loads(detail)
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


async def finish_part(s: Any, part: str) -> bool:
    """This part of the mark is applied again (in the caller's transaction): cleared, and the mark deleted when no
    part is left. Whether a mark was there."""
    if await pending_restore(s) is None:
        return False
    await s.execute(text("UPDATE brain_maintenance SET detail = detail || jsonb_build_object(CAST(:p AS text), false) "
                         "WHERE name = :n"), {"p": part, "n": PENDING})
    await s.execute(text("DELETE FROM brain_maintenance WHERE name = :n AND NOT EXISTS (SELECT 1 FROM "
                         "jsonb_each(detail) e WHERE e.key = ANY(CAST(:parts AS text[])) AND e.value = 'true'::jsonb)"),
                    {"n": PENDING, "parts": list(PARTS)})
    return True


def pending_message(detail: dict[str, Any]) -> str:
    """Why nothing starts, for the log (file names and the backup folder's name only)."""
    files = [str(detail.get(p + "_file") or "") for p in PARTS if detail.get(p) is True]
    return ("a restore did not finish: what it must not undo (withholds, purges and access changes made after the "
            "backup%s) is not applied again yet, so nothing may serve the restored data. Finish it: in the Command "
            "Center folder, powershell -NoProfile -ExecutionPolicy Bypass -File .\\scripts\\restore.ps1 "
            "-FinishPending (docs/DEPLOY_WINDOWS.md, Backup and restore)"
            % ("; files: " + ", ".join(f for f in files if f) if any(files) else ""))


def _password_fp(password_hash: str) -> str:
    # A fingerprint of the stored hash, to tell that a password changed after the backup. Never the hash itself.
    return hashlib.sha256(("restore-carry/v1:" + (password_hash or "")).encode("utf-8")).hexdigest()[:24]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


async def export(out_path: str) -> dict[str, Any]:
    """Writes the access state and the audit log (restore.ps1, before the restore)."""
    if os.path.exists(out_path):
        raise Refused("the output file already exists; nothing is overwritten")
    async with sessionmaker()() as s:
        users = [{"id": u.id, "email": u.email.lower(), "created_at": _iso(u.created_at), "role": u.role,
                  "is_active": bool(u.is_active), "can_access_sensitive": bool(u.can_access_sensitive),
                  "password_fp": _password_fp(u.password_hash)}
                 for u in (await s.scalars(select(User).order_by(User.id))).all()]
        entries = [{"id": a.id, "at": _iso(a.at), "actor": a.actor, "action": a.action,
                    "target_type": a.target_type, "target_id": a.target_id, "detail": a.detail, "ip": a.ip}
                   for a in (await s.scalars(select(AuditLog).order_by(AuditLog.id))).all()]
    content = {"format": FORMAT, "version": VERSION, "exported_at": datetime.now(timezone.utc).isoformat(),
               "users": users, "audit": entries}
    with open(out_path, "x", encoding="utf-8") as f:
        json.dump(content, f, indent=1, sort_keys=True, default=str)
    return {"users": len(users), "audit_entries": len(entries)}


def _read(in_path: str) -> dict[str, Any]:
    try:
        with open(in_path, "rb") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise Refused("the restore file could not be read as JSON") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise Refused("this is not a file written by restore-carry export")
    if not isinstance(data.get("version"), int) or isinstance(data["version"], bool) or data["version"] > VERSION:
        raise Refused("this file was written by a newer Command Center: apply it with that version")
    return data


async def _carry_audit(s: Any, entries: list[Any]) -> tuple[int, str]:
    """Step 1: the audit entries the restore dropped, put back as they were. (how many, why none were)."""
    # Raw SQL: the audit table has had these columns since 0001, and this runs before an older dump is migrated.
    by_id = {e["id"]: e for e in entries if isinstance(e, dict) and isinstance(e.get("id"), int)}
    if by_id:  # applied a second time (the commands restore.ps1 prints after a failure): already put back
        last = by_id[max(by_id)]
        here = (await s.execute(text("SELECT at, actor, action FROM audit_log WHERE id = :id"), {"id": last["id"]})
                ).first()
        if here is not None and (here.actor, here.action, here.at) == (last.get("actor"), last.get("action"),
                                                                       _when(last.get("at"))):
            return 0, ""
    newest = (await s.execute(text("SELECT id, at, actor, action FROM audit_log ORDER BY id DESC LIMIT 1"))).first()
    if newest is None:
        return 0, "the restored backup has no audit entry to line the file up with" if by_id else ""
    mine = by_id.get(int(newest.id))
    if mine is None or (mine.get("actor"), mine.get("action"), _when(mine.get("at"))) != (
            newest.actor, newest.action, newest.at):
        return 0, "the restored backup is not an earlier state of this database"
    later = [by_id[i] for i in sorted(by_id) if i > int(newest.id)]
    for e in later:
        detail = e.get("detail")
        await s.execute(pg_insert(AuditLog).values(
            id=e["id"], at=_when(e.get("at")) or datetime.now(timezone.utc), actor=str(e.get("actor") or ""),
            action=str(e.get("action") or ""), target_type=str(e.get("target_type") or ""),
            target_id=str(e.get("target_id") or ""), detail=detail if isinstance(detail, dict) else {},
            ip=str(e.get("ip") or "")).on_conflict_do_nothing(index_elements=[AuditLog.id]))
    if later:
        await s.execute(text("SELECT setval(pg_get_serial_sequence('audit_log', 'id'), (SELECT max(id) FROM audit_log))"))
    return len(later), ""


def _same_database(users: dict[str, User], before: dict[str, dict[str, Any]]) -> bool:
    """The restored users are this database's own: each one the file also names has the same id and creation time."""
    common = [e for e in users if e in before]
    return bool(common) and all(
        before[e].get("id") == users[e].id and _when(before[e].get("created_at")) == users[e].created_at
        for e in common)


async def apply(in_path: str, *, apply: bool, migrate: bool = True) -> dict[str, Any]:
    """Steps 1 to 5 above (restore.ps1, after pg_restore and before the services start). Without apply nothing is
    kept (a dry run that says what it would do)."""
    data = _read(in_path)
    before = {u["email"].lower(): u for u in data.get("users") or []
              if isinstance(u, dict) and isinstance(u.get("email"), str)}
    out: dict[str, Any] = {"kept": [], "reverted": [], "skipped": [], "sessions_ended": 0, "audit_kept": 0,
                           "audit_not_kept": "", "access_not_carried": ""}
    async with sessionmaker()() as s:
        out["audit_kept"], out["audit_not_kept"] = await _carry_audit(s, data.get("audit") or [])
        await (s.commit() if apply else s.rollback())
    if migrate and apply:
        await run_migrations()  # before the users are read: an older dump has an older schema
    async with sessionmaker()() as s:
        users = {u.email.lower(): u for u in (await s.scalars(select(User).order_by(User.id))).all()}
        if not _same_database(users, before):
            out["access_not_carried"] = ("the restored users are not this database's own (another installation's "
                                         "backup): access is as in the backup")
            users = {}
        admins = {e for e, u in users.items() if u.role == "admin" and u.is_active}
        for email in sorted(set(before) - set(users)) if users else []:
            out["reverted"].append("%s: created after the backup; the restore removes this user (create it again)"
                                   % email)
        for email, user in users.items():
            was = before.get(email)
            if was is None:
                continue  # in the backup but not in the file: nothing to compare (users are never deleted)
            changes: dict[str, Any] = {}
            if was.get("is_active") is False and user.is_active:
                changes["is_active"] = False
            elif was.get("is_active") is True and not user.is_active:
                out["reverted"].append("%s: reactivated after the backup; the restore deactivates this user again"
                                       % email)
            if was.get("can_access_sensitive") is False and user.can_access_sensitive:
                changes["can_access_sensitive"] = False
            elif was.get("can_access_sensitive") is True and not user.can_access_sensitive:
                out["reverted"].append("%s: given the sensitive-data permission after the backup; the restore takes "
                                       "it away again" % email)
            role = was.get("role")
            if role in RANK and user.role in RANK and RANK[role] < RANK[user.role]:
                changes["role"] = role
            elif role in RANK and user.role in RANK and RANK[role] > RANK[user.role]:
                out["reverted"].append("%s: role raised to %s after the backup; the restore puts back %s"
                                       % (email, role, user.role))
            if was.get("password_fp") and was["password_fp"] != _password_fp(user.password_hash):
                out["reverted"].append("%s: password changed after the backup; the older password works again "
                                       "(change it again)" % email)
            if not changes:
                continue
            loses_admin = user.role == "admin" and user.is_active and (
                changes.get("is_active") is False or changes.get("role", "admin") != "admin")
            if loses_admin and not admins - {email}:
                out["skipped"].append("%s: %s not applied again: it would leave no active admin (change it by hand "
                                      "once another admin can sign in)" % (email, ", ".join(sorted(changes))))
                continue
            for key, value in changes.items():
                setattr(user, key, value)
            if loses_admin:
                admins.discard(email)
            out["kept"].append("%s: %s" % (email, ", ".join(
                "deactivated" if k == "is_active" else "sensitive-data permission removed"
                if k == "can_access_sensitive" else "role lowered to %s" % v for k, v in sorted(changes.items()))))
            await audit.record(s, ACTOR, "user.updated", "user", user.id,
                               {**changes, "via": "restore", "reason": "made after the restored backup"})
        out["sessions_ended"] = int(await s.scalar(select(func.count()).select_from(Session)) or 0)
        await s.execute(delete(Session))
        await audit.record(s, ACTOR, "system.restore_carried", "system", "", {
            "access_kept": len(out["kept"]), "reverted": len(out["reverted"]), "skipped": len(out["skipped"]),
            "sessions_ended": out["sessions_ended"], "audit_kept": out["audit_kept"]})
        await finish_part(s, "access")  # with the changes above: applied again, so its part of the mark goes
        await (s.commit() if apply else s.rollback())
    return out
