"""Unit tests for stage-2 cleaning (``clean_for_scoring``).

Stage 2 removes the author's formulaic furniture — greetings, sign-offs,
signature blocks, mailing-list footers, mobile taglines — from the stage-1
extracted text, and reports which non-blank input lines it removed
(``ignored_lines``) so the dashboard can grey them out. These tests target that
removal directly (via :func:`clean_for_scoring` and its helpers) plus the
signature-heavy fixtures at the composite (stage-1 + stage-2) level.
"""

from __future__ import annotations

import pathlib

import pytest

from mailing_list_ai_check.cleaning import (
    CleanResult,
    clean_for_scoring,
    is_mobile_tagline,
    is_signature_delimiter,
)
from mailing_list_ai_check.extraction import extract_new_text
from mailing_list_ai_check.fetcher import parse_message

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
EXPECTED_DIR = FIXTURE_DIR / "expected"


def fixture_body(stem: str) -> str | None:
    parsed = parse_message((FIXTURE_DIR / f"{stem}.eml").read_bytes(), uid=1, folder="x")
    return parsed.body


def expected_text(stem: str) -> str:
    return (EXPECTED_DIR / f"{stem}.txt").read_text(encoding="utf-8")


def tolerant_lines(text: str) -> list[str]:
    out: list[str] = []
    for line in text.split("\n"):
        line = line.replace("\xa0", " ").replace(" ", " ").strip()
        if line:
            out.append(line)
    return out


def scored(text: str) -> str:
    """The cleaned text alone (drops the ignored-line bookkeeping)."""
    return clean_for_scoring(text).text


# --- signature delimiter ------------------------------------------------------


@pytest.mark.parametrize("line", ["-- ", "--", "--   "])
def test_is_signature_delimiter_true(line):
    assert is_signature_delimiter(line)


@pytest.mark.parametrize("line", ["--eh.", "-markku", "- bullet", "-- eh"])
def test_is_signature_delimiter_false(line):
    assert not is_signature_delimiter(line)


def test_truncates_at_hard_delimiter():
    text = "Real content.\n-- \nSignature line\nmore signature"
    assert scored(text) == "Real content."


# --- individually droppable signature debris ----------------------------------


def test_keeps_ps_block_after_tilde_signoff():
    text = "Hi John,\n\nA correction.\n\n~ Christian Veenman (NCSC-NL)\n\nPs. keep me."
    cleaned = scored(text)
    assert "~ Christian" not in cleaned
    assert "Ps. keep me." in cleaned
    assert "A correction." in cleaned


def test_drops_titled_contact_line_not_header_line():
    dropped = scored("Body.\nDr. Markku-Juhani O. Saarinen <mjos@iki.fi>")
    assert dropped == "Body."
    kept = scored("Body.\nFrom: Timo Gerke <timo.gerke@alice-dsl.net>")
    assert "From: Timo Gerke" in kept


@pytest.mark.parametrize(
    "line",
    [
        # The message-27 corporate signature block, line by line.
        "Tel: +1 905.502.7000 x 3288  |  Toll Free: 888.879.5879",
        "thi.nh@winmagic.com<mailto:thi.nh@winmagic.com> |  www.winmagic.com<http://www.winmagic.com/>",
        "WinMagic Corp. | 11-80 Galaxy Blvd.",
        "Toronto, ON  |  M9W 4Y8 |  Canada | www.winmagic.com<http://www.winmagic.com/>",
        # A telephony line needs no pipe at all.
        "Phone: (415) 555-0100",
    ],
)
def test_drops_corporate_contact_line(line):
    assert scored(f"Body.\n{line}") == "Body."


@pytest.mark.parametrize(
    "line",
    [
        # A name-and-title sign-off has a pipe but no contact cue.
        "Thi Nguyen-Huu | CEO",
        # Digest table rows: pipes plus a bare email are NOT contact furniture.
        " 2 (16.7%) |  11728 ( 3.3%) | NomCom Chair 2026 <nomcom-chair-2026@example.org>",
        "   Count    |      Bytes     |  Who",
        # A URL the author pasted on its own line (no pipe) must be kept.
        "https://mailarchive.example.org/arch/msg/wimse/d0Ua5jt7Ekkya9AiwUtTi99o1kY",
        # Prose with a phone keyword but no number tail.
        "the cell 5 in row 2 is wrong",
    ],
)
def test_keeps_non_contact_line(line):
    assert line.strip() in scored(f"Body.\n{line}")


def test_drops_identifier_line_but_not_prose():
    assert scored("Body.\nORCID: 0009-0007-4602-5624") == "Body."
    assert scored("Body.\nEmail: kunal@example.org") == "Body."
    # The bracketed-hyperlink rendering ("LinkedIn <https://…>") is also debris.
    assert scored("Body.\nLinkedIn <https://www.linkedin.com/in/kunalghosh87/>") == "Body."
    kept = scored("Happy to upload any of these as GitHub issues if the authors prefer.")
    assert "GitHub issues" in kept


# --- trailing sign-off debris -------------------------------------------------


def test_trailing_signoff_debris_dropped_signoff_kept():
    # The message-44 shape: sign-off, then identifier + bare-domain lines to EOF.
    text = "The review is attached.\n\nRegards,\nKunal Ghosh\nORCID: 0009-0007-4602-5624\nkghoshworkid.github.io"
    cleaned = scored(text)
    # The debris is dropped by the trailing rule; the "Regards, / Kunal Ghosh"
    # framing then goes to the closing-sign-off strip.
    assert cleaned == "The review is attached."
    assert "ORCID" not in cleaned
    assert "github.io" not in cleaned


def test_trailing_link_kept_when_authored_content_follows():
    # A Ps. after the link means the tail is not all debris — nothing is dropped.
    text = "Reply.\n\nCheers,\nAlice\nalice.example.org\n\nPs. one more authored thought."
    cleaned = scored(text)
    assert "alice.example.org" in cleaned
    assert "Ps. one more authored thought." in cleaned


def test_link_line_mid_prose_is_kept():
    # A URL pasted on its own line before the sign-off is authored content.
    text = "See the draft:\nhttps://datatracker.example.org/doc/html/draft-foo-01\n\nOlivier"
    assert "https://datatracker.example.org" in scored(text)


def test_trailing_debris_after_bare_name_needs_identifier_evidence():
    # The message-29 shape: no salutation line, a bare name, then identifier +
    # link debris to EOF — the identifier lines license dropping the whole tail.
    text = (
        "Thanks, and looking forward to participating.\n\n"
        "Kunal Ghosh\nORCID: 0009-0007-4602-5624\nkghoshworkid.github.io\n"
        "LinkedIn <https://www.linkedin.com/in/kunalghosh87/>"
    )
    cleaned = scored(text)
    assert cleaned.endswith("Kunal Ghosh")
    assert "github.io" not in cleaned
    assert "LinkedIn" not in cleaned
    # But a bare name followed ONLY by a link (no identifier/contact evidence)
    # is plausibly an authored citation — nothing is dropped.
    kept = scored("Check out this paper by\nJohn Smith\nexample.com/paper.pdf")
    assert "example.com/paper.pdf" in kept


# --- greeting / closing sign-off strip ----------------------------------------


@pytest.mark.parametrize(
    "greeting",
    ["Hi Songbo, Karthik, all,", "Hi", "Hello,", "Dear Community,", "Good morning all,"],
)
def test_opening_greeting_is_stripped(greeting):
    assert scored(f"{greeting}\n\nThe actual content.") == "The actual content."


def test_greeting_only_stripped_at_top_and_prose_kept():
    # "Hey <prose>" with no terminating comma is content, not a greeting.
    kept = scored("Hey that reminds me of the earlier draft discussion")
    assert kept.startswith("Hey that reminds")
    # A greeting-looking line mid-message is content too.
    text = "First line of content.\nHi Roy,\nMore content."
    assert "Hi Roy," in scored(text)


@pytest.mark.parametrize(
    "signoff",
    ["Best,\nSongbo", "Cheers, Peter", "Regards,\n\nTimo Gerke", "Cheers"],
)
def test_closing_signoff_is_stripped(signoff):
    assert scored(f"The actual content.\n\n{signoff}") == "The actual content."


def test_bare_trailing_name_is_kept():
    # A name with no salutation ("Dino") is indistinguishable from content.
    assert scored("Content line.\n\nDino").endswith("Dino")


# --- confidentiality / legal disclaimers ----------------------------------------


def test_trailing_disclaimer_paragraph_is_dropped():
    text = (
        "Substantive reply content here.\n\n"
        "Regards,\nBob Smith\n\n"
        "This email and any attachments are confidential and intended solely for\n"
        "the addressee. If you are not the intended recipient, please notify the\n"
        "sender and delete this message."
    )
    out = scored(text)
    assert out == "Substantive reply content here."
    assert "confidential" not in out
    # The fixpoint matters: the sign-off only becomes the tail once the
    # disclaimer below it is dropped, and is then removed too.
    assert "Bob Smith" not in out


def test_multiple_trailing_disclaimer_paragraphs_dropped():
    text = (
        "Real content.\n\n"
        "CONFIDENTIALITY NOTICE: This message contains privileged information.\n\n"
        "If you received this email in error, please destroy all copies."
    )
    assert scored(text) == "Real content."


def test_disclaimer_shaped_sentence_mid_message_is_kept():
    # Only *trailing* paragraphs are scanned — an author's own sentence about
    # confidentiality in the body must survive.
    text = (
        "This message is intended only for the design team, so please do not\n"
        "forward it beyond the WG.\n\n"
        "Here is the actual technical question about the draft."
    )
    out = scored(text)
    assert "intended only for the design team" in out
    assert "actual technical question" in out


# --- multilingual greetings / sign-offs -----------------------------------------


@pytest.mark.parametrize(
    "greeting",
    [
        "Hallo Peter,",
        "Guten Tag,",
        "Bonjour à tous,",
        "Hola Maria,",
        "Kia ora koutou,",
        "Beste Jan,",
    ],
)
def test_non_english_greeting_is_stripped(greeting):
    assert scored(f"{greeting}\n\nThe actual content.") == "The actual content."


@pytest.mark.parametrize(
    "signoff",
    [
        "Mit freundlichen Grüßen,\nDennis Jackson",
        "Viele Grüße\nTimo",
        "Cordialement,\nLouis",
        "Saludos,\nAntonio",
        "Ngā mihi\nBrian Carpenter",
        "Met vriendelijke groeten,\nJan",
    ],
)
def test_non_english_closing_signoff_is_stripped(signoff):
    assert scored(f"The actual content.\n\n{signoff}") == "The actual content."


# --- name + title/affiliation sign-offs ------------------------------------------


@pytest.mark.parametrize(
    "signoff",
    ["Cheers\n\nThi Nguyen-Huu | CEO", "Best regards,\nLouis Navarre, UCLouvain"],
)
def test_closing_signoff_with_title_or_affiliation_is_stripped(signoff):
    assert scored(f"The actual content.\n\n{signoff}") == "The actual content."


def test_name_title_line_without_salutation_is_kept():
    # A "Name | Title" line is only sign-off furniture when a salutation
    # precedes it; on its own it is indistinguishable from content.
    assert "Thi Nguyen-Huu | CEO" in scored("Body.\n\nThi Nguyen-Huu | CEO")


# --- mobile / client taglines -------------------------------------------------


@pytest.mark.parametrize(
    "tagline",
    [
        "Sent from my iPhone",
        "Sent from my iPad",
        "Sent from my Samsung Galaxy smartphone",
        "Sent from my mobile device",
        "Get Outlook for iOS",
        "Get Outlook for Android",
        "Sent from Proton Mail mobile",
    ],
)
def test_mobile_tagline_recognized_and_dropped(tagline):
    assert is_mobile_tagline(tagline)
    text = f"Here is the substance of my reply.\n{tagline}"
    assert scored(text) == "Here is the substance of my reply."


def test_mobile_tagline_does_not_match_prose():
    assert not is_mobile_tagline("Sent the draft to the working group earlier today.")
    assert "Sent the draft" in scored("Sent the draft to the working group earlier today.")


# --- ignored_lines bookkeeping ------------------------------------------------


def test_ignored_lines_point_at_correct_nonblank_input_lines():
    # Lines (0-based): 0 greeting, 1 blank, 2-3 content, 4 blank, 5-6 sign-off,
    # 7 identifier debris. The cleaner should report 0, 5, 6, 7 as ignored and
    # never a blank line (1, 4).
    text = (
        "Hi team,\n"
        "\n"
        "Here is my substantive review of the draft proposal today.\n"
        "It covers three separate points worth discussing in detail here.\n"
        "\n"
        "Best,\n"
        "Alice\n"
        "ORCID: 0000-0001-2345-6789"
    )
    result = clean_for_scoring(text)
    assert result.ignored_lines == [0, 5, 6, 7]
    # Every reported index is a non-blank input line.
    input_lines = text.split("\n")
    assert all(input_lines[i].strip() for i in result.ignored_lines)
    # The surviving text is the two content lines only.
    assert result.text == (
        "Here is my substantive review of the draft proposal today.\n"
        "It covers three separate points worth discussing in detail here."
    )


def test_nothing_to_clean_returns_identical_text_and_no_ignored_lines():
    text = "This is a plain paragraph.\nWith a second line and no furniture at all."
    result = clean_for_scoring(text)
    assert isinstance(result, CleanResult)
    assert result.text == text
    assert result.ignored_lines == []


def test_clean_never_raises_on_empty_input():
    assert clean_for_scoring("") == CleanResult(text="", ignored_lines=[])
    assert clean_for_scoring("   \n\n").ignored_lines == []


# --- HTML signature hint (step 8) ---------------------------------------------


def test_html_signature_hint_drops_matching_lines():
    # Signature furniture with no "-- " delimiter and no recognizable contact
    # shape, but the HTML marked it as a signature. The hint drops those lines.
    text = (
        "Here is my substantive review of the draft, which spans a few lines.\n"
        "It raises a couple of concrete points worth discussing on the list.\n"
        "Jane Researcher\n"
        "Principal Engineer, Example Networks\n"
        "jane@example.org"
    )
    hint = "Jane Researcher\nPrincipal Engineer, Example Networks\njane@example.org"
    result = clean_for_scoring(text, hint)
    assert "Jane Researcher" not in result.text
    assert "Principal Engineer, Example Networks" not in result.text
    assert "jane@example.org" not in result.text
    assert "substantive review of the draft" in result.text


def test_html_signature_hint_keeps_lone_salutation_word():
    # The substance guard: a lone "Cheers"/"Thanks" that happens to sit in the
    # signature container must NOT be stripped out of the body by the hint (a
    # single word, no digit/@/URL). (The closing-sign-off strip may still take a
    # true trailing sign-off, but the hint step itself must spare it.)
    text = "Thanks\nfor all the detailed feedback you shared on the draft revision today."
    hint = "Thanks\nJane Researcher"
    result = clean_for_scoring(text, hint)
    # "Thanks" is not a full sign-off here (no following name line), so it stays.
    assert result.text.startswith("Thanks")


def test_html_signature_hint_ignored_lines_include_hint_removals():
    # Hint-removed lines must be reported in ignored_lines like other furniture,
    # and blank lines are never reported.
    text = (
        "Substantive content line one of the review here for the group.\n"  # 0 kept
        "Substantive content line two of the review here for the group.\n"  # 1 kept
        "\n"  # 2 blank
        "Custom Signature Line With Details\n"  # 3 hint-dropped
        "contact: person@example.org"  # 4 hint-dropped (has @)
    )
    hint = "Custom Signature Line With Details\ncontact: person@example.org"
    result = clean_for_scoring(text, hint)
    assert result.ignored_lines == [3, 4]
    input_lines = text.split("\n")
    assert all(input_lines[i].strip() for i in result.ignored_lines)
    assert result.text == (
        "Substantive content line one of the review here for the group.\n"
        "Substantive content line two of the review here for the group."
    )


def test_html_signature_hint_none_is_a_noop():
    text = "A plain paragraph with no furniture.\nAnd a second content line here."
    assert clean_for_scoring(text, None) == clean_for_scoring(text)


# --- signature-heavy fixtures (composite stage-1 + stage-2) -------------------


def test_signature_identifier_links_fixture_extracts_clean():
    """The message-44 defect: an ORCID line and a bare personal-domain line
    trailing the "Regards, / Kunal Ghosh" sign-off must not ship to scoring.
    """
    stem = "signature-identifier-links-wimse-01"
    result = extract_new_text(fixture_body(stem))
    assert result.status == "ok"
    composite = clean_for_scoring(result.text).text
    # Sign-off framing and identifier debris both go; content is the last line.
    assert composite.strip().splitlines()[-1].startswith("Happy to upload")
    assert "ORCID" not in composite
    assert "kghoshworkid.github.io" not in composite
    assert "GitHub issues" in composite  # prose mentioning GitHub is kept
    assert tolerant_lines(composite) == tolerant_lines(expected_text(stem))


def test_signature_corporate_contact_fixture_drops_contact_block():
    """The message-27 defect: a corporate contact block (phone / piped
    address+URL lines) must not ship to scoring. With the fixpoint pass, the
    "Cheers" + "Name | Title" sign-off exposed by dropping the block is removed
    too — the scored text ends at the author's last content line.
    """
    stem = "signature-corporate-contact-wimse-01"
    result = extract_new_text(fixture_body(stem))
    assert result.status == "ok"
    composite = clean_for_scoring(result.text).text
    assert composite.strip().splitlines()[-1].startswith("You asked why the human binds")
    assert "Thi Nguyen-Huu | CEO" not in composite
    assert "Cheers" not in composite
    assert "Toll Free" not in composite
    assert "Galaxy Blvd" not in composite
    assert "M9W 4Y8" not in composite
    assert "www.winmagic.com" not in composite
    assert tolerant_lines(composite) == tolerant_lines(expected_text(stem))
