"""SQLite storage layer for the mail pipeline.

Everything downstream (fetch, extraction, scoring, API) reads and writes through
:class:`Store`. The design is deliberately plain ``sqlite3`` rather than an ORM
(see the module docstring rationale below).

Why ``sqlite3`` and not SQLAlchemy
----------------------------------
This project is a single-writer CLI pipeline feeding a read-mostly Flask API,
against one local SQLite file. The schema is small, fixed, and hand-tuned for a
few known access paths (dedupe upserts, a hash-keyed score cache, dashboard
filter indexes). An ORM's value — database portability, unit-of-work session
management, lazy relationship graphs, cross-backend migrations — buys us little
here, while adding a dependency, a mapping layer, and indirection over the exact
SQL (``INSERT ... ON CONFLICT``, ``PRAGMA`` tuning, partial-index choices) that
this layer's correctness depends on. The stdlib ``sqlite3`` module keeps the
dependency set tiny (a stated project goal), makes every query auditable, and is
more than adequate for the volumes involved. Typed :mod:`dataclasses` give us
the ergonomic "rows as objects" benefit without the ORM machinery.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# --- Extraction status values -------------------------------------------------

#: Allowed values for ``extractions.status``.
EXTRACTION_STATUSES = ("ok", "empty", "too_short", "failed")

# --- Dashboard query constants ------------------------------------------------

#: Pangram ``prediction_short`` labels that count as "flagged" for the dashboard
#: (any AI involvement). "Human"/"Mixed" are not flagged.
FLAGGED_LABELS = ("AI", "AI-Assisted")
#: Pre-built SQL ``IN`` list of the flagged labels. Values are trusted constants
#: defined here (no user input), so inlining them is safe from injection.
_FLAGGED_IN = "(" + ", ".join(f"'{label}'" for label in FLAGGED_LABELS) + ")"

#: Columns a message list may be sorted by, mapped to their SQL expression.
SORT_COLUMNS = {"date": "m.date", "fraction_ai": "s.fraction_ai"}
#: Default and maximum page sizes for :meth:`Store.query_messages`.
DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 200


# --- Schema migrations --------------------------------------------------------

_MIGRATION_001 = """
CREATE TABLE lists (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    folder         TEXT NOT NULL UNIQUE,
    last_synced_at TEXT
);

CREATE TABLE pull_state (
    list_id     INTEGER PRIMARY KEY REFERENCES lists(id) ON DELETE CASCADE,
    uidvalidity INTEGER NOT NULL,
    last_uid    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE persons (
    id             INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL
);

CREATE TABLE addresses (
    id           INTEGER PRIMARY KEY,
    email        TEXT NOT NULL UNIQUE,
    display_name TEXT,
    person_id    INTEGER REFERENCES persons(id) ON DELETE SET NULL
);

CREATE INDEX idx_addresses_person_id ON addresses(person_id);
CREATE INDEX idx_addresses_display_name ON addresses(display_name);

CREATE TABLE messages (
    id          INTEGER PRIMARY KEY,
    message_id  TEXT NOT NULL,          -- RFC 5322 Message-ID
    list_id     INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    address_id  INTEGER REFERENCES addresses(id) ON DELETE SET NULL,
    subject     TEXT,
    date        TEXT,                   -- UTC ISO-8601
    in_reply_to TEXT,
    raw_body    TEXT,                   -- text/plain body as fetched
    uid         INTEGER,                -- IMAP UID within the folder
    fetched_at  TEXT NOT NULL,
    UNIQUE(list_id, message_id)
);

CREATE INDEX idx_messages_date ON messages(date);
CREATE INDEX idx_messages_address_id ON messages(address_id);
CREATE INDEX idx_messages_list_id ON messages(list_id);

CREATE TABLE extractions (
    id             INTEGER PRIMARY KEY,
    message_id     INTEGER NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
    extracted_text TEXT NOT NULL,
    method         TEXT NOT NULL,
    char_count     INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ok', 'empty', 'too_short', 'failed')),
    created_at     TEXT NOT NULL
);

CREATE INDEX idx_extractions_status ON extractions(status);

CREATE TABLE scores (
    id                   INTEGER PRIMARY KEY,
    extraction_id        INTEGER NOT NULL UNIQUE REFERENCES extractions(id) ON DELETE CASCADE,
    fraction_ai          REAL,
    fraction_ai_assisted REAL,
    fraction_human       REAL,
    label                TEXT,
    detector_version     TEXT,
    raw_response         TEXT,          -- full Pangram JSON response
    text_sha256          TEXT NOT NULL,
    scored_at            TEXT NOT NULL
);

CREATE INDEX idx_scores_text_sha256 ON scores(text_sha256);
CREATE INDEX idx_scores_label ON scores(label);
"""

# NULL while the list exists on the IMAP server; stamped with the refresh time
# when a lists-index refresh finds it gone but local messages still reference it
# (server-deleted lists without messages are removed outright).
_MIGRATION_002 = """
ALTER TABLE lists ADD COLUMN removed_from_server_at TEXT;
"""

#: Ordered ``(version, sql)`` migrations. Append new ones; never edit applied.

# Backfill for PangramResult.label: Pangram's prediction_short never emits
# "AI-Assisted" — assisted-dominated text arrives as "Mixed". Rebadge stored
# rows the same way new scores are labeled so the dashboard's AI-Assisted band
# reflects the fractions. raw_response keeps the original prediction_short.
_MIGRATION_003 = """
UPDATE scores SET label = 'AI-Assisted'
WHERE label = 'Mixed'
  AND fraction_ai_assisted > COALESCE(fraction_ai, 0.0)
  AND fraction_ai_assisted > COALESCE(fraction_human, 0.0);
"""

# The decoded ``text/html`` part, captured alongside ``raw_body`` from Phase 8
# onward so the HTML structure can serve as an extraction oracle (see
# :mod:`mailing_list_ai_check.html_text`). NULL for rows fetched before this column
# existed and backfilled by the ``--backfill-html`` pull mode.
_MIGRATION_004 = """
ALTER TABLE messages ADD COLUMN raw_html TEXT;
"""

# ISO-8601 UTC timestamp of the newest message the IMAP server holds for the
# folder, or NULL when the list has never been checked against the server. Only
# tracked lists (those with local messages) are ever checked, so index-only
# lists keep NULL here — the server is never EXAMINEd for them.
_MIGRATION_005 = """
ALTER TABLE lists ADD COLUMN last_message_at TEXT;
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, _MIGRATION_001),
    (2, _MIGRATION_002),
    (3, _MIGRATION_003),
    (4, _MIGRATION_004),
    (5, _MIGRATION_005),
]


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring ``conn``'s database up to the latest schema version.

    Idempotent: creates the ``schema_version`` bookkeeping table if missing and
    applies only migrations newer than the recorded version, so calling this on
    an already-current database is a no-op.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    current = row["v"] if row and row["v"] is not None else 0
    for version, script in MIGRATIONS:
        if version > current:
            conn.executescript(script)
            conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
            conn.commit()


# --- Row dataclasses ----------------------------------------------------------


@dataclass(frozen=True)
class MailingList:
    """A mailing list / IMAP folder pairing."""

    id: int
    name: str
    folder: str
    last_synced_at: str | None
    removed_from_server_at: str | None = None
    last_message_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MailingList":
        return cls(
            id=row["id"],
            name=row["name"],
            folder=row["folder"],
            last_synced_at=row["last_synced_at"],
            removed_from_server_at=row["removed_from_server_at"],
            last_message_at=row["last_message_at"],
        )


@dataclass(frozen=True)
class PullState:
    """Incremental-pull cursor for one list (UIDVALIDITY + last UID seen)."""

    list_id: int
    uidvalidity: int
    last_uid: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PullState":
        return cls(
            list_id=row["list_id"],
            uidvalidity=row["uidvalidity"],
            last_uid=row["last_uid"],
        )


@dataclass(frozen=True)
class Address:
    """An email address seen as a message sender."""

    id: int
    email: str
    display_name: str | None
    person_id: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Address":
        return cls(
            id=row["id"],
            email=row["email"],
            display_name=row["display_name"],
            person_id=row["person_id"],
        )


@dataclass(frozen=True)
class Person:
    """A person entity grouping one or more addresses."""

    id: int
    canonical_name: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Person":
        return cls(id=row["id"], canonical_name=row["canonical_name"])


@dataclass(frozen=True)
class Message:
    """A fetched message (its ``text/plain`` body plus metadata).

    ``raw_html`` is the decoded ``text/html`` part when the message carried one
    (NULL otherwise, and NULL for rows fetched before the column existed until
    the ``--backfill-html`` pull mode fills them in).
    """

    id: int
    message_id: str
    list_id: int
    address_id: int | None
    subject: str | None
    date: str | None
    in_reply_to: str | None
    raw_body: str | None
    uid: int | None
    fetched_at: str
    raw_html: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Message":
        return cls(
            id=row["id"],
            message_id=row["message_id"],
            list_id=row["list_id"],
            address_id=row["address_id"],
            subject=row["subject"],
            date=row["date"],
            in_reply_to=row["in_reply_to"],
            raw_body=row["raw_body"],
            uid=row["uid"],
            fetched_at=row["fetched_at"],
            raw_html=row["raw_html"],
        )


@dataclass(frozen=True)
class Extraction:
    """The author's newly written text extracted from a message."""

    id: int
    message_id: int
    extracted_text: str
    method: str
    char_count: int
    status: str
    created_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Extraction":
        return cls(
            id=row["id"],
            message_id=row["message_id"],
            extracted_text=row["extracted_text"],
            method=row["method"],
            char_count=row["char_count"],
            status=row["status"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class Score:
    """A Pangram verdict for one extraction."""

    id: int
    extraction_id: int
    fraction_ai: float | None
    fraction_ai_assisted: float | None
    fraction_human: float | None
    label: str | None
    detector_version: str | None
    raw_response: str | None
    text_sha256: str
    scored_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Score":
        return cls(
            id=row["id"],
            extraction_id=row["extraction_id"],
            fraction_ai=row["fraction_ai"],
            fraction_ai_assisted=row["fraction_ai_assisted"],
            fraction_human=row["fraction_human"],
            label=row["label"],
            detector_version=row["detector_version"],
            raw_response=row["raw_response"],
            text_sha256=row["text_sha256"],
            scored_at=row["scored_at"],
        )


@dataclass(frozen=True)
class MessageUpsert:
    """Result of :meth:`Store.upsert_message`: the row plus whether it was new."""

    message: Message
    inserted: bool


@dataclass(frozen=True)
class MergeSuggestion:
    """A candidate person grouping: one display name shared by several emails."""

    display_name: str
    address_ids: tuple[int, ...]
    emails: tuple[str, ...]


@dataclass(frozen=True)
class MessageFilters:
    """Combinable filters for :meth:`Store.query_messages` and :meth:`Store.summary`.

    The dashboard's global filter bar maps onto this object. Every field is
    optional; ``None`` means "do not constrain on this dimension". Pagination and
    sort live here too so the API layer parses one shape. All filters combine
    with ``AND``.
    """

    list_name: str | None = None
    address: str | None = None
    person_id: int | None = None
    date_from: str | None = None
    date_to: str | None = None
    label: str | None = None
    min_likelihood: float | None = None
    max_likelihood: float | None = None
    q: str | None = None
    has_score: bool | None = None
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE
    sort: str = "date"
    order: str = "desc"


# --- Helpers ------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Current time as a UTC ISO-8601 string (second precision)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    """Return the hex SHA-256 of ``text`` (the score cache key)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _word_count(text: str) -> int:
    return len(text.split())


# --- Dashboard query building -------------------------------------------------

#: The shared FROM/JOIN skeleton for every dashboard query. A message has at
#: most one extraction (``extractions.message_id`` is UNIQUE) and an extraction
#: at most one score (``scores.extraction_id`` is UNIQUE), so these LEFT JOINs
#: never multiply message rows.
_MESSAGE_FROM = """
FROM messages m
JOIN lists l ON l.id = m.list_id
LEFT JOIN addresses a ON a.id = m.address_id
LEFT JOIN persons p ON p.id = a.person_id
LEFT JOIN extractions e ON e.message_id = m.id
LEFT JOIN scores s ON s.extraction_id = e.id
"""

#: The columns a message-list row exposes (joined across all five tables).
_MESSAGE_COLUMNS = """
    m.id AS id,
    m.message_id AS message_id,
    l.name AS list,
    m.date AS date,
    m.subject AS subject,
    m.in_reply_to AS in_reply_to,
    a.email AS from_address,
    a.display_name AS from_display_name,
    a.person_id AS person_id,
    p.canonical_name AS person_name,
    e.status AS extraction_status,
    e.method AS extraction_method,
    e.char_count AS extraction_char_count,
    s.fraction_ai AS fraction_ai,
    s.fraction_ai_assisted AS fraction_ai_assisted,
    s.fraction_human AS fraction_human,
    s.label AS label,
    s.detector_version AS detector_version,
    s.scored_at AS scored_at
"""


def _build_message_where(f: MessageFilters) -> tuple[str, list[Any]]:
    """Build the ``WHERE`` fragment + bound params for ``f`` (empty if no filters).

    Applied identically by :meth:`Store.query_messages` and :meth:`Store.summary`
    so the explorer and the overview always agree on what a filter selects.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if f.list_name:
        clauses.append("l.name = ?")
        params.append(f.list_name)
    if f.address:
        clauses.append("a.email = ?")
        params.append(f.address.strip().lower())
    if f.person_id is not None:
        clauses.append("a.person_id = ?")
        params.append(f.person_id)
    if f.date_from:
        clauses.append("m.date >= ?")
        params.append(f.date_from)
    if f.date_to:
        clauses.append("m.date <= ?")
        params.append(f.date_to)
    if f.label:
        clauses.append("s.label = ?")
        params.append(f.label)
    if f.min_likelihood is not None:
        clauses.append("s.fraction_ai >= ?")
        params.append(f.min_likelihood)
    if f.max_likelihood is not None:
        clauses.append("s.fraction_ai <= ?")
        params.append(f.max_likelihood)
    if f.q:
        like = f"%{f.q}%"
        clauses.append("(m.subject LIKE ? OR e.extracted_text LIKE ?)")
        params.extend([like, like])
    if f.has_score is True:
        clauses.append("s.id IS NOT NULL")
    elif f.has_score is False:
        clauses.append("s.id IS NULL")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


# --- Store --------------------------------------------------------------------


class Store:
    """Typed access to the SQLite database.

    Open with a filesystem path (or ``":memory:"``). Use as a context manager,
    or call :meth:`close` explicitly. Each connection runs with WAL journaling
    and foreign-key enforcement enabled, and the schema is migrated to the
    latest version on open.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            parent = Path(self.path).expanduser().parent
            parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        apply_migrations(self.conn)

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection."""
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- lists ----------------------------------------------------------------

    def upsert_list(self, name: str, folder: str) -> MailingList:
        """Insert the list if new (keyed on ``folder``), else return existing.

        Idempotent — re-inserting the same folder leaves the row untouched and
        returns it.
        """
        self.conn.execute(
            "INSERT INTO lists(name, folder) VALUES (?, ?) ON CONFLICT(folder) DO NOTHING",
            (name, folder),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM lists WHERE folder = ?", (folder,)).fetchone()
        return MailingList.from_row(row)

    def get_list(self, list_id: int) -> MailingList | None:
        """Return the list with ``list_id``, or ``None``."""
        row = self.conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
        return MailingList.from_row(row) if row else None

    def set_list_synced(self, list_id: int, when: str | None = None) -> None:
        """Stamp ``lists.last_synced_at`` (defaults to now)."""
        self.conn.execute(
            "UPDATE lists SET last_synced_at = ? WHERE id = ?",
            (when or _utcnow_iso(), list_id),
        )
        self.conn.commit()

    def set_list_last_message(self, list_id: int, when: str | None) -> None:
        """Record ``lists.last_message_at`` — the server's newest-message time.

        ``when`` is a UTC ISO-8601 string, or ``None`` when the folder holds no
        messages / has not been checked. Unlike :meth:`set_list_synced` there is
        no "now" default: the value is always the server's INTERNALDATE.
        """
        self.conn.execute(
            "UPDATE lists SET last_message_at = ? WHERE id = ?",
            (when, list_id),
        )
        self.conn.commit()

    def tracked_list_folders(self) -> list[tuple[int, str]]:
        """Return ``(id, folder)`` for every list worth checking against the server.

        A list is "tracked" once it holds at least one local message (the user
        has pulled it). This restricts the caller to those lists and excludes any
        the server no longer carries (``removed_from_server_at`` set), so the
        ~1,400 index-only folders are never EXAMINEd. Ordered by ``folder`` for a
        deterministic sweep.
        """
        rows = self.conn.execute(
            "SELECT l.id, l.folder FROM lists l "
            "WHERE l.removed_from_server_at IS NULL "
            "AND EXISTS (SELECT 1 FROM messages m WHERE m.list_id = l.id) "
            "ORDER BY l.folder"
        ).fetchall()
        return [(row["id"], row["folder"]) for row in rows]

    def refresh_lists_index(self, entries: Sequence[tuple[str, str]]) -> dict[str, int]:
        """Reconcile the ``lists`` table with the server's full folder enumeration.

        ``entries`` is every ``(name, folder)`` pair the IMAP ``LIST`` command
        returned. Rather than wiping and re-inserting (which would orphan
        messages), rows are reconciled:

        - folders new to the store are inserted;
        - known folders seen again have any ``removed_from_server_at`` cleared;
        - stored folders **missing** from the enumeration are deleted when no
          messages reference them, otherwise kept and stamped
          ``removed_from_server_at`` so the list and its messages survive.

        Returns counts: ``added``, ``restored``, ``deleted``, ``kept_missing``
        and the resulting ``total``. The enumeration goes through a temp table
        so ~1,400 folders never hit SQLite's bound-parameter limit.
        """
        conn = self.conn
        conn.execute("DROP TABLE IF EXISTS temp._server_folders")
        conn.execute(
            "CREATE TEMP TABLE _server_folders (name TEXT NOT NULL, folder TEXT PRIMARY KEY)"
        )
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO _server_folders(name, folder) VALUES (?, ?)", entries
            )

            added = conn.execute(
                "INSERT INTO lists(name, folder) "
                "SELECT name, folder FROM _server_folders WHERE true "
                "ON CONFLICT(folder) DO NOTHING"
            ).rowcount
            restored = conn.execute(
                "UPDATE lists SET removed_from_server_at = NULL "
                "WHERE removed_from_server_at IS NOT NULL "
                "AND folder IN (SELECT folder FROM _server_folders)"
            ).rowcount
            deleted = conn.execute(
                "DELETE FROM lists "
                "WHERE folder NOT IN (SELECT folder FROM _server_folders) "
                "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.list_id = lists.id)"
            ).rowcount
            # Everything still missing after the delete has messages: keep it,
            # stamped so the UI can say the server no longer carries the list.
            kept_missing = conn.execute(
                "UPDATE lists SET removed_from_server_at = ? "
                "WHERE removed_from_server_at IS NULL "
                "AND folder NOT IN (SELECT folder FROM _server_folders)",
                (_utcnow_iso(),),
            ).rowcount
            total = conn.execute("SELECT COUNT(*) FROM lists").fetchone()[0]
            conn.commit()
        finally:
            conn.execute("DROP TABLE IF EXISTS temp._server_folders")
        return {
            "added": added,
            "restored": restored,
            "deleted": deleted,
            "kept_missing": kept_missing,
            "total": total,
        }

    # -- pull_state -----------------------------------------------------------

    def get_pull_state(self, list_id: int) -> PullState | None:
        """Return the incremental cursor for ``list_id``, or ``None`` if unset."""
        row = self.conn.execute("SELECT * FROM pull_state WHERE list_id = ?", (list_id,)).fetchone()
        return PullState.from_row(row) if row else None

    def set_pull_state(self, list_id: int, uidvalidity: int, last_uid: int) -> PullState:
        """Create or replace the cursor for ``list_id``.

        On a UIDVALIDITY change the caller resyncs and then calls this with the
        new ``uidvalidity`` and recomputed ``last_uid``; the row is overwritten.
        """
        self.conn.execute(
            "INSERT INTO pull_state(list_id, uidvalidity, last_uid) VALUES (?, ?, ?) "
            "ON CONFLICT(list_id) DO UPDATE SET "
            "uidvalidity = excluded.uidvalidity, last_uid = excluded.last_uid",
            (list_id, uidvalidity, last_uid),
        )
        self.conn.commit()
        return PullState(list_id=list_id, uidvalidity=uidvalidity, last_uid=last_uid)

    # -- addresses ------------------------------------------------------------

    def upsert_address(self, email: str, display_name: str | None = None) -> Address:
        """Insert or fetch an address, normalizing ``email`` to lowercase.

        If the address exists and a ``display_name`` is supplied while the stored
        one is empty, the stored name is backfilled. Returns the current row.
        """
        normalized = email.strip().lower()
        self.conn.execute(
            "INSERT INTO addresses(email, display_name) VALUES (?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "display_name = COALESCE(NULLIF(addresses.display_name, ''), excluded.display_name)",
            (normalized, display_name),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM addresses WHERE email = ?", (normalized,)).fetchone()
        return Address.from_row(row)

    def get_address(self, address_id: int) -> Address | None:
        """Return the address with ``address_id``, or ``None``."""
        row = self.conn.execute("SELECT * FROM addresses WHERE id = ?", (address_id,)).fetchone()
        return Address.from_row(row) if row else None

    # -- messages -------------------------------------------------------------

    def upsert_message(
        self,
        *,
        message_id: str,
        list_id: int,
        address_id: int | None,
        subject: str | None,
        date: str | None,
        in_reply_to: str | None,
        raw_body: str | None,
        uid: int | None,
        fetched_at: str | None = None,
        raw_html: str | None = None,
    ) -> MessageUpsert:
        """Insert a message, deduping on ``(list_id, message_id)``.

        Idempotent: a re-pull of the same message is a no-op that returns the
        existing row with ``inserted=False`` (``raw_html`` is stored only on
        insert; a conflicting existing row is left exactly as-is). New rows
        return ``inserted=True``.
        """
        cur = self.conn.execute(
            "INSERT INTO messages("
            "message_id, list_id, address_id, subject, date, in_reply_to, raw_body, uid, "
            "fetched_at, raw_html"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(list_id, message_id) DO NOTHING",
            (
                message_id,
                list_id,
                address_id,
                subject,
                date,
                in_reply_to,
                raw_body,
                uid,
                fetched_at or _utcnow_iso(),
                raw_html,
            ),
        )
        self.conn.commit()
        inserted = cur.rowcount > 0
        row = self.conn.execute(
            "SELECT * FROM messages WHERE list_id = ? AND message_id = ?",
            (list_id, message_id),
        ).fetchone()
        return MessageUpsert(message=Message.from_row(row), inserted=inserted)

    def get_message(self, message_pk: int) -> Message | None:
        """Return the message with primary key ``message_pk``, or ``None``."""
        row = self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_pk,)).fetchone()
        return Message.from_row(row) if row else None

    def get_parent_body(
        self, in_reply_to: str, *, exclude_message_id: str | None = None
    ) -> str | None:
        """Return the raw body of the message this ``In-Reply-To`` points at.

        ``in_reply_to`` is the raw header value, which may carry surrounding
        whitespace or (rarely) several ids / CFWS comments; the first
        angle-bracket ``<...>`` token is used as the parent Message-ID, falling
        back to the stripped raw value when there is no such token. The same
        message can exist on several lists, so any stored copy is fine. Returns
        ``None`` when no parent is found or the found row has a NULL body.

        ``exclude_message_id`` (the requesting message's own Message-ID) guards
        the self-reply case: a message whose ``In-Reply-To`` names its own id
        would otherwise resolve to its own body, and the parent-diff assist would
        then delete the entire message. When the resolved parent id equals it,
        ``None`` is returned so the message is extracted as if it had no parent.
        """
        match = re.search(r"<[^>]+>", in_reply_to)
        parent_id = match.group(0) if match else in_reply_to.strip()
        if exclude_message_id is not None and parent_id == exclude_message_id:
            return None
        row = self.conn.execute(
            "SELECT raw_body FROM messages WHERE message_id = ? ORDER BY id LIMIT 1",
            (parent_id,),
        ).fetchone()
        return row["raw_body"] if row else None

    def set_message_raw_html(self, message_pk: int, raw_html: str) -> None:
        """Store the decoded ``text/html`` part for an already-stored message.

        Used by the ``--backfill-html`` pull mode to fill in ``raw_html`` for
        rows fetched before the column existed. Does not touch ``raw_body`` or
        any other field.

        ``raw_html`` is three-state: ``NULL`` means "never checked" (the
        backfill queue), a non-empty string is the captured HTML, and the empty
        string ``""`` is a tombstone meaning "checked — the message carries no
        HTML part". The backfill stamps ``""`` for HTML-less messages so
        :meth:`iter_messages_missing_html` stops returning them; ``""`` is falsy
        everywhere the HTML is consumed, so it behaves exactly like NULL for the
        extraction oracle and the signature hint.
        """
        self.conn.execute(
            "UPDATE messages SET raw_html = ? WHERE id = ?",
            (raw_html, message_pk),
        )
        self.conn.commit()

    def iter_messages_missing_html(self, list_id: int) -> Iterator[Message]:
        """Yield the list's messages that still need an HTML backfill, by UID.

        A message qualifies when ``raw_html IS NULL`` and it has a UID (so it can
        be re-fetched from IMAP). Ordered by ``uid`` so a capped backfill run
        makes deterministic forward progress across runs. Messages the backfill
        has already checked and found HTML-less carry the empty-string tombstone
        (see :meth:`set_message_raw_html`), not NULL, so they are excluded here
        and a run is never stuck re-fetching the same HTML-less messages.
        """
        rows = self.conn.execute(
            "SELECT * FROM messages "
            "WHERE list_id = ? AND raw_html IS NULL AND uid IS NOT NULL "
            "ORDER BY uid",
            (list_id,),
        ).fetchall()
        for row in rows:
            yield Message.from_row(row)

    def iter_messages_without_extraction(self) -> Iterator[Message]:
        """Yield messages that have no ``extractions`` row yet (extraction queue)."""
        rows = self.conn.execute(
            "SELECT m.* FROM messages m "
            "LEFT JOIN extractions e ON e.message_id = m.id "
            "WHERE e.id IS NULL ORDER BY m.id"
        ).fetchall()
        for row in rows:
            yield Message.from_row(row)

    # -- extractions ----------------------------------------------------------

    def insert_extraction(
        self,
        *,
        message_id: int,
        extracted_text: str,
        method: str,
        status: str,
        char_count: int | None = None,
        created_at: str | None = None,
    ) -> Extraction:
        """Record the extraction for a message (one per message).

        ``char_count`` defaults to ``len(extracted_text)``. ``status`` must be
        one of :data:`EXTRACTION_STATUSES` (also enforced by a CHECK constraint).
        """
        if status not in EXTRACTION_STATUSES:
            raise ValueError(
                f"invalid extraction status {status!r}; expected one of {EXTRACTION_STATUSES}"
            )
        cur = self.conn.execute(
            "INSERT INTO extractions("
            "message_id, extracted_text, method, char_count, status, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                message_id,
                extracted_text,
                method,
                len(extracted_text) if char_count is None else char_count,
                status,
                created_at or _utcnow_iso(),
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM extractions WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return Extraction.from_row(row)

    def get_extraction(self, extraction_id: int) -> Extraction | None:
        """Return the extraction with ``extraction_id``, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM extractions WHERE id = ?", (extraction_id,)
        ).fetchone()
        return Extraction.from_row(row) if row else None

    def _unscored_ok_extractions(self) -> list[Extraction]:
        """Return every ``status='ok'`` extraction that has no ``scores`` row yet."""
        rows = self.conn.execute(
            "SELECT e.* FROM extractions e "
            "LEFT JOIN scores s ON s.extraction_id = e.id "
            "WHERE s.id IS NULL AND e.status = 'ok' ORDER BY e.id"
        ).fetchall()
        return [Extraction.from_row(row) for row in rows]

    def iter_extractions_needing_score(self, min_words: int = 50) -> Iterator[Extraction]:
        """Yield unscored ``ok`` extractions with at least ``min_words`` words.

        This is the Pangram scoring queue: only ``status='ok'`` extractions that
        have no ``scores`` row yet, filtered to the reliability floor (default 50
        words, per the Pangram findings) so short text is never sent.
        """
        for extraction in self._unscored_ok_extractions():
            if _word_count(extraction.extracted_text) >= min_words:
                yield extraction

    def iter_too_short_extractions(self, min_words: int = 50) -> Iterator[Extraction]:
        """Yield unscored ``ok`` extractions with fewer than ``min_words`` words.

        The complement of :meth:`iter_extractions_needing_score`: these fall
        under the reliability floor and the scorer marks them ``too_short``
        rather than paying Pangram for a verdict the vendor deems untrustworthy.
        """
        for extraction in self._unscored_ok_extractions():
            if _word_count(extraction.extracted_text) < min_words:
                yield extraction

    def update_extraction_status(self, extraction_id: int, status: str) -> Extraction | None:
        """Set ``extractions.status`` for ``extraction_id``; return the updated row.

        ``status`` must be one of :data:`EXTRACTION_STATUSES`. Returns ``None`` if
        no such extraction exists.
        """
        if status not in EXTRACTION_STATUSES:
            raise ValueError(
                f"invalid extraction status {status!r}; expected one of {EXTRACTION_STATUSES}"
            )
        self.conn.execute(
            "UPDATE extractions SET status = ? WHERE id = ?",
            (status, extraction_id),
        )
        self.conn.commit()
        return self.get_extraction(extraction_id)

    # -- scores ---------------------------------------------------------------

    def find_score_by_text_sha256(self, text_sha256: str) -> Score | None:
        """Return any existing score for identical text (the Pangram cache).

        Lets the scorer reuse a verdict for text it has already paid to classify,
        keyed on the SHA-256 of the extracted text — never score identical text
        twice.
        """
        row = self.conn.execute(
            "SELECT * FROM scores WHERE text_sha256 = ? ORDER BY id LIMIT 1",
            (text_sha256,),
        ).fetchone()
        return Score.from_row(row) if row else None

    def insert_score(
        self,
        *,
        extraction_id: int,
        text_sha256: str,
        fraction_ai: float | None = None,
        fraction_ai_assisted: float | None = None,
        fraction_human: float | None = None,
        label: str | None = None,
        detector_version: str | None = None,
        raw_response: Mapping[str, Any] | str | None = None,
        scored_at: str | None = None,
    ) -> Score:
        """Store a Pangram verdict for ``extraction_id`` (one per extraction).

        ``raw_response`` may be a mapping (serialized to JSON text) or a
        pre-serialized JSON string.
        """
        if isinstance(raw_response, Mapping):
            raw_json: str | None = json.dumps(raw_response)
        else:
            raw_json = raw_response
        cur = self.conn.execute(
            "INSERT INTO scores("
            "extraction_id, fraction_ai, fraction_ai_assisted, fraction_human, "
            "label, detector_version, raw_response, text_sha256, scored_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                extraction_id,
                fraction_ai,
                fraction_ai_assisted,
                fraction_human,
                label,
                detector_version,
                raw_json,
                text_sha256,
                scored_at or _utcnow_iso(),
            ),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM scores WHERE id = ?", (cur.lastrowid,)).fetchone()
        return Score.from_row(row)

    # -- persons --------------------------------------------------------------

    def create_person(self, canonical_name: str) -> Person:
        """Create a person entity and return it."""
        cur = self.conn.execute("INSERT INTO persons(canonical_name) VALUES (?)", (canonical_name,))
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM persons WHERE id = ?", (cur.lastrowid,)).fetchone()
        return Person.from_row(row)

    def get_person(self, person_id: int) -> Person | None:
        """Return the person with ``person_id``, or ``None``."""
        row = self.conn.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()
        return Person.from_row(row) if row else None

    def assign_address_to_person(self, address_id: int, person_id: int | None) -> None:
        """Link ``address_id`` to ``person_id`` (pass ``None`` to detach)."""
        self.conn.execute(
            "UPDATE addresses SET person_id = ? WHERE id = ?",
            (person_id, address_id),
        )
        self.conn.commit()

    def addresses_for_person(self, person_id: int) -> list[Address]:
        """Return all addresses linked to ``person_id``."""
        rows = self.conn.execute(
            "SELECT * FROM addresses WHERE person_id = ? ORDER BY email",
            (person_id,),
        ).fetchall()
        return [Address.from_row(row) for row in rows]

    def suggest_person_merges(self) -> list[MergeSuggestion]:
        """Suggest person groupings by identical display name across addresses.

        Returns one :class:`MergeSuggestion` per non-empty ``display_name`` shared
        by more than one distinct email, for one-click confirmation in the UI.
        """
        rows = self.conn.execute(
            "SELECT display_name, "
            "GROUP_CONCAT(id) AS ids, GROUP_CONCAT(email) AS emails "
            "FROM addresses "
            "WHERE display_name IS NOT NULL AND display_name <> '' "
            "GROUP BY display_name "
            "HAVING COUNT(DISTINCT email) > 1 "
            "ORDER BY display_name"
        ).fetchall()
        suggestions: list[MergeSuggestion] = []
        for row in rows:
            ids = tuple(int(x) for x in row["ids"].split(","))
            emails = tuple(row["emails"].split(","))
            suggestions.append(
                MergeSuggestion(
                    display_name=row["display_name"],
                    address_ids=ids,
                    emails=emails,
                )
            )
        return suggestions

    def update_person_name(self, person_id: int, canonical_name: str) -> Person | None:
        """Rename ``person_id``; return the updated row, or ``None`` if absent."""
        self.conn.execute(
            "UPDATE persons SET canonical_name = ? WHERE id = ?",
            (canonical_name, person_id),
        )
        self.conn.commit()
        return self.get_person(person_id)

    def delete_person(self, person_id: int) -> bool:
        """Delete ``person_id``; return whether a row was removed.

        The ``addresses.person_id`` FK is ``ON DELETE SET NULL``, so a person's
        addresses are detached (not deleted) automatically.
        """
        cur = self.conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # -- dashboard: message detail helpers ------------------------------------

    def find_message_by_message_id(self, message_id: str) -> Message | None:
        """Return the lowest-id message with this RFC 5322 Message-ID, or ``None``.

        Message-IDs are unique only per list, so a Message-ID can in principle
        appear on more than one list; the earliest-stored match is returned. Used
        to resolve a reply's ``in_reply_to`` to a stored thread parent.
        """
        row = self.conn.execute(
            "SELECT * FROM messages WHERE message_id = ? ORDER BY id LIMIT 1",
            (message_id,),
        ).fetchone()
        return Message.from_row(row) if row else None

    def extraction_for_message(self, message_pk: int) -> Extraction | None:
        """Return the extraction for message ``message_pk`` (by FK), or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (message_pk,)
        ).fetchone()
        return Extraction.from_row(row) if row else None

    def score_for_extraction(self, extraction_id: int) -> Score | None:
        """Return the score for ``extraction_id``, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM scores WHERE extraction_id = ?", (extraction_id,)
        ).fetchone()
        return Score.from_row(row) if row else None

    # -- dashboard: entity listings -------------------------------------------

    def list_rows(self) -> list[dict[str, Any]]:
        """Every list with its message count, scored count and label mix (for /api/lists).

        Each row carries the base list columns plus ``message_count`` and two
        scoring aggregates powering the lists-index mix bars: ``scored_count``
        (messages on the list that have a Pangram score) and ``label_counts``
        (a ``{label: count}`` dict of scored messages per label, null labels
        omitted, empty when nothing on the list is scored). The label mix comes
        from one extra aggregate query merged in Python — mirroring the
        ``scores → extractions → messages`` join chain of ``_MESSAGE_FROM``.
        """
        rows = self.conn.execute(
            "SELECT l.id, l.name, l.folder, l.last_synced_at, l.removed_from_server_at, "
            "l.last_message_at, COUNT(m.id) AS message_count "
            "FROM lists l LEFT JOIN messages m ON m.list_id = l.id "
            "GROUP BY l.id ORDER BY l.name"
        ).fetchall()
        result = [dict(row) for row in rows]

        mix = self.conn.execute(
            "SELECT m.list_id AS list_id, s.label AS label, COUNT(*) AS count "
            "FROM messages m "
            "JOIN extractions e ON e.message_id = m.id "
            "JOIN scores s ON s.extraction_id = e.id "
            "GROUP BY m.list_id, s.label"
        ).fetchall()
        scored_by_list: dict[int, int] = {}
        labels_by_list: dict[int, dict[str, int]] = {}
        for row in mix:
            scored_by_list[row["list_id"]] = scored_by_list.get(row["list_id"], 0) + row["count"]
            if row["label"] is not None:
                labels_by_list.setdefault(row["list_id"], {})[row["label"]] = row["count"]

        for item in result:
            item["scored_count"] = scored_by_list.get(item["id"], 0)
            item["label_counts"] = labels_by_list.get(item["id"], {})
        return result

    def address_rows(self, q: str | None = None) -> list[dict[str, Any]]:
        """Every address with person + message count; ``q`` filters email/name.

        The substring match is case-insensitive over both the email and the
        display name.
        """
        sql = (
            "SELECT a.id, a.email, a.display_name, a.person_id, "
            "p.canonical_name AS person_name, COUNT(m.id) AS message_count "
            "FROM addresses a "
            "LEFT JOIN persons p ON p.id = a.person_id "
            "LEFT JOIN messages m ON m.address_id = a.id "
        )
        params: list[Any] = []
        if q:
            sql += "WHERE a.email LIKE ? OR a.display_name LIKE ? "
            like = f"%{q}%"
            params = [like, like]
        sql += "GROUP BY a.id ORDER BY a.email"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def person_rows(self) -> list[dict[str, Any]]:
        """Every person with its addresses and total message count (for /api/persons)."""
        persons = self.conn.execute(
            "SELECT p.id, p.canonical_name, COUNT(m.id) AS message_count "
            "FROM persons p "
            "LEFT JOIN addresses a ON a.person_id = p.id "
            "LEFT JOIN messages m ON m.address_id = a.id "
            "GROUP BY p.id ORDER BY p.canonical_name"
        ).fetchall()
        result: list[dict[str, Any]] = []
        for person in persons:
            addrs = self.conn.execute(
                "SELECT id, email, display_name FROM addresses WHERE person_id = ? ORDER BY email",
                (person["id"],),
            ).fetchall()
            result.append(
                {
                    "id": person["id"],
                    "canonical_name": person["canonical_name"],
                    "message_count": person["message_count"],
                    "addresses": [dict(a) for a in addrs],
                }
            )
        return result

    def sender_rows(
        self,
        *,
        q: str | None = None,
        sort: str = "count",
        order: str = "desc",
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        list_name: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return ``(rows, total)`` of senders for the Senders pane (/api/senders).

        A "sender" is either a **person** (a linked group of addresses) or an
        **unlinked address** (``addresses.person_id`` is NULL). Addresses attached
        to a person never appear as their own entry — they roll up into the person.

        The heavy lifting is two aggregate queries at ``(entity, label)``
        granularity — one over persons, one over unlinked addresses — mirroring
        the ``messages → extractions → scores`` join chain of ``_MESSAGE_FROM``.
        Each ``COUNT(m.id)`` counts messages (the extraction/score joins never
        multiply rows, both being UNIQUE), so summing the per-label counts gives
        the entity's ``message_count`` and the non-null label groups give its
        ``label_counts``. Senders with zero messages are included. The
        merge/filter/sort/paginate then happens in Python over the small result:

        - ``q`` — case-insensitive substring over the name or ANY email;
        - ``sort`` — ``"count"`` (by ``message_count``, secondary name asc for a
          stable order) or ``"name"`` (case-insensitive);
        - ``order`` — ``"asc"``/``"desc"``;
        - ``page``/``per_page`` — 1-based, ``per_page`` clamped to
          ``[1, MAX_PER_PAGE]``.

        ``total`` is the full match count before pagination.

        When ``list_name`` is given, message joins are restricted (via an extra
        ``AND m.list_id = ?`` inside the ``messages`` ON clause) so that
        ``message_count`` and ``label_counts`` reflect only that list, and senders
        who never posted to it (``message_count == 0``) are dropped from the
        result. An unknown ``list_name`` yields ``([], 0)``. When ``None`` (the
        default), all senders are included, zero-message ones among them.
        """
        list_filter = ""
        list_params: list[Any] = []
        if list_name is not None:
            list_row = self.conn.execute(
                "SELECT id FROM lists WHERE name = ?", (list_name,)
            ).fetchone()
            if list_row is None:
                return ([], 0)
            list_filter = " AND m.list_id = ?"
            list_params = [list_row["id"]]

        person_mix = self.conn.execute(
            "SELECT p.id AS person_id, p.canonical_name AS name, "
            "s.label AS label, COUNT(m.id) AS msg_count "
            "FROM persons p "
            "LEFT JOIN addresses a ON a.person_id = p.id "
            "LEFT JOIN messages m ON m.address_id = a.id" + list_filter + " "
            "LEFT JOIN extractions e ON e.message_id = m.id "
            "LEFT JOIN scores s ON s.extraction_id = e.id "
            "GROUP BY p.id, s.label",
            list_params,
        ).fetchall()
        person_addrs = self.conn.execute(
            "SELECT person_id, id, email FROM addresses WHERE person_id IS NOT NULL ORDER BY email"
        ).fetchall()
        unlinked_mix = self.conn.execute(
            "SELECT a.id AS address_id, a.email AS email, a.display_name AS display_name, "
            "s.label AS label, COUNT(m.id) AS msg_count "
            "FROM addresses a "
            "LEFT JOIN messages m ON m.address_id = a.id" + list_filter + " "
            "LEFT JOIN extractions e ON e.message_id = m.id "
            "LEFT JOIN scores s ON s.extraction_id = e.id "
            "WHERE a.person_id IS NULL "
            "GROUP BY a.id, s.label",
            list_params,
        ).fetchall()

        persons: dict[int, dict[str, Any]] = {}
        for row in person_mix:
            entry = persons.setdefault(
                row["person_id"],
                {
                    "type": "person",
                    "person_id": row["person_id"],
                    "name": row["name"],
                    "emails": [],
                    "address_ids": [],
                    "message_count": 0,
                    "label_counts": {},
                },
            )
            entry["message_count"] += row["msg_count"]
            if row["label"] is not None:
                entry["label_counts"][row["label"]] = row["msg_count"]
        for row in person_addrs:
            entry = persons.get(row["person_id"])
            if entry is not None:  # ordered by email, so emails come out sorted
                entry["emails"].append(row["email"])
                entry["address_ids"].append(row["id"])

        addresses: dict[int, dict[str, Any]] = {}
        for row in unlinked_mix:
            entry = addresses.setdefault(
                row["address_id"],
                {
                    "type": "address",
                    "address_id": row["address_id"],
                    "name": row["display_name"] or row["email"],
                    "emails": [row["email"]],
                    "message_count": 0,
                    "label_counts": {},
                },
            )
            entry["message_count"] += row["msg_count"]
            if row["label"] is not None:
                entry["label_counts"][row["label"]] = row["msg_count"]

        entries = [*persons.values(), *addresses.values()]

        if list_name is not None:
            # Scoped to a list: only senders who actually posted to it.
            entries = [e for e in entries if e["message_count"] > 0]

        if q:
            needle = q.strip().lower()
            entries = [
                e
                for e in entries
                if needle in e["name"].lower()
                or any(needle in email.lower() for email in e["emails"])
            ]

        # Python's sort is stable: apply the secondary key first, primary last.
        entries.sort(key=lambda e: e["name"].lower())
        if sort == "name":
            if order == "desc":
                entries.reverse()
        else:  # count
            entries.sort(key=lambda e: e["message_count"], reverse=(order != "asc"))

        total = len(entries)
        per_page = max(1, min(per_page, MAX_PER_PAGE))
        page = max(1, page)
        offset = (page - 1) * per_page
        return entries[offset : offset + per_page], total

    def db_size_bytes(self) -> int:
        """Size in bytes of the SQLite file backing this store, or 0 if none.

        In-memory stores (``path == ":memory:"``) and any path that is not present
        on disk report 0 rather than raising, so callers (e.g. the summary
        endpoint under an in-memory test store) get a number unconditionally. The
        main database file is measured; WAL/SHM sidecars are not counted.
        """
        if self.path == ":memory:":
            return 0
        try:
            return Path(self.path).stat().st_size
        except OSError:
            return 0

    # -- dashboard: filtered message query + summary --------------------------

    def query_messages(self, filters: MessageFilters) -> tuple[list[dict[str, Any]], int]:
        """Return ``(rows, total)`` for ``filters`` — the explorer's data source.

        ``rows`` is the requested page (each a dict joining messages + addresses +
        persons + extractions + scores); ``total`` is the full match count before
        pagination. ``per_page`` is clamped to ``[1, MAX_PER_PAGE]`` and ``page``
        to ``>= 1``. Unknown ``sort`` falls back to ``date``; ``order`` is ``asc``
        only when explicitly ``"asc"``, else ``desc``. A stable secondary sort on
        ``m.id`` makes pagination deterministic.
        """
        where, params = _build_message_where(filters)
        total = self.conn.execute(
            "SELECT COUNT(*) AS c" + _MESSAGE_FROM + where, params
        ).fetchone()["c"]

        sort_col = SORT_COLUMNS.get(filters.sort, SORT_COLUMNS["date"])
        order = "ASC" if str(filters.order).lower() == "asc" else "DESC"
        per_page = max(1, min(filters.per_page, MAX_PER_PAGE))
        page = max(1, filters.page)
        offset = (page - 1) * per_page

        sql = (
            "SELECT"
            + _MESSAGE_COLUMNS
            + _MESSAGE_FROM
            + where
            + f" ORDER BY {sort_col} {order}, m.id {order} LIMIT ? OFFSET ?"
        )
        rows = self.conn.execute(sql, [*params, per_page, offset]).fetchall()
        return [dict(row) for row in rows], total

    def summary(self, filters: MessageFilters) -> dict[str, Any]:
        """Aggregate the filtered message set for the overview page.

        Honours the same ``filters`` as :meth:`query_messages` (pagination/sort
        are ignored). ``extracted`` counts ``status='ok'`` extractions; ``scored``
        counts messages with a Pangram score; ``too_short`` counts gated ones.
        ``flagged`` means a label in :data:`FLAGGED_LABELS`.
        """
        where, params = _build_message_where(filters)
        base = _MESSAGE_FROM + where

        totals = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "COUNT(CASE WHEN e.status = 'ok' THEN 1 END) AS extracted, "
            "COUNT(s.id) AS scored, "
            "COUNT(CASE WHEN e.status = 'too_short' THEN 1 END) AS too_short, "
            "AVG(s.fraction_ai) AS avg_fraction_ai" + base,
            params,
        ).fetchone()

        label_rows = self.conn.execute(
            "SELECT s.label AS label, COUNT(*) AS count" + base + " GROUP BY s.label",
            params,
        ).fetchall()
        label_distribution = {
            row["label"]: row["count"] for row in label_rows if row["label"] is not None
        }

        list_label_rows = self.conn.execute(
            "SELECT l.name AS list, s.label AS label, COUNT(*) AS count"
            + base
            + " GROUP BY l.id, s.label",
            params,
        ).fetchall()
        list_label_counts: dict[str, dict[str, int]] = {}
        for row in list_label_rows:
            if row["label"] is not None:
                list_label_counts.setdefault(row["list"], {})[row["label"]] = row["count"]

        by_list = [
            dict(row)
            for row in self.conn.execute(
                "SELECT l.name AS list, COUNT(*) AS count, AVG(s.fraction_ai) AS avg_fraction_ai"
                + base
                + " GROUP BY l.id ORDER BY count DESC, l.name LIMIT 20",
                params,
            ).fetchall()
        ]
        for item in by_list:
            item["label_counts"] = list_label_counts.get(item["list"], {})

        addr_rows = self.conn.execute(
            "SELECT a.id AS address_id, a.email AS email, a.display_name AS display_name, "
            "a.person_id AS person_id, p.canonical_name AS person_name, "
            "COUNT(s.id) AS scored_count, "
            f"COUNT(CASE WHEN s.label IN {_FLAGGED_IN} THEN 1 END) AS flagged_count, "
            "AVG(s.fraction_ai) AS avg_fraction_ai"
            + base
            + " GROUP BY a.id HAVING scored_count > 0 "
            "ORDER BY scored_count DESC, flagged_count DESC, a.email LIMIT 20",
            params,
        ).fetchall()
        by_address = []
        for row in addr_rows:
            item = dict(row)
            scored = item["scored_count"] or 0
            item["flagged_share"] = (item["flagged_count"] / scored) if scored else 0.0
            by_address.append(item)

        month_rows = self.conn.execute(
            "SELECT substr(m.date, 1, 7) AS month, COUNT(*) AS count, "
            "AVG(s.fraction_ai) AS avg_fraction_ai, "
            f"COUNT(CASE WHEN s.label IN {_FLAGGED_IN} THEN 1 END) AS flagged_count"
            + base
            + " GROUP BY month ORDER BY month",
            params,
        ).fetchall()
        by_month = [dict(row) for row in month_rows if row["month"] is not None]

        return {
            "total": totals["total"],
            "extracted": totals["extracted"],
            "scored": totals["scored"],
            "too_short": totals["too_short"],
            "avg_fraction_ai": totals["avg_fraction_ai"],
            "label_distribution": label_distribution,
            "by_list": by_list,
            "by_address": by_address,
            "by_month": by_month,
            "db_size_bytes": self.db_size_bytes(),
        }
