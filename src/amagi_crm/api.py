"""REST API and the bundled web UI.

Auth is a bearer token per user (``amagi-crm user add``). Roles:
``readonly`` can read, ``member`` can read and write CRM records, ``admin``
can also manage users and read the audit log.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import date, timedelta
from importlib import resources
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, scheduling
from .models import User
from .services import CRM, Conflict, CRMError, Invalid


class ContactIn(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None


class DealIn(BaseModel):
    title: Optional[str] = None
    contact_id: Optional[int] = None
    stage: Optional[str] = None
    stage_id: Optional[int] = None
    amount_cents: Optional[int] = None
    expected_close: Optional[str] = None
    notes: Optional[str] = None


class MoveIn(BaseModel):
    stage: str
    position: Optional[int] = None


class StageIn(BaseModel):
    name: str
    kind: str = "open"


class TaskIn(BaseModel):
    title: Optional[str] = None
    notes: Optional[str] = None
    due_date: Optional[str] = None
    done: Optional[bool] = None
    contact_id: Optional[int] = None
    deal_id: Optional[int] = None
    assignee_id: Optional[int] = None


class AppointmentIn(BaseModel):
    title: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    duration_minutes: Optional[int] = None
    contact_id: Optional[int] = None
    notes: Optional[str] = None
    location: Optional[str] = None


class UserIn(BaseModel):
    name: str
    role: str = "member"


class BookingIn(BaseModel):
    name: str
    email: str
    start: str
    phone: Optional[str] = None
    message: Optional[str] = None


def _week_bounds(today: date) -> tuple[date, date]:
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


class _RateLimiter:
    def __init__(self, limit: int, window_s: int):
        self.limit, self.window = limit, window_s
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


def create_app(crm: CRM) -> FastAPI:
    app = FastAPI(title="amagi-crm", version=__version__,
                  description="Self-hosted small-firm CRM: contacts, pipeline, tasks, booking.")
    static_dir = resources.files("amagi_crm") / "static"
    booking_limiter = _RateLimiter(limit=5, window_s=3600)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        if request.url.path.startswith(("/api/", "/feeds/", "/public/")):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(CRMError)
    async def crm_error(_request: Request, exc: CRMError):
        body = {"detail": str(exc)}
        if isinstance(exc, Conflict) and exc.existing_id is not None:
            body["existing_id"] = exc.existing_id
        return JSONResponse(body, status_code=exc.status)

    def current_user(authorization: Optional[str] = Header(default=None)) -> User:
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        user = crm.authenticate(token)
        if user is None:
            raise HTTPException(401, "missing or invalid bearer token",
                                headers={"WWW-Authenticate": "Bearer"})
        return user

    def writer(user: User = Depends(current_user)) -> User:
        if user.role not in ("admin", "member"):
            raise HTTPException(403, "your role is read-only")
        return user

    def admin(user: User = Depends(current_user)) -> User:
        if user.role != "admin":
            raise HTTPException(403, "admin role required")
        return user

    def parse_day(value: Optional[str]) -> date:
        if not value:
            return crm.today()
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise Invalid(f"not an ISO date (YYYY-MM-DD): {value!r}") from exc

    # --- meta ---------------------------------------------------------------

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        return {**crm.user_dict(user), "timezone": crm.settings.timezone,
                "public_booking": crm.settings.public_booking}

    # --- contacts -----------------------------------------------------------

    @app.get("/api/contacts")
    def list_contacts(search: Optional[str] = None, tag: Optional[str] = None,
                      limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                      user: User = Depends(current_user)):
        rows, total = crm.list_contacts(user.name, search=search, tag=tag, limit=limit, offset=offset)
        return {"total": total, "items": [crm.contact_dict(c) for c in rows]}

    @app.get("/api/contacts/export.csv")
    def export_contacts(user: User = Depends(admin)):
        return PlainTextResponse(crm.export_contacts_csv(user.name), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=contacts.csv"})

    @app.post("/api/contacts", status_code=201)
    def create_contact(body: ContactIn, user: User = Depends(writer)):
        return crm.contact_dict(crm.create_contact(body.model_dump(exclude_unset=True), user.name))

    @app.get("/api/contacts/{contact_id}")
    def get_contact(contact_id: int, user: User = Depends(current_user)):
        return crm.contact_dict(crm.get_contact(contact_id, user.name))

    @app.patch("/api/contacts/{contact_id}")
    def update_contact(contact_id: int, body: ContactIn, user: User = Depends(writer)):
        return crm.contact_dict(crm.update_contact(contact_id, body.model_dump(exclude_unset=True), user.name))

    @app.delete("/api/contacts/{contact_id}", status_code=204)
    def delete_contact(contact_id: int, user: User = Depends(writer)):
        crm.delete_contact(contact_id, user.name)
        return Response(status_code=204)

    # --- pipeline -----------------------------------------------------------

    @app.get("/api/stages")
    def list_stages(user: User = Depends(current_user)):
        return [crm.stage_dict(s) for s in crm.list_stages()]

    @app.post("/api/stages", status_code=201)
    def create_stage(body: StageIn, user: User = Depends(admin)):
        return crm.stage_dict(crm.create_stage(body.name, body.kind, user.name))

    @app.get("/api/pipeline")
    def pipeline(user: User = Depends(current_user)):
        return crm.board()

    @app.get("/api/deals")
    def list_deals(stage_id: Optional[int] = None, user: User = Depends(current_user)):
        return [crm.deal_dict(d) for d in crm.list_deals(stage_id)]

    @app.post("/api/deals", status_code=201)
    def create_deal(body: DealIn, user: User = Depends(writer)):
        return crm.deal_dict(crm.create_deal(body.model_dump(exclude_unset=True), user.name))

    @app.get("/api/deals/{deal_id}")
    def get_deal(deal_id: int, user: User = Depends(current_user)):
        return crm.deal_dict(crm.get_deal(deal_id, user.name))

    @app.patch("/api/deals/{deal_id}")
    def update_deal(deal_id: int, body: DealIn, user: User = Depends(writer)):
        return crm.deal_dict(crm.update_deal(deal_id, body.model_dump(exclude_unset=True), user.name))

    @app.post("/api/deals/{deal_id}/move")
    def move_deal(deal_id: int, body: MoveIn, user: User = Depends(writer)):
        return crm.deal_dict(crm.move_deal(deal_id, body.stage, body.position, user.name))

    @app.delete("/api/deals/{deal_id}", status_code=204)
    def delete_deal(deal_id: int, user: User = Depends(writer)):
        crm.delete_deal(deal_id, user.name)
        return Response(status_code=204)

    @app.get("/api/reports/deals")
    def deal_report(start: Optional[str] = None, end: Optional[str] = None,
                    user: User = Depends(current_user)):
        if start or end:
            lo, hi = parse_day(start), parse_day(end)
        else:
            lo, hi = _week_bounds(parse_day(None))
        if hi < lo:
            raise Invalid("end is before start")
        return crm.deal_summary(lo, hi)

    # --- tasks --------------------------------------------------------------

    @app.get("/api/tasks")
    def list_tasks(done: Optional[bool] = None, due_before: Optional[str] = None,
                   contact_id: Optional[int] = None, user: User = Depends(current_user)):
        due = parse_day(due_before) if due_before else None
        return [crm.task_dict(t) for t in crm.list_tasks(done, due, contact_id)]

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskIn, user: User = Depends(writer)):
        return crm.task_dict(crm.create_task(body.model_dump(exclude_unset=True), user.name))

    @app.patch("/api/tasks/{task_id}")
    def update_task(task_id: int, body: TaskIn, user: User = Depends(writer)):
        return crm.task_dict(crm.update_task(task_id, body.model_dump(exclude_unset=True), user.name))

    @app.delete("/api/tasks/{task_id}", status_code=204)
    def delete_task(task_id: int, user: User = Depends(writer)):
        crm.delete_task(task_id, user.name)
        return Response(status_code=204)

    # --- calendar -----------------------------------------------------------

    @app.get("/api/appointments")
    def list_appointments(start: Optional[str] = None, end: Optional[str] = None,
                          include_cancelled: bool = False, user: User = Depends(current_user)):
        try:
            lo = scheduling.parse_datetime(start, crm.zone) if start else None
            hi = scheduling.parse_datetime(end, crm.zone) if end else None
        except scheduling.ScheduleError as exc:
            raise Invalid(str(exc)) from exc
        return [crm.appt_dict(a) for a in crm.list_appointments(lo, hi, include_cancelled)]

    @app.get("/api/agenda")
    def agenda(day: Optional[str] = Query(None, alias="date"), user: User = Depends(current_user)):
        d = parse_day(day)
        return {
            "date": d.isoformat(),
            "appointments": [crm.appt_dict(a) for a in crm.appointments_on(d)],
            "tasks_due": [crm.task_dict(t) for t in crm.list_tasks(done=False, due_on_or_before=d)],
        }

    @app.post("/api/appointments", status_code=201)
    def create_appointment(body: AppointmentIn, user: User = Depends(writer)):
        return crm.appt_dict(crm.create_appointment(body.model_dump(exclude_unset=True), user.name))

    @app.patch("/api/appointments/{appt_id}")
    def update_appointment(appt_id: int, body: AppointmentIn, user: User = Depends(writer)):
        return crm.appt_dict(crm.update_appointment(appt_id, body.model_dump(exclude_unset=True), user.name))

    @app.post("/api/appointments/{appt_id}/cancel")
    def cancel_appointment(appt_id: int, user: User = Depends(writer)):
        return crm.appt_dict(crm.cancel_appointment(appt_id, user.name))

    @app.get("/api/availability")
    def availability(day: Optional[str] = Query(None, alias="date"), user: User = Depends(current_user)):
        d = parse_day(day)
        return {"date": d.isoformat(), "slot_minutes": crm.settings.slot_minutes,
                "slots": [x.isoformat() for x in crm.availability(d)]}

    @app.get("/feeds/{token}.ics", include_in_schema=False)
    def feed(token: str):
        user = crm.user_for_feed(token)
        if user is None:
            raise HTTPException(404, "not found")
        return Response(crm.calendar_feed(user), media_type="text/calendar; charset=utf-8")

    # --- admin --------------------------------------------------------------

    @app.get("/api/users")
    def list_users(user: User = Depends(admin)):
        return [crm.user_dict(u) for u in crm.list_users()]

    @app.post("/api/users", status_code=201)
    def create_user(body: UserIn, user: User = Depends(admin)):
        new, token = crm.create_user(body.name, body.role, user.name)
        return {**crm.user_dict(new), "token": token}

    @app.post("/api/users/{name}/revoke")
    def revoke_user(name: str, user: User = Depends(admin)):
        return crm.user_dict(crm.revoke_user(name, user.name))

    @app.get("/api/audit")
    def audit_log(limit: int = Query(50, ge=1, le=1000), user: User = Depends(admin)):
        return crm.audit_tail(limit)

    @app.get("/api/audit/verify")
    def audit_verify(user: User = Depends(admin)):
        r = crm.audit_verify()
        return {"ok": r.ok, "entries": r.count, "head_hash": r.head_hash, "bad_id": r.bad_id, "reason": r.reason}

    # --- public booking (off unless AMAGI_CRM_PUBLIC_BOOKING=1) ----------------

    def booking_enabled():
        if not crm.settings.public_booking:
            raise HTTPException(404, "not found")

    @app.get("/public/slots", dependencies=[Depends(booking_enabled)])
    def public_slots(day: Optional[str] = Query(None, alias="date")):
        d = parse_day(day)
        return {"date": d.isoformat(), "slot_minutes": crm.settings.slot_minutes,
                "slots": [x.isoformat() for x in crm.availability(d)]}

    @app.post("/public/book", status_code=201, dependencies=[Depends(booking_enabled)])
    def public_book(body: BookingIn, request: Request):
        ip = request.client.host if request.client else "unknown"
        if not booking_limiter.allow(ip):
            raise HTTPException(429, "too many booking attempts; try again later")
        a = crm.public_book(body.model_dump())
        return {"id": a.id, "start": crm.appt_dict(a)["start"], "end": crm.appt_dict(a)["end"]}

    @app.get("/book", include_in_schema=False, dependencies=[Depends(booking_enabled)])
    def booking_page():
        return FileResponse(str(static_dir / "book.html"))

    # --- web UI -------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(str(static_dir / "index.html"))

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    return app


def app_from_env():
    """Uvicorn factory: ``uvicorn amagi_crm.api:app_from_env --factory``."""
    from .config import Settings
    from .db import Database

    db = Database(Settings.load())
    db.create_all()
    return create_app(CRM(db))
