from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from amagi_crm.config import Settings
from amagi_crm.crypto import generate_fernet_key, generate_secret
from amagi_crm.db import Database
from amagi_crm.services import CRM

FIXTURES = Path(__file__).parent / "fixtures"

# A moment safely before any day the tests book, so "is this slot in the past?" never interferes.
LONG_AGO = datetime(2000, 1, 1)


def make_settings(**overrides) -> Settings:
    values = dict(database_url="sqlite://", encryption_keys=[generate_fernet_key()],
                  secret_key=generate_secret(), timezone="America/New_York",
                  working_hours="mon-fri 09:00-17:00", slot_minutes=30)
    values.update(overrides)
    return Settings(**values)


def make_crm(**overrides) -> CRM:
    db = Database(make_settings(**overrides))
    db.create_all()
    return CRM(db)


def next_monday(crm: CRM) -> date:
    """A Monday at least a week out, so it's always in the future and inside working hours."""
    d = crm.today() + timedelta(days=7)
    return d + timedelta(days=(7 - d.weekday()) % 7)


@pytest.fixture
def crm() -> CRM:
    return make_crm()


@pytest.fixture
def fixture_csv() -> Path:
    return FIXTURES / "contacts_export.csv"
