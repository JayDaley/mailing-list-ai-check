"""Tests for configuration loading and its defaults."""

from __future__ import annotations

import pytest

from mailing_list_ai_check.config import Config
from mailing_list_ai_check.fetcher import open_client


def test_imap_settings_have_no_baked_in_defaults(monkeypatch):
    for var in ("IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD", "DATABASE_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PANGRAM_API_KEY", "test-key")

    cfg = Config.load()
    assert cfg.imap_host == ""
    assert cfg.imap_port == 993
    assert cfg.imap_username == ""
    assert cfg.imap_password == ""
    assert cfg.database_path == "./data/mail.db"


def test_open_client_rejects_missing_imap_host():
    with pytest.raises(RuntimeError, match="IMAP_HOST is not set"):
        open_client("", 993, "", "")


def test_pangram_key_is_optional_and_defaults_empty(monkeypatch):
    """Pull-only and web-only runs must work without a Pangram key.

    The scoring CLI is responsible for rejecting an empty key with a clear
    message (covered in the score CLI tests).
    """
    monkeypatch.delenv("PANGRAM_API_KEY", raising=False)
    cfg = Config.load()
    assert cfg.pangram_api_key == ""


def test_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("PANGRAM_API_KEY", "k")
    monkeypatch.setenv("IMAP_USERNAME", "real-user")
    monkeypatch.setenv("DATABASE_PATH", "/tmp/custom.db")
    cfg = Config.load()
    assert cfg.imap_username == "real-user"
    assert cfg.database_path == "/tmp/custom.db"
