"""Tests for the reply-timing analysis (``messages.timing``).

Covers the rate classification bands, every not-computable case,
In-Reply-To normalization, same-list parent preference, recompute
idempotence, the migration 009 Python backfill, the query/summary
plumbing, and the pipeline stages that trigger a recompute.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest
from conftest import FakeFolder, FakeImapConn, make_raw

from mailing_list_ai_check import cli, export_import
from mailing_list_ai_check.fetcher import DepthMode, FetchRequest, run_fetch
from mailing_list_ai_check.imap_client import ImapClient
from mailing_list_ai_check.staleness import reextract
from mailing_list_ai_check.store import (
    MIGRATIONS,
    MessageFilters,
    Store,
    classify_timing,
)

T0 = "2026-05-01T10:00:00+00:00"
T0_PLUS_10M = "2026-05-01T10:10:00+00:00"
T0_MINUS_10M = "2026-05-01T09:50:00+00:00"


@pytest.fixture()
def store():
    with Store(":memory:") as s:
        yield s


def _list(store: Store, name: str = "l1") -> int:
    return store.upsert_list(name, f"Shared Folders/{name}").id


def _message(
    store: Store,
    list_id: int,
    mid: str,
    date: str | None,
    *,
    in_reply_to: str | None = None,
    chars: int | None = None,
    status: str = "ok",
    raw_body: str = "body",
) -> int:
    msg = store.upsert_message(
        message_id=mid,
        list_id=list_id,
        address_id=None,
        subject="subject",
        date=date,
        in_reply_to=in_reply_to,
        raw_body=raw_body,
        uid=None,
    ).message
    if chars is not None:
        store.insert_extraction(
            message_id=msg.id, extracted_text="x" * chars, method="test", status=status
        )
    return msg.id


def _timing(store: Store, pk: int) -> str | None:
    message = store.get_message(pk)
    assert message is not None
    return message.timing


# --- classification bands -------------------------------------------------------


def test_classify_timing_boundaries():
    assert classify_timing(250, 60) == "implausible"  # exactly 250 chars/min
    assert classify_timing(249, 60) == "suspicious"
    assert classify_timing(100, 60) == "suspicious"  # exactly 100 chars/min
    assert classify_timing(99, 60) == "normal"
    assert classify_timing(0, 60) == "normal"


def test_recompute_classifies_each_band(store):
    lid = _list(store)
    _message(store, lid, "<p@x>", T0)
    fast = _message(store, lid, "<r1@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
    brisk = _message(store, lid, "<r2@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=1500)
    slow = _message(store, lid, "<r3@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=500)

    store.recompute_timing()

    assert _timing(store, fast) == "implausible"  # 300 chars/min
    assert _timing(store, brisk) == "suspicious"  # 150 chars/min
    assert _timing(store, slow) == "normal"  # 50 chars/min


def test_too_short_extraction_is_classified(store):
    lid = _list(store)
    _message(store, lid, "<p@x>", T0)
    reply = _message(
        store,
        lid,
        "<r@x>",
        "2026-05-01T10:00:05+00:00",  # 40 chars in 5 seconds = 480 chars/min
        in_reply_to="<p@x>",
        chars=40,
        status="too_short",
    )
    store.recompute_timing()
    assert _timing(store, reply) == "implausible"


# --- not-computable cases -> NULL ------------------------------------------------


def test_non_reply_is_null(store):
    lid = _list(store)
    pk = _message(store, lid, "<m@x>", T0, chars=1000)
    store.recompute_timing()
    assert _timing(store, pk) is None


def test_missing_parent_is_null(store):
    lid = _list(store)
    pk = _message(store, lid, "<r@x>", T0_PLUS_10M, in_reply_to="<gone@x>", chars=1000)
    store.recompute_timing()
    assert _timing(store, pk) is None


def test_self_reply_is_null(store):
    lid = _list(store)
    pk = _message(store, lid, "<r@x>", T0_PLUS_10M, in_reply_to="<r@x>", chars=1000)
    store.recompute_timing()
    assert _timing(store, pk) is None


def test_non_positive_gap_is_null(store):
    lid = _list(store)
    _message(store, lid, "<p@x>", T0)
    before = _message(store, lid, "<r1@x>", T0_MINUS_10M, in_reply_to="<p@x>", chars=1000)
    same = _message(store, lid, "<r2@x>", T0, in_reply_to="<p@x>", chars=1000)
    store.recompute_timing()
    assert _timing(store, before) is None
    assert _timing(store, same) is None


def test_unusable_extraction_is_null(store):
    lid = _list(store)
    _message(store, lid, "<p@x>", T0)
    empty = _message(
        store, lid, "<r1@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=0, status="empty"
    )
    failed = _message(
        store, lid, "<r2@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=0, status="failed"
    )
    none = _message(store, lid, "<r3@x>", T0_PLUS_10M, in_reply_to="<p@x>")
    store.recompute_timing()
    assert _timing(store, empty) is None
    assert _timing(store, failed) is None
    assert _timing(store, none) is None


def test_unparsable_dates_are_null(store):
    lid = _list(store)
    _message(store, lid, "<p@x>", "not-a-date")
    bad_parent = _message(store, lid, "<r1@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=1000)
    _message(store, lid, "<p2@x>", T0)
    bad_reply = _message(store, lid, "<r2@x>", "not-a-date", in_reply_to="<p2@x>", chars=1000)
    store.recompute_timing()
    assert _timing(store, bad_parent) is None
    assert _timing(store, bad_reply) is None


def test_naive_dates_are_taken_as_utc(store):
    lid = _list(store)
    _message(store, lid, "<p@x>", "2026-05-01T10:00:00")
    reply = _message(store, lid, "<r@x>", "2026-05-01T10:10:00", in_reply_to="<p@x>", chars=3000)
    store.recompute_timing()
    assert _timing(store, reply) == "implausible"


# --- parent resolution ------------------------------------------------------------


def test_in_reply_to_header_is_normalized(store):
    lid = _list(store)
    _message(store, lid, "<p@x>", T0)
    reply = _message(
        store,
        lid,
        "<r@x>",
        T0_PLUS_10M,
        in_reply_to="  (comment) <p@x> <other@x>  ",
        chars=3000,
    )
    store.recompute_timing()
    assert _timing(store, reply) == "implausible"


def test_same_list_parent_copy_preferred(store):
    l1 = _list(store, "l1")
    l2 = _list(store, "l2")
    # The same Message-ID on both lists with different dates; the reply is on
    # l2, so the l2 copy's date must win (70 min gap -> normal, not the l1
    # copy's 10 min gap -> implausible).
    _message(store, l1, "<p@x>", T0)
    _message(store, l2, "<p@x>", "2026-05-01T09:00:00+00:00")
    reply = _message(store, l2, "<r@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
    store.recompute_timing()
    assert _timing(store, reply) == "normal"


# --- recompute behaviour ------------------------------------------------------------


def test_recompute_is_idempotent_and_reports_changes(store):
    lid = _list(store)
    _message(store, lid, "<p@x>", T0)
    _message(store, lid, "<r@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
    assert store.recompute_timing() == 1
    assert store.recompute_timing() == 0


def test_late_arriving_parent_fills_in_timing(store):
    lid = _list(store)
    reply = _message(store, lid, "<r@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
    store.recompute_timing()
    assert _timing(store, reply) is None
    _message(store, lid, "<p@x>", T0)
    assert store.recompute_timing() == 1
    assert _timing(store, reply) == "implausible"


# --- migration backfill ------------------------------------------------------------


def test_migration_009_backfills_existing_rows(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for version, script in MIGRATIONS:
        if version > 8:
            break
        conn.executescript(script)
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
    conn.execute("INSERT INTO lists(id, name, folder) VALUES (1, 'l1', 'f1')")
    conn.execute(
        "INSERT INTO messages(id, message_id, list_id, date, in_reply_to, fetched_at) "
        "VALUES (1, '<p@x>', 1, ?, NULL, ?)",
        (T0, T0),
    )
    conn.execute(
        "INSERT INTO messages(id, message_id, list_id, date, in_reply_to, fetched_at) "
        "VALUES (2, '<r@x>', 1, ?, '<p@x>', ?)",
        (T0_PLUS_10M, T0),
    )
    conn.execute(
        "INSERT INTO extractions(message_id, extracted_text, method, char_count, status, "
        "created_at) VALUES (2, ?, 'test', 3000, 'ok', ?)",
        ("x" * 3000, T0),
    )
    conn.commit()
    conn.close()

    with Store(path) as store:
        assert _timing(store, 1) is None
        assert _timing(store, 2) == "implausible"


# --- query / summary plumbing --------------------------------------------------------


def test_timing_filter_and_summary_distribution(store):
    lid = _list(store)
    _message(store, lid, "<p@x>", T0)
    _message(store, lid, "<r1@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
    _message(store, lid, "<r2@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=1500)
    _message(store, lid, "<r3@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=500)
    store.recompute_timing()

    rows, total = store.query_messages(MessageFilters(timing="implausible"))
    assert total == 1
    assert rows[0]["message_id"] == "<r1@x>"
    assert rows[0]["timing"] == "implausible"

    summary = store.summary(MessageFilters())
    assert summary["timing_distribution"] == {"implausible": 1, "suspicious": 1, "normal": 1}


# --- pipeline hooks -------------------------------------------------------------------

#: ~6000 characters of unquoted prose. Over a 10-minute gap that implies about
#: 600 chars/minute, far enough above the implausible threshold that the exact
#: extracted length does not matter.
LONG_BODY = "The working group should adopt this draft as written. " * 115


def _fake_client(date: str = "Fri, 01 May 2026 10:00:00 +0000") -> ImapClient:
    """A one-message folder holding the parent of the seeded reply."""
    folder = FakeFolder(uidvalidity=1000, uidnext=2, exists=1)
    folder.messages[1] = make_raw(message_id="<p@x>", date=date)
    folder.dates[1] = datetime(2026, 5, 1, 10, 0, 0)
    folder.froms[1] = "alice@example.org"
    return ImapClient(FakeImapConn(folders={"Shared Folders/t": folder}))


def _fetch_request(*, dry_run: bool) -> FetchRequest:
    return FetchRequest(
        folders=("Shared Folders/t",),
        depth=DepthMode(count=10),  # the CLAUDE.md testing cap
        from_filters=(),
        limit=10,
        dry_run=dry_run,
        batch_size=200,
    )


def test_run_extract_classifies_the_replies_it_extracts(store):
    lid = _list(store)
    _message(store, lid, "<p@x>", T0, raw_body="The original proposal.")
    reply = _message(store, lid, "<r@x>", T0_PLUS_10M, in_reply_to="<p@x>", raw_body=LONG_BODY)
    assert _timing(store, reply) is None  # no extraction to time yet
    cli.run_extract(store)
    assert _timing(store, reply) == "implausible"


def test_reextract_reclassifies_a_rewritten_extraction(store):
    lid = _list(store)
    _message(store, lid, "<p@x>", T0, raw_body="The original proposal.")
    reply = _message(store, lid, "<r@x>", T0_PLUS_10M, in_reply_to="<p@x>", raw_body=LONG_BODY)
    # A short extraction from an older routine: normal until it is re-derived
    # from the much longer stored body.
    store.insert_extraction(
        message_id=reply,
        extracted_text="short",
        method="old",
        status="ok",
        pipeline_version="1.0.0",
    )
    store.recompute_timing()
    assert _timing(store, reply) == "normal"

    assert reextract(store, [reply]).rewritten == 1
    assert _timing(store, reply) == "implausible"


def test_run_fetch_classifies_when_the_parent_arrives(tmp_path):
    with Store(tmp_path / "fetch.db") as store:
        lid = _list(store, "t")
        reply = _message(store, lid, "<r@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
        assert _timing(store, reply) is None
        run_fetch(_fake_client(), store, _fetch_request(dry_run=False))
        assert _timing(store, reply) == "implausible"


def test_run_fetch_dry_run_does_not_classify(tmp_path):
    with Store(tmp_path / "dry.db") as store:
        lid = _list(store, "t")
        # The parent is already stored, so a recompute would classify the reply
        # immediately; a dry run must leave the column untouched.
        _message(store, lid, "<p@x>", T0)
        reply = _message(store, lid, "<r@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
        run_fetch(_fake_client(), store, _fetch_request(dry_run=True))
        assert _timing(store, reply) is None


def test_import_classifies_the_imported_messages(tmp_path):
    # timing is derived, so it is not carried in the export file; the importer
    # recomputes it in the target instead.
    out = tmp_path / "export.jsonl"
    with Store(tmp_path / "source.db") as source:
        lid = _list(source, "t")
        _message(source, lid, "<p@x>", T0)
        _message(source, lid, "<r@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
        export_import.export_lists(source, None, out, all_lists=True)

    with Store(tmp_path / "target.db") as target:
        export_import.import_file(target, out)
        reply = target.find_message_by_message_id("<r@x>")
        assert _timing(target, reply.id) == "implausible"
