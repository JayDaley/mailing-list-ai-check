"""Pangram AI-detection client (raw ``requests``, not the SDK).

Implements the live-verified async contract from ``docs/findings/pangram.md``:
submit a single text with ``POST /task`` (returns HTTP 200 + ``{task_id}``),
then poll ``GET /task/{task_id}`` until ``stage`` is terminal
(``STAGE_SUCCESS`` / ``STAGE_FAILED``). Auth is the ``x-api-key`` header.

The client owns its HTTP layer deliberately (rather than using ``pangram-sdk``)
so it can add behaviour the SDK lacks and the findings doc requires: retry with
exponential backoff on ``429``/``5xx`` and connection errors, a conservative
client-side submit rate limit (the vendor ceiling is ~5 QPS), an overall task
deadline, and — importantly for a public repo — a guarantee that the API key is
only ever read from the caller (``Config.load().pangram_api_key``) and never
logged. A single :class:`requests.Session` is reused across calls.

The 50-word "too short to score" gate is **not** enforced here — that is the
scoring pipeline's job (the server itself does not enforce it).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger("mailing_list_ai_check.pangram")

# --- Contract constants (from pangram-sdk 0.3.1, live-verified 2026-07-21) ----

API_BASE = "https://text.external-api.pangram.com"
SUBMIT_URL = f"{API_BASE}/task"
TASK_URL = f"{API_BASE}/task/{{task_id}}"

SUCCESS_STAGE = "STAGE_SUCCESS"
FAILED_STAGE = "STAGE_FAILED"

#: HTTP status codes accepted from ``POST /task`` (live returns 200; the SDK/v3
#: migration notes mention 202 — accept both defensively).
_SUBMIT_OK = (200, 202)

# Timeouts / intervals mirror the SDK defaults documented in the findings.
DEFAULT_HTTP_TIMEOUT = 10.0  # per-request
DEFAULT_OVERALL_TIMEOUT = 300.0  # overall task deadline
DEFAULT_POLL_INTERVAL = 0.5  # between polls
DEFAULT_MAX_RETRIES = 5  # per HTTP request, on 429/5xx/connection errors
DEFAULT_INITIAL_BACKOFF = 0.5  # seconds; doubles each retry
#: Conservative submit spacing. The vendor ceiling is ~5 QPS (0.2s); we stay
#: comfortably under it to avoid ever tripping the limiter.
DEFAULT_MIN_SUBMIT_INTERVAL = 0.25


# --- Errors -------------------------------------------------------------------


class PangramError(Exception):
    """Base class for all Pangram client failures."""


class PangramTransportError(PangramError):
    """An HTTP request failed after exhausting retries (non-2xx or connection).

    ``status_code`` is the last HTTP status seen, or ``None`` for a connection
    error with no response.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PangramTaskFailed(PangramError):
    """The async task reached ``STAGE_FAILED``."""


class PangramTimeout(PangramError):
    """The overall task deadline elapsed before a terminal stage."""


# --- Result -------------------------------------------------------------------


@dataclass(frozen=True)
class PangramResult:
    """The fields Phase 4 surfaces from a successful classification.

    ``raw`` is the full parsed JSON response (including ``headline``,
    ``prediction``, ``num_*_segments`` and the ``windows`` array), stored
    verbatim so nothing is lost.
    """

    fraction_ai: float | None
    fraction_ai_assisted: float | None
    fraction_human: float | None
    prediction_short: str | None
    version: str | None
    raw: dict[str, Any]

    @classmethod
    def from_response(cls, data: dict[str, Any]) -> "PangramResult":
        return cls(
            fraction_ai=data.get("fraction_ai"),
            fraction_ai_assisted=data.get("fraction_ai_assisted"),
            fraction_human=data.get("fraction_human"),
            prediction_short=data.get("prediction_short"),
            version=data.get("version"),
            raw=data,
        )

    @property
    def label(self) -> str | None:
        """The categorical label to store, in the dashboard's four-band vocabulary.

        Pangram's ``prediction_short`` never emits ``"AI-Assisted"`` in practice:
        assisted-dominated text (even ``fraction_ai_assisted == 1.0``) comes back
        as ``"Mixed"``, with only the free-text ``headline`` saying "AI Assisted".
        Rebadge that case so the dashboard's AI-Assisted band matches the
        fractions; genuine AI/human mixes keep Pangram's ``"Mixed"``.
        """
        if (
            self.prediction_short == "Mixed"
            and self.fraction_ai_assisted is not None
            and self.fraction_ai_assisted > (self.fraction_ai or 0.0)
            and self.fraction_ai_assisted > (self.fraction_human or 0.0)
        ):
            return "AI-Assisted"
        return self.prediction_short


# --- Client -------------------------------------------------------------------


class PangramClient:
    """Async single-text Pangram client with retries and rate limiting.

    Parameters
    ----------
    api_key:
        Read from ``Config.load().pangram_api_key`` by the caller. Never logged.
    session:
        A :class:`requests.Session` to reuse; a fresh one is created if omitted.
    """

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
        overall_timeout: float = DEFAULT_OVERALL_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
        min_submit_interval: float = DEFAULT_MIN_SUBMIT_INTERVAL,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be a non-empty string")
        self._api_key = api_key
        self.session = session or requests.Session()
        self.http_timeout = http_timeout
        self.overall_timeout = overall_timeout
        self.poll_interval = poll_interval
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff
        self.min_submit_interval = min_submit_interval
        self._last_submit_at: float | None = None

    # -- public ---------------------------------------------------------------

    def predict(self, text: str) -> PangramResult:
        """Classify ``text``, blocking until the async task completes.

        Raises :class:`PangramTransportError`, :class:`PangramTaskFailed`,
        :class:`PangramTimeout`, or :class:`PangramError` (malformed response).
        """
        deadline = time.monotonic() + self.overall_timeout

        self._throttle_submit()
        resp = self._request(
            "POST",
            SUBMIT_URL,
            expected=_SUBMIT_OK,
            json={"text": text, "public_dashboard_link": False},
        )
        self._last_submit_at = time.monotonic()
        task_id = self._json(resp).get("task_id")
        if not task_id:
            raise PangramError("submit response contained no task_id")

        url = TASK_URL.format(task_id=task_id)
        while True:
            if time.monotonic() >= deadline:
                raise PangramTimeout(
                    f"task {task_id} did not complete within {self.overall_timeout:.0f}s"
                )
            resp = self._request("GET", url, expected=(200,))
            data = self._json(resp)
            stage = data.get("stage")
            if stage == SUCCESS_STAGE:
                return PangramResult.from_response(data)
            if stage == FAILED_STAGE:
                detail = data.get("detail") or data.get("headline") or "no detail"
                raise PangramTaskFailed(f"task {task_id} failed: {detail}")
            # Non-terminal stage (e.g. queued/running): keep polling.
            time.sleep(self.poll_interval)

    # -- internals ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "x-api-key": self._api_key}

    def _throttle_submit(self) -> None:
        """Enforce the minimum spacing between submits (client-side QPS cap)."""
        if self._last_submit_at is None or self.min_submit_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_submit_at
        wait = self.min_submit_interval - elapsed
        if wait > 0:
            time.sleep(wait)

    def _request(
        self, method: str, url: str, *, expected: tuple[int, ...], **kwargs: Any
    ) -> requests.Response:
        """Issue one HTTP request, retrying transient failures with backoff.

        Retries ``429`` and ``5xx`` responses and connection-level errors up to
        ``max_retries`` times, honouring a ``Retry-After`` header when present.
        Any other non-``expected`` status raises immediately (not retryable).
        """
        backoff = self.initial_backoff
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(
                    method, url, headers=self._headers(), timeout=self.http_timeout, **kwargs
                )
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise PangramTransportError(
                        f"{method} {self._safe_url(url)} failed after "
                        f"{self.max_retries} retries: {type(exc).__name__}"
                    ) from exc
                log.debug("connection error on attempt %d, backing off %.1fs", attempt, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code in expected:
                return resp

            retryable = resp.status_code == 429 or 500 <= resp.status_code < 600
            if retryable and attempt < self.max_retries:
                wait = self._retry_after(resp) or backoff
                log.debug(
                    "HTTP %d on attempt %d, retrying in %.1fs", resp.status_code, attempt, wait
                )
                time.sleep(wait)
                backoff *= 2
                continue

            raise PangramTransportError(
                f"{method} {self._safe_url(url)} returned HTTP {resp.status_code}",
                status_code=resp.status_code,
            )
        # Unreachable: the loop either returns or raises on the final attempt.
        raise PangramTransportError(f"{method} {self._safe_url(url)} exhausted retries")

    @staticmethod
    def _retry_after(resp: requests.Response) -> float | None:
        """Parse a ``Retry-After`` header (seconds form) if present and valid."""
        value = resp.headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    @staticmethod
    def _json(resp: requests.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as exc:
            raise PangramError("response body was not valid JSON") from exc
        if not isinstance(data, dict):
            raise PangramError(f"expected a JSON object, got {type(data).__name__}")
        return data

    @staticmethod
    def _safe_url(url: str) -> str:
        """URLs carry no secrets (the key is a header), but strip any query."""
        return url.split("?", 1)[0]
