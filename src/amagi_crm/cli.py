"""Command-line interface: ``amagi-crm <command>``.

Everything the web UI does is also here, so the CRM can be scripted and run
headless. Settings come from ``.env`` in the current folder (or ``--env-file``)
and from ``AMAGI_CRM_*`` environment variables.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__

if TYPE_CHECKING:
    from .services import CRM

ACTOR = "cli"


class UsageError(Exception):
    """Bad input to a CLI command (printed without a traceback)."""


def _money(cents: int) -> str:
    return f"${cents / 100:,.0f}" if cents % 100 == 0 else f"${cents / 100:,.2f}"


def _parse_amount(text: str) -> int:
    try:
        return round(float(text.replace("$", "").replace(",", "")) * 100)
    except ValueError as exc:
        raise UsageError(f"not an amount: {text!r}") from exc


def _parse_date(text: str | None, crm: CRM) -> date:
    if not text or text == "today":
        return crm.today()
    if text == "tomorrow":
        return crm.today() + timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise UsageError(f"not an ISO date (YYYY-MM-DD): {text!r}") from exc


def _open(args) -> CRM:
    from .config import Settings
    from .db import Database
    from .services import CRM

    db = Database(Settings.load(args.env_file))
    db.create_all()
    return CRM(db)


def _table(rows: list[list[str]], headers: list[str]) -> str:
    if not rows:
        return ""
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    lines = [fmt.format(*headers).rstrip(), fmt.format(*["-" * w for w in widths]).rstrip()]
    lines += [fmt.format(*[str(c) for c in r]).rstrip() for r in rows]
    return "\n".join(lines)


def _contact_by_email(crm: CRM, email: str | None) -> int | None:
    if not email:
        return None
    c = crm.find_contact_by_email(email)
    if c is None:
        raise UsageError(f"no contact with email {email}")
    return c.id


# --- commands ----------------------------------------------------------------

ENV_TEMPLATE = """# amagi-crm settings. Keep this file private: it holds the keys that decrypt your data.
# Losing AMAGI_CRM_ENCRYPTION_KEYS means losing the encrypted fields. Back it up separately
# from the database.
DATABASE_URL={database_url}
AMAGI_CRM_ENCRYPTION_KEYS={key}
AMAGI_CRM_SECRET_KEY={secret}
AMAGI_CRM_TIMEZONE={timezone}
AMAGI_CRM_WORKING_HOURS={hours}
AMAGI_CRM_SLOT_MINUTES=30
AMAGI_CRM_PUBLIC_BOOKING=0
AMAGI_CRM_FEED_DETAILS=0
"""


def cmd_init(args) -> int:
    from .crypto import generate_fernet_key, generate_secret

    env_path = Path(args.env_file)
    if env_path.exists():
        print(f"Using existing {env_path}")
    else:
        from .scheduling import get_zone, parse_working_hours

        get_zone(args.timezone)
        parse_working_hours(args.hours)
        env_path.write_text(ENV_TEMPLATE.format(
            database_url=args.database_url, key=generate_fernet_key(), secret=generate_secret(),
            timezone=args.timezone, hours=args.hours), encoding="utf-8")
        print(f"Wrote {env_path} with new encryption keys. Back it up somewhere safe.")
    crm = _open(args)
    if crm.list_users():
        print("Already set up: users exist. Add more with `amagi-crm user add NAME`.")
        return 0
    _, token = crm.create_user(args.admin, "admin", ACTOR)
    print(f"Created admin user {args.admin!r}. API token (shown once, store it now):")
    print(f"  {token}")
    print("Next: amagi-crm serve, then open http://127.0.0.1:8000")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .api import create_app

    crm = _open(args)
    print(f"amagi-crm {__version__} on http://{args.host}:{args.port} "
          f"(timezone {crm.settings.timezone}, public booking {'on' if crm.settings.public_booking else 'off'})")
    uvicorn.run(create_app(crm), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_user(args) -> int:
    crm = _open(args)
    if args.user_cmd == "add":
        user, token = crm.create_user(args.name, args.role, ACTOR)
        print(f"Created {user.role} user {user.name!r}. API token (shown once):")
        print(f"  {token}")
    elif args.user_cmd == "list":
        rows = [[u.id, u.name, u.role, "revoked" if u.revoked_at else "active"] for u in crm.list_users()]
        print(_table(rows, ["id", "name", "role", "status"]) or "No users.")
    elif args.user_cmd == "revoke":
        crm.revoke_user(args.name, ACTOR)
        print(f"Revoked {args.name!r}. Their API token and calendar feed stop working now.")
    elif args.user_cmd == "feed-token":
        token = crm.issue_feed_token(args.name, ACTOR)
        print(f"Calendar feed for {args.name!r} (subscribe in Google/Outlook/Apple Calendar):")
        print(f"  {args.base_url.rstrip('/')}/feeds/{token}.ics")
        print("Anyone with this link sees busy times. Run this again to replace the link.")
    return 0


def cmd_import(args) -> int:
    from .importer import read_contacts_csv

    path = Path(args.csv)
    text = path.read_text(encoding="utf-8-sig")
    rows, ignored = read_contacts_csv(text)
    if ignored:
        print("Ignored columns: " + ", ".join(ignored))
    crm = _open(args)
    result = crm.import_contacts(rows, ACTOR, dry_run=args.dry_run)
    verb = "Dry run, nothing written. Would import" if args.dry_run else "Imported"
    print(f"{verb} {result['created']} contacts from {path.name}: "
          f"{result['duplicates']} duplicate emails skipped, {result['invalid']} invalid rows.")
    for err in result["errors"]:
        print(f"  {err}")
    return 0


def cmd_export(args) -> int:
    crm = _open(args)
    data = crm.export_contacts_csv(ACTOR)
    if args.out == "-":
        sys.stdout.write(data)
    else:
        Path(args.out).write_text(data, encoding="utf-8", newline="")
        print(f"Wrote {data.count(chr(10)) - 1} contacts to {args.out} (decrypted; handle with care).")
    return 0


def cmd_contacts(args) -> int:
    crm = _open(args)
    if args.contacts_cmd == "add":
        data = {k: getattr(args, k) for k in ("first_name", "last_name", "email", "phone", "company", "notes")
                if getattr(args, k)}
        if args.tags:
            data["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
        c = crm.create_contact(data, ACTOR)
        print(f"Added contact #{c.id}: {crm.display_name(c)}")
        return 0
    rows, total = crm.list_contacts(ACTOR, search=args.search, tag=args.tag, limit=args.limit)
    table = [[c.id, crm.display_name(c), c.company or "", c.email or "", c.phone or "", ",".join(c.tags or [])]
             for c in rows]
    print(_table(table, ["id", "name", "company", "email", "phone", "tags"]) or "No contacts.")
    if total > len(rows):
        print(f"({len(rows)} of {total} shown; use --limit)")
    return 0


def cmd_deals(args) -> int:
    crm = _open(args)
    if args.deals_cmd == "add":
        data = {"title": args.title, "amount_cents": _parse_amount(args.amount),
                "contact_id": _contact_by_email(crm, args.contact_email)}
        if args.stage:
            data["stage"] = args.stage
        if args.close:
            data["expected_close"] = args.close
        d = crm.create_deal(data, ACTOR)
        print(f"Added deal #{d.id}: {d.title} ({_money(d.amount_cents)})")
    elif args.deals_cmd == "move":
        d = crm.move_deal(args.id, args.stage, None, ACTOR)
        stage = next(s.name for s in crm.list_stages() if s.id == d.stage_id)
        print(f"Moved deal #{d.id} {d.title!r} to {stage}.")
    elif args.deals_cmd == "board":
        for col in crm.board():
            print(f"{col['name']} ({col['count']}, {_money(col['total_cents'])})")
            for d in col["deals"]:
                print(f"  #{d['id']:<4} {d['title']:<32} {_money(d['amount_cents']):>10}")
    elif args.deals_cmd == "summary":
        if args.start:
            start = _parse_date(args.start, crm)
            end = _parse_date(args.end, crm) if args.end else start + timedelta(days=6)
        else:
            today = crm.today()
            start = today - timedelta(days=today.weekday())
            end = start + timedelta(days=6)
        s = crm.deal_summary(start, end)
        if args.json:
            print(json.dumps(s, indent=2))
            return 0
        print(f"Pipeline, {s['start']} to {s['end']}")
        print(f"  New deals   {s['new']['count']:>3}  {_money(s['new']['total_cents']):>10}")
        print(f"  Won         {s['won']['count']:>3}  {_money(s['won']['total_cents']):>10}")
        print(f"  Lost        {s['lost']['count']:>3}  {_money(s['lost']['total_cents']):>10}")
        print(f"  Open now    {s['open_pipeline']['count']:>3}  {_money(s['open_pipeline']['total_cents']):>10}")
        for st in s["open_pipeline"]["by_stage"]:
            print(f"    {st['stage']:<12}{st['count']:>3}  {_money(st['total_cents']):>10}")
    return 0


def cmd_tasks(args) -> int:
    crm = _open(args)
    if args.tasks_cmd == "add":
        data = {"title": args.title, "contact_id": _contact_by_email(crm, args.contact_email)}
        if args.due:
            data["due_date"] = _parse_date(args.due, crm).isoformat()
        t = crm.create_task(data, ACTOR)
        print(f"Added task #{t.id}: {t.title}" + (f" (due {t.due_date})" if t.due_date else ""))
    elif args.tasks_cmd == "done":
        t = crm.update_task(args.id, {"done": True}, ACTOR)
        print(f"Done: #{t.id} {t.title}")
    else:
        tasks = crm.list_tasks(done=None if args.all else False)
        today = crm.today()
        rows = [[t.id, "x" if t.done else " ", t.due_date or "",
                 "OVERDUE" if (t.due_date and not t.done and t.due_date < today) else "", t.title] for t in tasks]
        print(_table(rows, ["id", "done", "due", "flag", "title"]) or "No open tasks.")
    return 0


def cmd_book(args) -> int:
    crm = _open(args)
    data = {"title": args.title, "start": args.start, "duration_minutes": args.minutes,
            "contact_id": _contact_by_email(crm, args.contact_email)}
    if args.location:
        data["location"] = args.location
    a = crm.create_appointment(data, ACTOR)
    d = crm.appt_dict(a)
    print(f"Booked #{a.id}: {a.title}, {d['start'][:16].replace('T', ' ')} to {d['end'][11:16]}")
    return 0


def cmd_agenda(args) -> int:
    crm = _open(args)
    day = _parse_date(args.date, crm)
    print(f"{day:%A %d %B %Y} ({crm.settings.timezone})")
    appts = crm.appointments_on(day)
    if not appts:
        print("  No appointments.")
    for a in appts:
        d = crm.appt_dict(a)
        extra = " (booked online)" if a.source == "public" else ""
        print(f"  {d['start'][11:16]}-{d['end'][11:16]}  {a.title}{extra}")
    due = crm.list_tasks(done=False, due_on_or_before=day)
    if due:
        print("Tasks due:")
        for t in due:
            print(f"  #{t.id} {t.title} ({'overdue, ' if t.due_date < day else ''}due {t.due_date})")
    slots = crm.availability(day)
    print("Open slots: " + (", ".join(s.strftime("%H:%M") for s in slots) if slots else "none"))
    return 0


def cmd_audit(args) -> int:
    crm = _open(args)
    if args.audit_cmd == "verify":
        r = crm.audit_verify()
        if r.ok:
            print(f"Audit chain OK: {r.count} entries, head {r.head_hash}")
            return 0
        print(f"Audit chain BROKEN at entry {r.bad_id}: {r.reason}")
        return 1
    for e in crm.audit_tail(args.n):
        target = e["entity"] + (f" #{e['entity_id']}" if e["entity_id"] else "")
        fields = ", ".join(json.loads(e["fields"]))
        print(f"{e['ts'][:19].replace('T', ' ')}  {e['actor']:<14} {e['action']:<11} {target:<16} {fields}")
    return 0


def cmd_rotate_keys(args) -> int:
    crm = _open(args)
    counts = crm.rotate_keys(ACTOR)
    total = sum(counts.values())
    print(f"Re-encrypted {total} rows with the primary key: "
          + ", ".join(f"{k} {v}" for k, v in counts.items()))
    if total:
        print("Once you have a backup, you can drop the old key from AMAGI_CRM_ENCRYPTION_KEYS.")
    return 0


def cmd_keygen(args) -> int:
    import secrets

    from .crypto import generate_fernet_key, generate_secret

    if args.key_only:
        print(generate_fernet_key())
        return 0
    print("# amagi-crm secrets. Keep them apart from database backups: whoever has both can read the data.")
    print(f"AMAGI_CRM_ENCRYPTION_KEYS={generate_fernet_key()}")
    print(f"AMAGI_CRM_SECRET_KEY={generate_secret()}")
    if args.postgres:
        print(f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}")
    return 0


# --- parser ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="amagi-crm",
        description="Self-hosted CRM for small firms: contacts, deal pipeline, tasks and booking, "
                    "encrypted at rest with a tamper-evident audit log.")
    p.add_argument("--version", action="version", version=f"amagi-crm {__version__}")
    p.add_argument("--env-file", default=".env", help="settings file (default: ./.env)")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("init", help="create .env with fresh keys, the database, and the first admin")
    s.add_argument("--admin", default="admin", help="name of the first admin user (default: admin)")
    s.add_argument("--timezone", default="UTC", help="firm time zone, e.g. America/New_York")
    s.add_argument("--hours", default="mon-fri 09:00-17:00", help='working hours, e.g. "mon-fri 09:00-17:00; sat 10:00-13:00"')
    s.add_argument("--database-url", default="sqlite:///./amagi_crm.db", help="SQLAlchemy URL (default: SQLite file here)")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("serve", help="run the web UI and REST API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("user", help="manage users and API tokens")
    us = s.add_subparsers(dest="user_cmd", required=True, metavar="ACTION")
    a = us.add_parser("add", help="create a user and print their API token")
    a.add_argument("name")
    a.add_argument("--role", choices=["admin", "member", "readonly"], default="member")
    us.add_parser("list", help="list users")
    a = us.add_parser("revoke", help="revoke a user's token")
    a.add_argument("name")
    a = us.add_parser("feed-token", help="issue a private calendar (.ics) feed link")
    a.add_argument("name")
    a.add_argument("--base-url", default="http://127.0.0.1:8000")
    s.set_defaults(func=cmd_user)

    s = sub.add_parser("import", help="import contacts from a CSV export (GoHighLevel-style headers work)")
    s.add_argument("csv")
    s.add_argument("--dry-run", action="store_true", help="run every check, write nothing")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("export", help="export all contacts to CSV (decrypted)")
    s.add_argument("--out", default="-", help="file to write (default: stdout)")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("contacts", help="list or add contacts")
    cs = s.add_subparsers(dest="contacts_cmd", metavar="ACTION")
    s.add_argument("--search")
    s.add_argument("--tag")
    s.add_argument("--limit", type=int, default=50)
    a = cs.add_parser("add", help="add a contact")
    for flag in ("--first-name", "--last-name", "--email", "--phone", "--company", "--tags", "--notes"):
        a.add_argument(flag)
    s.set_defaults(func=cmd_contacts, contacts_cmd="list")

    s = sub.add_parser("deals", help="pipeline: board, add, move, weekly summary")
    ds = s.add_subparsers(dest="deals_cmd", required=True, metavar="ACTION")
    ds.add_parser("board", help="show the pipeline by stage")
    a = ds.add_parser("add", help="add a deal")
    a.add_argument("title")
    a.add_argument("--amount", default="0", help="e.g. 4800 or 4,800.00")
    a.add_argument("--contact-email")
    a.add_argument("--stage", help="stage name (default: first stage)")
    a.add_argument("--close", help="expected close date, YYYY-MM-DD")
    a = ds.add_parser("move", help="move a deal to another stage")
    a.add_argument("id", type=int)
    a.add_argument("stage")
    a = ds.add_parser("summary", help="new, won, lost and open pipeline for a week")
    a.add_argument("--start", help="first day (default: this Monday)")
    a.add_argument("--end", help="last day (default: start + 6 days)")
    a.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_deals)

    s = sub.add_parser("tasks", help="list, add or complete tasks")
    ts = s.add_subparsers(dest="tasks_cmd", metavar="ACTION")
    s.add_argument("--all", action="store_true", help="include finished tasks")
    a = ts.add_parser("add", help="add a task")
    a.add_argument("title")
    a.add_argument("--due", help="YYYY-MM-DD, today or tomorrow")
    a.add_argument("--contact-email")
    a = ts.add_parser("done", help="mark a task done")
    a.add_argument("id", type=int)
    s.set_defaults(func=cmd_tasks, tasks_cmd="list")

    s = sub.add_parser("book", help="book an appointment (refuses overlaps)")
    s.add_argument("start", help='firm-local time, e.g. "2026-11-02 10:00"')
    s.add_argument("--title", required=True)
    s.add_argument("--minutes", type=int, default=30)
    s.add_argument("--contact-email")
    s.add_argument("--location")
    s.set_defaults(func=cmd_book)

    s = sub.add_parser("agenda", help="appointments, tasks due and open slots for a day")
    s.add_argument("--date", help="YYYY-MM-DD, today (default) or tomorrow")
    s.set_defaults(func=cmd_agenda)

    s = sub.add_parser("audit", help="verify or read the audit log")
    aus = s.add_subparsers(dest="audit_cmd", required=True, metavar="ACTION")
    aus.add_parser("verify", help="check the hash chain; exit 1 if anything was altered")
    a = aus.add_parser("tail", help="show the latest entries")
    a.add_argument("-n", type=int, default=20)
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("rotate-keys", help="re-encrypt old rows with the first key in AMAGI_CRM_ENCRYPTION_KEYS")
    s.set_defaults(func=cmd_rotate_keys)

    s = sub.add_parser("keygen", help="print fresh secrets for a .env file (nothing is written)")
    s.add_argument("--postgres", action="store_true", help="also print a POSTGRES_PASSWORD (for Docker Compose)")
    s.add_argument("--key-only", action="store_true", help="print one new encryption key, for key rotation")
    s.set_defaults(func=cmd_keygen)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from .config import ConfigError
    from .scheduling import ScheduleError
    from .services import CRMError

    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"amagi-crm: {exc}", file=sys.stderr)
        return 2
    except (CRMError, ScheduleError, UsageError, OSError) as exc:
        print(f"amagi-crm: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
