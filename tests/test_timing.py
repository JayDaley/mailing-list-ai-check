"""Tests for the reply-timing analysis (``messages.timing``/``timing_cpm``).

Covers the rate classification bands, every not-computable case,
In-Reply-To normalization, same-list parent preference, recompute
idempotence, the stored rate staying in step with the band, the Python
backfill behind migrations 009 and 010 (including concurrent first opens),
the query/summary plumbing and the chars/minute range filter, and the
pipeline stages that trigger a recompute.
"""

from __future__ import annotations

import sqlite3
import threading
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
    chars_per_minute,
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


def _rate(store: Store, pk: int) -> float | None:
    """The stored ``messages.timing_cpm`` of one message."""
    return store.conn.execute("SELECT timing_cpm FROM messages WHERE id = ?", (pk,)).fetchone()[0]


# --- classification bands -------------------------------------------------------


def test_chars_per_minute_is_the_rate_the_bands_classify():
    assert chars_per_minute(3000, 600) == pytest.approx(300.0)  # 3000 chars in 10 min
    assert chars_per_minute(40, 5) == pytest.approx(480.0)
    assert classify_timing(3000, 600) == "implausible"


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


def test_recompute_stores_the_rate_beside_the_band(store):
    """``timing_cpm`` is exactly the rate the band was classified from."""
    lid = _list(store)
    _message(store, lid, "<p@x>", T0)
    fast = _message(store, lid, "<r1@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
    slow = _message(store, lid, "<r2@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=500)

    store.recompute_timing()

    assert _rate(store, fast) == pytest.approx(chars_per_minute(3000, 600))
    assert _rate(store, slow) == pytest.approx(chars_per_minute(500, 600))
    assert _rate(store, 1) is None  # the parent is not a reply: no band, no rate


def test_band_and_rate_are_written_and_cleared_together(store):
    lid = _list(store)
    parent = _message(store, lid, "<p@x>", T0)
    reply = _message(store, lid, "<r@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
    store.recompute_timing()
    assert _timing(store, reply) == "implausible"
    assert _rate(store, reply) == pytest.approx(300.0)

    # The band stops being computable, so both columns go back to NULL.
    store.conn.execute("DELETE FROM messages WHERE id = ?", (parent,))
    assert store.recompute_timing() == 1
    assert _timing(store, reply) is None
    assert _rate(store, reply) is None


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


def _build_old_db(path, replies: int = 1, *, up_to: int = 8) -> None:
    """A schema-``up_to`` database: one parent plus ``replies`` fast replies.

    Written with raw SQL, so the Python backfill a real upgrade would have run
    (see :meth:`Store.__init__`) has not run over these rows.
    """
    conn = sqlite3.connect(path)
    # The app has opened its database in WAL mode since the first release, so
    # any real pre-upgrade database is WAL; build the fixture the same way.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    for version, script in MIGRATIONS:
        if version > up_to:
            break
        conn.executescript(script)
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
    conn.execute("INSERT INTO lists(id, name, folder) VALUES (1, 'l1', 'f1')")
    conn.execute(
        "INSERT INTO messages(id, message_id, list_id, date, in_reply_to, fetched_at) "
        "VALUES (1, '<p@x>', 1, ?, NULL, ?)",
        (T0, T0),
    )
    conn.executemany(
        "INSERT INTO messages(id, message_id, list_id, date, in_reply_to, fetched_at) "
        "VALUES (?, ?, 1, ?, '<p@x>', ?)",
        [(i + 2, f"<r{i}@x>", T0_PLUS_10M, T0) for i in range(replies)],
    )
    conn.executemany(
        "INSERT INTO extractions(message_id, extracted_text, method, char_count, status, "
        "created_at) VALUES (?, ?, 'test', 3000, 'ok', ?)",
        [(i + 2, "x" * 3000, T0) for i in range(replies)],
    )
    conn.commit()
    conn.close()


def test_migration_009_backfills_existing_rows(tmp_path):
    path = tmp_path / "old.db"
    _build_old_db(path)

    with Store(path) as store:
        assert _timing(store, 1) is None
        assert _timing(store, 2) == "implausible"
        assert _rate(store, 2) == pytest.approx(chars_per_minute(3000, 600))


def test_migration_010_backfills_the_rate_behind_an_existing_band(tmp_path):
    """A version-9 database carries bands but no rates; the open fills them in.

    The backfilled rate must be exactly what :func:`chars_per_minute` returns
    for the inputs the stored band was classified from.
    """
    path = tmp_path / "v9.db"
    _build_old_db(path, up_to=9)
    # Stand in for migration 009's Python backfill, so the fixture looks like a
    # version-9 database that has been in use rather than a freshly rewound one.
    conn = sqlite3.connect(path)
    conn.execute("UPDATE messages SET timing = 'implausible' WHERE id = 2")
    conn.commit()
    conn.close()

    with Store(path) as store:
        assert _timing(store, 2) == "implausible"  # the band is left as it was
        assert _rate(store, 2) == pytest.approx(chars_per_minute(3000, 600))
        assert _timing(store, 1) is None
        assert _rate(store, 1) is None


def test_concurrent_opens_apply_migrations_once(tmp_path):
    """Concurrent first opens after an upgrade must not race the migration.

    The dashboard opens one Store per request and the SPA fires several
    requests in parallel, so the first page load after an upgrade opens the
    out-of-date database from many threads at once. Regression test for the
    read-check-apply race in apply_migrations, whose losing connections threw
    "duplicate column name" (and, without a busy timeout, "database is
    locked") as transient 500s.
    """
    path = tmp_path / "old.db"
    _build_old_db(path, replies=3000)

    thread_count = 6
    barrier = threading.Barrier(thread_count)
    errors: list[Exception] = []

    def open_store() -> None:
        barrier.wait()
        try:
            Store(path).close()
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    with Store(path) as store:
        rows = store.conn.execute(
            "SELECT version, COUNT(*) AS n FROM schema_version GROUP BY version"
        ).fetchall()
        assert {row["version"] for row in rows} == {v for v, _ in MIGRATIONS}
        assert all(row["n"] == 1 for row in rows)
        classified = store.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE timing = 'implausible'"
        ).fetchone()[0]
        assert classified == 3000


# --- query / summary plumbing --------------------------------------------------------


def _three_replies(store: Store) -> int:
    """One parent plus replies implying 300, 150 and 50 chars/minute."""
    lid = _list(store)
    _message(store, lid, "<p@x>", T0)
    _message(store, lid, "<r1@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
    _message(store, lid, "<r2@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=1500)
    _message(store, lid, "<r3@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=500)
    store.recompute_timing()
    return lid


def _matching(store: Store, **filters) -> list[str]:
    """The Message-IDs matching ``filters``, sorted; asserts ``total`` agrees."""
    rows, total = store.query_messages(MessageFilters(**filters))
    assert total == len(rows)
    return sorted(row["message_id"] for row in rows)


def test_summary_distribution_counts_the_bands(store):
    _three_replies(store)
    summary = store.summary(MessageFilters())
    assert summary["timing_distribution"] == {"implausible": 1, "suspicious": 1, "normal": 1}


def test_rate_filter_lower_bound_is_inclusive(store):
    _three_replies(store)
    assert _matching(store, cpm_min=150) == ["<r1@x>", "<r2@x>"]
    assert _matching(store, cpm_min=300) == ["<r1@x>"]
    assert _matching(store, cpm_min=300.5) == []


def test_rate_filter_upper_bound_is_inclusive(store):
    _three_replies(store)
    assert _matching(store, cpm_max=150) == ["<r2@x>", "<r3@x>"]
    assert _matching(store, cpm_max=50) == ["<r3@x>"]
    assert _matching(store, cpm_max=49.5) == []


def test_rate_filter_combines_both_bounds(store):
    _three_replies(store)
    assert _matching(store, cpm_min=50, cpm_max=150) == ["<r2@x>", "<r3@x>"]
    assert _matching(store, cpm_min=60, cpm_max=200) == ["<r2@x>"]
    assert _matching(store, cpm_min=200, cpm_max=100) == []  # empty, not an error


def test_rate_filter_excludes_messages_with_no_rate(store):
    """Either bound drops every message whose rate is not computable."""
    lid = _three_replies(store)
    _message(store, lid, "<r4@x>", T0_PLUS_10M, in_reply_to="<gone@x>", chars=1000)
    store.recompute_timing()

    # Unfiltered, the parent and the parentless reply are in the result set.
    assert _matching(store) == ["<p@x>", "<r1@x>", "<r2@x>", "<r3@x>", "<r4@x>"]
    assert _matching(store, cpm_min=0) == ["<r1@x>", "<r2@x>", "<r3@x>"]
    assert _matching(store, cpm_max=1000) == ["<r1@x>", "<r2@x>", "<r3@x>"]


def test_rate_filter_applies_before_pagination(store):
    """The filter runs in SQL, so the count and the pages span every match."""
    _three_replies(store)
    rows, total = store.query_messages(MessageFilters(cpm_min=0, per_page=2, page=1))
    assert (total, len(rows)) == (3, 2)
    rows, total = store.query_messages(MessageFilters(cpm_min=0, per_page=2, page=2))
    assert (total, len(rows)) == (3, 1)


def _cpm_by_message_id(store: Store, filters: MessageFilters | None = None) -> dict[str, float]:
    rows, _ = store.query_messages(filters or MessageFilters())
    return {row["message_id"]: row["timing_cpm"] for row in rows}


def test_query_rows_carry_the_rate_behind_the_band(store):
    lid = _list(store)
    _message(store, lid, "<p@x>", T0)
    _message(store, lid, "<r1@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
    _message(store, lid, "<r2@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=500)
    store.recompute_timing()

    rates = _cpm_by_message_id(store)
    assert rates["<r1@x>"] == pytest.approx(300.0)  # 3000 chars / 10 min
    assert rates["<r2@x>"] == pytest.approx(50.0)
    assert rates["<p@x>"] is None  # not a reply: no band, so no rate


def test_query_rate_is_null_wherever_the_band_is(store):
    lid = _list(store)
    _message(store, lid, "<p@x>", T0)
    _message(store, lid, "<r1@x>", T0_PLUS_10M, in_reply_to="<gone@x>", chars=1000)
    _message(store, lid, "<r2@x>", T0_MINUS_10M, in_reply_to="<p@x>", chars=1000)
    _message(store, lid, "<r3@x>", T0_PLUS_10M, in_reply_to="<p@x>")
    store.recompute_timing()

    rates = _cpm_by_message_id(store)
    assert rates["<r1@x>"] is None  # parent not stored
    assert rates["<r2@x>"] is None  # non-positive gap
    assert rates["<r3@x>"] is None  # no extraction


def test_query_rate_resolves_the_parent_as_the_classification_does(store):
    """Header normalization and the same-list preference, as in recompute_timing."""
    l1 = _list(store, "l1")
    l2 = _list(store, "l2")
    _message(store, l1, "<p@x>", T0)
    _message(store, l2, "<p@x>", "2026-05-01T09:00:00+00:00")
    _message(
        store,
        l2,
        "<r@x>",
        T0_PLUS_10M,
        in_reply_to="  (comment) <p@x> <other@x>  ",
        chars=3500,
    )
    store.recompute_timing()

    # The l2 copy of the parent is 70 minutes before the reply, the l1 copy 10.
    assert _cpm_by_message_id(store)["<r@x>"] == pytest.approx(50.0)


def test_query_serves_the_stored_rate_rather_than_recomputing(store):
    """The row reports what is stored, so a lost parent shows until a recompute."""
    lid = _list(store)
    parent = _message(store, lid, "<p@x>", T0)
    _message(store, lid, "<r@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
    store.recompute_timing()
    store.conn.execute("DELETE FROM messages WHERE id = ?", (parent,))

    rows, _ = store.query_messages(MessageFilters())
    assert [row["timing"] for row in rows] == ["implausible"]
    assert rows[0]["timing_cpm"] == pytest.approx(300.0)

    store.recompute_timing()
    rows, _ = store.query_messages(MessageFilters())
    assert rows[0]["timing"] is None
    assert rows[0]["timing_cpm"] is None


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
    with Store(tmp_path / "source.db") as source:
        lid = _list(source, "t")
        _message(source, lid, "<p@x>", T0)
        _message(source, lid, "<r@x>", T0_PLUS_10M, in_reply_to="<p@x>", chars=3000)
        # The exporter compresses and appends '.zst', so the summary reports the
        # path actually written.
        out = export_import.export_lists(
            source, None, tmp_path / "export.jsonl", all_lists=True
        ).path

    with Store(tmp_path / "target.db") as target:
        export_import.import_file(target, out)
        reply = target.find_message_by_message_id("<r@x>")
        assert _timing(target, reply.id) == "implausible"
