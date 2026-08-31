"""Tests for the extract stage of the pipeline (``cli.run_extract``).

The extraction routine itself lives in the email-reply-extractor library and is
tested there (including the hand-labeled fixture corpus and the generation
digest). These tests cover the store-facing stage around it: status/method
recording, idempotence, the ``limit`` cap, and parent resolution via
``In-Reply-To`` (including the missing-parent and self-reply cases).
"""

from __future__ import annotations

from mailing_list_ai_check import cli
from mailing_list_ai_check.store import Store

# --- pipeline (cli.run_extract) -----------------------------------------------


def _seed_message(store: Store, *, message_id: str, raw_body: str | None) -> int:
    mlist = store.upsert_list("tls", "Shared Folders/tls")
    addr = store.upsert_address("author@example.org", "Author")
    upsert = store.upsert_message(
        message_id=message_id,
        list_id=mlist.id,
        address_id=addr.id,
        subject="Re: something",
        date="2026-07-21T00:00:00+00:00",
        in_reply_to=None,
        raw_body=raw_body,
        uid=1,
    )
    return upsert.message.id


def _seed_reply(store: Store, *, message_id: str, in_reply_to: str, raw_body: str | None) -> int:
    """Seed a reply with a distinct Message-ID and an ``In-Reply-To`` header."""
    mlist = store.upsert_list("tls", "Shared Folders/tls")
    addr = store.upsert_address("author@example.org", "Author")
    upsert = store.upsert_message(
        message_id=message_id,
        list_id=mlist.id,
        address_id=addr.id,
        subject="Re: something",
        date="2026-07-21T00:00:00+00:00",
        in_reply_to=in_reply_to,
        raw_body=raw_body,
        uid=2,
    )
    return upsert.message.id


def test_pipeline_extracts_and_records_statuses(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        ok_id = _seed_message(
            store,
            message_id="<ok@x>",
            raw_body="> quoted\n\nThis is my new reply text.",
        )
        empty_id = _seed_message(store, message_id="<html@x>", raw_body=None)

        status_counts, method_counts = cli.run_extract(store)

        assert status_counts["ok"] == 1
        assert status_counts["empty"] == 1
        assert sum(method_counts.values()) == 2

        ok_row = store.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (ok_id,)
        ).fetchone()
        assert ok_row["status"] == "ok"
        assert "new reply text" in ok_row["extracted_text"]
        assert ok_row["char_count"] == len(ok_row["extracted_text"])

        empty_row = store.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (empty_id,)
        ).fetchone()
        assert empty_row["status"] == "empty"
        assert empty_row["method"] == "none"


def test_pipeline_is_idempotent(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        _seed_message(store, message_id="<a@x>", raw_body="Some new text here.")

        first, _ = cli.run_extract(store)
        assert sum(first.values()) == 1

        # Second run finds no messages lacking an extraction row: a no-op.
        second, _ = cli.run_extract(store)
        assert sum(second.values()) == 0

        count = store.conn.execute("SELECT COUNT(*) AS c FROM extractions").fetchone()["c"]
        assert count == 1


def test_pipeline_respects_limit(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        for i in range(5):
            _seed_message(store, message_id=f"<m{i}@x>", raw_body=f"Reply number {i} text.")

        status_counts, _ = cli.run_extract(store, limit=2)
        assert sum(status_counts.values()) == 2

        remaining = list(store.iter_messages_without_extraction())
        assert len(remaining) == 3


def test_pipeline_parent_diff_removes_quoted_parent(tmp_path):
    # A reply whose parent is in the store: cli.run_extract resolves the parent
    # via In-Reply-To and the assist strips the quoted parent thread.
    parent_body = (
        "Thanks for the detailed proposal about the new key rotation scheme.\n"
        "I think the possession side needs a clearer failure mode when the status\n"
        "cannot be established within the negotiated window between the two peers.\n"
    )
    child_body = (
        "I have revised the entire draft to incorporate all of your suggestions.\n"
        "Every reviewer concern is now tracked in the updated issue list online.\n"
        "\n" + parent_body
    )
    with Store(tmp_path / "db.sqlite") as store:
        _seed_message(store, message_id="<parent@x>", raw_body=parent_body)
        child_id = _seed_reply(
            store, message_id="<child@x>", in_reply_to="<parent@x>", raw_body=child_body
        )

        cli.run_extract(store)

        row = store.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (child_id,)
        ).fetchone()
        assert "I have revised the entire draft" in row["extracted_text"]
        assert "possession side needs a clearer failure mode" not in row["extracted_text"]
        assert row["method"].endswith("+parent-diff")


def test_pipeline_reply_without_stored_parent_extracts_normally(tmp_path):
    # The parent is not in the store: get_parent_body returns None, so extraction
    # runs exactly as it would with no parent (no "+parent-diff" suffix).
    child_body = "> some quoted line\n\nThis is my genuinely new reply content here today."
    with Store(tmp_path / "db.sqlite") as store:
        child_id = _seed_reply(
            store, message_id="<child@x>", in_reply_to="<missing@x>", raw_body=child_body
        )

        cli.run_extract(store)

        row = store.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (child_id,)
        ).fetchone()
        assert "This is my genuinely new reply content" in row["extracted_text"]
        assert "+parent-diff" not in row["method"]


def test_pipeline_self_reply_is_not_wiped(tmp_path):
    # A malformed message whose In-Reply-To names its own Message-ID must not be
    # treated as a reply to itself: resolving the "parent" to its own body would
    # make the parent-diff assist delete every line and report the message empty.
    child_body = (
        "I have reviewed the whole proposal in detail and I think we should adopt it.\n"
        "The rotation scheme handles every failure mode that we discussed at length.\n"
    )
    with Store(tmp_path / "db.sqlite") as store:
        child_id = _seed_reply(
            store, message_id="<self@x>", in_reply_to="<self@x>", raw_body=child_body
        )

        cli.run_extract(store)

        row = store.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (child_id,)
        ).fetchone()
        assert row["status"] == "ok"
        assert "I have reviewed the whole proposal" in row["extracted_text"]
        assert "+parent-diff" not in row["method"]
