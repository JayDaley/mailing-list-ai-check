"""The ``/api`` JSON blueprint over the SQLite store.

Every endpoint the Vue dashboard needs lives here: a filterable, paginated
message explorer, message detail, overview aggregates, and the list / address /
person entities plus person management. The heavy filtered query itself lives in
:meth:`mailing_list_ai_check.store.Store.query_messages`; this module only parses and
validates request input, calls the store, and shapes the JSON.

Connection handling
-------------------
A fresh :class:`~mailing_list_ai_check.store.Store` (one ``sqlite3`` connection) is
opened per request via :func:`get_store` and closed on app-context teardown (see
:func:`mailing_list_ai_check.webapp.create_app`). Because the connection is created
and used within a single request — and therefore a single thread — the default
``sqlite3`` ``check_same_thread=True`` is correct and needs no relaxing, even
under the threaded dev server.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any

from flask import Blueprint, Response, current_app, g, jsonify, request

from ..cleaning import clean_for_scoring
from ..cli import run_extract, run_score
from ..export_import import ExportImportError, export_lists, import_file
from ..html_text import split_html_parts
from ..fetcher import (
    DepthMode,
    FetchRequest,
    open_client,
    parse_header,
    refresh_lists_index,
    resolve_folders,
    run_fetch,
    run_fetch_uids,
)
from ..imap_client import build_search_criteria
from ..pangram import PangramClient
from ..store import (
    DEFAULT_PER_PAGE,
    MAX_PER_PAGE,
    SORT_COLUMNS,
    MessageFilters,
    Store,
)

api_bp = Blueprint("api", __name__, url_prefix="/api")

#: Allowed characters in a mailing-list name (maps to an IMAP folder slug):
#: letters, digits, dot, hyphen, underscore. Guards against odd/injection-y
#: folder names before we ever touch the server.
_LIST_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Bounds on the per-pull message count (the field is the only cap on this
#: network+paid endpoint — there is no server-side testing limit here).
_MIN_PULL_COUNT = 1
_MAX_PULL_COUNT = 1000

#: Default page size for the Senders pane (denser than the message explorer).
_DEFAULT_SENDER_PER_PAGE = 60
#: Sort keys the Senders pane accepts, mapped to their default sort direction
#: (used when the request omits ``order``).
_SENDER_SORTS = {"count": "desc", "name": "asc"}


# --- errors -------------------------------------------------------------------


class ApiError(Exception):
    """An error to surface to the client as ``{"error": msg}`` with a status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# --- per-request store --------------------------------------------------------


def get_store() -> Store:
    """Return this request's :class:`Store`, opening one on first use."""
    if "store" not in g:
        g.store = Store(current_app.config["STORE_PATH"])
    return g.store


# --- input parsing / validation ----------------------------------------------


def _parse_int(name: str, raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ApiError(f"{name} must be an integer") from exc


def _parse_float(name: str, raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ApiError(f"{name} must be a number") from exc


def _parse_bool(name: str, raw: str | None) -> bool | None:
    if raw is None or raw == "":
        return None
    low = raw.strip().lower()
    if low in ("1", "true", "yes"):
        return True
    if low in ("0", "false", "no"):
        return False
    raise ApiError(f"{name} must be a boolean (true/false)")


def _validate_iso(name: str, raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    try:
        # Accepts both date ("2026-03-01") and datetime forms.
        from datetime import datetime

        datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(f"{name} must be an ISO-8601 date or datetime") from exc
    return raw


def parse_filters(args: Any) -> MessageFilters:
    """Parse and validate the shared query params into a :class:`MessageFilters`.

    Raises :class:`ApiError` (400) on any malformed value so callers never see a
    500 for bad input. ``per_page`` above the cap is clamped, not rejected.
    """
    page = _parse_int("page", args.get("page"))
    if page is None:
        page = 1
    elif page < 1:
        raise ApiError("page must be >= 1")

    per_page = _parse_int("per_page", args.get("per_page"))
    if per_page is None:
        per_page = DEFAULT_PER_PAGE
    elif per_page < 1:
        raise ApiError("per_page must be >= 1")
    else:
        per_page = min(per_page, MAX_PER_PAGE)

    sort = args.get("sort", "date")
    if sort not in SORT_COLUMNS:
        raise ApiError(f"sort must be one of {sorted(SORT_COLUMNS)}")

    order = args.get("order", "desc").lower()
    if order not in ("asc", "desc"):
        raise ApiError("order must be 'asc' or 'desc'")

    min_l = _parse_float("min_likelihood", args.get("min_likelihood"))
    max_l = _parse_float("max_likelihood", args.get("max_likelihood"))
    for label, value in (("min_likelihood", min_l), ("max_likelihood", max_l)):
        if value is not None and not (0.0 <= value <= 1.0):
            raise ApiError(f"{label} must be between 0 and 1")

    return MessageFilters(
        list_name=args.get("list") or None,
        address=args.get("address") or None,
        person_id=_parse_int("person", args.get("person")),
        date_from=_validate_iso("date_from", args.get("date_from")),
        date_to=_validate_iso("date_to", args.get("date_to")),
        label=args.get("label") or None,
        min_likelihood=min_l,
        max_likelihood=max_l,
        q=args.get("q") or None,
        has_score=_parse_bool("has_score", args.get("has_score")),
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
    )


# --- serialization ------------------------------------------------------------


def _serialize_message_row(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a :meth:`Store.query_messages` row into the list-item JSON."""
    extraction = None
    if row["extraction_status"] is not None:
        extraction = {
            "status": row["extraction_status"],
            "method": row["extraction_method"],
            "char_count": row["extraction_char_count"],
        }
    score = None
    if row["scored_at"] is not None:
        score = {
            "fraction_ai": row["fraction_ai"],
            "fraction_ai_assisted": row["fraction_ai_assisted"],
            "fraction_human": row["fraction_human"],
            "label": row["label"],
            "detector_version": row["detector_version"],
            "scored_at": row["scored_at"],
        }
    person = None
    if row["person_id"] is not None:
        person = {"id": row["person_id"], "name": row["person_name"]}
    return {
        "id": row["id"],
        "message_id": row["message_id"],
        "list": row["list"],
        "date": row["date"],
        "subject": row["subject"],
        "from": {"address": row["from_address"], "display_name": row["from_display_name"]},
        "person": person,
        "extraction": extraction,
        "score": score,
    }


def _person_detail(store: Store, person_id: int) -> dict[str, Any] | None:
    """Build the canonical person JSON (name + attached addresses)."""
    person = store.get_person(person_id)
    if person is None:
        return None
    addrs = store.addresses_for_person(person_id)
    return {
        "id": person.id,
        "canonical_name": person.canonical_name,
        "addresses": [
            {"id": a.id, "email": a.email, "display_name": a.display_name} for a in addrs
        ],
    }


def _json_body() -> dict[str, Any]:
    """Return the request JSON object, or ``{}``; 400 if the body is not an object."""
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApiError("request body must be a JSON object")
    return data


def _address_id_list(data: dict[str, Any], key: str) -> list[int]:
    """Validate ``data[key]`` is a list of ints (empty if absent)."""
    raw = data.get(key, [])
    if not isinstance(raw, list):
        raise ApiError(f"{key} must be a list of address ids")
    ids: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ApiError(f"{key} must contain integer address ids")
        ids.append(item)
    return ids


# --- message endpoints --------------------------------------------------------


@api_bp.get("/messages")
def list_messages() -> Any:
    filters = parse_filters(request.args)
    rows, total = get_store().query_messages(filters)
    pages = math.ceil(total / filters.per_page) if filters.per_page else 0
    return jsonify(
        {
            "messages": [_serialize_message_row(r) for r in rows],
            "page": filters.page,
            "per_page": filters.per_page,
            "total": total,
            "pages": pages,
            "sort": filters.sort,
            "order": filters.order,
        }
    )


@api_bp.get("/messages/<int:message_id>")
def message_detail(message_id: int) -> Any:
    store = get_store()
    msg = store.get_message(message_id)
    if msg is None:
        raise ApiError("message not found", 404)

    extraction = store.extraction_for_message(msg.id)
    score = store.score_for_extraction(extraction.id) if extraction is not None else None

    thread_parent_id = None
    if msg.in_reply_to:
        parent = store.find_message_by_message_id(msg.in_reply_to)
        if parent is not None and parent.id != msg.id:
            thread_parent_id = parent.id

    mailing_list = store.get_list(msg.list_id)
    address = store.get_address(msg.address_id) if msg.address_id is not None else None
    person = None
    if address is not None and address.person_id is not None:
        person_row = store.get_person(address.person_id)
        if person_row is not None:
            person = {"id": person_row.id, "name": person_row.canonical_name}

    extraction_json = None
    if extraction is not None:
        # Report what the scoring stage would remove so the dashboard can grey
        # those lines out. ``ignored_lines`` are 0-based indices into
        # ``extracted_text.split("\n")``; ``scored_word_count`` is the word count
        # of the cleaned text that would actually be sent to the detector. The
        # HTML signature hint (when the message has ``raw_html``) is applied so
        # these reflect exactly what scoring would drop.
        html_signature = split_html_parts(msg.raw_html).signature_text if msg.raw_html else None
        clean = clean_for_scoring(extraction.extracted_text, html_signature or None)
        extraction_json = {
            "status": extraction.status,
            "method": extraction.method,
            "char_count": extraction.char_count,
            "extracted_text": extraction.extracted_text,
            "ignored_lines": clean.ignored_lines,
            "scored_word_count": len(clean.text.split()),
        }

    score_json = None
    if score is not None:
        raw_response = None
        if score.raw_response:
            try:
                raw_response = json.loads(score.raw_response)
            except (ValueError, TypeError):
                raw_response = None
        score_json = {
            "fraction_ai": score.fraction_ai,
            "fraction_ai_assisted": score.fraction_ai_assisted,
            "fraction_human": score.fraction_human,
            "label": score.label,
            "detector_version": score.detector_version,
            "scored_at": score.scored_at,
            "raw_response": raw_response,
        }

    return jsonify(
        {
            "id": msg.id,
            "message_id": msg.message_id,
            "list": mailing_list.name if mailing_list else None,
            "date": msg.date,
            "subject": msg.subject,
            "in_reply_to": msg.in_reply_to,
            "thread_parent_id": thread_parent_id,
            "raw_body": msg.raw_body,
            "from": {
                "address": address.email if address else None,
                "display_name": address.display_name if address else None,
            },
            "person": person,
            "extraction": extraction_json,
            "score": score_json,
        }
    )


@api_bp.get("/summary")
def summary() -> Any:
    filters = parse_filters(request.args)
    return jsonify(get_store().summary(filters))


# --- pull (fetch + extract + score) ------------------------------------------


def _fetch_for_list(config: Any, store: Store, list_name: str, count: int) -> Any:
    """Run the Phase 2 fetcher for one list in ``--count`` mode.

    Mirrors the way ``cli.py``'s pull command wires the fetcher: open a client,
    resolve the single list to its folder, fetch the most recent ``count``
    messages, and always close/log out. Any IMAP-side failure (connection error,
    unknown folder) is surfaced as a 502 ``ApiError`` — never a 500 traceback.
    """
    try:
        client = open_client(
            config.imap_host, config.imap_port, config.imap_username, config.imap_password
        )
    except Exception as exc:  # noqa: BLE001 - report any connection failure cleanly
        raise ApiError(f"could not connect to the IMAP server: {exc}", 502) from exc

    try:
        folders = resolve_folders(client, [list_name], all_lists=False)
        fetch_request = FetchRequest(
            folders=tuple(folders),
            depth=DepthMode(count=count),
            limit=count,
        )
        return run_fetch(client, store, fetch_request)
    except Exception as exc:  # noqa: BLE001 - IMAP/fetch failures become a 502
        raise ApiError(f"IMAP fetch failed for list {list_name!r}: {exc}", 502) from exc
    finally:
        try:
            client.close()
            client.logout()
        except Exception:  # noqa: BLE001 - never let teardown mask the real result
            pass


@api_bp.post("/pull")
def pull() -> Any:
    """Fetch → extract → (optionally) score the most recent messages of a list.

    Body: ``{"list": "<name>", "count": <int 1-1000>}``. Scoring runs only when a
    Pangram API key is configured; otherwise it is skipped and ``scoring_skipped``
    is true in the response. This endpoint deliberately performs network and paid
    work on an explicit user click — ``count`` is the only cap.
    """
    data = _json_body()

    list_name = data.get("list")
    if not isinstance(list_name, str) or not list_name.strip():
        raise ApiError("list is required")
    list_name = list_name.strip()
    if not _LIST_NAME_RE.match(list_name):
        raise ApiError("list name may contain only letters, digits, '.', '-' and '_'")

    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        raise ApiError("count must be an integer")
    if not (_MIN_PULL_COUNT <= count <= _MAX_PULL_COUNT):
        raise ApiError(f"count must be between {_MIN_PULL_COUNT} and {_MAX_PULL_COUNT}")

    config = current_app.config["APP_CONFIG"]
    store = get_store()

    fetch_summary = _fetch_for_list(config, store, list_name, count)

    status_counts, _method_counts = run_extract(store, limit=count)

    scoring_skipped = not config.pangram_api_key
    scored = cache_hits = api_calls = too_short = 0
    if not scoring_skipped:
        pangram = PangramClient(config.pangram_api_key)
        score_summary = run_score(store, pangram, limit=count)
        scored = score_summary.scored
        cache_hits = score_summary.cache_hits
        api_calls = score_summary.api_calls
        too_short = score_summary.too_short

    return jsonify(
        {
            "fetched": fetch_summary.fetched,
            "duplicates": fetch_summary.duplicates,
            "parse_errors": fetch_summary.parse_errors,
            "extracted": status_counts.get("ok", 0),
            "empty": status_counts.get("empty", 0),
            "too_short": too_short,
            "scored": scored,
            "cache_hits": cache_hits,
            "api_calls": api_calls,
            "scoring_skipped": scoring_skipped,
        }
    )


# --- add messages: preview + ranged pull -------------------------------------
#
# Two endpoints back the dashboard's "Add messages" popover. Both work in a
# direction relative to what is already stored for a list:
#   - "new":    messages with a UID greater than the incremental cursor (or, with
#               no cursor valid for the folder's current UIDVALIDITY, greater than
#               the largest stored UID; else everything);
#   - "before": messages with a UID smaller than the earliest stored UID.
# /lists/preview is strictly read-only (EXAMINE + UID SEARCH + a header-only
# FETCH — no store write, no pull_state change, no Pangram). /pull/range then
# fetches the chosen bodies and runs the same extract/score pipeline as /pull.

#: How many messages a preview shows (the first N for "new", the last N for
#: "before"); also the default "before" ``count``.
_PREVIEW_COUNT = 25


def _open_client_or_502(config: Any) -> Any:
    """Open an IMAP client, mapping any connection failure to a 502 ``ApiError``."""
    try:
        return open_client(
            config.imap_host, config.imap_port, config.imap_username, config.imap_password
        )
    except Exception as exc:  # noqa: BLE001 - report any connection failure cleanly
        raise ApiError(f"could not connect to the IMAP server: {exc}", 502) from exc


def _close_client_quietly(client: Any) -> None:
    """Close and log out ``client``, never letting teardown mask the real result."""
    try:
        client.close()
        client.logout()
    except Exception:  # noqa: BLE001 - teardown errors are irrelevant to the result
        pass


def _list_and_mode(data: dict[str, Any]) -> tuple[str, str]:
    """Validate and return the shared ``(list_name, mode)`` for both endpoints.

    Raises a 400 ``ApiError`` for a missing/ill-formed list name (same rule as
    :func:`pull`) or a ``mode`` that is not exactly ``"new"`` or ``"before"``.
    List-row existence (a 404) is checked separately by the caller.
    """
    list_name = data.get("list")
    if not isinstance(list_name, str) or not list_name.strip():
        raise ApiError("list is required")
    list_name = list_name.strip()
    if not _LIST_NAME_RE.match(list_name):
        raise ApiError("list name may contain only letters, digits, '.', '-' and '_'")

    mode = data.get("mode")
    if mode not in ("new", "before"):
        raise ApiError("mode must be 'new' or 'before'")
    return list_name, mode


def _resolve_list_or_404(store: Store, list_name: str) -> Any:
    """Return the existing list row for ``list_name`` or raise a 404 ``ApiError``.

    Never creates a row — preview and ranged pull operate only on lists already
    known to the store (indexed or previously pulled).
    """
    row = store.get_list_by_name(list_name)
    if row is None:
        raise ApiError(f"unknown list {list_name!r}", 404)
    return row


def _preview_count(data: dict[str, Any]) -> int:
    """Parse the "before" preview ``count``: default 25, clamped to 1..1000.

    A non-integer (including ``bool``) is a 400; an out-of-range integer is
    clamped rather than rejected.
    """
    count = data.get("count")
    if count is None:
        return _PREVIEW_COUNT
    if isinstance(count, bool) or not isinstance(count, int):
        raise ApiError("count must be an integer")
    return max(_MIN_PULL_COUNT, min(count, _MAX_PULL_COUNT))


def _range_count(data: dict[str, Any], mode: str) -> int | None:
    """Parse the ranged-pull ``count`` per mode.

    ``mode "new"``: a missing/``null`` count means "all" (returned as ``None``;
    the caller caps at :data:`_MAX_PULL_COUNT`); a provided value must be an
    integer in 1..1000. ``mode "before"``: ``count`` is required and must be an
    integer in 1..1000. ``bool`` is never a valid integer here (mirrors
    :func:`pull`). Out-of-range or ill-typed values are a 400.
    """
    count = data.get("count")
    if mode == "new" and count is None:
        return None
    if isinstance(count, bool) or not isinstance(count, int):
        raise ApiError("count must be an integer")
    if not (_MIN_PULL_COUNT <= count <= _MAX_PULL_COUNT):
        raise ApiError(f"count must be between {_MIN_PULL_COUNT} and {_MAX_PULL_COUNT}")
    return count


def _candidate_uids(client: Any, store: Store, list_row: Any, mode: str) -> tuple[int, list[int]]:
    """Return ``(uidvalidity, uids)`` for the "new"/"before" candidate set.

    Shared by preview and the ranged pull so both agree on the full set before
    slicing. EXAMINEs the folder read-only, then runs one UID SEARCH:

    - ``"new"``: the baseline is the incremental cursor's ``last_uid`` when a
      cursor exists whose UIDVALIDITY matches the folder's current one, else the
      largest stored UID, else 0. The set is ``UID SEARCH {baseline+1}:*``
      filtered to ``uid > baseline`` (dropping the ``n:*`` echo of the top UID),
      ascending.
    - ``"before"``: anchored on the smallest stored UID; a 404 when the list has
      no UID-bearing message to anchor against. The set is ``UID SEARCH
      1:{min_uid-1}`` (empty when ``min_uid <= 1``), ascending.
    """
    status = client.examine(list_row.folder)
    uidvalidity = status.uidvalidity

    if mode == "new":
        cursor = store.get_pull_state(list_row.id)
        if cursor is not None and cursor.uidvalidity == uidvalidity:
            baseline = cursor.last_uid
        else:
            baseline = store.max_uid_for_list(list_row.id) or 0
        found = client.uid_search(build_search_criteria(uid_range=f"{baseline + 1}:*"))
        return uidvalidity, sorted(u for u in found if u > baseline)

    min_uid = store.min_uid_for_list(list_row.id)
    if min_uid is None:
        raise ApiError(
            f"list {list_row.name!r} has no stored messages to anchor a 'before' pull",
            404,
        )
    if min_uid <= 1:
        return uidvalidity, []
    found = client.uid_search(build_search_criteria(uid_range=f"1:{min_uid - 1}"))
    return uidvalidity, sorted(found)


def _preview_rows(client: Any, uids: Sequence[int]) -> list[dict[str, Any]]:
    """Fetch header-only rows for ``uids`` and shape them, ascending by UID.

    Only ``From``/``Subject``/``Date`` are fetched (``BODY.PEEK[HEADER.FIELDS]``)
    and parsed via :func:`~mailing_list_ai_check.fetcher.parse_header`, so a
    preview never touches a body or the ``\\Seen`` flag. UIDs missing from the
    server response (or an un-mapped echo) are skipped.
    """
    by_uid: dict[int, dict[str, Any]] = {}
    for uid, raw in client.fetch_headers(uids):
        if uid is None:
            continue
        header = parse_header(raw)
        by_uid[uid] = {
            "from_name": header.from_name,
            "from_email": header.from_email,
            "subject": header.subject,
            "date": header.date,
        }
    return [by_uid[u] for u in sorted(by_uid)]


@api_bp.post("/lists/preview")
def preview() -> Any:
    """Preview candidate messages to add for a list, storing nothing.

    Body: ``{"list": "<name>", "mode": "new"|"before", "count": <int>}``. Strictly
    read-only: EXAMINE + UID SEARCH + a header-only FETCH; no body is fetched, no
    row is written, the ``pull_state`` cursor is untouched, and Pangram is never
    called. ``mode "new"`` always previews the first (oldest) 25 newer-than-stored
    messages; ``mode "before"`` previews the last ``count`` (default 25, clamped
    1..1000) messages immediately preceding the earliest stored one. Rows come
    back in ascending UID order. An IMAP connect/enumeration failure is a 502.
    """
    data = _json_body()
    list_name, mode = _list_and_mode(data)
    store = get_store()
    list_row = _resolve_list_or_404(store, list_name)
    count = _preview_count(data) if mode == "before" else _PREVIEW_COUNT

    config = current_app.config["APP_CONFIG"]
    client = _open_client_or_502(config)
    try:
        _uidvalidity, uids = _candidate_uids(client, store, list_row, mode)
        chosen = uids[:_PREVIEW_COUNT] if mode == "new" else uids[-count:]
        rows = _preview_rows(client, chosen)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - IMAP/enumeration failures become a 502
        raise ApiError(f"IMAP preview failed for list {list_name!r}: {exc}", 502) from exc
    finally:
        _close_client_quietly(client)

    total = len(uids)
    shown = len(rows)
    return jsonify(
        {
            "mode": mode,
            "list": list_name,
            "total": total,
            "shown": shown,
            "more": total - shown,
            "messages": rows,
        }
    )


@api_bp.post("/pull/range")
def pull_range() -> Any:
    """Fetch → extract → (optionally) score a directional range of a list's messages.

    Body: ``{"list": "<name>", "mode": "new"|"before", "count": <int|null>}``.
    ``mode "new"`` pulls the first ``count`` newer-than-stored messages (a
    missing/``null`` count means all, capped at 1000); ``mode "before"`` pulls the
    last ``count`` messages preceding the earliest stored one (``count``
    required). The candidate set is recomputed exactly as :func:`preview` does.
    Scoring runs only when a Pangram API key is configured. The ``pull_state``
    cursor advances for a "new" pull (never regressing) and is never touched by a
    "before" pull. An IMAP failure is a 502.
    """
    data = _json_body()
    list_name, mode = _list_and_mode(data)
    store = get_store()
    list_row = _resolve_list_or_404(store, list_name)
    count = _range_count(data, mode)

    config = current_app.config["APP_CONFIG"]
    client = _open_client_or_502(config)
    try:
        uidvalidity, uids = _candidate_uids(client, store, list_row, mode)
        matched = len(uids)
        capped = False
        if mode == "new":
            if count is None:
                capped = matched > _MAX_PULL_COUNT
                chosen = uids[:_MAX_PULL_COUNT]
            else:
                chosen = uids[:count]
        else:  # before — count is a required int here
            chosen = uids[-count:] if count is not None and count < len(uids) else uids

        fetch_summary = run_fetch_uids(client, store, list_row.folder, chosen)

        # Cursor bookkeeping: a "new" pull may only ever advance the cursor; a
        # "before" pull reaches into older UIDs and must never move it.
        if mode == "new" and chosen:
            max_uid = max(chosen)
            cursor = store.get_pull_state(list_row.id)
            if cursor is None or cursor.uidvalidity != uidvalidity or max_uid > cursor.last_uid:
                store.set_pull_state(list_row.id, uidvalidity, max_uid)
        store.set_list_synced(list_row.id)
        # Best-effort activity stamp, exactly like run_fetch — never fatal.
        try:
            when = client.last_message_internaldate(list_row.folder)
            if when is not None:
                store.set_list_last_message(list_row.id, when)
        except Exception:  # noqa: BLE001 - an activity check never fails a pull
            pass
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - IMAP/fetch failures become a 502
        raise ApiError(f"IMAP ranged pull failed for list {list_name!r}: {exc}", 502) from exc
    finally:
        _close_client_quietly(client)

    limit = len(chosen)
    status_counts, _method_counts = run_extract(store, limit=limit)

    scoring_skipped = not config.pangram_api_key
    scored = cache_hits = api_calls = too_short = 0
    if not scoring_skipped:
        pangram = PangramClient(config.pangram_api_key)
        score_summary = run_score(store, pangram, limit=limit)
        scored = score_summary.scored
        cache_hits = score_summary.cache_hits
        api_calls = score_summary.api_calls
        too_short = score_summary.too_short

    return jsonify(
        {
            "mode": mode,
            "matched": matched,
            "capped": capped,
            "fetched": fetch_summary.fetched,
            "duplicates": fetch_summary.duplicates,
            "parse_errors": fetch_summary.parse_errors,
            "extracted": status_counts.get("ok", 0),
            "empty": status_counts.get("empty", 0),
            "too_short": too_short,
            "scored": scored,
            "cache_hits": cache_hits,
            "api_calls": api_calls,
            "scoring_skipped": scoring_skipped,
        }
    )


# --- entity endpoints ---------------------------------------------------------


@api_bp.get("/lists")
def list_lists() -> Any:
    return jsonify({"lists": get_store().list_rows()})


@api_bp.post("/lists/regenerate")
def regenerate_lists() -> Any:
    """Re-populate the lists index from the server's IMAP ``LIST`` enumeration.

    A single ``LIST`` round-trip — no message fetches, nothing paid. Lists that
    have disappeared from the server are dropped unless the store holds messages
    for them, in which case the row is kept and stamped ``removed_from_server_at``
    (see :meth:`Store.refresh_lists_index`). Returns the reconciliation counts.
    """
    config = current_app.config["APP_CONFIG"]
    try:
        client = open_client(
            config.imap_host, config.imap_port, config.imap_username, config.imap_password
        )
    except Exception as exc:  # noqa: BLE001 - report any connection failure cleanly
        raise ApiError(f"could not connect to the IMAP server: {exc}", 502) from exc

    try:
        counts = refresh_lists_index(client, get_store())
    except Exception as exc:  # noqa: BLE001 - IMAP failures become a 502
        raise ApiError(f"IMAP list enumeration failed: {exc}", 502) from exc
    finally:
        try:
            client.close()
            client.logout()
        except Exception:  # noqa: BLE001 - never let teardown mask the real result
            pass

    return jsonify(counts)


@api_bp.get("/addresses")
def list_addresses() -> Any:
    q = request.args.get("q") or None
    return jsonify({"addresses": get_store().address_rows(q)})


@api_bp.get("/persons")
def list_persons() -> Any:
    return jsonify({"persons": get_store().person_rows()})


@api_bp.get("/senders")
def list_senders() -> Any:
    """One entry per person (linked address group) or per unlinked address.

    Query params (all optional): ``q`` (case-insensitive substring over name or
    any email), ``list`` (restrict to senders who posted to that list, with
    counts/labels scoped to it; an unknown list yields no senders), ``sort``
    (``count`` default, or ``name``), ``order`` (``asc``/``desc`` — defaults to
    the natural direction for the chosen sort: ``desc`` for count, ``asc`` for
    name), ``page`` (default 1), ``per_page`` (default 60, clamped to
    ``MAX_PER_PAGE``). Bad input yields a 400 like :func:`parse_filters`.
    """
    args = request.args

    q = args.get("q") or None
    list_name = args.get("list") or None

    sort = args.get("sort", "count")
    if sort not in _SENDER_SORTS:
        raise ApiError(f"sort must be one of {sorted(_SENDER_SORTS)}")

    order = args.get("order")
    if order is None or order == "":
        order = _SENDER_SORTS[sort]  # natural default for the chosen sort
    else:
        order = order.lower()
        if order not in ("asc", "desc"):
            raise ApiError("order must be 'asc' or 'desc'")

    page = _parse_int("page", args.get("page"))
    if page is None:
        page = 1
    elif page < 1:
        raise ApiError("page must be >= 1")

    per_page = _parse_int("per_page", args.get("per_page"))
    if per_page is None:
        per_page = _DEFAULT_SENDER_PER_PAGE
    elif per_page < 1:
        raise ApiError("per_page must be >= 1")
    else:
        per_page = min(per_page, MAX_PER_PAGE)

    rows, total = get_store().sender_rows(
        q=q, sort=sort, order=order, page=page, per_page=per_page, list_name=list_name
    )
    return jsonify(
        {
            "senders": rows,
            "total": total,
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
            "list": list_name,
        }
    )


@api_bp.get("/persons/suggestions")
def person_suggestions() -> Any:
    suggestions = get_store().suggest_person_merges()
    return jsonify(
        {
            "suggestions": [
                {
                    "display_name": s.display_name,
                    "address_ids": list(s.address_ids),
                    "emails": list(s.emails),
                }
                for s in suggestions
            ]
        }
    )


@api_bp.post("/persons")
def create_person() -> Any:
    store = get_store()
    data = _json_body()
    name = data.get("canonical_name")
    if not isinstance(name, str) or not name.strip():
        raise ApiError("canonical_name is required")
    address_ids = _address_id_list(data, "address_ids")
    # Validate every address exists before mutating, so a bad id can't leave a
    # half-assigned person behind.
    for aid in address_ids:
        if store.get_address(aid) is None:
            raise ApiError(f"address {aid} not found", 404)

    person = store.create_person(name.strip())
    for aid in address_ids:
        store.assign_address_to_person(aid, person.id)
    return jsonify(_person_detail(store, person.id)), 201


@api_bp.put("/persons/<int:person_id>")
def update_person(person_id: int) -> Any:
    store = get_store()
    if store.get_person(person_id) is None:
        raise ApiError("person not found", 404)

    data = _json_body()
    if "canonical_name" in data:
        name = data["canonical_name"]
        if not isinstance(name, str) or not name.strip():
            raise ApiError("canonical_name must be a non-empty string")
        store.update_person_name(person_id, name.strip())

    add_ids = _address_id_list(data, "add_address_ids")
    remove_ids = _address_id_list(data, "remove_address_ids")
    for aid in (*add_ids, *remove_ids):
        if store.get_address(aid) is None:
            raise ApiError(f"address {aid} not found", 404)
    for aid in add_ids:
        store.assign_address_to_person(aid, person_id)
    for aid in remove_ids:
        store.assign_address_to_person(aid, None)

    return jsonify(_person_detail(store, person_id))


@api_bp.delete("/persons/<int:person_id>")
def delete_person(person_id: int) -> Any:
    store = get_store()
    if not store.delete_person(person_id):
        raise ApiError("person not found", 404)
    return jsonify({"deleted": person_id})


# --- export / import ----------------------------------------------------------


def _export_slug(list_name: str | None) -> str:
    """A filename-safe slug for the export: the sanitized list name, or ``all``."""
    if list_name is None:
        return "all"
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", list_name)
    return slug or "list"


@api_bp.get("/export")
def export() -> Any:
    """Download a list's messages and pipeline state as a gzip JSON Lines export.

    Query param ``list`` (optional) names one list to export (an unknown name is a
    404); omitting it exports every list that has at least one message. When there
    is nothing to export — an empty database, or no list has any message — the
    response is a 404. The file is built via
    :func:`mailing_list_ai_check.export_import.export_lists` into a temporary
    ``.jsonl.gz`` file that is always removed before returning, and served as an
    ``application/gzip`` attachment named
    ``mlac-export-<slug>-<YYYYMMDD>.jsonl.gz``. A local database read only — no
    IMAP or Pangram calls, and message bodies are never logged.
    """
    store = get_store()
    list_name = request.args.get("list") or None

    fd, tmp_path = tempfile.mkstemp(suffix=".jsonl.gz")
    os.close(fd)
    try:
        try:
            if list_name is None:
                summary = export_lists(store, None, tmp_path, all_lists=True)
            else:
                summary = export_lists(store, [list_name], tmp_path)
        except ValueError as exc:
            # Unknown list name (the only ValueError export_lists raises for input).
            raise ApiError(str(exc), 404) from exc

        if summary.lists == 0:
            raise ApiError("nothing to export", 404)

        with open(tmp_path, "rb") as fh:
            data = fh.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass

    filename = f"mlac-export-{_export_slug(list_name)}-{datetime.now().strftime('%Y%m%d')}.jsonl.gz"
    return Response(
        data,
        mimetype="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@api_bp.post("/import")
def import_() -> Any:
    """Import an uploaded export file into the store (idempotent, all-or-nothing).

    Expects a multipart upload with the export in the ``file`` field (missing ⇒
    400). ``dry_run`` (query or form param, parsed like the other boolean params)
    validates and reports without writing. The upload is saved to a temporary file
    — preserving a ``.gz`` suffix so the importer's gzip sniffing works — passed to
    :func:`mailing_list_ai_check.export_import.import_file`, and the temp file is
    always removed. Returns the :class:`ImportSummary` fields plus ``"ok": true``;
    a malformed or corrupt file surfaces as a 400.
    """
    upload = request.files.get("file")
    if upload is None:
        raise ApiError("no file uploaded (expected multipart field 'file')")

    dry_run_raw = request.args.get("dry_run")
    if dry_run_raw is None:
        dry_run_raw = request.form.get("dry_run")
    dry_run = bool(_parse_bool("dry_run", dry_run_raw))

    suffix = ".jsonl.gz" if (upload.filename or "").endswith(".gz") else ".jsonl"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        upload.save(tmp_path)
        try:
            summary = import_file(get_store(), tmp_path, dry_run=dry_run)
        except ExportImportError as exc:
            raise ApiError(str(exc)) from exc
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass

    return jsonify({**asdict(summary), "ok": True})
