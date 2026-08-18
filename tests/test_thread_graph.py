"""Tests for the list panel's thread graph.

Covers :meth:`Store.thread_graph` — a rank window of a list's messages in IMAP
receipt order, grouped into reply threads — and the
``GET /api/lists/thread-graph`` endpoint that serves it. The fixture builds its
own small thread structure, as :mod:`tests.test_reply_rugs` does.
"""

from __future__ import annotations

import pytest

from mailing_list_ai_check.config import Config
from mailing_list_ai_check.store import Store, sha256_text
from mailing_list_ai_check.webapp import create_app

# Dates ascend with the message's day number.
_DAY = "2026-05-{:02d}T10:00:00+00:00"


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "threads.db"


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
    uid: int | None = None,
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
        uid=uid,
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


def _thread_mids(graph: dict) -> list[list[str]]:
    return [[m["message_id"] for m in t["messages"]] for t in graph["threads"]]


def _flat(graph: dict) -> dict[str, dict]:
    return {m["message_id"]: m for t in graph["threads"] for m in t["messages"]}


@pytest.fixture()
def fixture(store):
    """One list with two threads and a singleton, plus a decoy list.

    List ``alpha``, in receipt (uid) order: ``root1`` (1), ``reply1`` → root1
    (2), ``solo`` (3), ``root2`` (4), ``reply2`` → root2 (5), ``reply3`` →
    reply2 (6). ``reply2`` carries a messy ``In-Reply-To`` value the linkage
    must normalise. List ``beta`` holds one unrelated message.
    """
    alpha = _list(store, "alpha")
    beta = _list(store, "beta")
    alice = store.upsert_address("alice@example.org", "Alice").id
    bob = store.upsert_address("bob@example.org", "Bob").id

    ids: dict[str, int] = {}
    ids["root1"] = _message(store, alpha, "root1", 1, uid=1, address_id=alice, label="Human")
    ids["reply1"] = _message(
        store, alpha, "reply1", 2, uid=2, address_id=bob, in_reply_to="<root1@test>", label="AI"
    )
    ids["solo"] = _message(store, alpha, "solo", 3, uid=3, address_id=alice)
    ids["root2"] = _message(store, alpha, "root2", 4, uid=4, address_id=bob, label="Mixed")
    ids["reply2"] = _message(
        store,
        alpha,
        "reply2",
        5,
        uid=5,
        address_id=alice,
        in_reply_to=" <root2@test> (comment)",
        label="Mixed",
    )
    ids["reply3"] = _message(
        store, alpha, "reply3", 6, uid=6, address_id=bob, in_reply_to="<reply2@test>"
    )
    _message(store, beta, "beta-msg", 7, uid=1, address_id=bob)
    return {"store": store, "alpha": alpha, "beta": beta, "ids": ids}


# --- Store.thread_graph ---------------------------------------------------------


def test_groups_reply_components_into_threads(fixture):
    graph = fixture["store"].thread_graph(fixture["alpha"])
    assert graph["total"] == 6
    # Threads ordered by their oldest message; messages oldest first within one.
    assert _thread_mids(graph) == [
        ["<root1@test>", "<reply1@test>"],
        ["<solo@test>"],
        ["<root2@test>", "<reply2@test>", "<reply3@test>"],
    ]


def test_default_window_spans_the_whole_list(fixture):
    """With no bounds given, the window is the whole list, rank 0 to the last."""
    graph = fixture["store"].thread_graph(fixture["alpha"])
    assert (graph["list_total"], graph["start"], graph["end"], graph["total"]) == (6, 0, 5, 6)


def test_first_and_last_date_bound_the_window(fixture):
    graph = fixture["store"].thread_graph(fixture["alpha"])
    assert (graph["first_date"], graph["last_date"]) == (_DAY.format(1), _DAY.format(6))
    inner = fixture["store"].thread_graph(fixture["alpha"], start=2, end=4)
    assert (inner["first_date"], inner["last_date"]) == (_DAY.format(3), _DAY.format(5))


def test_seq_is_the_receipt_rank_and_parents_resolve(fixture):
    flat = _flat(fixture["store"].thread_graph(fixture["alpha"]))
    assert [flat[f"<{k}@test>"]["seq"] for k in ("root1", "reply1", "solo", "root2")] == [
        0,
        1,
        2,
        3,
    ]
    ids = fixture["ids"]
    assert flat["<reply1@test>"]["parent_id"] == ids["root1"]
    assert flat["<reply2@test>"]["parent_id"] == ids["root2"]  # messy header normalised
    assert flat["<reply3@test>"]["parent_id"] == ids["reply2"]
    assert flat["<root1@test>"]["parent_id"] is None


def test_receipt_order_is_uid_not_date(store):
    """A message dated later but received earlier keeps its receipt position."""
    list_id = _list(store, "alpha")
    _message(store, list_id, "late-date", 9, uid=1)
    _message(store, list_id, "early-date", 1, uid=2)
    flat = _flat(store.thread_graph(list_id))
    assert flat["<late-date@test>"]["seq"] == 0
    assert flat["<early-date@test>"]["seq"] == 1


def test_messages_without_a_uid_sort_oldest(store):
    list_id = _list(store, "alpha")
    _message(store, list_id, "no-uid", 9, uid=None)
    _message(store, list_id, "with-uid", 1, uid=1)
    flat = _flat(store.thread_graph(list_id))
    assert flat["<no-uid@test>"]["seq"] == 0
    assert flat["<with-uid@test>"]["seq"] == 1


def test_window_start_clips_parents(fixture):
    """A reply whose parent falls before the window becomes a thread root."""
    graph = fixture["store"].thread_graph(fixture["alpha"], start=4, end=5)
    assert (graph["start"], graph["end"], graph["total"]) == (4, 5, 2)
    # reply2 and reply3 are the window; reply2's parent (root2) is outside it,
    # so the pair still forms one thread rooted at reply2.
    assert _thread_mids(graph) == [["<reply2@test>", "<reply3@test>"]]
    flat = _flat(graph)
    assert flat["<reply2@test>"]["parent_id"] is None
    assert flat["<reply2@test>"]["seq"] == 0
    assert flat["<reply3@test>"]["parent_id"] == fixture["ids"]["reply2"]


def test_interior_window_rebases_seq_and_drops_outside_parents(fixture):
    """An interior window keeps only its own ranks, numbered from 0."""
    graph = fixture["store"].thread_graph(fixture["alpha"], start=2, end=4)
    assert (graph["list_total"], graph["start"], graph["end"], graph["total"]) == (6, 2, 4, 3)
    assert _thread_mids(graph) == [["<solo@test>"], ["<root2@test>", "<reply2@test>"]]
    flat = _flat(graph)
    assert [flat[f"<{k}@test>"]["seq"] for k in ("solo", "root2", "reply2")] == [0, 1, 2]
    assert flat["<reply2@test>"]["parent_id"] == fixture["ids"]["root2"]
    # reply3, rank 5, is past the window's end.
    assert "<reply3@test>" not in flat


def test_end_beyond_the_list_is_clamped(fixture):
    graph = fixture["store"].thread_graph(fixture["alpha"], start=3, end=999)
    assert (graph["start"], graph["end"], graph["total"]) == (3, 5, 3)


def test_start_beyond_the_end_collapses_to_one_message(fixture):
    """A start past the clamped end is pulled back to it."""
    graph = fixture["store"].thread_graph(fixture["alpha"], start=99, end=999)
    assert (graph["start"], graph["end"], graph["total"]) == (5, 5, 1)


#: Messages on the long list the span tests build — more than any window the
#: method used to allow, so a cap on the span would show up as a short result.
_LONG_LIST_TOTAL = 503


def _long_list(store: Store) -> int:
    """A list of :data:`_LONG_LIST_TOTAL` unthreaded messages, UIDs 1..n."""
    list_id = _list(store, "alpha")
    for uid in range(1, _LONG_LIST_TOTAL + 1):
        _message(store, list_id, f"m{uid:04d}", 1, uid=uid)
    return list_id


def test_a_wide_span_is_honoured_in_full(store):
    """An explicit range is served entire, however wide, keeping both bounds."""
    list_id = _long_list(store)
    total = _LONG_LIST_TOTAL
    graph = store.thread_graph(list_id, start=0, end=total - 1)
    assert graph["list_total"] == total
    assert (graph["start"], graph["end"], graph["total"]) == (0, total - 1, total)
    flat = _flat(graph)
    assert flat["<m0001@test>"]["seq"] == 0
    assert flat[f"<m{total:04d}@test>"]["seq"] == total - 1


def test_the_default_window_covers_a_long_list(store):
    """With no bounds the whole list comes back, however many messages it holds."""
    list_id = _long_list(store)
    total = _LONG_LIST_TOTAL
    graph = store.thread_graph(list_id)
    assert (graph["start"], graph["end"], graph["total"]) == (0, total - 1, total)
    assert _flat(graph)["<m0001@test>"]["seq"] == 0


def test_a_reply_received_before_its_parent_joins_the_thread(store):
    list_id = _list(store, "alpha")
    child = _message(store, list_id, "child", 1, uid=1, in_reply_to="<parent@test>")
    parent = _message(store, list_id, "parent", 2, uid=2)
    graph = store.thread_graph(list_id)
    assert _thread_mids(graph) == [["<child@test>", "<parent@test>"]]
    flat = _flat(graph)
    assert flat["<child@test>"]["parent_id"] == parent
    assert flat["<parent@test>"]["parent_id"] is None
    assert child != parent


def test_self_referencing_message_is_not_a_reply(store):
    list_id = _list(store, "alpha")
    _message(store, list_id, "loop", 1, uid=1, in_reply_to="<loop@test>")
    flat = _flat(store.thread_graph(list_id))
    assert flat["<loop@test>"]["parent_id"] is None


def test_unstored_parent_leaves_a_singleton(store):
    list_id = _list(store, "alpha")
    _message(store, list_id, "orphan", 1, uid=1, in_reply_to="<nowhere@test>")
    graph = store.thread_graph(list_id)
    assert _thread_mids(graph) == [["<orphan@test>"]]
    assert _flat(graph)["<orphan@test>"]["parent_id"] is None


def test_other_lists_do_not_leak(fixture):
    """The window covers one list; a parent stored only elsewhere stays unlinked."""
    store = fixture["store"]
    graph = store.thread_graph(fixture["alpha"])
    assert "<beta-msg@test>" not in _flat(graph)
    cross = _message(
        store, fixture["alpha"], "cross-reply", 8, uid=7, in_reply_to="<beta-msg@test>"
    )
    graph = store.thread_graph(fixture["alpha"])
    assert graph["list_total"] == 7  # beta's message is not counted
    flat = _flat(graph)
    assert flat["<cross-reply@test>"]["parent_id"] is None
    assert flat["<cross-reply@test>"]["id"] == cross


def test_messages_carry_sender_and_prediction_fields(fixture):
    flat = _flat(fixture["store"].thread_graph(fixture["alpha"]))
    root1 = flat["<root1@test>"]
    assert set(root1) == {
        "id",
        "message_id",
        "seq",
        "uid",
        "date",
        "subject",
        "from_name",
        "from_email",
        "extraction_status",
        "label",
        "prediction_short",
        "timing_cpm",
        "parent_id",
    }
    # No per-message from_name on the fixture rows, so the address name is served.
    assert (root1["from_name"], root1["from_email"]) == ("Alice", "alice@example.org")
    # label and prediction_short are the same stored value, served verbatim.
    assert (flat["<root2@test>"]["label"], flat["<root2@test>"]["prediction_short"]) == (
        "Mixed",
        "Mixed",
    )
    # An unscored message still appears, with no label or bucket.
    assert flat["<solo@test>"]["label"] is None
    assert flat["<solo@test>"]["prediction_short"] is None


def test_thread_graph_prefers_the_messages_own_from_name(fixture):
    store = fixture["store"]
    store.conn.execute(
        "UPDATE messages SET from_name = ? WHERE id = ?",
        ("Someone Else", fixture["ids"]["root1"]),
    )
    store.conn.commit()
    root1 = _flat(store.thread_graph(fixture["alpha"]))["<root1@test>"]
    assert (root1["from_name"], root1["from_email"]) == ("Someone Else", "alice@example.org")


def test_messages_carry_the_stored_writing_rate(store):
    list_id = _list(store, "alpha")
    fast = _message(store, list_id, "fast", 1, uid=1)
    _message(store, list_id, "unrated", 2, uid=2)
    store.conn.execute("UPDATE messages SET timing_cpm = ? WHERE id = ?", (1234.5, fast))
    flat = _flat(store.thread_graph(list_id))
    assert flat["<fast@test>"]["timing_cpm"] == pytest.approx(1234.5)
    assert flat["<unrated@test>"]["timing_cpm"] is None


def test_messages_carry_the_extraction_status(store):
    """A message gated under the reliability floor is not a merely unscored one."""
    list_id = _list(store, "alpha")
    gated = _message(store, list_id, "gated", 1, uid=1)
    store.insert_extraction(
        message_id=gated, extracted_text="tiny", method="test", status="too_short"
    )
    flat = _flat(store.thread_graph(list_id))
    assert flat["<gated@test>"]["extraction_status"] == "too_short"
    assert flat["<gated@test>"]["label"] is None


def test_empty_list_yields_an_empty_graph(store):
    assert store.thread_graph(_list(store, "alpha")) == {
        "list_total": 0,
        "start": None,
        "end": None,
        "total": 0,
        "first_date": None,
        "last_date": None,
        "threads": [],
    }


# --- GET /api/lists/thread-graph ------------------------------------------------


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


def test_endpoint_returns_the_graph(client):
    resp = client.get("/api/lists/thread-graph?list=alpha")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["list"] == "alpha"
    assert (body["list_total"], body["start"], body["end"], body["total"]) == (6, 0, 5, 6)
    assert (body["first_date"], body["last_date"]) == (_DAY.format(1), _DAY.format(6))
    assert _thread_mids(body) == [
        ["<root1@test>", "<reply1@test>"],
        ["<solo@test>"],
        ["<root2@test>", "<reply2@test>", "<reply3@test>"],
    ]


def test_endpoint_honours_an_explicit_window(client):
    body = client.get("/api/lists/thread-graph?list=alpha&start=2&end=4").get_json()
    assert (body["start"], body["end"], body["total"]) == (2, 4, 3)
    assert _thread_mids(body) == [["<solo@test>"], ["<root2@test>", "<reply2@test>"]]


def test_endpoint_clamps_end_to_the_last_rank(client):
    body = client.get("/api/lists/thread-graph?list=alpha&start=1&end=999").get_json()
    assert (body["start"], body["end"], body["total"]) == (1, 5, 5)


def test_endpoint_start_only_runs_to_the_end_of_the_list(client):
    body = client.get("/api/lists/thread-graph?list=alpha&start=4").get_json()
    assert (body["start"], body["end"], body["total"]) == (4, 5, 2)


def test_endpoint_end_only_runs_from_the_start_of_the_list(client):
    """An omitted ``start`` is rank 0, not a fixed-width window before ``end``."""
    body = client.get("/api/lists/thread-graph?list=alpha&end=2").get_json()
    assert (body["start"], body["end"], body["total"]) == (0, 2, 3)


def test_endpoint_empty_list_reports_a_null_window(store, client):
    """A known list with no stored messages returns nulls, not a zero-rank window."""
    _list(store, "empty")
    body = client.get("/api/lists/thread-graph?list=empty&start=0&end=10").get_json()
    assert body == {
        "list": "empty",
        "list_total": 0,
        "start": None,
        "end": None,
        "total": 0,
        "first_date": None,
        "last_date": None,
        "threads": [],
    }


def test_endpoint_unknown_list_is_404(client):
    resp = client.get("/api/lists/thread-graph?list=nowhere")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


@pytest.mark.parametrize(
    "query",
    [
        "",
        "list=alpha&start=4&end=2",
        "list=alpha&start=-1",
        "list=alpha&end=-1",
        "list=alpha&start=x",
        "list=alpha&end=x",
    ],
)
def test_endpoint_rejects_bad_input(client, query):
    resp = client.get(f"/api/lists/thread-graph?{query}")
    assert resp.status_code == 400
    assert "error" in resp.get_json()
