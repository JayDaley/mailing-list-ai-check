"""Quality and unit tests for the new-text extraction pipeline (stage 1).

The corpus test runs the **composite** of both pipeline stages —
``clean_for_scoring(extract_new_text(body).text).text`` — over all hand-labeled
fixtures (currently 23) and compares against ``tests/fixtures/expected/*.txt``
with the tolerant whitespace comparison documented in
``tests/fixtures/README.md``. The expected files are exactly the text sent to
Pangram, i.e. the stage-1 extraction *after* stage-2 cleaning; they did not
change when extraction was split from cleaning. Two of the trickiest interleaved
fixtures are additionally pinned to exact composite output, and one signed
fixture is pinned at the stage boundary (kept in stage 1, removed in stage 2).

Stage-2 cleaning (signature/greeting/sign-off removal) is exercised in
``tests/test_cleaning.py``; this module covers stage-1 concerns only: quote and
attribution stripping, the sign-off *boundary*, forwarded/quote-header blocks,
the over-strip guard, and the parent-diff assist.
"""

from __future__ import annotations

import pathlib

import pytest

from mailing_list_ai_check import cli
from mailing_list_ai_check.cleaning import clean_for_scoring
from mailing_list_ai_check.extraction import (
    ExtractionResult,
    count_unquoted_content_lines,
    custom_clean,
    extract_new_text,
    find_quote_header_block,
    find_signoff_boundary,
    is_attribution_line,
    is_quote_line,
    is_signoff_name_line,
    normalize_body,
    strip_after_original_message_divider,
    strip_attribution_lines,
    strip_parent_content,
)
from mailing_list_ai_check.fetcher import parse_message
from mailing_list_ai_check.store import Store

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
EXPECTED_DIR = FIXTURE_DIR / "expected"

ALL_STEMS = sorted(p.stem for p in FIXTURE_DIR.glob("*.eml"))
INTERLEAVED_STEMS = [s for s in ALL_STEMS if s.startswith("interleaved-")]


# --- tolerant comparison (README §"How the expected files were derived") ------


def tolerant_lines(text: str) -> list[str]:
    """Normalize for comparison: unify non-breaking spaces, strip each line's
    leading/trailing whitespace, and drop blank lines. Non-space content
    (including author's curly quotes / em dashes / § / emoji) is preserved.
    """
    out: list[str] = []
    for line in text.split("\n"):
        line = line.replace("\xa0", " ").replace(" ", " ").strip()
        if line:
            out.append(line)
    return out


def fixture_body(stem: str) -> str | None:
    parsed = parse_message((FIXTURE_DIR / f"{stem}.eml").read_bytes(), uid=1, folder="x")
    return parsed.body


def fixture_html(stem: str) -> str | None:
    """The message's decoded ``text/html`` part (the extraction oracle), if any."""
    parsed = parse_message((FIXTURE_DIR / f"{stem}.eml").read_bytes(), uid=1, folder="x")
    return parsed.html_body


def expected_text(stem: str) -> str:
    return (EXPECTED_DIR / f"{stem}.txt").read_text(encoding="utf-8")


def composite_text(stem: str) -> str:
    """The exact text sent to Pangram: stage 1 (with the HTML oracle) then stage 2."""
    result = extract_new_text(fixture_body(stem), html_body=fixture_html(stem))
    return clean_for_scoring(result.text).text


def matches_fixture(stem: str) -> tuple[bool, ExtractionResult]:
    # The corpus now feeds each fixture's HTML part in as the extraction oracle,
    # exactly as the pull pipeline does (parse_message exposes html_body).
    result = extract_new_text(fixture_body(stem), html_body=fixture_html(stem))
    composite = clean_for_scoring(result.text).text
    ok = tolerant_lines(composite) == tolerant_lines(expected_text(stem))
    return ok, result


# --- corpus accuracy ----------------------------------------------------------


def test_corpus_meets_accuracy_target():
    """At most 2 tolerant mismatches, and every interleaved fixture must match."""
    results = {stem: matches_fixture(stem) for stem in ALL_STEMS}
    passed = [stem for stem, (ok, _) in results.items() if ok]
    failed = [stem for stem, (ok, _) in results.items() if not ok]

    target = len(ALL_STEMS) - 2
    assert len(passed) >= target, f"only {len(passed)}/{len(ALL_STEMS)} matched; failures: {failed}"

    interleaved_failures = [s for s in INTERLEAVED_STEMS if not results[s][0]]
    assert not interleaved_failures, f"interleaved fixtures must all pass: {interleaved_failures}"


@pytest.mark.parametrize("stem", ALL_STEMS)
def test_fixture_matches_expected(stem):
    """Per-fixture visibility: currently every fixture matches (loud on regression)."""
    ok, result = matches_fixture(stem)
    assert ok, (
        f"{stem} [{result.method}] mismatch\n"
        f"--- got ---\n{composite_text(stem)}\n--- expected ---\n{expected_text(stem)}"
    )


def test_every_result_ok_status():
    """No fixture body should extract to empty or failed."""
    for stem in ALL_STEMS:
        result = extract_new_text(fixture_body(stem))
        assert result.status == "ok", f"{stem}: {result.status}"


# --- pinned exact composite output for the two trickiest interleaved fixtures --


def test_pinned_pointbypoint():
    """Gold interleaved case: 6-way point-by-point reply (README's #1 guard).

    Pins the composite (stage 1 + stage 2) — the exact text scored.
    """
    result = extract_new_text(fixture_body("interleaved-pointbypoint-lastcall-01"))
    assert result.method == "erp"
    assert clean_for_scoring(result.text).text == (
        "Some quick replies.\n"
        "Thanks.\n"
        "These would be different domains that don't connect to each other. You can "
        "run separate instances, especially when there are multicast group boundaries "
        "so the well-known groups stay disjoint.\n"
        "If there are no group boundaries, then the packets go to all members of the "
        "well-known group but are dropped by the ones without common encryption keys.\n"
        "Right, a tradeoff between plug-and-play and secure communication.\n"
        "I thought we had something like that. I'll leave this for Mike and Stig to "
        "comment about.\n"
        "\n"
        "Dino"
    )


def test_pinned_indented_quotes():
    """Indented-``>`` quotes: pins the custom indented-quote + attribution pass."""
    result = extract_new_text(fixture_body("interleaved-indented-quotes-lastcall-01"))
    assert result.method == "erp+custom"
    assert clean_for_scoring(result.text).text == (
        "Correct.\n"
        "We believe that we understood your point of view.\n"
        "\n"
        "We discussed the view that all profiles create non-interoperability, and\n"
        "that none of the certificate belongs, and we respectfully disagree.\n"
        "\n"
        "We think you are in the rough on this.\n"
        "But, we very much appreciated the engagement."
    )


def test_stage_split_pins_signature_kept_then_cleaned():
    """Lock the stage boundary on a signed fixture: the deep-thread reply's
    "Dr. … <mjos@iki.fi>" contact line is *kept* by stage 1 (novel content) and
    *removed* by stage 2 (furniture stripped for scoring).
    """
    stage1 = extract_new_text(fixture_body("interleaved-deep-thread-tls-01")).text
    scored = clean_for_scoring(stage1).text
    assert "Dr. Markku-Juhani O. Saarinen <mjos@iki.fi>" in stage1
    assert "mjos@iki.fi" not in scored
    # The author's substantive prose survives both stages.
    assert "consumer products" in scored


# --- unit tests: pre-normalization --------------------------------------------


def test_normalize_strips_bom_and_zero_width():
    assert normalize_body("﻿> quote") == "> quote"
    assert normalize_body("a​b‌‍⁠c") == "abc"


def test_normalize_converts_line_endings():
    assert normalize_body("a\r\nb\rc") == "a\nb\nc"


# --- unit tests: quote detection ----------------------------------------------


@pytest.mark.parametrize(
    "line",
    ["> quoted", ">> nested", "    > indented", "\t>deep", "...> flowed elision", ".. > x"],
)
def test_is_quote_line_true(line):
    assert is_quote_line(line)


@pytest.mark.parametrize("line", ["not a quote", "a > b comparison", "-markku", "  Brian"])
def test_is_quote_line_false(line):
    assert not is_quote_line(line)


# --- unit tests: attribution variants -----------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "On Tue, Jul 7, 2026, at 03:25, Dan Wing wrote:",
        "Martin Thomson via Datatracker <noreply@example.org> wrote:",
        "Am Montag, dem 20.07.2026 um 22:55 +0200 schrieb Dennis Jackson:",
        # Japanese (Spark): "<date>、<who>のメール:" — "mail from <who>".
        "2026年7月22日 18:12 +0900、Alex Konviser <ietf@gravit.space>のメール:",
        "2026/07/22 18:12、Alex Konviser <ietf@gravit.space>のメール:",
    ],
)
def test_is_attribution_line_true(line):
    assert is_attribution_line(line)


@pytest.mark.parametrize(
    "line",
    [
        "Just wrote some code.",
        "On the other hand, no.",
        "Hi Roy,",
        # A のメール: ending without the leading date never matches.
        "アレックスのメール:",
    ],
)
def test_is_attribution_line_false(line):
    assert not is_attribution_line(line)


def test_strip_attribution_handles_wrapped_form():
    lines = [
        "keep this",
        "On Tue, Jul 7, 2026, at 03:25,",
        "Dan Wing wrote:",
        "keep that",
    ]
    assert strip_attribution_lines(lines) == ["keep this", "keep that"]


def test_strip_attribution_does_not_span_blank_line():
    lines = ["On something happened", "", "wrote: this is real prose"]
    # A blank line breaks the wrapped-attribution window, so nothing is dropped.
    assert strip_attribution_lines(lines) == lines


# --- unit tests: stage-1 keeps signatures/greetings/sign-offs -----------------


def test_custom_clean_keeps_signature_and_signoff():
    # Stage 1 removes quotes/attributions but KEEPS the author's furniture; the
    # signature and sign-off are stripped later by cleaning, not here.
    text = "> old quote\n\nReal content.\n-- \nSignature line\nmore signature"
    cleaned = custom_clean(text)
    assert "old quote" not in cleaned  # quote removed
    assert "Real content." in cleaned
    # The signature delimiter line survives stage 1 (rstripped to "--").
    assert "--" in cleaned.split("\n")
    assert "Signature line" in cleaned


def test_custom_clean_keeps_greeting_and_closing():
    text = "Hi all,\n\nThe actual content.\n\nBest,\nSongbo"
    cleaned = custom_clean(text)
    assert "Hi all," in cleaned
    assert "The actual content." in cleaned
    assert "Songbo" in cleaned


# --- unit tests: over-strip guard ---------------------------------------------


def test_count_unquoted_content_lines_excludes_quotes_keeps_signature():
    # The denominator now mirrors stage 1: it excludes quoted lines but KEEPS the
    # signature region (stage 1 retains ERP signature fragments).
    body = "line one\n> quoted\nline two\n-- \nsignature stuff\nmore sig"
    assert count_unquoted_content_lines(body) == 5


def test_dashed_digest_content_is_preserved():
    # A dashed-separator "digest" makes ERP label the table a signature; stage 1
    # keeps signature fragments, so the whole table is recovered without a
    # custom-fallback. (Before the extraction/cleaning split, ERP's parse_reply
    # dropped the table and the over-strip guard rebuilt it via custom-fallback.)
    body = "Header row of a report\n----------+----------\n" + "\n".join(
        f"data row {i}" for i in range(10)
    )
    result = extract_new_text(body)
    assert result.method != "custom-fallback"
    assert "data row 9" in result.text
    assert "Header row of a report" in result.text


# --- unit tests: sign-off boundary --------------------------------------------


def test_find_signoff_boundary_before_wrapped_attribution():
    # The message-39 shape: salutation + name, then a wrapped attribution
    # introducing a quoted thread with no ">" markers.
    lines = (
        "The proposed outcome labels are vector vocabulary only.\n\n"
        "Best,\nSongbo\n\n"
        "On Tue, 14 Jul 2026 18:45:07 +0000, Thi Nguyen-Huu thi.nh@winmagic.com\n"
        "wrote:\n\n"
        "Karthik, Songbo, all,\n\nQuoted content with no markers.\n"
    ).split("\n")
    idx = find_signoff_boundary(lines)
    assert idx is not None
    assert lines[idx - 1] == "Songbo"
    # Stage 1 truncates the quoted thread at the boundary but keeps the sign-off;
    # stage 2 then removes the "Best, / Songbo" framing.
    stage1 = custom_clean("\n".join(lines))
    assert stage1.endswith("Songbo")
    assert clean_for_scoring(stage1).text.endswith("vector vocabulary only.")


def test_find_signoff_boundary_inline_form():
    lines = ["Done, see branch.", "", "Cheers, Peter", "", "On Mon, Bob Smith wrote:", "old text"]
    assert find_signoff_boundary(lines) == 3


def test_find_signoff_boundary_single_line_attribution():
    lines = ["Reply text.", "", "Regards", "Alice", "", "Bob <bob@example.org> wrote:", "old"]
    assert find_signoff_boundary(lines) == 4


def test_no_boundary_without_name_line():
    # A bare mid-thread "Thanks." (the pointbypoint fixture shape) must never
    # truncate — the name line is required.
    lines = ["Some quick replies.", "Thanks.", "> quoted point one", "My answer."]
    assert find_signoff_boundary(lines) is None


def test_no_boundary_without_following_attribution():
    # Sign-off at the true end, or followed by authored content: no truncation.
    assert find_signoff_boundary(["Reply.", "", "Best,", "Songbo"]) is None
    lines = ["Reply.", "", "Best,", "Songbo", "", "Ps. one more authored thought."]
    assert find_signoff_boundary(lines) is None


@pytest.mark.parametrize("text", ["Songbo", "Thi Nguyen-Huu", "Markku-Juhani O. Saarinen", "Brian"])
def test_is_signoff_name_line_true(text):
    assert is_signoff_name_line(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "see the attached test-vector plan",
        "Row 4 needs a re-assessment lease",
        "the next governed key use must fail because status cannot be established",
        "Chair, WIMSE WG <chair@example.org>",
    ],
)
def test_is_signoff_name_line_false(text):
    assert not is_signoff_name_line(text)


def test_toppost_signoff_unquoted_fixture_extracts_author_only():
    """The message-39 defect: a Gmail-style fully top-posted reply whose quoted
    thread has no ``>`` markers must stop at the "Best, / Songbo" sign-off
    instead of shipping ~1800 lines of quoted thread.
    """
    stem = "toppost-signoff-unquoted-wimse-01"
    body = fixture_body(stem)
    result = extract_new_text(body)
    assert result.method != "custom-fallback"
    composite = clean_for_scoring(result.text).text
    # The "Best, / Songbo" framing is stripped by stage 2 along with the thread.
    assert composite.strip().splitlines()[-1].startswith("vector vocabulary only")
    assert "possession-side rows" in composite  # author's text kept
    assert "Karthik, Songbo, all," not in composite  # quoted thread dropped
    assert tolerant_lines(composite) == tolerant_lines(expected_text(stem))
    # The guard denominator must exclude everything after the sign-off too.
    assert count_unquoted_content_lines(normalize_body(body)) < 30


# --- unit tests: forwarded / quote-header block detection ---------------------


def test_original_message_divider_truncates_from_own_line():
    lines = ["My reply.", "", "-------- Original message --------", "From: Someone", "Old text."]
    assert strip_after_original_message_divider(lines) == ["My reply.", ""]
    # Outlook's tighter form, no inner spaces.
    lines = ["My reply.", "-----Original Message-----", "From: Someone"]
    assert strip_after_original_message_divider(lines) == ["My reply."]
    # Gmail-style forwarded divider.
    lines = ["Note.", "---------- Forwarded message ---------", "From: X"]
    assert strip_after_original_message_divider(lines) == ["Note."]


def test_original_message_divider_glued_midline_keeps_prefix():
    # HTML-to-text flattening can glue the whole quoted mail onto one line
    # (the message-79 defect): the author's prefix before the divider survives.
    lines = ["All,Three quick questions:-------- Original message --------From: Steven Mih"]
    assert strip_after_original_message_divider(lines) == ["All,Three quick questions:"]


def test_original_message_divider_prose_is_safe():
    # Mentioning an "original message" without flanking dashes never truncates.
    lines = ["The original message said something else.", "-- Original message"]
    assert strip_after_original_message_divider(lines) == lines


def test_toppost_originalmsg_divider_fixture_uses_html_fallback():
    """The message-79 defect, now resolved via the HTML oracle: the plain part is
    two giant glued lines (the client dropped the newlines), so the degenerate
    plain fallback derives the properly line-broken novel text from the HTML part
    instead, and the stage-1 pipeline still truncates at the dashed "Original
    message" divider.
    """
    stem = "toppost-originalmsg-divider-agent2agent-01"
    result = extract_new_text(fixture_body(stem), html_body=fixture_html(stem))
    assert result.status == "ok"
    assert result.method.startswith("html-")
    # Now properly line-broken (not one glued line), and the quoted thread gone.
    lines = [ln for ln in result.text.split("\n") if ln.strip()]
    assert len(lines) > 1
    assert lines[0] == "All,"
    assert "Original message" not in result.text
    assert "From: Steven Mih" not in result.text
    # Word-stream equality with the pre-HTML single-line extraction is preserved:
    # only whitespace (the restored line breaks) differs.
    old_glued = extract_new_text(fixture_body(stem)).text
    assert "".join(result.text.split()) == "".join(old_glued.split())
    composite = clean_for_scoring(result.text).text
    assert tolerant_lines(composite) == tolerant_lines(expected_text(stem))


def test_find_quote_header_block_outlook_sent():
    lines = (
        "Cheers,\nNate\n\n"
        "From: Wesley Eddy via Datatracker <noreply@example.org>\n"
        "Sent: Tuesday, June 23, 2026 3:45 PM\n"
        "To: tsv-art@example.org\n"
        "Subject: [Int-area] draft review\n\n"
        "Reviewer: Wesley Eddy\n"
    ).split("\n")
    idx = find_quote_header_block(lines)
    assert idx is not None
    assert lines[idx].startswith("From: Wesley Eddy")


def test_find_quote_header_block_date_variant_with_cc():
    # "Date:" instead of "Sent:", with a Cc: line — still a quote header.
    lines = (
        "Regards,\nAlice\n\n"
        "From: Bob Smith <bob@example.org>\n"
        "Date: Mon, 20 Jul 2026 10:00:00 +0000\n"
        "To: wg@example.org\n"
        "Cc: chairs@example.org\n"
        "Subject: Re: draft comments\n\n"
        "Old quoted content.\n"
    ).split("\n")
    idx = find_quote_header_block(lines)
    assert idx is not None
    assert lines[idx].startswith("From: Bob Smith")


def test_find_quote_header_block_ignores_pasted_evidence():
    # A From: preceded by another header line is pasted evidence embedded in
    # prose (the threadstarter-rfc2047-header fixture), not a real quote header.
    lines = (
        "TECHNICAL EVIDENCE\n\n"
        "Message-ID: <a@b>\n"
        "Date: Tue, 12 May 2026 10:15:36 +0200\n"
        "From: Timo Gerke <timo.gerke@alice-dsl.net>\n"
        "To: Deb Cooley <debcooley1@gmail.com>\n"
        "Subject: Notice\n\n"
        "Best regards,\nTimo Gerke\n"
    ).split("\n")
    assert find_quote_header_block(lines) is None


def test_find_quote_header_block_requires_signal_field():
    # A lone "From:" with no Sent/Date/To/Cc/Subject following is not a block.
    lines = ["From: someone", "Just ordinary prose here.", "More prose."]
    assert find_quote_header_block(lines) is None


def test_strip_after_chinese_original_message_divider():
    # The Chinese Outlook divider "-----邮件原件-----" (and QQ Mail's
    # "-----原始邮件-----") truncates like "-----Original Message-----".
    lines = ["My reply.", "-----邮件原件-----", "发件人: Li Ming <li@example.org>", "Quoted."]
    assert strip_after_original_message_divider(lines) == ["My reply."]
    lines = ["My reply.", "------------------ 原始邮件 ------------------", "Quoted."]
    assert strip_after_original_message_divider(lines) == ["My reply."]


def test_find_quote_header_block_chinese_alibaba():
    # Alibaba Mail style: CJK labels, full-width colons with no space after
    # them, 主　题 padded with an ideographic space (U+3000), and a dashed
    # divider drawn above the block.
    lines = (
        "Looking forward to the discussions.\n"
        "Regards,\nWei Zhang\n"
        "------------------------------------------------------------------\n"
        "发件人：Li Ming <li.ming@example.org>\n"
        "发送时间：2026年7月15日(周三) 10:05\n"
        "收件人：Anna Schmidt<anna.schmidt@example.org>\n"
        "主　题：[wg] Re: charter scope\n"
        "Old quoted content here.\n"
    ).split("\n")
    idx = find_quote_header_block(lines)
    assert idx is not None
    assert lines[idx].startswith("发件人")


def test_find_quote_header_block_chinese_outlook_ascii_colon():
    # Chinese Outlook style: CJK labels with an ASCII colon and a space.
    lines = (
        "Thanks for the summary.\n\n"
        "发件人: Anna Schmidt [mailto:anna.schmidt@example.org]\n"
        "发送时间: 2026年7月15日 8:40\n"
        "收件人: Li Ming <li.ming@example.org>; wg@example.org\n"
        "主题: [wg] Re: charter scope\n"
        "Old quoted content here.\n"
    ).split("\n")
    idx = find_quote_header_block(lines)
    assert idx is not None
    assert lines[idx].startswith("发件人")


def test_strip_after_chinese_quote_header_drops_dangling_divider():
    # Truncating at an Alibaba-style block also drops the dashed divider the
    # client drew above it — furniture, not author content. A dashed rule
    # elsewhere in the body is untouched.
    body = (
        "First point.\n"
        "----------\n"
        "Second point after an author-drawn rule.\n"
        "Regards,\nWei Zhang\n"
        "------------------------------------------------------------------\n"
        "发件人：Li Ming <li.ming@example.org>\n"
        "发送时间：2026年7月15日(周三) 10:05\n"
        "收件人：Anna Schmidt<anna.schmidt@example.org>\n"
        "主　题：[wg] Re: charter scope\n"
        "Old quoted content here.\n"
        " Anna Schmidt\n"
        " Mobile: +49-000000000\n"
        " Mail: anna.schmidt@example.org <mailto:anna.schmidt@example.org >\n"
        "From:Third Person <third@example.org <mailto:third@example.org >>\n"
        "To:Li Ming <li.ming@example.org <mailto:li.ming@example.org >>\n"
    )
    result = extract_new_text(body)
    lines = [ln for ln in result.text.split("\n") if ln.strip()]
    assert lines[0] == "First point."
    assert "----------" in lines  # the author's own rule survives
    assert lines[-1] == "Wei Zhang"
    assert "发件人" not in result.text
    assert "Anna Schmidt" not in result.text.split("Regards,")[-1]
    assert "Mobile:" not in result.text
    assert "From:Third Person" not in result.text


def test_toppost_outlook_quoteheader_fixture_extracts_author_only():
    """The Phase 7.2 defect: an Outlook top-post must not ship the quoted review.

    ERP's non-quoted fragments now include the header-introduced quoted review;
    the fix is that stage 1's ``strip_after_quote_header_block`` truncates it, and
    the over-strip guard must NOT then fire (so it does not fall back to
    custom-fallback on the whole body). Method should therefore be ``erp+custom``.
    """
    stem = "toppost-outlook-quoteheader-lastcall-01"
    body = fixture_body(stem)
    result = extract_new_text(body)
    assert result.method != "custom-fallback"
    assert result.method == "erp+custom"
    composite = clean_for_scoring(result.text).text
    # Only the author's content lines survive; no framing, none of the review.
    assert "Nate" not in composite
    assert "Reviewer: Wesley Eddy" not in composite
    assert "congestion" not in composite
    assert tolerant_lines(composite) == tolerant_lines(expected_text(stem))
    # The guard's denominator excludes the quote-header block and everything
    # after it, so a ~1.7 KB body reduces to the author's few framing/content
    # lines (greeting + two content lines + sign-off are all kept in stage 1).
    assert count_unquoted_content_lines(body) == 4


# --- unit tests: statuses / edge cases ----------------------------------------


def test_none_body_is_empty():
    result = extract_new_text(None)
    assert result.status == "empty"
    assert result.method == "none"
    assert result.text == ""


def test_blank_body_is_empty():
    assert extract_new_text("   \n\n\t").status == "empty"


def test_quote_only_body_is_empty():
    result = extract_new_text("> just a quote\n> and more quote\n")
    assert result.status == "empty"


def test_bom_prefixed_quote_is_stripped_end_to_end():
    # Without normalization ERP would leak the BOM-prefixed quote line.
    body = "﻿> Given the proposal\n\nMy actual reply here."
    result = extract_new_text(body)
    assert "Given the proposal" not in result.text
    assert result.text.strip() == "My actual reply here."


# --- unit tests: HTML oracle (html_body) --------------------------------------


def test_html_only_extracts_novel_text_with_html_prefix():
    # No plain body at all: the novel text is derived from the HTML part and run
    # through the normal stage-1 pipeline; the method is prefixed "html-".
    html = (
        "<div>Here is my genuinely new reply, written for the working group.</div>"
        "<blockquote><div>On some date X wrote:</div><div>the old quoted message</div>"
        "</blockquote>"
    )
    result = extract_new_text(None, html_body=html)
    assert result.status == "ok"
    assert result.method.startswith("html-")
    assert "genuinely new reply" in result.text
    assert "old quoted message" not in result.text


def test_html_only_blank_html_is_empty_none():
    # No plain body and no usable HTML falls back to the original empty result.
    result = extract_new_text(None, html_body="   ")
    assert result.status == "empty"
    assert result.method == "none"


def test_degenerate_plain_falls_back_to_html():
    # A flattened plain body (one giant glued line) with a structured HTML part:
    # the HTML novel text (properly line-broken) is used instead, "html-" prefix.
    glued = (
        "First point about the draft that runs on and on and on. " * 12
    ).strip() + "Second sentence glued here.Third sentence also glued on."
    html = (
        "<div>First point about the draft.</div>"
        "<div>Second structured line here.</div>"
        "<div>Third structured line here.</div>"
        "<div>Fourth structured line here.</div>"
    )
    result = extract_new_text(glued, html_body=html)
    assert result.status == "ok"
    assert result.method.startswith("html-")
    # Multi-line, from the HTML — not the single glued plain line.
    assert len([ln for ln in result.text.split("\n") if ln.strip()]) >= 4


def test_non_degenerate_plain_is_not_replaced_by_html():
    # A perfectly normal multi-line plain body is used as-is (no html- prefix),
    # even when an HTML part is present.
    body = "Line one of my reply.\nLine two of my reply.\nLine three.\nLine four here."
    html = "<div>totally different html novel text that should be ignored</div>"
    result = extract_new_text(body, html_body=html)
    assert not result.method.startswith("html-")
    assert "Line one of my reply." in result.text
    assert "different html novel text" not in result.text


def test_oracle_assist_removes_blockquote_matching_unmarked_quote():
    # A top-post whose quoted previous message is reproduced in the plain body
    # with NO ">" markers (so nothing is stripped), while the HTML wraps that
    # same message in a <blockquote>. The oracle removes the leaked block and
    # appends "+html-quote".
    quoted_para_1 = "The registry should remain open for new entries at all times going forward."
    quoted_para_2 = "Each entry must include a stable reference and a short human readable summary."
    quoted_para_3 = "Reviewers should be given at least two full weeks to raise any late concerns."
    body = (
        "I have revised the entire draft to address every one of your review points.\n"
        "\n"
        f"{quoted_para_1}\n{quoted_para_2}\n{quoted_para_3}\n"
    )
    html = (
        "<div>I have revised the entire draft to address every one of your review points.</div>"
        f"<blockquote><div>{quoted_para_1}</div><div>{quoted_para_2}</div>"
        f"<div>{quoted_para_3}</div></blockquote>"
    )
    result = extract_new_text(body, html_body=html)
    assert "I have revised the entire draft" in result.text
    assert quoted_para_1 not in result.text
    assert quoted_para_2 not in result.text
    assert result.method.endswith("+html-quote")


def test_oracle_assist_spares_isolated_coincidental_match():
    # A single author line that happens to echo a quoted sentence verbatim must
    # NOT be stripped — the oracle only adopts a substantial (block) removal.
    echoed = "Each entry must include a stable reference and a short human readable summary."
    body = f"I strongly agree with your point that follows below in the quoted thread.\n{echoed}\n"
    html = (
        "<div>I strongly agree with your point that follows below in the quoted thread.</div>"
        f"<div>{echoed}</div>"
        f"<blockquote><div>{echoed}</div></blockquote>"
    )
    result = extract_new_text(body, html_body=html)
    assert echoed in result.text  # the single coincidental match is kept
    assert "+html-quote" not in result.method


def test_html_body_none_is_identical_to_no_argument():
    body = "> quoted line\n\nMy genuinely new reply text here today."
    assert extract_new_text(body, html_body=None) == extract_new_text(body)


# --- unit tests: parent-diff assist -------------------------------------------


def test_parent_diff_removes_unquoted_toppost_thread():
    # Fully top-posted reply: author's new text, then the entire parent message
    # reproduced with no ">" markers and no attribution. The parent portion must
    # go; the authored portion must stay; the method gains "+parent-diff".
    parent = (
        "Thanks for the detailed proposal about the new key rotation scheme.\n"
        "I think the possession side needs a clearer failure mode when the status\n"
        "cannot be established within the negotiated window between the two peers.\n"
    )
    child = (
        "I have revised the entire draft to incorporate all of your suggestions.\n"
        "Every reviewer concern is now tracked in the updated issue list online.\n"
        "\n" + parent
    )
    result = extract_new_text(child, parent)
    assert "I have revised the entire draft" in result.text
    assert "possession side needs a clearer failure mode" not in result.text
    assert result.method.endswith("+parent-diff")


def test_parent_diff_removes_rewrapped_paragraph():
    # The parent paragraph is one long line; the child re-wraps it at a narrower
    # width, so no child line equals a parent line (aligned-run misses it). The
    # rewrap rule removes each long child line found in the parent word-stream.
    parent = (
        "the registry should remain open for new entries but each entry must "
        "include a stable reference and a short human readable description"
    )
    child = (
        "I strongly agree with the summary you posted to the list earlier today\n"
        "\n"
        "the registry should remain open for new entries but each entry\n"
        "must include a stable reference and a short human readable description\n"
    )
    result = extract_new_text(child, parent)
    assert "I strongly agree with the summary" in result.text
    assert "registry should remain open" not in result.text
    assert result.method.endswith("+parent-diff")


def test_parent_diff_keeps_short_signoff_present_in_parent():
    # The author's own sign-off also sits inside the parent's nested quotes, but
    # it is too short to be strong evidence, so it must survive.
    text = "Best,\nSongbo"
    parent = "Some earlier discussion here about the draft.\nBest,\nSongbo"
    assert strip_parent_content(text, parent) == "Best,\nSongbo"


def test_parent_diff_keeps_short_authored_echo():
    # A short (<8-word) echo of the parent quoted inline is coincidental, not
    # proof, so it is not deleted.
    parent = "We noted that the status cannot be established within the negotiated window."
    text = (
        "Quoting you: the status cannot be established.\n"
        "I think we should fix that in the next revision of the draft."
    )
    result = strip_parent_content(text, parent)
    assert "the status cannot be established" in result
    assert "next revision of the draft" in result


def test_parent_diff_none_is_identical_to_no_argument():
    body = "> quoted line\n\nMy genuinely new reply text here."
    assert extract_new_text(body, None) == extract_new_text(body)


def test_parent_diff_entire_parent_resend_is_empty():
    # An exact re-send of the parent legitimately reduces to nothing.
    parent = (
        "This entire message is a verbatim resend of the earlier note today.\n"
        "Every single line here comes straight from the parent message body.\n"
    )
    result = extract_new_text(parent, parent)
    assert result.status == "empty"
    assert result.text == ""
    assert result.method.endswith("+parent-diff")


def test_strip_parent_content_thresholds():
    # A block of two short lines totaling < 10 words (and no line >= 8) survives.
    short_block = "Hi all,\nBest wishes"
    parent = "Hi all,\nBest wishes\nand some other content only in the parent body."
    assert strip_parent_content(short_block, parent) == "Hi all,\nBest wishes"

    # A single line of >= 8 words is strong enough on its own to be removed.
    long_line = "This is a fairly long authored looking line with plenty of words."
    assert strip_parent_content(long_line, long_line) == ""


def test_strip_parent_content_never_raises_on_empty_input():
    assert strip_parent_content("", "") == ""
    assert strip_parent_content("   \n\n", "anything at all here") == ""


def test_parent_diff_no_removal_leaves_text_and_method_untouched():
    # A parent that shares no substantial content must not change the output.
    # strip_parent_content collapses internal blank runs, so a naive string
    # comparison would spuriously add "+parent-diff" and reformat the text; the
    # assist must key off real content removal, not incidental reformatting.
    body = "First authored paragraph here.\n\n\nSecond authored paragraph here."
    parent = "Totally unrelated earlier message about something else entirely."
    with_parent = extract_new_text(body, parent)
    without_parent = extract_new_text(body, None)
    assert with_parent == without_parent
    assert "+parent-diff" not in with_parent.method
    # An empty parent body (a stored row with raw_body = "") is the same no-op.
    assert extract_new_text(body, "") == without_parent


# --- pipeline (cli.run_extract) -----------------------------------------------


def _seed_message(store: Store, *, message_id: str, raw_body: str | None) -> int:
    mlist = store.upsert_list("tls", "Shared Folders/tls")
    addr = store.upsert_address("author@example.org", "Author")
    upsert = store.upsert_message(
        message_id=message_id,
        list_id=mlist.id,
        address_id=addr.id,
        subject="Re: something",
        date="2026-07-21T00:00:00+00:00",
        in_reply_to=None,
        raw_body=raw_body,
        uid=1,
    )
    return upsert.message.id


def _seed_reply(store: Store, *, message_id: str, in_reply_to: str, raw_body: str | None) -> int:
    """Seed a reply with a distinct Message-ID and an ``In-Reply-To`` header."""
    mlist = store.upsert_list("tls", "Shared Folders/tls")
    addr = store.upsert_address("author@example.org", "Author")
    upsert = store.upsert_message(
        message_id=message_id,
        list_id=mlist.id,
        address_id=addr.id,
        subject="Re: something",
        date="2026-07-21T00:00:00+00:00",
        in_reply_to=in_reply_to,
        raw_body=raw_body,
        uid=2,
    )
    return upsert.message.id


def test_pipeline_extracts_and_records_statuses(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        ok_id = _seed_message(
            store,
            message_id="<ok@x>",
            raw_body="> quoted\n\nThis is my new reply text.",
        )
        empty_id = _seed_message(store, message_id="<html@x>", raw_body=None)

        status_counts, method_counts = cli.run_extract(store)

        assert status_counts["ok"] == 1
        assert status_counts["empty"] == 1
        assert sum(method_counts.values()) == 2

        ok_row = store.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (ok_id,)
        ).fetchone()
        assert ok_row["status"] == "ok"
        assert "new reply text" in ok_row["extracted_text"]
        assert ok_row["char_count"] == len(ok_row["extracted_text"])

        empty_row = store.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (empty_id,)
        ).fetchone()
        assert empty_row["status"] == "empty"
        assert empty_row["method"] == "none"


def test_pipeline_is_idempotent(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        _seed_message(store, message_id="<a@x>", raw_body="Some new text here.")

        first, _ = cli.run_extract(store)
        assert sum(first.values()) == 1

        # Second run finds no messages lacking an extraction row: a no-op.
        second, _ = cli.run_extract(store)
        assert sum(second.values()) == 0

        count = store.conn.execute("SELECT COUNT(*) AS c FROM extractions").fetchone()["c"]
        assert count == 1


def test_pipeline_respects_limit(tmp_path):
    with Store(tmp_path / "db.sqlite") as store:
        for i in range(5):
            _seed_message(store, message_id=f"<m{i}@x>", raw_body=f"Reply number {i} text.")

        status_counts, _ = cli.run_extract(store, limit=2)
        assert sum(status_counts.values()) == 2

        remaining = list(store.iter_messages_without_extraction())
        assert len(remaining) == 3


def test_pipeline_parent_diff_removes_quoted_parent(tmp_path):
    # A reply whose parent is in the store: cli.run_extract resolves the parent
    # via In-Reply-To and the assist strips the quoted parent thread.
    parent_body = (
        "Thanks for the detailed proposal about the new key rotation scheme.\n"
        "I think the possession side needs a clearer failure mode when the status\n"
        "cannot be established within the negotiated window between the two peers.\n"
    )
    child_body = (
        "I have revised the entire draft to incorporate all of your suggestions.\n"
        "Every reviewer concern is now tracked in the updated issue list online.\n"
        "\n" + parent_body
    )
    with Store(tmp_path / "db.sqlite") as store:
        _seed_message(store, message_id="<parent@x>", raw_body=parent_body)
        child_id = _seed_reply(
            store, message_id="<child@x>", in_reply_to="<parent@x>", raw_body=child_body
        )

        cli.run_extract(store)

        row = store.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (child_id,)
        ).fetchone()
        assert "I have revised the entire draft" in row["extracted_text"]
        assert "possession side needs a clearer failure mode" not in row["extracted_text"]
        assert row["method"].endswith("+parent-diff")


def test_pipeline_reply_without_stored_parent_extracts_normally(tmp_path):
    # The parent is not in the store: get_parent_body returns None, so extraction
    # runs exactly as it would with no parent (no "+parent-diff" suffix).
    child_body = "> some quoted line\n\nThis is my genuinely new reply content here today."
    with Store(tmp_path / "db.sqlite") as store:
        child_id = _seed_reply(
            store, message_id="<child@x>", in_reply_to="<missing@x>", raw_body=child_body
        )

        cli.run_extract(store)

        row = store.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (child_id,)
        ).fetchone()
        assert "This is my genuinely new reply content" in row["extracted_text"]
        assert "+parent-diff" not in row["method"]


def test_pipeline_self_reply_is_not_wiped(tmp_path):
    # A malformed message whose In-Reply-To names its own Message-ID must not be
    # treated as a reply to itself: resolving the "parent" to its own body would
    # make the parent-diff assist delete every line and report the message empty.
    child_body = (
        "I have reviewed the whole proposal in detail and I think we should adopt it.\n"
        "The rotation scheme handles every failure mode that we discussed at length.\n"
    )
    with Store(tmp_path / "db.sqlite") as store:
        child_id = _seed_reply(
            store, message_id="<self@x>", in_reply_to="<self@x>", raw_body=child_body
        )

        cli.run_extract(store)

        row = store.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (child_id,)
        ).fetchone()
        assert row["status"] == "ok"
        assert "I have reviewed the whole proposal" in row["extracted_text"]
        assert "+parent-diff" not in row["method"]
