from datetime import datetime, timedelta

import pytest

from amagi_crm.importer import read_contacts_csv
from amagi_crm.scheduling import to_utc_naive
from amagi_crm.services import Conflict, Invalid, NotFound

from conftest import LONG_AGO, make_crm, next_monday


# --- contacts ----------------------------------------------------------------

def test_email_dedupe_is_case_insensitive_and_reports_the_existing_id(crm):
    first = crm.create_contact({"first_name": "Lee", "email": "lee@example.com"}, "t")
    with pytest.raises(Conflict) as exc:
        crm.create_contact({"first_name": "Lee", "email": "LEE@Example.com"}, "t")
    assert exc.value.existing_id == first.id
    assert crm.create_contact({"first_name": "Lee", "email": "lee@example.org"}, "t").id != first.id


def test_changing_email_to_one_already_taken_is_refused(crm):
    crm.create_contact({"email": "a@example.com"}, "t")
    b = crm.create_contact({"email": "b@example.com"}, "t")
    with pytest.raises(Conflict):
        crm.update_contact(b.id, {"email": "A@example.com"}, "t")
    assert crm.update_contact(b.id, {"email": "b2@example.com"}, "t").email == "b2@example.com"


def test_contact_validation(crm):
    with pytest.raises(Invalid):
        crm.create_contact({"email": "not-an-email"}, "t")
    with pytest.raises(Invalid):
        crm.create_contact({"phone": "555-0100"}, "t")  # no name, email or company
    c = crm.create_contact({"company": "Harbor Dental", "tags": "b, a, a"}, "t")
    assert c.tags == ["a", "b"]


def test_search_and_tag_filter(crm):
    crm.create_contact({"first_name": "Dana", "last_name": "Whitfield", "company": "Harbor Dental",
                        "tags": ["vip"]}, "t")
    crm.create_contact({"first_name": "Lee", "last_name": "Park", "company": "Park Roofing"}, "t")
    crm.create_contact({"first_name": "Amara", "last_name": "Osei", "email": "amara@example.net"}, "t")
    rows, total = crm.list_contacts("t")
    assert total == 3 and [c.last_name for c in rows] == ["Osei", "Park", "Whitfield"]
    rows, total = crm.list_contacts("t", search="ROOF")
    assert total == 1 and rows[0].first_name == "Lee"
    rows, _ = crm.list_contacts("t", search="example.net")
    assert rows[0].first_name == "Amara"
    rows, total = crm.list_contacts("t", tag="vip")
    assert total == 1 and rows[0].first_name == "Dana"
    rows, total = crm.list_contacts("t", limit=1, offset=1)
    assert total == 3 and len(rows) == 1 and rows[0].last_name == "Park"


def test_missing_records_raise_not_found(crm):
    with pytest.raises(NotFound):
        crm.get_contact(99, "t")
    with pytest.raises(NotFound):
        crm.move_deal(99, "Won", None, "t")
    with pytest.raises(NotFound):
        crm.cancel_appointment(99, "t")


def test_deleting_a_contact_keeps_its_deal(crm):
    c = crm.create_contact({"first_name": "Lee"}, "t")
    d = crm.create_deal({"title": "Roof app", "contact_id": c.id}, "t")
    crm.delete_contact(c.id, "t")
    assert crm.get_deal(d.id, "t").contact_id is None


def test_fixture_import(crm, fixture_csv):
    rows, ignored = read_contacts_csv(fixture_csv.read_text(encoding="utf-8-sig"))
    assert set(ignored) == {"Source", "Date Added"}
    result = crm.import_contacts(rows, "t")
    assert (result["created"], result["duplicates"], result["invalid"]) == (8, 1, 1)
    assert result["errors"][0].startswith("row 11")
    _, total = crm.list_contacts("t")
    assert total == 8
    again = crm.import_contacts(rows, "t")
    assert again["created"] == 1  # only the row without an email can't be deduplicated
    assert again["duplicates"] == 8


def test_export_round_trips_decrypted_values(crm):
    crm.create_contact({"first_name": "Dana", "email": "dana@example.com", "tags": ["vip", "client"]}, "t")
    out = crm.export_contacts_csv("t").splitlines()
    assert out[0] == "id,first_name,last_name,email,phone,company,tags,notes"
    assert out[1].startswith("1,Dana,,dana@example.com,,,")
    assert '"client,vip"' in out[1]


# --- pipeline ----------------------------------------------------------------

def test_deal_stages_and_closed_at(crm):
    d = crm.create_deal({"title": "Retainer", "amount_cents": 250000}, "t")
    stages = {s.name: s for s in crm.list_stages()}
    assert d.stage_id == stages["Lead"].id and d.closed_at is None
    won = crm.move_deal(d.id, "won", None, "t")
    assert won.stage_id == stages["Won"].id and won.closed_at is not None
    reopened = crm.update_deal(d.id, {"stage": "Qualified"}, "t")
    assert reopened.closed_at is None
    with pytest.raises(Invalid):
        crm.move_deal(d.id, "Maybe", None, "t")
    with pytest.raises(Invalid):
        crm.create_deal({"title": "Negative", "amount_cents": -5}, "t")
    with pytest.raises(Invalid):
        crm.create_deal({"title": "Orphan", "contact_id": 42}, "t")


def test_move_renumbers_the_column(crm):
    a, b, c = (crm.create_deal({"title": t}, "t") for t in ("A", "B", "C"))
    crm.move_deal(c.id, "Lead", 0, "t")
    lead = crm.list_stages()[0].id
    assert [d.title for d in crm.list_deals(stage_id=lead)] == ["C", "A", "B"]
    crm.move_deal(c.id, "Qualified", None, "t")
    crm.move_deal(a.id, "Qualified", 5, "t")  # past the end lands at the bottom
    qualified = crm.list_stages()[1].id
    assert [d.title for d in crm.list_deals(stage_id=qualified)] == ["C", "A"]


def test_board_and_summary(crm):
    crm.create_deal({"title": "A", "amount_cents": 100000}, "t")
    b = crm.create_deal({"title": "B", "amount_cents": 50000, "stage": "Proposal"}, "t")
    c = crm.create_deal({"title": "C", "amount_cents": 70000}, "t")
    crm.move_deal(b.id, "Won", None, "t")
    crm.move_deal(c.id, "Lost", None, "t")
    board = {col["name"]: col for col in crm.board()}
    assert list(board) == ["Lead", "Qualified", "Proposal", "Negotiation", "Won", "Lost"]
    assert board["Lead"]["count"] == 1 and board["Lead"]["total_cents"] == 100000
    assert board["Won"]["deals"][0]["title"] == "B"
    today = crm.today()
    s = crm.deal_summary(today - timedelta(days=today.weekday()), today + timedelta(days=6))
    assert s["new"] == {"count": 3, "total_cents": 220000}
    assert s["won"]["count"] == 1 and s["won"]["total_cents"] == 50000
    assert s["lost"] == {"count": 1, "total_cents": 70000}
    assert s["open_pipeline"]["total_cents"] == 100000
    assert [x["stage"] for x in s["open_pipeline"]["by_stage"]] == ["Lead", "Qualified", "Proposal", "Negotiation"]
    old = crm.deal_summary(today - timedelta(days=60), today - timedelta(days=30))
    assert old["new"]["count"] == 0 and old["won"]["count"] == 0


# --- tasks -------------------------------------------------------------------

def test_tasks(crm):
    today = crm.today()
    t1 = crm.create_task({"title": "Call back", "due_date": today.isoformat()}, "t")
    crm.create_task({"title": "Later", "due_date": (today + timedelta(days=9)).isoformat()}, "t")
    crm.create_task({"title": "Someday"}, "t")
    assert [t.title for t in crm.list_tasks(due_on_or_before=today)] == ["Call back"]
    done = crm.update_task(t1.id, {"done": True}, "t")
    assert done.completed_at is not None
    assert [t.title for t in crm.list_tasks(done=False)] == ["Later", "Someday"]
    assert crm.update_task(t1.id, {"done": False}, "t").completed_at is None
    with pytest.raises(Invalid):
        crm.create_task({"title": "x", "contact_id": 7}, "t")
    with pytest.raises(Invalid):
        crm.create_task({"title": "x", "due_date": "soon"}, "t")


# --- appointments & booking --------------------------------------------------

def _at(day, hh_mm):
    return f"{day.isoformat()}T{hh_mm}"


def test_appointments_refuse_overlaps_but_allow_back_to_back(crm):
    day = next_monday(crm)
    a = crm.create_appointment({"title": "Kickoff", "start": _at(day, "10:00")}, "t")
    assert a.end_utc - a.start_utc == timedelta(minutes=30)
    with pytest.raises(Conflict) as exc:
        crm.create_appointment({"title": "Clash", "start": _at(day, "10:15"), "duration_minutes": 30}, "t")
    assert exc.value.existing_id == a.id
    crm.create_appointment({"title": "Next", "start": _at(day, "10:30")}, "t")
    crm.cancel_appointment(a.id, "t")
    crm.create_appointment({"title": "Rebooked", "start": _at(day, "10:00")}, "t")
    assert [x.title for x in crm.appointments_on(day)] == ["Rebooked", "Next"]


def test_appointment_validation_and_moves(crm):
    day = next_monday(crm)
    with pytest.raises(Invalid):
        crm.create_appointment({"title": "Backwards", "start": _at(day, "10:00"), "end": _at(day, "09:00")}, "t")
    with pytest.raises(Invalid):
        crm.create_appointment({"title": "Vague", "start": "next tuesday"}, "t")
    with pytest.raises(Invalid):
        crm.create_appointment({"start": _at(day, "10:00")}, "t")
    a = crm.create_appointment({"title": "A", "start": _at(day, "09:00")}, "t")
    b = crm.create_appointment({"title": "B", "start": _at(day, "11:00"), "duration_minutes": 60}, "t")
    with pytest.raises(Conflict):
        crm.update_appointment(a.id, {"start": _at(day, "11:30")}, "t")
    moved = crm.update_appointment(b.id, {"start": _at(day, "13:00")}, "t")
    assert moved.end_utc - moved.start_utc == timedelta(minutes=60)  # keeps its length


def test_availability_excludes_booked_time(crm):
    day = next_monday(crm)
    assert len(crm.availability(day, now_utc=LONG_AGO)) == 16
    crm.create_appointment({"title": "Long", "start": _at(day, "13:00"), "duration_minutes": 90}, "t")
    slots = [s.strftime("%H:%M") for s in crm.availability(day, now_utc=LONG_AGO)]
    assert len(slots) == 13 and "13:00" not in slots and "14:00" not in slots and "14:30" in slots
    assert crm.availability(day + timedelta(days=6), now_utc=LONG_AGO) == []  # Sunday


def test_public_booking_only_takes_open_slots(crm):
    day = next_monday(crm)
    a = crm.public_book({"name": "Nadia Fontaine", "email": "nadia@example.com", "phone": "555-0177",
                         "start": _at(day, "09:00"), "message": "Question about a lease"}, now_utc=LONG_AGO)
    assert a.source == "public" and a.notes == "Question about a lease"
    contact = crm.find_contact_by_email("nadia@example.com")
    assert contact.tags == ["booked-online"] and contact.last_name == "Fontaine" and a.contact_id == contact.id
    for bad_start in (_at(day, "09:00"),   # just taken
                      _at(day, "08:00"),   # before opening
                      _at(day, "09:10"),   # not on the slot grid
                      _at(day - timedelta(days=1), "10:00")):  # Sunday
        with pytest.raises(Conflict):
            crm.public_book({"name": "X", "email": "x@example.com", "start": bad_start}, now_utc=LONG_AGO)
    later = to_utc_naive(datetime.combine(day, datetime.min.time()), crm.zone) + timedelta(hours=20)
    with pytest.raises(Conflict):  # in the past
        crm.public_book({"name": "X", "email": "x@example.com", "start": _at(day, "10:00")}, now_utc=later)
    with pytest.raises(Invalid):
        crm.public_book({"name": "X", "email": "nope", "start": _at(day, "10:00")}, now_utc=LONG_AGO)


def test_public_booking_matches_an_existing_contact(crm):
    day = next_monday(crm)
    existing = crm.create_contact({"first_name": "Lee", "email": "lee@example.com"}, "t")
    a = crm.public_book({"name": "Lee Park", "email": "LEE@example.com", "start": _at(day, "14:00")},
                        now_utc=LONG_AGO)
    assert a.contact_id == existing.id
    assert crm.list_contacts("t")[1] == 1


# --- users & feed ------------------------------------------------------------

def test_users_and_tokens(crm):
    user, token = crm.create_user("alice", "member", "t")
    assert token.startswith("acrm_")
    assert crm.authenticate(token).id == user.id
    assert crm.authenticate("acrm_wrong") is None and crm.authenticate(None) is None
    with pytest.raises(Conflict):
        crm.create_user("alice", "member", "t")
    with pytest.raises(Invalid):
        crm.create_user("bob", "superuser", "t")
    crm.revoke_user("alice", "t")
    assert crm.authenticate(token) is None
    with pytest.raises(NotFound):
        crm.revoke_user("nobody", "t")


def test_calendar_feed_hides_details_by_default():
    for details in (False, True):
        crm = make_crm(feed_details=details)
        user, _ = crm.create_user("alice", "admin", "t")
        day = next_monday(crm)
        crm.create_appointment({"title": "Custody consult", "start": _at(day, "10:00"), "location": "Room 2"}, "t")
        feed_token = crm.issue_feed_token("alice", "t")
        assert crm.user_for_feed(feed_token).id == user.id
        ics = crm.calendar_feed(user)
        assert "BEGIN:VEVENT" in ics
        assert ("Custody consult" in ics) is details
        assert ("Room 2" in ics) is details
        assert ("SUMMARY:Busy" in ics) is (not details)
