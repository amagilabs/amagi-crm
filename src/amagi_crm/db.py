"""Engine and session setup shared by the API server and the CLI."""

from __future__ import annotations

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .crypto import FieldCipher, set_cipher
from .models import DEFAULT_STAGES, Base, Stage


def make_engine(url: str) -> Engine:
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, future=True, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_fk(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return engine


class Database:
    """Holds the engine, the session factory, and the active field cipher."""

    def __init__(self, settings: Settings):
        settings.require_keys()
        self.settings = settings
        self.cipher = FieldCipher(settings.encryption_keys, settings.secret_key)
        set_cipher(self.cipher)
        self.engine = make_engine(settings.database_url)
        self.sessionmaker = sessionmaker(self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.session() as s:
            if s.scalar(select(Stage.id).limit(1)) is None:
                for pos, (name, kind) in enumerate(DEFAULT_STAGES):
                    s.add(Stage(name=name, kind=kind, position=pos))
                s.commit()

    def session(self) -> Session:
        return self.sessionmaker()
