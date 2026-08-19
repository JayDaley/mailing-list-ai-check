"""Configuration loading.

Secrets come from environment variables, loaded from a gitignored ``.env`` file
in local development. Never hard-code credentials in source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Copy .env.example to .env and fill it in."
        )
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean env var. ``1/true/yes/on`` (any case) is true; unset is default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    imap_host: str
    imap_port: int
    imap_username: str
    imap_password: str
    pangram_api_key: str
    database_path: str
    log_level: str
    flask_host: str
    flask_port: int
    #: When true, the web app rejects every state-changing request (any method
    #: other than GET/HEAD/OPTIONS) with a 403. Intended for a read-only instance
    #: exposed on an untrusted network: the dashboard and all its GET data stay
    #: fully usable, while pull/extract/score, import, settings and the person
    #: edits are refused. Off by default, so a normal local instance is unchanged.
    public_readonly: bool = False
    #: Whether ``GET /api/export`` (the full corpus download, message bodies
    #: included) is served. These two export switches are GET endpoints that the
    #: read-only guard does not cover; set this false to refuse the full export
    #: with a 403 on an instance whose message text should not be downloadable.
    #: On by default, so a normal instance is unchanged.
    allow_export: bool = True
    #: Whether ``GET /api/export/stats`` (the scores/metadata CSV archive, which
    #: carries no message text) is served. Set false to refuse it with a 403. On
    #: by default.
    allow_stats_export: bool = True

    @classmethod
    def load(cls) -> "Config":
        # IMAP settings are deployment-specific and have no baked-in defaults.
        # Pulling requires IMAP_HOST; some archives accept an anonymous login,
        # in which case set IMAP_USERNAME/IMAP_PASSWORD to that documented
        # login rather than real credentials.
        return cls(
            imap_host=os.environ.get("IMAP_HOST", ""),
            imap_port=int(os.environ.get("IMAP_PORT", "993")),
            imap_username=os.environ.get("IMAP_USERNAME", ""),
            imap_password=os.environ.get("IMAP_PASSWORD", ""),
            pangram_api_key=os.environ.get("PANGRAM_API_KEY", ""),
            database_path=os.environ.get("DATABASE_PATH", "./data/mail.db"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            flask_host=os.environ.get("FLASK_HOST", "127.0.0.1"),
            flask_port=int(os.environ.get("FLASK_PORT", "8050")),
            public_readonly=_env_bool("PUBLIC_READONLY", False),
            allow_export=_env_bool("ALLOW_EXPORT", True),
            allow_stats_export=_env_bool("ALLOW_STATS_EXPORT", True),
        )
