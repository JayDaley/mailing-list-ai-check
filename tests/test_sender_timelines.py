"""Tests for the per-sender message timelines.

Covers :meth:`Store.sender_timelines` — the slim ``[id, t, bucket]`` point sets
the Senders pane's per-sender history rugs draw — and the
``GET /api/senders/timelines`` endpoint that serves them.
"""

from __future__ import annotations

import pytest

from mailing_list_ai_check.config import Config
from mailing_list_ai_check.store import TIMELINE_BUCKETS, Store, sha256_text
from mailing_list_ai_check.webapp import create_app

#: Bucket indexes, by name (the store serves indexes into TIMELINE_BUCKETS).
_B = {name: i for i, name in enumerate(TIMELINE_BUCKETS)}

# Dates ascend with the message's day number, so point order is predictable.
_DAY = "2026-04-{:02d}T10:00:00+00:00"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "sender_timelines.db"


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
    address_id: int | None = None,
    label: str | None = None,
    status: str | None = None,
) -> int:
    """Insert ``<key@test>``; extract with ``status`` and score with ``label``."""
    message = store.upsert_message(
        message_id=f"<{key}@test>",
        list_id=list_id,
        address_id=address_id,
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
    """Alice (person, addresses a1+a2), unlinked Bob, and an addressless message.

    List ``alpha`` holds Alice's Human/AI/AI run (the AI on her second address,
    so the person scope must roll it up), Bob's Mixed and too-short posts, and
    one message with no address at all. List ``beta`` holds one later Alice AI
    message, so a list scope must narrow both points and domain. Alice also has
    an undated AI message on ``alpha``.
    """
    alpha = store.upsert_list("alpha", "Shared Folders/alpha").id
    beta = store.upsert_list("beta", "Shared Folders/beta").id

    a1 = store.upsert_address("alice@example.org", "Alice").id
    a2 = store.upsert_address("alice@work.example", "Alice").id
    bob = store.upsert_address("bob@example.org", "Bob").id
    person = store.create_person("Alice").id
    store.assign_address_to_person(a1, person)
    store.assign_address_to_person(a2, person)

    ids: dict[str, int] = {}
    ids["h1"] = _message(store, alpha, "h1", _DAY.format(1), address_id=a1, label="Human")
    ids["ai1"] = _message(store, alpha, "ai1", _DAY.format(2), address_id=a1, label="AI")
    ids["ai2"] = _message(store, alpha, "ai2", _DAY.format(3), address_id=a2, label="AI")
    ids["m1"] = _message(store, alpha, "m1", _DAY.format(4), address_id=bob, label="Mixed")
    ids["ts1"] = _message(store, alpha, "ts1", _DAY.format(5), address_id=bob, status="too_short")
    ids["anon"] = _message(store, alpha, "anon", _DAY.format(6), address_id=None)
    ids["und"] = _message(store, alpha, "und", None, address_id=a1, label="AI")
    ids["b1"] = _message(store, beta, "b1", _DAY.format(7), address_id=a1, label="AI")

    return {"store": store, "person": person, "ids": ids}


def _ids(entry):
    return [p[0] for p in entry["points"]]


def _epochs(entry):
    return [p[1] for p in entry["points"]]


def _buckets(entry):
    return [p[2] for p in entry["points"]]


# --- Store.sender_timelines -------------------------------------------------------


def test_person_scope_rolls_up_every_linked_address(fixture):
    result = fixture["store"].sender_timelines(person_ids=[fixture["person"]])
    entry = result["persons"][str(fixture["person"])]
    ids = fixture["ids"]
    # h1, ai1 (a1), ai2 (a2) and the beta message, dates ascending; und dropped.
    assert _ids(entry) == [ids["h1"], ids["ai1"], ids["ai2"], ids["b1"]]
    assert _buckets(entry) == [_B["Human"], _B["AI"], _B["AI"], _B["AI"]]
    assert _epochs(entry) == sorted(_epochs(entry))
    assert entry["undated"] == 1


def test_address_scope_covers_that_email_alone(fixture):
    result = fixture["store"].sender_timelines(addresses=["bob@example.org"])
    entry = result["addresses"]["bob@example.org"]
    # Mixed and too_short both count as posts, each with its own bucket.
    assert _buckets(entry) == [_B["Mixed"], _B["too_short"]]


def test_address_lookup_is_case_insensitive_and_keyed_lowercase(fixture):
    result = fixture["store"].sender_timelines(addresses=["Bob@Example.org "])
    assert list(result["addresses"]) == ["bob@example.org"]
    assert len(result["addresses"]["bob@example.org"]["points"]) == 2


def test_domain_spans_the_whole_corpus_not_the_requested_senders(fixture):
    result = fixture["store"].sender_timelines(addresses=["bob@example.org"])
    full = fixture["store"].sender_timelines(person_ids=[fixture["person"]])
    all_epochs = _epochs(full["persons"][str(fixture["person"])])
    assert result["start"] < min(_epochs(result["addresses"]["bob@example.org"]))
    assert result["end"] == max(all_epochs)  # beta's b1 is the corpus's latest


def test_list_scope_narrows_points_and_domain(fixture):
    result = fixture["store"].sender_timelines(person_ids=[fixture["person"]], list_name="alpha")
    entry = result["persons"][str(fixture["person"])]
    assert _buckets(entry) == [_B["Human"], _B["AI"], _B["AI"]]  # beta's b1 dropped
    unscoped = fixture["store"].sender_timelines(person_ids=[fixture["person"]])
    assert result["end"] < unscoped["end"]


def test_unknown_list_yields_empty_maps(fixture):
    result = fixture["store"].sender_timelines(person_ids=[fixture["person"]], list_name="nope")
    assert result == {"start": None, "end": None, "persons": {}, "addresses": {}}


def test_unknown_sender_yields_an_empty_entry(fixture):
    result = fixture["store"].sender_timelines(person_ids=[999], addresses=["no@where"])
    assert result["persons"]["999"] == {"points": [], "undated": 0}
    assert result["addresses"]["no@where"] == {"points": [], "undated": 0}


# --- GET /api/senders/timelines ---------------------------------------------------


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


def test_endpoint_serves_persons_and_addresses(client, fixture):
    resp = client.get(
        f"/api/senders/timelines?persons={fixture['person']}&addresses=bob@example.org"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["buckets"] == list(TIMELINE_BUCKETS)
    person_buckets = [p[2] for p in body["persons"][str(fixture["person"])]["points"]]
    assert person_buckets == [_B["Human"], _B["AI"], _B["AI"], _B["AI"]]
    assert len(body["addresses"]["bob@example.org"]["points"]) == 2
    assert body["start"] is not None and body["end"] is not None
    assert body["list"] is None


def test_endpoint_scopes_to_a_list(client, fixture):
    body = client.get(f"/api/senders/timelines?persons={fixture['person']}&list=alpha").get_json()
    assert [p[2] for p in body["persons"][str(fixture["person"])]["points"]] == [
        _B["Human"],
        _B["AI"],
        _B["AI"],
    ]
    assert body["list"] == "alpha"


def test_endpoint_requires_at_least_one_sender(client):
    assert client.get("/api/senders/timelines").status_code == 400


def test_endpoint_rejects_a_non_integer_person(client):
    assert client.get("/api/senders/timelines?persons=abc").status_code == 400


def test_endpoint_caps_the_sender_count(client):
    ids = ",".join(str(i) for i in range(201))
    assert client.get(f"/api/senders/timelines?persons={ids}").status_code == 400
