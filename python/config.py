"""Configuration loading and validation (all secrets come from .env only)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from this folder (python/) or any parent of the CWD.
_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env", override=False)


class ConfigError(Exception):
    """Raised when the environment is misconfigured."""


# Postgres identifier we accept for schema names (defensive, not security).
_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_URL_RE = re.compile(r"^postgres(ql)?(\+[a-z]+)?://\S+", re.IGNORECASE)


@dataclass(frozen=True)
class Settings:
    source_database_url: str
    copy_database_url: str | None
    source_schema: str
    copy_schema: str
    groq_api_key: str | None
    groq_model: str
    store_name: str
    currency: str
    return_window_days: int
    confirmation_ttl_minutes: int

    @property
    def same_database(self) -> bool:
        """True when the copy lives in the same database as the source."""
        if not self.copy_database_url:
            return True
        return _strip(self.source_database_url) == _strip(self.copy_database_url)

    @property
    def groq_key_present(self) -> bool:
        return bool(self.groq_api_key)


def _strip(url: str) -> str:
    """Normalize a DSN so two spellings of the same database compare equal."""
    url = re.sub(r"(?i)^postgresql(\+[a-z]+)?", "postgres://", url.strip())
    # drop everything after the database name that is not part of identity
    url = re.sub(r"([?&])schema=[^&]*", r"\1", url)
    return url.rstrip("/")


def mask_url(url: str | None) -> str:
    """Mask the password part of a DSN for display/logging."""
    if not url:
        return "(not set)"
    return re.sub(r"(://[^:/@\s]+:)[^@\s]+(@)", r"\1***\2", url)


def mask_key(key: str | None) -> str:
    if not key:
        return "(not set)"
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}****{key[-4:]}"


def load_settings(require_groq: bool = False, require_source: bool = True) -> Settings:
    """Read env vars, validate them, and return immutable settings.

    require_groq:   True for commands that must talk to the LLM (chat).
    require_source: False for commands that only touch the copy DB (admin).
    """
    def req(name: str) -> str:
        val = os.environ.get(name, "").strip()
        if not val:
            raise ConfigError(
                f"Missing required environment variable {name}. "
                f"Copy .env.example to .env and fill it in."
            )
        return val

    def opt(name: str, default: str | None = None) -> str | None:
        val = os.environ.get(name, "").strip()
        return val or default

    if require_source:
        source_url = req("SOURCE_DATABASE_URL")
    else:
        source_url = opt("SOURCE_DATABASE_URL", "") or ""
    if require_groq and not (os.environ.get("GROQ_API_KEY", "").strip()):
        raise ConfigError(
            "Missing required environment variable GROQ_API_KEY for this command."
        )
    if source_url and not _URL_RE.match(source_url):
        raise ConfigError("SOURCE_DATABASE_URL must be a postgres:// or postgresql:// URL.")

    copy_url = opt("COPY_DATABASE_URL")
    if copy_url and not _URL_RE.match(copy_url):
        raise ConfigError("COPY_DATABASE_URL must be a postgres:// or postgresql:// URL.")

    source_schema = (opt("SOURCE_SCHEMA", "public") or "public").lower()
    copy_schema = (opt("COPY_SCHEMA", "agent_copy") or "agent_copy").lower()

    if not _SCHEMA_RE.match(source_schema):
        raise ConfigError(f"SOURCE_SCHEMA {source_schema!r} is not a valid Postgres identifier.")
    if not _SCHEMA_RE.match(copy_schema):
        raise ConfigError(f"COPY_SCHEMA {copy_schema!r} is not a valid Postgres identifier.")

    # NON-NEGOTIABLE RULE 1: copy schema must never equal source schema or "public".
    if copy_schema == "public":
        raise ConfigError('COPY_SCHEMA must never be "public".')
    if copy_schema == source_schema:
        raise ConfigError(
            f"COPY_SCHEMA ({copy_schema!r}) must differ from SOURCE_SCHEMA ({source_schema!r})."
        )

    ttl_raw = opt("CONFIRMATION_TTL_MINUTES", "15") or "15"
    window_raw = opt("RETURN_WINDOW_DAYS", "7") or "7"
    try:
        ttl = int(ttl_raw)
        window = int(window_raw)
    except ValueError as exc:  # pragma: no cover
        raise ConfigError("CONFIRMATION_TTL_MINUTES and RETURN_WINDOW_DAYS must be integers.") from exc
    if ttl <= 0 or window < 0:
        raise ConfigError("CONFIRMATION_TTL_MINUTES must be > 0 and RETURN_WINDOW_DAYS must be >= 0.")

    return Settings(
        source_database_url=source_url,
        copy_database_url=copy_url,
        source_schema=source_schema,
        copy_schema=copy_schema,
        groq_api_key=os.environ.get("GROQ_API_KEY", "").strip() or None,
        groq_model=(opt("GROQ_MODEL", "llama-3.3-70b-versatile") or "llama-3.3-70b-versatile"),
        store_name=opt("STORE_NAME", "Demo Store") or "Demo Store",
        currency=opt("CURRENCY", "INR") or "INR",
        return_window_days=window,
        confirmation_ttl_minutes=ttl,
    )


# Shared settings instance, loaded once per process.
settings: Settings | None = None


def get_settings() -> Settings:
    global settings
    if settings is None:
        settings = load_settings()
    return settings
