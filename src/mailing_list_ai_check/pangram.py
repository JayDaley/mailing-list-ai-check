"""Pangram AI-detection client (raw ``requests``, not the SDK).

Implements the live-verified async contract from ``docs/findings/pangram.md``:
submit a single text with ``POST /task`` (returns HTTP 200 + ``{task_id}``),
then poll ``GET /task/{task_id}`` until ``stage`` is terminal
(``STAGE_SUCCESS`` / ``STAGE_FAILED``). Auth is the ``x-api-key`` header.
Every submit names the detector generation explicitly (``model``, default
``pangram-4``) because an omitted ``model`` still routes to Pangram 3 until
that generation is deprecated.

The Bulk API is supported alongside the realtime endpoint:
:meth:`PangramClient.predict_bulk` submits many texts as one job
(``POST /bulk`` with caller-chosen item ids), polls ``GET /bulk/{bulk_id}``
until ``status`` is terminal (``succeeded`` / ``failed`` / ``partial``), then
pages ``GET /bulk/{bulk_id}/results`` and returns per-item outcomes. Bulk
words are billed at a 20% discount, which suits large catch-up runs.

The client owns its HTTP layer deliberately (rather than using ``pangram-sdk``)
so it can add behaviour the SDK lacks and the findings doc requires: retry with
exponential backoff on ``429``/``5xx`` and connection errors, a conservative
client-side submit rate limit (the vendor ceiling is ~5 QPS), an overall task
deadline, and — importantly for a public repo — a guarantee that the API key is
only ever read from the caller (``Config.load().pangram_api_key``) and never
logged. A single :class:`requests.Session` is reused across calls.

The submit POSTs are the exception to the retry policy: ``POST /task`` and
``POST /bulk`` create billed work, so a request the server may already have
received is never re-sent. A submit is retried only on HTTP 429 (the server
rejected it before doing work) or a connect-phase timeout (it was never sent);
a read timeout, a mid-request connection failure or a 5xx response raises
immediately instead, leaving the run to resume on a later pass. A 1,000-item
bulk submit was observed taking longer than the 10 s default request timeout
to be accepted, and the retries then created one billed job per attempt —
hence both the rule and the separate, larger ``bulk_submit_timeout``.

The 50-word "too short to score" gate is **not** enforced here — that is the
scoring pipeline's job (the server itself does not enforce it).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger("mailing_list_ai_check.pangram")

# --- Contract constants (from pangram-sdk 0.3.1, live-verified 2026-07-21) ----

API_BASE = "https://text.external-api.pangram.com"
SUBMIT_URL = f"{API_BASE}/task"
TASK_URL = f"{API_BASE}/task/{{task_id}}"
BULK_SUBMIT_URL = f"{API_BASE}/bulk"
BULK_STATUS_URL = f"{API_BASE}/bulk/{{bulk_id}}"
BULK_RESULTS_URL = f"{API_BASE}/bulk/{{bulk_id}}/results"

SUCCESS_STAGE = "STAGE_SUCCESS"
FAILED_STAGE = "STAGE_FAILED"

#: Terminal values of a bulk job's ``status``; anything else means keep polling.
BULK_TERMINAL_STATUSES = ("succeeded", "failed", "partial")
BULK_FAILED_STATUS = "failed"

#: Documented maximum ``limit`` for one ``GET /bulk/{bulk_id}/results`` page.
BULK_RESULTS_PAGE_LIMIT = 1000

#: Detector generation requested on every submit. The API resolves an omitted
#: ``model`` to ``"default"``, which still routes to Pangram 3 until that
#: generation is deprecated (announced for 2026-09-30), so v4 must be named
#: explicitly. ``GET /models`` lists the selectors an API key may use.
DEFAULT_MODEL = "pangram-4"

#: The detector generation each accepted ``model`` selector resolves to, as the
#: leading component of the ``version`` a response stamps (Pangram 4 returns
#: "4.0", Pangram 3 returned "3.3.2"), which is what ``scores.detector_version``
#: holds. ``"default"`` maps to generation 3 only until Pangram 3 is deprecated
#: (announced for 2026-09-30); after that the API resolves it to a later
#: generation and this mapping must be revisited.
MODEL_GENERATIONS = {"pangram-4": "4", "default": "3"}


def generation_for_model(model: str) -> str | None:
    """Return the detector generation ``model`` selects, or ``None`` if unknown.

    An unknown selector yields ``None`` rather than a guess, so callers that key
    the score cache on the generation fall back to not filtering at all instead
    of silently matching the wrong verdicts.
    """
    return MODEL_GENERATIONS.get(model)


#: HTTP status codes accepted from ``POST /task`` (live returns 200; the SDK/v3
#: migration notes mention 202 — accept both defensively) and from
#: ``POST /bulk`` (documented as 202; accept 200 defensively for the same
#: reason the single-text submit accepts both).
_SUBMIT_OK = (200, 202)

# Timeouts / intervals mirror the SDK defaults documented in the findings.
DEFAULT_HTTP_TIMEOUT = 10.0  # per-request
#: Per-request timeout for the bulk submit alone: accepting a large item list
#: server-side takes well over the 10 s default (measured on a 1,000-item job),
#: and a timed-out submit cannot be retried safely (see the module docstring).
DEFAULT_BULK_SUBMIT_TIMEOUT = 120.0
DEFAULT_OVERALL_TIMEOUT = 300.0  # overall task deadline
DEFAULT_POLL_INTERVAL = 0.5  # between polls
DEFAULT_BULK_OVERALL_TIMEOUT = 3600.0  # overall bulk-job deadline (findings §4)
#: Bulk jobs run for minutes, not seconds — poll far less often than /task.
DEFAULT_BULK_POLL_INTERVAL = 5.0
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


class PangramBulkFailed(PangramError):
    """The bulk job reached ``status: "failed"`` (no item produced a result).

    Per-item failures inside a ``succeeded``/``partial`` job do not raise; they
    are returned in :attr:`PangramBulkOutcome.errors`.
    """


# --- Result -------------------------------------------------------------------


@dataclass(frozen=True)
class PangramResult:
    """The fields Phase 4 surfaces from a successful classification.

    Every field is stored exactly as the API returned it; ``prediction_short``
    (``AI`` / ``Human`` / ``Mixed``) is the categorical the pipeline stores as
    ``scores.label``. Assisted-dominated text arrives as ``Mixed`` — the
    ``fraction_ai_assisted`` value and the free-text ``headline`` carry that
    distinction; no derived label is computed (the pre-1.4.1 "AI-Assisted"
    rebadge was removed by migration 013).

    ``raw`` is the full parsed JSON response (including ``headline``,
    ``prediction``, ``num_*_segments`` and the ``windows`` array — with the
    per-window ``is_humanized``/``humanizer_score`` fields Pangram 4 added),
    stored verbatim so nothing is lost. Note that Pangram 4 may normalize the
    submitted text before inference; ``raw["text"]`` is that normalized text
    and window offsets index into it, not into the submitted string.
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


@dataclass(frozen=True)
class PangramBulkOutcome:
    """Per-item outcomes of one bulk job, keyed by the caller's item ids.

    Every id submitted to :meth:`PangramClient.predict_bulk` appears in exactly
    one of ``results`` (a successful classification, same schema as a
    single-text result) or ``errors`` (the item's error message, or
    ``"missing from bulk results"`` when the job never reported it back).
    ``status`` is the job's terminal status (``succeeded`` or ``partial``; a
    ``failed`` job raises :class:`PangramBulkFailed` instead).
    """

    bulk_id: str
    status: str
    results: dict[str, PangramResult] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)


# --- Client -------------------------------------------------------------------


class PangramClient:
    """Async Pangram client (realtime and bulk) with retries and rate limiting.

    Parameters
    ----------
    api_key:
        Read from ``Config.load().pangram_api_key`` by the caller. Never logged.
    session:
        A :class:`requests.Session` to reuse; a fresh one is created if omitted.
    model:
        The detector generation sent with every submit (default
        :data:`DEFAULT_MODEL`, i.e. Pangram 4).
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        session: requests.Session | None = None,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
        bulk_submit_timeout: float = DEFAULT_BULK_SUBMIT_TIMEOUT,
        overall_timeout: float = DEFAULT_OVERALL_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_retries: int = DEFAULT_MAX_RETRIES,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
        min_submit_interval: float = DEFAULT_MIN_SUBMIT_INTERVAL,
        bulk_overall_timeout: float = DEFAULT_BULK_OVERALL_TIMEOUT,
        bulk_poll_interval: float = DEFAULT_BULK_POLL_INTERVAL,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be a non-empty string")
        self._api_key = api_key
        self.model = model
        self.session = session or requests.Session()
        self.http_timeout = http_timeout
        self.bulk_submit_timeout = bulk_submit_timeout
        self.overall_timeout = overall_timeout
        self.poll_interval = poll_interval
        self.bulk_overall_timeout = bulk_overall_timeout
        self.bulk_poll_interval = bulk_poll_interval
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
            idempotent=False,
            json={"text": text, "model": self.model, "public_dashboard_link": False},
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

    def predict_bulk(self, items: Mapping[str, str]) -> PangramBulkOutcome:
        """Classify many texts as one bulk job, blocking until it completes.

        ``items`` maps caller-chosen ids (echoed back by the API) to the texts
        to classify; the outcome's ``results``/``errors`` are keyed by those
        same ids. Raises :class:`PangramBulkFailed` when the whole job fails,
        or :class:`PangramTransportError` / :class:`PangramTimeout` /
        :class:`PangramError` as :meth:`predict` does; per-item failures within
        a completed job are returned, not raised.
        """
        if not items:
            raise ValueError("items must not be empty")
        deadline = time.monotonic() + self.bulk_overall_timeout

        self._throttle_submit()
        resp = self._request(
            "POST",
            BULK_SUBMIT_URL,
            expected=_SUBMIT_OK,
            idempotent=False,
            timeout=self.bulk_submit_timeout,
            json={
                "items": [{"id": item_id, "text": text} for item_id, text in items.items()],
                "model": self.model,
            },
        )
        self._last_submit_at = time.monotonic()
        bulk_id = self._json(resp).get("bulk_id")
        if not bulk_id:
            raise PangramError("bulk submit response contained no bulk_id")

        status_url = BULK_STATUS_URL.format(bulk_id=bulk_id)
        while True:
            if time.monotonic() >= deadline:
                raise PangramTimeout(
                    f"bulk job {bulk_id} did not complete within {self.bulk_overall_timeout:.0f}s"
                )
            resp = self._request("GET", status_url, expected=(200,))
            data = self._json(resp)
            status = data.get("status")
            if status == BULK_FAILED_STATUS:
                detail = data.get("detail") or data.get("error") or "no detail"
                raise PangramBulkFailed(f"bulk job {bulk_id} failed: {detail}")
            if status in BULK_TERMINAL_STATUSES:  # succeeded / partial
                break
            time.sleep(self.bulk_poll_interval)

        results: dict[str, PangramResult] = {}
        errors: dict[str, str] = {}
        results_url = BULK_RESULTS_URL.format(bulk_id=bulk_id)
        offset = 0
        while True:
            resp = self._request(
                "GET",
                results_url,
                expected=(200,),
                params={"offset": offset, "limit": BULK_RESULTS_PAGE_LIMIT},
            )
            entries = self._bulk_page_entries(self._json(resp))
            for entry in entries:
                item_id = entry.get("id")
                if item_id is None:
                    continue
                item_id = str(item_id)
                result = entry.get("result")
                if isinstance(result, dict) and result.get("stage") != FAILED_STAGE:
                    results[item_id] = PangramResult.from_response(result)
                    continue
                error = entry.get("error")
                if not error and isinstance(result, dict):
                    error = result.get("detail") or result.get("headline")
                errors[item_id] = str(error) if error else "no result returned"
            offset += len(entries)
            if len(entries) < BULK_RESULTS_PAGE_LIMIT:
                break

        # An id the job never reported back is a failure, not a silent gap.
        for item_id in items:
            if item_id not in results and item_id not in errors:
                errors[item_id] = "missing from bulk results"
        return PangramBulkOutcome(
            bulk_id=str(bulk_id), status=str(status), results=results, errors=errors
        )

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _bulk_page_entries(data: dict[str, Any]) -> list[dict[str, Any]]:
        """The item list of one results page.

        Live-verified (2026-08-12) under ``items``; ``results`` is accepted
        defensively in case the envelope ever changes.
        """
        for key in ("items", "results"):
            entries = data.get(key)
            if isinstance(entries, list):
                return [entry for entry in entries if isinstance(entry, dict)]
        raise PangramError("bulk results page contained no items/results list")

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
        self,
        method: str,
        url: str,
        *,
        expected: tuple[int, ...],
        idempotent: bool = True,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Issue one HTTP request, retrying transient failures with backoff.

        Retries ``429`` and ``5xx`` responses and connection-level errors up to
        ``max_retries`` times, honouring a ``Retry-After`` header when present.
        Any other non-``expected`` status raises immediately (not retryable).

        ``idempotent=False`` marks a request that creates billed work (the
        ``/task`` and ``/bulk`` submits): it is retried only when the server
        demonstrably did no work — an HTTP 429, or a connect-phase timeout
        (the request was never sent). A read timeout, another connection
        failure or a 5xx raises immediately, because the job may exist
        server-side and a re-send would be billed again.

        ``timeout`` overrides the per-request ``http_timeout`` for this call.
        """
        backoff = self.initial_backoff
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    headers=self._headers(),
                    timeout=timeout if timeout is not None else self.http_timeout,
                    **kwargs,
                )
            except requests.RequestException as exc:
                # ConnectTimeout means the connection was never established, so
                # nothing reached the server; every other transport failure may
                # have delivered the request.
                may_retry = idempotent or isinstance(exc, requests.exceptions.ConnectTimeout)
                if not may_retry or attempt >= self.max_retries:
                    raise PangramTransportError(
                        f"{method} {self._safe_url(url)} failed"
                        + (
                            f" after {self.max_retries} retries"
                            if may_retry
                            else " (not retried: the submit may already have been accepted)"
                        )
                        + f": {type(exc).__name__}"
                    ) from exc
                log.debug("connection error on attempt %d, backing off %.1fs", attempt, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code in expected:
                return resp

            retryable = resp.status_code == 429 or (idempotent and 500 <= resp.status_code < 600)
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
