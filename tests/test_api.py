import pytest
from fastapi.testclient import TestClient

from amagi_crm.api import create_app

from conftest import make_crm, next_monday


def _client(**settings):
    crm = make_crm(**settings)
    tokens = {role: crm.create_user(role, role, "setup")[1] for role in ("admin", "member", "readonly")}
    return crm, TestClient(create_app(crm)), tokens


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api():
    return _client()


def test_requires_a_bearer_token(api):
    _, client, tokens = api
    r = client.get("/api/me")
    assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer"
    assert client.get("/api/me", headers=_auth("acrm_nope")).status_code == 401
    me = client.get("/api/me", headers=_auth(tokens["member"])).json()
    assert me["role"] == "member" and me["timezone"] == "America/New_York" and me["public_booking"] is False


def test_roles(api):
    _, client, tokens = api
    body = {"first_name": "Dana", "email": "dana@example.com"}
    r = client.post("/api/contacts", json=body, headers=_auth(tokens["readonly"]))
    assert r.status_code == 403 and r.json()["detail"] == "your role is read-only"
    assert client.post("/api/contacts", json=body, headers=_auth(tokens["member"])).status_code == 201
    assert client.get("/api/contacts", headers=_auth(tokens["readonly"])).json()["total"] == 1
    for path in ("/api/audit", "/api/audit/verify", "/api/users", "/api/contacts/export.csv"):
        assert client.get(path, headers=_auth(tokens["member"])).status_code == 403, path
        assert client.get(path, headers=_auth(tokens["admin"])).status_code == 200, path


def test_errors_map_to_status_codes(api):
    _, client, tokens = api
    h = _auth(tokens["member"])
    first = client.post("/api/contacts", json={"email": "lee@example.com"}, headers=h).json()
    dup = client.post("/api/contacts", json={"email": "Lee@example.com"}, headers=h)
    assert dup.status_code == 409 and dup.json()["existing_id"] == first["id"]
    assert client.post("/api/contacts", json={"email": "bad"}, headers=h).status_code == 422
    assert client.get("/api/contacts/999", headers=h).status_code == 404


def test_security_headers(api):
    _, client, tokens = api
    for path in ("/", "/api/me"):
        r = client.get(path, headers=_auth(tokens["admin"]))
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert client.get("/api/me", headers=_auth(tokens["admin"])).headers["Cache-Control"] == "no-store"
    assert "Cache-Control" not in client.get("/").headers


def test_ui_and_static_are_served(api):
    _, client, _ = api
    assert "amagi-crm" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/healthz").json()["ok"] is True


def test_pipeline_move_and_reads_are_audited(api):
    crm, client, tokens = api
    h = _auth(tokens["member"])
    c = client.post("/api/contacts", json={"first_name": "Lee"}, headers=h).json()
    client.get(f"/api/contacts/{c['id']}", headers=_auth(tokens["readonly"]))
    d = client.post("/api/deals", json={"title": "Roof app", "amount_cents": 1200000, "contact_id": c["id"]},
                    headers=h).json()
    moved = client.post(f"/api/deals/{d['id']}/move", json={"stage": "Won", "position": 0}, headers=h).json()
    won = next(col for col in client.get("/api/pipeline", headers=h).json() if col["name"] == "Won")
    assert moved["stage_id"] == won["id"] and moved["closed_at"] is not None
    assert won["total_cents"] == 1200000
    log = client.get("/api/audit", headers=_auth(tokens["admin"])).json()
    assert ("readonly", "read", "contact") in [(e["actor"], e["action"], e["entity"]) for e in log]
    assert ("member", "move", "deal") in [(e["actor"], e["action"], e["entity"]) for e in log]
    assert client.get("/api/audit/verify", headers=_auth(tokens["admin"])).json()["ok"] is True


def test_appointments_and_availability(api):
    crm, client, tokens = api
    h = _auth(tokens["member"])
    day = next_monday(crm).isoformat()
    r = client.post("/api/appointments", json={"title": "Kickoff", "start": f"{day}T10:00"}, headers=h)
    assert r.status_code == 201 and r.json()["start"].startswith(f"{day}T10:00:00-0")
    assert client.post("/api/appointments", json={"title": "Clash", "start": f"{day}T10:15"},
                       headers=h).status_code == 409
    slots = client.get(f"/api/availability?date={day}", headers=h).json()["slots"]
    assert len(slots) == 15 and not any("T10:00" in s for s in slots)
    agenda = client.get(f"/api/agenda?date={day}", headers=h).json()
    assert len(agenda["appointments"]) == 1


def test_public_routes_are_off_by_default(api):
    _, client, _ = api
    assert client.get("/public/slots").status_code == 404
    assert client.get("/book").status_code == 404
    assert client.post("/public/book", json={"name": "X", "email": "x@example.com",
                                             "start": "2030-01-07T10:00"}).status_code == 404


def test_public_booking_and_rate_limit():
    crm, client, _ = _client(public_booking=True)
    day = next_monday(crm).isoformat()
    assert client.get("/book").status_code == 200
    slots = client.get(f"/public/slots?date={day}").json()["slots"]
    assert len(slots) == 16
    r = client.post("/public/book", json={"name": "Amara Osei", "email": "amara@example.com", "start": slots[0]})
    assert r.status_code == 201
    assert client.post("/public/book", json={"name": "B", "email": "b@example.com",
                                             "start": slots[0]}).status_code == 409
    assert len(client.get(f"/public/slots?date={day}").json()["slots"]) == 15
    for i in range(3):
        client.post("/public/book", json={"name": "C", "email": "c@example.com", "start": slots[0]})
    limited = client.post("/public/book", json={"name": "D", "email": "d@example.com", "start": slots[5]})
    assert limited.status_code == 429


def test_calendar_feed(api):
    crm, client, tokens = api
    token = crm.issue_feed_token("member", "setup")
    r = client.get(f"/feeds/{token}.ics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert r.text.startswith("BEGIN:VCALENDAR")
    assert client.get("/feeds/acrmfeed_wrong.ics").status_code == 404
