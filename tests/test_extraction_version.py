"""A fail-safe guard on :data:`~mailing_list_ai_check.extraction.EXTRACTION_VERSION`.

``extractions.extraction_version`` is only meaningful if it is incremented
whenever the routine behind the stored text changes. Nothing enforces that
automatically — the constant is hand-maintained — so this module pins the
routine's *real behaviour*: a single SHA-256 over the composite output of every
``.eml`` fixture in ``tests/fixtures``, taken in stem order. Any change to
:mod:`~mailing_list_ai_check.extraction`, :mod:`~mailing_list_ai_check.cleaning`
or :mod:`~mailing_list_ai_check.html_text` that moves what a fixture produces
moves the digest, and the developer has to decide, deliberately, whether it was
a behaviour change (bump the constant, re-record the digest) or a mistake
(revert).

This is stricter than the corpus test in ``tests/test_extraction.py``, which
compares with ``tolerant_lines()`` — blank lines dropped, each line stripped. A
whitespace-only difference passes there and still changes the exact bytes sent to
Pangram, which changes the score cache key. The digest is taken over the exact
strings.

Three values per fixture go into it: the composite (stage 1 then stage 2 —
literally what Pangram receives), plus stage 1's ``text`` and ``method``, so a
change confined to stage 1 that stage 2 happens to erase is still caught.
"""

from __future__ import annotations

import hashlib
import pathlib

from mailing_list_ai_check.cleaning import clean_for_scoring
from mailing_list_ai_check.extraction import EXTRACTION_VERSION, extract_new_text
from mailing_list_ai_check.fetcher import parse_message

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
ALL_STEMS = sorted(p.stem for p in FIXTURE_DIR.glob("*.eml"))

#: SHA-256 of :func:`corpus_digest` over the fixture corpus, recorded against
#: ``EXTRACTION_VERSION = 4``. Re-record it (and bump the constant) only when the
#: change in behaviour is intended. The generation-3 change (quote-header
#: truncation before ERP, tolerant header walks) and the generation-4 change
#: (parent-diff continuation rule, Gmail interleaved-wrapper reclassification)
#: each left every corpus output byte-identical — the shapes they fix occur in
#: stored mail, not in the corpus — so the digest value carries over from
#: generation 2 unchanged.
EXPECTED_DIGEST = "faf5f388795897201e92a58600b345c19656df939228bd87bd47ab1578f1db5f"

#: The generation the digest above was recorded against. It exists so that a
#: bump without a re-record, or a re-record without a bump, is visible in the
#: diff of this file.
DIGEST_EXTRACTION_VERSION = 4


def corpus_digest() -> str:
    """Return the SHA-256 over the whole corpus's derived text.

    For each fixture, in stem order: the composite text sent to Pangram, then
    stage 1's text and method. Every field is length-prefixed so no combination
    of contents can imitate a different split.
    """
    digest = hashlib.sha256()
    for stem in ALL_STEMS:
        parsed = parse_message((FIXTURE_DIR / f"{stem}.eml").read_bytes(), uid=1, folder="x")
        stage1 = extract_new_text(parsed.body, html_body=parsed.html_body)
        composite = clean_for_scoring(stage1.text).text
        for field in (stem, composite, stage1.text, stage1.method):
            encoded = field.encode("utf-8")
            digest.update(f"{len(encoded)}:".encode("ascii"))
            digest.update(encoded)
    return digest.hexdigest()


def test_the_corpus_has_fixtures_to_hash():
    """A digest over an empty corpus would pin nothing and still pass."""
    assert len(ALL_STEMS) >= 20


def test_extraction_output_matches_the_recorded_digest():
    assert corpus_digest() == EXPECTED_DIGEST, (
        "The extraction/cleaning pipeline no longer produces the text this digest "
        "was recorded against.\n"
        "If the change is intended: increment EXTRACTION_VERSION in "
        "src/mailing_list_ai_check/extraction.py, then re-record EXPECTED_DIGEST "
        "and DIGEST_EXTRACTION_VERSION in this file with the value printed by\n"
        "  .venv/bin/python -c \"import sys; sys.path.insert(0, 'tests'); "
        'import test_extraction_version as t; print(t.corpus_digest())"\n'
        "Stored extractions then read as stale and the dashboard offers the "
        "re-derivation check.\n"
        "If the change is not intended: revert it — the exact bytes sent to "
        "Pangram, and so every score cache key, have moved."
    )


def test_the_digest_was_recorded_against_the_current_generation():
    """Bumping the constant without re-recording the digest is incomplete work."""
    assert DIGEST_EXTRACTION_VERSION == EXTRACTION_VERSION, (
        "EXTRACTION_VERSION changed but the digest in this file was not re-recorded; "
        "record the current corpus_digest() and set DIGEST_EXTRACTION_VERSION to match."
    )
