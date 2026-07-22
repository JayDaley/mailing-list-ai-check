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
    PangramClient,
    PangramError,
    PangramResult,
    PangramTaskFailed,
    PangramTimeout,
    PangramTransportError,
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

    def request(self, method, url, headers=None, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs))
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
    "version": "3.3.2",
    "headline": "AI Generated",
    "windows": [{"label": "AI-Generated", "word_count": 60}],
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
    assert result.version == "3.3.2"
    # full raw JSON is preserved, including windows.
    assert result.raw["windows"][0]["word_count"] == 60


# --- label derivation -----------------------------------------------------


def _result(short, ai, assisted, human):
    return PangramResult.from_response(
        {
            "prediction_short": short,
            "fraction_ai": ai,
            "fraction_ai_assisted": assisted,
            "fraction_human": human,
        }
    )


def test_label_rebadges_assisted_dominated_mixed():
    # Pangram calls fully AI-assisted text "Mixed"; the label must not.
    assert _result("Mixed", 0.0, 1.0, 0.0).label == "AI-Assisted"
    assert _result("Mixed", 0.0, 0.66, 0.34).label == "AI-Assisted"


def test_label_keeps_genuine_mixed():
    assert _result("Mixed", 0.63, 0.0, 0.37).label == "Mixed"
    assert _result("Mixed", 0.28, 0.34, 0.38).label == "Mixed"


def test_label_passes_through_other_predictions():
    assert _result("AI", 1.0, 0.0, 0.0).label == "AI"
    assert _result("Human", 0.0, 0.0, 1.0).label == "Human"
    assert _result(None, None, None, None).label is None
    # Missing fractions never rebadge.
    assert _result("Mixed", None, None, None).label == "Mixed"


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


def test_5xx_then_success(_no_sleep):
    client = _client(
        [
            FakeResponse(503),
            FakeResponse(200, {"task_id": "t6"}),
            FakeResponse(200, SUCCESS_BODY),
        ]
    )
    assert client.predict("text").fraction_ai == 1.0


def test_connection_error_then_success():
    client = _client(
        [
            requests.ConnectionError("boom"),
            FakeResponse(200, {"task_id": "t7"}),
            FakeResponse(200, SUCCESS_BODY),
        ]
    )
    assert client.predict("text").prediction_short == "AI"


def test_5xx_exhausts_retries():
    client = _client([FakeResponse(500)] * 3, max_retries=2)
    with pytest.raises(PangramTransportError) as exc:
        client.predict("text")
    assert exc.value.status_code == 500


def test_connection_error_exhausts_retries():
    client = _client([requests.ConnectionError("x")] * 3, max_retries=2)
    with pytest.raises(PangramTransportError):
        client.predict("text")


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
