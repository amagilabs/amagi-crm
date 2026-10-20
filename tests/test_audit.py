import pytest
from sqlalchemy import select, text

from amagi_crm import audit
from amagi_crm.crypto import derive_key, generate_secret
from amagi_crm.models import AuditEntry
from amagi_crm.services import Conflict


def _busy_crm(crm):
    c = crm.create_contact({"first_name": "Dana", "email": "dana@example.com", "phone": "555-0140"}, "alice")
    crm.update_contact(c.id, {"phone": "555-0199"}, "alice")
    crm.get_contact(c.id, "bob")
    crm.create_deal({"title": "Retainer", "contact_id": c.id}, "alice")
    crm.export_contacts_csv("alice")
    return c


def test_chain_verifies_and_records_who_did_what(crm):
    _busy_crm(crm)
    r = crm.audit_verify()
    assert r.ok and r.count == 5
    actions = [(e["actor"], e["action"], e["entity"]) for e in crm.audit_tail(10)]
    assert actions == [("alice", "create", "contact"), ("alice", "update", "contact"),
                       ("bob", "read", "contact"), ("alice", "create", "deal"),
                       ("alice", "export", "contact")]


def test_empty_log_verifies():
    from conftest import make_crm

    r = make_crm().audit_verify()
    assert r.ok and r.count == 0 and r.head_hash == audit.GENESIS


def test_log_stores_field_names_never_values(crm):
    _busy_crm(crm)
    with crm.session() as s:
        dump = " ".join(str(v) for row in s.execute(text("SELECT * FROM audit_log")) for v in row)
    assert '"phone"' in dump
    for value in ("Dana", "dana@example.com", "555-0140", "555-0199", "Retainer"):
        assert value not in dump


def test_edited_row_is_detected(crm):
    _busy_crm(crm)
    with crm.session() as s:
        s.execute(text("UPDATE audit_log SET actor = 'mallory' WHERE id = 2"))
        s.commit()
    r = crm.audit_verify()
    assert not r.ok and r.bad_id == 2 and "edited" in r.reason


def test_deleted_middle_row_is_detected(crm):
    _busy_crm(crm)
    with crm.session() as s:
        s.execute(text("DELETE FROM audit_log WHERE id = 3"))
        s.commit()
    r = crm.audit_verify()
    assert not r.ok and r.bad_id == 4 and "link" in r.reason


def _rechain(crm, key, rename_row):
    """Rewrite row ``rename_row``'s actor and recompute every hash with ``key``."""
    with crm.session() as s:
        prev = audit.GENESIS
        for e in s.scalars(select(AuditEntry).order_by(AuditEntry.id)):
            if e.id == rename_row:
                e.actor = "mallory"
            e.prev_hash = prev
            e.hash = audit._digest(key, prev, e.ts, e.actor, e.action, e.entity, e.entity_id, e.fields)
            prev = e.hash
        s.commit()


def test_rewriting_the_chain_needs_the_secret(crm):
    """Database access alone isn't enough to forge a chain that verifies."""
    _busy_crm(crm)
    _rechain(crm, derive_key(generate_secret(), "audit-chain"), rename_row=2)
    assert not crm.audit_verify().ok


def test_rechaining_with_the_real_secret_passes(crm):
    """Control for the test above: the forging procedure is correct, only the key is missing."""
    _busy_crm(crm)
    _rechain(crm, crm.db.cipher.audit_key, rename_row=2)
    assert crm.audit_verify().ok


def test_head_hash_changes_so_an_external_anchor_catches_tail_cuts(crm):
    _busy_crm(crm)
    head = crm.audit_verify().head_hash
    with crm.session() as s:
        s.execute(text("DELETE FROM audit_log WHERE id = (SELECT MAX(id) FROM audit_log)"))
        s.commit()
    r = crm.audit_verify()
    assert r.ok  # documented limitation: the chain alone can't see a cut tail
    assert r.head_hash != head  # but a head hash kept elsewhere no longer matches


def test_failed_write_leaves_no_audit_row(crm):
    crm.create_contact({"email": "lee@example.com"}, "alice")
    with pytest.raises(Conflict):
        crm.create_contact({"email": "lee@example.com"}, "alice")
    assert crm.audit_verify().count == 1
