"""Cleaning: prepare extracted new text for the Pangram AI detector.

This is **stage 2** of the two-stage pipeline. :mod:`extraction` (stage 1)
recovers the author's *novel content* — everything they wrote that is not quoted
or reproduced from elsewhere — but it deliberately keeps the author-typed but
formulaic *furniture*: greetings, sign-offs, signature blocks, mailing-list
footers, mobile-client taglines. This module removes that furniture so only
substantive prose reaches the detector, and — crucially — **reports which input
lines it removed** (:class:`CleanResult.ignored_lines`) so the dashboard can grey
them out in the extracted-text view.

Why furniture is removed before scoring
---------------------------------------
A scored ablation (2026-07-22, messages 42/44) showed that greetings,
sign-offs and signature blocks materially **mask AI-generated content** in the
Pangram check: removing a one-line greeting flipped one Mixed verdict to AI 0.84,
and removing a "Regards, / Name" sign-off nearly doubled another message's AI
fraction, zeroing its "AI-assisted" share in both cases. These fragments are
short, formulaic and human-written by construction, so leaving them in dilutes
the very signal the detector is looking for. They are therefore stripped from the
text that is scored — but only from the *scored* text: stage 1 still preserves
them, and the detail view can show the author exactly what was set aside.

The removal steps, in order (order matters — see the notes):

1. ``-- `` signature delimiter: that line and everything after it.
2. **Trailing sign-off debris** (bare links/domains after a sign-off, with the
   two-anchor rules) — run *before* step 3 because its bare-name anchor needs the
   identifier/contact lines still present as evidence; step 3 would erase them.
3. **Individually droppable signature debris**: "~ Name" sign-offs, titled
   contact lines, corporate contact lines (phone / piped address+URL),
   personal-identifier lines (ORCID/LinkedIn/…), mailing-list footers, PGP lines.
4. One opening greeting line (multilingual — see :data:`_GREETING_WORDS`).
5. Mobile/client taglines ("Sent from my iPhone", "Get Outlook for iOS", …).
6. **Trailing confidentiality/legal disclaimer paragraphs** ("This email and any
   attachments are confidential…") — trailing paragraphs only, scanned from the
   bottom, so a disclaimer-shaped sentence in the body is never touched.
7. One closing sign-off (salutation+name — the name may carry a title or
   affiliation ("Thi Nguyen-Huu | CEO", "Louis Navarre, UCLouvain") — the
   one-line form, or a bare trailing salutation). Multilingual salutations
   (German/French/Spanish/Dutch/Māori/…) are shared with stage 1's boundary
   detection in :mod:`extraction`.
8. **HTML signature hint** (optional): when the caller passes
   ``html_signature_text`` — the visible text of the message's HTML signature
   container (:func:`~html_text.split_html_parts`) — any body line whose
   normalized form exactly equals a normalized non-blank line of that hint is
   dropped individually, *provided* the line carries substance (≥ 2 words, or a
   digit / ``@`` / URL). The substance guard keeps a lone "Cheers"/"Thanks" that
   also appears in the signature container from being stripped out of the body.
   This catches signature furniture with no ``-- `` delimiter and no recognizable
   contact shape that only the HTML marked as a signature.

The sequence runs to a **fixpoint** (bounded): removing one layer can expose
another — a closing sign-off only becomes the tail once the contact block below
it is dropped.

The final ``text`` is the survivors ``rstrip``-ed, runs of 2+ blank lines
collapsed to one, and blank edges trimmed — the same conventions as stage 1's
tail. The shared sign-off predicates live in :mod:`extraction` (stage 1 needs
them for its sign-off *boundary*); this module imports them, never the reverse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .extraction import (
    _normalize_line_for_diff,
    _SIGNOFF_LINE_RE,
    _signoff_name_index,
    is_signoff_name_line,
)

# --- result -------------------------------------------------------------------


@dataclass(frozen=True)
class CleanResult:
    """The outcome of :func:`clean_for_scoring` on one stage-1 extraction.

    - ``text``: the furniture-free text that is sent to the detector.
    - ``ignored_lines``: 0-based indices into the *input* text's ``split("\\n")``
      of every **non-blank** line the clean pass removed. The dashboard uses
      these to grey out the corresponding lines of the extracted text; the
      contract is fixed (indices into ``extracted_text.split("\\n")``).
    """

    text: str
    ignored_lines: list[int]


# --- signature delimiter ------------------------------------------------------

# The RFC 3676 signature delimiter, "-- " (trailing space often stripped by
# clients). Deliberately does NOT match "--eh." or "-markku".
_SIG_DELIM_RE = re.compile(r"^--[ \t]*$")


def is_signature_delimiter(line: str) -> bool:
    """True for the ``-- `` signature delimiter (not "--eh." or "-markku")."""
    return bool(_SIG_DELIM_RE.match(line))


# --- individually droppable signature-debris lines ----------------------------

# A "~ Name" sign-off line.
_TILDE_SIGNOFF_RE = re.compile(r"^[ \t]*~[ \t]+\S")

# A titled personal contact line that is only a name and a bracketed email,
# e.g. "Dr. Markku-Juhani O. Saarinen <mjos@iki.fi>". The leading honorific keeps
# this from matching pasted header lines like "From: Name <a@b>".
_CONTACT_RE = re.compile(
    r"^[ \t]*(?:Dr|Prof|Mr|Mrs|Ms|Miss|PhD|Herr|Frau|Sir|Dame|Prof\.-Dr)\.?[ \t]+"
    r"\S.*<[^>]+@[^>]+>[ \t]*$"
)

# A telephony contact line: a phone keyword directly followed by a number, e.g.
# "Tel: +1 905.502.7000 x 3288  |  Toll Free: 888.879.5879". The number tail
# (5+ chars of digits/punctuation) keeps prose like "the cell 5 in row 2" safe.
_PHONE_LINE_RE = re.compile(
    r"\b(?:tel|telephone|phone|mobile|cell|fax|toll[ -]?free)\b[.:]?[ \t]*[+(]?\d[\d \t().x/-]{5,}",
    re.IGNORECASE,
)

# A cue that a pipe-separated line is corporate-signature contact/address
# furniture rather than prose or a digest table row: a URL, a phone keyword, a
# street-suffix word, or a postal code (Canadian "M9W 4Y8" / US "City, ST
# 12345"). A bare email is deliberately NOT a cue — digest tables legitimately
# hold "count | bytes | Name <a@b>" rows that must be kept. Extend the
# street-suffix / postal alternations for more locales as they show up.
_CONTACT_CUE_RE = re.compile(
    r"\bwww\.|https?://"
    r"|\b(?:tel|telephone|phone|mobile|cell|fax|toll[ -]?free)\b"
    r"|\b(?:blvd|boulevard|street|ave|avenue|suite|floor|road|drive|lane)\b"
    r"|\b[A-Za-z]\d[A-Za-z][ \t]?\d[A-Za-z]\d\b"
    r"|,[ \t]*[A-Z]{2}[ \t]+\d{5}\b",
    re.IGNORECASE,
)

# A personal-identifier contact line: an identity/social/contact keyword
# followed by a colon and a handle/id ("ORCID: 0009-0007-4602-5624",
# "Email: a@b"), or by a bracketed URL and nothing else ("LinkedIn
# <https://www.linkedin.com/in/…>" — the plain-text rendering of a hyperlink).
# The colon / lone-bracketed-URL requirement keeps prose ("Happy to upload
# these as GitHub issues") safe. Extend the keyword alternation as new
# services show up.
_IDENTIFIER_LINE_RE = re.compile(
    r"^[ \t]*(?:ORCID|LinkedIn|GitHub|GitLab|Mastodon|Bluesky|Twitter|Skype|Signal"
    r"|Telegram|WhatsApp|WeChat|Matrix|IRC|Website?|Homepage|Blog|E-?mail)"
    r"[ \t]*(?::[ \t]*\S|<https?://[^>]+>[ \t]*$)",
    re.IGNORECASE,
)

# Mailing-list footer debris.
_LIST_FOOTER_RE = re.compile(
    r"^[ \t]*(?:_{5,}|.*\bmailing list\b.*--.*|To unsubscribe\b.*)[ \t]*$",
    re.IGNORECASE,
)

# A PGP armor / key line.
_PGP_RE = re.compile(
    r"^[ \t]*(?:-----(?:BEGIN|END) PGP|PGP key-(?:id|fingerprint)\b)",
    re.IGNORECASE,
)


def _is_contact_signature_line(line: str) -> bool:
    """True for a corporate-signature contact line, dropped individually.

    Two shapes: a telephony line ("Tel: +1 905.502.7000 x 3288"), and a
    pipe-separated contact/address line where at least one contact cue (URL,
    phone keyword, street suffix, postal code) marks it as signature furniture.
    A pipe alone is not enough — a "Name | Title" sign-off line and digest
    table rows ("2 (16.7%) | 11728 ( 3.3%) | Name <a@b>") survive.
    """
    if _PHONE_LINE_RE.search(line):
        return True
    return "|" in line and bool(_CONTACT_CUE_RE.search(line))


def _is_droppable_signature_line(line: str) -> bool:
    """True for a line that is itself signature debris but not a text boundary.

    These are dropped individually (not "everything after"), so authored content
    that happens to follow — a "Ps." block after a "~ Name" sign-off — survives.
    """
    return bool(
        _TILDE_SIGNOFF_RE.match(line)
        or _CONTACT_RE.match(line)
        or _IDENTIFIER_LINE_RE.match(line)
        or _LIST_FOOTER_RE.match(line)
        or _PGP_RE.match(line)
        or _is_contact_signature_line(line)
    )


# --- trailing sign-off debris -------------------------------------------------

# A line that is nothing but a link, bare domain, or email address — the tail
# furniture of a personal signature ("kghoshworkid.github.io"). Only consulted
# *after* a sign-off by :func:`strip_trailing_signoff_debris`; a URL the author
# pastes on its own line mid-prose is never touched.
_LINK_ONLY_LINE_RE = re.compile(
    r"^[ \t]*(?:"
    r"https?://\S+"
    r"|www\.\S+"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|(?:[\w-]+\.){1,6}[A-Za-z]{2,24}(?:/\S*)?"
    r")[ \t]*$"
)


def strip_trailing_signoff_debris(lines: list[str]) -> list[str]:
    """Drop personal-signature furniture trailing a closing sign-off.

    When a sign-off is followed only by contact debris to the end of the
    message — identifier lines ("ORCID: …"), bare links/domains/emails — that
    tail is dropped and the sign-off itself kept. Requiring *every* remaining
    line to be debris means a "Ps." block or any authored prose after a
    sign-off keeps the whole tail intact.

    Two anchors, with different bars for the tail:

    - a full sign-off (salutation + name): any all-debris tail qualifies, so
      "Thanks, / Arun Thallapelly / OmniArx.ai" loses the bare domain;
    - a bare name line (no salutation — "Thanks, and looking forward. /
      Kunal Ghosh / ORCID: … / …"): the tail must also contain at least one
      identifier/contact line, so an authored ending of a name followed by a
      pasted link is never mistaken for a signature.
    """
    n = len(lines)
    for i in range(n):
        name_idx = _signoff_name_index(lines, i)
        need_droppable = False
        if name_idx is None:
            if not is_signoff_name_line(lines[i]):
                continue
            name_idx = i
            need_droppable = True
        tail = [line for line in lines[name_idx + 1 :] if line.strip()]
        if not tail:
            continue
        if all(
            _is_droppable_signature_line(line) or _LINK_ONLY_LINE_RE.match(line) for line in tail
        ) and (not need_droppable or any(_is_droppable_signature_line(line) for line in tail)):
            return lines[: name_idx + 1]
    return lines


# --- greeting / closing sign-off ----------------------------------------------

# An opening greeting line: a greeting word, then either a short run of names
# ending in "," / "!" / "." or nothing else. The mandatory trailing punctuation
# on the name-run form keeps prose like "Hey that reminds me of X" intact.
# Grouped by language; extend a group (or add one) as new greetings show up.
_GREETING_WORDS = (
    # English
    r"hi|hello|hey|dear|greetings|good[ \t]+(?:morning|afternoon|evening|day)"
    # German
    r"|hallo|guten[ \t]+(?:tag|morgen|abend)|liebe[rs]?|sehr[ \t]+geehrte[rs]?"
    # French / Spanish / Italian / Portuguese
    r"|bonjour|salut|ch(?:è|e)re?s?|hola|estimad[oa]s?|buenos[ \t]+d(?:í|i)as|ciao|ol(?:á|a)"
    # Dutch / Nordic
    r"|beste|geachte|hej|hoi"
    # Māori (common in NZ English mail)
    r"|kia[ \t]+ora|t(?:ē|e)n(?:ā|a)[ \t]+koe"
)
_GREETING_RE = re.compile(
    rf"^[ \t]*(?:{_GREETING_WORDS})\b(?:[^.!?;:]{{0,60}}[,!.]|[ \t]*[,!.]?)[ \t]*$",
    re.IGNORECASE,
)


def strip_greeting_lines(lines: list[str]) -> list[str]:
    """Drop an opening greeting line ("Hi Songbo, Karthik, all,") if present.

    Only the first non-blank line is considered — a greeting quoted or repeated
    mid-message is content, not framing.
    """
    idx = next((i for i, line in enumerate(lines) if line.strip()), None)
    if idx is not None and _GREETING_RE.match(lines[idx]):
        return lines[:idx] + lines[idx + 1 :]
    return lines


def strip_closing_signoff(lines: list[str]) -> list[str]:
    """Drop a closing sign-off — salutation(+name) — from the end of the text.

    Handles the two-line form ("Best, / Songbo"), the one-line form
    ("Cheers, Peter"), and a bare trailing salutation ("Cheers"). A bare
    trailing *name* with no salutation ("Dino") is kept — it is not
    distinguishable from content with enough confidence.
    """
    nonblank = [i for i, line in enumerate(lines) if line.strip()]
    if not nonblank:
        return lines
    last = nonblank[-1]
    for i in nonblank[-2:]:
        if _signoff_name_index(lines, i) == last:
            return lines[:i]
    if _SIGNOFF_LINE_RE.match(lines[last]):
        return lines[:last]
    return lines


# --- confidentiality / legal disclaimers ----------------------------------------

# The opening line of a boilerplate legal footer ("CONFIDENTIALITY NOTICE: …",
# "This email and any attachments are confidential…", "If you are not the
# intended recipient…"). Matched only at the START of a paragraph, and only for
# paragraphs in the message's trailing furniture region (see
# :func:`_drop_trailing_disclaimer_paragraphs`) — an author's own sentence like
# "This message is intended only for the design team" mid-mail is never touched.
_DISCLAIMER_START_RE = re.compile(
    r"^[ \t]*(?:"
    r"(?:CONFIDENTIALITY|LEGAL|PRIVACY)[ \t]+(?:NOTICE|NOTE|DISCLAIMER|STATEMENT)"
    r"|DISCLAIMER[ \t]*[:\-]"
    r"|This[ \t]+(?:e-?mail|message|communication)\b[^.]{0,120}?"
    r"\b(?:confidential|privileged|intended[ \t]+(?:only|solely)[ \t]+for)"
    r"|The[ \t]+information[ \t]+(?:contained|transmitted)\b[^.]{0,80}?"
    r"\b(?:confidential|privileged)"
    r"|If[ \t]+you[ \t]+(?:are[ \t]+not[ \t]+the[ \t]+intended[ \t]+recipient"
    r"|(?:have[ \t]+)?received[ \t]+this[ \t]+(?:e-?mail|message)[ \t]+in[ \t]+error)"
    r")",
    re.IGNORECASE,
)


# --- mobile / client taglines -------------------------------------------------

# A one-line client tagline appended by mobile or webmail clients. Anchored at
# line start; the alternation is deliberately extensible as new clients show up.
# ERP already treats "Sent from my …" as a signature boundary, so such a line
# often arrives inside a stage-1 signature fragment — but "Get Outlook for …"
# and "Sent from Proton Mail mobile" do not, so we drop them explicitly here.
_MOBILE_TAGLINE_RE = re.compile(
    r"^[ \t]*(?:"
    r"Sent from my \w+"  # Sent from my iPhone / iPad / Samsung / mobile device
    r"|Sent from Proton Mail"  # Sent from Proton Mail mobile
    r"|Sent from Mail for Windows"
    r"|Sent from Outlook"
    r"|Get Outlook for \w+"  # Get Outlook for iOS / Android
    r")",
    re.IGNORECASE,
)


def is_mobile_tagline(line: str) -> bool:
    """True for a mobile/webmail client tagline ("Sent from my iPhone")."""
    return bool(_MOBILE_TAGLINE_RE.match(line))


# --- public entry point -------------------------------------------------------

# Each step is a pure ``list -> list`` transform over (original-index, text)
# pairs, so the removed input-line indices fall out by comparing the surviving
# indices against the input. The truncating steps (delimiter, trailing debris,
# closing sign-off) all return a prefix of their input; the dropping steps
# (debris, greeting, taglines) drop individual lines.


def _truncate_at_signature_delimiter(pairs: list[tuple[int, str]]) -> list[tuple[int, str]]:
    for pos, (_idx, line) in enumerate(pairs):
        if is_signature_delimiter(line):
            return pairs[:pos]
    return pairs


def _strip_trailing_debris_pairs(pairs: list[tuple[int, str]]) -> list[tuple[int, str]]:
    kept = strip_trailing_signoff_debris([line for _idx, line in pairs])
    return pairs[: len(kept)]


def _drop_signature_debris(pairs: list[tuple[int, str]]) -> list[tuple[int, str]]:
    return [(idx, line) for idx, line in pairs if not _is_droppable_signature_line(line)]


def _strip_greeting_pairs(pairs: list[tuple[int, str]]) -> list[tuple[int, str]]:
    pos = next((p for p, (_idx, line) in enumerate(pairs) if line.strip()), None)
    if pos is not None and _GREETING_RE.match(pairs[pos][1]):
        return pairs[:pos] + pairs[pos + 1 :]
    return pairs


def _strip_closing_signoff_pairs(pairs: list[tuple[int, str]]) -> list[tuple[int, str]]:
    kept = strip_closing_signoff([line for _idx, line in pairs])
    return pairs[: len(kept)]


def _drop_mobile_taglines(pairs: list[tuple[int, str]]) -> list[tuple[int, str]]:
    return [(idx, line) for idx, line in pairs if not is_mobile_tagline(line)]


# A line that carries a URL, used with the HTML-signature-hint substance guard.
_URL_RE = re.compile(r"https?://|\bwww\.", re.IGNORECASE)


def _is_html_signature_line(line: str, hint_norms: frozenset[str]) -> bool:
    """True for a body line the HTML signature container marks as furniture.

    The line's normalized form (via extraction's :func:`_normalize_line_for_diff`,
    so quote markers and whitespace do not defeat the match) must exactly equal a
    normalized non-blank line of the hint, and it must carry substance — at least
    two words, or a digit, ``@`` or URL — so a lone salutation like "Cheers" that
    happens to sit in the signature container is not stripped out of the body.
    """
    norm = _normalize_line_for_diff(line)
    if not norm or norm not in hint_norms:
        return False
    return (
        len(norm.split()) >= 2
        or any(ch.isdigit() for ch in norm)
        or "@" in norm
        or bool(_URL_RE.search(norm))
    )


def _drop_html_signature_lines(
    pairs: list[tuple[int, str]], hint_norms: frozenset[str]
) -> list[tuple[int, str]]:
    if not hint_norms:
        return pairs
    return [(idx, line) for idx, line in pairs if not _is_html_signature_line(line, hint_norms)]


def _drop_trailing_disclaimer_paragraphs(pairs: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Drop legal/confidentiality boilerplate paragraphs from the tail.

    Paragraphs (blank-line-delimited blocks) are examined from the END of the
    text upward; a paragraph whose first line matches
    :data:`_DISCLAIMER_START_RE` is dropped, and the scan continues to the
    paragraph above it. The scan stops at the first trailing paragraph that is
    NOT a disclaimer, so a disclaimer-shaped sentence in the body of the message
    is never removed — only bottom boilerplate is.
    """
    # Build paragraphs as [start, end) index ranges over `pairs`.
    paragraphs: list[tuple[int, int]] = []
    start = None
    for pos, (_idx, line) in enumerate(pairs):
        if line.strip():
            if start is None:
                start = pos
        elif start is not None:
            paragraphs.append((start, pos))
            start = None
    if start is not None:
        paragraphs.append((start, len(pairs)))

    cut = None
    for p_start, _p_end in reversed(paragraphs):
        if _DISCLAIMER_START_RE.match(pairs[p_start][1]):
            cut = p_start
            continue
        break
    return pairs if cut is None else pairs[:cut]


def _clean_pass(
    pairs: list[tuple[int, str]], hint_norms: frozenset[str] = frozenset()
) -> list[tuple[int, str]]:
    """One full pass of every removal step, in order."""
    pairs = _truncate_at_signature_delimiter(pairs)
    # Trailing-debris before the per-line debris drop: its bare-name anchor needs
    # the identifier/contact lines still present as evidence.
    pairs = _strip_trailing_debris_pairs(pairs)
    pairs = _drop_signature_debris(pairs)
    # The HTML signature hint drops individually, alongside the debris drop.
    pairs = _drop_html_signature_lines(pairs, hint_norms)
    pairs = _strip_greeting_pairs(pairs)
    # Taglines and disclaimers go before the closing sign-off so a sign-off with
    # boilerplate below it ("Regards, / Bob / This email is confidential…") is
    # exposed as the true tail and removed in the same pass.
    pairs = _drop_mobile_taglines(pairs)
    pairs = _drop_trailing_disclaimer_paragraphs(pairs)
    pairs = _strip_closing_signoff_pairs(pairs)
    return pairs


#: Fixpoint cap for :func:`clean_for_scoring`. One pass usually suffices; a
#: second catches furniture exposed by the first (e.g. a sign-off that becomes
#: the tail only after a contact block below it is dropped). The cap is a
#: safety bound, not a tuning knob.
_MAX_CLEAN_PASSES = 4


def clean_for_scoring(extracted_text: str, html_signature_text: str | None = None) -> CleanResult:
    """Strip formulaic furniture from stage-1 ``extracted_text`` for scoring.

    Pure and never raises. Returns a :class:`CleanResult` with the scored
    ``text`` and the 0-based indices of every non-blank input line removed. When
    there is nothing to clean, ``text`` equals the trimmed input and
    ``ignored_lines`` is empty. The pass sequence runs to a fixpoint (bounded by
    :data:`_MAX_CLEAN_PASSES`): removing one layer of furniture can expose
    another (a closing sign-off only becomes the tail once the contact block
    under it is gone).

    ``html_signature_text`` is the optional visible text of the message's HTML
    signature container (see :func:`~html_text.split_html_parts`); its non-blank
    lines become a per-line drop hint (step 8 above). Lines it removes are
    reported in ``ignored_lines`` like any other furniture.
    """
    hint_norms = frozenset(
        norm
        for norm in (
            _normalize_line_for_diff(line) for line in (html_signature_text or "").split("\n")
        )
        if norm
    )

    original = extracted_text.split("\n")
    pairs: list[tuple[int, str]] = list(enumerate(original))

    for _ in range(_MAX_CLEAN_PASSES):
        before = len(pairs)
        pairs = _clean_pass(pairs, hint_norms)
        if len(pairs) == before:
            break

    kept_indices = {idx for idx, _line in pairs}
    ignored_lines = [i for i, line in enumerate(original) if line.strip() and i not in kept_indices]

    # Final formatting: rstrip each survivor, collapse runs of 2+ blank lines to
    # one, and trim blank edges (matching extraction's tail handling).
    lines = [line.rstrip() for _idx, line in pairs]
    collapsed: list[str] = []
    for line in lines:
        if not line and collapsed and not collapsed[-1]:
            continue
        collapsed.append(line)
    while collapsed and not collapsed[0]:
        collapsed.pop(0)
    while collapsed and not collapsed[-1]:
        collapsed.pop()

    return CleanResult(text="\n".join(collapsed), ignored_lines=ignored_lines)
