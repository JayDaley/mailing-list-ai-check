"""Separate Stylome scoring, current-result lookup and streaming JSONL export."""

from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TextIO

from email_reply_extractor import clean_for_scoring, split_html_parts

from .cli import refresh_parents
from .config import Config
from .store import Extraction, Store, _utcnow_iso, sha256_text
from .stylome import MODEL, OPERATING_POINTS, StylomeClient, StylomeError, normalize_text

log = logging.getLogger("mailing_list_ai_check.stylome")


def scoring_text(store: Store, extraction: Extraction) -> str:
    msg = store.get_message(extraction.message_id)
    hint = split_html_parts(msg.raw_html).signature_text if msg and msg.raw_html else None
    return normalize_text(clean_for_scoring(extraction.extracted_text, hint or None).text)


def eligible(text: str) -> bool:
    return len(text.split()) >= 50 and 200 <= len(text) <= 10_000


def current_scores(store: Store, extraction: Extraction) -> list[dict]:
    """Return only verdicts on the current, eligible normalized cleaned input."""
    msg = store.get_message(extraction.message_id)
    if extraction.status not in ("ok", "too_short") or not msg or msg.auto_generated is not None:
        return []
    text = scoring_text(store, extraction)
    if not eligible(text):
        return []
    rows = store.conn.execute(
        "SELECT model, operating_point, evaluation_version, label, confidence, "
        "text_sha256, scored_at FROM stylome_scores "
        "WHERE extraction_id=? AND text_sha256=? ORDER BY scored_at, id",
        (extraction.id, sha256_text(text)),
    )
    return [dict(row) for row in rows]


@dataclass
class StylomeSummary:
    scored: int = 0
    cache_hits: int = 0
    too_short: int = 0
    too_long: int = 0
    already_scored: int = 0
    api_calls: int = 0
    characters_sent: int = 0
    failed: int = 0


def run_stylome(
    store: Store,
    client: StylomeClient | None,
    *,
    operating_point: str,
    evaluation_version: str,
    limit: int = 10,
    dry_run: bool = False,
) -> StylomeSummary:
    """Score a capped queue, including extractions already scored by Pangram.

    Results are keyed by input, model, point and evaluation identity. Changes to
    cleaned text cannot reuse or display an old result. No Pangram score, status,
    fraction, part-check result or threshold is used to select a Stylome input.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if operating_point not in OPERATING_POINTS or not evaluation_version:
        raise ValueError("an operating point and evaluation version are required")
    if not dry_run and client is None:
        raise ValueError("a client is required outside dry-run mode")
    if client and (client.operating_point, client.evaluation_version) != (
        operating_point,
        evaluation_version,
    ):
        raise ValueError("client does not match the selected measurement contract")
    if not dry_run:
        refresh_parents(store, log)
    summary = StylomeSummary()
    # Keyset pagination avoids loading the corpus and avoids a long-lived read
    # cursor while inserts commit. Exclude automatic mail independently of score.
    last_id = 0
    pending_hashes: set[str] = set()
    while True:
        row = store.conn.execute(
            "SELECT e.* FROM extractions e JOIN messages m ON m.id=e.message_id "
            "WHERE e.id>? AND e.status IN ('ok','too_short') AND m.auto_generated IS NULL "
            "ORDER BY e.id LIMIT 1",
            (last_id,),
        ).fetchone()
        if row is None:
            break
        extraction = Extraction.from_row(row)
        last_id = extraction.id
        text = scoring_text(store, extraction)
        if len(text.split()) < 50 or len(text) < 200:
            summary.too_short += 1
            continue
        if len(text) > 10_000:
            summary.too_long += 1
            continue
        text_sha = sha256_text(text)
        cache = store.conn.execute(
            "SELECT * FROM stylome_scores WHERE model=? AND operating_point=? "
            "AND evaluation_version=? AND text_sha256=? ORDER BY id LIMIT 1",
            (MODEL, operating_point, evaluation_version, text_sha),
        ).fetchone()
        existing = store.conn.execute(
            "SELECT 1 FROM stylome_scores WHERE extraction_id=? AND model=? "
            "AND operating_point=? AND evaluation_version=? AND text_sha256=?",
            (extraction.id, MODEL, operating_point, evaluation_version, text_sha),
        ).fetchone()
        if existing:
            summary.already_scored += 1
            continue
        if cache or (dry_run and text_sha in pending_hashes):
            summary.cache_hits += 1
            if not dry_run:
                _save(
                    store,
                    extraction.id,
                    text,
                    json.loads(cache["response_json"]),
                    cache["scored_at"],
                )
            continue
        if summary.scored + summary.failed >= limit:
            continue
        summary.characters_sent += len(text)
        if dry_run:
            pending_hashes.add(text_sha)
            summary.scored += 1
            continue
        summary.api_calls += 1
        try:
            result = client.predict(text)
        except StylomeError as exc:
            summary.failed += 1
            log.error("Stylome stopped at extraction %d: %s", extraction.id, exc)
            break  # Stop on auth, balance, contract or ambiguous billing failure.
        _save(store, extraction.id, text, result.raw, _utcnow_iso())
        summary.scored += 1
    return summary


def _save(store: Store, extraction_id: int, text: str, response: dict, scored_at: str) -> None:
    store.conn.execute(
        "INSERT INTO stylome_scores(extraction_id, model, operating_point, "
        "evaluation_version, text_sha256, input_text, label, confidence, response_json, "
        "scored_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            extraction_id,
            response["model_version"],
            response["operating_point"],
            response["evaluation_version"],
            sha256_text(text),
            text,
            response["label"],
            response["confidence"],
            json.dumps(response, ensure_ascii=True),
            scored_at,
        ),
    )
    store.conn.commit()


def export_stylome(store: Store, output: TextIO) -> int:
    """Export retained scores with exact input and source keys, including stale rows.

    Every row says whether its input still matches the current extraction. No
    implicit label mapping, filtering by verdict, or aggregation is performed.
    """
    count = 0
    rows = store.conn.execute(
        "SELECT s.*, m.message_id, m.id AS message_pk, m.date, l.name AS list_name "
        "FROM stylome_scores s JOIN extractions e ON e.id=s.extraction_id "
        "JOIN messages m ON m.id=e.message_id JOIN lists l ON l.id=m.list_id ORDER BY s.id"
    )
    for row in rows:
        item = dict(row)
        extraction = store.get_extraction(item["extraction_id"])
        current = current_scores(store, extraction)
        item["current_input"] = any(
            (r["model"], r["operating_point"], r["evaluation_version"], r["text_sha256"])
            == (
                item["model"],
                item["operating_point"],
                item["evaluation_version"],
                item["text_sha256"],
            )
            for r in current
        )
        item["response"] = json.loads(item.pop("response_json"))
        item["any_ai"] = item["label"] != "human_written"
        output.write(json.dumps(item, ensure_ascii=True) + "\n")
        count += 1
    return count


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score with Stylome; Pangram data stays separate.")
    parser.add_argument("--db")
    parser.add_argument("--limit", type=int, default=10, help="maximum billed texts; default 10")
    parser.add_argument("--operating-point", choices=OPERATING_POINTS)
    parser.add_argument("--evaluation-version", help="require this API evaluation identity")
    parser.add_argument("--dry-run", action="store_true", help="no API calls or text changes")
    parser.add_argument("--export", type=Path, help="export retained results only; never score")
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be positive")
    config = Config.load()
    point = args.operating_point or config.stylome_operating_point
    evaluation = args.evaluation_version or config.stylome_evaluation_version
    if point not in OPERATING_POINTS or not evaluation:
        parser.error("invalid Stylome operating point or evaluation version")
    if args.export and args.dry_run:
        parser.error("--export and --dry-run cannot be combined")
    if not (args.dry_run or args.export) and not config.stylome_api_key:
        parser.error("STYLOME_API_KEY is not set")
    logging.basicConfig(level=getattr(logging, config.log_level.upper(), logging.INFO))
    with Store(args.db or config.database_path) as store:
        if args.export:
            # Publish only a complete export. link() refuses an existing target,
            # including one created by another process after the initial check.
            if args.export.exists():
                raise FileExistsError(args.export)
            partial = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=args.export.parent,
                    prefix=args.export.name + ".",
                    suffix=".partial",
                    delete=False,
                ) as output:
                    partial = Path(output.name)
                    count = export_stylome(store, output)
                os.link(partial, args.export)
            finally:
                if partial is not None:
                    partial.unlink(missing_ok=True)
            log.info("exported %d Stylome rows", count)
            return 0
        client = (
            None
            if args.dry_run
            else StylomeClient(
                config.stylome_api_key,
                operating_point=point,
                evaluation_version=evaluation,
            )
        )
        try:
            summary = run_stylome(
                store,
                client,
                operating_point=point,
                evaluation_version=evaluation,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        finally:
            if client:
                client.close()
    log.info("Stylome %s %s dry_run=%s: %s", MODEL, point, args.dry_run, asdict(summary))
    return 1 if summary.failed else 0
