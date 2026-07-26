"""Tests for the sender screen's reply rugs.

Covers :meth:`Store.sender_reply_rugs` — the per-(sender, list) "replied to" and
"reply from" message sets — and the ``GET /api/senders/reply-rugs`` endpoint that
serves them. The fixture below builds its own small thread structure rather than
using :mod:`tests.seed`, whose messages carry almost no reply linkage.
"""

from __future__ import annotations

import pytest

from mailing_list_ai_check.config import Config
from mailing_list_ai_check.store import REPLY_RUG_LIMIT, Store, sha256_text
from mailing_list_ai_check.webapp import create_app

# Dates ascend with the message's day number, so "newest first" is predictable.
_DAY = "2026-04-{:02d}T10:00:00+00:00"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "rugs.db"


@pytest.fixture()
def store(db_path):
    with Store(db_path) as s:
        yield s


def _list(store: Store, name: str) -> int:
    return store.upsert_list(name, f"Shared Folders/{name}").id


def _message(
    store: Store,
    list_id: int,
    key: str,
    day: int,
    *,
    address_id: int | None = None,
    in_reply_to: str | None = None,
    label: str | None = None,
) -> int:
    """Insert ``<key@test>`` on ``list_id``, extracted and scored when labelled."""
    message = store.upsert_message(
        message_id=f"<{key}@test>",
        list_id=list_id,
        address_id=address_id,
        subject=f"Subject {key}",
        date=_DAY.format(day),
        in_reply_to=in_reply_to,
        raw_body="body",
        uid=None,
    ).message
    if label is not None:
        extraction = store.insert_extraction(
            message_id=message.id, extracted_text=f"text {key}", method="test", status="ok"
        )
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
    """A two-list thread fixture.

    List ``alpha``:

    - Alice (person P, addresses a1 + a2) posts ``a-root`` (a1) and replies
      ``a-reply1`` → ``x-root`` (a1) and ``a-reply2`` → ``y-root`` (a2, so the
      person scope must pick it up and the a1-only scope must not).
    - Bob replies ``b-reply`` → ``a-root`` and Carol replies ``c-reply`` →
      ``a-root``; Alice also self-replies ``a-self`` → ``a-root``.
    - ``a-reply3`` names ``z-root``, which is stored nowhere, so it contributes
      nothing.

    List ``beta``: Alice posts ``b-root`` and Dave replies ``d-reply`` →
    ``b-root``. ``x-root`` also has a copy on ``beta``, stored first (the lower
    id), to exercise the same-list parent preference.
    """
    alpha = _list(store, "alpha")
    beta = _list(store, "beta")

    a1 = store.upsert_address("alice@example.org", "Alice").id
    a2 = store.upsert_address("alice@work.example", "Alice").id
    bob = store.upsert_address("bob@example.org", "Bob").id
    carol = store.upsert_address("carol@example.org", "Carol").id
    dave = store.upsert_address("dave@example.org", "Dave").id
    person = store.create_person("Alice").id
    store.assign_address_to_person(a1, person)
    store.assign_address_to_person(a2, person)

    ids: dict[str, int] = {}
    # The cross-posted copy of x-root goes in first, so beta holds the lower id.
    ids["x-root@beta"] = _message(store, beta, "x-root", 1, address_id=bob, label="Human")
    ids["x-root"] = _message(store, alpha, "x-root", 2, address_id=bob, label="AI")
    ids["y-root"] = _message(store, alpha, "y-root", 3, address_id=carol, label="AI-Assisted")
    ids["a-root"] = _message(store, alpha, "a-root", 4, address_id=a1, label="Human")
    ids["a-reply1"] = _message(
        store, alpha, "a-reply1", 5, address_id=a1, in_reply_to="<x-root@test>"
    )
    # Deliberately messy header value: the linkage must normalise it the way the
    # reply-timing analysis does (first <...> token wins).
    ids["a-reply2"] = _message(
        store, alpha, "a-reply2", 6, address_id=a2, in_reply_to=" <y-root@test> (comment)"
    )
    ids["a-reply3"] = _message(
        store, alpha, "a-reply3", 7, address_id=a1, in_reply_to="<z-root@test>"
    )
    ids["b-reply"] = _message(
        store, alpha, "b-reply", 8, address_id=bob, in_reply_to="<a-root@test>", label="AI"
    )
    ids["c-reply"] = _message(
        store, alpha, "c-reply", 9, address_id=carol, in_reply_to="<a-root@test>", label="Mixed"
    )
    ids["a-self"] = _message(store, alpha, "a-self", 10, address_id=a1, in_reply_to="<a-root@test>")
    ids["b-root"] = _message(store, beta, "b-root", 11, address_id=a1, label="Human")
    ids["d-reply"] = _message(
        store, beta, "d-reply", 12, address_id=dave, in_reply_to="<b-root@test>", label="Human"
    )
    return {"store": store, "person": person, "ids": ids}


def _by_list(rugs: list[dict]) -> dict[str, dict]:
    return {entry["list"]: entry for entry in rugs}


def _mids(rows: list[dict]) -> list[str]:
    return [row["message_id"] for row in rows]


# --- Store.sender_reply_rugs --------------------------------------------------


def test_person_scope_covers_every_linked_address(fixture):
    rugs = _by_list(fixture["store"].sender_reply_rugs(person_id=fixture["person"]))
    # alpha (4 of Alice's messages) before beta (1), matching summary()'s by_list.
    assert list(rugs) == ["alpha", "beta"]
    # a-reply1 -> x-root, a-reply2 -> y-root, a-self -> a-root; a-reply3's parent
    # is not stored. Newest parent first: a-root (day 4), y-root (3), x-root (2).
    assert _mids(rugs["alpha"]["replied_to"]) == [
        "<a-root@test>",
        "<y-root@test>",
        "<x-root@test>",
    ]


def test_address_scope_covers_only_that_address(fixture):
    rugs = _by_list(fixture["store"].sender_reply_rugs(address="alice@example.org"))
    # a-reply2 was sent from the person's other address, so y-root drops out.
    assert _mids(rugs["alpha"]["replied_to"]) == ["<a-root@test>", "<x-root@test>"]


def test_address_scope_is_case_insensitive(fixture):
    rugs = _by_list(fixture["store"].sender_reply_rugs(address="ALICE@EXAMPLE.ORG"))
    assert _mids(rugs["alpha"]["replied_to"]) == ["<a-root@test>", "<x-root@test>"]


def test_same_list_parent_copy_wins(fixture):
    """x-root is on both lists; the reply is on alpha, so alpha's copy shows."""
    rugs = _by_list(fixture["store"].sender_reply_rugs(person_id=fixture["person"]))
    row = next(r for r in rugs["alpha"]["replied_to"] if r["message_id"] == "<x-root@test>")
    assert row["id"] == fixture["ids"]["x-root"]
    assert row["list"] == "alpha"


def test_reply_from_excludes_the_sender_and_is_newest_first(fixture):
    rugs = _by_list(fixture["store"].sender_reply_rugs(person_id=fixture["person"]))
    # b-reply and c-reply answer a-root; a-self is Alice's own and drops out.
    assert _mids(rugs["alpha"]["reply_from"]) == ["<c-reply@test>", "<b-reply@test>"]
    assert _mids(rugs["beta"]["reply_from"]) == ["<d-reply@test>"]


def test_each_direction_is_scoped_to_its_own_list(fixture):
    rugs = _by_list(fixture["store"].sender_reply_rugs(person_id=fixture["person"]))
    assert rugs["beta"]["replied_to"] == []  # Alice never replies on beta


def test_rug_rows_carry_the_prediction_bucket(fixture):
    rugs = _by_list(fixture["store"].sender_reply_rugs(person_id=fixture["person"]))
    buckets = {
        r["message_id"]: (r["label"], r["prediction_short"]) for r in rugs["alpha"]["replied_to"]
    }
    # The four-band label is preserved; prediction_short un-rebadges AI-Assisted.
    assert buckets["<y-root@test>"] == ("AI-Assisted", "Mixed")
    assert buckets["<x-root@test>"] == ("AI", "AI")
    assert buckets["<a-root@test>"] == ("Human", "Human")


def test_unscored_rug_rows_have_no_bucket(store):
    """An unscored message still gets a bar; the client colours it "unscored"."""
    list_id = _list(store, "alpha")
    addr = store.upsert_address("alice@example.org", "Alice").id
    other = store.upsert_address("bob@example.org", "Bob").id
    _message(store, list_id, "root", 1, address_id=other)  # no extraction/score
    _message(store, list_id, "reply", 2, address_id=addr, in_reply_to="<root@test>")
    rugs = _by_list(store.sender_reply_rugs(address="alice@example.org"))
    row = rugs["alpha"]["replied_to"][0]
    assert row["message_id"] == "<root@test>"
    assert row["label"] is None
    assert row["prediction_short"] is None


def test_rug_rows_carry_the_extraction_status(store):
    """A parent gated under the reliability floor is not a merely unscored one."""
    list_id = _list(store, "alpha")
    addr = store.upsert_address("alice@example.org", "Alice").id
    other = store.upsert_address("bob@example.org", "Bob").id
    gated = _message(store, list_id, "gated", 1, address_id=other)
    store.insert_extraction(
        message_id=gated, extracted_text="tiny", method="test", status="too_short"
    )
    _message(store, list_id, "bare", 2, address_id=other)  # no extraction at all
    _message(store, list_id, "r-gated", 3, address_id=addr, in_reply_to="<gated@test>")
    _message(store, list_id, "r-bare", 4, address_id=addr, in_reply_to="<bare@test>")
    rugs = _by_list(store.sender_reply_rugs(address="alice@example.org"))
    rows = {row["message_id"]: row for row in rugs["alpha"]["replied_to"]}
    assert rows["<gated@test>"]["extraction_status"] == "too_short"
    assert rows["<gated@test>"]["label"] is None
    assert rows["<bare@test>"]["extraction_status"] is None


def test_limit_keeps_the_newest(store):
    list_id = _list(store, "alpha")
    addr = store.upsert_address("alice@example.org", "Alice").id
    other = store.upsert_address("bob@example.org", "Bob").id
    # Alice posts 5 roots, Bob replies to each, Alice replies to each of those.
    for n in range(1, 6):
        _message(store, list_id, f"root{n}", n, address_id=addr)
        _message(store, list_id, f"bob{n}", n + 10, address_id=other, in_reply_to=f"<root{n}@test>")
        _message(store, list_id, f"alice{n}", n + 20, address_id=addr, in_reply_to=f"<bob{n}@test>")
    rugs = _by_list(store.sender_reply_rugs(address="alice@example.org", limit=2))
    assert _mids(rugs["alpha"]["replied_to"]) == ["<bob5@test>", "<bob4@test>"]
    assert _mids(rugs["alpha"]["reply_from"]) == ["<bob5@test>", "<bob4@test>"]


def test_max_lists_caps_the_entries(store):
    addr = store.upsert_address("alice@example.org", "Alice").id
    for n in range(1, 4):
        _message(store, _list(store, f"l{n}"), f"m{n}", n, address_id=addr)
    assert len(store.sender_reply_rugs(address="alice@example.org", max_lists=2)) == 2


def test_unknown_sender_yields_no_entries(fixture):
    assert fixture["store"].sender_reply_rugs(address="nobody@example.org") == []
    assert fixture["store"].sender_reply_rugs(person_id=9999) == []


def test_a_scope_is_required(store):
    with pytest.raises(ValueError):
        store.sender_reply_rugs()


def test_self_referencing_message_is_not_a_reply(store):
    list_id = _list(store, "alpha")
    addr = store.upsert_address("alice@example.org", "Alice").id
    _message(store, list_id, "loop", 1, address_id=addr, in_reply_to="<loop@test>")
    rugs = _by_list(store.sender_reply_rugs(address="alice@example.org"))
    assert rugs["alpha"]["replied_to"] == []
    assert rugs["alpha"]["reply_from"] == []


def test_replies_from_unattributed_messages_still_count(store):
    """A reply whose ``address_id`` is NULL is not the sender's, so it counts."""
    list_id = _list(store, "alpha")
    addr = store.upsert_address("alice@example.org", "Alice").id
    _message(store, list_id, "root", 1, address_id=addr)
    _message(store, list_id, "anon", 2, address_id=None, in_reply_to="<root@test>")
    rugs = _by_list(store.sender_reply_rugs(address="alice@example.org"))
    assert _mids(rugs["alpha"]["reply_from"]) == ["<anon@test>"]


# --- GET /api/senders/reply-rugs ---------------------------------------------


@pytest.fixture()
def client(db_path, fixture):
    """A test client over the thread fixture's database (no built frontend)."""
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


def test_endpoint_returns_the_rugs_for_a_person(client, fixture):
    resp = client.get(f"/api/senders/reply-rugs?person={fixture['person']}")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["person"] == fixture["person"]
    assert body["address"] is None
    assert body["limit"] == REPLY_RUG_LIMIT
    alpha = _by_list(body["by_list"])["alpha"]
    assert _mids(alpha["replied_to"]) == ["<a-root@test>", "<y-root@test>", "<x-root@test>"]
    assert _mids(alpha["reply_from"]) == ["<c-reply@test>", "<b-reply@test>"]
    assert set(alpha["replied_to"][0]) == {
        "id",
        "message_id",
        "list",
        "date",
        "subject",
        "extraction_status",
        "label",
        "prediction_short",
    }


def test_endpoint_returns_the_rugs_for_an_address(client):
    body = client.get("/api/senders/reply-rugs?address=alice@example.org").get_json()
    assert body["person"] is None
    assert body["address"] == "alice@example.org"
    assert _mids(_by_list(body["by_list"])["alpha"]["replied_to"]) == [
        "<a-root@test>",
        "<x-root@test>",
    ]


def test_endpoint_honours_limit(client, fixture):
    body = client.get(f"/api/senders/reply-rugs?person={fixture['person']}&limit=1").get_json()
    assert body["limit"] == 1
    assert _mids(_by_list(body["by_list"])["alpha"]["replied_to"]) == ["<a-root@test>"]


@pytest.mark.parametrize(
    "query",
    [
        "",  # neither person nor address
        "person=1&address=alice@example.org",  # both
        "person=abc",
        "address=alice@example.org&limit=0",
        "address=alice@example.org&limit=x",
    ],
)
def test_endpoint_rejects_bad_input(client, query):
    resp = client.get(f"/api/senders/reply-rugs?{query}")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_endpoint_unknown_sender_is_empty_not_404(client):
    resp = client.get("/api/senders/reply-rugs?address=nobody@example.org")
    assert resp.status_code == 200
    assert resp.get_json()["by_list"] == []
