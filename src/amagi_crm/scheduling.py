"""Working hours, open-slot calculation, overlap checks, and iCalendar output.

The firm has one timezone (``AMAGI_CRM_TIMEZONE``). The database stores naive
UTC; the API takes and returns ISO 8601 with an offset. A time sent without an
offset is read as firm-local time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_RULE = re.compile(r"^\s*([a-z]{3})(?:-([a-z]{3}))?\s+(\d{1,2}:\d{2})-(\d{1,2}:\d{2})\s*$")


class ScheduleError(ValueError):
    pass


def get_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"unknown timezone: {name!r}") from exc


def _hm(value: str) -> time:
    h, m = value.split(":")
    try:
        return time(int(h), int(m))
    except ValueError as exc:
        raise ScheduleError(f"bad time {value!r}") from exc


def parse_working_hours(spec: str) -> dict[int, list[tuple[time, time]]]:
    """Parse ``"mon-fri 09:00-17:00; sat 10:00-13:00"`` into ``{weekday: [(start, end)]}``."""
    hours: dict[int, list[tuple[time, time]]] = {i: [] for i in range(7)}
    for part in filter(None, (p.strip() for p in spec.lower().split(";"))):
        m = _RULE.match(part)
        if not m:
            raise ScheduleError(f"can't read working-hours rule {part!r}; expected e.g. 'mon-fri 09:00-17:00'")
        first, last, start, end = m.groups()
        if first not in DAYS or (last and last not in DAYS):
            raise ScheduleError(f"unknown day in {part!r}")
        a, b = DAYS.index(first), DAYS.index(last or first)
        if b < a:
            raise ScheduleError(f"day range runs backwards in {part!r}")
        t0, t1 = _hm(start), _hm(end)
        if t1 <= t0:
            raise ScheduleError(f"end time must be after start time in {part!r}")
        for day in range(a, b + 1):
            hours[day].append((t0, t1))
    return hours


def to_utc_naive(value: datetime, zone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def parse_datetime(value: str, zone: ZoneInfo) -> datetime:
    """ISO 8601 string -> naive UTC. ``Z`` suffix accepted."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScheduleError(f"not an ISO 8601 date-time: {value!r}") from exc
    return to_utc_naive(parsed, zone)


def to_local(value_utc: datetime, zone: ZoneInfo) -> datetime:
    return value_utc.replace(tzinfo=timezone.utc).astimezone(zone)


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Half-open intervals: a 10:00-10:30 slot does not clash with 10:30-11:00."""
    return a_start < b_end and b_start < a_end


@dataclass
class Busy:
    start_utc: datetime
    end_utc: datetime


def open_slots(day: date, busy: list[Busy], zone: ZoneInfo, hours: dict[int, list[tuple[time, time]]],
               slot_minutes: int, now_utc: datetime | None = None) -> list[datetime]:
    """Slot start times (firm-local, tz-aware) on ``day`` that are inside working hours,
    don't overlap a booked appointment, and aren't in the past."""
    if slot_minutes <= 0:
        raise ScheduleError("slot length must be positive")
    step = timedelta(minutes=slot_minutes)
    slots = []
    for start_t, end_t in hours.get(day.weekday(), []):
        cursor = datetime.combine(day, start_t, tzinfo=zone)
        window_end = datetime.combine(day, end_t, tzinfo=zone)
        while cursor + step <= window_end:
            s_utc = to_utc_naive(cursor, zone)
            e_utc = to_utc_naive(cursor + step, zone)
            free = not any(overlaps(s_utc, e_utc, b.start_utc, b.end_utc) for b in busy)
            future = now_utc is None or s_utc >= now_utc
            if free and future:
                slots.append(cursor)
            cursor += step
    return slots


def within_working_hours(start_utc: datetime, end_utc: datetime, zone: ZoneInfo,
                         hours: dict[int, list[tuple[time, time]]]) -> bool:
    start_l, end_l = to_local(start_utc, zone), to_local(end_utc, zone)
    if start_l.date() != end_l.date():
        return False
    for t0, t1 in hours.get(start_l.weekday(), []):
        if t0 <= start_l.time() and end_l.time() <= t1:
            return True
    return False


# --- iCalendar ---------------------------------------------------------------

def _ics_escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets."""
    out, current = [], ""
    for ch in line:
        if len((current + ch).encode("utf-8")) > 75:
            out.append(current)
            current = " " + ch
        else:
            current += ch
    out.append(current)
    return "\r\n".join(out)


def _ics_time(value_utc: datetime) -> str:
    return value_utc.strftime("%Y%m%dT%H%M%SZ")


@dataclass
class FeedEvent:
    uid: str
    start_utc: datetime
    end_utc: datetime
    summary: str
    description: str | None = None
    location: str | None = None
    cancelled: bool = False


def build_ics(events: list[FeedEvent], calendar_name: str = "amagi-crm", stamp_utc: datetime | None = None) -> str:
    stamp = _ics_time(stamp_utc or datetime.now(timezone.utc).replace(tzinfo=None))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//AmagiLabs//amagi-crm//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_ics_escape(calendar_name)}",
    ]
    for ev in events:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{ev.uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_ics_time(ev.start_utc)}",
            f"DTEND:{_ics_time(ev.end_utc)}",
            f"SUMMARY:{_ics_escape(ev.summary)}",
        ]
        if ev.description:
            lines.append(f"DESCRIPTION:{_ics_escape(ev.description)}")
        if ev.location:
            lines.append(f"LOCATION:{_ics_escape(ev.location)}")
        lines.append("STATUS:CANCELLED" if ev.cancelled else "STATUS:CONFIRMED")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
