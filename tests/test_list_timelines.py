"""Tests for the per-list message timelines.

Covers :meth:`Store.list_timelines` — the slim, uncapped per-list point sets the
dashboard's adaptive rug plots draw — and the ``GET /api/lists/timelines``
endpoint that serves them.
"""

from __future__ import annotations

import pytest

from mailing_list_ai_check.config import Config
from mailing_list_ai_check.store import TIMELINE_BUCKETS, Store, sha256_text
from mailing_list_ai_check.webapp import create_app

#: Bucket indexes, by name (the store serves indexes into TIMELINE_BUCKETS).
_B = {name: i for i, name in enumerate(TIMELINE_BUCKETS)}


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "timelines.db"


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
    """Two lists: ``big`` (3 messages, one per bucket kind) and ``small`` (2).

    ``big`` also holds one undated message; ``small`` one whose date does not
    parse. An empty list ``bare`` is tracked but has no messages at all.
    """
    big = store.upsert_list("big", "Shared Folders/big").id
    small = store.upsert_list("small", "Shared Folders/small").id
    store.upsert_list("bare", "Shared Folders/bare")

    ids: dict[str, int] = {}
    ids["b1"] = _message(store, big, "b1", "2026-01-01T10:00:00+00:00", label="Human")
    ids["b2"] = _message(store, big, "b2", "2026-01-02T10:00:00+00:00", label="AI")
    ids["b3"] = _message(store, big, "b3", "2026-01-03T10:00:00+00:00", status="too_short")
    ids["b4"] = _message(store, big, "b4", None, label="Mixed")
    ids["s1"] = _message(store, small, "s1", "2026-02-01T10:00:00+00:00", label="Mixed")
    ids["s2"] = _message(store, small, "s2", "not-a-date")
    return {"store": store, "ids": ids}


# --- Store.list_timelines -------------------------------------------------------


def test_all_lists_ordered_by_count_desc(fixture):
    result = fixture["store"].list_timelines()
    assert [entry["list"] for entry in result["lists"]] == ["big", "small"]
    assert [entry["total"] for entry in result["lists"]] == [4, 2]


def test_a_list_without_messages_has_no_entry(fixture):
    assert "bare" not in {e["list"] for e in fixture["store"].list_timelines()["lists"]}


def test_points_carry_id_epoch_and_bucket(fixture):
    ids = fixture["ids"]
    big = fixture["store"].list_timelines()["lists"][0]
    assert [p[0] for p in big["points"]] == [ids["b1"], ids["b2"], ids["b3"]]
    assert [p[2] for p in big["points"]] == [_B["Human"], _B["AI"], _B["too_short"]]
    # Epochs ascend with the dates and carry no subject in the all-lists form.
    assert big["points"][0][1] < big["points"][1][1] < big["points"][2][1]
    assert all(len(p) == 3 for p in big["points"])


def test_unscored_and_unparseable_dates(fixture):
    result = fixture["store"].list_timelines()
    small = result["lists"][1]
    # s2 has an extraction but no score and an unparseable date.
    assert small["total"] == 2
    assert small["undated"] == 1
    assert [p[2] for p in small["points"]] == [_B["Mixed"]]


def test_undated_messages_are_counted_not_plotted(fixture):
    big = fixture["store"].list_timelines()["lists"][0]
    assert big["total"] == 4
    assert big["undated"] == 1
    assert len(big["points"]) == 3


def test_start_end_span_every_dated_point(fixture):
    result = fixture["store"].list_timelines()
    epochs = [p[1] for entry in result["lists"] for p in entry["points"]]
    assert result["start"] == min(epochs)
    assert result["end"] == max(epochs)


def test_single_list_includes_subjects(fixture):
    result = fixture["store"].list_timelines("big")
    assert [entry["list"] for entry in result["lists"]] == ["big"]
    assert [p[3] for p in result["lists"][0]["points"]] == [
        "Subject b1",
        "Subject b2",
        "Subject b3",
    ]
    # The domain narrows to the requested list's dated points.
    assert result["start"] == result["lists"][0]["points"][0][1]
    assert result["end"] == result["lists"][0]["points"][-1][1]


def test_unknown_list_yields_empty(fixture):
    result = fixture["store"].list_timelines("nope")
    assert result == {"start": None, "end": None, "lists": []}


# --- GET /api/lists/timelines ---------------------------------------------------


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


def test_endpoint_returns_every_list(client):
    resp = client.get("/api/lists/timelines")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["buckets"] == list(TIMELINE_BUCKETS)
    assert [entry["list"] for entry in body["lists"]] == ["big", "small"]
    assert body["start"] is not None and body["end"] is not None


def test_endpoint_scopes_to_one_list_with_subjects(client):
    body = client.get("/api/lists/timelines?list=small").get_json()
    assert [entry["list"] for entry in body["lists"]] == ["small"]
    assert body["lists"][0]["points"][0][3] == "Subject s1"


def test_endpoint_unknown_list_is_empty_not_404(client):
    resp = client.get("/api/lists/timelines?list=nope")
    assert resp.status_code == 200
    assert resp.get_json()["lists"] == []
