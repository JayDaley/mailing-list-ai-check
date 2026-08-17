"""One-way export of scores and message metadata for statistical analysis.

Writes a zip archive of CSV files — one row per message, plus per-list and
per-sender aggregates, a manifest and a data dictionary — for analysis outside
the app, in a spreadsheet, pandas or R. The full format and semantics live in
``docs/stats-export.md``; this module is the authoritative implementation of
that spec. It complements :mod:`.export_import`, which moves complete databases
between installs: nothing here is ever read back by the app, and the archive
carries no message content — no bodies, extracted text, subjects, raw headers or
detector responses.

Four properties follow from that purpose:

- **Denominators, not only hits.** Every message in scope is exported, scored or
  not, because a share calculation needs the messages that were never scored and
  those gated under the reliability floor. ``lists.csv`` and ``senders.csv`` are
  aggregates over the identical scope, so their counts sum to ``messages.csv``
  and an analyst can verify one against the other.
- **Open everywhere.** Zip and CSV rather than the full export's zstd JSON
  Lines: the audience is analysis tools, not this app, and the archive bundles
  its own data dictionary with the data.
- **Two identity variants.** An identified export carries sender addresses,
  names and Message-IDs; a pseudonymous one omits those columns outright — not
  blanked, so a file's header row states what it holds — and numbers senders
  ``s1``, ``s2``, … in first-seen order. Keys are sequential rather than hashed,
  because a hash of a known address is reversible by dictionary.
- **The message pass streams.** Rows are written into the zip member one at a
  time (:meth:`zipfile.ZipFile.open` in write mode), so no message row is held
  beyond the one being written. The pre-pass that precedes it holds only what
  cross-references need: the sender of each referenced address, and the key of
  each message in scope so a reply can name its parent.

Purely a local database read: no IMAP, no Pangram, no caps involved.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .export_import import _range_clause, _range_params, _select_lists
from .store import TIMING_IMPLAUSIBLE_CPM, TIMING_SUSPICIOUS_CPM, Store, _parent_message_id

#: Format identifiers written into ``manifest.json``. :data:`STATS_FORMAT_VERSION`
#: is this format's own number, independent of the app version and of the full
#: export's ``FORMAT_VERSION``. Nothing reads the file back, so the version exists
#: for analysts and their scripts rather than for rejection logic: an added
#: column does not bump it, a removed or re-defined one does.
STATS_FORMAT_NAME = "mlac-stats"
STATS_FORMAT_VERSION = 1

#: Suffix of the archive, appended to the caller's path unless already present.
ZIP_SUFFIX = ".zip"

#: The five archive members, in the order they are written.
MESSAGES_MEMBER = "messages.csv"
LISTS_MEMBER = "lists.csv"
SENDERS_MEMBER = "senders.csv"
MANIFEST_MEMBER = "manifest.json"
README_MEMBER = "README.md"

#: Pangram ``prediction_short`` values, which ``scores.label`` stores verbatim,
#: and the reply-timing bands — both listed in the manifest so a consumer knows
#: the closed sets behind the ``label`` and ``timing`` columns without having to
#: infer them from the rows present.
LABELS = ("Human", "Mixed", "AI")
TIMING_BANDS = ("normal", "suspicious", "implausible")

#: ``messages.csv`` columns in file order. The identity columns are dropped from
#: a pseudonymous export (see :data:`_MESSAGE_IDENTITY_COLUMNS`); column order is
#: part of the format.
_MESSAGE_COLUMNS = (
    "message_key",
    "list",
    "folder",
    "date",
    "sender_key",
    "email",
    "sender_name",
    "is_reply",
    "parent_key",
    "message_id",
    "in_reply_to",
    "auto_generated",
    "timing",
    "timing_cpm",
    "extraction_status",
    "extraction_method",
    "extraction_chars",
    "extraction_version",
    "pipeline_version",
    "label",
    "fraction_ai",
    "fraction_ai_assisted",
    "fraction_human",
    "detector_version",
    "scored_at",
)
_MESSAGE_IDENTITY_COLUMNS = frozenset({"email", "sender_name", "message_id", "in_reply_to"})

#: ``lists.csv`` columns in file order.
_LIST_COLUMNS = (
    "list",
    "folder",
    "messages",
    "scored",
    "too_short",
    "human",
    "mixed",
    "ai",
    "ai_share",
    "first_date",
    "last_date",
)

#: ``senders.csv`` columns in file order, with the same identity rule.
_SENDER_COLUMNS = (
    "sender_key",
    "sender_type",
    "name",
    "emails",
    "messages",
    "scored",
    "too_short",
    "human",
    "mixed",
    "ai",
    "ai_share",
    "first_date",
    "last_date",
)
_SENDER_IDENTITY_COLUMNS = frozenset({"name", "emails"})

#: The join chain every query in this module walks: a message, its extraction if
#: it has one, and that extraction's score if it has one. Both joins are on
#: UNIQUE columns, so neither multiplies rows and ``COUNT(*)`` counts messages.
_MESSAGE_JOINS = (
    " FROM messages m "
    "LEFT JOIN extractions e ON e.message_id = m.id "
    "LEFT JOIN scores sc ON sc.extraction_id = e.id"
)

#: The aggregate expressions shared by ``lists.csv`` and ``senders.csv``, so the
#: two tables count the same things over the same scope. The label literals are
#: the constants in :data:`LABELS` (no user input), so inlining them is safe.
_AGGREGATE_COLUMNS = (
    "COUNT(*) AS messages, "
    "COUNT(sc.id) AS scored, "
    "COUNT(CASE WHEN e.status = 'too_short' THEN 1 END) AS too_short, "
    "COUNT(CASE WHEN sc.label = 'Human' THEN 1 END) AS human, "
    "COUNT(CASE WHEN sc.label = 'Mixed' THEN 1 END) AS mixed, "
    "COUNT(CASE WHEN sc.label = 'AI' THEN 1 END) AS ai, "
    "MIN(m.date) AS first_date, MAX(m.date) AS last_date"
)

#: The per-message columns, named so the streaming pass reads them by name.
_MESSAGE_SELECT = (
    "SELECT m.id AS id, m.message_id AS message_id, m.date AS date, "
    "m.in_reply_to AS in_reply_to, m.address_id AS address_id, m.from_name AS from_name, "
    "m.auto_generated AS auto_generated, m.timing AS timing, m.timing_cpm AS timing_cpm, "
    "m.pipeline_version AS pipeline_version, "
    "e.status AS extraction_status, e.method AS extraction_method, "
    "e.char_count AS extraction_chars, e.extraction_version AS extraction_version, "
    "sc.id AS score_id, sc.label AS label, sc.fraction_ai AS fraction_ai, "
    "sc.fraction_ai_assisted AS fraction_ai_assisted, sc.fraction_human AS fraction_human, "
    "sc.detector_version AS detector_version, sc.scored_at AS scored_at"
)


@dataclass(frozen=True)
class StatsExportSummary:
    """Tally of one :func:`export_stats` run."""

    lists: int
    senders: int
    messages: int
    scored: int
    path: str

    def as_line(self) -> str:
        return (
            f"lists={self.lists} senders={self.senders} messages={self.messages} "
            f"scored={self.scored} path={self.path}"
        )


# --- Helpers ------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Current time as a UTC ISO-8601 string (second precision)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def zip_path(path: str | Path) -> Path:
    """Return ``path`` with :data:`ZIP_SUFFIX` appended, idempotently.

    A path already ending ``.zip`` is returned unchanged, so a caller that passes
    ``stats.zip`` does not end up with ``stats.zip.zip``.
    """
    p = Path(path)
    if p.name.endswith(ZIP_SUFFIX):
        return p
    return p.with_name(p.name + ZIP_SUFFIX)


def _columns(columns: Sequence[str], identity: frozenset[str], identified: bool) -> list[str]:
    """``columns`` less the identity ones when the export is pseudonymous."""
    if identified:
        return list(columns)
    return [column for column in columns if column not in identity]


def _cell(value: Any) -> Any:
    """One CSV field: NULL as an empty field, a boolean as ``true`` / ``false``.

    Everything else is handed to :mod:`csv` unchanged, which renders a float at
    full stored precision (``repr``) rather than rounding it.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _share(ai: int, scored: int, too_short: int) -> float | str:
    """The ``ai_share`` field of one aggregate row, empty when it has no meaning.

    The dashboard's definition (:func:`~.store.ai_share`): the AI verdicts over
    every message the mix bar draws, the scored ones plus those gated under the
    reliability floor. A zero denominator is written as an empty field rather
    than the 0.0 the dashboard displays, because a share of nothing is not zero
    and an empty field cannot be averaged into one by mistake.
    """
    denominator = scored + too_short
    if denominator == 0:
        return ""
    return ai / denominator


def _aggregate_row(row: Any) -> dict[str, Any]:
    """The shared aggregate columns of one ``GROUP BY`` row, as a dict."""
    return {
        "messages": row["messages"],
        "scored": row["scored"],
        "too_short": row["too_short"],
        "human": row["human"],
        "mixed": row["mixed"],
        "ai": row["ai"],
        "first_date": row["first_date"],
        "last_date": row["last_date"],
    }


def _empty_aggregate() -> dict[str, Any]:
    """A zeroed aggregate, for a named list the date range leaves no message."""
    return {
        "messages": 0,
        "scored": 0,
        "too_short": 0,
        "human": 0,
        "mixed": 0,
        "ai": 0,
        "first_date": None,
        "last_date": None,
    }


def _merge_aggregate(into: dict[str, Any], row: dict[str, Any]) -> None:
    """Add one address's aggregate into the sender it rolls up to."""
    for key in ("messages", "scored", "too_short", "human", "mixed", "ai"):
        into[key] += row[key]
    for key, pick in (("first_date", min), ("last_date", max)):
        dates = [d for d in (into[key], row[key]) if d]
        into[key] = pick(dates) if dates else None


class _CsvMember:
    """One CSV member of the archive, written row by row.

    Wraps the binary stream :meth:`zipfile.ZipFile.open` returns in a UTF-8 text
    layer and a :mod:`csv` writer with ``\\n`` line endings, so a caller writes
    dicts keyed by column name and never sees the encoding.
    """

    def __init__(self, archive: zipfile.ZipFile, name: str, columns: Sequence[str]) -> None:
        self.columns = list(columns)
        self._raw = archive.open(name, "w")
        self._text = io.TextIOWrapper(self._raw, encoding="utf-8", newline="")
        self._writer = csv.writer(self._text, lineterminator="\n")
        self._writer.writerow(self.columns)

    def write(self, values: dict[str, Any]) -> None:
        self._writer.writerow([_cell(values.get(column)) for column in self.columns])

    def close(self) -> None:
        # Closing the text layer flushes it and closes the member; the member's
        # own close is idempotent, so closing it again is harmless.
        self._text.close()
        self._raw.close()

    def __enter__(self) -> "_CsvMember":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- Export -------------------------------------------------------------------


def export_stats(
    store: Store,
    list_names: Sequence[str] | None,
    out_path: str | Path,
    *,
    all_lists: bool = False,
    pseudonymous: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> StatsExportSummary:
    """Export the scores and metadata of the selected messages as a CSV zip archive.

    Selection is identical to :func:`~.export_import.export_lists`, with which it
    shares :func:`~.export_import._select_lists`: lists by ``lists.name`` or,
    with ``all_lists=True``, every list with a message in scope; passing both —
    or neither — is a :class:`ValueError`, as is an unknown name. ``date_from`` /
    ``date_to`` bound the messages by ``messages.date``, inclusively at both ends
    and independently, by the same lexical comparison the dashboard's date filter
    applies, including its one sharp edge: a bare ``date_to`` day excludes that
    day's messages, whose stored value carries a time. A named list is exported
    whether or not the range leaves it any message.

    Every message in scope is exported whether or not it was scored. With
    ``pseudonymous`` the identity columns are omitted and senders are numbered
    ``s1``, ``s2``, … in first-seen order, assigned per export and deliberately
    unstable across exports.

    :data:`ZIP_SUFFIX` is appended to ``out_path`` unless it is already there;
    the returned summary's ``path`` is the path actually written, so a caller
    that passed ``stats`` can report the ``stats.zip`` it got.

    Purely a local database read: no IMAP, no Pangram, no caps involved.
    """
    conn = store.conn
    selected = _select_lists(
        conn, list_names, all_lists=all_lists, date_from=date_from, date_to=date_to
    )
    identified = not pseudonymous
    range_clause = _range_clause("m.date", date_from, date_to)
    range_params = _range_params(date_from, date_to)
    # List ids are integers read from the database, never user input, so they are
    # inlined rather than bound: the count of selected lists is unbounded and
    # SQLite's bound-parameter limit is not.
    scope = f" WHERE m.list_id IN ({','.join(str(int(lst['id'])) for lst in selected)})"

    # Pre-pass. Two cross-references have to be resolvable before a message row
    # can be written: the sender its address rolls up to (a person groups several
    # addresses, and a pseudonymous key is an ordinal over first sightings), and
    # the key of the message its In-Reply-To names, which may be emitted later or
    # on another list. Only those are collected — a message contributes its
    # Message-ID and an ordinal, never a row — so what is held scales with the
    # messages in scope rather than with their size.
    addresses: dict[int, Any] = {}  # address id -> addresses row
    sender_of_address: dict[int, str] = {}  # address id -> canonical sender id
    senders: dict[str, dict[str, Any]] = {}  # canonical sender id -> sender record
    person_names: dict[int, str] = {}
    parents: dict[str, list[tuple[int, str]]] = {}  # Message-ID -> [(list id, key)]

    def register_address(address_id: int) -> str:
        """The canonical sender id for one address, registering it on first sight."""
        known = sender_of_address.get(address_id)
        if known is not None:
            return known
        row = conn.execute(
            "SELECT id, email, display_name, person_id FROM addresses WHERE id = ?",
            (address_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - address_id came from a live FK
            sender_of_address[address_id] = ""
            return ""
        addresses[address_id] = row
        person_id = row["person_id"]
        # A linked person is one sender across its addresses; an unlinked address
        # is its own sender — the grouping the dashboard's Senders pane applies.
        canonical = f"p{person_id}" if person_id is not None else f"a{address_id}"
        record = senders.get(canonical)
        if record is None:
            if person_id is not None and person_id not in person_names:
                person = conn.execute(
                    "SELECT canonical_name FROM persons WHERE id = ?", (person_id,)
                ).fetchone()
                person_names[person_id] = person["canonical_name"] if person else ""
            record = {
                "key": f"s{len(senders) + 1}" if pseudonymous else canonical,
                "type": "person" if person_id is not None else "address",
                "name": person_names[person_id] if person_id is not None else row["display_name"],
                "emails": [],
                **_empty_aggregate(),
            }
            senders[canonical] = record
        if row["email"] not in record["emails"]:
            record["emails"].append(row["email"])
        sender_of_address[address_id] = canonical
        return canonical

    ordinal = 0
    for lst in selected:
        for row in conn.execute(
            f"SELECT m.id AS id, m.message_id AS message_id, m.address_id AS address_id "
            f"FROM messages m WHERE m.list_id = ?{range_clause} ORDER BY m.id",
            [lst["id"], *range_params],
        ):
            ordinal += 1
            parents.setdefault(row["message_id"], []).append((lst["id"], f"m{ordinal}"))
            if row["address_id"] is not None:
                register_address(row["address_id"])

    def parent_key(list_id: int, message_id: str, in_reply_to: str | None) -> str:
        """The exported key of the message ``in_reply_to`` names, else empty.

        The header is normalised by :func:`~.store._parent_message_id`, the same
        lookup the reply-timing recompute applies, so a thread reconstructed here
        is the one the ``timing`` column was computed over. A parent stored on
        several lists resolves to the copy on the reply's own list, then to the
        first emitted; a message naming itself has no parent, as in the timing
        pass.
        """
        if not in_reply_to:
            return ""
        parent_mid = _parent_message_id(in_reply_to)
        if parent_mid == message_id:
            return ""
        candidates = parents.get(parent_mid, [])
        chosen = next((c for c in candidates if c[0] == list_id), None)
        if chosen is None:
            chosen = candidates[0] if candidates else None
        return chosen[1] if chosen else ""

    n_messages = 0
    n_scored = 0
    detector_versions: set[str] = set()
    extraction_versions: set[int] = set()

    written_path = zip_path(out_path)
    with zipfile.ZipFile(written_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        # The streaming pass: one message row is live at a time, serialised into
        # the open member before the next is read.
        with _CsvMember(
            archive,
            MESSAGES_MEMBER,
            _columns(_MESSAGE_COLUMNS, _MESSAGE_IDENTITY_COLUMNS, identified),
        ) as member:
            for lst in selected:
                for row in conn.execute(
                    f"{_MESSAGE_SELECT}{_MESSAGE_JOINS} WHERE m.list_id = ?{range_clause} "
                    "ORDER BY m.id",
                    [lst["id"], *range_params],
                ):
                    n_messages += 1
                    if row["score_id"] is not None:
                        n_scored += 1
                    if row["detector_version"] is not None:
                        detector_versions.add(row["detector_version"])
                    if row["extraction_version"] is not None:
                        extraction_versions.add(row["extraction_version"])

                    address = addresses.get(row["address_id"]) if row["address_id"] else None
                    canonical = (
                        sender_of_address.get(row["address_id"]) if row["address_id"] else None
                    )
                    sender = senders.get(canonical) if canonical else None
                    member.write(
                        {
                            "message_key": f"m{n_messages}",
                            "list": lst["name"],
                            "folder": lst["folder"],
                            "date": row["date"],
                            "sender_key": sender["key"] if sender else "",
                            "email": address["email"] if address else "",
                            # The message's own From name, falling back to the
                            # display name stored for its address.
                            "sender_name": row["from_name"]
                            or (address["display_name"] if address else None),
                            "is_reply": bool(row["in_reply_to"]),
                            "parent_key": parent_key(
                                lst["id"], row["message_id"], row["in_reply_to"]
                            ),
                            "message_id": row["message_id"],
                            "in_reply_to": row["in_reply_to"],
                            "auto_generated": row["auto_generated"],
                            "timing": row["timing"],
                            "timing_cpm": row["timing_cpm"],
                            "extraction_status": row["extraction_status"],
                            "extraction_method": row["extraction_method"],
                            "extraction_chars": row["extraction_chars"],
                            "extraction_version": row["extraction_version"],
                            "pipeline_version": row["pipeline_version"],
                            "label": row["label"],
                            "fraction_ai": row["fraction_ai"],
                            "fraction_ai_assisted": row["fraction_ai_assisted"],
                            "fraction_human": row["fraction_human"],
                            "detector_version": row["detector_version"],
                            "scored_at": row["scored_at"],
                        }
                    )

        # The aggregates: one GROUP BY per table over the identical scope, so
        # their counts sum to the rows just written.
        list_aggregates = {
            row["list_id"]: _aggregate_row(row)
            for row in conn.execute(
                f"SELECT m.list_id AS list_id, {_AGGREGATE_COLUMNS}{_MESSAGE_JOINS}"
                f"{scope}{range_clause} GROUP BY m.list_id",
                range_params,
            )
        }
        with _CsvMember(archive, LISTS_MEMBER, _LIST_COLUMNS) as member:
            for lst in selected:
                # A named list with nothing in range is exported as a zero row:
                # the file says the list was asked for and held no message.
                counts = list_aggregates.get(lst["id"]) or _empty_aggregate()
                member.write(
                    {
                        "list": lst["name"],
                        "folder": lst["folder"],
                        **counts,
                        "ai_share": _share(counts["ai"], counts["scored"], counts["too_short"]),
                    }
                )

        # Grouped by address, then folded into the sender each address rolls up
        # to, which is how a person's several addresses become one row.
        for row in conn.execute(
            f"SELECT m.address_id AS address_id, {_AGGREGATE_COLUMNS}{_MESSAGE_JOINS}"
            f"{scope}{range_clause} AND m.address_id IS NOT NULL GROUP BY m.address_id",
            range_params,
        ):
            canonical = sender_of_address.get(row["address_id"])
            if canonical and canonical in senders:
                _merge_aggregate(senders[canonical], _aggregate_row(row))
        with _CsvMember(
            archive,
            SENDERS_MEMBER,
            _columns(_SENDER_COLUMNS, _SENDER_IDENTITY_COLUMNS, identified),
        ) as member:
            for record in senders.values():
                member.write(
                    {
                        **record,
                        "sender_key": record["key"],
                        "sender_type": record["type"],
                        "emails": ";".join(sorted(record["emails"])),
                        "ai_share": _share(record["ai"], record["scored"], record["too_short"]),
                    }
                )

        schema_row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        manifest = {
            "format": STATS_FORMAT_NAME,
            "stats_format_version": STATS_FORMAT_VERSION,
            "app_version": __version__,
            "schema_version": schema_row["v"] if schema_row and schema_row["v"] is not None else 0,
            "exported_at": _utcnow_iso(),
            "folders": [lst["folder"] for lst in selected],
            # Provenance for a partial export, so a file can say which messages
            # it was asked for rather than only which it holds. Absent (not
            # null) when the bound was not given.
            **({"date_from": date_from} if date_from else {}),
            **({"date_to": date_to} if date_to else {}),
            "identified": identified,
            "rows": {
                "messages": n_messages,
                "lists": len(selected),
                "senders": len(senders),
            },
            "labels": list(LABELS),
            "timing_bands": list(TIMING_BANDS),
            "detector_versions": sorted(detector_versions),
            "extraction_versions": sorted(extraction_versions),
        }
        archive.writestr(MANIFEST_MEMBER, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        archive.writestr(README_MEMBER, _readme(identified))

    return StatsExportSummary(
        lists=len(selected),
        senders=len(senders),
        messages=n_messages,
        scored=n_scored,
        path=str(written_path),
    )


# --- Data dictionary ------------------------------------------------------------


def _table(rows: Iterable[tuple[str, str]]) -> str:
    """A two-column Markdown table of ``(column, meaning)`` pairs."""
    lines = ["| column | meaning |", "|---|---|"]
    lines += [f"| `{column}` | {meaning} |" for column, meaning in rows]
    return "\n".join(lines)


def _readme(identified: bool) -> str:
    """The archive's ``README.md``: the data dictionary for this exact file.

    Written for someone who has only the zip, so it describes the columns the
    file actually carries — a pseudonymous export documents no identity column,
    having none — and the caveats that stop the obvious misreadings.
    """
    message_rows = [
        ("message_key", "file-scoped key `m1`, `m2`, … in the order rows appear"),
        ("list", "the mailing list's name; not unique across lists"),
        ("folder", "the list's IMAP folder, its unique key; joins to `lists.csv`"),
        ("date", "the message's `Date`, UTC ISO-8601; empty when it had none"),
        ("sender_key", "joins to `senders.csv`; empty when the message has no sender address"),
        ("email", "sender address, empty when the message has none"),
        ("sender_name", "the message's `From` name, falling back to the address's display name"),
        ("is_reply", "`true` when the message carries an `In-Reply-To` header"),
        ("parent_key", "`message_key` of the parent when it is in this file, else empty"),
        ("message_id", "RFC 5322 Message-ID"),
        ("in_reply_to", "raw `In-Reply-To` value, empty when not a reply"),
        (
            "auto_generated",
            "the marker that classified the message auto-generated, empty when none",
        ),
        ("timing", "reply-timing band: `normal`, `suspicious`, `implausible`, or empty"),
        (
            "timing_cpm",
            "the characters-per-minute rate behind the band, empty exactly where `timing` is",
        ),
        (
            "extraction_status",
            "`ok`, `empty`, `too_short`, `failed`, or empty when never extracted",
        ),
        (
            "extraction_method",
            "the extraction routine that produced the text, empty when never extracted",
        ),
        ("extraction_chars", "characters of extracted text, empty when never extracted"),
        ("extraction_version", "generation of the extraction routine, may be empty"),
        ("pipeline_version", "app version that last processed the message, may be empty"),
        ("label", "detector verdict `Human`, `Mixed` or `AI`; empty when unscored"),
        ("fraction_ai", "detector fraction in [0, 1], empty when unscored"),
        ("fraction_ai_assisted", "as above"),
        ("fraction_human", "as above"),
        ("detector_version", "the detector build that produced the score, empty when unscored"),
        ("scored_at", "UTC ISO-8601, empty when unscored"),
    ]
    aggregate_rows = [
        ("messages", "messages in scope"),
        ("scored", "messages with a score"),
        (
            "too_short",
            "messages gated under the reliability floor (`extraction_status = too_short`)",
        ),
        ("human", "scored messages labelled `Human`"),
        ("mixed", "scored messages labelled `Mixed`"),
        ("ai", "scored messages labelled `AI`"),
        ("ai_share", "`ai / (scored + too_short)`, empty when that denominator is 0"),
        ("first_date", "oldest `date` in scope, empty when none"),
        ("last_date", "newest `date` in scope, empty when none"),
    ]
    list_rows = [
        ("list", f"as in `{MESSAGES_MEMBER}`"),
        ("folder", f"as in `{MESSAGES_MEMBER}`"),
        *aggregate_rows,
    ]
    sender_rows = [
        ("sender_key", "the key `messages.csv` joins on"),
        ("sender_type", "`person` when the sender is a linked group of addresses, else `address`"),
        ("name", "the person's canonical name, or the address's display name; empty when none"),
        ("emails", "the sender's addresses with a message in scope, `;`-separated"),
        *aggregate_rows,
    ]
    if not identified:
        message_rows = [row for row in message_rows if row[0] not in _MESSAGE_IDENTITY_COLUMNS]
        sender_rows = [row for row in sender_rows if row[0] not in _SENDER_IDENTITY_COLUMNS]

    bands = f"{int(TIMING_SUSPICIOUS_CPM)}, `implausible` from {int(TIMING_IMPLAUSIBLE_CPM)}"
    # Pre-wrapped, because this paragraph goes into a Markdown file rather than
    # through a formatter.
    identity = (
        """This is an **identified** export: sender addresses and names are present, and
`message_id` / `in_reply_to` are the real header values. `sender_key` is
`p<person id>` or `a<address id>`, stable across exports from the same
database."""
        if identified
        else """This is a **pseudonymous** export: the identity columns (`email`, `sender_name`,
`name`, `emails`, `message_id`, `in_reply_to`) are omitted rather than blanked,
so the header rows state what the file holds. `sender_key` is `s1`, `s2`, … in
first-seen order, assigned for this file alone and not comparable with any other
export. Pseudonymous is not anonymous: list names, dates and thread shapes
remain, and mailing-list archives are public."""
    )

    return f"""# Mailing-list AI check — statistics export

One row per message, plus per-list and per-sender aggregates, from an
AI-detection pipeline run over one or more mailing lists. The archive carries no
message content: no bodies, no extracted text, no subjects, no raw headers and
no detector responses. Nothing here is read back by the application that wrote
it; this is an analysis artifact.

## Members

| member | contents |
|---|---|
| `{MESSAGES_MEMBER}` | one row per message in scope, scored or not |
| `{LISTS_MEMBER}` | one row per exported list, aggregated over the same scope |
| `{SENDERS_MEMBER}` | one row per sender with a message in scope |
| `{MANIFEST_MEMBER}` | provenance, row counts and the values present in the file |
| `{README_MEMBER}` | this file |

The CSV files are UTF-8, RFC 4180, with a header row and `\\n` line endings. A
NULL is an empty field, booleans are `true` / `false`, dates are the stored UTC
ISO-8601 strings, and fractions are written at full stored precision, unrounded.

{identity}

## `{MESSAGES_MEMBER}`

Every message in scope, whether or not it was scored: a share calculation needs
the messages that carry no verdict as much as those that do.

{_table(message_rows)}

## `{LISTS_MEMBER}`

One row per exported list. The counts are over the messages in this file, so
they sum exactly to `{MESSAGES_MEMBER}`.

{_table(list_rows)}

## `{SENDERS_MEMBER}`

One row per sender with at least one message in scope. A sender is a linked
person when the address belongs to one, otherwise the bare address, so a person
who posts from several addresses is one row. A message with no sender address
at all has an empty `sender_key`: it is counted in the list aggregate, and in no
sender row.

{_table(sender_rows)}

## Reading the numbers

- `label` is the detector's own verdict, stored verbatim; the application
  derives nothing from it.
- A share must include the too-short messages in its denominator to match the
  application's own figures. The reliability floor gates messages under 50 words
  of authored text: they are never sent to the detector, so they are neither
  human nor AI, but they are messages.
- Scores in one file may come from different detector versions and different
  extraction generations. Both are per-row columns and are listed in
  `{MANIFEST_MEMBER}`; an aggregate over a mixed file mixes them.
- A reply's timing band is the implied composition rate of its new text, in
  characters per minute of the gap between the parent message and the reply:
  `suspicious` from {bands}. An empty band means the rate could not be computed
  — the message is not a reply, its parent is not stored, a date is missing or
  unusable, or the message has no extracted text — not that it was normal.
- The date range, when one was applied, is recorded in `{MANIFEST_MEMBER}`. Its
  comparison is lexical over the stored dates, so a bare `date_to` day
  ("2026-03-01") excludes that day's messages, whose stored value carries a
  time.
- `parent_key` links a reply to its parent only when the parent is in this file.
  A reply whose parent falls outside the selected lists or date range keeps
  `is_reply` true and an empty `parent_key`.
"""
