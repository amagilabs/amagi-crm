from datetime import date, datetime, time, timedelta

import pytest

from amagi_crm.scheduling import (Busy, FeedEvent, ScheduleError, build_ics, get_zone, open_slots,
                                  overlaps, parse_datetime, parse_working_hours, within_working_hours)

NY = get_zone("America/New_York")
HOURS = parse_working_hours("mon-fri 09:00-17:00; sat 10:00-13:00")


def test_working_hours_parse():
    assert HOURS[0] == [(time(9), time(17))]
    assert HOURS[4] == [(time(9), time(17))]
    assert HOURS[5] == [(time(10), time(13))]
    assert HOURS[6] == []


@pytest.mark.parametrize("spec", ["fri-mon 09:00-17:00", "mon 17:00-09:00", "weekdays 9 to 5",
                                  "xyz 09:00-10:00", "mon 25:00-26:00"])
def test_bad_working_hours_rejected(spec):
    with pytest.raises(ScheduleError):
        parse_working_hours(spec)


def test_unknown_timezone():
    with pytest.raises(ScheduleError):
        get_zone("Mars/Olympus_Mons")


def test_slots_follow_dst():
    # US daylight saving ends on 2026-11-01: 09:00 New York is 13:00 UTC before, 14:00 UTC after.
    before, after = date(2026, 10, 30), date(2026, 11, 2)
    assert (before.weekday(), after.weekday()) == (4, 0)
    s1 = open_slots(before, [], NY, HOURS, 30)
    s2 = open_slots(after, [], NY, HOURS, 30)
    assert len(s1) == len(s2) == 16
    assert s1[0].hour == s2[0].hour == 9
    assert parse_datetime(s1[0].isoformat(), NY) == datetime(2026, 10, 30, 13, 0)
    assert parse_datetime(s2[0].isoformat(), NY) == datetime(2026, 11, 2, 14, 0)


def test_busy_and_past_slots_are_removed():
    day = date(2026, 11, 2)
    busy = [Busy(datetime(2026, 11, 2, 15, 0), datetime(2026, 11, 2, 16, 0))]  # 10:00-11:00 local
    slots = open_slots(day, busy, NY, HOURS, 30)
    times = [s.strftime("%H:%M") for s in slots]
    assert "10:00" not in times and "10:30" not in times
    assert "09:30" in times and "11:00" in times
    assert len(slots) == 14
    later = open_slots(day, [], NY, HOURS, 30, now_utc=datetime(2026, 11, 2, 20, 0))  # 15:00 local
    assert [s.strftime("%H:%M") for s in later] == ["15:00", "15:30", "16:00", "16:30"]


def test_no_slots_on_a_closed_day_and_slot_length_must_be_positive():
    assert open_slots(date(2026, 11, 1), [], NY, HOURS, 30) == []  # Sunday
    with pytest.raises(ScheduleError):
        open_slots(date(2026, 11, 2), [], NY, HOURS, 0)


def test_back_to_back_is_not_an_overlap():
    t = datetime(2026, 11, 2, 15)
    assert not overlaps(t, t + timedelta(minutes=30), t + timedelta(minutes=30), t + timedelta(minutes=60))
    assert overlaps(t, t + timedelta(minutes=30), t + timedelta(minutes=29), t + timedelta(minutes=60))


def test_within_working_hours():
    assert within_working_hours(datetime(2026, 11, 2, 14), datetime(2026, 11, 2, 14, 30), NY, HOURS)
    assert not within_working_hours(datetime(2026, 11, 2, 13), datetime(2026, 11, 2, 13, 30), NY, HOURS)
    assert not within_working_hours(datetime(2026, 11, 1, 15), datetime(2026, 11, 1, 15, 30), NY, HOURS)


def test_parse_datetime_forms():
    assert parse_datetime("2026-11-02T09:00", NY) == datetime(2026, 11, 2, 14)  # naive = firm-local
    assert parse_datetime("2026-11-02T14:00Z", NY) == datetime(2026, 11, 2, 14)
    assert parse_datetime("2026-11-02T15:00+01:00", NY) == datetime(2026, 11, 2, 14)
    with pytest.raises(ScheduleError):
        parse_datetime("next tuesday", NY)


def test_ics_escapes_folds_and_uses_crlf():
    long_title = "Consultation, intake; " + "é" * 60
    ev = FeedEvent(uid="appt-1@amagi-crm", start_utc=datetime(2026, 11, 2, 14), end_utc=datetime(2026, 11, 2, 14, 30),
                   summary=long_title, description="line one\nline two", location="Room 2", cancelled=True)
    ics = build_ics([ev], stamp_utc=datetime(2026, 10, 1))
    assert ics.endswith("\r\n")
    assert "\n" not in ics.replace("\r\n", "")
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    unfolded = ics.replace("\r\n ", "")
    assert "SUMMARY:Consultation\\, intake\\; " + "é" * 60 in unfolded
    assert "DESCRIPTION:line one\\nline two" in unfolded
    assert "DTSTART:20261102T140000Z" in unfolded
    assert "STATUS:CANCELLED" in unfolded
    assert unfolded.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0")
