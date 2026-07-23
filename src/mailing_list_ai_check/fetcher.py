"""Fetch orchestration: IMAP selection → parsed messages → the SQLite store.

Resolves a user's list/depth/sender selection into per-folder UID sets, fetches
them in batches over :class:`~mailing_list_ai_check.imap_client.ImapClient`, parses
each RFC 5322 message with the stdlib :mod:`email` package (``policy=default``,
so RFC 2047 headers decode automatically), and upserts the result through
:class:`~mailing_list_ai_check.store.Store`.

Every stage is idempotent: re-pulling a message is a no-op (dedupe on
``(list_id, message_id)``), and the per-folder ``(uidvalidity, last_uid)`` cursor
lets ``--incremental`` resume.

HTML-only messages
------------------
When a message has no ``text/plain`` part (HTML only), we **store the row with an
empty ``raw_body``** (``raw_body = None``) — but we now also capture the decoded
``text/html`` part into ``raw_html`` whenever one is present (HTML-only or
alongside a plain part). The HTML gives the extraction pipeline a structural
oracle (see :mod:`mailing_list_ai_check.html_text`): it can recover novel text from
HTML-only messages and use ``<blockquote>``/Gmail/Outlook quote containers as
evidence for what is quoted. HTML-only rows are still counted separately
(``html_only``) in the run summary so they are visible, not silently dropped.
"""

from __future__ import annotations

import email
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

from .imap_client import DEFAULT_BATCH_SIZE, FOLDER_PREFIX, ImapClient, build_search_criteria
from .store import Store

log = logging.getLogger(__name__)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --- selection / depth --------------------------------------------------------


@dataclass(frozen=True)
class DepthMode:
    """How much of each folder to pull. Exactly one field is meaningful.

    - ``count``: the most recent N messages (UID slice from the top).
    - ``since``: server-side ``SINCE`` from an ISO ``YYYY-MM-DD`` date.
    - ``incremental``: resume from the stored ``pull_state`` cursor.
    """

    count: int | None = None
    since: str | None = None
    incremental: bool = False


@dataclass(frozen=True)
class FetchRequest:
    """A fully-resolved fetch request."""

    folders: tuple[str, ...]
    depth: DepthMode
    from_filters: tuple[str, ...] = ()
    limit: int | None = None
    dry_run: bool = False
    batch_size: int = 200


@dataclass
class FetchSummary:
    """Counts collected across a run."""

    fetched: int = 0
    duplicates: int = 0
    parse_errors: int = 0
    html_only: int = 0
    matched: int = 0
    per_list: dict[str, int] = field(default_factory=dict)

    def as_line(self) -> str:
        return (
            f"fetched={self.fetched} duplicates={self.duplicates} "
            f"parse_errors={self.parse_errors} html_only={self.html_only} "
            f"matched={self.matched}"
        )


# --- parsing ------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedMessage:
    """The fields we persist from one RFC 5322 message.

    ``body`` is the decoded ``text/plain`` part (``None`` when HTML-only) and
    ``html_body`` the decoded ``text/html`` part (``None`` when absent). Both are
    captured with the same charset-fallback handling; ``html_only`` stays true
    only when there is no plain part.
    """

    message_id: str
    from_email: str
    from_name: str | None
    subject: str | None
    date: str | None
    in_reply_to: str | None
    body: str | None
    html_only: bool
    html_body: str | None = None


@dataclass(frozen=True)
class ParsedHeader:
    """The header-only fields a message-list preview shows for a candidate message.

    The read-only preview path (see the dashboard's "Add messages" popover) fetches
    only ``From``/``Subject``/``Date`` and never a body, so this is the subset of
    :class:`ParsedMessage` derivable from those headers alone.
    """

    from_email: str
    from_name: str | None
    subject: str | None
    date: str | None


def iso_to_imap_date(iso_date: str) -> str:
    """Convert ``YYYY-MM-DD`` to IMAP's ``DD-Mon-YYYY`` (for ``SINCE``)."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year}"


def _header_str(msg: EmailMessage, name: str) -> str | None:
    value = msg[name]
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decode_part(part: EmailMessage) -> str | None:
    """Decode one MIME part to text, falling back on a lenient UTF-8 decode.

    Mirrors the charset handling used for the plain part: an unknown or broken
    charset never raises — the raw payload is decoded with ``errors="replace"``.
    Returns ``None`` for an empty part.
    """
    try:
        content = part.get_content()
    except (LookupError, ValueError, UnicodeDecodeError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace")
    return content if content else None


def _extract_body(msg: EmailMessage) -> tuple[str | None, bool, str | None]:
    """Return ``(text_plain_body, html_only, text_html_body)``.

    Prefers a ``text/plain`` part for ``body``. The ``text/html`` part, if any,
    is decoded into the third element regardless of whether a plain part exists.
    ``html_only`` stays true only when there is no plain part (see the module
    docstring); it does not change just because the HTML is now captured.
    """
    html_part = msg.get_body(preferencelist=("html",))
    html_body = _decode_part(html_part) if html_part is not None else None

    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        return _decode_part(plain), False, html_body

    return None, (html_part is not None), html_body


def _date_header_to_iso(msg: EmailMessage) -> str | None:
    """Parse a message's ``Date`` header to a UTC ISO-8601 string, or ``None``.

    Shared by :func:`parse_message` and :func:`parse_header` so a preview's date
    matches exactly what a full pull would store. Prefers the ``policy=default``
    header's parsed ``datetime`` and falls back to :func:`parsedate_to_datetime`;
    a naive (offset-less) datetime is assumed to be UTC. Returns ``None`` for a
    missing or unparsable header.
    """
    date_hdr = msg["Date"]
    if date_hdr is None:
        return None
    dt = None
    try:
        dt = date_hdr.datetime  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        dt = None
    if dt is None:
        try:
            dt = parsedate_to_datetime(str(date_hdr))
        except (TypeError, ValueError):
            dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def parse_message(raw: bytes, *, uid: int | None = None, folder: str = "") -> ParsedMessage:
    """Parse raw RFC 5322 bytes into a :class:`ParsedMessage`.

    ``policy=default`` decodes RFC 2047 words in headers. The ``From`` address is
    lowercased and stripped; a missing ``Message-ID`` is synthesized from the UID
    so the row still has a stable dedupe key.
    """
    msg = email.message_from_bytes(raw, policy=policy.default)

    message_id = _header_str(msg, "Message-ID") or ""
    if not message_id:
        message_id = f"<no-message-id-{folder}-{uid}@mailing-list-ai-check>"

    display_name, addr = parseaddr(str(msg["From"] or ""))
    from_email = addr.strip().lower()
    from_name = display_name.strip() or None

    body, html_only, html_body = _extract_body(msg)

    return ParsedMessage(
        message_id=message_id,
        from_email=from_email,
        from_name=from_name,
        subject=_header_str(msg, "Subject"),
        date=_date_header_to_iso(msg),
        in_reply_to=_header_str(msg, "In-Reply-To"),
        body=body,
        html_only=html_only,
        html_body=html_body,
    )


def parse_header(raw: bytes) -> ParsedHeader:
    """Parse a header-only FETCH blob (``FROM``/``SUBJECT``/``DATE``) into a header.

    Uses the same stdlib :mod:`email` parser and ``policy=default`` as
    :func:`parse_message`, so the sender, subject and normalized UTC date a
    preview shows match exactly what a full pull would persist. The ``From``
    address is lowercased and stripped identically. No body is present or parsed.
    """
    msg = email.message_from_bytes(raw, policy=policy.default)
    display_name, addr = parseaddr(str(msg["From"] or ""))
    return ParsedHeader(
        from_email=addr.strip().lower(),
        from_name=display_name.strip() or None,
        subject=_header_str(msg, "Subject"),
        date=_date_header_to_iso(msg),
    )


# --- folder resolution --------------------------------------------------------


def folder_for_list(list_name: str) -> str:
    """Map a bare list slug (or an already-prefixed name) to its IMAP folder."""
    return list_name if list_name.startswith(FOLDER_PREFIX) else FOLDER_PREFIX + list_name


def list_name_for_folder(folder: str) -> str:
    """Inverse of :func:`folder_for_list` — strip the namespace prefix."""
    return folder[len(FOLDER_PREFIX) :] if folder.startswith(FOLDER_PREFIX) else folder


def resolve_folders(
    client: ImapClient, list_names: Sequence[str], *, all_lists: bool = False
) -> list[str]:
    """Resolve a selection into concrete folder names.

    ``all_lists`` enumerates the server; otherwise each name is mapped through
    :func:`folder_for_list`.
    """
    if all_lists:
        return client.list_folders()
    return [folder_for_list(name) for name in list_names]


def refresh_lists_index(client: ImapClient, store: Store) -> dict[str, int]:
    """Populate/refresh the stored lists index from the server's ``LIST`` output.

    The ``LIST`` enumeration is one IMAP round-trip; reconciliation semantics
    (what is added, deleted, or kept-but-stamped) live in
    :meth:`~mailing_list_ai_check.store.Store.refresh_lists_index`.

    After reconciling, the newest-message timestamp is refreshed for **tracked**
    lists only — those with local messages and still on the server (see
    :meth:`~mailing_list_ai_check.store.Store.tracked_list_folders`), so the
    ~1,400 index-only folders are never EXAMINEd. Each check is one more
    round-trip; an empty folder (``None``) still counts as ``activity_checked``,
    while an exception is logged, counted as ``activity_failed`` and never aborts
    the sweep.
    """
    folders = client.list_folders()
    entries = [(list_name_for_folder(folder), folder) for folder in folders]
    counts = store.refresh_lists_index(entries)

    activity_checked = 0
    activity_failed = 0
    for list_id, folder in store.tracked_list_folders():
        try:
            when = client.last_message_internaldate(folder)
        except Exception:
            activity_failed += 1
            log.warning("activity check failed for %s", folder)
            continue
        store.set_list_last_message(list_id, when)
        activity_checked += 1
    counts["activity_checked"] = activity_checked
    counts["activity_failed"] = activity_failed

    log.info(
        "lists index refreshed: total=%d added=%d restored=%d deleted=%d kept_missing=%d "
        "activity_checked=%d activity_failed=%d",
        counts["total"],
        counts["added"],
        counts["restored"],
        counts["deleted"],
        counts["kept_missing"],
        counts["activity_checked"],
        counts["activity_failed"],
    )
    return counts


# --- UID computation ----------------------------------------------------------


def _union_search(
    client: ImapClient,
    *,
    since: str | None,
    uid_range: str | None,
    from_filters: Sequence[str],
) -> list[int]:
    """Run one search per ``FROM`` filter and return the deduped, sorted union.

    With no filters a single search runs. Multiple ``--from`` values are a union
    of independent server-side searches (findings: ``FROM`` is a substring match).
    """
    if not from_filters:
        return client.uid_search(build_search_criteria(since=since, uid_range=uid_range))
    seen: set[int] = set()
    for term in from_filters:
        criteria = build_search_criteria(since=since, uid_range=uid_range, from_addr=term)
        seen.update(client.uid_search(criteria))
    return sorted(seen)


def compute_uids(
    client: ImapClient,
    store: Store,
    folder: str,
    list_id: int,
    depth: DepthMode,
    from_filters: Sequence[str],
) -> tuple[list[int], int]:
    """Compute the UID set to fetch for ``folder`` and the folder's UIDVALIDITY.

    Handles the three depth modes, including the documented UIDVALIDITY-change
    resync for ``--incremental``.
    """
    status = client.examine(folder)
    uidvalidity = status.uidvalidity

    if depth.incremental:
        cursor = store.get_pull_state(list_id)
        if cursor is not None and cursor.uidvalidity != uidvalidity:
            # Documented resync path: the folder was reset. Re-search from the
            # last successful sync date and rewrite the cursor afterwards.
            mlist = store.get_list(list_id)
            since_iso = (mlist.last_synced_at or "")[:10] if mlist else ""
            log.warning(
                "UIDVALIDITY changed for %s (stored=%s server=%s); resyncing via SINCE %s",
                folder,
                cursor.uidvalidity,
                uidvalidity,
                since_iso or "<none>",
            )
            since = iso_to_imap_date(since_iso) if since_iso else None
            uids = _union_search(client, since=since, uid_range=None, from_filters=from_filters)
        else:
            last_uid = cursor.last_uid if cursor else 0
            uid_range = f"{last_uid + 1}:*"
            uids = _union_search(client, since=None, uid_range=uid_range, from_filters=from_filters)
            # `n:*` can echo the highest UID when n exceeds it; drop stale ones.
            uids = [u for u in uids if u > last_uid]
        return uids, uidvalidity

    if depth.since is not None:
        since = iso_to_imap_date(depth.since)
        uids = _union_search(client, since=since, uid_range=None, from_filters=from_filters)
        return uids, uidvalidity

    # --count N: most recent N via a UID slice from the top.
    uids = _union_search(client, since=None, uid_range=None, from_filters=from_filters)
    if depth.count is not None:
        uids = uids[-depth.count :] if depth.count > 0 else []
    return uids, uidvalidity


# --- run ----------------------------------------------------------------------


def run_fetch(client: ImapClient, store: Store, request: FetchRequest) -> FetchSummary:
    """Execute a fetch request, returning a :class:`FetchSummary`.

    Respects ``request.limit`` as a hard global message cap across all folders
    (the safety valve for testing) and ``request.dry_run`` (search + count only).
    """
    summary = FetchSummary()
    remaining = request.limit

    for folder in request.folders:
        name = list_name_for_folder(folder)
        if remaining is not None and remaining <= 0:
            log.info("global limit reached; skipping %s", name)
            break

        mlist = store.upsert_list(name, folder)
        try:
            uids, uidvalidity = compute_uids(
                client, store, folder, mlist.id, request.depth, request.from_filters
            )
        except Exception:
            log.exception("failed to compute UID set for %s", name)
            continue

        summary.matched += len(uids)
        if request.dry_run:
            log.info("[dry-run] %s: %d message(s) match", name, len(uids))
            summary.per_list[name] = len(uids)
            continue

        if remaining is not None:
            uids = uids[:remaining] if remaining < len(uids) else uids

        list_count = _fetch_folder(
            client, store, mlist.id, folder, uids, request.batch_size, summary
        )
        summary.per_list[name] = list_count

        if uids:
            store.set_pull_state(mlist.id, uidvalidity, max(uids))
        store.set_list_synced(mlist.id)

        # Record when the server last saw traffic on this list. A failure here
        # must never fail the pull, so it is logged and swallowed.
        try:
            when = client.last_message_internaldate(folder)
            if when is not None:
                store.set_list_last_message(mlist.id, when)
        except Exception:
            log.warning("activity check failed for %s", name)

        if remaining is not None:
            remaining -= list_count

    return summary


def _fetch_folder(
    client: ImapClient,
    store: Store,
    list_id: int,
    folder: str,
    uids: Sequence[int],
    batch_size: int,
    summary: FetchSummary,
) -> int:
    """Fetch, parse and upsert ``uids`` from ``folder``. Returns rows fetched."""
    name = list_name_for_folder(folder)
    fetched_here = 0
    for uid, raw in client.fetch_bodies(uids, batch_size=batch_size):
        try:
            parsed = parse_message(raw, uid=uid, folder=folder)
        except Exception:
            summary.parse_errors += 1
            log.warning("parse error for %s uid=%s", name, uid)
            continue

        address_id: int | None = None
        if parsed.from_email:
            address_id = store.upsert_address(parsed.from_email, parsed.from_name).id

        result = store.upsert_message(
            message_id=parsed.message_id,
            list_id=list_id,
            address_id=address_id,
            subject=parsed.subject,
            date=parsed.date,
            in_reply_to=parsed.in_reply_to,
            raw_body=parsed.body,
            uid=uid,
            raw_html=parsed.html_body,
        )
        if result.inserted:
            summary.fetched += 1
            fetched_here += 1
            if parsed.html_only:
                summary.html_only += 1
            # Never log body content; a length is safe only at DEBUG.
            log.debug("stored %s uid=%s body_chars=%s", name, uid, len(parsed.body or ""))
        else:
            summary.duplicates += 1
    log.info("%s: %d fetched", name, fetched_here)
    return fetched_here


def run_fetch_uids(
    client: ImapClient,
    store: Store,
    folder: str,
    uids: Sequence[int],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> FetchSummary:
    """Fetch, parse and upsert an explicit, pre-computed UID set for one folder.

    A thin wrapper over :func:`_fetch_folder` for callers that have already
    resolved the exact UIDs to pull (the dashboard's ranged "new"/"before" pull),
    rather than going through :func:`compute_uids`' depth modes. The list row is
    upserted from ``folder`` (created if new, like :func:`run_fetch`), the bodies
    are fetched with ``BODY.PEEK[]`` and upserted idempotently, and a
    :class:`FetchSummary` is returned with ``matched`` set to ``len(uids)``.

    Cursor (``pull_state``), ``last_synced_at`` and ``last_message_at``
    bookkeeping are intentionally left to the caller, because whether the
    incremental cursor may advance depends on the pull direction (a "before" pull
    must never move it) — see the webapp's ``/api/pull/range`` endpoint.
    """
    name = list_name_for_folder(folder)
    mlist = store.upsert_list(name, folder)
    # A UID FETCH requires the mailbox to be selected; EXAMINE keeps it read-only
    # and makes this wrapper self-contained regardless of what the caller selected.
    client.examine(folder)
    summary = FetchSummary()
    summary.matched = len(uids)
    fetched = _fetch_folder(client, store, mlist.id, folder, uids, batch_size, summary)
    summary.per_list[name] = fetched
    return summary


def open_client(host: str, port: int, username: str, password: str) -> ImapClient:
    """Convenience: open and log in an :class:`ImapClient` from config values."""
    if not host:
        raise RuntimeError(
            "IMAP_HOST is not set. Copy .env.example to .env and fill in the "
            "IMAP settings for the mail archive you want to check."
        )
    return ImapClient.connect(host, port, username, password)
