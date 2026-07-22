"""``mail-ai-pull`` — command-line entry point for the IMAP fetcher.

Selects lists, a depth mode and optional sender filters, then pulls matching
mail into the SQLite store. Progress and the end-of-run summary go through
:mod:`logging` (level from ``LOG_LEVEL``); message bodies are never logged above
DEBUG.

Testing runs must stay within the project's hard cap — pass ``--limit 10``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from .cleaning import clean_for_scoring
from .config import Config
from .extraction import extract_new_text
from .fetcher import (
    DepthMode,
    FetchRequest,
    FetchSummary,
    open_client,
    parse_message,
    resolve_folders,
    run_fetch,
)
from .html_text import split_html_parts
from .imap_client import ImapClient
from .pangram import PangramClient, PangramError
from .store import Store, sha256_text

log = logging.getLogger("mailing_list_ai_check.pull")
extract_log = logging.getLogger("mailing_list_ai_check.extract")
score_log = logging.getLogger("mailing_list_ai_check.score")

#: Client-enforced reliability floor (words). Below it, extractions are marked
#: ``too_short`` and never sent to Pangram (see docs/findings/pangram.md).
SCORE_MIN_WORDS = 50
#: Default API-call cap per run. Deliberately small so an accidental run can't
#: spend; a production run must pass an explicit larger ``--limit``.
DEFAULT_SCORE_LIMIT = 10
#: Realtime Pangram price, for the end-of-run spend estimate ($/1000 words).
_PRICE_PER_1K_WORDS = 0.05


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-ai-pull",
        description="Fetch mailing-list mail over IMAP into the local store.",
    )
    parser.add_argument(
        "lists",
        nargs="*",
        help="one or more list names (e.g. 'announce' 'general'); omit with --all-lists",
    )
    parser.add_argument(
        "--all-lists",
        action="store_true",
        help="pull every list folder on the server (touches ~1374 folders)",
    )

    depth = parser.add_mutually_exclusive_group()
    depth.add_argument("--count", type=int, metavar="N", help="most recent N messages per list")
    depth.add_argument("--since", metavar="YYYY-MM-DD", help="messages on/after this date")
    depth.add_argument("--days", type=int, metavar="N", help="messages from the last N days")
    depth.add_argument(
        "--incremental",
        action="store_true",
        help="resume from the stored per-list cursor (UIDVALIDITY-aware)",
    )

    parser.add_argument(
        "--from",
        dest="from_filters",
        action="append",
        default=[],
        metavar="ADDR",
        help="server-side FROM substring filter; repeatable (values are OR-ed)",
    )
    parser.add_argument("--db", metavar="PATH", help="override the database path")
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="hard cap on messages fetched this run (use --limit 10 for testing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="search and report match counts without fetching or storing",
    )
    parser.add_argument(
        "--backfill-html",
        action="store_true",
        help=(
            "re-fetch already-stored messages that lack a raw_html part and fill "
            "it in (no normal pull); respects --limit as a per-run message cap"
        ),
    )
    parser.add_argument("--batch-size", type=int, default=200, help=argparse.SUPPRESS)
    return parser


def _resolve_depth(args: argparse.Namespace) -> DepthMode:
    """Turn parsed depth args into a :class:`DepthMode` (validated by caller)."""
    if args.incremental:
        return DepthMode(incremental=True)
    if args.since is not None:
        return DepthMode(since=args.since)
    if args.days is not None:
        from datetime import UTC, datetime, timedelta

        since = (datetime.now(UTC) - timedelta(days=args.days)).date().isoformat()
        return DepthMode(since=since)
    return DepthMode(count=args.count)


def _validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.all_lists and args.lists:
        parser.error("give either list names or --all-lists, not both")
    if not args.all_lists and not args.lists:
        parser.error("specify at least one list name, or --all-lists")

    depth_flags = [
        args.count is not None,
        args.since is not None,
        args.days is not None,
        args.incremental,
    ]
    # --backfill-html re-fetches stored messages rather than doing a depth-based
    # pull, so it takes no depth mode (and rejects one, to avoid confusion).
    if args.backfill_html:
        if sum(depth_flags) > 0:
            parser.error("--backfill-html does not take a depth mode (--count/--since/…)")
        if args.limit is not None and args.limit <= 0:
            parser.error("--limit must be a positive integer")
        return
    if sum(depth_flags) == 0:
        parser.error("choose a depth: --count, --since, --days, or --incremental")
    # --count/--days must be positive if given.
    if args.count is not None and args.count <= 0:
        parser.error("--count must be a positive integer")
    if args.days is not None and args.days <= 0:
        parser.error("--days must be a positive integer")
    if args.since is not None:
        from datetime import datetime

        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            parser.error("--since must be an ISO date (YYYY-MM-DD)")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be a positive integer")


def _log_summary(summary: FetchSummary, dry_run: bool) -> None:
    if dry_run:
        log.info("dry-run summary: %d message(s) would be fetched", summary.matched)
    else:
        log.info("summary: %s", summary.as_line())
    for name, count in sorted(summary.per_list.items()):
        log.info("  %s: %d", name, count)


#: Default per-run message cap for ``--backfill-html`` (the CLAUDE.md testing
#: cap). Production backfills pass a larger explicit ``--limit``.
DEFAULT_BACKFILL_LIMIT = 10


@dataclass
class BackfillSummary:
    """Tally of one ``--backfill-html`` run."""

    lists_processed: int = 0
    lists_skipped: int = 0
    fetched: int = 0
    html_found: int = 0
    html_missing: int = 0

    def as_line(self) -> str:
        return (
            f"lists_processed={self.lists_processed} lists_skipped={self.lists_skipped} "
            f"fetched={self.fetched} html_found={self.html_found} html_missing={self.html_missing}"
        )


def run_backfill_html(
    client: ImapClient,
    store: Store,
    folders: Sequence[str],
    *,
    limit: int = DEFAULT_BACKFILL_LIMIT,
    batch_size: int = 200,
) -> BackfillSummary:
    """Re-fetch stored messages missing ``raw_html`` and fill it in.

    For each folder: select it read-only and verify its UIDVALIDITY matches the
    stored ``pull_state`` (skip the list with a warning otherwise — a changed
    UIDVALIDITY means the stored UIDs no longer address the same messages). Then
    iterate :meth:`Store.iter_messages_missing_html`, UID-FETCH those messages in
    the usual batch size, parse each, and :meth:`Store.set_message_raw_html` with
    the HTML part when present or the empty-string tombstone when the message has
    none. Stamping the tombstone is what lets a capped run make forward progress:
    without it, HTML-less messages stay ``raw_html IS NULL`` and every run
    re-fetches the same ones forever, never reaching messages further up the UID
    order. ``raw_body`` and every other field are left untouched — history is
    never rewritten, only ``raw_html`` is added.

    ``limit`` is a hard cap on messages fetched across all folders this run
    (default the CLAUDE.md testing cap); production runs pass a larger value.
    """
    from .fetcher import list_name_for_folder

    summary = BackfillSummary()
    remaining = limit

    for folder in folders:
        name = list_name_for_folder(folder)
        if remaining <= 0:
            log.info("backfill message cap reached; skipping %s", name)
            break

        mlist = store.upsert_list(name, folder)
        status = client.examine(folder)
        cursor = store.get_pull_state(mlist.id)
        if cursor is None or cursor.uidvalidity != status.uidvalidity:
            log.warning(
                "skipping %s: UIDVALIDITY %s does not match stored cursor %s",
                name,
                status.uidvalidity,
                cursor.uidvalidity if cursor else "<none>",
            )
            summary.lists_skipped += 1
            continue

        # Messages needing a backfill, in UID order, capped to what's left.
        pending = list(store.iter_messages_missing_html(mlist.id))[:remaining]
        by_uid = {m.uid: m.id for m in pending if m.uid is not None}
        if not by_uid:
            summary.lists_processed += 1
            continue

        for uid, raw in client.fetch_bodies(sorted(by_uid), batch_size=batch_size):
            message_pk = by_uid.get(uid)
            if message_pk is None:
                continue
            try:
                parsed = parse_message(raw, uid=uid, folder=folder)
            except Exception:
                log.warning("parse error backfilling %s uid=%s", name, uid)
                continue
            summary.fetched += 1
            remaining -= 1
            if parsed.html_body:
                store.set_message_raw_html(message_pk, parsed.html_body)
                summary.html_found += 1
            else:
                # Tombstone: the message genuinely has no HTML part. Stamp an
                # empty string ("checked, none present") so it is not re-fetched
                # every run — iter_messages_missing_html only returns raw_html
                # IS NULL rows, so '' drops out of the backfill queue and the
                # run makes forward progress. Downstream, '' is falsy exactly
                # like NULL (no HTML oracle / signature hint), so the sentinel
                # changes only the backfill queue, never extraction/scoring.
                store.set_message_raw_html(message_pk, "")
                summary.html_missing += 1

        summary.lists_processed += 1

    log.info("backfill summary: %s", summary.as_line())
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate(args, parser)

    config = Config.load()
    if args.db:
        db_path = args.db
    else:
        db_path = config.database_path

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.backfill_html:
        limit = args.limit if args.limit is not None else DEFAULT_BACKFILL_LIMIT
        client = open_client(
            config.imap_host, config.imap_port, config.imap_username, config.imap_password
        )
        try:
            folders = resolve_folders(client, args.lists, all_lists=args.all_lists)
            with Store(db_path) as store:
                run_backfill_html(client, store, folders, limit=limit, batch_size=args.batch_size)
        finally:
            client.close()
            client.logout()
        return 0

    depth = _resolve_depth(args)

    client = open_client(
        config.imap_host, config.imap_port, config.imap_username, config.imap_password
    )
    try:
        folders = resolve_folders(client, args.lists, all_lists=args.all_lists)
        request = FetchRequest(
            folders=tuple(folders),
            depth=depth,
            from_filters=tuple(args.from_filters),
            limit=args.limit,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
        )
        with Store(db_path) as store:
            summary = run_fetch(client, store, request)
    finally:
        client.close()
        client.logout()

    _log_summary(summary, args.dry_run)
    return 0


# --- extraction command -------------------------------------------------------


def build_extract_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-ai-extract",
        description=(
            "Extract each author's newly written text from stored messages "
            "(email-reply-parser + custom cleanup). Idempotent: only messages "
            "without an extraction row are processed."
        ),
    )
    parser.add_argument("--db", metavar="PATH", help="override the database path")
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="stop after processing N messages this run",
    )
    return parser


def run_extract(store: Store, limit: int | None = None) -> tuple[Counter[str], Counter[str]]:
    """Extract text for every message lacking an extraction row.

    Returns ``(status_counts, method_counts)``. Idempotent — a second run over an
    already-extracted store processes nothing. ``limit`` caps messages per run.
    """
    status_counts: Counter[str] = Counter()
    method_counts: Counter[str] = Counter()
    processed = 0
    for message in store.iter_messages_without_extraction():
        if limit is not None and processed >= limit:
            break
        parent_body = (
            store.get_parent_body(message.in_reply_to, exclude_message_id=message.message_id)
            if message.in_reply_to
            else None
        )
        result = extract_new_text(message.raw_body, parent_body, html_body=message.raw_html)
        store.insert_extraction(
            message_id=message.id,
            extracted_text=result.text,
            method=result.method,
            status=result.status,
        )
        status_counts[result.status] += 1
        method_counts[result.method] += 1
        processed += 1
        extract_log.debug(
            "extracted message id=%s status=%s method=%s chars=%d",
            message.id,
            result.status,
            result.method,
            len(result.text),
        )
    return status_counts, method_counts


def extract_main(argv: Sequence[str] | None = None) -> int:
    parser = build_extract_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be a positive integer")

    config = Config.load()
    db_path = args.db or config.database_path

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with Store(db_path) as store:
        status_counts, method_counts = run_extract(store, limit=args.limit)

    total = sum(status_counts.values())
    extract_log.info(
        "summary: processed=%d extracted=%d empty=%d failed=%d",
        total,
        status_counts.get("ok", 0),
        status_counts.get("empty", 0),
        status_counts.get("failed", 0),
    )
    for method, count in sorted(method_counts.items()):
        extract_log.info("  method %s: %d", method, count)
    return 0


# --- scoring command ----------------------------------------------------------


@dataclass
class ScoreSummary:
    """Tally of one ``mail-ai-score`` run."""

    scored: int = 0
    cache_hits: int = 0
    too_short: int = 0
    failed: int = 0
    api_calls: int = 0
    words_sent: int = 0

    @property
    def estimated_spend(self) -> float:
        """Rough realtime cost of the words actually sent this run (USD)."""
        return self.words_sent / 1000 * _PRICE_PER_1K_WORDS

    def as_line(self) -> str:
        return (
            f"scored={self.scored} cache_hits={self.cache_hits} "
            f"too_short={self.too_short} failed={self.failed} "
            f"api_calls={self.api_calls} words_sent={self.words_sent} "
            f"est_spend=${self.estimated_spend:.4f}"
        )


def build_score_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-ai-score",
        description=(
            "Score each unscored, long-enough extraction for AI-generated content "
            "with Pangram. Each extraction is first cleaned for scoring (greetings, "
            "sign-offs and signatures removed); extractions whose cleaned text is "
            "under the 50-word reliability floor are marked 'too_short' and never "
            "sent; identical cleaned text is served from the score cache without an "
            "API call. Idempotent."
        ),
    )
    parser.add_argument("--db", metavar="PATH", help="override the database path")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_SCORE_LIMIT,
        metavar="N",
        help=(
            f"cap on Pangram API calls this run (cache hits are free and uncapped); "
            f"default {DEFAULT_SCORE_LIMIT}. Pass a larger value for production runs."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be scored / gated / cache-hit without calling the API",
    )
    return parser


def _html_signature_hint(store: Store, message_pk: int) -> str | None:
    """Return the HTML signature-container text for a message, or ``None``.

    Resolves the message for an extraction and, when it has a stored ``raw_html``,
    returns :func:`~html_text.split_html_parts`'s ``signature_text`` (a stage-2
    cleaning hint). ``None`` when the message is gone or has no HTML part.
    """
    msg = store.get_message(message_pk)
    if msg is None or not msg.raw_html:
        return None
    return split_html_parts(msg.raw_html).signature_text or None


def run_score(
    store: Store,
    client: PangramClient | None,
    *,
    limit: int = DEFAULT_SCORE_LIMIT,
    min_words: int = SCORE_MIN_WORDS,
    dry_run: bool = False,
) -> ScoreSummary:
    """Score unscored extractions, capping real API calls at ``limit``.

    Each ok extraction is first run through :func:`cleaning.clean_for_scoring`
    (with the message's HTML signature hint when it has stored ``raw_html``);
    the ``min_words`` floor, the score-cache key and the text sent to Pangram all
    use that **cleaned** text (not the raw stage-1 extraction). Marks
    sub-``min_words`` (cleaned) extractions ``too_short``; serves identical
    cleaned text from the score cache (uncapped); scores the rest via ``client``
    up to ``limit`` calls. Idempotent — a re-run over a fully scored/gated store
    does nothing. ``client`` may be ``None`` only in ``dry_run`` mode.
    """
    summary = ScoreSummary()

    # One pass over every unscored ok extraction (min_words=0 yields all); the
    # short-text gate is applied here on the *cleaned* word count so it never
    # depends on the raw extraction length. Cache first, API second (capped).
    for extraction in store.iter_extractions_needing_score(min_words=0):
        # Compute the HTML signature hint once per extraction and clean with it,
        # so the reliability floor, cache key and scored text all reflect it.
        hint = _html_signature_hint(store, extraction.message_id)
        text = clean_for_scoring(extraction.extracted_text, hint).text
        word_count = len(text.split())

        # Gate short cleaned text (never sent). In dry-run, only count.
        if word_count < min_words:
            summary.too_short += 1
            if not dry_run:
                store.update_extraction_status(extraction.id, "too_short")
            continue

        cached = store.find_score_by_text_sha256(sha256_text(text))
        if cached is not None:
            summary.cache_hits += 1
            if not dry_run:
                store.insert_score(
                    extraction_id=extraction.id,
                    text_sha256=cached.text_sha256,
                    fraction_ai=cached.fraction_ai,
                    fraction_ai_assisted=cached.fraction_ai_assisted,
                    fraction_human=cached.fraction_human,
                    label=cached.label,
                    detector_version=cached.detector_version,
                    raw_response=cached.raw_response,
                )
            continue

        # Needs a real API call — respect the per-run cap (leaves the rest for
        # a later run so scoring resumes cleanly).
        attempts = summary.scored + summary.failed
        if attempts >= limit:
            continue
        summary.words_sent += word_count

        if dry_run:
            summary.scored += 1
            continue

        assert client is not None  # guaranteed by score_main outside dry-run
        summary.api_calls += 1
        try:
            result = client.predict(text)
        except PangramError as exc:
            summary.failed += 1
            score_log.warning("scoring failed for extraction id=%s: %s", extraction.id, exc)
            continue
        store.insert_score(
            extraction_id=extraction.id,
            text_sha256=sha256_text(text),
            fraction_ai=result.fraction_ai,
            fraction_ai_assisted=result.fraction_ai_assisted,
            fraction_human=result.fraction_human,
            label=result.label,
            detector_version=result.version,
            raw_response=result.raw,
        )
        summary.scored += 1
        score_log.debug(
            "scored extraction id=%s label=%s fraction_ai=%s",
            extraction.id,
            result.label,
            result.fraction_ai,
        )

    return summary


def score_main(argv: Sequence[str] | None = None) -> int:
    parser = build_score_parser()
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be a positive integer")

    config = Config.load()
    db_path = args.db or config.database_path

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.dry_run and not config.pangram_api_key:
        parser.error(
            "PANGRAM_API_KEY is not set. Add it to .env (see .env.example) to score messages;"
            " pulling and extraction work without it."
        )
    client = None if args.dry_run else PangramClient(config.pangram_api_key)
    with Store(db_path) as store:
        summary = run_score(
            store,
            client,
            limit=args.limit,
            min_words=SCORE_MIN_WORDS,
            dry_run=args.dry_run,
        )

    prefix = "dry-run summary" if args.dry_run else "summary"
    score_log.info("%s: %s", prefix, summary.as_line())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
