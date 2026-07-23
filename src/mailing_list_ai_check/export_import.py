"""Portable export / import of a list's messages and their pipeline state.

Moves everything related to one or more mailing lists — the ``lists`` row, its
pull cursor, the sender ``addresses`` and ``persons`` groupings, the ``messages``
themselves, and the ``extractions`` / ``scores`` derived from them — between
SQLite databases as a single JSON Lines file, without ever corrupting the target
on re-import. The full format and semantics live in ``docs/export-import.md``;
this module is the authoritative implementation of that spec.

Two design choices keep the file small and the import safe:

- **Text pointers.** A message body is static, so an extraction's text is stored
  as a pointer into ``raw_body`` (whole body, or a character span) rather than a
  duplicated copy, falling back to an inline literal only when the text is not a
  contiguous substring of the body. Every extraction also carries the SHA-256 of
  its text; the importer reconstructs the text from the pointer and aborts on a
  hash mismatch, so a pointer that no longer resolves is caught as corruption.
- **One transaction.** Import runs as a single explicit transaction on the
  store's connection using raw SQL (not the per-call-committing :class:`Store`
  mutators), committed once at the end and rolled back on any error, so a
  truncated or malformed file can never leave a half-imported database.
  ``dry_run`` takes the identical code path and rolls back instead of committing.

Import is idempotent and collision-safe: a message already present in the target
(same Message-ID on the same list) is skipped along with its extraction/score,
so importing the same file twice — or into the database it came from — is a
no-op.
"""

from __future__ import annotations

import gzip
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .store import EXTRACTION_STATUSES, Store, sha256_text, version_key

log = logging.getLogger("mailing_list_ai_check.export_import")

#: On-disk format identifiers, written into the ``header`` record and checked on
#: import. Bump :data:`FORMAT_VERSION` only on an incompatible format change.
#: Version 2 added the header ``app_version`` and per-message ``pipeline_version``
#: fields; version-1 files are rejected (the format shipped unreleased, so none
#: exist in the wild).
FORMAT_NAME = "mlac-export"
FORMAT_VERSION = 2

#: Record ``type`` values in their fixed file order, mapped to a monotonic rank.
#: The importer requires records to appear in non-decreasing rank (``header``
#: first, ``trailer`` last), which guarantees every cross-reference is a
#: backward reference (a message's folder/email, an address's person) already
#: seen by the time it is needed.
_RECORD_RANK = {
    "header": 0,
    "list": 1,
    "pull_state": 1,
    "person": 2,
    "address": 3,
    "message": 4,
    "trailer": 5,
}


class ExportImportError(Exception):
    """Raised for any export/import validation failure (bad header, out-of-order
    record, forward reference, unknown type, invalid extraction status, hash
    mismatch, truncated file, or JSON parse error)."""


@dataclass(frozen=True)
class ExportSummary:
    """Tally of one :func:`export_lists` run."""

    lists: int
    messages: int
    extractions: int
    scores: int
    path: str

    def as_line(self) -> str:
        return (
            f"lists={self.lists} messages={self.messages} "
            f"extractions={self.extractions} scores={self.scores} path={self.path}"
        )


@dataclass(frozen=True)
class ImportSummary:
    """Tally of one :func:`import_file` run.

    ``*_created`` / ``*_inserted`` count rows actually written; ``messages_skipped``
    counts collision-guarded messages already present in the target (their
    embedded extraction/score are not imported); ``body_mismatches`` counts
    skipped messages whose stored ``raw_body`` differed from the file's (logged,
    never overwritten). ``extractions_updated`` / ``scores_updated`` count
    skipped messages whose derived state was refreshed from a later
    ``pipeline_version``; ``versions_bumped`` counts skipped messages whose
    derived state already matched a later-version file, so only their
    ``pipeline_version`` stamp was advanced (no other message column is ever
    modified). ``dry_run`` is true when the run rolled back instead of
    committing.
    """

    lists_created: int = 0
    lists_existing: int = 0
    pull_states_created: int = 0
    persons_created: int = 0
    addresses_upserted: int = 0
    messages_inserted: int = 0
    messages_skipped: int = 0
    body_mismatches: int = 0
    extractions_inserted: int = 0
    scores_inserted: int = 0
    extractions_updated: int = 0
    scores_updated: int = 0
    versions_bumped: int = 0
    dry_run: bool = False

    def as_line(self) -> str:
        return (
            f"lists_created={self.lists_created} lists_existing={self.lists_existing} "
            f"pull_states_created={self.pull_states_created} persons_created={self.persons_created} "
            f"addresses_upserted={self.addresses_upserted} "
            f"messages_inserted={self.messages_inserted} messages_skipped={self.messages_skipped} "
            f"body_mismatches={self.body_mismatches} "
            f"extractions_inserted={self.extractions_inserted} scores_inserted={self.scores_inserted} "
            f"extractions_updated={self.extractions_updated} scores_updated={self.scores_updated} "
            f"versions_bumped={self.versions_bumped} "
            f"dry_run={self.dry_run}"
        )


# --- Helpers ------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Current time as a UTC ISO-8601 string (second precision)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _open_text(path: str | Path, mode: str):
    """Open ``path`` for text I/O, transparently gzip-compressed when it ends ``.gz``."""
    opener = gzip.open if str(path).endswith(".gz") else open
    return opener(path, mode, encoding="utf-8")


def _text_pointer(extracted_text: str, raw_body: str | None) -> dict[str, Any]:
    """Choose the smallest faithful pointer for ``extracted_text`` in ``raw_body``.

    In priority order: ``full_body`` when the text is the whole body, ``span``
    when it occurs verbatim (first ``str.find`` offset), else an ``inline``
    literal (also the case when ``raw_body`` is null).
    """
    if raw_body is not None and extracted_text == raw_body:
        return {"kind": "full_body"}
    if raw_body is not None:
        start = raw_body.find(extracted_text)
        if start != -1:
            return {"kind": "span", "start": start, "length": len(extracted_text)}
    return {"kind": "inline", "value": extracted_text}


def _resolve_pointer(pointer: Any, raw_body: str | None) -> str:
    """Reconstruct extracted text from its ``pointer`` and the message ``raw_body``.

    Inverse of :func:`_text_pointer`. Raises :class:`ExportImportError` on a
    malformed pointer or one that cannot resolve against ``raw_body``.
    """
    if not isinstance(pointer, dict):
        raise ExportImportError(f"malformed extraction text pointer: {pointer!r}")
    kind = pointer.get("kind")
    if kind == "full_body":
        if raw_body is None:
            raise ExportImportError("full_body text pointer but message has no raw_body")
        return raw_body
    if kind == "span":
        start, length = pointer.get("start"), pointer.get("length")
        if not isinstance(start, int) or not isinstance(length, int):
            raise ExportImportError(f"malformed span pointer: {pointer!r}")
        if raw_body is None:
            raise ExportImportError("span text pointer but message has no raw_body")
        return raw_body[start : start + length]
    if kind == "inline":
        value = pointer.get("value")
        if not isinstance(value, str):
            raise ExportImportError(f"malformed inline pointer: {pointer!r}")
        return value
    raise ExportImportError(f"unknown extraction text pointer kind: {kind!r}")


# --- Export -------------------------------------------------------------------


def export_lists(
    store: Store,
    list_names: Sequence[str] | None,
    out_path: str | Path,
    *,
    all_lists: bool = False,
) -> ExportSummary:
    """Export one or more lists and everything derived from their messages.

    Lists are chosen by ``lists.name`` (a name may match several rows — every
    match is exported, keyed by ``folder``) or, with ``all_lists=True``, every
    list that has at least one message. Passing both ``list_names`` and
    ``all_lists`` — or neither — is a :class:`ValueError`, as is an unknown name.

    Only persons/addresses actually referenced by the exported messages are
    written, each once (deduplicated across the whole file). Records are emitted
    in the fixed order ``header`` → (``list``, optional ``pull_state``) per list
    → ``person``s → ``address``es → ``message``s (extraction and score embedded)
    → ``trailer``, and the file is gzip-compressed when ``out_path`` ends ``.gz``.

    Purely a local database read: no IMAP, no Pangram, no caps involved.
    """
    has_names = list_names is not None and len(list_names) > 0
    if all_lists and has_names:
        raise ValueError("give either list names or all_lists=True, not both")
    if not all_lists and not has_names:
        raise ValueError("give one or more list names, or all_lists=True")

    conn = store.conn

    # Resolve the selected list rows, deduplicated by folder in a stable order.
    selected: list[Any] = []
    seen_folders: set[str] = set()
    if all_lists:
        rows = conn.execute(
            "SELECT * FROM lists l "
            "WHERE EXISTS (SELECT 1 FROM messages m WHERE m.list_id = l.id) "
            "ORDER BY l.folder"
        ).fetchall()
        for row in rows:
            selected.append(row)
            seen_folders.add(row["folder"])
    else:
        assert list_names is not None
        for name in list_names:
            matches = conn.execute(
                "SELECT * FROM lists WHERE name = ? ORDER BY folder", (name,)
            ).fetchall()
            if not matches:
                raise ValueError(f"unknown list name: {name!r}")
            for row in matches:
                if row["folder"] not in seen_folders:
                    seen_folders.add(row["folder"])
                    selected.append(row)

    # Gather message rows per list, collecting referenced addresses as we go so
    # persons/addresses can be emitted (once each) ahead of the messages.
    message_records: list[dict[str, Any]] = []
    referenced_address_ids: list[int] = []
    seen_address_ids: set[int] = set()
    n_extractions = 0
    n_scores = 0

    for lst in selected:
        msg_rows = conn.execute(
            "SELECT * FROM messages WHERE list_id = ? ORDER BY id", (lst["id"],)
        ).fetchall()
        for m in msg_rows:
            email = None
            if m["address_id"] is not None:
                if m["address_id"] not in seen_address_ids:
                    seen_address_ids.add(m["address_id"])
                    referenced_address_ids.append(m["address_id"])
                addr = conn.execute(
                    "SELECT email FROM addresses WHERE id = ?", (m["address_id"],)
                ).fetchone()
                email = addr["email"] if addr else None

            extraction_obj: dict[str, Any] | None = None
            ext = conn.execute(
                "SELECT * FROM extractions WHERE message_id = ?", (m["id"],)
            ).fetchone()
            if ext is not None:
                n_extractions += 1
                score_obj: dict[str, Any] | None = None
                sc = conn.execute(
                    "SELECT * FROM scores WHERE extraction_id = ?", (ext["id"],)
                ).fetchone()
                if sc is not None:
                    n_scores += 1
                    score_obj = {
                        "fraction_ai": sc["fraction_ai"],
                        "fraction_ai_assisted": sc["fraction_ai_assisted"],
                        "fraction_human": sc["fraction_human"],
                        "label": sc["label"],
                        "detector_version": sc["detector_version"],
                        "raw_response": sc["raw_response"],
                        "text_sha256": sc["text_sha256"],
                        "scored_at": sc["scored_at"],
                    }
                extraction_obj = {
                    "method": ext["method"],
                    "char_count": ext["char_count"],
                    "status": ext["status"],
                    "created_at": ext["created_at"],
                    "text": _text_pointer(ext["extracted_text"], m["raw_body"]),
                    "sha256": sha256_text(ext["extracted_text"]),
                    "score": score_obj,
                }

            message_records.append(
                {
                    "type": "message",
                    "folder": lst["folder"],
                    "message_id": m["message_id"],
                    "email": email,
                    "subject": m["subject"],
                    "date": m["date"],
                    "in_reply_to": m["in_reply_to"],
                    "raw_body": m["raw_body"],
                    "raw_html": m["raw_html"],
                    "uid": m["uid"],
                    "fetched_at": m["fetched_at"],
                    "pipeline_version": m["pipeline_version"],
                    "extraction": extraction_obj,
                }
            )

    # Resolve the referenced addresses and, through them, the persons to emit.
    address_records: list[dict[str, Any]] = []
    person_ids: list[int] = []
    seen_person_ids: set[int] = set()
    for address_id in referenced_address_ids:
        a = conn.execute("SELECT * FROM addresses WHERE id = ?", (address_id,)).fetchone()
        if a is None:  # pragma: no cover - address_id came from a live FK
            continue
        person_key = None
        if a["person_id"] is not None:
            person_key = f"p{a['person_id']}"
            if a["person_id"] not in seen_person_ids:
                seen_person_ids.add(a["person_id"])
                person_ids.append(a["person_id"])
        address_records.append(
            {
                "type": "address",
                "email": a["email"],
                "display_name": a["display_name"],
                "person_key": person_key,
            }
        )

    person_records: list[dict[str, Any]] = []
    for person_id in person_ids:
        p = conn.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()
        if p is None:  # pragma: no cover - person_id came from a live FK
            continue
        person_records.append(
            {
                "type": "person",
                "person_key": f"p{person_id}",
                "canonical_name": p["canonical_name"],
            }
        )

    schema_row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    schema_version = schema_row["v"] if schema_row and schema_row["v"] is not None else 0
    folders = [lst["folder"] for lst in selected]

    with _open_text(out_path, "wt") as fh:

        def emit(record: dict[str, Any]) -> None:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        emit(
            {
                "type": "header",
                "format": FORMAT_NAME,
                "format_version": FORMAT_VERSION,
                "app_version": __version__,
                "exported_at": _utcnow_iso(),
                "schema_version": schema_version,
                "folders": folders,
            }
        )
        for lst in selected:
            emit(
                {
                    "type": "list",
                    "name": lst["name"],
                    "folder": lst["folder"],
                    "last_synced_at": lst["last_synced_at"],
                    "removed_from_server_at": lst["removed_from_server_at"],
                    "last_message_at": lst["last_message_at"],
                }
            )
            ps = conn.execute("SELECT * FROM pull_state WHERE list_id = ?", (lst["id"],)).fetchone()
            if ps is not None:
                emit(
                    {
                        "type": "pull_state",
                        "folder": lst["folder"],
                        "uidvalidity": ps["uidvalidity"],
                        "last_uid": ps["last_uid"],
                    }
                )
        for record in person_records:
            emit(record)
        for record in address_records:
            emit(record)
        for record in message_records:
            emit(record)
        emit(
            {
                "type": "trailer",
                "lists": len(selected),
                "messages": len(message_records),
                "extractions": n_extractions,
                "scores": n_scores,
            }
        )

    summary = ExportSummary(
        lists=len(selected),
        messages=len(message_records),
        extractions=n_extractions,
        scores=n_scores,
        path=str(out_path),
    )
    return summary


# --- Import -------------------------------------------------------------------


class _Importer:
    """One import pass over a stream, accumulating state and summary counters.

    Isolated in a class so the per-record handlers can share the folder/email/
    person lookup maps built as records stream by. All writes go through raw SQL
    on ``store.conn`` inside a single transaction owned by :func:`import_file`.
    """

    def __init__(self, store: Store) -> None:
        self.conn = store.conn

        # File-scoped resolution maps (all references are backward references).
        self.folder_to_list_id: dict[str, int] = {}
        self.person_meta: dict[str, str] = {}  # person_key -> canonical_name
        self.group_person_id: dict[str, int] = {}  # person_key -> resolved person id
        self.seen_emails: set[str] = set()

        # Ordering / structure state.
        self.records_seen = 0
        self.max_rank = -1
        self.header_seen = False
        self.trailer_seen = False

        # Trailer-verification counters (records present in the file).
        self.file_lists = 0
        self.file_messages = 0
        self.file_extractions = 0
        self.file_scores = 0

        # Summary counters.
        self.lists_created = 0
        self.lists_existing = 0
        self.pull_states_created = 0
        self.persons_created = 0
        self.addresses_upserted = 0
        self.messages_inserted = 0
        self.messages_skipped = 0
        self.body_mismatches = 0
        self.extractions_inserted = 0
        self.scores_inserted = 0
        self.extractions_updated = 0
        self.scores_updated = 0
        self.versions_bumped = 0

    # -- dispatch -------------------------------------------------------------

    def handle(self, record: Any, lineno: int) -> None:
        if not isinstance(record, dict):
            raise ExportImportError(f"line {lineno}: record is not a JSON object")
        rtype = record.get("type")
        if rtype not in _RECORD_RANK:
            raise ExportImportError(f"line {lineno}: unknown record type {rtype!r}")

        if self.trailer_seen:
            raise ExportImportError(f"line {lineno}: record after trailer")

        rank = _RECORD_RANK[rtype]
        if self.records_seen == 0:
            if rtype != "header":
                raise ExportImportError(f"line {lineno}: first record must be a header")
        else:
            if rtype == "header":
                raise ExportImportError(f"line {lineno}: duplicate header")
            if rank < self.max_rank:
                raise ExportImportError(f"line {lineno}: {rtype!r} record out of order")
        self.max_rank = max(self.max_rank, rank)
        self.records_seen += 1

        handler = getattr(self, f"_handle_{rtype}")
        handler(record, lineno)

    def finish(self) -> None:
        if not self.header_seen:
            raise ExportImportError("missing header record")
        if not self.trailer_seen:
            raise ExportImportError("missing trailer record (file truncated?)")

    # -- per-record handlers --------------------------------------------------

    def _handle_header(self, record: dict[str, Any], lineno: int) -> None:
        if record.get("format") != FORMAT_NAME:
            raise ExportImportError(
                f"line {lineno}: unexpected format {record.get('format')!r} "
                f"(expected {FORMAT_NAME!r})"
            )
        if record.get("format_version") != FORMAT_VERSION:
            raise ExportImportError(
                f"line {lineno}: unsupported format_version {record.get('format_version')!r} "
                f"(expected {FORMAT_VERSION})"
            )
        self.header_seen = True

    def _handle_list(self, record: dict[str, Any], lineno: int) -> None:
        self.file_lists += 1
        folder = record["folder"]
        existing = self.conn.execute("SELECT id FROM lists WHERE folder = ?", (folder,)).fetchone()
        if existing is not None:
            # An existing row is authoritative; its metadata is not overwritten.
            self.folder_to_list_id[folder] = existing["id"]
            self.lists_existing += 1
            return
        cur = self.conn.execute(
            "INSERT INTO lists(name, folder, last_synced_at, removed_from_server_at, "
            "last_message_at) VALUES (?, ?, ?, ?, ?)",
            (
                record["name"],
                folder,
                record.get("last_synced_at"),
                record.get("removed_from_server_at"),
                record.get("last_message_at"),
            ),
        )
        self.folder_to_list_id[folder] = cur.lastrowid
        self.lists_created += 1

    def _handle_pull_state(self, record: dict[str, Any], lineno: int) -> None:
        folder = record["folder"]
        list_id = self.folder_to_list_id.get(folder)
        if list_id is None:
            raise ExportImportError(f"line {lineno}: pull_state for unseen folder {folder!r}")
        # An existing cursor reflects the target's own sync state and always wins.
        existing = self.conn.execute(
            "SELECT 1 FROM pull_state WHERE list_id = ?", (list_id,)
        ).fetchone()
        if existing is not None:
            return
        self.conn.execute(
            "INSERT INTO pull_state(list_id, uidvalidity, last_uid) VALUES (?, ?, ?)",
            (list_id, record["uidvalidity"], record["last_uid"]),
        )
        self.pull_states_created += 1

    def _handle_person(self, record: dict[str, Any], lineno: int) -> None:
        person_key = record.get("person_key")
        if not isinstance(person_key, str):
            raise ExportImportError(f"line {lineno}: person record missing person_key")
        # Registered only; persons are created lazily, when an address needs one.
        self.person_meta[person_key] = record["canonical_name"]

    def _handle_address(self, record: dict[str, Any], lineno: int) -> None:
        self.addresses_upserted += 1
        email = record["email"].strip().lower()
        self.seen_emails.add(email)
        # Same display-name backfill semantics as Store.upsert_address.
        self.conn.execute(
            "INSERT INTO addresses(email, display_name) VALUES (?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "display_name = COALESCE(NULLIF(addresses.display_name, ''), excluded.display_name)",
            (email, record.get("display_name")),
        )
        row = self.conn.execute(
            "SELECT id, person_id FROM addresses WHERE email = ?", (email,)
        ).fetchone()

        person_key = record.get("person_key")
        if person_key is None:
            return
        if person_key not in self.person_meta:
            raise ExportImportError(
                f"line {lineno}: address references unseen person_key {person_key!r}"
            )

        if row["person_id"] is not None:
            # Already linked in the target: keep it, and let it recruit the group
            # so later unlinked members of the same group join this person.
            self.group_person_id.setdefault(person_key, row["person_id"])
            return

        target_id = self.group_person_id.get(person_key)
        if target_id is None:
            cur = self.conn.execute(
                "INSERT INTO persons(canonical_name) VALUES (?)",
                (self.person_meta[person_key],),
            )
            target_id = cur.lastrowid
            self.group_person_id[person_key] = target_id
            self.persons_created += 1
        self.conn.execute("UPDATE addresses SET person_id = ? WHERE id = ?", (target_id, row["id"]))

    def _handle_message(self, record: dict[str, Any], lineno: int) -> None:
        self.file_messages += 1
        folder = record["folder"]
        list_id = self.folder_to_list_id.get(folder)
        if list_id is None:
            raise ExportImportError(f"line {lineno}: message references unseen folder {folder!r}")

        extraction = record.get("extraction")
        if extraction is not None:
            self.file_extractions += 1
            if extraction.get("score") is not None:
                self.file_scores += 1

        address_id = None
        email = record.get("email")
        if email is not None:
            normalized = email.strip().lower()
            if normalized not in self.seen_emails:
                raise ExportImportError(f"line {lineno}: message references unseen email {email!r}")
            addr = self.conn.execute(
                "SELECT id FROM addresses WHERE email = ?", (normalized,)
            ).fetchone()
            address_id = addr["id"] if addr else None

        raw_body = record.get("raw_body")
        cur = self.conn.execute(
            "INSERT INTO messages("
            "message_id, list_id, address_id, subject, date, in_reply_to, raw_body, uid, "
            "fetched_at, raw_html, pipeline_version"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(list_id, message_id) DO NOTHING",
            (
                record["message_id"],
                list_id,
                address_id,
                record.get("subject"),
                record.get("date"),
                record.get("in_reply_to"),
                raw_body,
                record.get("uid"),
                record["fetched_at"],
                record.get("raw_html"),
                record.get("pipeline_version"),
            ),
        )

        if cur.rowcount == 0:
            # Collision: the message already exists. Its own row is never
            # modified. Its embedded extraction/score are not imported either,
            # unless the file carries a later pipeline_version (see below).
            self.messages_skipped += 1
            existing = self.conn.execute(
                "SELECT id, raw_body, pipeline_version "
                "FROM messages WHERE list_id = ? AND message_id = ?",
                (list_id, record["message_id"]),
            ).fetchone()
            if existing is None:  # pragma: no cover - conflict implies a row exists
                return
            if existing["raw_body"] != raw_body:
                self.body_mismatches += 1
                # Identifiers only — never the body itself.
                log.warning(
                    "raw_body mismatch for existing message (folder=%s message_id=%s); "
                    "keeping stored copy",
                    folder,
                    record["message_id"],
                )
            # Version-aware refresh: only when the file's pipeline_version is
            # strictly later (tuple comparison, NULL oldest). Differing derived
            # data is replaced; identical derived data means the later pipeline
            # validated what the target already holds, so only the version stamp
            # is adopted — the data is correct as of that newer version.
            file_version = record.get("pipeline_version")
            if version_key(file_version) > version_key(existing["pipeline_version"]):
                if self._derived_matches(existing["id"], extraction):
                    self.conn.execute(
                        "UPDATE messages SET pipeline_version = ? WHERE id = ?",
                        (file_version, existing["id"]),
                    )
                    self.versions_bumped += 1
                else:
                    self._refresh_derived(
                        existing["id"], extraction, raw_body, file_version, lineno
                    )
            return

        self.messages_inserted += 1
        if extraction is not None:
            self._insert_extraction(cur.lastrowid, extraction, raw_body, lineno)

    def _write_extraction(
        self, message_pk: int, extraction: dict[str, Any], raw_body: str | None, lineno: int
    ) -> bool:
        """Insert one extraction (and its score when present) for ``message_pk``.

        Reconstructs the extracted text from its pointer, verifies it against the
        stored ``sha256``, and writes the ``extractions`` row plus an embedded
        ``scores`` row if any. Returns ``True`` when a score row was inserted.
        Touches no summary counters — the caller records inserted/updated counts.
        """
        status = extraction.get("status")
        if status not in EXTRACTION_STATUSES:
            raise ExportImportError(
                f"line {lineno}: invalid extraction status {status!r}; "
                f"expected one of {EXTRACTION_STATUSES}"
            )
        text = _resolve_pointer(extraction.get("text"), raw_body)
        expected = extraction.get("sha256")
        actual = sha256_text(text)
        if actual != expected:
            raise ExportImportError(
                f"line {lineno}: extraction text hash mismatch "
                f"(expected {expected!r}, got {actual!r}) — file corrupt"
            )
        char_count = extraction.get("char_count")
        if char_count is None:
            char_count = len(text)
        cur = self.conn.execute(
            "INSERT INTO extractions("
            "message_id, extracted_text, method, char_count, status, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                message_pk,
                text,
                extraction["method"],
                char_count,
                status,
                extraction["created_at"],
            ),
        )

        score = extraction.get("score")
        if score is not None:
            self.conn.execute(
                "INSERT INTO scores("
                "extraction_id, fraction_ai, fraction_ai_assisted, fraction_human, "
                "label, detector_version, raw_response, text_sha256, scored_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cur.lastrowid,
                    score.get("fraction_ai"),
                    score.get("fraction_ai_assisted"),
                    score.get("fraction_human"),
                    score.get("label"),
                    score.get("detector_version"),
                    score.get("raw_response"),
                    score["text_sha256"],
                    score["scored_at"],
                ),
            )
            return True
        return False

    def _insert_extraction(
        self, message_pk: int, extraction: dict[str, Any], raw_body: str | None, lineno: int
    ) -> None:
        """Insert the extraction (and score) for a freshly inserted message,
        counting them under ``extractions_inserted`` / ``scores_inserted``."""
        score_inserted = self._write_extraction(message_pk, extraction, raw_body, lineno)
        self.extractions_inserted += 1
        if score_inserted:
            self.scores_inserted += 1

    def _derived_matches(self, message_pk: int, extraction: dict[str, Any] | None) -> bool:
        """Return whether the target's derived state already equals the file's.

        Compares the existing ``extractions`` / ``scores`` rows for ``message_pk``
        against the file's embedded ``extraction`` (which may be ``None``).
        Presence/absence on either side counts as a difference; otherwise the
        extraction is compared on text ``sha256``/``method``/``status``/
        ``char_count`` and the score on ``text_sha256``, the three fractions,
        ``label``, ``detector_version`` and ``raw_response``.
        """
        target_ext = self.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (message_pk,)
        ).fetchone()
        if extraction is None:
            return target_ext is None
        if target_ext is None:
            return False
        if sha256_text(target_ext["extracted_text"]) != extraction.get("sha256"):
            return False
        if target_ext["method"] != extraction.get("method"):
            return False
        if target_ext["status"] != extraction.get("status"):
            return False
        file_char = extraction.get("char_count")
        if file_char is not None and target_ext["char_count"] != file_char:
            return False

        target_score = self.conn.execute(
            "SELECT * FROM scores WHERE extraction_id = ?", (target_ext["id"],)
        ).fetchone()
        file_score = extraction.get("score")
        if (target_score is None) != (file_score is None):
            return False
        if file_score is not None:
            for column, key in (
                ("text_sha256", "text_sha256"),
                ("fraction_ai", "fraction_ai"),
                ("fraction_ai_assisted", "fraction_ai_assisted"),
                ("fraction_human", "fraction_human"),
                ("label", "label"),
                ("detector_version", "detector_version"),
                ("raw_response", "raw_response"),
            ):
                if target_score[column] != file_score.get(key):
                    return False
        return True

    def _refresh_derived(
        self,
        message_pk: int,
        extraction: dict[str, Any] | None,
        raw_body: str | None,
        file_version: str | None,
        lineno: int,
    ) -> None:
        """Replace the target's derived state with the file's (message untouched).

        Deletes the existing extraction (its score cascades away), inserts the
        file's extraction and score when present — a ``None`` extraction just
        clears the old derived data — and stamps the message's
        ``pipeline_version`` with the file's later value. Records
        ``extractions_updated`` once, and ``scores_updated`` when the score state
        changed (a score was inserted or an existing one removed).
        """
        target_ext = self.conn.execute(
            "SELECT id FROM extractions WHERE message_id = ?", (message_pk,)
        ).fetchone()
        target_had_score = target_ext is not None and (
            self.conn.execute(
                "SELECT 1 FROM scores WHERE extraction_id = ?", (target_ext["id"],)
            ).fetchone()
            is not None
        )

        self.conn.execute("DELETE FROM extractions WHERE message_id = ?", (message_pk,))
        file_has_score = False
        if extraction is not None:
            file_has_score = self._write_extraction(message_pk, extraction, raw_body, lineno)

        self.extractions_updated += 1
        if file_has_score or target_had_score:
            self.scores_updated += 1

        self.conn.execute(
            "UPDATE messages SET pipeline_version = ? WHERE id = ?",
            (file_version, message_pk),
        )

    def _handle_trailer(self, record: dict[str, Any], lineno: int) -> None:
        expected = {
            "lists": self.file_lists,
            "messages": self.file_messages,
            "extractions": self.file_extractions,
            "scores": self.file_scores,
        }
        for key, count in expected.items():
            if record.get(key) != count:
                raise ExportImportError(
                    f"line {lineno}: trailer {key}={record.get(key)!r} does not match "
                    f"{count} record(s) seen — file truncated or corrupt"
                )
        self.trailer_seen = True

    def summary(self, *, dry_run: bool) -> ImportSummary:
        return ImportSummary(
            lists_created=self.lists_created,
            lists_existing=self.lists_existing,
            pull_states_created=self.pull_states_created,
            persons_created=self.persons_created,
            addresses_upserted=self.addresses_upserted,
            messages_inserted=self.messages_inserted,
            messages_skipped=self.messages_skipped,
            body_mismatches=self.body_mismatches,
            extractions_inserted=self.extractions_inserted,
            scores_inserted=self.scores_inserted,
            extractions_updated=self.extractions_updated,
            scores_updated=self.scores_updated,
            versions_bumped=self.versions_bumped,
            dry_run=dry_run,
        )


def import_file(
    store: Store,
    in_path: str | Path,
    *,
    dry_run: bool = False,
) -> ImportSummary:
    """Import an export file into ``store`` as one all-or-nothing transaction.

    Streams the JSON Lines file once (gzip-decompressed when it ends ``.gz``),
    validating structure — header first with a matching format/version, records
    in their fixed order, no forward references, valid extraction statuses,
    extraction hashes that resolve, and a trailer whose counts match the records
    seen. Messages colliding on ``(list_id, message_id)`` are skipped (their
    extraction/score with them); everything else is inserted with raw SQL.

    The whole pass runs inside a single explicit transaction on ``store.conn``,
    committed once on success and rolled back on any error (which is re-raised).
    ``dry_run`` takes the identical path and rolls back instead of committing, so
    its returned :class:`ImportSummary` (with ``dry_run=True``) is exact. Any
    validation failure raises :class:`ExportImportError`.
    """
    conn = store.conn
    importer = _Importer(store)

    conn.execute("BEGIN")
    try:
        with _open_text(in_path, "rt") as fh:
            for lineno, line in enumerate(fh, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ExportImportError(f"line {lineno}: invalid JSON: {exc}") from exc
                importer.handle(record, lineno)
        importer.finish()
    except Exception:
        conn.rollback()
        raise

    if dry_run:
        conn.rollback()
    else:
        conn.commit()

    return importer.summary(dry_run=dry_run)
