"""Detection and repair of extractions derived by an older extraction routine.

A change to extraction or post-extraction processing is a minor version bump, and
every ``extractions`` row records the app version that produced its text
(``extractions.pipeline_version``). Comparing that stamp's extraction generation —
its ``(major, minor)`` pair — with the running version is therefore a cheap test
for "this text may not be what the current routine would produce".
:func:`check` performs exactly that test and nothing else; it is what the
dashboard runs at start-up.

The version stamp decides only whether to offer the check, never the answer.
:func:`diff` re-derives every stored extraction with the current routine and
compares the result against what is stored, so the affected set is measured
rather than inferred. Rows that come out identical are stamped with the running
version — they are provably current whatever their old stamp said — and drop out
of the report, which is also what stops a store with no real differences from
being reported stale again on the next start-up. :func:`reextract` then rewrites
only the rows the caller passes in.

Two texts are compared per message, because either can move independently:

- the extracted text, which is what the dashboard displays; and
- the cleaned text (:func:`~mailing_list_ai_check.cleaning.clean_for_scoring`),
  which is what a score is a verdict on.

A changed cleaned text is what invalidates a score, so :func:`reextract` deletes
the score row in that case and reports the message as needing a re-score. A
message whose extracted text changed but whose cleaned text did not keeps its
verdict, and costs nothing.

Nothing in this module calls IMAP or Pangram: it re-runs local extraction and
cleaning only. Re-scoring the rewritten rows is the caller's separate, paid step
(:func:`mailing_list_ai_check.cli.run_score`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from . import __version__
from .cleaning import clean_for_scoring
from .extraction import extract_new_text
from .html_text import split_html_parts
from .store import Extraction, Message, Store, extraction_generation

# --- reports ------------------------------------------------------------------


@dataclass(frozen=True)
class VersionCount:
    """How many extractions carry one ``pipeline_version``, and whether it is old."""

    version: str | None
    count: int
    stale: bool


@dataclass(frozen=True)
class StalenessReport:
    """The result of :func:`check` — a version comparison, no text re-derived."""

    app_version: str
    stale: bool
    stale_count: int
    current_count: int
    total: int
    versions: list[VersionCount] = field(default_factory=list)


@dataclass(frozen=True)
class ExtractionDiff:
    """One message whose stored extraction differs from a fresh re-derivation."""

    message_id: int
    extraction_id: int
    list_name: str | None
    date: str | None
    subject: str | None
    from_address: str | None
    from_display_name: str | None
    pipeline_version: str | None
    old_chars: int
    new_chars: int
    old_status: str
    new_status: str
    text_changed: bool
    scored_text_changed: bool
    scored: bool


@dataclass(frozen=True)
class DiffReport:
    """The result of :func:`diff`: what was checked, and what differs."""

    app_version: str
    checked: int
    unchanged: int
    stamped: int
    differing: list[ExtractionDiff] = field(default_factory=list)


@dataclass
class ReextractSummary:
    """Tally of one :func:`reextract` run.

    ``rescore_message_ids`` are the messages whose extraction now has no score
    and an ``ok`` status — the set worth handing to the scoring stage.
    """

    processed: int = 0
    rewritten: int = 0
    unchanged: int = 0
    not_ok: int = 0
    scores_invalidated: int = 0
    rescore_message_ids: list[int] = field(default_factory=list)


# --- shared re-derivation -----------------------------------------------------


@dataclass(frozen=True)
class _Rederived:
    """A fresh extraction for one message, next to what is stored for it."""

    extraction: Extraction
    text: str
    method: str
    status: str
    scored_text: str
    stored_scored_text: str

    @property
    def text_changed(self) -> bool:
        return self.text != self.extraction.extracted_text

    @property
    def scored_text_changed(self) -> bool:
        return self.scored_text != self.stored_scored_text

    @property
    def changed(self) -> bool:
        return self.text_changed or self.scored_text_changed


def _signature_hint(message: Message) -> str | None:
    """The HTML signature-container text for a message, or ``None``.

    The same stage-2 cleaning hint :func:`mailing_list_ai_check.cli.run_score`
    passes, so a cleaned text computed here matches the one that would be sent to
    Pangram.
    """
    if not message.raw_html:
        return None
    return split_html_parts(message.raw_html).signature_text or None


def _rederive(store: Store, message: Message, extraction: Extraction) -> _Rederived:
    """Re-run extraction and cleaning for one message; write nothing.

    Mirrors :func:`mailing_list_ai_check.cli.run_extract`'s call exactly —
    including the thread-parent body resolved from ``In-Reply-To`` — so the text
    is what a fresh pipeline run would store.
    """
    parent_body = (
        store.get_parent_body(message.in_reply_to, exclude_message_id=message.message_id)
        if message.in_reply_to
        else None
    )
    result = extract_new_text(message.raw_body, parent_body, html_body=message.raw_html)
    hint = _signature_hint(message)
    return _Rederived(
        extraction=extraction,
        text=result.text,
        method=result.method,
        status=result.status,
        scored_text=clean_for_scoring(result.text, hint).text,
        stored_scored_text=clean_for_scoring(extraction.extracted_text, hint).text,
    )


# --- the three operations -----------------------------------------------------


def check(store: Store) -> StalenessReport:
    """Report whether any stored extraction predates the current routine.

    A version comparison over one grouped query — no message is read and no text
    is re-derived, so this is cheap enough to run on every dashboard load. An
    extraction is counted stale when its ``pipeline_version`` belongs to an older
    extraction generation than the running version (see
    :func:`~mailing_list_ai_check.store.extraction_generation`); a store with no
    extractions at all is never stale.
    """
    current = extraction_generation(__version__)
    versions: list[VersionCount] = []
    stale_count = 0
    current_count = 0
    for version, count in store.extraction_version_counts():
        is_stale = extraction_generation(version) < current
        versions.append(VersionCount(version=version, count=count, stale=is_stale))
        if is_stale:
            stale_count += count
        else:
            current_count += count
    return StalenessReport(
        app_version=__version__,
        stale=stale_count > 0,
        stale_count=stale_count,
        current_count=current_count,
        total=stale_count + current_count,
        versions=versions,
    )


def diff(store: Store) -> DiffReport:
    """Re-derive every stored extraction and report the ones that differ.

    Walks every message that has an extraction, re-runs extraction and cleaning
    (:func:`_rederive`), and compares both the extracted and the cleaned text
    against what is stored. Rows that match are stamped with the running version
    and counted in ``unchanged``; the rest are returned in ``differing``, ordered
    by message id. The only write is that version stamp: no text is rewritten, no
    score is touched, and Pangram is never called.
    """
    differing: list[ExtractionDiff] = []
    checked = unchanged = stamped = 0

    for message_pk in store.extracted_message_ids():
        message = store.get_message(message_pk)
        extraction = store.extraction_for_message(message_pk)
        if message is None or extraction is None:  # pragma: no cover - concurrent delete
            continue
        checked += 1
        rederived = _rederive(store, message, extraction)

        if not rederived.changed:
            unchanged += 1
            if extraction.pipeline_version != __version__:
                store.set_extraction_version(extraction.id)
                stamped += 1
            continue

        mailing_list = store.get_list(message.list_id)
        address = store.get_address(message.address_id) if message.address_id is not None else None
        differing.append(
            ExtractionDiff(
                message_id=message.id,
                extraction_id=extraction.id,
                list_name=mailing_list.name if mailing_list else None,
                date=message.date,
                subject=message.subject,
                from_address=address.email if address else None,
                from_display_name=address.display_name if address else None,
                pipeline_version=extraction.pipeline_version,
                old_chars=len(extraction.extracted_text),
                new_chars=len(rederived.text),
                old_status=extraction.status,
                new_status=rederived.status,
                text_changed=rederived.text_changed,
                scored_text_changed=rederived.scored_text_changed,
                scored=store.score_for_extraction(extraction.id) is not None,
            )
        )

    return DiffReport(
        app_version=__version__,
        checked=checked,
        unchanged=unchanged,
        stamped=stamped,
        differing=differing,
    )


def reextract(store: Store, message_ids: Sequence[int]) -> ReextractSummary:
    """Re-extract the given messages, rewriting the rows whose text has moved.

    For each message with an extraction row: re-derive it, and

    - if neither the extracted nor the cleaned text changed, only stamp the
      version (counted in ``unchanged``);
    - otherwise rewrite the extraction in place
      (:meth:`~mailing_list_ai_check.store.Store.replace_extraction`), and when
      the cleaned text changed, delete any score for it — that verdict was
      reached on text that no longer exists.

    Messages without an extraction row are skipped. Nothing is re-scored here:
    ``rescore_message_ids`` lists the messages whose extraction is now ``ok`` and
    unscored, for the caller to pass to the scoring stage.
    """
    summary = ReextractSummary()

    for message_pk in message_ids:
        message = store.get_message(message_pk)
        extraction = store.extraction_for_message(message_pk)
        if message is None or extraction is None:
            continue
        summary.processed += 1
        rederived = _rederive(store, message, extraction)

        if not rederived.changed:
            summary.unchanged += 1
            if extraction.pipeline_version != __version__:
                store.set_extraction_version(extraction.id)
            continue

        store.replace_extraction(
            extraction.id,
            extracted_text=rederived.text,
            method=rederived.method,
            status=rederived.status,
        )
        summary.rewritten += 1
        if rederived.status != "ok":
            summary.not_ok += 1

        if rederived.scored_text_changed and store.delete_score_for_extraction(extraction.id):
            summary.scores_invalidated += 1

        if rederived.status == "ok" and store.score_for_extraction(extraction.id) is None:
            summary.rescore_message_ids.append(message_pk)

    return summary
