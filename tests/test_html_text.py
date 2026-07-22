"""Unit tests for the HTML-as-oracle helper (:mod:`mailing_list_ai_check.html_text`).

Cover the block-structure conversion, entity handling, skipped script/style/head,
the blockquote / gmail_quote / signature container splits (including a signature
nested inside a quote, which counts as quoted), and defensive handling of
malformed markup — unclosed tags, stray closes, and empty/garbage input never
raise.
"""

from __future__ import annotations

import pytest

from mailing_list_ai_check.html_text import HtmlParts, html_to_text, split_html_parts


# --- block conversion ---------------------------------------------------------


def test_paragraphs_and_divs_become_separate_lines():
    # A block's open and close each break the line, so adjacent blocks are
    # separated by a blank line (collapsed blank runs). ``<br>`` breaks singly.
    html = "<p>First para.</p><div>Second block.</div>"
    assert html_to_text(html) == "First para.\n\nSecond block."


def test_br_forces_a_single_line_break():
    assert html_to_text("<div>one<br>two<br>three</div>") == "one\ntwo\nthree"


def test_list_items_and_table_rows_break():
    html = "<ul><li>alpha</li><li>beta</li></ul><table><tr>r1</tr><tr>r2</tr></table>"
    assert html_to_text(html) == "alpha\n\nbeta\n\nr1\n\nr2"


def test_inline_whitespace_is_collapsed_per_line():
    html = "<div>  lots   of\n\tspace   here </div>"
    assert html_to_text(html) == "lots of space here"


def test_blank_line_runs_collapse_and_edges_trimmed():
    html = "<div><br><br></div><p>content</p><div><br></div>"
    assert html_to_text(html) == "content"


# --- entities -----------------------------------------------------------------


def test_entities_are_unescaped():
    assert html_to_text("<p>a &amp; b &lt; c &gt; d &nbsp;e</p>") == "a & b < c > d e"


def test_numeric_char_refs_unescaped():
    assert html_to_text("<p>&#233;&#x2014;</p>") == "é—"


# --- skipped regions ----------------------------------------------------------


def test_script_and_style_content_is_dropped():
    html = "<style>.x{color:red}</style><div>visible</div><script>var y = 1 < 2;</script>"
    assert html_to_text(html) == "visible"


def test_head_content_is_dropped():
    html = "<head><title>hidden title</title></head><body><p>body text</p></body>"
    assert html_to_text(html) == "body text"


# --- split: blockquote / gmail_quote / signature ------------------------------


def test_blockquote_is_quoted_rest_is_novel():
    html = "<div>my reply</div><blockquote><div>their words</div></blockquote>"
    parts = split_html_parts(html)
    assert parts.novel_text == "my reply"
    assert parts.quoted_text == "their words"
    assert parts.signature_text == ""


@pytest.mark.parametrize(
    "attr",
    [
        'class="gmail_quote"',
        'class="gmail_quote gmail_quote_container"',
        'class="moz-cite-prefix"',
        'class="OutlookMessageHeader"',
        'id="divRplyFwdMsg"',
        'id="appendonsend"',
    ],
)
def test_quote_container_hooks(attr):
    html = f"<div>mine</div><div {attr}>quoted stuff</div>"
    parts = split_html_parts(html)
    assert parts.novel_text == "mine"
    assert "quoted stuff" in parts.quoted_text


@pytest.mark.parametrize(
    "html",
    [
        '<div>body</div><div class="gmail_signature">my sig</div>',
        '<div>body</div><div class="moz-signature">my sig</div>',
        '<div>body</div><div id="Signature">my sig</div>',
    ],
)
def test_signature_container_hooks(html):
    parts = split_html_parts(html)
    assert parts.novel_text == "body"
    assert parts.signature_text == "my sig"
    assert parts.quoted_text == ""


def test_signature_inside_quote_counts_as_quoted():
    # A quoted message's own signature belongs to the quote, not to this author.
    html = (
        "<div>my reply</div>"
        "<blockquote><div>their words</div>"
        '<div class="gmail_signature">their sig</div></blockquote>'
        '<div class="gmail_signature">my sig</div>'
    )
    parts = split_html_parts(html)
    assert parts.novel_text == "my reply"
    assert "their words" in parts.quoted_text
    assert "their sig" in parts.quoted_text
    assert parts.signature_text == "my sig"


def test_nested_quotes_all_count_as_quoted():
    html = "<blockquote>outer<blockquote>inner</blockquote>more outer</blockquote>"
    parts = split_html_parts(html)
    assert parts.novel_text == ""
    for token in ("outer", "inner", "more outer"):
        assert token in parts.quoted_text


# --- malformed markup ---------------------------------------------------------


def test_unclosed_tags_do_not_raise_and_still_split():
    html = "<div>novel text<blockquote>quoted text with no close"
    parts = split_html_parts(html)
    assert "novel text" in parts.novel_text
    assert "quoted text with no close" in parts.quoted_text


def test_stray_close_tags_are_ignored():
    # A close with no matching open must not corrupt the depth counters.
    html = "</blockquote></div><div>still novel</div></blockquote>"
    parts = split_html_parts(html)
    assert parts.novel_text == "still novel"
    assert parts.quoted_text == ""


def test_text_across_a_quote_does_not_glue():
    # Novel text on both sides of a quote stays on separate lines (the quote's
    # structural break flushes the line even though its text is a different part).
    html = "<div>before</div><blockquote>middle</blockquote><div>after</div>"
    parts = split_html_parts(html)
    assert [ln for ln in parts.novel_text.split("\n") if ln] == ["before", "after"]
    assert "before" not in parts.quoted_text and "after" not in parts.quoted_text


@pytest.mark.parametrize("bad", ["", "   ", "<<<>>> & & &", "<b><i>no closes", "plain no tags"])
def test_empty_or_garbage_never_raises(bad):
    assert isinstance(html_to_text(bad), str)
    result = split_html_parts(bad)
    assert isinstance(result, HtmlParts)


def test_empty_input_yields_empty_parts():
    assert split_html_parts("") == HtmlParts("", "", "")
    assert html_to_text("") == ""
