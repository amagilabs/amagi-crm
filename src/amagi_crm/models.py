"""Database schema. Columns typed ``Encrypted`` never hold plaintext on disk.

Plaintext columns are limited to what the app must sort, sum, or schedule on:
ids, timestamps, deal amounts, stage, task done/due flags, and appointment times.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .crypto import get_cipher


def utcnow() -> datetime:
    """Naive UTC timestamp; every datetime column stores UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Encrypted(TypeDecorator):
    """A text column that is Fernet-encrypted on write and decrypted on read."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return get_cipher().encrypt(str(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return get_cipher().decrypt(value)


class EncryptedJSON(Encrypted):
    """Encrypted JSON (used for tag lists)."""

    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return super().process_bind_param(json.dumps(value), dialect)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return json.loads(super().process_result_value(value, dialect))


class Base(DeclarativeBase):
    pass


ROLES = ("admin", "member", "readonly")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="member")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    feed_token_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str | None] = mapped_column(Encrypted, nullable=True)
    last_name: Mapped[str | None] = mapped_column(Encrypted, nullable=True)
    email: Mapped[str | None] = mapped_column(Encrypted, nullable=True)
    email_index: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(Encrypted, nullable=True)
    company: Mapped[str | None] = mapped_column(Encrypted, nullable=True)
    tags: Mapped[list | None] = mapped_column(EncryptedJSON, nullable=True)
    notes: Mapped[str | None] = mapped_column(Encrypted, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


STAGE_KINDS = ("open", "won", "lost")


class Stage(Base):
    __tablename__ = "stages"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(8), default="open")


DEFAULT_STAGES = [
    ("Lead", "open"),
    ("Qualified", "open"),
    ("Proposal", "open"),
    ("Negotiation", "open"),
    ("Won", "won"),
    ("Lost", "lost"),
]


class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Encrypted)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True)
    stage_id: Mapped[int] = mapped_column(ForeignKey("stages.id"))
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    expected_close: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Encrypted, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Encrypted)
    notes: Mapped[str | None] = mapped_column(Encrypted, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id", ondelete="SET NULL"), nullable=True)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Encrypted)
    notes: Mapped[str | None] = mapped_column(Encrypted, nullable=True)
    location: Mapped[str | None] = mapped_column(Encrypted, nullable=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True)
    start_utc: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_utc: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(12), default="booked")
    source: Mapped[str] = mapped_column(String(12), default="staff")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEntry(Base):
    """Append-only, HMAC-chained log of who did what. Holds field names, never values."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(16))
    entity: Mapped[str] = mapped_column(String(24))
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fields: Mapped[str] = mapped_column(Text, default="[]")
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)
