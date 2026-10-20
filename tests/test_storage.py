"""Encryption at rest, checked against the bytes actually on disk."""

import sqlite3

import pytest
from sqlalchemy import text

from amagi_crm.crypto import DecryptionError, generate_fernet_key, generate_secret
from amagi_crm.db import Database
from amagi_crm.services import CRM

from conftest import make_settings


def _file_crm(path, keys, secret):
    db = Database(make_settings(database_url=f"sqlite:///{path}", encryption_keys=keys, secret_key=secret))
    db.create_all()
    return CRM(db)


def test_no_plaintext_in_the_database_file(tmp_path):
    path = tmp_path / "crm.db"
    crm = _file_crm(path, [generate_fernet_key()], generate_secret())
    c = crm.create_contact({"first_name": "Dana", "last_name": "Whitfield", "email": "dana@example.com",
                            "phone": "555-0140", "company": "Harbor Dental", "tags": ["vip"],
                            "notes": "prefers mornings"}, "test")
    d = crm.create_deal({"title": "Website rebuild", "contact_id": c.id, "notes": "phase one"}, "test")
    crm.create_task({"title": "Send engagement letter", "contact_id": c.id, "deal_id": d.id}, "test")
    crm.create_appointment({"title": "Kickoff call", "start": "2030-11-04T10:00", "location": "Front office"},
                           "test")
    crm.db.engine.dispose()

    raw = path.read_bytes()
    for plain in [b"Dana", b"Whitfield", b"dana@example.com", b"555-0140", b"Harbor Dental",
                  b"prefers mornings", b"Website rebuild", b"phase one", b"engagement letter",
                  b"Kickoff", b"Front office", b"vip"]:
        assert plain not in raw, plain

    con = sqlite3.connect(path)
    first_name, email_index = con.execute("SELECT first_name, email_index FROM contacts").fetchone()
    con.close()
    assert first_name.startswith("gAAAAA")  # a Fernet token
    assert len(email_index) == 64  # HMAC-SHA256 hex, not the address


def test_key_rotation_reencrypts_so_the_old_key_can_be_dropped(tmp_path):
    path, secret = tmp_path / "crm.db", generate_secret()
    old, new = generate_fernet_key(), generate_fernet_key()
    crm = _file_crm(path, [old], secret)
    crm.create_contact({"first_name": "Lee", "email": "lee@example.com"}, "test")
    crm.create_deal({"title": "Roof app"}, "test")
    crm.db.engine.dispose()

    crm = _file_crm(path, [new, old], secret)
    counts = crm.rotate_keys("test")
    assert counts["contacts"] == 1 and counts["deals"] == 1
    assert crm.rotate_keys("test")["contacts"] == 0
    crm.db.engine.dispose()

    crm = _file_crm(path, [new], secret)
    rows, _ = crm.list_contacts("test")
    assert rows[0].first_name == "Lee"
    assert crm.find_contact_by_email("LEE@example.com") is not None
    assert crm.audit_verify().ok
    crm.db.engine.dispose()

    crm = _file_crm(path, [old], secret)
    with pytest.raises(DecryptionError):
        crm.list_contacts("test")
    crm.db.engine.dispose()


def test_tags_are_encrypted_as_a_whole(crm):
    c = crm.create_contact({"first_name": "Amara", "tags": ["client", "referral"]}, "test")
    assert crm.get_contact(c.id, "test").tags == ["client", "referral"]
    with crm.session() as s:
        raw = s.execute(text("SELECT tags FROM contacts")).scalar()
    assert "client" not in raw
