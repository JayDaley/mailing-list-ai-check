"""Unit tests for the Pangram client — mocked transport only, never the network.

A :class:`FakeSession` is injected in place of ``requests.Session`` and returns a
scripted sequence of responses (or raises a scripted exception), so submit/poll,
retry/backoff and every error path are exercised without a key or a real call.
``time.sleep`` is neutralised in every test so retries/polls don't actually wait.
"""

from __future__ import annotations

import os

import pytest
import requests

from mailing_list_ai_check import pangram
from mailing_list_ai_check.pangram import (
    DEFAULT_MODEL,
    MODEL_GENERATIONS,
    PangramBulkFailed,
    PangramClient,
    PangramError,
    PangramResult,
    PangramTaskFailed,
    PangramTimeout,
    PangramTransportError,
    generation_for_model,
)


class FakeResponse:
    """Minimal stand-in for a :class:`requests.Response`."""

    def __init__(self, status_code, payload=None, *, headers=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._bad_json = bad_json
        self.text = ""

    def json(self):
        if self._bad_json:
            raise ValueError("not JSON")
        return self._payload


class FakeSession:
    """Returns scripted responses in order; a scripted Exception is raised."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.timeouts = []

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs))
        self.timeouts.append(timeout)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


SUCCESS_BODY = {
    "stage": "STAGE_SUCCESS",
    "fraction_ai": 1.0,
    "fraction_ai_assisted": 0.0,
    "fraction_human": 0.0,
    "prediction_short": "AI",
    "version": "4.0",
    "headline": "AI Generated",
    "windows": [
        {
            "label": "AI-Generated",
            "word_count": 60,
            "is_humanized": False,
            "humanizer_score": 0.02,
        }
    ],
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Record sleep durations but never actually wait."""
    slept = []
    monkeypatch.setattr(pangram.time, "sleep", lambda s: slept.append(s))
    return slept


def _client(script, **kwargs):
    return PangramClient(
        "test-key",
        session=FakeSession(script),
        min_submit_interval=0,
        initial_backoff=0.5,
        **kwargs,
    )


# --- happy path ---------------------------------------------------------------


def test_submit_and_poll_success():
    client = _client(
        [
            FakeResponse(200, {"task_id": "t1"}),
            FakeResponse(200, SUCCESS_BODY),
        ]
    )
    result = client.predict("some sufficiently long text " * 20)
    assert result.fraction_ai == 1.0
    assert result.fraction_ai_assisted == 0.0
    assert result.fraction_human == 0.0
    assert result.prediction_short == "AI"
    assert result.version == "4.0"
    # full raw JSON is preserved, including windows and the v4 humanizer fields.
    assert result.raw["windows"][0]["word_count"] == 60
    assert result.raw["windows"][0]["is_humanized"] is False
    assert result.raw["windows"][0]["humanizer_score"] == 0.02


def test_submit_pins_v4_model_by_default():
    # The server still resolves an omitted model to Pangram 3, so every submit
    # must name the generation explicitly.
    session = FakeSession([FakeResponse(200, {"task_id": "t"}), FakeResponse(200, SUCCESS_BODY)])
    client = PangramClient("k", session=session, min_submit_interval=0)
    client.predict("text")
    _method, _url, kwargs = session.calls[0]
    assert kwargs["json"] == {
        "text": "text",
        "model": "pangram-4",
        "public_dashboard_link": False,
    }


def test_model_override_is_sent():
    session = FakeSession([FakeResponse(200, {"task_id": "t"}), FakeResponse(200, SUCCESS_BODY)])
    client = PangramClient("k", session=session, min_submit_interval=0, model="default")
    client.predict("text")
    assert session.calls[0][2]["json"]["model"] == "default"


# --- detector generations -------------------------------------------------


def test_generation_for_model_maps_each_selector():
    # The generation is the leading component of the version a response stamps:
    # Pangram 4 returns "4.0", the default selector (Pangram 3) returned "3.3.2".
    assert generation_for_model("pangram-4") == "4"
    assert generation_for_model("default") == "3"
    assert generation_for_model(DEFAULT_MODEL) == "4"
    assert SUCCESS_BODY["version"].startswith(generation_for_model("pangram-4") + ".")


def test_generation_for_model_unknown_selector_is_none():
    assert generation_for_model("pangram-5") is None
    assert generation_for_model("") is None


def test_model_generations_covers_every_accepted_selector():
    assert set(MODEL_GENERATIONS) == {"pangram-4", "default"}


# --- result fields ----------------------------------------------------------


def test_result_fields_are_stored_verbatim():
    # No label is derived: prediction_short is surfaced exactly as returned,
    # whatever the fractions say (assisted-dominance lives in the fractions).
    result = PangramResult.from_response(
        {
            "prediction_short": "Mixed",
            "fraction_ai": 0.0,
            "fraction_ai_assisted": 1.0,
            "fraction_human": 0.0,
        }
    )
    assert result.prediction_short == "Mixed"
    assert result.fraction_ai_assisted == 1.0
    assert not hasattr(result, "label")


def test_submit_accepts_202():
    client = _client(
        [
            FakeResponse(202, {"task_id": "t2"}),
            FakeResponse(200, SUCCESS_BODY),
        ]
    )
    assert client.predict("text").prediction_short == "AI"


def test_polls_through_non_terminal_stage():
    client = _client(
        [
            FakeResponse(200, {"task_id": "t3"}),
            FakeResponse(200, {"stage": "STAGE_RUNNING"}),
            FakeResponse(200, SUCCESS_BODY),
        ]
    )
    assert client.predict("text").fraction_ai == 1.0


# --- retries / backoff --------------------------------------------------------


def test_429_then_success(_no_sleep):
    client = _client(
        [
            FakeResponse(429),
            FakeResponse(200, {"task_id": "t4"}),
            FakeResponse(200, SUCCESS_BODY),
        ]
    )
    assert client.predict("text").prediction_short == "AI"
    assert _no_sleep  # a backoff sleep happened


def test_retry_after_header_respected(_no_sleep):
    client = _client(
        [
            FakeResponse(429, headers={"Retry-After": "7"}),
            FakeResponse(200, {"task_id": "t5"}),
            FakeResponse(200, SUCCESS_BODY),
        ]
    )
    client.predict("text")
    assert 7.0 in _no_sleep


def test_poll_5xx_then_success(_no_sleep):
    # The status poll is idempotent, so a transient 5xx there is retried.
    client = _client(
        [
            FakeResponse(200, {"task_id": "t6"}),
            FakeResponse(503),
            FakeResponse(200, SUCCESS_BODY),
        ]
    )
    assert client.predict("text").fraction_ai == 1.0


def test_poll_connection_error_then_success():
    client = _client(
        [
            FakeResponse(200, {"task_id": "t7"}),
            requests.ConnectionError("boom"),
            FakeResponse(200, SUCCESS_BODY),
        ]
    )
    assert client.predict("text").prediction_short == "AI"


def test_poll_5xx_exhausts_retries():
    client = _client([FakeResponse(200, {"task_id": "t"})] + [FakeResponse(500)] * 3, max_retries=2)
    with pytest.raises(PangramTransportError) as exc:
        client.predict("text")
    assert exc.value.status_code == 500


def test_poll_connection_error_exhausts_retries():
    client = _client(
        [FakeResponse(200, {"task_id": "t"})] + [requests.ConnectionError("x")] * 3,
        max_retries=2,
    )
    with pytest.raises(PangramTransportError):
        client.predict("text")


# --- submit safety --------------------------------------------------------
# The /task and /bulk submits create billed work, so they are never re-sent
# when the server may already have received them: only a 429 response or a
# connect-phase timeout is retried. See the 2026-08-12 incident in the module
# docstring (a timed-out 1,000-item bulk submit, retried 5 times, likely
# created one billed job per attempt).


def test_task_submit_read_timeout_not_retried():
    session = FakeSession([requests.ReadTimeout("slow accept")])
    client = PangramClient("k", session=session, min_submit_interval=0)
    with pytest.raises(PangramTransportError) as exc:
        client.predict("text")
    assert len(session.calls) == 1
    assert "not retried" in str(exc.value)


def test_task_submit_5xx_not_retried():
    session = FakeSession([FakeResponse(502)])
    client = PangramClient("k", session=session, min_submit_interval=0)
    with pytest.raises(PangramTransportError) as exc:
        client.predict("text")
    assert exc.value.status_code == 502
    assert len(session.calls) == 1


def test_task_submit_connect_timeout_is_retried(_no_sleep):
    # ConnectTimeout means the request never reached the server — safe to retry.
    client = _client(
        [
            requests.ConnectTimeout("no route"),
            FakeResponse(200, {"task_id": "t"}),
            FakeResponse(200, SUCCESS_BODY),
        ]
    )
    assert client.predict("text").prediction_short == "AI"


def test_bulk_submit_read_timeout_not_retried():
    session = FakeSession([requests.ReadTimeout("slow accept")])
    client = PangramClient("k", session=session, min_submit_interval=0)
    with pytest.raises(PangramTransportError) as exc:
        client.predict_bulk({"e1": "text"})
    assert len(session.calls) == 1
    assert "not retried" in str(exc.value)


def test_bulk_submit_connection_error_not_retried():
    session = FakeSession([requests.ConnectionError("reset")])
    client = PangramClient("k", session=session, min_submit_interval=0)
    with pytest.raises(PangramTransportError):
        client.predict_bulk({"e1": "text"})
    assert len(session.calls) == 1


def test_bulk_submit_5xx_not_retried():
    session = FakeSession([FakeResponse(500)])
    client = PangramClient("k", session=session, min_submit_interval=0)
    with pytest.raises(PangramTransportError) as exc:
        client.predict_bulk({"e1": "text"})
    assert exc.value.status_code == 500
    assert len(session.calls) == 1


def test_bulk_submit_429_is_retried(_no_sleep):
    client = _client(
        [
            FakeResponse(429),
            FakeResponse(202, {"bulk_id": "b"}),
            FakeResponse(200, {"status": "succeeded"}),
            FakeResponse(200, {"items": [{"id": "e1", "result": SUCCESS_BODY}]}),
        ]
    )
    outcome = client.predict_bulk({"e1": "text"})
    assert outcome.results["e1"].prediction_short == "AI"


def test_bulk_submit_uses_its_own_timeout():
    # The submit uses bulk_submit_timeout; polls and results pages keep the
    # ordinary per-request http_timeout.
    session = FakeSession(
        [
            FakeResponse(202, {"bulk_id": "b"}),
            FakeResponse(200, {"status": "succeeded"}),
            FakeResponse(200, {"items": [{"id": "e1", "result": SUCCESS_BODY}]}),
        ]
    )
    client = PangramClient(
        "k", session=session, min_submit_interval=0, http_timeout=10.0, bulk_submit_timeout=120.0
    )
    client.predict_bulk({"e1": "text"})
    assert session.timeouts == [120.0, 10.0, 10.0]


# --- non-retryable / failure paths --------------------------------------------


def test_non_retryable_4xx_raises_immediately():
    session = FakeSession([FakeResponse(401)])
    client = PangramClient("k", session=session, min_submit_interval=0)
    with pytest.raises(PangramTransportError) as exc:
        client.predict("text")
    assert exc.value.status_code == 401
    assert len(session.calls) == 1  # no retry on 401


def test_stage_failed_raises():
    client = _client(
        [
            FakeResponse(200, {"task_id": "t8"}),
            FakeResponse(200, {"stage": "STAGE_FAILED", "detail": "bad input"}),
        ]
    )
    with pytest.raises(PangramTaskFailed):
        client.predict("text")


def test_timeout_raises():
    # overall_timeout=0 means the deadline is already past once submit returns.
    client = _client([FakeResponse(200, {"task_id": "t9"})], overall_timeout=0)
    with pytest.raises(PangramTimeout):
        client.predict("text")


def test_missing_task_id_raises():
    client = _client([FakeResponse(200, {})])
    with pytest.raises(PangramError):
        client.predict("text")


def test_malformed_json_raises():
    client = _client([FakeResponse(200, bad_json=True)])
    with pytest.raises(PangramError):
        client.predict("text")


def test_non_object_json_raises():
    client = _client([FakeResponse(200, ["not", "an", "object"])])
    with pytest.raises(PangramError):
        client.predict("text")


def test_empty_api_key_rejected():
    with pytest.raises(ValueError):
        PangramClient("")


def test_request_sends_api_key_header():
    session = FakeSession([FakeResponse(200, {"task_id": "t"}), FakeResponse(200, SUCCESS_BODY)])
    client = PangramClient("secret-key", session=session, min_submit_interval=0)
    client.predict("text")
    # The key travels in the x-api-key header, added by the client per call.
    assert client._headers()["x-api-key"] == "secret-key"


# --- bulk API -------------------------------------------------------------


def test_bulk_submit_poll_and_results():
    # Response shapes mirror the live service (verified 2026-08-12): submit is
    # 202 with accepted_items as a list, polls report queued/running/succeeded,
    # and the results page lists entries under "items" with per-entry
    # index/task_id/stage/error alongside the single-text-schema "result".
    session = FakeSession(
        [
            FakeResponse(
                202,
                {
                    "bulk_id": "b1",
                    "status": "queued",
                    "total_items": 2,
                    "accepted_items": [
                        {"index": 0, "id": "e1", "task_id": "t1"},
                        {"index": 1, "id": "e2", "task_id": "t2"},
                    ],
                    "failed_items": [],
                },
            ),
            FakeResponse(200, {"status": "running", "succeeded": 0, "failed": 0}),
            FakeResponse(200, {"status": "succeeded", "succeeded": 2, "failed": 0}),
            FakeResponse(
                200,
                {
                    "bulk_id": "b1",
                    "offset": 0,
                    "limit": 1000,
                    "total_items": 2,
                    "items": [
                        {
                            "index": 0,
                            "id": "e1",
                            "stage": "STAGE_SUCCESS",
                            "error": None,
                            "result": SUCCESS_BODY,
                        },
                        {
                            "index": 1,
                            "id": "e2",
                            "stage": "STAGE_SUCCESS",
                            "error": None,
                            "result": SUCCESS_BODY,
                        },
                    ],
                    "failed_items": [],
                },
            ),
        ]
    )
    client = PangramClient("k", session=session, min_submit_interval=0)
    outcome = client.predict_bulk({"e1": "first text", "e2": "second text"})

    assert outcome.bulk_id == "b1"
    assert outcome.status == "succeeded"
    assert set(outcome.results) == {"e1", "e2"}
    assert outcome.results["e1"].prediction_short == "AI"
    assert outcome.errors == {}

    # Submit carries the items (id + text) and pins the model, like /task.
    method, url, kwargs = session.calls[0]
    assert (method, url) == ("POST", pangram.BULK_SUBMIT_URL)
    assert kwargs["json"] == {
        "items": [
            {"id": "e1", "text": "first text"},
            {"id": "e2", "text": "second text"},
        ],
        "model": "pangram-4",
    }
    # Status polls hit /bulk/{id}; the results page carries offset/limit.
    assert (
        session.calls[1][1] == session.calls[2][1] == pangram.BULK_STATUS_URL.format(bulk_id="b1")
    )
    method, url, kwargs = session.calls[3]
    assert url == pangram.BULK_RESULTS_URL.format(bulk_id="b1")
    assert kwargs["params"] == {"offset": 0, "limit": pangram.BULK_RESULTS_PAGE_LIMIT}


def test_bulk_pages_results(monkeypatch):
    # With a page limit of 1, each full page triggers another fetch at the next
    # offset until a short (empty) page ends the paging.
    monkeypatch.setattr(pangram, "BULK_RESULTS_PAGE_LIMIT", 1)
    session = FakeSession(
        [
            FakeResponse(202, {"bulk_id": "b2"}),
            FakeResponse(200, {"status": "succeeded"}),
            FakeResponse(200, {"items": [{"id": "e1", "result": SUCCESS_BODY}]}),
            FakeResponse(200, {"items": [{"id": "e2", "result": SUCCESS_BODY}]}),
            FakeResponse(200, {"items": []}),
        ]
    )
    client = PangramClient("k", session=session, min_submit_interval=0)
    outcome = client.predict_bulk({"e1": "one", "e2": "two"})
    assert set(outcome.results) == {"e1", "e2"}
    offsets = [call[2]["params"]["offset"] for call in session.calls[2:]]
    assert offsets == [0, 1, 2]


def test_bulk_accepts_results_envelope_key():
    # The live service lists results-page entries under "items"; "results" is
    # accepted defensively in case the envelope ever changes.
    client = _client(
        [
            FakeResponse(202, {"bulk_id": "b3"}),
            FakeResponse(200, {"status": "succeeded"}),
            FakeResponse(200, {"results": [{"id": "e1", "result": SUCCESS_BODY}]}),
        ]
    )
    outcome = client.predict_bulk({"e1": "text"})
    assert outcome.results["e1"].fraction_ai == 1.0


def test_bulk_partial_reports_item_errors():
    client = _client(
        [
            FakeResponse(202, {"bulk_id": "b4"}),
            FakeResponse(200, {"status": "partial"}),
            FakeResponse(
                200,
                {
                    "items": [
                        {"id": "e1", "result": SUCCESS_BODY},
                        {"id": "e2", "result": None, "error": "too short"},
                    ]
                },
            ),
        ]
    )
    outcome = client.predict_bulk({"e1": "one", "e2": "two"})
    assert outcome.status == "partial"
    assert set(outcome.results) == {"e1"}
    assert outcome.errors == {"e2": "too short"}


def test_bulk_item_stage_failed_is_an_error():
    client = _client(
        [
            FakeResponse(202, {"bulk_id": "b5"}),
            FakeResponse(200, {"status": "partial"}),
            FakeResponse(
                200,
                {"items": [{"id": "e1", "result": {"stage": "STAGE_FAILED", "detail": "boom"}}]},
            ),
        ]
    )
    outcome = client.predict_bulk({"e1": "text"})
    assert outcome.results == {}
    assert outcome.errors == {"e1": "boom"}


def test_bulk_item_missing_from_results_is_an_error():
    client = _client(
        [
            FakeResponse(202, {"bulk_id": "b6"}),
            FakeResponse(200, {"status": "succeeded"}),
            FakeResponse(200, {"items": [{"id": "e1", "result": SUCCESS_BODY}]}),
        ]
    )
    outcome = client.predict_bulk({"e1": "one", "e2": "two"})
    assert set(outcome.results) == {"e1"}
    assert outcome.errors == {"e2": "missing from bulk results"}


def test_bulk_failed_status_raises():
    client = _client(
        [
            FakeResponse(202, {"bulk_id": "b7"}),
            FakeResponse(200, {"status": "failed", "detail": "out of credits"}),
        ]
    )
    with pytest.raises(PangramBulkFailed) as exc:
        client.predict_bulk({"e1": "text"})
    assert "out of credits" in str(exc.value)


def test_bulk_timeout_raises():
    # bulk_overall_timeout=0 means the deadline is already past once submit
    # returns; the realtime overall_timeout does not apply to bulk jobs.
    client = _client(
        [FakeResponse(202, {"bulk_id": "b8"})],
        bulk_overall_timeout=0,
        overall_timeout=999,
    )
    with pytest.raises(PangramTimeout):
        client.predict_bulk({"e1": "text"})


def test_bulk_missing_bulk_id_raises():
    client = _client([FakeResponse(202, {"status": "queued"})])
    with pytest.raises(PangramError):
        client.predict_bulk({"e1": "text"})


def test_bulk_malformed_results_page_raises():
    client = _client(
        [
            FakeResponse(202, {"bulk_id": "b9"}),
            FakeResponse(200, {"status": "succeeded"}),
            FakeResponse(200, {"unexpected": True}),
        ]
    )
    with pytest.raises(PangramError):
        client.predict_bulk({"e1": "text"})


def test_bulk_empty_items_rejected():
    session = FakeSession([])
    client = PangramClient("k", session=session, min_submit_interval=0)
    with pytest.raises(ValueError):
        client.predict_bulk({})
    assert session.calls == []  # nothing was submitted


# --- opt-in live test ---------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("PANGRAM_LIVE_TEST") != "1",
    reason="live Pangram test disabled; set PANGRAM_LIVE_TEST=1 to enable (spends money)",
)
def test_live_predict():
    """Opt-in live smoke test. Set PANGRAM_LIVE_TEST=1 and PANGRAM_API_KEY.

    Sends at most 2 short-but-over-50-word texts to the real API. Excluded by
    default so CI never spends money or needs a key. Stays within the project's
    hard 10-call testing cap.
    """
    from mailing_list_ai_check.config import Config

    client = PangramClient(Config.load().pangram_api_key)
    texts = [
        (
            "I have been thinking about this proposal for a while now and I am "
            "still not convinced that adding a brand new header is the right "
            "call here. We already have mechanisms in the base specification "
            "that cover almost every case the draft describes, and the one "
            "remaining gap seems like something a single implementation got "
            "wrong rather than a genuine protocol shortcoming worth fixing."
        ),
        (
            "It is important to note that this approach offers several key "
            "benefits. First, it enhances interoperability across a wide range "
            "of implementations. Second, it ensures a seamless transition for "
            "existing deployments while leveraging established standards to "
            "foster a robust and scalable ecosystem for the entire working "
            "group and the broader community as a whole going forward."
        ),
    ]
    for text in texts:
        result = client.predict(text)
        assert result.prediction_short is not None
        assert 0.0 <= (result.fraction_ai or 0.0) <= 1.0
        # The submit pins model=pangram-4; the verdict must come from it.
        assert (result.version or "").startswith("4.")
        for window in result.raw.get("windows", []):
            assert "is_humanized" in window
            assert "humanizer_score" in window
