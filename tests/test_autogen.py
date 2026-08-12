"""Tests for auto-generated mail detection (autogen.py) and its pipeline wiring."""

from __future__ import annotations

from email.message import EmailMessage

from conftest import FakeFolder, FakeImapConn, make_raw

from mailing_list_ai_check import export_import
from mailing_list_ai_check.autogen import (
    REASON_AUTO_SUBMITTED,
    REASON_NVN_FORWARD,
    REASON_PRECEDENCE_BULK,
    REASON_ROBOT_SENDER,
    classify_message,
    is_excluded_list,
)
from mailing_list_ai_check.fetcher import (
    DepthMode,
    FetchRequest,
    parse_message,
    resolve_folders,
    run_fetch,
)
from mailing_list_ai_check.imap_client import ImapClient
from mailing_list_ai_check.store import Store

# --- list exclusions ------------------------------------------------------------


def test_excluded_lists_by_name():
    for name in (
        "dmarc-report",
        "i-d-announce",
        "ietf-announce",
        "irtf-announce",
        "rfc-dist",
        "new-wg-docs",
        "ipr-announce",
        "iesg-agenda-dist",
        "netmod-ver-dt",
        "quic-issues",
    ):
        assert is_excluded_list(name), name


def test_excluded_meeting_broadcast_lists():
    for name in ("123all", "124attendees", "125-newparticipants", "recentattendees", "127ALL"):
        assert is_excluded_list(name), name


def test_discussion_lists_not_excluded():
    for name in ("tls", "last-call", "quic", "6man", "auth48archive", "gen-art", "123foo"):
        assert not is_excluded_list(name), name


# --- message classification ------------------------------------------------------


def _msg(headers: dict[str, str]) -> EmailMessage:
    msg = EmailMessage()
    for name, value in headers.items():
        msg[name] = value
    msg.set_content("body")
    return msg


def test_human_message_is_not_flagged():
    msg = _msg({"From": "Alice <alice@example.org>", "Subject": "Re: comments"})
    assert classify_message(msg) is None


def test_auto_submitted_flags():
    msg = _msg({"From": "a@example.org", "Auto-Submitted": "auto-generated"})
    assert classify_message(msg) == REASON_AUTO_SUBMITTED


def test_auto_submitted_no_is_not_flagged():
    msg = _msg({"From": "a@example.org", "Auto-Submitted": "no"})
    assert classify_message(msg) is None


def test_precedence_bulk_flags():
    msg = _msg({"From": "iana-issues@iana.org", "Precedence": "bulk"})
    assert classify_message(msg) == REASON_PRECEDENCE_BULK


def test_precedence_list_is_not_flagged():
    msg = _msg({"From": "a@example.org", "Precedence": "list"})
    assert classify_message(msg) is None


def test_robot_senders_flag():
    for sender in (
        "rfc-editor@rfc-editor.org",
        "noreply@github.com",
        "notifications@github.com",
        "do_not_reply@mnot.net",
        "MAILER-DAEMON@example.cisco.com",
    ):
        msg = _msg({"From": f"Robot <{sender}>"})
        assert classify_message(msg) == REASON_ROBOT_SENDER, sender


def test_iesg_ballot_position_is_kept():
    # Datatracker-delivered ballot text is human-written; the To: IESG address
    # is what distinguishes it from other datatracker notifications.
    msg = _msg(
        {
            "From": "Roman Danyliw via Datatracker <noreply@ietf.org>",
            "To": '"The IESG" <iesg@ietf.org>',
            "Reply-To": "Roman Danyliw <rdd@cert.org>",
            "Auto-Submitted": "auto-generated",
            "Precedence": "bulk",
            "Subject": "[6lo] Roman Danyliw's No Objection on draft-ietf-6lo-x-13",
        }
    )
    assert classify_message(msg) is None


def test_datatracker_notification_not_to_iesg_is_flagged():
    msg = _msg(
        {
            "From": "Jean Mahoney via Datatracker <noreply@ietf.org>",
            "To": "<asap-chairs@ietf.org>, <asap@ietf.org>",
            "Auto-Submitted": "auto-generated",
            "Subject": "[Asap] WG Last Call: draft-ietf-asap-sip-auto-peer-30",
        }
    )
    assert classify_message(msg) == REASON_AUTO_SUBMITTED


def test_forwarded_new_version_notification_is_flagged():
    msg = _msg(
        {
            "From": "Alice <alice@example.org>",
            "Subject": "[tls] Fwd: New Version Notification for draft-x-01.txt",
        }
    )
    assert classify_message(msg) == REASON_NVN_FORWARD


def test_reply_to_new_version_notification_is_kept():
    msg = _msg(
        {
            "From": "Alice <alice@example.org>",
            "Subject": "[tls] Re: New Version Notification for draft-x-01.txt",
            "In-Reply-To": "<parent@example.org>",
        }
    )
    assert classify_message(msg) is None


# --- parse_message wiring ---------------------------------------------------------


def test_parse_message_carries_classification():
    raw = make_raw(extra_headers={"Auto-Submitted": "auto-generated"})
    assert parse_message(raw).auto_generated == REASON_AUTO_SUBMITTED
    assert parse_message(make_raw()).auto_generated is None


# --- resolve_folders exclusion -----------------------------------------------------


_LIST_LINES = [
    rb'(\Noselect) "/" "Shared Folders"',
    rb'(\HasNoChildren) "/" "Shared Folders/tls"',
    rb'(\HasNoChildren) "/" "Shared Folders/i-d-announce"',
    rb'(\HasNoChildren) "/" "Shared Folders/123all"',
]


def test_resolve_folders_all_lists_skips_excluded():
    client = ImapClient(FakeImapConn(list_lines=_LIST_LINES))
    assert resolve_folders(client, [], all_lists=True) == ["Shared Folders/tls"]


def test_resolve_folders_include_excluded_keeps_everything():
    client = ImapClient(FakeImapConn(list_lines=_LIST_LINES))
    assert resolve_folders(client, [], all_lists=True, include_excluded=True) == [
        "Shared Folders/tls",
        "Shared Folders/i-d-announce",
        "Shared Folders/123all",
    ]


def test_resolve_folders_explicit_names_are_always_honoured():
    client = ImapClient(FakeImapConn(list_lines=_LIST_LINES))
    assert resolve_folders(client, ["i-d-announce"]) == ["Shared Folders/i-d-announce"]


# --- fetch + store + extraction-queue wiring ---------------------------------------


def _store_with_one_auto_one_human(tmp_path):
    human = make_raw(message_id="<human@example.org>", plain="written by a person")
    auto = make_raw(
        message_id="<auto@example.org>",
        from_header="internet-drafts@ietf.org",
        subject="I-D Action: draft-x-01.txt",
        plain="A new version is available.",
        extra_headers={"Auto-Submitted": "auto-generated", "Precedence": "bulk"},
    )
    folder = FakeFolder(uidvalidity=7, uidnext=3, exists=2, messages={1: human, 2: auto})
    conn = FakeImapConn(folders={"Shared Folders/tls": folder})
    store = Store(tmp_path / "t.db")
    request = FetchRequest(folders=("Shared Folders/tls",), depth=DepthMode(count=10))
    summary = run_fetch(ImapClient(conn), store, request)
    return store, summary


def test_run_fetch_stores_classification_and_counts(tmp_path):
    store, summary = _store_with_one_auto_one_human(tmp_path)
    assert summary.fetched == 2
    assert summary.auto_generated == 1
    flagged = store.conn.execute(
        "SELECT message_id, auto_generated FROM messages ORDER BY message_id"
    ).fetchall()
    by_id = {row["message_id"]: row["auto_generated"] for row in flagged}
    assert by_id["<auto@example.org>"] == REASON_AUTO_SUBMITTED
    assert by_id["<human@example.org>"] is None
    store.close()


def test_extraction_queue_skips_auto_generated(tmp_path):
    store, _summary = _store_with_one_auto_one_human(tmp_path)
    queued = [m.message_id for m in store.iter_messages_without_extraction()]
    assert queued == ["<human@example.org>"]
    store.close()


def test_export_import_round_trips_classification(tmp_path):
    store, _summary = _store_with_one_auto_one_human(tmp_path)
    out = tmp_path / "dump.jsonl"
    export_import.export_lists(store, None, out, all_lists=True, compress=False)
    store.close()

    with Store(tmp_path / "copy.db") as copy:
        export_import.import_file(copy, out)
        row = copy.conn.execute(
            "SELECT auto_generated FROM messages WHERE message_id = '<auto@example.org>'"
        ).fetchone()
        assert row["auto_generated"] == REASON_AUTO_SUBMITTED
