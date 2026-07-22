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
        )
