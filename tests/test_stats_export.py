"""Tests for the stats export (:mod:`mailing_list_ai_check.stats_export`).

Written against ``docs/stats-export.md``: a zip archive of five members —
``messages.csv`` (one row per message in scope, scored or not), the
``lists.csv`` / ``senders.csv`` aggregates over the identical scope,
``manifest.json`` and a data-dictionary ``README.md`` — in two variants, an
identified one and a pseudonymous one whose identity columns are absent
altogether.

The archive is an analysis artifact: nothing reads it back, so the assertions
here are what an analyst would check. The aggregates are recomputed from
``messages.csv`` and compared with the aggregate members, the row counts with
the manifest, and the selected messages with the full export's for the same
range.

Fixtures are built through the public :class:`Store` API. The source database
covers: two lists; a person posting from two addresses; two unlinked addresses,
one of them never extracted; a message with no sender address; a reply whose
parent is in the export and one whose parent is not; extractions that are
scored, unscored and gated under the reliability floor; two detector versions
and two extraction generations.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path

import pytest

from mailing_list_ai_check import __version__, codec, export_import
from mailing_list_ai_check.cli import stats_export_main
from mailing_list_ai_check.stats_export import (
    LISTS_MEMBER,
    MANIFEST_MEMBER,
    MESSAGES_MEMBER,
    README_MEMBER,
    SENDERS_MEMBER,
    STATS_FORMAT_NAME,
    STATS_FORMAT_VERSION,
    export_stats,
)
from mailing_list_ai_check.store import Store, ai_share, sha256_text

# --- fixture data -------------------------------------------------------------

ANNOUNCE = "Shared Folders/announce"
LAST_CALL = "Shared Folders/last-call"

# Message natural-key ids, in the order they are inserted and therefore emitted,
# so <mN@example.org> carries the message key "mN".
M1 = "<m1@example.org>"  # announce, Alice (person), scored AI
M2 = "<m2@example.org>"  # announce, Bob, reply to M1, scored Human
M3 = "<m3@example.org>"  # announce, no sender address, extracted but unscored
M4 = "<m4@example.org>"  # announce, Carol, auto-generated, never extracted
M5 = "<m5@example.org>"  # announce, Alice's second address, too_short
M6 = "<m6@example.org>"  # last-call, Alice, reply to a parent outside the export

# In-Reply-To as it can arrive: surrounded by whitespace and trailing CFWS. The
# parent lookup normalises it exactly as the reply-timing recompute does.
M2_IN_REPLY_TO = f"  {M1} (the original)"
OUTSIDE = "<outside@example.org>"


def _build_source(store: Store) -> dict[str, int]:
    """Populate ``store`` with the stats fixture; return the ids tests need."""
    announce = store.upsert_list("announce", ANNOUNCE)
    last_call = store.upsert_list("last-call", LAST_CALL)

    person = store.create_person("Alice Smith")
    alice = store.upsert_address("alice@example.org", "Alice Smith")
    alice_work = store.upsert_address("alice@work.example", "Alice Smith")
    bob = store.upsert_address("bob@example.org", "Bob Jones")
    carol = store.upsert_address("carol@example.org", "Carol")
    store.upsert_address("dave@example.org", "Dave")  # no messages -> not a sender
    store.assign_address_to_person(alice.id, person.id)
    store.assign_address_to_person(alice_work.id, person.id)

    m1 = store.upsert_message(
        message_id=M1,
        list_id=announce.id,
        address_id=alice.id,
        subject="Intro",
        date="2026-01-05T10:00:00+00:00",
        in_reply_to=None,
        raw_body="Body one",
        uid=101,
        # Deliberately not the address's "Alice Smith": the per-message From name
        # is what the sender_name column reports when there is one.
        from_name="Alice A. Smith",
    ).message
    e1 = store.insert_extraction(
        message_id=m1.id,
        extracted_text="Body one, extracted",
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
        raw_response={"prediction_short": "AI"},
        scored_at="2026-01-07T00:00:00+00:00",
    )

    m2 = store.upsert_message(
        message_id=M2,
        list_id=announce.id,
        address_id=bob.id,
        subject="Re: Intro",
        date="2026-01-10T10:00:00+00:00",
        in_reply_to=M2_IN_REPLY_TO,
        raw_body="Body two",
        uid=102,
    ).message
    e2 = store.insert_extraction(
        message_id=m2.id,
        extracted_text="Body two, extracted",
        method="reply_parser",
        status="ok",
        created_at="2026-01-11T01:00:00+00:00",
    )
    store.insert_score(
        extraction_id=e2.id,
        text_sha256=sha256_text("cleaned two"),
        fraction_ai=0.02,
        fraction_ai_assisted=0.01,
        fraction_human=0.97,
        label="Human",
        detector_version="4.0.0",
        raw_response=None,
        scored_at="2026-01-12T00:00:00+00:00",
    )

    m3 = store.upsert_message(
        message_id=M3,
        list_id=announce.id,
        address_id=None,
        subject="No sender",
        date="2026-01-15T10:00:00+00:00",
        in_reply_to=None,
        raw_body="Body three",
        uid=103,
    ).message
    store.insert_extraction(
        message_id=m3.id,
        extracted_text="Body three, extracted",
        method="custom",
        status="ok",
        created_at="2026-01-16T01:00:00+00:00",
    )

    store.upsert_message(
        message_id=M4,
        list_id=announce.id,
        address_id=carol.id,
        subject="Automated notice",
        date="2026-01-20T10:00:00+00:00",
        in_reply_to=None,
        raw_body="Body four",
        uid=104,
        auto_generated="auto-submitted",
    )

    m5 = store.upsert_message(
        message_id=M5,
        list_id=announce.id,
        address_id=alice_work.id,
        subject="Short note",
        date="2026-01-25T10:00:00+00:00",
        in_reply_to=None,
        raw_body="Body five",
        uid=105,
    ).message
    store.insert_extraction(
        message_id=m5.id,
        extracted_text="tiny",
        method="reply_parser",
        status="too_short",
        created_at="2026-01-26T01:00:00+00:00",
        # An older generation, so the file carries two of them.
        extraction_version=1,
    )

    m6 = store.upsert_message(
        message_id=M6,
        list_id=last_call.id,
        address_id=alice.id,
        subject="Re: something else",
        date="2026-02-01T10:00:00+00:00",
        in_reply_to=OUTSIDE,
        raw_body="Body six",
        uid=201,
    ).message
    e6 = store.insert_extraction(
        message_id=m6.id,
        extracted_text="Body six, extracted",
        method="reply_parser",
        status="ok",
        created_at="2026-02-02T01:00:00+00:00",
    )
    store.insert_score(
        extraction_id=e6.id,
        text_sha256=sha256_text("cleaned six"),
        fraction_ai=0.55,
        fraction_ai_assisted=0.30,
        fraction_human=0.15,
        label="Mixed",
        detector_version="4.0.0",
        raw_response=None,
        scored_at="2026-02-03T00:00:00+00:00",
    )

    # The timing columns are derived, not written per message: M2 is the only
    # reply whose parent is stored, so it is the only row with a band.
    store.recompute_timing()

    return {
        "person": person.id,
        "alice": alice.id,
        "alice_work": alice_work.id,
        "bob": bob.id,
        "carol": carol.id,
    }


@pytest.fixture
def source():
    """An in-memory Store populated with the stats fixture."""
    with Store(":memory:") as s:
        s.ids = _build_source(s)  # type: ignore[attr-defined]
        yield s


# --- archive helpers ------------------------------------------------------------


def _export(store: Store, tmp_path: Path, name: str = "stats", **kwargs) -> Path:
    """Export every list unless ``kwargs`` says otherwise; return the path written."""
    kwargs.setdefault("all_lists", True)
    list_names = kwargs.pop("list_names", None)
    summary = export_stats(store, list_names, tmp_path / name, **kwargs)
    return Path(summary.path)


def _member_bytes(path: Path, member: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(member)


def _rows(path: Path, member: str) -> list[dict[str, str]]:
    """Read one CSV member with the stdlib reader, as a consumer would."""
    text = io.StringIO(_member_bytes(path, member).decode("utf-8"), newline="")
    return list(csv.DictReader(text))


def _columns(path: Path, member: str) -> list[str]:
    text = io.StringIO(_member_bytes(path, member).decode("utf-8"), newline="")
    return next(csv.reader(text))


def _manifest(path: Path) -> dict:
    return json.loads(_member_bytes(path, MANIFEST_MEMBER).decode("utf-8"))


def _by_key(rows: list[dict[str, str]], column: str = "message_key") -> dict[str, dict[str, str]]:
    return {row[column]: row for row in rows}


def _recomputed(rows: list[dict[str, str]], column: str) -> dict[str, dict[str, object]]:
    """The aggregates ``lists.csv`` / ``senders.csv`` hold, recomputed from the rows.

    Grouped by ``column`` (``folder`` or ``sender_key``), counting exactly what
    the spec defines each aggregate column to be, so a mismatch is a bug in the
    export rather than a restatement of its own arithmetic.
    """
    groups: dict[str, dict[str, object]] = {}
    for row in rows:
        key = row[column]
        if not key:
            continue  # a message with no sender address belongs to no sender
        group = groups.setdefault(
            key,
            {"messages": 0, "scored": 0, "too_short": 0, "human": 0, "mixed": 0, "ai": 0},
        )
        group["messages"] += 1
        if row["label"]:
            group["scored"] += 1
            group[row["label"].lower()] += 1
        if row["extraction_status"] == "too_short":
            group["too_short"] += 1
        dates = [d for d in (group.get("first_date"), row["date"]) if d]
        group["first_date"] = min(dates) if dates else ""
        dates = [d for d in (group.get("last_date"), row["date"]) if d]
        group["last_date"] = max(dates) if dates else ""
    return groups


def _as_counts(row: dict[str, str]) -> dict[str, object]:
    """One aggregate CSV row as the shape :func:`_recomputed` produces."""
    counts: dict[str, object] = {
        name: int(row[name]) for name in ("messages", "scored", "too_short", "human", "mixed", "ai")
    }
    counts["first_date"] = row["first_date"]
    counts["last_date"] = row["last_date"]
    return counts


# ==============================================================================
# ARCHIVE SHAPE
# ==============================================================================


def test_archive_holds_exactly_the_five_members(source, tmp_path):
    path = _export(source, tmp_path)
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {
            MESSAGES_MEMBER,
            LISTS_MEMBER,
            SENDERS_MEMBER,
            MANIFEST_MEMBER,
            README_MEMBER,
        }


def test_zip_suffix_is_appended_once(source, tmp_path):
    """The summary reports the path actually written, whatever it was handed."""
    plain = export_stats(source, None, tmp_path / "stats", all_lists=True)
    already = export_stats(source, None, tmp_path / "other.zip", all_lists=True)
    assert plain.path == str(tmp_path / "stats.zip")
    assert already.path == str(tmp_path / "other.zip")
    assert Path(plain.path).exists() and Path(already.path).exists()


def test_summary_counts_the_export(source, tmp_path):
    summary = export_stats(source, None, tmp_path / "stats", all_lists=True)
    assert (summary.lists, summary.senders, summary.messages, summary.scored) == (2, 3, 6, 3)
    assert summary.as_line().startswith("lists=2 senders=3 messages=6 scored=3 path=")


def test_manifest_matches_the_csv_row_counts(source, tmp_path):
    path = _export(source, tmp_path)
    manifest = _manifest(path)
    assert manifest["rows"] == {
        "messages": len(_rows(path, MESSAGES_MEMBER)),
        "lists": len(_rows(path, LISTS_MEMBER)),
        "senders": len(_rows(path, SENDERS_MEMBER)),
    }
    assert manifest["rows"] == {"messages": 6, "lists": 2, "senders": 3}


def test_manifest_records_provenance_and_the_values_present(source, tmp_path):
    path = _export(source, tmp_path)
    manifest = _manifest(path)
    assert manifest["format"] == STATS_FORMAT_NAME
    assert manifest["stats_format_version"] == STATS_FORMAT_VERSION
    assert manifest["app_version"] == __version__
    assert manifest["schema_version"] > 0
    assert manifest["identified"] is True
    assert manifest["folders"] == [ANNOUNCE, LAST_CALL]
    assert manifest["labels"] == ["Human", "Mixed", "AI"]
    assert manifest["timing_bands"] == ["normal", "suspicious", "implausible"]
    # Only the versions actually present in the file, not everything the app knows.
    assert manifest["detector_versions"] == ["3.3.2", "4.0.0"]
    assert manifest["extraction_versions"] == [1, 2]
    assert "date_from" not in manifest and "date_to" not in manifest


def test_manifest_records_the_range_only_when_bounded(source, tmp_path):
    path = _export(source, tmp_path, date_from="2026-01-15", date_to="2026-02-01T23:59:59+00:00")
    manifest = _manifest(path)
    assert manifest["date_from"] == "2026-01-15"
    assert manifest["date_to"] == "2026-02-01T23:59:59+00:00"


def test_csv_conventions(source, tmp_path):
    """UTF-8, a header row, ``\\n`` line endings, NULL as an empty field."""
    path = _export(source, tmp_path)
    raw = _member_bytes(path, MESSAGES_MEMBER)
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")
    assert raw.decode("utf-8").splitlines()[0].startswith("message_key,list,folder,date,sender_key")

    rows = _by_key(_rows(path, MESSAGES_MEMBER))
    # Never extracted, never scored: every derived column is empty, not "None".
    assert rows["m4"]["label"] == ""
    assert rows["m4"]["extraction_status"] == ""
    assert rows["m4"]["fraction_ai"] == ""
    # Booleans are words, not Python repr.
    assert {row["is_reply"] for row in rows.values()} == {"true", "false"}


def test_readme_documents_every_column_it_ships(source, tmp_path):
    path = _export(source, tmp_path)
    readme = _member_bytes(path, README_MEMBER).decode("utf-8")
    for member in (MESSAGES_MEMBER, LISTS_MEMBER, SENDERS_MEMBER):
        for column in _columns(path, member):
            assert f"`{column}`" in readme, f"{member}.{column} is undocumented"


# ==============================================================================
# messages.csv
# ==============================================================================


def test_messages_csv_holds_every_message_in_scope(source, tmp_path):
    """Unscored and gated messages are rows too: a share needs its denominator."""
    path = _export(source, tmp_path)
    rows = _rows(path, MESSAGES_MEMBER)
    stored = source.conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
    assert len(rows) == stored == 6
    assert [row["message_id"] for row in rows] == [M1, M2, M3, M4, M5, M6]
    # Keys are dense and in emission order.
    assert [row["message_key"] for row in rows] == [f"m{n}" for n in range(1, 7)]


def test_message_rows_equal_the_stored_values(source, tmp_path):
    path = _export(source, tmp_path)
    rows = _by_key(_rows(path, MESSAGES_MEMBER))

    stored = source.conn.execute(
        "SELECT l.name AS list, l.folder AS folder, m.message_id AS message_id, "
        "m.date AS date, m.in_reply_to AS in_reply_to, m.auto_generated AS auto_generated, "
        "m.timing AS timing, m.pipeline_version AS pipeline_version, "
        "e.status AS status, e.method AS method, e.char_count AS char_count, "
        "e.extraction_version AS extraction_version, sc.label AS label, "
        "sc.fraction_ai AS fraction_ai, sc.detector_version AS detector_version, "
        "sc.scored_at AS scored_at "
        "FROM messages m JOIN lists l ON l.id = m.list_id "
        "LEFT JOIN extractions e ON e.message_id = m.id "
        "LEFT JOIN scores sc ON sc.extraction_id = e.id ORDER BY m.list_id, m.id"
    ).fetchall()

    def blank(value):
        return "" if value is None else str(value)

    for row, want in zip(rows.values(), stored, strict=True):
        assert row["list"] == want["list"]
        assert row["folder"] == want["folder"]
        assert row["message_id"] == want["message_id"]
        assert row["date"] == blank(want["date"])
        assert row["in_reply_to"] == blank(want["in_reply_to"])
        assert row["auto_generated"] == blank(want["auto_generated"])
        assert row["timing"] == blank(want["timing"])
        assert row["pipeline_version"] == blank(want["pipeline_version"])
        assert row["extraction_status"] == blank(want["status"])
        assert row["extraction_method"] == blank(want["method"])
        assert row["extraction_chars"] == blank(want["char_count"])
        assert row["extraction_version"] == blank(want["extraction_version"])
        assert row["label"] == blank(want["label"])
        assert row["fraction_ai"] == blank(want["fraction_ai"])
        assert row["detector_version"] == blank(want["detector_version"])
        assert row["scored_at"] == blank(want["scored_at"])


def test_fractions_keep_their_stored_precision(source, tmp_path):
    """A third written unrounded, so a consumer's own rounding is the only one."""
    source.conn.execute("UPDATE scores SET fraction_ai = ? WHERE id = 1", (1 / 3,))
    source.conn.commit()
    path = _export(source, tmp_path)
    assert float(_by_key(_rows(path, MESSAGES_MEMBER))["m1"]["fraction_ai"]) == 1 / 3


def test_sender_identity_columns_name_the_message_and_the_address(source, tmp_path):
    path = _export(source, tmp_path)
    rows = _by_key(_rows(path, MESSAGES_MEMBER))
    # The message's own From name wins over the address's display name.
    assert (rows["m1"]["email"], rows["m1"]["sender_name"]) == (
        "alice@example.org",
        "Alice A. Smith",
    )
    # With no From name of its own, the address's display name is reported.
    assert (rows["m2"]["email"], rows["m2"]["sender_name"]) == ("bob@example.org", "Bob Jones")
    # A message with no sender address has neither, and joins to no sender row.
    assert (rows["m3"]["email"], rows["m3"]["sender_name"], rows["m3"]["sender_key"]) == (
        "",
        "",
        "",
    )


def test_timing_columns_come_from_the_stored_bands(source, tmp_path):
    path = _export(source, tmp_path)
    rows = _by_key(_rows(path, MESSAGES_MEMBER))
    # M2 is the only reply whose parent is stored, so the only timed message.
    assert rows["m2"]["timing"] == "normal"
    assert float(rows["m2"]["timing_cpm"]) > 0
    # Empty band and empty rate, together: neither is reported without the other.
    assert (rows["m1"]["timing"], rows["m1"]["timing_cpm"]) == ("", "")


def test_parent_key_links_a_reply_to_its_in_scope_parent(source, tmp_path):
    path = _export(source, tmp_path)
    rows = _by_key(_rows(path, MESSAGES_MEMBER))
    # The In-Reply-To carries whitespace and a comment; the parent resolves anyway.
    assert (rows["m2"]["is_reply"], rows["m2"]["parent_key"]) == ("true", "m1")
    # A reply whose parent is not in the export keeps is_reply and loses the link.
    assert (rows["m6"]["is_reply"], rows["m6"]["parent_key"]) == ("true", "")
    assert (rows["m1"]["is_reply"], rows["m1"]["parent_key"]) == ("false", "")


def test_a_range_that_excludes_the_parent_drops_only_the_link(source, tmp_path):
    path = _export(source, tmp_path, date_from="2026-01-08")
    rows = _by_key(_rows(path, MESSAGES_MEMBER))
    reply = next(row for row in rows.values() if row["message_id"] == M2)
    assert (reply["is_reply"], reply["parent_key"]) == ("true", "")


# ==============================================================================
# lists.csv / senders.csv
# ==============================================================================


def test_list_aggregates_equal_the_message_rows(source, tmp_path):
    path = _export(source, tmp_path)
    recomputed = _recomputed(_rows(path, MESSAGES_MEMBER), "folder")
    rows = {row["folder"]: row for row in _rows(path, LISTS_MEMBER)}
    assert set(rows) == set(recomputed)
    for folder, row in rows.items():
        assert _as_counts(row) == recomputed[folder]
    # And the totals reach the primitive table.
    assert sum(int(row["messages"]) for row in rows.values()) == 6


def test_sender_aggregates_equal_the_message_rows(source, tmp_path):
    path = _export(source, tmp_path)
    messages = _rows(path, MESSAGES_MEMBER)
    recomputed = _recomputed(messages, "sender_key")
    rows = {row["sender_key"]: row for row in _rows(path, SENDERS_MEMBER)}
    assert set(rows) == set(recomputed)
    for key, row in rows.items():
        assert _as_counts(row) == recomputed[key]
    # Every message but the one without a sender address is covered.
    assert sum(int(row["messages"]) for row in rows.values()) == 5


def test_ai_share_matches_the_dashboard_definition(source, tmp_path):
    path = _export(source, tmp_path)
    for member in (LISTS_MEMBER, SENDERS_MEMBER):
        for row in _rows(path, member):
            counts = {label: int(row[label.lower()]) for label in ("Human", "Mixed", "AI")}
            too_short = int(row["too_short"])
            if int(row["scored"]) + too_short == 0:
                assert row["ai_share"] == ""
            else:
                assert float(row["ai_share"]) == ai_share(counts, too_short)
    announce = next(row for row in _rows(path, LISTS_MEMBER) if row["folder"] == ANNOUNCE)
    # 1 AI over 2 scored + 1 gated: the gated message is in the denominator.
    assert float(announce["ai_share"]) == 1 / 3


def test_a_sender_with_nothing_scored_has_an_empty_share(source, tmp_path):
    path = _export(source, tmp_path)
    carol = next(row for row in _rows(path, SENDERS_MEMBER) if row["emails"] == "carol@example.org")
    assert (carol["messages"], carol["scored"], carol["ai_share"]) == ("1", "0", "")


def test_a_person_is_one_sender_across_their_addresses(source, tmp_path):
    path = _export(source, tmp_path)
    rows = {row["sender_key"]: row for row in _rows(path, SENDERS_MEMBER)}
    key = f"p{source.ids['person']}"
    assert rows[key]["sender_type"] == "person"
    assert rows[key]["name"] == "Alice Smith"
    assert rows[key]["emails"] == "alice@example.org;alice@work.example"
    assert rows[key]["messages"] == "3"
    # An unlinked address is its own sender, keyed by its address id.
    assert rows[f"a{source.ids['bob']}"]["sender_type"] == "address"
    # An address with no message in scope is not a sender at all.
    assert not any("dave@example.org" in row["emails"] for row in rows.values())


def test_a_named_list_with_nothing_in_range_is_a_zero_row(source, tmp_path):
    """The file says the list was asked for and held no message."""
    path = _export(
        source, tmp_path, list_names=["announce"], all_lists=False, date_from="2026-02-01"
    )
    rows = _rows(path, LISTS_MEMBER)
    assert len(rows) == 1
    assert rows[0]["folder"] == ANNOUNCE
    assert rows[0]["messages"] == "0"
    assert rows[0]["ai_share"] == ""
    assert (rows[0]["first_date"], rows[0]["last_date"]) == ("", "")
    assert _rows(path, MESSAGES_MEMBER) == []


# ==============================================================================
# PSEUDONYMOUS VARIANT
# ==============================================================================


def test_pseudonymous_export_omits_the_identity_columns(source, tmp_path):
    path = _export(source, tmp_path, name="anon", pseudonymous=True)
    message_columns = _columns(path, MESSAGES_MEMBER)
    sender_columns = _columns(path, SENDERS_MEMBER)
    for column in ("email", "sender_name", "message_id", "in_reply_to"):
        assert column not in message_columns
    for column in ("name", "emails"):
        assert column not in sender_columns
    # Omitted, not blanked: the remaining columns are unchanged and in order.
    assert message_columns[:6] == [
        "message_key",
        "list",
        "folder",
        "date",
        "sender_key",
        "is_reply",
    ]
    assert _manifest(path)["identified"] is False


def test_pseudonymous_export_carries_no_identifying_value(source, tmp_path):
    path = _export(source, tmp_path, name="anon", pseudonymous=True)
    body = (
        _member_bytes(path, MESSAGES_MEMBER)
        + _member_bytes(path, SENDERS_MEMBER)
        + _member_bytes(path, LISTS_MEMBER)
    )
    for secret in (b"alice@example.org", b"Alice", b"bob@example.org", M1.encode(), b"outside"):
        assert secret not in body


def test_pseudonymous_sender_keys_are_dense_and_first_seen(source, tmp_path):
    path = _export(source, tmp_path, name="anon", pseudonymous=True)
    keys = [row["sender_key"] for row in _rows(path, SENDERS_MEMBER)]
    assert keys == ["s1", "s2", "s3"]
    rows = _by_key(_rows(path, MESSAGES_MEMBER))
    # First seen at m1, and the same sender again at m5 from another address.
    assert rows["m1"]["sender_key"] == "s1"
    assert rows["m5"]["sender_key"] == "s1"
    assert rows["m2"]["sender_key"] == "s2"
    assert rows["m3"]["sender_key"] == ""  # no address, so no sender


def test_pseudonymous_export_keeps_thread_shape(source, tmp_path):
    path = _export(source, tmp_path, name="anon", pseudonymous=True)
    rows = _by_key(_rows(path, MESSAGES_MEMBER))
    assert (rows["m2"]["is_reply"], rows["m2"]["parent_key"]) == ("true", "m1")
    assert (rows["m6"]["is_reply"], rows["m6"]["parent_key"]) == ("true", "")


def test_both_variants_count_the_same_messages(source, tmp_path):
    identified = _export(source, tmp_path, name="named")
    pseudonymous = _export(source, tmp_path, name="anon", pseudonymous=True)
    assert _manifest(identified)["rows"] == _manifest(pseudonymous)["rows"]
    for member in (LISTS_MEMBER, SENDERS_MEMBER):
        assert [_as_counts(row) for row in _rows(identified, member)] == [
            _as_counts(row) for row in _rows(pseudonymous, member)
        ]


# ==============================================================================
# SELECTION
# ==============================================================================


def test_named_lists_select_only_their_messages(source, tmp_path):
    path = _export(source, tmp_path, list_names=["last-call"], all_lists=False)
    assert [row["message_id"] for row in _rows(path, MESSAGES_MEMBER)] == [M6]
    assert _manifest(path)["folders"] == [LAST_CALL]


@pytest.mark.parametrize(
    ("names", "all_lists"),
    [(["announce"], True), (None, False)],
    ids=["both", "neither"],
)
def test_selection_needs_names_xor_all_lists(source, tmp_path, names, all_lists):
    with pytest.raises(ValueError):
        export_stats(source, names, tmp_path / "x", all_lists=all_lists)


def test_unknown_list_name_is_an_error(source, tmp_path):
    with pytest.raises(ValueError, match="unknown list name"):
        export_stats(source, ["nope"], tmp_path / "x")


def test_date_range_selects_the_same_messages_as_the_full_export(source, tmp_path):
    """The two exports share their selection, so their ranges cannot drift apart."""
    bounds = {"date_from": "2026-01-15", "date_to": "2026-02-01T23:59:59+00:00"}
    stats = _export(source, tmp_path, **bounds)
    full = export_import.export_lists(
        source, None, tmp_path / "full.jsonl", all_lists=True, **bounds
    )
    with codec.open_read_text(full.path) as fh:
        records = [json.loads(line) for line in fh if line.strip()]

    exported = {row["message_id"] for row in _rows(stats, MESSAGES_MEMBER)}
    assert exported == {r["message_id"] for r in records if r.get("type") == "message"}
    assert exported == {M3, M4, M5, M6}


def test_a_bare_date_to_excludes_that_days_messages(source, tmp_path):
    """The same lexical edge the full export and the dashboard filter have."""
    path = _export(source, tmp_path, date_to="2026-01-20")
    assert {row["message_id"] for row in _rows(path, MESSAGES_MEMBER)} == {M1, M2, M3}


def test_all_lists_skips_a_list_with_nothing_in_range(source, tmp_path):
    path = _export(source, tmp_path, date_to="2026-01-31")
    assert _manifest(path)["folders"] == [ANNOUNCE]
    assert [row["folder"] for row in _rows(path, LISTS_MEMBER)] == [ANNOUNCE]


# ==============================================================================
# CLI
# ==============================================================================


def _build_source_db(path: Path) -> None:
    with Store(path) as store:
        _build_source(store)


def test_stats_export_main_writes_the_archive(tmp_path, caplog):
    import logging

    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli-stats"

    with caplog.at_level(logging.INFO, logger="mailing_list_ai_check.export-stats"):
        rc = stats_export_main(["--all-lists", "-o", str(out), "--db", str(src)])
    assert rc == 0
    written = tmp_path / "cli-stats.zip"
    assert f"path={written}" in caplog.text
    assert len(_rows(written, MESSAGES_MEMBER)) == 6
    assert _manifest(written)["identified"] is True


def test_stats_export_main_pseudonymous_and_ranged(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli-anon.zip"
    rc = stats_export_main(
        [
            "announce",
            "-o",
            str(out),
            "--db",
            str(src),
            "--pseudonymous",
            "--date-from",
            "2026-01-15",
        ]
    )
    assert rc == 0
    assert _manifest(out)["identified"] is False
    assert _manifest(out)["date_from"] == "2026-01-15"
    assert "email" not in _columns(out, MESSAGES_MEMBER)


@pytest.mark.parametrize("flag", ["--date-from", "--date-to"], ids=["from", "to"])
def test_stats_export_main_rejects_an_unparseable_date(tmp_path, flag):
    src = tmp_path / "src.db"
    _build_source_db(src)
    with pytest.raises(SystemExit) as exc:
        stats_export_main(
            ["--all-lists", "-o", str(tmp_path / "x.zip"), "--db", str(src), flag, "last week"]
        )
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["announce", "--all-lists"],
        [],
    ],
    ids=["both", "neither"],
)
def test_stats_export_main_rejects_a_bad_selection(tmp_path, argv):
    src = tmp_path / "src.db"
    _build_source_db(src)
    with pytest.raises(SystemExit) as exc:
        stats_export_main([*argv, "-o", str(tmp_path / "x.zip"), "--db", str(src)])
    assert exc.value.code == 2


def test_stats_export_main_requires_an_output_path(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    with pytest.raises(SystemExit) as exc:
        stats_export_main(["--all-lists", "--db", str(src)])
    assert exc.value.code == 2
