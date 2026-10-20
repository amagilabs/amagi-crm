"""Business logic shared by the REST API and the CLI.

Every write goes through ``audit.write_txn`` and records an audit entry in the
same transaction, so a change and its audit row commit together or not at all.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets
from datetime import date, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from . import audit, scheduling
from .db import Database
from .models import (
    ROLES,
    STAGE_KINDS,
    Appointment,
    Contact,
    Deal,
    Encrypted,
    Stage,
    Task,
    User,
    utcnow,
)


class CRMError(Exception):
    status = 400


class NotFound(CRMError):
    status = 404


class Conflict(CRMError):
    status = 409

    def __init__(self, message: str, existing_id: int | None = None):
        super().__init__(message)
        self.existing_id = existing_id


class Invalid(CRMError):
    status = 422


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CONTACT_FIELDS = ("first_name", "last_name", "email", "phone", "company", "tags", "notes")
DEAL_FIELDS = ("title", "contact_id", "stage_id", "amount_cents", "expected_close", "notes")
TASK_FIELDS = ("title", "notes", "due_date", "done", "contact_id", "deal_id", "assignee_id")
APPT_FIELDS = ("title", "notes", "location", "contact_id", "start", "end")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _clean_str(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise Invalid(f"not an ISO date (YYYY-MM-DD): {value!r}") from exc


def _int_or_none(value, name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise Invalid(f"{name} must be an integer") from exc


class CRM:
    def __init__(self, db: Database):
        self.db = db
        self.settings = db.settings
        self.zone = scheduling.get_zone(self.settings.timezone)
        self.hours = scheduling.parse_working_hours(self.settings.working_hours)

    def session(self) -> Session:
        return self.db.session()

    def today(self) -> date:
        return datetime.now(self.zone).date()

    # --- serialization -------------------------------------------------------

    def _iso(self, value_utc: datetime | None) -> str | None:
        if value_utc is None:
            return None
        return scheduling.to_local(value_utc, self.zone).isoformat()

    def contact_dict(self, c: Contact) -> dict:
        return {
            "id": c.id, "first_name": c.first_name, "last_name": c.last_name,
            "email": c.email, "phone": c.phone, "company": c.company,
            "tags": c.tags or [], "notes": c.notes,
            "created_at": self._iso(c.created_at), "updated_at": self._iso(c.updated_at),
        }

    @staticmethod
    def display_name(c: Contact) -> str:
        name = " ".join(p for p in (c.first_name, c.last_name) if p)
        return name or c.email or f"contact #{c.id}"

    def deal_dict(self, d: Deal) -> dict:
        return {
            "id": d.id, "title": d.title, "contact_id": d.contact_id, "stage_id": d.stage_id,
            "amount_cents": d.amount_cents,
            "expected_close": d.expected_close.isoformat() if d.expected_close else None,
            "notes": d.notes, "position": d.position,
            "created_at": self._iso(d.created_at), "updated_at": self._iso(d.updated_at),
            "closed_at": self._iso(d.closed_at),
        }

    def task_dict(self, t: Task) -> dict:
        return {
            "id": t.id, "title": t.title, "notes": t.notes,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "done": t.done, "contact_id": t.contact_id, "deal_id": t.deal_id,
            "assignee_id": t.assignee_id, "created_at": self._iso(t.created_at),
            "completed_at": self._iso(t.completed_at),
        }

    def appt_dict(self, a: Appointment) -> dict:
        return {
            "id": a.id, "title": a.title, "notes": a.notes, "location": a.location,
            "contact_id": a.contact_id, "start": self._iso(a.start_utc), "end": self._iso(a.end_utc),
            "status": a.status, "source": a.source, "created_at": self._iso(a.created_at),
        }

    @staticmethod
    def stage_dict(s: Stage) -> dict:
        return {"id": s.id, "name": s.name, "position": s.position, "kind": s.kind}

    @staticmethod
    def user_dict(u: User) -> dict:
        return {"id": u.id, "name": u.name, "role": u.role, "revoked": u.revoked_at is not None}

    # --- users & auth --------------------------------------------------------

    def create_user(self, name: str, role: str, actor: str) -> tuple[User, str]:
        name = _clean_str(name)
        if not name:
            raise Invalid("user name is required")
        if role not in ROLES:
            raise Invalid(f"role must be one of {', '.join(ROLES)}")
        token = "acrm_" + secrets.token_urlsafe(32)
        with self.session() as s, audit.write_txn(s):
            if s.scalar(select(User).where(User.name == name)):
                raise Conflict(f"user {name!r} already exists")
            user = User(name=name, role=role, token_hash=hash_token(token))
            s.add(user)
            s.flush()
            audit.record(s, actor, "create", "user", user.id, ["name", "role"])
        return user, token

    def list_users(self) -> list[User]:
        with self.session() as s:
            return list(s.scalars(select(User).order_by(User.id)))

    def revoke_user(self, name: str, actor: str) -> User:
        with self.session() as s, audit.write_txn(s):
            user = s.scalar(select(User).where(User.name == name))
            if not user:
                raise NotFound(f"no user named {name!r}")
            user.revoked_at = utcnow()
            audit.record(s, actor, "revoke", "user", user.id, ["revoked_at"])
        return user

    def authenticate(self, token: str | None) -> User | None:
        if not token:
            return None
        with self.session() as s:
            user = s.scalar(select(User).where(User.token_hash == hash_token(token)))
        if user is None or user.revoked_at is not None:
            return None
        return user

    def issue_feed_token(self, name: str, actor: str) -> str:
        token = "acrmfeed_" + secrets.token_urlsafe(32)
        with self.session() as s, audit.write_txn(s):
            user = s.scalar(select(User).where(User.name == name))
            if not user:
                raise NotFound(f"no user named {name!r}")
            user.feed_token_hash = hash_token(token)
            audit.record(s, actor, "feed-token", "user", user.id, ["feed_token_hash"])
        return token

    def user_for_feed(self, token: str) -> User | None:
        with self.session() as s:
            user = s.scalar(select(User).where(User.feed_token_hash == hash_token(token)))
        if user is None or user.revoked_at is not None:
            return None
        return user

    # --- contacts ------------------------------------------------------------

    def _apply_contact(self, c: Contact, data: dict) -> list[str]:
        changed = []
        for key in CONTACT_FIELDS:
            if key not in data:
                continue
            value = data[key]
            if key == "tags":
                if value is None:
                    value = []
                if isinstance(value, str):
                    value = [t for t in (p.strip() for p in value.split(",")) if t]
                if not isinstance(value, list):
                    raise Invalid("tags must be a list of strings")
                value = sorted({str(t).strip() for t in value if str(t).strip()})
            else:
                value = _clean_str(value)
            if key == "email" and value:
                value = value.lower()
                if not EMAIL_RE.match(value):
                    raise Invalid(f"not a valid email address: {value!r}")
            if getattr(c, key) != value:
                setattr(c, key, value)
                changed.append(key)
        if "email" in changed:
            c.email_index = self.db.cipher.blind_index(c.email)
        if not (c.first_name or c.last_name or c.email or c.company):
            raise Invalid("a contact needs at least a name, email, or company")
        return changed

    def _check_email_free(self, s: Session, email: str | None, exclude_id: int | None = None) -> None:
        idx = self.db.cipher.blind_index(email)
        if not idx:
            return
        q = select(Contact.id).where(Contact.email_index == idx)
        if exclude_id is not None:
            q = q.where(Contact.id != exclude_id)
        existing = s.scalar(q.limit(1))
        if existing is not None:
            raise Conflict("a contact with that email already exists", existing_id=existing)

    def find_contact_by_email(self, email: str) -> Contact | None:
        idx = self.db.cipher.blind_index(email)
        if not idx:
            return None
        with self.session() as s:
            return s.scalar(select(Contact).where(Contact.email_index == idx).limit(1))

    def create_contact(self, data: dict, actor: str) -> Contact:
        with self.session() as s, audit.write_txn(s):
            c = Contact()
            changed = self._apply_contact(c, data)
            self._check_email_free(s, c.email)
            s.add(c)
            s.flush()
            audit.record(s, actor, "create", "contact", c.id, changed)
        return c

    def get_contact(self, contact_id: int, actor: str) -> Contact:
        with self.session() as s, audit.write_txn(s):
            c = s.get(Contact, contact_id)
            if c is None:
                raise NotFound(f"contact {contact_id} not found")
            audit.record(s, actor, "read", "contact", c.id)
        return c

    def list_contacts(self, actor: str, search: str | None = None, tag: str | None = None,
                      limit: int = 100, offset: int = 0) -> tuple[list[Contact], int]:
        """Encrypted columns can't be filtered in SQL, so search runs on decrypted rows
        in the app. Fine for a small firm's book of business (tens of thousands of rows)."""
        with self.session() as s, audit.write_txn(s):
            rows = list(s.scalars(select(Contact).order_by(Contact.id)))
            audit.record(s, actor, "list", "contact", None, ["search"] if search else [])
        needle = (search or "").strip().lower()
        if needle:
            rows = [c for c in rows if needle in " ".join(
                filter(None, [c.first_name, c.last_name, c.email, c.phone, c.company])).lower()]
        if tag:
            rows = [c for c in rows if tag in (c.tags or [])]
        rows.sort(key=lambda c: ((c.last_name or "").lower(), (c.first_name or "").lower(), c.id))
        return rows[offset:offset + limit], len(rows)

    def update_contact(self, contact_id: int, data: dict, actor: str) -> Contact:
        with self.session() as s, audit.write_txn(s):
            c = s.get(Contact, contact_id)
            if c is None:
                raise NotFound(f"contact {contact_id} not found")
            changed = self._apply_contact(c, data)
            if "email" in changed:
                self._check_email_free(s, c.email, exclude_id=c.id)
            if changed:
                audit.record(s, actor, "update", "contact", c.id, changed)
        return c

    def delete_contact(self, contact_id: int, actor: str) -> None:
        with self.session() as s, audit.write_txn(s):
            c = s.get(Contact, contact_id)
            if c is None:
                raise NotFound(f"contact {contact_id} not found")
            s.delete(c)
            audit.record(s, actor, "delete", "contact", contact_id)

    def import_contacts(self, rows: list[tuple[int, dict]], actor: str) -> dict:
        """Create contacts from ``(line_number, fields)`` rows (see ``importer.read_contacts_csv``). Duplicate emails (already stored, or
        repeated in the file) are skipped, not merged."""
        result = {"created": 0, "duplicates": 0, "invalid": 0, "errors": []}
        with self.session() as s, audit.write_txn(s):
            seen: set[str] = set()
            for lineno, row in rows:
                c = Contact()
                try:
                    changed = self._apply_contact(c, row)
                except Invalid as exc:
                    result["invalid"] += 1
                    result["errors"].append(f"row {lineno}: {exc}")
                    continue
                if c.email_index:
                    if c.email_index in seen or s.scalar(
                            select(Contact.id).where(Contact.email_index == c.email_index).limit(1)):
                        result["duplicates"] += 1
                        continue
                    seen.add(c.email_index)
                s.add(c)
                s.flush()
                audit.record(s, actor, "import", "contact", c.id, changed)
                result["created"] += 1
        return result

    def export_contacts_csv(self, actor: str) -> str:
        with self.session() as s, audit.write_txn(s):
            rows = list(s.scalars(select(Contact).order_by(Contact.id)))
            audit.record(s, actor, "export", "contact", None, list(CONTACT_FIELDS))
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", *CONTACT_FIELDS])
        for c in rows:
            writer.writerow([c.id, c.first_name, c.last_name, c.email, c.phone, c.company,
                             ",".join(c.tags or []), c.notes])
        return buf.getvalue()

    # --- stages --------------------------------------------------------------

    def list_stages(self) -> list[Stage]:
        with self.session() as s:
            return list(s.scalars(select(Stage).order_by(Stage.position, Stage.id)))

    def _resolve_stage(self, s: Session, stage) -> Stage:
        if isinstance(stage, int) or (isinstance(stage, str) and stage.isdigit()):
            found = s.get(Stage, int(stage))
        else:
            found = s.scalar(select(Stage).where(func.lower(Stage.name) == str(stage).strip().lower()))
        if found is None:
            raise Invalid(f"unknown pipeline stage: {stage!r}")
        return found

    def create_stage(self, name: str, kind: str, actor: str) -> Stage:
        name = _clean_str(name)
        if not name:
            raise Invalid("stage name is required")
        if kind not in STAGE_KINDS:
            raise Invalid(f"stage kind must be one of {', '.join(STAGE_KINDS)}")
        with self.session() as s, audit.write_txn(s):
            if s.scalar(select(Stage).where(func.lower(Stage.name) == name.lower())):
                raise Conflict(f"stage {name!r} already exists")
            last = s.scalar(select(Stage.position).order_by(Stage.position.desc()).limit(1)) or 0
            stage = Stage(name=name, kind=kind, position=last + 1)
            s.add(stage)
            s.flush()
            audit.record(s, actor, "create", "stage", stage.id, ["name", "kind"])
        return stage

    # --- deals ---------------------------------------------------------------

    def _apply_deal(self, s: Session, d: Deal, data: dict) -> list[str]:
        changed = []
        for key in DEAL_FIELDS:
            if key not in data:
                continue
            value = data[key]
            if key == "title":
                value = _clean_str(value)
                if not value:
                    raise Invalid("deal title is required")
            elif key == "notes":
                value = _clean_str(value)
            elif key == "amount_cents":
                value = _int_or_none(value, "amount_cents") or 0
                if value < 0:
                    raise Invalid("amount_cents can't be negative")
            elif key == "expected_close":
                value = _parse_date(value)
            elif key == "contact_id":
                value = _int_or_none(value, "contact_id")
                if value is not None and s.get(Contact, value) is None:
                    raise Invalid(f"contact {value} not found")
            elif key == "stage_id":
                value = self._resolve_stage(s, value).id
            if getattr(d, key) != value:
                setattr(d, key, value)
                changed.append(key)
        if "stage_id" in changed:
            stage = s.get(Stage, d.stage_id)
            d.closed_at = None if stage.kind == "open" else utcnow()
        return changed

    def create_deal(self, data: dict, actor: str) -> Deal:
        data = dict(data)
        if "stage" in data:
            data["stage_id"] = data.pop("stage")
        with self.session() as s, audit.write_txn(s):
            if data.get("stage_id") in (None, ""):
                data["stage_id"] = s.scalar(select(Stage.id).order_by(Stage.position, Stage.id).limit(1))
            d = Deal()
            changed = self._apply_deal(s, d, data)
            if not d.title:
                raise Invalid("deal title is required")
            top = s.scalar(select(Deal.position).where(Deal.stage_id == d.stage_id)
                           .order_by(Deal.position.desc()).limit(1))
            d.position = (top + 1) if top is not None else 0
            s.add(d)
            s.flush()
            audit.record(s, actor, "create", "deal", d.id, changed)
        return d

    def get_deal(self, deal_id: int, actor: str) -> Deal:
        with self.session() as s, audit.write_txn(s):
            d = s.get(Deal, deal_id)
            if d is None:
                raise NotFound(f"deal {deal_id} not found")
            audit.record(s, actor, "read", "deal", d.id)
        return d

    def list_deals(self, stage_id: int | None = None) -> list[Deal]:
        with self.session() as s:
            q = select(Deal).order_by(Deal.stage_id, Deal.position, Deal.id)
            if stage_id is not None:
                q = q.where(Deal.stage_id == stage_id)
            return list(s.scalars(q))

    def update_deal(self, deal_id: int, data: dict, actor: str) -> Deal:
        data = dict(data)
        if "stage" in data:
            data["stage_id"] = data.pop("stage")
        with self.session() as s, audit.write_txn(s):
            d = s.get(Deal, deal_id)
            if d is None:
                raise NotFound(f"deal {deal_id} not found")
            changed = self._apply_deal(s, d, data)
            if changed:
                audit.record(s, actor, "update", "deal", d.id, changed)
        return d

    def move_deal(self, deal_id: int, stage, position: int | None, actor: str) -> Deal:
        """Kanban move: put the deal in ``stage`` at ``position`` (0 = top), renumbering
        the cards in that column."""
        with self.session() as s, audit.write_txn(s):
            d = s.get(Deal, deal_id)
            if d is None:
                raise NotFound(f"deal {deal_id} not found")
            changed = self._apply_deal(s, d, {"stage_id": stage})
            column = [x for x in s.scalars(select(Deal).where(Deal.stage_id == d.stage_id)
                                           .order_by(Deal.position, Deal.id)) if x.id != d.id]
            pos = len(column) if position is None else max(0, min(int(position), len(column)))
            column.insert(pos, d)
            for i, card in enumerate(column):
                if card.position != i:
                    card.position = i
                    if card is d and "position" not in changed:
                        changed.append("position")
            audit.record(s, actor, "move", "deal", d.id, changed or ["position"])
        return d

    def delete_deal(self, deal_id: int, actor: str) -> None:
        with self.session() as s, audit.write_txn(s):
            d = s.get(Deal, deal_id)
            if d is None:
                raise NotFound(f"deal {deal_id} not found")
            s.delete(d)
            audit.record(s, actor, "delete", "deal", deal_id)

    def board(self) -> list[dict]:
        stages = self.list_stages()
        deals = self.list_deals()
        columns = []
        for st in stages:
            cards = [self.deal_dict(d) for d in deals if d.stage_id == st.id]
            columns.append({**self.stage_dict(st), "deals": cards,
                            "count": len(cards), "total_cents": sum(c["amount_cents"] for c in cards)})
        return columns

    def deal_summary(self, start: date, end: date) -> dict:
        """What happened in the pipeline between ``start`` and ``end`` (inclusive, firm-local dates)."""
        lo = scheduling.to_utc_naive(datetime.combine(start, datetime.min.time()), self.zone)
        hi = scheduling.to_utc_naive(datetime.combine(end + timedelta(days=1), datetime.min.time()), self.zone)
        stages = {st.id: st for st in self.list_stages()}
        deals = self.list_deals()
        new = [d for d in deals if lo <= d.created_at < hi]
        won = [d for d in deals if d.closed_at and lo <= d.closed_at < hi and stages[d.stage_id].kind == "won"]
        lost = [d for d in deals if d.closed_at and lo <= d.closed_at < hi and stages[d.stage_id].kind == "lost"]
        open_deals = [d for d in deals if stages[d.stage_id].kind == "open"]
        by_stage = []
        for st in sorted(stages.values(), key=lambda x: (x.position, x.id)):
            if st.kind != "open":
                continue
            in_stage = [d for d in open_deals if d.stage_id == st.id]
            by_stage.append({"stage": st.name, "count": len(in_stage),
                             "total_cents": sum(d.amount_cents for d in in_stage)})
        return {
            "start": start.isoformat(), "end": end.isoformat(),
            "new": {"count": len(new), "total_cents": sum(d.amount_cents for d in new)},
            "won": {"count": len(won), "total_cents": sum(d.amount_cents for d in won),
                    "deals": [{"id": d.id, "title": d.title, "amount_cents": d.amount_cents} for d in won]},
            "lost": {"count": len(lost), "total_cents": sum(d.amount_cents for d in lost)},
            "open_pipeline": {"count": len(open_deals), "total_cents": sum(d.amount_cents for d in open_deals),
                              "by_stage": by_stage},
        }

    # --- tasks ---------------------------------------------------------------

    def _apply_task(self, s: Session, t: Task, data: dict) -> list[str]:
        changed = []
        for key in TASK_FIELDS:
            if key not in data:
                continue
            value = data[key]
            if key == "title":
                value = _clean_str(value)
                if not value:
                    raise Invalid("task title is required")
            elif key == "notes":
                value = _clean_str(value)
            elif key == "due_date":
                value = _parse_date(value)
            elif key == "done":
                value = bool(value)
            else:
                value = _int_or_none(value, key)
                model = {"contact_id": Contact, "deal_id": Deal, "assignee_id": User}[key]
                if value is not None and s.get(model, value) is None:
                    raise Invalid(f"{key} {value} not found")
            if getattr(t, key) != value:
                setattr(t, key, value)
                changed.append(key)
        if "done" in changed:
            t.completed_at = utcnow() if t.done else None
        return changed

    def create_task(self, data: dict, actor: str) -> Task:
        with self.session() as s, audit.write_txn(s):
            t = Task(done=False)
            changed = self._apply_task(s, t, data)
            if not t.title:
                raise Invalid("task title is required")
            s.add(t)
            s.flush()
            audit.record(s, actor, "create", "task", t.id, changed)
        return t

    def list_tasks(self, done: bool | None = None, due_on_or_before: date | None = None,
                   contact_id: int | None = None) -> list[Task]:
        with self.session() as s:
            q = select(Task)
            if done is not None:
                q = q.where(Task.done == done)
            if due_on_or_before is not None:
                q = q.where(Task.due_date.is_not(None), Task.due_date <= due_on_or_before)
            if contact_id is not None:
                q = q.where(Task.contact_id == contact_id)
            rows = list(s.scalars(q))
        rows.sort(key=lambda t: (t.done, t.due_date or date.max, t.id))
        return rows

    def update_task(self, task_id: int, data: dict, actor: str) -> Task:
        with self.session() as s, audit.write_txn(s):
            t = s.get(Task, task_id)
            if t is None:
                raise NotFound(f"task {task_id} not found")
            changed = self._apply_task(s, t, data)
            if changed:
                audit.record(s, actor, "update", "task", t.id, changed)
        return t

    def delete_task(self, task_id: int, actor: str) -> None:
        with self.session() as s, audit.write_txn(s):
            t = s.get(Task, task_id)
            if t is None:
                raise NotFound(f"task {task_id} not found")
            s.delete(t)
            audit.record(s, actor, "delete", "task", task_id)

    # --- appointments --------------------------------------------------------

    def _busy(self, s: Session, lo: datetime, hi: datetime, exclude_id: int | None = None):
        q = select(Appointment).where(Appointment.status == "booked",
                                      Appointment.start_utc < hi, Appointment.end_utc > lo)
        if exclude_id is not None:
            q = q.where(Appointment.id != exclude_id)
        return list(s.scalars(q))

    def _times(self, data: dict, current: Appointment | None = None) -> tuple[datetime, datetime]:
        start = (scheduling.parse_datetime(data["start"], self.zone) if data.get("start")
                 else current.start_utc if current else None)
        if start is None:
            raise Invalid("start is required")
        if data.get("end"):
            end = scheduling.parse_datetime(data["end"], self.zone)
        elif data.get("duration_minutes"):
            end = start + timedelta(minutes=_int_or_none(data["duration_minutes"], "duration_minutes"))
        elif current is not None:
            end = start + (current.end_utc - current.start_utc)
        else:
            end = start + timedelta(minutes=self.settings.slot_minutes)
        if end <= start:
            raise Invalid("end must be after start")
        return start, end

    def create_appointment(self, data: dict, actor: str, source: str = "staff") -> Appointment:
        try:
            start, end = self._times(data)
        except scheduling.ScheduleError as exc:
            raise Invalid(str(exc)) from exc
        title = _clean_str(data.get("title"))
        if not title:
            raise Invalid("appointment title is required")
        with self.session() as s, audit.write_txn(s):
            clash = self._busy(s, start, end)
            if clash:
                raise Conflict("that time overlaps another booked appointment", existing_id=clash[0].id)
            contact_id = _int_or_none(data.get("contact_id"), "contact_id")
            if contact_id is not None and s.get(Contact, contact_id) is None:
                raise Invalid(f"contact {contact_id} not found")
            a = Appointment(title=title, notes=_clean_str(data.get("notes")),
                            location=_clean_str(data.get("location")), contact_id=contact_id,
                            start_utc=start, end_utc=end, status="booked", source=source)
            s.add(a)
            s.flush()
            audit.record(s, actor, "create", "appointment", a.id,
                         [k for k in ("title", "notes", "location", "contact_id") if data.get(k)] + ["start", "end"])
        return a

    def list_appointments(self, start: datetime | None = None, end: datetime | None = None,
                          include_cancelled: bool = False) -> list[Appointment]:
        with self.session() as s:
            q = select(Appointment).order_by(Appointment.start_utc)
            if start is not None:
                q = q.where(Appointment.end_utc > start)
            if end is not None:
                q = q.where(Appointment.start_utc < end)
            if not include_cancelled:
                q = q.where(Appointment.status == "booked")
            return list(s.scalars(q))

    def appointments_on(self, day: date) -> list[Appointment]:
        lo = scheduling.to_utc_naive(datetime.combine(day, datetime.min.time()), self.zone)
        return self.list_appointments(lo, lo + timedelta(days=1))

    def update_appointment(self, appt_id: int, data: dict, actor: str) -> Appointment:
        with self.session() as s, audit.write_txn(s):
            a = s.get(Appointment, appt_id)
            if a is None:
                raise NotFound(f"appointment {appt_id} not found")
            changed = []
            if any(data.get(k) for k in ("start", "end", "duration_minutes")):
                try:
                    start, end = self._times(data, current=a)
                except scheduling.ScheduleError as exc:
                    raise Invalid(str(exc)) from exc
                if a.status == "booked":
                    clash = self._busy(s, start, end, exclude_id=a.id)
                    if clash:
                        raise Conflict("that time overlaps another booked appointment", existing_id=clash[0].id)
                if (start, end) != (a.start_utc, a.end_utc):
                    a.start_utc, a.end_utc = start, end
                    changed += ["start", "end"]
            for key in ("title", "notes", "location"):
                if key in data:
                    value = _clean_str(data[key])
                    if key == "title" and not value:
                        raise Invalid("appointment title is required")
                    if getattr(a, key) != value:
                        setattr(a, key, value)
                        changed.append(key)
            if "contact_id" in data:
                cid = _int_or_none(data["contact_id"], "contact_id")
                if cid is not None and s.get(Contact, cid) is None:
                    raise Invalid(f"contact {cid} not found")
                if a.contact_id != cid:
                    a.contact_id = cid
                    changed.append("contact_id")
            if changed:
                audit.record(s, actor, "update", "appointment", a.id, changed)
        return a

    def cancel_appointment(self, appt_id: int, actor: str) -> Appointment:
        with self.session() as s, audit.write_txn(s):
            a = s.get(Appointment, appt_id)
            if a is None:
                raise NotFound(f"appointment {appt_id} not found")
            a.status = "cancelled"
            audit.record(s, actor, "cancel", "appointment", a.id, ["status"])
        return a

    def availability(self, day: date, now_utc: datetime | None = None) -> list[datetime]:
        lo = scheduling.to_utc_naive(datetime.combine(day, datetime.min.time()), self.zone)
        with self.session() as s:
            busy = [scheduling.Busy(a.start_utc, a.end_utc) for a in self._busy(s, lo, lo + timedelta(days=1))]
        return scheduling.open_slots(day, busy, self.zone, self.hours, self.settings.slot_minutes,
                                     now_utc=now_utc if now_utc is not None else utcnow())

    def public_book(self, data: dict, now_utc: datetime | None = None) -> Appointment:
        """Self-service booking. Only an open slot from ``availability`` is accepted.
        Matches an existing contact by email (blind index) or creates one."""
        email = (_clean_str(data.get("email")) or "").lower()
        if not EMAIL_RE.match(email):
            raise Invalid("a valid email is required")
        name = _clean_str(data.get("name"))
        if not name:
            raise Invalid("name is required")
        try:
            start = scheduling.parse_datetime(str(data.get("start") or ""), self.zone)
        except scheduling.ScheduleError as exc:
            raise Invalid(str(exc)) from exc
        day = scheduling.to_local(start, self.zone).date()
        open_starts = {scheduling.to_utc_naive(x, self.zone) for x in self.availability(day, now_utc=now_utc)}
        if start not in open_starts:
            raise Conflict("that slot is not available")
        contact = self.find_contact_by_email(email)
        if contact is None:
            first, _, last = name.partition(" ")
            contact = self.create_contact({"first_name": first, "last_name": last or None, "email": email,
                                           "phone": data.get("phone"), "tags": ["booked-online"]},
                                          actor="public-booking")
        end = start + timedelta(minutes=self.settings.slot_minutes)
        return self.create_appointment(
            {"title": f"Consultation: {name}", "notes": _clean_str(data.get("message")),
             "contact_id": contact.id, "start": start.isoformat() + "+00:00", "end": end.isoformat() + "+00:00"},
            actor="public-booking", source="public")

    def calendar_feed(self, user: User) -> str:
        """iCalendar feed of booked and cancelled appointments from 30 days back.
        Titles are hidden ("Busy") unless AMAGI_CRM_FEED_DETAILS=1, because the feed
        is fetched by a third-party calendar service."""
        since = utcnow() - timedelta(days=30)
        events = []
        for a in self.list_appointments(start=since, include_cancelled=True):
            detail = self.settings.feed_details
            events.append(scheduling.FeedEvent(
                uid=f"appt-{a.id}@amagi-crm", start_utc=a.start_utc, end_utc=a.end_utc,
                summary=a.title if detail else "Busy",
                description=a.notes if detail else None,
                location=a.location if detail else None,
                cancelled=a.status == "cancelled"))
        return scheduling.build_ics(events, calendar_name=f"amagi-crm ({user.name})")

    # --- audit & keys --------------------------------------------------------

    def audit_tail(self, limit: int = 20) -> list[dict]:
        from .models import AuditEntry
        with self.session() as s:
            rows = list(s.scalars(select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit)))
        return [{"id": e.id, "ts": self._iso(e.ts), "actor": e.actor, "action": e.action,
                 "entity": e.entity, "entity_id": e.entity_id, "fields": e.fields,
                 "hash": e.hash} for e in reversed(rows)]

    def audit_verify(self) -> audit.VerifyResult:
        with self.session() as s:
            return audit.verify(s)

    def rotate_keys(self, actor: str) -> dict:
        """Re-encrypt every value not written with the first key in AMAGI_CRM_ENCRYPTION_KEYS."""
        counts: dict[str, int] = {}
        cipher = self.db.cipher
        with self.session() as s, audit.write_txn(s):
            for model in (Contact, Deal, Task, Appointment):
                table = model.__table__
                enc_cols = [c.name for c in table.columns if isinstance(c.type, Encrypted)]
                stale_ids = set()
                raw = s.execute(text(f"SELECT id, {', '.join(enc_cols)} FROM {table.name}"))
                for row in raw:
                    if any(v is not None and not cipher.is_primary(v) for v in row[1:]):
                        stale_ids.add(row[0])
                for obj_id in stale_ids:
                    obj = s.get(model, obj_id)
                    for col in enc_cols:
                        flag_modified(obj, col)
                counts[table.name] = len(stale_ids)
            s.flush()
            audit.record(s, actor, "rotate-keys", "system", None, sorted(counts))
        return counts
