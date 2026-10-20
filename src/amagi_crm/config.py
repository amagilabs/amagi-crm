"""Settings, read from environment variables and an optional ``.env`` file.

Every setting is an ``AMAGI_CRM_*`` variable (plus ``DATABASE_URL``). Real
environment variables win over values in ``.env``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required settings are missing or malformed."""


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=value`` file. Blank lines and ``#`` comments are skipped."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    database_url: str = "sqlite:///./amagi_crm.db"
    encryption_keys: list[str] = field(default_factory=list)
    secret_key: str = ""
    timezone: str = "UTC"
    working_hours: str = "mon-fri 09:00-17:00"
    slot_minutes: int = 30
    public_booking: bool = False
    feed_details: bool = False

    @classmethod
    def load(cls, env_file: str | os.PathLike | None = ".env", environ: dict | None = None) -> "Settings":
        merged: dict[str, str] = {}
        if env_file:
            merged.update(read_env_file(Path(env_file)))
        merged.update(os.environ if environ is None else environ)

        keys_raw = merged.get("AMAGI_CRM_ENCRYPTION_KEYS", "")
        try:
            slot = int(merged.get("AMAGI_CRM_SLOT_MINUTES", "30"))
        except ValueError as exc:
            raise ConfigError("AMAGI_CRM_SLOT_MINUTES must be a whole number of minutes") from exc
        return cls(
            database_url=merged.get("DATABASE_URL") or cls.database_url,
            encryption_keys=[k.strip() for k in keys_raw.split(",") if k.strip()],
            secret_key=merged.get("AMAGI_CRM_SECRET_KEY", ""),
            timezone=merged.get("AMAGI_CRM_TIMEZONE") or "UTC",
            working_hours=merged.get("AMAGI_CRM_WORKING_HOURS") or cls.working_hours,
            slot_minutes=slot,
            public_booking=_truthy(merged.get("AMAGI_CRM_PUBLIC_BOOKING")),
            feed_details=_truthy(merged.get("AMAGI_CRM_FEED_DETAILS")),
        )

    def require_keys(self) -> None:
        """Fail closed: the app never runs, and never stores plaintext, without keys."""
        missing = []
        if not self.encryption_keys:
            missing.append("AMAGI_CRM_ENCRYPTION_KEYS")
        if len(self.secret_key) < 32:
            missing.append("AMAGI_CRM_SECRET_KEY (32+ characters)")
        if missing:
            raise ConfigError(
                "Missing required setting(s): " + ", ".join(missing)
                + ". Run `amagi-crm init` to generate a .env file."
            )
