"""Unit tests for fetch orchestration and RFC 5322 parsing (no network)."""

from __future__ import annotations

from datetime import datetime

from conftest import FakeFolder, FakeImapConn, make_raw

from mailing_list_ai_check.fetcher import (
    DepthMode,
    FetchRequest,
    compute_uids,
    folder_for_list,
    iso_to_imap_date,
    list_name_for_folder,
    parse_header,
    parse_message,
    refresh_lists_index,
    resolve_folders,
    run_fetch,
    run_fetch_uids,
)
from mailing_list_ai_check.imap_client import ImapClient
from mailing_list_ai_check.store import Store


# --- date helper --------------------------------------------------------------


def test_iso_to_imap_date():
    assert iso_to_imap_date("2025-01-06") == "06-Jan-2025"
    assert iso_to_imap_date("2025-12-31") == "31-Dec-2025"


# --- folder mapping -----------------------------------------------------------


def test_folder_and_list_name_roundtrip():
    assert folder_for_list("announce") == "Shared Folders/announce"
    assert folder_for_list("Shared Folders/announce") == "Shared Folders/announce"
    assert list_name_for_folder("Shared Folders/last-call") == "last-call"


def test_resolve_folders_named():
    client = ImapClient(FakeImapConn())
    assert resolve_folders(client, ["announce", "quic"]) == [
        "Shared Folders/announce",
        "Shared Folders/quic",
    ]


def test_resolve_folders_all_lists_enumerates():
    lines = [
        rb'(\Noselect) "/" "Shared Folders"',
        rb'(\HasNoChildren) "/" "Shared Folders/announce"',
    ]
    client = ImapClient(FakeImapConn(list_lines=lines))
    assert resolve_folders(client, [], all_lists=True) == ["Shared Folders/announce"]


def test_refresh_lists_index_maps_folders_to_names(tmp_path):
    lines = [
        rb'(\Noselect) "/" "Shared Folders"',
        rb'(\HasNoChildren) "/" "Shared Folders/announce"',
        rb'(\HasNoChildren) "/" "Shared Folders/last-call"',
    ]
    client = ImapClient(FakeImapConn(list_lines=lines))
    with Store(tmp_path / "t.db") as store:
        counts = refresh_lists_index(client, store)
        assert counts["added"] == 2
        assert counts["total"] == 2
        rows = {row["name"]: row["folder"] for row in store.list_rows()}
    assert rows == {
        "announce": "Shared Folders/announce",
        "last-call": "Shared Folders/last-call",
    }


def test_refresh_lists_index_checks_activity_only_for_message_lists(tmp_path):
    lines = [
        rb'(\HasNoChildren) "/" "Shared Folders/announce"',
        rb'(\HasNoChildren) "/" "Shared Folders/last-call"',
    ]
    announce_fd = FakeFolder(uidvalidity=1, uidnext=10, exists=1)
    announce_fd.messages[1] = make_raw(message_id="<1@x>")
    announce_fd.dates[1] = datetime(2025, 3, 1, 12, 0, 0)
    lastcall_fd = FakeFolder(uidvalidity=1, uidnext=10, exists=1)
    lastcall_fd.messages[1] = make_raw(message_id="<2@x>")
    conn = FakeImapConn(
        folders={"Shared Folders/announce": announce_fd, "Shared Folders/last-call": lastcall_fd},
        list_lines=lines,
    )
    client = ImapClient(conn)
    with Store(tmp_path / "t.db") as store:
        # Only announce holds a local message, so only it is tracked/checked.
        lst = store.upsert_list("announce", "Shared Folders/announce")
        store.upsert_message(
            message_id="<1@x>",
            list_id=lst.id,
            address_id=None,
            subject="s",
            date=None,
            in_reply_to=None,
            raw_body="body",
            uid=1,
        )
        counts = refresh_lists_index(client, store)
        assert counts["activity_checked"] == 1
        assert counts["activity_failed"] == 0
        # last-call is index-only, so it is never EXAMINEd/fetched.
        assert conn.internaldate_calls == ["Shared Folders/announce"]
        assert store.get_list(lst.id).last_message_at == "2025-03-01T12:00:00+00:00"


def test_refresh_lists_index_counts_activity_failures(tmp_path):
    lines = [rb'(\HasNoChildren) "/" "Shared Folders/announce"']

    class Boom(FakeImapConn):
        def fetch(self, message_set, message_parts):
            raise RuntimeError("fetch blew up")

    fd = FakeFolder(uidvalidity=1, uidnext=10, exists=1)
    fd.messages[1] = make_raw(message_id="<1@x>")
    conn = Boom(folders={"Shared Folders/announce": fd}, list_lines=lines)
    client = ImapClient(conn)
    with Store(tmp_path / "t.db") as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        store.upsert_message(
            message_id="<1@x>",
            list_id=lst.id,
            address_id=None,
            subject="s",
            date=None,
            in_reply_to=None,
            raw_body="body",
            uid=1,
        )
        counts = refresh_lists_index(client, store)
        # A failed check is counted, never fatal, and leaves the stamp untouched.
        assert counts["activity_checked"] == 0
        assert counts["activity_failed"] == 1
        assert store.get_list(lst.id).last_message_at is None


# --- parsing ------------------------------------------------------------------


def test_parse_message_decodes_rfc2047_headers_and_normalizes_address():
    raw = (
        b"Message-ID: <r1@example.org>\r\n"
        b"From: =?UTF-8?Q?Andr=C3=A9?= <Andre@Example.ORG>\r\n"
        b"Subject: =?UTF-8?Q?Caf=C3=A9_meeting?=\r\n"
        b"Date: Mon, 06 Jan 2025 10:00:00 +0000\r\n"
        b"\r\n"
        b"body text here\r\n"
    )
    parsed = parse_message(raw, uid=1, folder="Shared Folders/announce")
    assert parsed.from_name == "André"
    assert parsed.from_email == "andre@example.org"  # lowercased
    assert parsed.subject == "Café meeting"
    assert parsed.message_id == "<r1@example.org>"


def test_parse_message_prefers_text_plain_over_html():
    raw = make_raw(plain="the plain new text", html="<p>the html version</p>")
    parsed = parse_message(raw)
    assert parsed.html_only is False
    assert "the plain new text" in parsed.body
    assert "html version" not in parsed.body


def test_parse_message_captures_html_body_alongside_plain():
    # A multipart/alternative message keeps the plain body AND the HTML part.
    raw = make_raw(plain="the plain new text", html="<p>the html version</p>")
    parsed = parse_message(raw)
    assert "the plain new text" in parsed.body
    assert parsed.html_body is not None
    assert "the html version" in parsed.html_body


def test_parse_message_html_only_leaves_empty_body_but_captures_html():
    raw = make_raw(plain=None, html="<p>only html here</p>")
    parsed = parse_message(raw)
    assert parsed.html_only is True
    assert parsed.body is None
    assert parsed.html_body is not None
    assert "only html here" in parsed.html_body


def test_parse_message_plain_only_has_no_html_body():
    raw = make_raw(plain="just plain text", html=None)
    parsed = parse_message(raw)
    assert parsed.html_only is False
    assert parsed.html_body is None


def test_parse_message_date_normalized_to_utc():
    raw = make_raw(date="Mon, 06 Jan 2025 10:00:00 +0200")
    parsed = parse_message(raw)
    assert parsed.date == "2025-01-06T08:00:00+00:00"


def test_parse_message_synthesizes_missing_message_id():
    raw = make_raw(message_id="")
    parsed = parse_message(raw, uid=42, folder="Shared Folders/quic")
    assert "42" in parsed.message_id
    assert parsed.message_id.startswith("<no-message-id-")


def test_parse_message_captures_in_reply_to():
    raw = make_raw(in_reply_to="<parent@example.org>")
    parsed = parse_message(raw)
    assert parsed.in_reply_to == "<parent@example.org>"


# --- header-only parse (preview) ----------------------------------------------


def test_parse_header_extracts_from_subject_and_utc_date():
    raw = (
        b"From: =?UTF-8?Q?Andr=C3=A9?= <Andre@Example.ORG>\r\n"
        b"Subject: =?UTF-8?Q?Caf=C3=A9_meeting?=\r\n"
        b"Date: Mon, 06 Jan 2025 10:00:00 +0200\r\n"
        b"\r\n"
    )
    header = parse_header(raw)
    assert header.from_name == "André"
    assert header.from_email == "andre@example.org"  # lowercased/stripped
    assert header.subject == "Café meeting"
    assert header.date == "2025-01-06T08:00:00+00:00"  # normalized to UTC


def test_parse_header_matches_parse_message_on_full_message():
    raw = make_raw(
        from_header="Bob <bob@example.org>",
        subject="Hello there",
        date="Tue, 07 Jan 2025 09:00:00 +0000",
    )
    header = parse_header(raw)
    message = parse_message(raw)
    assert (header.from_email, header.from_name, header.subject, header.date) == (
        message.from_email,
        message.from_name,
        message.subject,
        message.date,
    )


def test_parse_header_missing_date_is_none():
    raw = b"From: a@x.org\r\nSubject: no date\r\n\r\n"
    header = parse_header(raw)
    assert header.date is None
    assert header.subject == "no date"


# --- UID computation ----------------------------------------------------------


def _folder(uids, uidvalidity=1000, uidnext=999):
    fd = FakeFolder(uidvalidity=uidvalidity, uidnext=uidnext, exists=len(uids))
    for uid, (date, frm) in uids.items():
        fd.messages[uid] = make_raw(
            message_id=f"<{uid}@example.org>",
            from_header=f"X <{frm}>",
            date=date.strftime("%a, %d %b %Y %H:%M:%S +0000"),
        )
        fd.dates[uid] = date
        fd.froms[uid] = frm
    return fd


def _client_store(fd, folder="Shared Folders/t"):
    client = ImapClient(FakeImapConn(folders={folder: fd}))
    store = Store(":memory:")
    mlist = store.upsert_list(list_name_for_folder(folder), folder)
    return client, store, mlist


def test_compute_uids_count_slices_from_top():
    fd = _folder({u: (datetime(2025, 1, u), "a@x") for u in range(1, 6)})
    client, store, mlist = _client_store(fd)
    uids, uidvalidity = compute_uids(
        client, store, "Shared Folders/t", mlist.id, DepthMode(count=2), ()
    )
    assert uids == [4, 5]
    assert uidvalidity == 1000
    store.close()


def test_compute_uids_since_filters_server_side():
    fd = _folder(
        {
            1: (datetime(2024, 12, 1), "a@x"),
            2: (datetime(2025, 2, 1), "b@x"),
            3: (datetime(2025, 3, 1), "c@x"),
        }
    )
    client, store, mlist = _client_store(fd)
    uids, _ = compute_uids(
        client, store, "Shared Folders/t", mlist.id, DepthMode(since="2025-01-01"), ()
    )
    assert uids == [2, 3]
    store.close()


def test_compute_uids_incremental_fresh_takes_all():
    fd = _folder({u: (datetime(2025, 1, u), "a@x") for u in range(1, 4)})
    client, store, mlist = _client_store(fd)
    uids, _ = compute_uids(
        client, store, "Shared Folders/t", mlist.id, DepthMode(incremental=True), ()
    )
    assert uids == [1, 2, 3]
    store.close()


def test_compute_uids_incremental_advances_past_cursor():
    fd = _folder({u: (datetime(2025, 1, u), "a@x") for u in range(1, 6)})
    client, store, mlist = _client_store(fd)
    store.set_pull_state(mlist.id, 1000, 3)  # same uidvalidity, last_uid=3
    uids, _ = compute_uids(
        client, store, "Shared Folders/t", mlist.id, DepthMode(incremental=True), ()
    )
    assert uids == [4, 5]
    store.close()


def test_compute_uids_incremental_uidvalidity_change_resyncs_by_date():
    fd = _folder(
        {
            1: (datetime(2024, 6, 1), "a@x"),
            2: (datetime(2025, 3, 1), "b@x"),
        },
        uidvalidity=2000,
    )
    client, store, mlist = _client_store(fd)
    # Stored cursor has a DIFFERENT uidvalidity → forces resync.
    store.set_pull_state(mlist.id, 1111, 99)
    store.set_list_synced(mlist.id, "2025-01-01T00:00:00+00:00")
    uids, uidvalidity = compute_uids(
        client, store, "Shared Folders/t", mlist.id, DepthMode(incremental=True), ()
    )
    # Resync re-searches SINCE 2025-01-01, so only uid 2 (Mar) matches.
    assert uids == [2]
    assert uidvalidity == 2000
    store.close()


def test_compute_uids_union_of_multiple_from_filters_deduped():
    fd = _folder(
        {
            1: (datetime(2025, 1, 1), "alice@example.org"),
            2: (datetime(2025, 1, 2), "bob@example.com"),
            3: (datetime(2025, 1, 3), "carol@example.org"),
        }
    )
    client, store, mlist = _client_store(fd)
    uids, _ = compute_uids(
        client,
        store,
        "Shared Folders/t",
        mlist.id,
        DepthMode(count=None),
        ("example.org", "example.com"),
    )
    assert uids == [1, 2, 3]
    store.close()


# --- run_fetch ----------------------------------------------------------------


def _request(**kw):
    defaults = dict(
        folders=("Shared Folders/t",),
        depth=DepthMode(count=100),
        from_filters=(),
        limit=None,
        dry_run=False,
        batch_size=200,
    )
    defaults.update(kw)
    return FetchRequest(**defaults)


def test_run_fetch_stores_messages_addresses_and_cursor():
    fd = _folder({u: (datetime(2025, 1, u), f"user{u}@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request())
    assert summary.fetched == 3
    assert summary.per_list["t"] == 3
    # cursor advanced to max UID, list stamped
    mlist = store.upsert_list("t", "Shared Folders/t")
    cursor = store.get_pull_state(mlist.id)
    assert cursor is not None and cursor.last_uid == 3 and cursor.uidvalidity == 1000
    assert store.get_list(mlist.id).last_synced_at is not None
    # address landed, normalized
    addr = store.upsert_address("user1@example.org")
    assert addr.email == "user1@example.org"
    store.close()


def test_run_fetch_sets_last_message_at():
    fd = _folder({u: (datetime(2025, 1, u), f"user{u}@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch(client, store, _request())
    mlist = store.upsert_list("t", "Shared Folders/t")
    # Newest message is uid 3 (dated 2025-01-03), recorded as UTC ISO-8601.
    assert store.get_list(mlist.id).last_message_at == "2025-01-03T00:00:00+00:00"
    store.close()


def test_run_fetch_is_idempotent_on_repull():
    fd = _folder({u: (datetime(2025, 1, u), "a@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch(client, store, _request())
    second = run_fetch(client, store, _request())
    assert second.fetched == 0
    assert second.duplicates == 3
    store.close()


def test_run_fetch_limit_caps_messages():
    fd = _folder({u: (datetime(2025, 1, u), "a@example.org") for u in range(1, 6)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request(limit=3))
    assert summary.fetched == 3
    store.close()


def test_run_fetch_dry_run_stores_nothing():
    fd = _folder({u: (datetime(2025, 1, u), "a@example.org") for u in range(1, 4)})
    conn = FakeImapConn(folders={"Shared Folders/t": fd})
    client = ImapClient(conn)
    store = Store(":memory:")
    summary = run_fetch(client, store, _request(dry_run=True))
    assert summary.matched == 3
    assert summary.fetched == 0
    assert conn.fetch_calls == []  # never fetched bodies
    mlist = store.upsert_list("t", "Shared Folders/t")
    assert store.get_pull_state(mlist.id) is None
    store.close()


def test_run_fetch_counts_html_only():
    fd = FakeFolder(uidvalidity=1000, uidnext=10, exists=1)
    fd.messages[1] = make_raw(message_id="<h@x>", plain=None, html="<p>hi</p>")
    fd.dates[1] = datetime(2025, 1, 1)
    fd.froms[1] = "a@example.org"
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request())
    assert summary.fetched == 1
    assert summary.html_only == 1
    store.close()


def test_run_fetch_stores_raw_html():
    # A multipart/alternative message stores both raw_body and raw_html.
    fd = FakeFolder(uidvalidity=1000, uidnext=10, exists=1)
    fd.messages[1] = make_raw(
        message_id="<mix@x>", plain="the plain body", html="<p>the html body</p>"
    )
    fd.dates[1] = datetime(2025, 1, 1)
    fd.froms[1] = "a@example.org"
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch(client, store, _request())
    row = store.conn.execute(
        "SELECT raw_body, raw_html FROM messages WHERE message_id = ?", ("<mix@x>",)
    ).fetchone()
    assert "the plain body" in row["raw_body"]
    assert "the html body" in row["raw_html"]
    store.close()


# --- run_fetch_uids -----------------------------------------------------------


def test_run_fetch_uids_fetches_exact_set_and_upserts():
    fd = _folder({u: (datetime(2025, 1, u), f"user{u}@example.org") for u in range(1, 6)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    # Pull only an explicit subset (uids 2 and 4), not the whole folder.
    summary = run_fetch_uids(client, store, "Shared Folders/t", [2, 4])
    assert summary.fetched == 2
    assert summary.matched == 2
    assert summary.per_list["t"] == 2
    mlist = store.get_list_by_name("t")
    stored = {
        row["uid"]
        for row in store.conn.execute(
            "SELECT uid FROM messages WHERE list_id = ?", (mlist.id,)
        ).fetchall()
    }
    assert stored == {2, 4}
    store.close()


def test_run_fetch_uids_leaves_cursor_and_sync_to_caller():
    fd = _folder({u: (datetime(2025, 1, u), "a@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch_uids(client, store, "Shared Folders/t", [1, 2, 3])
    mlist = store.get_list_by_name("t")
    # The wrapper deliberately does not touch pull_state or last_synced_at.
    assert store.get_pull_state(mlist.id) is None
    assert store.get_list(mlist.id).last_synced_at is None
    store.close()


def test_run_fetch_uids_is_idempotent_on_repull():
    fd = _folder({u: (datetime(2025, 1, u), "a@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch_uids(client, store, "Shared Folders/t", [1, 2, 3])
    second = run_fetch_uids(client, store, "Shared Folders/t", [1, 2, 3])
    assert second.fetched == 0
    assert second.duplicates == 3
    store.close()
