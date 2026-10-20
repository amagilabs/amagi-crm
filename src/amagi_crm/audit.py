"""Tamper-evident audit log.

Every create, update, delete, single-record read, and export writes one row in
the same transaction as the change. Each row stores an HMAC over its own
content plus the previous row's hash, keyed with a secret that lives in the
app's environment, not in the database. Editing, reordering, or deleting a
row in the middle breaks the chain, and someone with database access alone
can't recompute it.

What it can't catch on its own: deleting the newest rows. Run
``amagi-crm audit verify`` on a schedule and keep the printed head hash
somewhere outside the server; a head hash that later goes missing means the
tail was cut.

Rows record field *names* only ("email", "phone"), never values, so the log
holds no client data, and erasing a contact never breaks the chain.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .crypto import get_cipher
from .models import AuditEntry, utcnow

GENESIS = "0" * 64
_LOCK = threading.RLock()


def _digest(key: bytes, prev_hash: str, ts: datetime, actor: str, action: str,
            entity: str, entity_id: int | None, fields: str) -> str:
    payload = json.dumps(
        [prev_hash, ts.isoformat(timespec="microseconds"), actor, action, entity, entity_id, fields],
        separators=(",", ":"),
    )
    return hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()


@contextmanager
def write_txn(session: Session):
    """Serialize writers so the hash chain never forks, then commit (or roll back)."""
    with _LOCK:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(4242618)"))
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise


def record(session: Session, actor: str, action: str, entity: str,
           entity_id: int | None = None, fields: list[str] | None = None) -> AuditEntry:
    """Append one entry. Call inside ``write_txn`` so it commits with the change."""
    prev = session.scalar(select(AuditEntry.hash).order_by(AuditEntry.id.desc()).limit(1)) or GENESIS
    ts = utcnow()
    fields_json = json.dumps(sorted(fields or []))
    entry = AuditEntry(
        ts=ts, actor=actor, action=action, entity=entity, entity_id=entity_id,
        fields=fields_json, prev_hash=prev,
        hash=_digest(get_cipher().audit_key, prev, ts, actor, action, entity, entity_id, fields_json),
    )
    session.add(entry)
    session.flush()
    return entry


@dataclass
class VerifyResult:
    ok: bool
    count: int
    head_hash: str
    bad_id: int | None = None
    reason: str = ""


def verify(session: Session) -> VerifyResult:
    key = get_cipher().audit_key
    prev = GENESIS
    count = 0
    for entry in session.scalars(select(AuditEntry).order_by(AuditEntry.id)):
        count += 1
        if entry.prev_hash != prev:
            return VerifyResult(False, count, prev, entry.id, "previous-hash link broken (row removed or reordered)")
        expected = _digest(key, entry.prev_hash, entry.ts, entry.actor, entry.action,
                           entry.entity, entry.entity_id, entry.fields)
        if not hmac.compare_digest(expected, entry.hash):
            return VerifyResult(False, count, prev, entry.id, "row content does not match its hash (row edited)")
        prev = entry.hash
    return VerifyResult(True, count, prev)
