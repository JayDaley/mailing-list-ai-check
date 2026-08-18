"""Tests for the filtered message timeline.

Covers :meth:`Store.message_timeline` — the slim point set the messages pane's
heading rug draws, honouring the same filters as the message table — and the
``GET /api/messages/timeline`` endpoint that serves it.
"""

from __future__ import annotations

import pytest

from mailing_list_ai_check.config import Config
from mailing_list_ai_check.store import TIMELINE_BUCKETS, MessageFilters, Store, sha256_text
from mailing_list_ai_check.webapp import create_app

#: Bucket indexes, by name (the store serves indexes into TIMELINE_BUCKETS).
_B = {name: i for i, name in enumerate(TIMELINE_BUCKETS)}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "timeline.db"


@pytest.fixture()
def store(db_path):
    with Store(db_path) as s:
        yield s


def _message(
    store: Store,
    list_id: int,
    key: str,
    date: str | None,
    *,
    label: str | None = None,
    status: str | None = None,
) -> int:
    """Insert ``<key@test>``; extract with ``status`` and score with ``label``."""
    message = store.upsert_message(
        message_id=f"<{key}@test>",
        list_id=list_id,
        address_id=None,
        subject=f"Subject {key}",
        date=date,
        in_reply_to=None,
        raw_body="body",
        uid=None,
    ).message
    if status is not None or label is not None:
        extraction = store.insert_extraction(
            message_id=message.id,
            extracted_text=f"text {key}",
            method="test",
            status=status or "ok",
        )
        if label is not None:
            store.insert_score(
                extraction_id=extraction.id,
                text_sha256=sha256_text(f"text {key}"),
                fraction_ai=0.9 if label == "AI" else 0.1,
                label=label,
                detector_version="v3",
            )
    return message.id


@pytest.fixture()
def fixture(store):
    """Two lists holding one message per bucket kind, plus an undated one."""
    big = store.upsert_list("big", "Shared Folders/big").id
    small = store.upsert_list("small", "Shared Folders/small").id

    ids: dict[str, int] = {}
    ids["b1"] = _message(store, big, "b1", "2026-01-01T10:00:00+00:00", label="Human")
    ids["b2"] = _message(store, big, "b2", "2026-01-02T10:00:00+00:00", label="AI")
    ids["b3"] = _message(store, big, "b3", "2026-01-03T10:00:00+00:00", status="too_short")
    ids["b4"] = _message(store, big, "b4", None, label="Mixed")
    ids["s1"] = _message(store, small, "s1", "2026-02-01T10:00:00+00:00", label="Mixed")
    ids["s2"] = _message(store, small, "s2", "not-a-date")
    return {"store": store, "ids": ids}


# --- Store.message_timeline ------------------------------------------------------


def test_unfiltered_covers_every_message(fixture):
    result = fixture["store"].message_timeline(MessageFilters())
    assert result["total"] == 6
    # b4 has no date and s2's does not parse: neither carries a point.
    assert result["undated"] == 2
    assert len(result["points"]) == 4


def test_points_carry_id_epoch_and_bucket_in_date_order(fixture):
    ids = fixture["ids"]
    result = fixture["store"].message_timeline(MessageFilters())
    assert [p[0] for p in result["points"]] == [ids["b1"], ids["b2"], ids["b3"], ids["s1"]]
    assert [p[2] for p in result["points"]] == [
        _B["Human"],
        _B["AI"],
        _B["too_short"],
        _B["Mixed"],
    ]
    epochs = [p[1] for p in result["points"]]
    assert epochs == sorted(epochs)
    assert result["start"] == epochs[0]
    assert result["end"] == epochs[-1]


def test_filters_narrow_the_timeline_like_the_table(fixture):
    store = fixture["store"]
    ids = fixture["ids"]
    by_list = store.message_timeline(MessageFilters(list_name="small"))
    assert by_list["total"] == 2
    assert [p[0] for p in by_list["points"]] == [ids["s1"]]
    by_label = store.message_timeline(MessageFilters(label="AI"))
    assert [p[0] for p in by_label["points"]] == [ids["b2"]]
    by_date = store.message_timeline(MessageFilters(date_from="2026-01-02"))
    assert [p[0] for p in by_date["points"]] == [ids["b2"], ids["b3"], ids["s1"]]


def test_empty_match_has_null_domain(fixture):
    result = fixture["store"].message_timeline(MessageFilters(list_name="nope"))
    assert result == {"start": None, "end": None, "total": 0, "undated": 0, "points": []}


# --- GET /api/messages/timeline ---------------------------------------------------


@pytest.fixture()
def client(db_path, fixture):
    """A test client over the fixture's database (no built frontend)."""
    config = Config(
        imap_host="imap.example.org",
        imap_port=993,
        imap_username="anonymous",
        imap_password="anonymous@example.com",
        pangram_api_key="test-key",
        database_path=str(db_path),
        log_level="INFO",
        flask_host="127.0.0.1",
        flask_port=8050,
    )
    app = create_app(config, frontend_dist=None)
    app.testing = True
    return app.test_client()


def test_endpoint_returns_the_filtered_timeline(client):
    resp = client.get("/api/messages/timeline?list=big")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["buckets"] == list(TIMELINE_BUCKETS)
    assert body["total"] == 4
    assert body["undated"] == 1
    assert [p[2] for p in body["points"]] == [_B["Human"], _B["AI"], _B["too_short"]]


def test_endpoint_rejects_malformed_filters(client):
    assert client.get("/api/messages/timeline?min_likelihood=2").status_code == 400
