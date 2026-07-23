"""Tests for the Flask JSON API (Phase 5).

Every test drives ``app.test_client()`` against a seeded temp database (see
:mod:`tests.seed`). Covers each endpoint, every filter (alone and combined),
pagination edges, sort orders, free-text search, summary correctness, person
CRUD + suggestions, input-validation 400s, 404s, the no-frontend JSON notice,
and CORS headers in dev mode.
"""

from __future__ import annotations

import gzip
import io
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from conftest import FakeFolder, FakeImapConn, make_raw

from mailing_list_ai_check.cli import ScoreSummary
from mailing_list_ai_check.config import Config
from mailing_list_ai_check.fetcher import FetchSummary
from mailing_list_ai_check.imap_client import ImapClient
from mailing_list_ai_check.store import Store
from mailing_list_ai_check.webapp import DEV_CORS_ORIGIN, api as webapp_api, create_app

from seed import seed


def _config(db_path: Path) -> Config:
    return Config(
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


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "web.db"
    with Store(path) as store:
        seed(store)
    return path


@pytest.fixture
def client(db_path):
    """A test client in dev mode (no built frontend)."""
    app = create_app(_config(db_path), frontend_dist=None)
    app.testing = True
    return app.test_client()


def _ids(messages):
    return {m["message_id"] for m in messages}


# --- /api/messages ------------------------------------------------------------


def test_messages_default(client):
    resp = client.get("/api/messages")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == 15
    assert body["page"] == 1
    assert body["per_page"] == 50
    assert body["pages"] == 1
    assert len(body["messages"]) == 15


def test_message_row_shape(client):
    body = client.get("/api/messages?list=announce&label=AI").get_json()
    assert body["total"] == 1
    row = body["messages"][0]
    assert row["message_id"] == "<m1@test>"
    assert row["from"] == {"address": "alice@example.org", "display_name": "Alice Smith"}
    assert row["person"]["name"] == "Alice Smith"
    assert row["extraction"] == {
        "status": "ok",
        "method": "email-reply-parser",
        "char_count": len("Body of Intro to draft"),
    }
    assert row["score"]["label"] == "AI"
    assert row["score"]["fraction_ai"] == pytest.approx(0.95)


def test_row_without_extraction_or_score(client):
    body = client.get("/api/messages?q=No extraction yet").get_json()
    row = body["messages"][0]
    assert row["extraction"] is None
    assert row["score"] is None


@pytest.mark.parametrize(
    "query,expected_total",
    [
        ("list=announce", 7),
        ("address=bob@example.org", 3),
        ("label=AI", 3),
        ("label=Human", 3),
        ("date_from=2026-02-01&date_to=2026-02-28", 5),
        ("min_likelihood=0.5", 5),
        ("max_likelihood=0.1", 3),
        ("has_score=true", 9),
        ("has_score=false", 6),
        ("q=QUIC", 3),
        ("list=announce&label=AI", 1),  # combined
        ("list=last-call&has_score=true", 3),  # combined
    ],
)
def test_message_filters(client, query, expected_total):
    body = client.get(f"/api/messages?{query}").get_json()
    assert body["total"] == expected_total


def test_filter_by_person(client):
    body = client.get("/api/messages?person=1&per_page=200").get_json()
    assert body["total"] == 5
    assert _ids(body["messages"]) == {
        "<m1@test>",
        "<m2@test>",
        "<m7@test>",
        "<m11@test>",
        "<m15@test>",
    }


def test_pagination_edges(client):
    p1 = client.get("/api/messages?page=1&per_page=10").get_json()
    p2 = client.get("/api/messages?page=2&per_page=10").get_json()
    p3 = client.get("/api/messages?page=3&per_page=10").get_json()
    assert len(p1["messages"]) == 10
    assert p1["pages"] == 2
    assert len(p2["messages"]) == 5
    assert p3["messages"] == []  # beyond the end


def test_per_page_cap(client):
    body = client.get("/api/messages?per_page=100000").get_json()
    assert body["per_page"] == 200  # capped
    assert len(body["messages"]) == 15


def test_sort_orders(client):
    asc = client.get("/api/messages?sort=date&order=asc&per_page=200").get_json()
    desc = client.get("/api/messages?sort=date&order=desc&per_page=200").get_json()
    assert asc["messages"][0]["message_id"] == "<m1@test>"
    assert desc["messages"][0]["message_id"] == "<m15@test>"
    frac = client.get("/api/messages?sort=fraction_ai&order=desc&per_page=200").get_json()
    assert frac["messages"][0]["message_id"] == "<m14@test>"


@pytest.mark.parametrize(
    "query",
    [
        "page=abc",
        "page=0",
        "per_page=xyz",
        "per_page=0",
        "sort=bogus",
        "order=sideways",
        "min_likelihood=high",
        "min_likelihood=2",
        "max_likelihood=-1",
        "person=notanint",
        "date_from=not-a-date",
        "has_score=maybe",
    ],
)
def test_bad_query_params_return_400(client, query):
    resp = client.get(f"/api/messages?{query}")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --- /api/messages/<id> -------------------------------------------------------


def test_message_detail(client, db_path):
    # Find m2's db id via its message_id.
    with Store(db_path) as store:
        m2_id = store.find_message_by_message_id("<m2@test>").id
        m1_id = store.find_message_by_message_id("<m1@test>").id
    body = client.get(f"/api/messages/{m2_id}").get_json()
    assert body["message_id"] == "<m2@test>"
    assert body["raw_body"] == "RAW Re: Intro to draft"
    assert body["in_reply_to"] == "<m1@test>"
    assert body["thread_parent_id"] == m1_id  # resolved to stored parent
    assert body["extraction"]["extracted_text"] == "Body of Re: Intro to draft"
    assert body["score"]["label"] == "Human"
    assert body["score"]["raw_response"]["prediction_short"] == "Human"


def test_message_detail_reports_ignored_lines_and_scored_word_count(client, db_path):
    # Seed a message whose extraction contains a greeting + sign-off + signature,
    # then assert message_detail reports which lines scoring would drop and the
    # word count of what would actually be sent to the detector.
    text = (
        "Hi all,\n"  # 0: greeting (dropped)
        "This is the substantive content of my message.\n"  # 1: kept
        "It has a couple of lines worth scoring here.\n"  # 2: kept
        "\n"  # 3: blank (never reported)
        "Best,\n"  # 4: sign-off (dropped)
        "Alice\n"  # 5: sign-off name (dropped)
        "-- \n"  # 6: signature delimiter (dropped)
        "Alice Example\n"  # 7: after delimiter (dropped)
        "ORCID: 0000-0002"  # 8: after delimiter (dropped)
    )
    with Store(db_path) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce").id
        addr = store.upsert_address("frank@example.org", "Frank").id
        msg = store.upsert_message(
            message_id="<furniture@test>",
            list_id=lst,
            address_id=addr,
            subject="Furniture",
            date="2026-03-30T10:00:00",
            in_reply_to=None,
            raw_body="RAW Furniture",
            uid=None,
        ).message
        store.insert_extraction(message_id=msg.id, extracted_text=text, method="erp", status="ok")
        msg_id = msg.id

    body = client.get(f"/api/messages/{msg_id}").get_json()
    extraction = body["extraction"]
    # The full stage-1 text (furniture included) is still returned verbatim.
    assert extraction["extracted_text"] == text
    # Only non-blank furniture lines are reported; the blank line 3 is not.
    assert extraction["ignored_lines"] == [0, 4, 5, 6, 7, 8]
    # scored_word_count is the two surviving content lines (8 + 9 words).
    assert extraction["scored_word_count"] == 17


def test_message_detail_ignored_lines_reflect_html_signature_hint(client, db_path):
    # A signature line with no "-- " delimiter and no recognizable contact shape
    # would survive ordinary cleaning, but the message's HTML marks it as a
    # signature. message_detail must apply that hint so ignored_lines/scored count
    # reflect exactly what scoring drops.
    sig_line = "Frank Q Example Distinguished Engineer at Example Systems Group"
    text = (
        "This is the substantive content of my message for the group today.\n"  # 0 kept
        "It carries a second line of genuine review commentary to score.\n"  # 1 kept
        f"{sig_line}"  # 2 dropped only via the HTML signature hint
    )
    raw_html = (
        "<div>This is the substantive content of my message for the group today.</div>"
        "<div>It carries a second line of genuine review commentary to score.</div>"
        f'<div class="gmail_signature">{sig_line}</div>'
    )
    with Store(db_path) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce").id
        addr = store.upsert_address("frank@example.org", "Frank").id
        msg = store.upsert_message(
            message_id="<htmlsig@test>",
            list_id=lst,
            address_id=addr,
            subject="HTML sig",
            date="2026-03-31T10:00:00",
            in_reply_to=None,
            raw_body="RAW",
            uid=None,
            raw_html=raw_html,
        ).message
        store.insert_extraction(message_id=msg.id, extracted_text=text, method="erp", status="ok")
        msg_id = msg.id

    extraction = client.get(f"/api/messages/{msg_id}").get_json()["extraction"]
    assert extraction["ignored_lines"] == [2]  # the signature line, via the hint
    assert extraction["scored_word_count"] == len(
        (
            "This is the substantive content of my message for the group today. "
            "It carries a second line of genuine review commentary to score."
        ).split()
    )


def test_message_detail_no_thread_parent(client, db_path):
    with Store(db_path) as store:
        m1_id = store.find_message_by_message_id("<m1@test>").id
    body = client.get(f"/api/messages/{m1_id}").get_json()
    assert body["thread_parent_id"] is None


def test_message_detail_404(client):
    resp = client.get("/api/messages/99999")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


# --- /api/summary -------------------------------------------------------------


def test_summary(client):
    body = client.get("/api/summary").get_json()
    assert body["total"] == 15
    assert body["extracted"] == 10
    assert body["scored"] == 9
    assert body["too_short"] == 1
    assert body["label_distribution"] == {"AI": 3, "Human": 3, "AI-Assisted": 2, "Mixed": 1}
    assert body["avg_fraction_ai"] == pytest.approx(4.52 / 9)
    assert len(body["by_month"]) == 3


def test_summary_respects_filters(client):
    body = client.get("/api/summary?list=quic").get_json()
    assert body["total"] == 3
    assert body["scored"] == 1
    assert body["label_distribution"] == {"AI": 1}


def test_summary_db_size_bytes(client):
    # The test client is file-backed (see the db_path fixture), so the SQLite
    # file exists and has a positive size.
    body = client.get("/api/summary").get_json()
    assert body["db_size_bytes"] > 0


# --- /api/lists, /api/addresses, /api/persons ---------------------------------


def test_lists_endpoint(client):
    lists = client.get("/api/lists").get_json()["lists"]
    counts = {row["name"]: row["message_count"] for row in lists}
    assert counts == {"announce": 7, "last-call": 5, "quic": 3}


def test_addresses_endpoint_and_q(client):
    assert len(client.get("/api/addresses").get_json()["addresses"]) == 6
    filtered = client.get("/api/addresses?q=alice").get_json()["addresses"]
    assert {a["email"] for a in filtered} == {"alice@example.org", "alice@work.example"}


def test_persons_endpoint(client):
    persons = {p["canonical_name"]: p for p in client.get("/api/persons").get_json()["persons"]}
    assert set(persons) == {"Alice Smith", "Bob Jones"}
    assert persons["Alice Smith"]["message_count"] == 5


def test_lists_endpoint_label_mix(client):
    rows = {row["name"]: row for row in client.get("/api/lists").get_json()["lists"]}
    assert rows["announce"]["scored_count"] == 5
    assert rows["announce"]["label_counts"] == {
        "AI": 1,
        "Human": 2,
        "AI-Assisted": 1,
        "Mixed": 1,
    }
    assert rows["quic"]["label_counts"] == {"AI": 1}


# --- /api/senders -------------------------------------------------------------


def _senders_by_name(body):
    return {row["name"]: row for row in body["senders"]}


def test_senders_default(client):
    body = client.get("/api/senders").get_json()
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["per_page"] == 60
    assert body["sort"] == "count"
    assert body["order"] == "desc"  # natural default for count
    # Default sort is count desc, ties broken by name asc.
    assert [row["name"] for row in body["senders"]] == [
        "Alice Smith",
        "Bob Jones",
        "Carol",
        "Dave",
        "Eve",
    ]


def test_senders_person_and_unlinked_shape(client):
    senders = _senders_by_name(client.get("/api/senders").get_json())

    alice = senders["Alice Smith"]
    assert alice["type"] == "person"
    assert "person_id" in alice
    assert alice["emails"] == ["alice@example.org", "alice@work.example"]
    assert alice["message_count"] == 5
    assert alice["label_counts"] == {"AI": 1, "Human": 1, "AI-Assisted": 1, "Mixed": 1}

    carol = senders["Carol"]
    assert carol["type"] == "address"
    assert "address_id" in carol
    assert carol["emails"] == ["carol@example.org"]
    assert carol["label_counts"] == {"Human": 2, "AI": 1}

    # Linked addresses never surface as their own entry.
    assert "bob@example.org" not in senders


def test_senders_q_over_name_and_email(client):
    by_name = client.get("/api/senders?q=alice").get_json()
    assert {r["name"] for r in by_name["senders"]} == {"Alice Smith"}
    assert by_name["total"] == 1
    by_email = client.get("/api/senders?q=work.example").get_json()
    assert {r["name"] for r in by_email["senders"]} == {"Alice Smith"}
    assert client.get("/api/senders?q=nobody").get_json()["total"] == 0


def test_senders_sort_name_default_order(client):
    body = client.get("/api/senders?sort=name").get_json()
    assert body["order"] == "asc"  # natural default for name
    assert [r["name"] for r in body["senders"]] == [
        "Alice Smith",
        "Bob Jones",
        "Carol",
        "Dave",
        "Eve",
    ]


def test_senders_sort_count_asc_explicit(client):
    body = client.get("/api/senders?sort=count&order=asc").get_json()
    assert [r["name"] for r in body["senders"]] == [
        "Dave",
        "Eve",
        "Bob Jones",
        "Carol",
        "Alice Smith",
    ]


def test_senders_pagination_and_total(client):
    p1 = client.get("/api/senders?sort=name&order=asc&page=1&per_page=2").get_json()
    p2 = client.get("/api/senders?sort=name&order=asc&page=2&per_page=2").get_json()
    p3 = client.get("/api/senders?sort=name&order=asc&page=3&per_page=2").get_json()
    assert p1["total"] == p2["total"] == p3["total"] == 5
    assert [r["name"] for r in p1["senders"]] == ["Alice Smith", "Bob Jones"]
    assert [r["name"] for r in p2["senders"]] == ["Carol", "Dave"]
    assert [r["name"] for r in p3["senders"]] == ["Eve"]


def test_senders_per_page_cap(client):
    body = client.get("/api/senders?per_page=100000").get_json()
    assert body["per_page"] == 200  # clamped to MAX_PER_PAGE


def test_senders_list_param_filters_and_echoes(client):
    body = client.get("/api/senders?list=quic").get_json()
    assert body["list"] == "quic"
    # quic messages: m13(a3) m14(a4) m15(a2) -> Alice, Bob, Carol only.
    assert body["total"] == 3
    senders = _senders_by_name(body)
    assert set(senders) == {"Alice Smith", "Bob Jones", "Carol"}
    assert senders["Carol"]["message_count"] == 1
    assert senders["Carol"]["label_counts"] == {"AI": 1}


def test_senders_list_param_default_is_null(client):
    body = client.get("/api/senders").get_json()
    assert body["list"] is None


def test_senders_unknown_list_returns_empty(client):
    body = client.get("/api/senders?list=does-not-exist").get_json()
    assert body["list"] == "does-not-exist"
    assert body["total"] == 0
    assert body["senders"] == []


@pytest.mark.parametrize(
    "query",
    [
        "sort=bogus",
        "order=sideways",
        "page=abc",
        "page=0",
        "per_page=xyz",
        "per_page=0",
    ],
)
def test_senders_bad_query_params_return_400(client, query):
    resp = client.get(f"/api/senders?{query}")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_person_suggestions(client):
    body = client.get("/api/persons/suggestions").get_json()
    suggestions = {s["display_name"]: s for s in body["suggestions"]}
    # "Alice Smith" is the only display name shared by 2+ distinct emails.
    assert "Alice Smith" in suggestions
    assert set(suggestions["Alice Smith"]["emails"]) == {
        "alice@example.org",
        "alice@work.example",
    }


# --- person CRUD --------------------------------------------------------------


def test_person_crud_roundtrip(client, db_path):
    with Store(db_path) as store:
        carol_id = store.address_rows("carol")[0]["id"]
        dave_id = store.address_rows("dave")[0]["id"]

    # Create with one address assigned.
    resp = client.post(
        "/api/persons",
        json={"canonical_name": "Carol Danvers", "address_ids": [carol_id]},
    )
    assert resp.status_code == 201
    person = resp.get_json()
    pid = person["id"]
    assert {a["id"] for a in person["addresses"]} == {carol_id}

    # Rename + add another address.
    resp = client.put(
        f"/api/persons/{pid}",
        json={"canonical_name": "Carol D.", "add_address_ids": [dave_id]},
    )
    assert resp.status_code == 200
    updated = resp.get_json()
    assert updated["canonical_name"] == "Carol D."
    assert {a["id"] for a in updated["addresses"]} == {carol_id, dave_id}

    # Detach one.
    resp = client.put(f"/api/persons/{pid}", json={"remove_address_ids": [carol_id]})
    assert {a["id"] for a in resp.get_json()["addresses"]} == {dave_id}

    # Delete detaches remaining address, not deletes it.
    assert client.delete(f"/api/persons/{pid}").status_code == 200
    assert client.get(f"/api/messages?person={pid}").get_json()["total"] == 0
    with Store(db_path) as store:
        assert store.get_address(dave_id).person_id is None


def test_person_create_validation(client):
    assert client.post("/api/persons", json={}).status_code == 400
    assert client.post("/api/persons", json={"canonical_name": ""}).status_code == 400
    # Non-existent address id.
    resp = client.post("/api/persons", json={"canonical_name": "X", "address_ids": [99999]})
    assert resp.status_code == 404
    # Bad address_ids type.
    resp = client.post("/api/persons", json={"canonical_name": "X", "address_ids": "nope"})
    assert resp.status_code == 400


def test_person_update_and_delete_404(client):
    assert client.put("/api/persons/99999", json={"canonical_name": "X"}).status_code == 404
    assert client.delete("/api/persons/99999").status_code == 404


# --- frontend / CORS / errors -------------------------------------------------


def test_no_frontend_json_notice(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["api_base"] == "/api"
    assert "not been built" in body["message"]


def test_cors_headers_in_dev_mode(client):
    resp = client.get("/api/lists")
    assert resp.headers["Access-Control-Allow-Origin"] == DEV_CORS_ORIGIN
    assert "GET" in resp.headers["Access-Control-Allow-Methods"]


def test_production_serves_frontend(tmp_path, db_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>dashboard</title>")
    app = create_app(_config(db_path), frontend_dist=dist)
    app.testing = True
    c = app.test_client()

    # Root serves the SPA shell.
    root = c.get("/")
    assert root.status_code == 200
    assert b"dashboard" in root.data
    # Unknown client route also falls back to index.html.
    assert b"dashboard" in c.get("/explorer").data
    # API still works and is not shadowed.
    assert c.get("/api/lists").status_code == 200
    # Unknown API route still returns JSON 404, not the SPA shell.
    missing = c.get("/api/nope")
    assert missing.status_code == 404
    assert "error" in missing.get_json()
    # No CORS header in production mode.
    assert "Access-Control-Allow-Origin" not in c.get("/api/lists").headers


def test_unknown_api_route_404_dev(client):
    resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


# --- /api/pull (fetch + extract + score) --------------------------------------
#
# The pipeline is mocked at the api-module boundary (open_client / resolve_folders
# / run_fetch / run_extract / run_score / PangramClient) so no test ever touches
# the network or the paid Pangram API.


class _FakeImapClient:
    def close(self) -> None:
        pass

    def logout(self) -> None:
        pass


def _pull_client(db_path: Path, *, pangram_key: str = "test-key"):
    config = replace(_config(db_path), pangram_api_key=pangram_key)
    app = create_app(config, frontend_dist=None)
    app.testing = True
    return app.test_client()


def test_pull_happy_path_with_scoring(db_path, monkeypatch):
    calls: dict = {}

    def fake_resolve_folders(client, names, all_lists=False):
        calls["names"] = list(names)
        return [f"Shared Folders/{names[0]}"]

    def fake_run_fetch(client, store, request):
        calls["request"] = request
        return FetchSummary(fetched=5, duplicates=1, parse_errors=0)

    def fake_run_extract(store, limit=None):
        calls["extract_limit"] = limit
        return Counter({"ok": 4, "empty": 1}), Counter({"email-reply-parser": 5})

    def fake_run_score(store, client, *, limit=None, **kwargs):
        calls["score_limit"] = limit
        return ScoreSummary(scored=3, cache_hits=1, too_short=1, api_calls=3)

    class FakePangram:
        def __init__(self, key):
            calls["pangram_key"] = key

    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: _FakeImapClient())
    monkeypatch.setattr(webapp_api, "resolve_folders", fake_resolve_folders)
    monkeypatch.setattr(webapp_api, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(webapp_api, "run_extract", fake_run_extract)
    monkeypatch.setattr(webapp_api, "run_score", fake_run_score)
    monkeypatch.setattr(webapp_api, "PangramClient", FakePangram)

    resp = _pull_client(db_path).post("/api/pull", json={"list": "newlist", "count": 25})
    assert resp.status_code == 200
    assert resp.get_json() == {
        "fetched": 5,
        "duplicates": 1,
        "parse_errors": 0,
        "extracted": 4,
        "empty": 1,
        "too_short": 1,
        "scored": 3,
        "cache_hits": 1,
        "api_calls": 3,
        "scoring_skipped": False,
    }
    # count drives depth, the fetch cap, extraction and scoring limits.
    assert calls["names"] == ["newlist"]
    assert calls["request"].depth.count == 25
    assert calls["request"].limit == 25
    assert calls["extract_limit"] == 25
    assert calls["score_limit"] == 25
    assert calls["pangram_key"] == "test-key"


def test_pull_skips_scoring_without_api_key(db_path, monkeypatch):
    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: _FakeImapClient())
    monkeypatch.setattr(
        webapp_api, "resolve_folders", lambda c, names, all_lists=False: ["Shared Folders/x"]
    )
    monkeypatch.setattr(webapp_api, "run_fetch", lambda c, s, r: FetchSummary(fetched=2))
    monkeypatch.setattr(
        webapp_api, "run_extract", lambda s, limit=None: (Counter({"ok": 2}), Counter())
    )

    def _must_not_call(*a, **k):
        raise AssertionError("run_score must not run without an API key")

    monkeypatch.setattr(webapp_api, "run_score", _must_not_call)

    resp = _pull_client(db_path, pangram_key="").post(
        "/api/pull", json={"list": "newlist", "count": 5}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["scoring_skipped"] is True
    assert body["scored"] == 0
    assert body["cache_hits"] == 0
    assert body["api_calls"] == 0
    assert body["too_short"] == 0
    assert body["fetched"] == 2
    assert body["extracted"] == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"count": 10},  # missing list
        {"list": "", "count": 10},  # empty list
        {"list": "   ", "count": 10},  # whitespace-only list
        {"list": "bad name", "count": 10},  # space is not allowed
        {"list": "bad/name", "count": 10},  # slash not allowed
        {"list": "ok"},  # missing count
        {"list": "ok", "count": 0},  # below min
        {"list": "ok", "count": 1001},  # above max
        {"list": "ok", "count": "ten"},  # non-int
        {"list": "ok", "count": 1.5},  # float is not an int
        {"list": "ok", "count": True},  # bool is not a valid int here
    ],
)
def test_pull_validation_400(client, payload):
    resp = client.post("/api/pull", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --- /api/lists/regenerate ------------------------------------------------------


def test_regenerate_lists_reconciles_and_returns_counts(db_path, monkeypatch):
    # The seed has announce/last-call/quic, all with messages. The fake server
    # enumeration drops last-call and adds wimse: last-call must survive with a
    # removed_from_server_at stamp, wimse must appear.
    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: _FakeImapClient())

    def fake_refresh(client, store):
        # Mirror the real fetcher.refresh_lists_index contract: the store
        # reconciliation counts plus the two activity-check keys it appends.
        counts = store.refresh_lists_index(
            [
                ("announce", "Shared Folders/announce"),
                ("quic", "Shared Folders/quic"),
                ("wimse", "Shared Folders/wimse"),
            ]
        )
        counts["activity_checked"] = 0
        counts["activity_failed"] = 0
        return counts

    monkeypatch.setattr(webapp_api, "refresh_lists_index", fake_refresh)

    c = _pull_client(db_path)
    resp = c.post("/api/lists/regenerate")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "added": 1,
        "restored": 0,
        "deleted": 0,
        "kept_missing": 1,
        "total": 4,
        "activity_checked": 0,
        "activity_failed": 0,
    }
    rows = {row["name"]: row for row in c.get("/api/lists").get_json()["lists"]}
    assert set(rows) == {"announce", "last-call", "quic", "wimse"}
    assert rows["last-call"]["removed_from_server_at"] is not None
    assert rows["announce"]["removed_from_server_at"] is None
    # /api/lists now exposes the new column on every row.
    assert all("last_message_at" in row for row in rows.values())


def test_regenerate_lists_imap_connect_failure_502(db_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(webapp_api, "open_client", _boom)
    resp = _pull_client(db_path).post("/api/lists/regenerate")
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_regenerate_lists_enumeration_failure_502(db_path, monkeypatch):
    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: _FakeImapClient())

    def _boom(client, store):
        raise RuntimeError("LIST failed")

    monkeypatch.setattr(webapp_api, "refresh_lists_index", _boom)
    resp = _pull_client(db_path).post("/api/lists/regenerate")
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_pull_imap_connect_failure_502(db_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(webapp_api, "open_client", _boom)
    resp = _pull_client(db_path).post("/api/pull", json={"list": "newlist", "count": 5})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_pull_fetch_failure_502(db_path, monkeypatch):
    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: _FakeImapClient())
    monkeypatch.setattr(
        webapp_api, "resolve_folders", lambda c, names, all_lists=False: ["Shared Folders/x"]
    )

    def _boom(*a, **k):
        raise RuntimeError("EXAMINE failed")

    monkeypatch.setattr(webapp_api, "run_fetch", _boom)
    resp = _pull_client(db_path).post("/api/pull", json={"list": "newlist", "count": 5})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


# --- /api/export, /api/import -------------------------------------------------
#
# The seed carries 15 messages across 3 lists, 13 extraction rows (every status),
# and 9 scores; export/import counts below are read against those totals.


def _empty_client(tmp_path, name="empty.db"):
    """A dev-mode client over a fresh, schema-initialised but empty database."""
    path = tmp_path / name
    with Store(path):
        pass  # opening a new path runs the migrations, creating an empty schema
    app = create_app(_config(path), frontend_dist=None)
    app.testing = True
    return path, app.test_client()


def _records(gz_bytes):
    """Decode a gzip JSON Lines export body into its list of records."""
    text = gzip.decompress(gz_bytes).decode("utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _multipart(data_bytes, filename="mlac-export.jsonl.gz"):
    return {"file": (io.BytesIO(data_bytes), filename)}


def test_export_all_lists(client):
    resp = client.get("/api/export")
    assert resp.status_code == 200
    assert resp.mimetype == "application/gzip"
    disposition = resp.headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert disposition.endswith('.jsonl.gz"')

    records = _records(resp.data)
    assert records[0]["type"] == "header"
    assert records[0]["format"] == "mlac-export"
    assert records[-1]["type"] == "trailer"
    assert records[-1]["messages"] == 15


def test_export_single_list(client):
    resp = client.get("/api/export?list=announce")
    assert resp.status_code == 200
    header = _records(resp.data)[0]
    # Only the requested list's folder is present.
    assert header["folders"] == ["Shared Folders/announce"]


def test_export_unknown_list_404(client):
    resp = client.get("/api/export?list=does-not-exist")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_export_empty_db_404(tmp_path):
    _, c = _empty_client(tmp_path)
    resp = c.get("/api/export")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_import_roundtrip(client, tmp_path):
    export_bytes = client.get("/api/export").data
    _, c2 = _empty_client(tmp_path)

    resp = c2.post("/api/import", data=_multipart(export_bytes), content_type="multipart/form-data")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["dry_run"] is False
    assert body["messages_inserted"] == 15
    assert body["extractions_inserted"] == 13
    assert body["scores_inserted"] == 9
    # The imported data is now queryable in the target.
    assert c2.get("/api/messages").get_json()["total"] == 15

    # Re-importing the same file is a no-op: every message is skipped, nothing new.
    again = c2.post(
        "/api/import", data=_multipart(export_bytes), content_type="multipart/form-data"
    ).get_json()
    assert again["messages_skipped"] == 15
    assert again["messages_inserted"] == 0
    assert again["extractions_inserted"] == 0
    assert again["scores_inserted"] == 0


def test_import_dry_run_leaves_target_unchanged(client, tmp_path):
    export_bytes = client.get("/api/export").data
    _, c2 = _empty_client(tmp_path)

    resp = c2.post(
        "/api/import?dry_run=true",
        data=_multipart(export_bytes),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["dry_run"] is True
    assert body["messages_inserted"] == 15  # reported...
    assert c2.get("/api/messages").get_json()["total"] == 0  # ...but nothing written


def test_import_no_file_400(tmp_path):
    _, c2 = _empty_client(tmp_path)
    resp = c2.post("/api/import", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_import_corrupt_file_400(tmp_path):
    _, c2 = _empty_client(tmp_path)
    resp = c2.post(
        "/api/import",
        data=_multipart(b"this is not a valid export\n", filename="x.jsonl"),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    # All-or-nothing: the failed import left the target empty.
    assert c2.get("/api/messages").get_json()["total"] == 0


# --- /api/lists/preview + /api/pull/range -------------------------------------
#
# Both endpoints back the dashboard's "Add messages" popover. open_client is
# monkeypatched to return a real ImapClient over the network-free FakeImapConn
# (so EXAMINE / UID SEARCH / header + body FETCH all run against an in-memory
# folder), while run_extract / run_score / PangramClient are faked at the api
# boundary so no test touches the network or the paid Pangram API.


def _server_folder(uids, *, uidvalidity=1000, name="announce"):
    """A FakeFolder of real messages at ``uids`` (distinct sender/subject each)."""
    fd = FakeFolder(uidvalidity=uidvalidity, uidnext=(max(uids) + 1 if uids else 1))
    fd.exists = len(uids)
    for uid in uids:
        fd.messages[uid] = make_raw(
            message_id=f"<msg{uid}@x>",
            from_header=f"User{uid} <user{uid}@example.org>",
            subject=f"Subject {uid}",
            date="Mon, 06 Jan 2025 10:00:00 +0000",
        )
        fd.dates[uid] = datetime(2025, 1, 6, 10, 0, 0)
        fd.froms[uid] = f"user{uid}@example.org"
    return fd


def _range_db(tmp_path, *, stored_uids, cursor=None, name="announce"):
    """A fresh db with list ``name``, messages at ``stored_uids`` and opt. cursor."""
    db = tmp_path / "range.db"
    with Store(db) as store:
        lst = store.upsert_list(name, f"Shared Folders/{name}")
        addr = store.upsert_address("stored@example.org", "Stored")
        for uid in stored_uids:
            store.upsert_message(
                message_id=f"<stored{uid}@x>",
                list_id=lst.id,
                address_id=addr.id,
                subject=f"stored {uid}",
                date="2025-01-01T00:00:00+00:00",
                in_reply_to=None,
                raw_body="stored body",
                uid=uid,
            )
        if cursor is not None:
            store.set_pull_state(lst.id, cursor[0], cursor[1])
    return db


def _client_over(db, conn, monkeypatch, *, pangram_key=""):
    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: ImapClient(conn))
    config = replace(_config(db), pangram_api_key=pangram_key)
    app = create_app(config, frontend_dist=None)
    app.testing = True
    return app.test_client()


# --- preview ------------------------------------------------------------------


def test_preview_new_lists_first_25_ascending(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[])  # nothing stored -> baseline 0
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 31))})
    c = _client_over(db, conn, monkeypatch)

    body = c.post("/api/lists/preview", json={"list": "announce", "mode": "new"}).get_json()
    assert body["mode"] == "new"
    assert body["list"] == "announce"
    assert body["total"] == 30
    assert body["shown"] == 25  # only the first 25 are previewed
    assert body["more"] == 5
    # First 25 ascending: uids 1..25, oldest first.
    assert [m["from_email"] for m in body["messages"][:2]] == [
        "user1@example.org",
        "user2@example.org",
    ]
    assert body["messages"][-1]["from_email"] == "user25@example.org"
    assert body["messages"][0]["subject"] == "Subject 1"
    assert body["messages"][0]["date"] == "2025-01-06T10:00:00+00:00"


def test_preview_before_lists_last_count_ascending(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5])  # earliest stored uid is 5
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 11))})
    c = _client_over(db, conn, monkeypatch)

    body = c.post(
        "/api/lists/preview", json={"list": "announce", "mode": "before", "count": 2}
    ).get_json()
    assert body["mode"] == "before"
    # Older-than-5 uids on the server are 1..4.
    assert body["total"] == 4
    assert body["shown"] == 2
    assert body["more"] == 2
    # The LAST 2 (immediately preceding uid 5), ascending: uids 3, 4.
    assert [m["from_email"] for m in body["messages"]] == [
        "user3@example.org",
        "user4@example.org",
    ]


def test_preview_before_no_stored_uids_404(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[])  # nothing to anchor "before"
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 5))})
    c = _client_over(db, conn, monkeypatch)
    resp = c.post("/api/lists/preview", json={"list": "announce", "mode": "before"})
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_preview_unknown_list_404(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[1])
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder([1])})
    c = _client_over(db, conn, monkeypatch)
    resp = c.post("/api/lists/preview", json={"list": "nope", "mode": "new"})
    assert resp.status_code == 404
    assert "error" in resp.get_json()


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "new"},  # missing list
        {"list": "bad name", "mode": "new"},  # space not allowed
        {"list": "announce"},  # missing mode
        {"list": "announce", "mode": "sideways"},  # bad mode
        {"list": "announce", "mode": "before", "count": "ten"},  # non-int count
        {"list": "announce", "mode": "before", "count": True},  # bool not an int
    ],
)
def test_preview_validation_400(tmp_path, monkeypatch, payload):
    db = _range_db(tmp_path, stored_uids=[5])
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 6))})
    c = _client_over(db, conn, monkeypatch)
    resp = c.post("/api/lists/preview", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_preview_imap_connect_failure_502(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5])

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(webapp_api, "open_client", _boom)
    config = replace(_config(db), pangram_api_key="")
    app = create_app(config, frontend_dist=None)
    app.testing = True
    resp = app.test_client().post("/api/lists/preview", json={"list": "announce", "mode": "new"})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


# --- ranged pull --------------------------------------------------------------


def _fake_pipeline(monkeypatch, calls, *, scored=False):
    def fake_extract(store, limit=None):
        calls["extract_limit"] = limit
        return Counter({"ok": 1}), Counter({"email-reply-parser": 1})

    monkeypatch.setattr(webapp_api, "run_extract", fake_extract)

    def fake_score(store, client, *, limit=None, **kw):
        calls["score_limit"] = limit
        return ScoreSummary(scored=2, cache_hits=1, too_short=0, api_calls=2)

    monkeypatch.setattr(webapp_api, "run_score", fake_score)
    monkeypatch.setattr(webapp_api, "PangramClient", lambda key: None)


def test_pull_range_new_with_cursor_advances_pull_state(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5], cursor=(1000, 5))
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 9))})
    calls: dict = {}
    _fake_pipeline(monkeypatch, calls)
    c = _client_over(db, conn, monkeypatch)

    body = c.post(
        "/api/pull/range", json={"list": "announce", "mode": "new", "count": 2}
    ).get_json()
    # Baseline is the cursor's last_uid 5, so new uids are 6,7,8; first 2 -> 6,7.
    assert body["mode"] == "new"
    assert body["matched"] == 3
    assert body["capped"] is False
    assert body["fetched"] == 2
    assert body["scoring_skipped"] is True  # no api key on this client
    assert calls["extract_limit"] == 2
    # Cursor advanced to the max fetched uid (7), same UIDVALIDITY.
    with Store(db) as store:
        lst = store.get_list_by_name("announce")
        ps = store.get_pull_state(lst.id)
        assert (ps.uidvalidity, ps.last_uid) == (1000, 7)


def test_pull_range_new_without_cursor_falls_back_to_max_stored_uid(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5])  # no cursor
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 9))})
    calls: dict = {}
    _fake_pipeline(monkeypatch, calls)
    c = _client_over(db, conn, monkeypatch)

    body = c.post("/api/pull/range", json={"list": "announce", "mode": "new"}).get_json()
    # Baseline = max stored uid (5); new = 6,7,8; count omitted -> all.
    assert body["matched"] == 3
    assert body["capped"] is False
    assert body["fetched"] == 3
    with Store(db) as store:
        lst = store.get_list_by_name("announce")
        assert store.get_pull_state(lst.id).last_uid == 8


def test_pull_range_new_uidvalidity_mismatch_ignores_stale_cursor(tmp_path, monkeypatch):
    # Cursor is from a DIFFERENT UIDVALIDITY with a high last_uid; it must be
    # ignored in favour of the max stored uid, and then rewritten.
    db = _range_db(tmp_path, stored_uids=[5], cursor=(999, 100))
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 9))})
    calls: dict = {}
    _fake_pipeline(monkeypatch, calls)
    c = _client_over(db, conn, monkeypatch)

    body = c.post("/api/pull/range", json={"list": "announce", "mode": "new"}).get_json()
    assert body["matched"] == 3  # 6,7,8 (baseline fell back to stored max 5)
    with Store(db) as store:
        lst = store.get_list_by_name("announce")
        ps = store.get_pull_state(lst.id)
        assert (ps.uidvalidity, ps.last_uid) == (1000, 8)  # rewritten to server's


def test_pull_range_before_never_touches_pull_state(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5, 6], cursor=(1000, 6))
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 7))})
    calls: dict = {}
    _fake_pipeline(monkeypatch, calls)
    c = _client_over(db, conn, monkeypatch)

    body = c.post(
        "/api/pull/range", json={"list": "announce", "mode": "before", "count": 2}
    ).get_json()
    # Older-than-5 uids are 1..4; last 2 -> 3,4.
    assert body["mode"] == "before"
    assert body["matched"] == 4
    assert body["capped"] is False
    assert body["fetched"] == 2
    with Store(db) as store:
        lst = store.get_list_by_name("announce")
        ps = store.get_pull_state(lst.id)
        assert (ps.uidvalidity, ps.last_uid) == (1000, 6)  # unchanged


def test_pull_range_new_all_caps_at_max(tmp_path, monkeypatch):
    # 1001 new uids with count omitted ("all") must be capped to _MAX_PULL_COUNT.
    db = _range_db(tmp_path, stored_uids=[])
    fd = FakeFolder(uidvalidity=1000, uidnext=1003)
    fd.exists = 1001
    for uid in range(1, 1002):
        fd.messages[uid] = b""  # search only needs the keys; run_fetch_uids is faked
    conn = FakeImapConn(folders={"Shared Folders/announce": fd})

    calls: dict = {}
    _fake_pipeline(monkeypatch, calls)

    def fake_fetch_uids(client, store, folder, uids, *, batch_size=200):
        calls["n_uids"] = len(uids)
        return FetchSummary(fetched=len(uids))

    monkeypatch.setattr(webapp_api, "run_fetch_uids", fake_fetch_uids)
    c = _client_over(db, conn, monkeypatch)

    body = c.post("/api/pull/range", json={"list": "announce", "mode": "new"}).get_json()
    assert body["matched"] == 1001
    assert body["capped"] is True
    assert calls["n_uids"] == 1000  # trimmed to the cap
    assert body["fetched"] == 1000


def test_pull_range_scores_when_api_key_present(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5], cursor=(1000, 5))
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 9))})
    calls: dict = {}
    _fake_pipeline(monkeypatch, calls)
    c = _client_over(db, conn, monkeypatch, pangram_key="test-key")

    body = c.post(
        "/api/pull/range", json={"list": "announce", "mode": "new", "count": 3}
    ).get_json()
    assert body["scoring_skipped"] is False
    assert body["scored"] == 2
    assert body["cache_hits"] == 1
    assert body["api_calls"] == 2
    assert calls["score_limit"] == 3  # limit = number of uids fetched


@pytest.mark.parametrize(
    "payload",
    [
        {"list": "announce", "mode": "before"},  # before requires count
        {"list": "announce", "mode": "before", "count": 0},  # below min
        {"list": "announce", "mode": "before", "count": 1001},  # above max
        {"list": "announce", "mode": "before", "count": "x"},  # non-int
        {"list": "announce", "mode": "before", "count": True},  # bool not an int
        {"list": "announce", "mode": "new", "count": 0},  # below min
        {"list": "announce", "mode": "new", "count": 1001},  # above max
        {"list": "announce", "mode": "sideways", "count": 5},  # bad mode
    ],
)
def test_pull_range_validation_400(tmp_path, monkeypatch, payload):
    db = _range_db(tmp_path, stored_uids=[5])
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 6))})
    c = _client_over(db, conn, monkeypatch)
    resp = c.post("/api/pull/range", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_pull_range_unknown_list_404(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5])
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 6))})
    c = _client_over(db, conn, monkeypatch)
    resp = c.post("/api/pull/range", json={"list": "nope", "mode": "new"})
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_pull_range_imap_connect_failure_502(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5])

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(webapp_api, "open_client", _boom)
    config = replace(_config(db), pangram_api_key="")
    app = create_app(config, frontend_dist=None)
    app.testing = True
    resp = app.test_client().post("/api/pull/range", json={"list": "announce", "mode": "new"})
    assert resp.status_code == 502
    assert "error" in resp.get_json()
