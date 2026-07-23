"""Tests for the export/import feature (:mod:`mailing_list_ai_check.export_import`).

Written against ``docs/export-import.md``: a JSON Lines stream (``header`` first,
per-list ``list``/``pull_state`` then global ``person``/``address``/``message``
records with extraction + score embedded, ``trailer`` last), text stored as a
pointer into the message body (``full_body`` / ``span`` / ``inline``), and an
idempotent, collision-safe, all-or-nothing import.

Fixtures are built through the public :class:`Store` API. The source database
covers: two lists; messages with and without a sender address; an address linked
to a person plus unlinked ones; an address with no messages (never exported); a
full-body / span / inline / no-extraction spread; a scored extraction carrying a
raw_response JSON blob, one carrying a null raw_response, and one ok-but-unscored;
and a pull_state cursor for one list.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from mailing_list_ai_check import __version__, export_import
from mailing_list_ai_check.cli import export_main, import_main
from mailing_list_ai_check.store import Store, sha256_text

# --- fixture data -------------------------------------------------------------

ANNOUNCE = "Shared Folders/announce"
LAST_CALL = "Shared Folders/last-call"

# Message natural-key ids used throughout.
M1 = "<m1@example.org>"  # full_body, scored (raw_response JSON)
M2 = "<m2@example.org>"  # span, ok-but-unscored
M3 = "<m3@example.org>"  # inline, no sender, scored (raw_response null)
M4 = "<m4@example.org>"  # no extraction
M5 = "<m5@example.org>"  # last-call, too_short span, unscored
M6 = "<m6@example.org>"  # full_body, unscored, second Alice address

BODY1 = "This is the whole body of message one."
BODY2 = "Header line\nExtracted middle part\nFooter line"
SPAN2 = "Extracted middle part"
BODY3 = "Only quoted original content here."
INLINE3 = "Reconstructed text that is absent from the raw body"
BODY5 = "prefix Last call body suffix"
SPAN5 = "Last call body"
BODY6 = "Second alice address body."

RAW_RESPONSE_M1 = {"prediction_short": "AI", "fraction_ai": 0.95, "windows": []}


def _build_source(store: Store) -> None:
    """Populate ``store`` with the representative export fixture (see module docstring)."""
    ann = store.upsert_list("announce", ANNOUNCE)
    lc = store.upsert_list("last-call", LAST_CALL)

    p1 = store.create_person("Alice Smith")
    alice = store.upsert_address("alice@example.org", "Alice Smith")
    alice2 = store.upsert_address("alice@work.example", "Alice Smith")
    bob = store.upsert_address("bob@example.org", "Bob Jones")
    carol = store.upsert_address("carol@example.org", "Carol")
    store.upsert_address("dave@example.org", "Dave")  # no messages -> never exported
    store.assign_address_to_person(alice.id, p1.id)
    store.assign_address_to_person(alice2.id, p1.id)  # a1 + a2 form a person group

    # m1 -- extracted_text == raw_body -> full_body; scored with a raw_response blob.
    m1 = store.upsert_message(
        message_id=M1,
        list_id=ann.id,
        address_id=alice.id,
        subject="Full body",
        date="2026-01-05T10:00:00+00:00",
        in_reply_to=None,
        raw_body=BODY1,
        uid=101,
        fetched_at="2026-01-06T00:00:00+00:00",
        raw_html="<p>whole</p>",
    ).message
    e1 = store.insert_extraction(
        message_id=m1.id,
        extracted_text=BODY1,
        method="reply_parser",
        status="ok",
        created_at="2026-01-06T01:00:00+00:00",
    )
    store.insert_score(
        extraction_id=e1.id,
        text_sha256=sha256_text("cleaned one"),
        fraction_ai=0.95,
        fraction_ai_assisted=0.03,
        fraction_human=0.02,
        label="AI",
        detector_version="3.3.2",
        raw_response=RAW_RESPONSE_M1,
        scored_at="2026-01-07T00:00:00+00:00",
    )

    # m2 -- extracted_text is a substring of raw_body -> span; ok but not scored.
    m2 = store.upsert_message(
        message_id=M2,
        list_id=ann.id,
        address_id=bob.id,
        subject="Span",
        date="2026-01-10T10:00:00+00:00",
        in_reply_to=M1,
        raw_body=BODY2,
        uid=102,
        fetched_at="2026-01-11T00:00:00+00:00",
    ).message
    store.insert_extraction(
        message_id=m2.id,
        extracted_text=SPAN2,
        method="reply_parser",
        status="ok",
        created_at="2026-01-11T01:00:00+00:00",
    )

    # m3 -- no sender address; extracted_text not a substring -> inline; scored
    # with a null raw_response.
    m3 = store.upsert_message(
        message_id=M3,
        list_id=ann.id,
        address_id=None,
        subject="Inline",
        date="2026-01-15T10:00:00+00:00",
        in_reply_to=None,
        raw_body=BODY3,
        uid=103,
        fetched_at="2026-01-16T00:00:00+00:00",
    ).message
    e3 = store.insert_extraction(
        message_id=m3.id,
        extracted_text=INLINE3,
        method="custom",
        status="ok",
        created_at="2026-01-16T01:00:00+00:00",
    )
    store.insert_score(
        extraction_id=e3.id,
        text_sha256=sha256_text("cleaned three"),
        fraction_ai=0.10,
        fraction_ai_assisted=0.05,
        fraction_human=0.85,
        label="Human",
        detector_version="3.3.2",
        raw_response=None,
        scored_at="2026-01-17T00:00:00+00:00",
    )

    # m4 -- no extraction row at all.
    store.upsert_message(
        message_id=M4,
        list_id=ann.id,
        address_id=carol.id,
        subject="No extraction",
        date="2026-01-20T10:00:00+00:00",
        in_reply_to=None,
        raw_body="Body with no extraction row.",
        uid=104,
        fetched_at="2026-01-21T00:00:00+00:00",
    )

    # m6 -- full_body, unscored; second Alice address completes the person group.
    m6 = store.upsert_message(
        message_id=M6,
        list_id=ann.id,
        address_id=alice2.id,
        subject="Group",
        date="2026-01-25T10:00:00+00:00",
        in_reply_to=None,
        raw_body=BODY6,
        uid=106,
        fetched_at="2026-01-26T00:00:00+00:00",
    ).message
    store.insert_extraction(
        message_id=m6.id,
        extracted_text=BODY6,
        method="reply_parser",
        status="ok",
        created_at="2026-01-26T01:00:00+00:00",
    )

    # m5 -- last-call, too_short span, unscored.
    m5 = store.upsert_message(
        message_id=M5,
        list_id=lc.id,
        address_id=alice.id,
        subject="Last call",
        date="2026-02-01T10:00:00+00:00",
        in_reply_to=None,
        raw_body=BODY5,
        uid=201,
        fetched_at="2026-02-02T00:00:00+00:00",
    ).message
    store.insert_extraction(
        message_id=m5.id,
        extracted_text=SPAN5,
        method="reply_parser",
        status="too_short",
        created_at="2026-02-02T01:00:00+00:00",
    )

    store.set_pull_state(lc.id, uidvalidity=42, last_uid=99)


@pytest.fixture
def source():
    """An in-memory source Store populated with the export fixture."""
    with Store(":memory:") as s:
        _build_source(s)
        yield s


@pytest.fixture
def target():
    """A fresh, empty in-memory target Store."""
    with Store(":memory:") as s:
        yield s


# --- file helpers -------------------------------------------------------------


def _opener(path: Path):
    return gzip.open if path.suffix == ".gz" else open


def _read_records(path: str | Path) -> list[dict]:
    """Read a (optionally gzip) JSONL export file into a list of record dicts."""
    p = Path(path)
    with _opener(p)(p, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _write_records(path: str | Path, records: list[dict]) -> None:
    """Write record dicts back out as JSONL (gzip when the suffix is .gz)."""
    p = Path(path)
    with _opener(p)(p, "wt", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _messages_of_type(records: list[dict], type_: str) -> list[dict]:
    return [r for r in records if r.get("type") == type_]


def _message_by_id(records: list[dict]) -> dict[str, dict]:
    return {r["message_id"]: r for r in _messages_of_type(records, "message")}


# --- DB introspection helpers (natural keys, so ids need not match) -----------

_DATA_TABLES = ("lists", "pull_state", "persons", "addresses", "messages", "extractions", "scores")


def _counts(store: Store) -> dict[str, int]:
    return {
        t: store.conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
        for t in _DATA_TABLES
    }


def _messages_by_key(store: Store) -> dict[tuple[str, str], dict]:
    rows = store.conn.execute(
        "SELECT l.folder AS folder, m.message_id AS message_id, m.subject AS subject, "
        "m.date AS date, m.in_reply_to AS in_reply_to, m.raw_body AS raw_body, "
        "m.raw_html AS raw_html, m.uid AS uid, m.fetched_at AS fetched_at, "
        "m.pipeline_version AS pipeline_version, a.email AS email "
        "FROM messages m JOIN lists l ON l.id = m.list_id "
        "LEFT JOIN addresses a ON a.id = m.address_id"
    ).fetchall()
    return {(r["folder"], r["message_id"]): dict(r) for r in rows}


def _extractions_by_key(store: Store) -> dict[tuple[str, str], dict]:
    rows = store.conn.execute(
        "SELECT l.folder AS folder, m.message_id AS message_id, e.extracted_text AS extracted_text, "
        "e.method AS method, e.char_count AS char_count, e.status AS status, "
        "e.created_at AS created_at "
        "FROM extractions e JOIN messages m ON m.id = e.message_id "
        "JOIN lists l ON l.id = m.list_id"
    ).fetchall()
    return {(r["folder"], r["message_id"]): dict(r) for r in rows}


def _scores_by_key(store: Store) -> dict[tuple[str, str], dict]:
    rows = store.conn.execute(
        "SELECT l.folder AS folder, m.message_id AS message_id, s.fraction_ai AS fraction_ai, "
        "s.fraction_ai_assisted AS fraction_ai_assisted, s.fraction_human AS fraction_human, "
        "s.label AS label, s.detector_version AS detector_version, "
        "s.raw_response AS raw_response, s.text_sha256 AS text_sha256, s.scored_at AS scored_at "
        "FROM scores s JOIN extractions e ON e.id = s.extraction_id "
        "JOIN messages m ON m.id = e.message_id JOIN lists l ON l.id = m.list_id"
    ).fetchall()
    return {(r["folder"], r["message_id"]): dict(r) for r in rows}


def _person_groups(store: Store) -> dict[str, frozenset[str]]:
    """Map each person's canonical name to the frozenset of its addresses' emails."""
    rows = store.conn.execute(
        "SELECT p.canonical_name AS name, a.email AS email "
        "FROM persons p JOIN addresses a ON a.person_id = p.id"
    ).fetchall()
    groups: dict[str, set[str]] = {}
    for r in rows:
        groups.setdefault(r["name"], set()).add(r["email"])
    return {k: frozenset(v) for k, v in groups.items()}


def _pull_states_by_folder(store: Store) -> dict[str, tuple[int, int]]:
    rows = store.conn.execute(
        "SELECT l.folder AS folder, ps.uidvalidity AS uidvalidity, ps.last_uid AS last_uid "
        "FROM pull_state ps JOIN lists l ON l.id = ps.list_id"
    ).fetchall()
    return {r["folder"]: (r["uidvalidity"], r["last_uid"]) for r in rows}


# ==============================================================================
# EXPORT
# ==============================================================================


def test_export_writes_valid_jsonl_with_header_and_trailer(source, tmp_path):
    """Header first, trailer last, records grouped in the spec order; summary matches."""
    out = tmp_path / "all.jsonl"
    summary = export_import.export_lists(source, None, out, all_lists=True)

    records = _read_records(out)
    assert records[0]["type"] == "header"
    assert records[-1]["type"] == "trailer"

    header = records[0]
    assert header["format"] == export_import.FORMAT_NAME == "mlac-export"
    assert header["format_version"] == export_import.FORMAT_VERSION == 2
    assert header["app_version"] == __version__
    schema_version = source.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()[
        "v"
    ]
    assert header["schema_version"] == schema_version
    assert set(header["folders"]) == {ANNOUNCE, LAST_CALL}

    # Record grouping: header, then lists (+pull_state), then persons, addresses,
    # then messages, then trailer -- a non-decreasing rank sequence.
    rank = {"header": 0, "list": 1, "pull_state": 1, "person": 2, "address": 3, "message": 4}
    body_ranks = [rank[r["type"]] for r in records[:-1]]
    assert body_ranks == sorted(body_ranks)

    trailer = records[-1]
    assert (trailer["lists"], trailer["messages"], trailer["extractions"], trailer["scores"]) == (
        2,
        6,
        5,
        2,
    )
    assert (summary.lists, summary.messages, summary.extractions, summary.scores) == (2, 6, 5, 2)
    assert summary.path == str(out)


def test_export_text_pointers_and_sha256(source, tmp_path):
    """full_body / span / inline are chosen per spec; sha256 is always the text hash."""
    out = tmp_path / "ptr.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)
    msgs = _message_by_id(_read_records(out))

    # full_body: extracted_text == raw_body.
    e1 = msgs[M1]["extraction"]
    assert e1["text"] == {"kind": "full_body"}
    assert e1["sha256"] == sha256_text(BODY1)

    # span: start/length resolve to the exact substring of the record's own body.
    e2 = msgs[M2]["extraction"]
    assert e2["text"]["kind"] == "span"
    start, length = e2["text"]["start"], e2["text"]["length"]
    assert msgs[M2]["raw_body"][start : start + length] == SPAN2
    assert length == len(SPAN2)
    assert e2["sha256"] == sha256_text(SPAN2)

    # inline: text not a contiguous substring -> carried verbatim.
    e3 = msgs[M3]["extraction"]
    assert e3["text"] == {"kind": "inline", "value": INLINE3}
    assert e3["sha256"] == sha256_text(INLINE3)


def test_export_message_without_extraction_and_unscored(source, tmp_path):
    """A message with no extraction and an ok-but-unscored extraction round-trip as nulls."""
    out = tmp_path / "misc.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)
    msgs = _message_by_id(_read_records(out))

    assert msgs[M4]["extraction"] is None  # no extraction row
    assert msgs[M2]["extraction"] is not None
    assert msgs[M2]["extraction"]["score"] is None  # ok but not scored


def test_export_message_without_sender(source, tmp_path):
    """A message with no sender address exports ``email: null``."""
    out = tmp_path / "nosender.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)
    msgs = _message_by_id(_read_records(out))
    assert msgs[M3]["email"] is None
    assert msgs[M1]["email"] == "alice@example.org"


def test_export_score_record_fields(source, tmp_path):
    """The embedded score carries fractions, label, version, raw_response verbatim, sha, time."""
    out = tmp_path / "score.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)
    msgs = _message_by_id(_read_records(out))

    score = msgs[M1]["extraction"]["score"]
    assert score["fraction_ai"] == 0.95
    assert score["fraction_ai_assisted"] == 0.03
    assert score["fraction_human"] == 0.02
    assert score["label"] == "AI"
    assert score["detector_version"] == "3.3.2"
    assert score["text_sha256"] == sha256_text("cleaned one")
    assert score["scored_at"] == "2026-01-07T00:00:00+00:00"
    # raw_response is the verbatim stored JSON string.
    assert json.loads(score["raw_response"]) == RAW_RESPONSE_M1

    # A score with a null raw_response stays null.
    assert msgs[M3]["extraction"]["score"]["raw_response"] is None


def test_export_message_records_carry_pipeline_version(source, tmp_path):
    """Every message record carries a ``pipeline_version`` key; the fixture rows
    were all written under the current package version."""
    out = tmp_path / "pv.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)
    msgs = _message_by_id(_read_records(out))
    assert set(msgs) == {M1, M2, M3, M4, M5, M6}
    for rec in msgs.values():
        assert "pipeline_version" in rec
        assert rec["pipeline_version"] == __version__


def test_export_single_list_only_that_lists_data(source, tmp_path):
    """Selecting one list exports only its messages and only referenced addresses/persons."""
    out = tmp_path / "announce.jsonl"
    summary = export_import.export_lists(source, ["announce"], out)
    records = _read_records(out)

    assert set(_message_by_id(records)) == {M1, M2, M3, M4, M6}  # no M5 (last-call)
    emails = {r["email"] for r in _messages_of_type(records, "address")}
    assert emails == {
        "alice@example.org",
        "alice@work.example",
        "bob@example.org",
        "carol@example.org",
    }
    assert "dave@example.org" not in emails  # unreferenced address excluded
    persons = _messages_of_type(records, "person")
    assert [p["canonical_name"] for p in persons] == ["Alice Smith"]
    assert records[0]["folders"] == [ANNOUNCE]
    assert (summary.lists, summary.messages) == (1, 5)


def test_export_other_single_list_narrows_addresses(source, tmp_path):
    """last-call alone references only Alice; Bob/Carol are not exported."""
    out = tmp_path / "lc.jsonl"
    summary = export_import.export_lists(source, ["last-call"], out)
    records = _read_records(out)
    assert set(_message_by_id(records)) == {M5}
    emails = {r["email"] for r in _messages_of_type(records, "address")}
    assert emails == {"alice@example.org"}
    assert (summary.lists, summary.messages, summary.extractions, summary.scores) == (1, 1, 1, 0)


def test_export_all_lists_covers_every_list_with_messages(source, tmp_path):
    out = tmp_path / "all2.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)
    folders = {r["folder"] for r in _messages_of_type(_read_records(out), "list")}
    assert folders == {ANNOUNCE, LAST_CALL}


def test_export_rejects_names_and_all_lists_together(source, tmp_path):
    with pytest.raises(ValueError):
        export_import.export_lists(source, ["announce"], tmp_path / "x.jsonl", all_lists=True)


def test_export_rejects_neither_names_nor_all_lists(source, tmp_path):
    with pytest.raises(ValueError):
        export_import.export_lists(source, None, tmp_path / "x.jsonl")


def test_export_rejects_unknown_list_name(source, tmp_path):
    with pytest.raises(ValueError):
        export_import.export_lists(source, ["nope"], tmp_path / "x.jsonl")


def test_export_gzip_roundtrips(source, target, tmp_path):
    """A .gz output is a real gzip file and imports back into a fresh DB."""
    out = tmp_path / "all.jsonl.gz"
    export_import.export_lists(source, None, out, all_lists=True)

    with open(out, "rb") as fh:
        assert fh.read(2) == b"\x1f\x8b"  # gzip magic
    records = _read_records(out)  # transparently decompressed
    assert records[0]["type"] == "header"

    summary = export_import.import_file(target, out)
    assert summary.messages_inserted == 6
    assert _messages_by_key(target) == _messages_by_key(source)


# ==============================================================================
# IMPORT -- round trip
# ==============================================================================


def test_import_into_fresh_db_reproduces_everything(source, target, tmp_path):
    """A fresh import reproduces lists, pull_state, persons, addresses, messages, +children."""
    out = tmp_path / "all.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)
    summary = export_import.import_file(target, out)

    assert summary.lists_created == 2
    assert summary.lists_existing == 0
    assert summary.pull_states_created == 1
    assert summary.persons_created == 1
    assert summary.addresses_upserted == 4
    assert summary.messages_inserted == 6
    assert summary.messages_skipped == 0
    assert summary.body_mismatches == 0
    assert summary.extractions_inserted == 5
    assert summary.scores_inserted == 2
    assert summary.dry_run is False

    # Everything matches the source, compared by natural key.
    assert _messages_by_key(target) == _messages_by_key(source)
    assert _extractions_by_key(target) == _extractions_by_key(source)
    assert _scores_by_key(target) == _scores_by_key(source)
    assert (
        _person_groups(target)
        == _person_groups(source)
        == {
            "Alice Smith": frozenset(
                {
                    "alice@example.org",
                    "alice@work.example",
                }
            )
        }
    )
    assert _pull_states_by_folder(target) == {LAST_CALL: (42, 99)}

    # Address display names came across too.
    disp = dict(target.conn.execute("SELECT email, display_name FROM addresses").fetchall())
    assert disp["bob@example.org"] == "Bob Jones"
    assert disp["carol@example.org"] == "Carol"

    # Message columns incl. raw_html / uid / fetched_at.
    m1 = _messages_by_key(target)[(ANNOUNCE, M1)]
    assert m1["raw_html"] == "<p>whole</p>"
    assert m1["uid"] == 101
    assert m1["fetched_at"] == "2026-01-06T00:00:00+00:00"


def test_import_preserves_file_pipeline_version(source, target, tmp_path):
    """Each imported message carries the exact ``pipeline_version`` from the file."""
    out = tmp_path / "all.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)
    export_import.import_file(target, out)

    file_versions = {
        (rec["folder"], rec["message_id"]): rec["pipeline_version"]
        for rec in _messages_of_type(_read_records(out), "message")
    }
    stored = _messages_by_key(target)
    for key, version in file_versions.items():
        assert stored[key]["pipeline_version"] == version


def test_import_reconstructs_pointer_text_exactly(source, target, tmp_path):
    """full_body / span / inline all reconstruct the original extracted_text verbatim."""
    out = tmp_path / "all.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)
    export_import.import_file(target, out)

    ext = _extractions_by_key(target)
    assert ext[(ANNOUNCE, M1)]["extracted_text"] == BODY1  # full_body
    assert ext[(ANNOUNCE, M2)]["extracted_text"] == SPAN2  # span
    assert ext[(ANNOUNCE, M3)]["extracted_text"] == INLINE3  # inline
    assert ext[(LAST_CALL, M5)]["extracted_text"] == SPAN5
    # Method / status / char_count / created_at preserved.
    assert ext[(ANNOUNCE, M3)]["method"] == "custom"
    assert ext[(LAST_CALL, M5)]["status"] == "too_short"
    assert ext[(ANNOUNCE, M1)]["char_count"] == len(BODY1)
    assert ext[(ANNOUNCE, M1)]["created_at"] == "2026-01-06T01:00:00+00:00"


def test_import_is_idempotent(source, target, tmp_path):
    """Importing the same file twice inserts nothing new and skips every message."""
    out = tmp_path / "all.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)
    export_import.import_file(target, out)
    before = _counts(target)

    second = export_import.import_file(target, out)
    assert second.messages_skipped == 6
    assert second.messages_inserted == 0
    assert second.extractions_inserted == 0
    assert second.scores_inserted == 0
    assert _counts(target) == before  # nothing duplicated


def test_import_into_source_db_is_a_noop(source, tmp_path):
    """Importing an export back into the database it came from skips everything."""
    out = tmp_path / "all.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)
    before = _counts(source)

    summary = export_import.import_file(source, out)
    assert summary.messages_inserted == 0
    assert summary.messages_skipped == 6
    assert summary.extractions_inserted == 0
    assert summary.scores_inserted == 0
    assert _counts(source) == before


def test_import_partial_overlap_skips_existing_and_flags_body_mismatch(source, target, tmp_path):
    """A pre-existing message (same folder+id, different body) is skipped and its
    children are not imported; the mismatch is counted; other messages import."""
    out = tmp_path / "all.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)

    # Seed the target with m1 under a different body and no extraction.
    ann = target.upsert_list("announce", ANNOUNCE)
    addr = target.upsert_address("alice@example.org", "Alice Smith")
    target.upsert_message(
        message_id=M1,
        list_id=ann.id,
        address_id=addr.id,
        subject="Pre-existing",
        date="2026-01-05T10:00:00+00:00",
        in_reply_to=None,
        raw_body="DIFFERENT body already stored",
        uid=999,
        fetched_at="2026-01-01T00:00:00+00:00",
    )

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.body_mismatches == 1
    assert summary.messages_inserted == 5  # m2, m3, m4, m5, m6

    # m1 kept the target's own body and gained no extraction/score.
    m1 = _messages_by_key(target)[(ANNOUNCE, M1)]
    assert m1["raw_body"] == "DIFFERENT body already stored"
    assert (ANNOUNCE, M1) not in _extractions_by_key(target)
    assert (ANNOUNCE, M1) not in _scores_by_key(target)
    # Only m3's score came across (m1's was skipped with the message).
    assert set(_scores_by_key(target)) == {(ANNOUNCE, M3)}


def test_import_does_not_overwrite_existing_pull_state(source, target, tmp_path):
    """An existing cursor for the list always wins; a fresh cursor is created."""
    out = tmp_path / "all.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)

    # Target already has a last-call list with its own cursor.
    lc = target.upsert_list("last-call", LAST_CALL)
    target.set_pull_state(lc.id, uidvalidity=7, last_uid=7000)

    summary = export_import.import_file(target, out)
    # last-call cursor untouched; announce got no cursor (source had none).
    assert _pull_states_by_folder(target) == {LAST_CALL: (7, 7000)}
    assert summary.pull_states_created == 0


def test_import_creates_pull_state_when_absent(source, target, tmp_path):
    out = tmp_path / "all.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)
    summary = export_import.import_file(target, out)
    assert summary.pull_states_created == 1
    assert _pull_states_by_folder(target) == {LAST_CALL: (42, 99)}


def test_import_existing_list_is_left_untouched(source, target, tmp_path):
    """A list already present is not re-created and its metadata is not overwritten."""
    out = tmp_path / "all.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)

    target.upsert_list("announce", ANNOUNCE)
    summary = export_import.import_file(target, out)
    assert summary.lists_created == 1  # only last-call is new
    assert summary.lists_existing == 1


def test_import_person_group_joins_existing_target_person(source, target, tmp_path):
    """When a target person already owns one of a group's addresses, the imported
    siblings join that person and no new person row is created."""
    out = tmp_path / "all.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)

    # Target already links alice@example.org to an existing person.
    existing = target.create_person("Existing Alice")
    a = target.upsert_address("alice@example.org", "Alice Smith")
    target.assign_address_to_person(a.id, existing.id)

    summary = export_import.import_file(target, out)
    assert summary.persons_created == 0  # no new person for the group

    # The sibling address joined the existing person.
    sibling = target.conn.execute(
        "SELECT person_id FROM addresses WHERE email = 'alice@work.example'"
    ).fetchone()
    assert sibling["person_id"] == existing.id
    assert target.conn.execute("SELECT COUNT(*) AS c FROM persons").fetchone()["c"] == 1


def test_import_dry_run_reports_but_writes_nothing(source, target, tmp_path):
    """dry_run mirrors a real run's counts, sets the flag, and leaves the DB untouched."""
    out = tmp_path / "all.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)

    before = _counts(target)
    dry = export_import.import_file(target, out, dry_run=True)
    assert dry.dry_run is True
    assert _counts(target) == before  # nothing written

    real = export_import.import_file(target, out)
    # The dry-run's would-be counts equal the real run's actual counts.
    assert (dry.lists_created, dry.pull_states_created, dry.persons_created) == (
        real.lists_created,
        real.pull_states_created,
        real.persons_created,
    )
    assert (dry.messages_inserted, dry.extractions_inserted, dry.scores_inserted) == (
        real.messages_inserted,
        real.extractions_inserted,
        real.scores_inserted,
    )
    assert dry.addresses_upserted == real.addresses_upserted


# ==============================================================================
# IMPORT -- version-aware refresh of existing messages
# ==============================================================================
#
# When an imported message already exists in the target, the message row is never
# modified, but its derived data (extraction + score) may be refreshed when the
# file carries a *later* ``pipeline_version`` and the derived data differs. These
# tests build a single-message file with a controlled version + derived data and
# import it over a target holding its own version of the same message.

VF = "Shared Folders/vtest"  # version-test folder
VMID = "<v1@example.org>"
# A body wide enough to contain both distinct span extractions used below.
VBODY = "The quick brown fox jumps over the lazy dog near the riverbank."
TEXT_A = "The quick brown fox"
TEXT_B = "jumps over the lazy dog"

V_SCORE_A = {
    "text_sha256": sha256_text("cleaned vA"),
    "fraction_ai": 0.90,
    "fraction_ai_assisted": 0.05,
    "fraction_human": 0.05,
    "label": "AI",
    "detector_version": "3.3.2",
    "raw_response": {"prediction_short": "AI", "variant": "A"},
    "scored_at": "2026-03-03T00:00:00+00:00",
}
V_SCORE_B = {
    "text_sha256": sha256_text("cleaned vB"),
    "fraction_ai": 0.10,
    "fraction_ai_assisted": 0.05,
    "fraction_human": 0.85,
    "label": "Human",
    "detector_version": "3.4.0",
    "raw_response": {"prediction_short": "Human", "variant": "B"},
    "scored_at": "2026-03-04T00:00:00+00:00",
}


def _seed_versioned(
    store,
    *,
    pipeline_version,
    subject="V subject",
    date="2026-03-01T00:00:00+00:00",
    raw_body=VBODY,
    extraction=True,
    extracted_text=TEXT_A,
    method="reply_parser",
    status="ok",
    ext_created_at="2026-03-02T00:00:00+00:00",
    score=None,
):
    """Put the single message (folder ``VF``, id ``VMID``) into ``store``.

    Every pipeline stage is stamped with ``pipeline_version`` so the message row
    ends up holding exactly that version regardless of which stage ran last.
    """
    lst = store.upsert_list("vtest", VF)
    m = store.upsert_message(
        message_id=VMID,
        list_id=lst.id,
        address_id=None,
        subject=subject,
        date=date,
        in_reply_to=None,
        raw_body=raw_body,
        uid=1,
        fetched_at="2026-03-01T12:00:00+00:00",
        pipeline_version=pipeline_version,
    ).message
    if extraction:
        ext = store.insert_extraction(
            message_id=m.id,
            extracted_text=extracted_text,
            method=method,
            status=status,
            created_at=ext_created_at,
            pipeline_version=pipeline_version,
        )
        if score is not None:
            store.insert_score(extraction_id=ext.id, pipeline_version=pipeline_version, **score)
    return m


def _versioned_file(tmp_path, name, **seed_kwargs):
    """Build a one-message export file whose version + derived data we control."""
    with Store(":memory:") as s:
        _seed_versioned(s, **seed_kwargs)
        out = tmp_path / name
        export_import.export_lists(s, None, out, all_lists=True)
    return out


def test_import_later_version_replaces_differing_derived_data(target, tmp_path):
    """(a) File version later + derived data differs -> the message row is kept but
    its extraction + score are replaced with the file's and the version advances."""
    _seed_versioned(
        target,
        pipeline_version="1.0.0",
        subject="TARGET subject",
        date="2026-03-01T00:00:00+00:00",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    out = _versioned_file(
        tmp_path,
        "later.jsonl",
        pipeline_version="2.0.0",
        subject="FILE subject",
        date="2026-09-09T00:00:00+00:00",
        extracted_text=TEXT_B,
        method="custom",
        score=V_SCORE_B,
    )

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.messages_inserted == 0
    assert summary.body_mismatches == 0
    assert summary.extractions_updated == 1
    assert summary.scores_updated == 1

    # The message row itself stays authoritative (subject/date/body unchanged)...
    m = _messages_by_key(target)[(VF, VMID)]
    assert m["subject"] == "TARGET subject"
    assert m["date"] == "2026-03-01T00:00:00+00:00"
    assert m["raw_body"] == VBODY
    # ...but its pipeline_version advanced to the file's.
    assert m["pipeline_version"] == "2.0.0"

    # Extraction replaced with the file's text/method/status.
    ext = _extractions_by_key(target)[(VF, VMID)]
    assert ext["extracted_text"] == TEXT_B
    assert ext["method"] == "custom"
    assert ext["status"] == "ok"

    # Old score gone; the file's score is the one present.
    sc = _scores_by_key(target)[(VF, VMID)]
    assert sc["label"] == "Human"
    assert sc["text_sha256"] == V_SCORE_B["text_sha256"]
    assert sc["detector_version"] == "3.4.0"


def test_import_later_version_identical_data_bumps_version_only(target, tmp_path):
    """(b) File version later, derived data identical -> only the version stamp advances.

    The later pipeline validated exactly what the target already holds, so the
    message adopts the file's ``pipeline_version``; extraction/score rows are
    untouched and no other message column changes.
    """
    common = dict(
        subject="Same",
        date="2026-03-01T00:00:00+00:00",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    _seed_versioned(target, pipeline_version="1.0.0", **common)
    out = _versioned_file(tmp_path, "identical.jsonl", pipeline_version="2.0.0", **common)

    ext_before = _extractions_by_key(target)
    sc_before = _scores_by_key(target)

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.extractions_updated == 0
    assert summary.scores_updated == 0
    assert summary.versions_bumped == 1

    # Only the stamp moved; derived rows and the rest of the message are intact.
    row = _messages_by_key(target)[(VF, VMID)]
    assert row["pipeline_version"] == "2.0.0"
    assert row["subject"] == "Same"
    assert _extractions_by_key(target) == ext_before
    assert _scores_by_key(target) == sc_before


def test_import_equal_version_different_data_is_pure_skip(target, tmp_path):
    """(c) File version equal to target's -> not later, so skip even when data differs."""
    _seed_versioned(
        target,
        pipeline_version="1.5.0",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    out = _versioned_file(
        tmp_path,
        "equal.jsonl",
        pipeline_version="1.5.0",
        extracted_text=TEXT_B,
        method="custom",
        score=V_SCORE_B,
    )

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.extractions_updated == 0
    assert summary.scores_updated == 0

    # Target's own derived data is untouched.
    ext = _extractions_by_key(target)[(VF, VMID)]
    assert ext["extracted_text"] == TEXT_A
    assert ext["method"] == "reply_parser"
    assert _scores_by_key(target)[(VF, VMID)]["label"] == "AI"
    assert _messages_by_key(target)[(VF, VMID)]["pipeline_version"] == "1.5.0"


def test_import_earlier_version_is_pure_skip(target, tmp_path):
    """(d) File version earlier than target's -> skip, target data wins."""
    _seed_versioned(
        target,
        pipeline_version="2.0.0",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    out = _versioned_file(
        tmp_path,
        "earlier.jsonl",
        pipeline_version="1.0.0",
        extracted_text=TEXT_B,
        method="custom",
        score=V_SCORE_B,
    )

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.extractions_updated == 0
    assert summary.scores_updated == 0

    ext = _extractions_by_key(target)[(VF, VMID)]
    assert ext["extracted_text"] == TEXT_A
    assert _scores_by_key(target)[(VF, VMID)]["label"] == "AI"
    assert _messages_by_key(target)[(VF, VMID)]["pipeline_version"] == "2.0.0"


def test_import_updates_when_target_version_is_null_legacy(target, tmp_path):
    """(e) Legacy target row (NULL version) + a real file version -> NULL sorts
    oldest, so the file is 'later' and its differing data replaces the target's."""
    _seed_versioned(
        target,
        pipeline_version="1.0.0",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    # Simulate a row written before the pipeline_version column existed.
    target.conn.execute("UPDATE messages SET pipeline_version = NULL")
    target.conn.commit()
    assert _messages_by_key(target)[(VF, VMID)]["pipeline_version"] is None

    out = _versioned_file(
        tmp_path,
        "legacy.jsonl",
        pipeline_version="1.0.0",
        extracted_text=TEXT_B,
        method="custom",
        score=V_SCORE_B,
    )

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.extractions_updated == 1
    assert summary.scores_updated == 1

    ext = _extractions_by_key(target)[(VF, VMID)]
    assert ext["extracted_text"] == TEXT_B
    assert ext["method"] == "custom"
    assert _scores_by_key(target)[(VF, VMID)]["label"] == "Human"
    assert _messages_by_key(target)[(VF, VMID)]["pipeline_version"] == "1.0.0"


def test_import_later_version_null_extraction_clears_target_derived(target, tmp_path):
    """(f) Later file version whose extraction is null -> the later pipeline produced
    nothing, so the target's extraction + score are cleared and the version advances."""
    _seed_versioned(
        target,
        pipeline_version="1.0.0",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    out = _versioned_file(tmp_path, "nullext.jsonl", pipeline_version="2.0.0", extraction=False)

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.extractions_updated == 1
    assert summary.scores_updated == 1

    # Extraction and score both cleared; the message row survives with the new version.
    assert (VF, VMID) not in _extractions_by_key(target)
    assert (VF, VMID) not in _scores_by_key(target)
    assert _messages_by_key(target)[(VF, VMID)]["pipeline_version"] == "2.0.0"


def test_import_dry_run_over_update_reports_counts_without_writing(target, tmp_path):
    """(g) A dry-run over an update-triggering file reports the would-be counts but
    leaves the database completely unchanged."""
    _seed_versioned(
        target,
        pipeline_version="1.0.0",
        subject="TARGET subject",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    out = _versioned_file(
        tmp_path,
        "dryupdate.jsonl",
        pipeline_version="2.0.0",
        subject="FILE subject",
        extracted_text=TEXT_B,
        method="custom",
        score=V_SCORE_B,
    )

    msgs_before = _messages_by_key(target)
    ext_before = _extractions_by_key(target)
    sc_before = _scores_by_key(target)
    counts_before = _counts(target)

    dry = export_import.import_file(target, out, dry_run=True)
    assert dry.dry_run is True
    assert dry.messages_skipped == 1
    assert dry.extractions_updated == 1
    assert dry.scores_updated == 1

    # The database is untouched by the dry run.
    assert _counts(target) == counts_before
    assert _messages_by_key(target) == msgs_before
    assert _extractions_by_key(target) == ext_before
    assert _scores_by_key(target) == sc_before


# ==============================================================================
# IMPORT -- error handling (all raise ExportImportError, all roll back)
# ==============================================================================


def _valid_records(source, tmp_path) -> list[dict]:
    out = tmp_path / "valid.jsonl"
    export_import.export_lists(source, None, out, all_lists=True)
    return _read_records(out)


def _assert_import_fails_and_rolls_back(target, records, tmp_path, name="bad.jsonl"):
    """Write ``records`` and assert importing them raises and leaves ``target`` empty."""
    bad = tmp_path / name
    _write_records(bad, records)
    before = _counts(target)
    with pytest.raises(export_import.ExportImportError):
        export_import.import_file(target, bad)
    assert _counts(target) == before  # rolled back -- no partial write


def test_import_rejects_first_record_not_header(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    _assert_import_fails_and_rolls_back(target, records[1:], tmp_path)  # header dropped


def test_import_rejects_wrong_format_name(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    records[0]["format"] = "not-mlac"
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_wrong_format_version(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    records[0]["format_version"] = 999
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_missing_trailer(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    assert records[-1]["type"] == "trailer"
    _assert_import_fails_and_rolls_back(target, records[:-1], tmp_path)  # truncated


def test_import_rejects_trailer_count_mismatch(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    records[-1]["messages"] = records[-1]["messages"] + 1
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_extraction_sha256_mismatch(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    for rec in records:
        if rec.get("type") == "message" and rec["message_id"] == M1:
            rec["extraction"]["sha256"] = "0" * 64  # no longer matches the text
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_corrupted_span_pointer(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    for rec in records:
        if rec.get("type") == "message" and rec["message_id"] == M2:
            rec["extraction"]["text"]["start"] += 3  # reconstructs the wrong text
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_unknown_record_type(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    records.insert(-1, {"type": "wat", "surprise": True})  # before the trailer
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_message_referencing_undeclared_folder(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    for rec in records:
        if rec.get("type") == "message" and rec["message_id"] == M1:
            rec["folder"] = "Shared Folders/ghost"  # no list record declares it
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_address_referencing_unknown_person_key(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    for rec in records:
        if rec.get("type") == "address" and rec.get("person_key") is not None:
            rec["person_key"] = "p9999"  # no such person record
            break
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


# ==============================================================================
# CLI
# ==============================================================================


def _build_source_db(path: Path) -> None:
    with Store(path) as s:
        _build_source(s)


def test_export_main_happy_path(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli.jsonl"
    rc = export_main(["announce", "last-call", "-o", str(out), "--db", str(src)])
    assert rc == 0
    records = _read_records(out)
    assert records[0]["type"] == "header"
    assert len(_message_by_id(records)) == 6


def test_export_main_all_lists(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli-all.jsonl"
    rc = export_main(["--all-lists", "-o", str(out), "--db", str(src)])
    assert rc == 0
    assert {r["folder"] for r in _messages_of_type(_read_records(out), "list")} == {
        ANNOUNCE,
        LAST_CALL,
    }


def test_export_main_gzip(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli.jsonl.gz"
    rc = export_main(["--all-lists", "-o", str(out), "--db", str(src)])
    assert rc == 0
    with open(out, "rb") as fh:
        assert fh.read(2) == b"\x1f\x8b"


def test_export_main_rejects_names_and_all_lists(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    with pytest.raises(SystemExit) as exc:
        export_main(["announce", "--all-lists", "-o", str(tmp_path / "x.jsonl"), "--db", str(src)])
    assert exc.value.code == 2


def test_export_main_rejects_neither(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    with pytest.raises(SystemExit) as exc:
        export_main(["-o", str(tmp_path / "x.jsonl"), "--db", str(src)])
    assert exc.value.code == 2


def test_import_main_happy_path(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli.jsonl"
    export_main(["--all-lists", "-o", str(out), "--db", str(src)])

    target = tmp_path / "target.db"
    rc = import_main([str(out), "--db", str(target)])
    assert rc == 0
    with Store(target) as t, Store(src) as s:
        assert _messages_by_key(t) == _messages_by_key(s)


def test_import_main_dry_run_writes_nothing(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli.jsonl"
    export_main(["--all-lists", "-o", str(out), "--db", str(src)])

    target = tmp_path / "target.db"
    rc = import_main([str(out), "--db", str(target), "--dry-run"])
    assert rc == 0
    with Store(target) as t:
        assert _counts(t)["messages"] == 0


def test_import_main_returns_1_on_corrupt_file(tmp_path):
    bad = tmp_path / "corrupt.jsonl"
    bad.write_text('{"type": "list", "folder": "x"}\n', encoding="utf-8")  # no header
    target = tmp_path / "target.db"
    rc = import_main([str(bad), "--db", str(target)])
    assert rc == 1
    with Store(target) as t:
        assert _counts(t)["messages"] == 0
