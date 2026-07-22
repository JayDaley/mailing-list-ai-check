"""New-text extraction: recover the author's *novel content* (stage 1 of 2).

Strategy (decided in ``docs/findings/extraction.md``): **email-reply-parser as
the primary extractor, followed by a small custom cleanup pass.** Talon is not
used. The public entry point is the pure function :func:`extract_new_text`,
which takes a decoded ``text/plain`` body and returns an :class:`ExtractionResult`
(``text``, ``method``, ``status``) with no I/O — so the quality tests run without
a database.

This module is **stage 1** of a two-stage pipeline. Stage 1 recovers everything
the author put in the message that is *not* quoted or reproduced from elsewhere:
it removes quoted lines, attribution lines, forwarded/quote-header blocks, the
quoted thread after a sign-off boundary, and (optionally) parent-diff content —
but it **keeps** the author's own framing furniture (greetings, sign-offs,
signature blocks, mailing-list footers, mobile taglines). Removing that furniture
is **stage 2**'s job (:mod:`cleaning`, run just before scoring), which also
reports what it removed so the dashboard can show it. Keeping furniture in stage
1 means the stored "extracted text" is the author's full novel content, and the
scored text is a strict, documented subset of it.

The custom pass fixes, in a general way, the cases where raw email-reply-parser
(ERP) diverges from hand-labeled ground truth:

- **Pre-normalization** strips a byte-order mark and zero-width characters and
  converts CRLF/CR to LF *before* ERP runs. This alone lets ERP recognize a
  leading ``>`` that a BOM would otherwise hide.
- **ERP fragments, signatures kept**: the primary path joins ERP's fragments
  that are not ``quoted`` — *including* fragments ERP labels ``signature`` — so a
  signature block survives stage 1 (stage 2 removes it). This is the one place
  stage 1 keeps more than ``EmailReplyParser.parse_reply`` would, which hides
  signature fragments outright.
- **Attribution lines** ("On <date>, X wrote:", "Name <a@b> wrote:", and the
  German "Am <date> schrieb X:") are removed, including two-line-wrapped forms.
  The pattern lists are structured so more languages are easy to add.
- **Indented quote blocks** — lines that are optional whitespace then one or more
  ``>`` (which ERP misses when the marker is not in column 0) — are dropped.
- **Sign-off boundary**: a salutation line ("Best," / "Cheers" / "Thanks," …)
  plus a name line, directly followed by an attribution line, ends the new text —
  everything after the name is the quoted message even when the client added no
  ``>`` markers (Gmail-style fully top-posted replies, typical of AI-generated
  mail). The name is required: a bare mid-thread "Thanks." never truncates. The
  sign-off *itself* survives stage 1 (this rule removes only the quoted thread
  the sign-off precedes); stage 2 strips the sign-off.
- **Forwarded / quote-header blocks**: an Outlook-style ``From:``/``Sent:``/
  ``To:``/``Subject:`` block introducing quoted content with no ``>`` markers,
  and everything after it, is dropped.
- **"Original message" dividers**: the dashed ``-------- Original message
  --------`` (or ``-----Original Message-----`` / "Forwarded message") divider
  and everything after it is dropped — including when HTML-to-text conversion
  glues the divider mid-line onto the author's last sentence, where only the
  author's prefix before the divider is kept.
- **Over-strip guard**: dashed-separator digest bodies make ERP treat the ``---``
  rule as a signature boundary and truncate. When ERP keeps far fewer of the
  body's plainly-unquoted lines than the body actually has, we discard the ERP
  output and run the custom cleanup on the whole body instead.
- **Parent-diff assist** (optional): when the reply's thread parent is available,
  the caller passes ``parent_body`` and — *after* the guard has resolved the
  chosen text — any content that provably came from the parent is removed. This
  catches fully top-posted replies whose quoted previous message carries no
  ``>`` markers at all (so the quote/attribution/header filters see nothing to
  drop), using stdlib ``difflib`` as an evidence-based backstop. See
  :func:`strip_parent_content`; it adds the ``"+parent-diff"`` method suffix.

HTML as a structural oracle (optional ``html_body``)
----------------------------------------------------
When the caller passes the decoded ``text/html`` part, its structure (see
:mod:`html_text`) is used three ways. The resolved method makes the HTML source
visible so it is auditable in the dashboard:

a. **HTML-only** — the plain body is missing or blank but an HTML part exists:
   the HTML's novel text (:func:`~html_text.split_html_parts`) becomes the body
   for the normal stage-1 pipeline, and the method is prefixed ``html-`` (e.g.
   ``html-erp+custom``).
b. **Degenerate plain fallback** — the plain body survived but is flattened
   (fewer than 4 non-blank lines, at least one over 400 chars) and the HTML
   novel text has at least 3× as many non-blank lines: the HTML path (a) is used
   instead of the mangled plain body. The ``toppost-originalmsg-divider`` fixture
   triggers this.
c. **Oracle assist** — the plain body is fine: the normal plain pipeline runs,
   then any content provably present in the HTML's quoted text
   (:func:`strip_parent_content`, same thresholds as the parent-diff assist) is
   removed. When it drops a **block** of content lines, ``+html-quote`` is
   appended. The assist only fires for a substantial removal
   (:data:`_HTML_QUOTE_MIN_REMOVED` non-blank lines): its target is a whole
   quoted message leaked into the plain part unmarked, not the odd author line
   that happens to cite the same wording as the thread (a mail client wraps
   *interleaved* author replies inside the same ``gmail_quote`` container as the
   quote, and an author quoting a proposed sentence inline matches its verbatim
   copy in a real ``<blockquote>`` — both would otherwise cost genuine content).

The word-count "too short" gate is deliberately **not** applied here — that is
the scoring stage's job, and it applies to the stage-2 *cleaned* text.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from email_reply_parser import EmailReplyParser

from .html_text import split_html_parts

# --- result -------------------------------------------------------------------

#: Nothing left to score (blank body, HTML-only message, or a body that reduced
#: to only quotes/signatures).
STATUS_EMPTY = "empty"
#: Text was extracted.
STATUS_OK = "ok"
#: Extraction raised — the raw body is preserved so nothing is silently lost.
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class ExtractionResult:
    """The outcome of running :func:`extract_new_text` on one body.

    This is the **stage-1** result: the author's full novel content, still
    including their greetings, sign-offs and signature blocks. The scored text
    is a subset produced by :func:`cleaning.clean_for_scoring`.

    - ``text``: the author's newly written text, furniture included (may be
      empty).
    - ``method``: which path produced it — ``"none"`` (no body), ``"erp"``
      (email-reply-parser output needed no cleanup), ``"erp+custom"`` (ERP plus
      the custom pass), ``"custom-fallback"`` (over-strip guard fired; custom
      pass on the whole body), or ``"failed"``. When a thread parent was supplied
      and the parent-diff assist removed content, ``"+parent-diff"`` is appended
      to whichever of the above produced the text (e.g. ``"erp+parent-diff"``).
      When the text was derived from the HTML part (HTML-only or degenerate-plain
      fallback) the method is **prefixed** ``"html-"`` (e.g. ``"html-erp+custom"``);
      when the HTML quoted-text oracle removed content from a plain extraction,
      ``"+html-quote"`` is appended.
    - ``status``: one of :data:`STATUS_OK`, :data:`STATUS_EMPTY`,
      :data:`STATUS_FAILED` (a subset of ``store.EXTRACTION_STATUSES``).
    """

    text: str
    method: str
    status: str


# --- pre-normalization --------------------------------------------------------

#: Characters removed before parsing: BOM / zero-width space / zero-width
#: non-joiner / zero-width joiner / word-joiner.
_ZERO_WIDTH = "﻿​‌‍⁠"
_ZERO_WIDTH_RE = re.compile(f"[{_ZERO_WIDTH}]")


def normalize_body(body: str) -> str:
    """Normalize line endings and strip BOM / zero-width characters.

    Runs before email-reply-parser so a leading BOM cannot hide a ``>`` quote
    marker (the documented ``interleaved-bom`` fixture).
    """
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    return _ZERO_WIDTH_RE.sub("", body)


# --- line classifiers ---------------------------------------------------------

# A quoted line: optional indentation, an optional run of leader dots (the
# ``...>`` form that survives format=flowed rewrapping), then one or more ``>``.
_QUOTE_RE = re.compile(r"^[ \t]*(?:\.{2,}[ \t]*)?>+")


def is_quote_line(line: str) -> bool:
    """True for a quoted line (any indentation; also the ``...>`` flowed form)."""
    return bool(_QUOTE_RE.match(line))


# Signature-debris classifiers (the ``-- `` delimiter, "~ Name" sign-offs,
# titled/corporate contact lines, personal-identifier lines, list footers, PGP)
# and the greeting / closing sign-off strippers moved to :mod:`cleaning` — they
# remove author-typed furniture for scoring, not quoted material, so they belong
# to stage 2. The sign-off *boundary* below stays: it removes the quoted thread.


# --- attribution lines --------------------------------------------------------

# Full single-line attribution forms. Add a language by adding a pattern.
_ATTRIBUTION_RES = (
    # English: "On <date>, <who> wrote:"
    re.compile(r"^[ \t]*On\b.*\bwrote:[ \t]*$"),
    # "<who> <a@b> wrote:" (no leading "On")
    re.compile(r"^.*<[^>]+@[^>]+>[ \t]+wrote:[ \t]*$"),
    # "<who> via Datatracker <a@b> wrote:" and similar are covered by the above.
    # German: "Am <date> schrieb <who>:"
    re.compile(r"^[ \t]*Am\b.*\bschrieb\b.*:[ \t]*$"),
)

# For two-line-wrapped attributions: a start token and a terminator that may
# appear on the following line(s). Structured per language for easy extension.
_ATTRIBUTION_START_RE = re.compile(r"^[ \t]*(?:On|Am|Le|El|El día)\b")
_ATTRIBUTION_END_RE = re.compile(r"(?:\bwrote:|\bschrieb\b.*:|\ba écrit\s*:|\bescribió:)[ \t]*$")


def is_attribution_line(line: str) -> bool:
    """True if ``line`` on its own is a complete attribution line."""
    return any(pat.match(line) for pat in _ATTRIBUTION_RES)


def _wrapped_attribution_end(lines: list[str], i: int) -> int | None:
    """If ``lines[i]`` opens a wrapped attribution — a start line ("On …" /
    "Am …") whose terminator ("wrote:" / "schrieb …:") lands on the next line or
    two, with no blank between — return the terminator line's index, else None.
    """
    if not _ATTRIBUTION_START_RE.match(lines[i]) or _ATTRIBUTION_END_RE.search(lines[i]):
        return None
    for j in range(i + 1, min(i + 3, len(lines))):
        if not lines[j].strip():
            return None
        if _ATTRIBUTION_END_RE.search(lines[j]):
            return j
    return None


def strip_attribution_lines(lines: list[str]) -> list[str]:
    """Drop attribution lines, including forms wrapped across up to two lines."""
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if is_attribution_line(lines[i]):
            i += 1
            continue
        end = _wrapped_attribution_end(lines, i)
        if end is not None:
            i = end + 1
            continue
        out.append(lines[i])
        i += 1
    return out


# --- sign-off boundary ----------------------------------------------------------

# Fully top-posted replies — Gmail's default, and the typical shape of
# AI-generated mail — end with a short salutation plus the author's name, then an
# attribution line introducing the quoted message *without* ``>`` markers. The
# quote/attribution filters see nothing to drop there, so without this boundary
# the entire quoted thread ships as "new text". A salutation alone is weak (a
# mid-thread "Thanks." is common prose); the name line is the strong signal, and
# the attribution that follows is what proves quoted material comes next.

# A line that is only a sign-off salutation (optional trailing , . or !).
# Ordered longest-first; add a salutation (or a language) by extending the list.
_SIGNOFF_SALUTATIONS = (
    # English
    r"all\s+the\s+best|best\s+(?:regards|wishes)|kind\s+regards|warm(?:est)?\s+regards|"
    r"many\s+thanks|thanks\s+again|thank\s+you|yours(?:\s+(?:sincerely|truly|faithfully))?|"
    r"best|cheers|thanks|thx|regards|sincerely|respectfully|cordially|br|rgds"
    # German
    r"|mit\s+freundlichen\s+gr(?:ü|ue)(?:ß|ss)en|freundliche\s+gr(?:ü|ue)(?:ß|ss)e|"
    r"(?:viele|beste|liebe)\s+gr(?:ü|ue)(?:ß|ss)e|gr(?:ü|ue)(?:ß|ss)e?|mfg"
    # French
    r"|(?:bien\s+)?cordialement|amicalement|amiti(?:é|e)s|salutations"
    # Spanish
    r"|saludos(?:\s+cordiales)?|un\s+saludo|atentamente"
    # Dutch
    r"|met\s+vriendelijke\s+groet(?:en)?|groet(?:en|jes)?|mvg"
    # Māori (common in NZ English mail)
    r"|ng(?:ā|a)\s+mihi(?:\s+nui)?|noho\s+ora\s+mai"
    # Nordic / other
    r"|v(?:ä|a)nliga\s+h(?:ä|a)lsningar|med\s+v(?:e|ä)nlig\s+h(?:i|ä)lsen|mvh|terveisin"
    r"|cumprimentos|atenciosamente|ciao|saluti|cordiali\s+saluti"
)
_SIGNOFF_LINE_RE = re.compile(
    rf"^[ \t]*(?:{_SIGNOFF_SALUTATIONS})[ \t]*[,.!]?[ \t]*$", re.IGNORECASE
)

# The one-line form: "Cheers, Peter" — salutation, comma, then the name.
_SIGNOFF_INLINE_RE = re.compile(
    rf"^[ \t]*(?:{_SIGNOFF_SALUTATIONS})[ \t]*,[ \t]*(?P<name>\S.*)$", re.IGNORECASE
)

# One word of a personal name: starts with a letter, then letters and the
# joiners that appear in names ("Nguyen-Huu", "O'Brien", "J."). Digits are
# rejected separately in :func:`is_signoff_name_line`.
_NAME_TOKEN_RE = re.compile(r"^[^\W\d_][\w'’.\-]*$")


def is_signoff_name_line(text: str) -> bool:
    """True if ``text`` is plausibly just a person's name (1–4 capitalized-ish
    words, no digits, short). Case-marked scripts only — the first token must
    start uppercase, which keeps ordinary prose from qualifying.
    """
    s = text.strip()
    if not s or len(s) > 40 or any(ch.isdigit() for ch in s):
        return False
    tokens = s.split()
    if not 1 <= len(tokens) <= 4:
        return False
    if not tokens[0][0].isupper():
        return False
    return all(_NAME_TOKEN_RE.match(tok) for tok in tokens)


# A name line with a short title/affiliation tail after "|" or "," —
# "Thi Nguyen-Huu | CEO", "Louis Navarre, UCLouvain". Only meaningful where a
# sign-off is expected; on its own such a line is indistinguishable from prose.
_NAME_TITLE_SEP_RE = re.compile(r"[|,]")


def _is_signoff_name_title_line(text: str) -> bool:
    """True for a sign-off name line carrying a title/affiliation tail."""
    parts = _NAME_TITLE_SEP_RE.split(text.strip(), maxsplit=1)
    if len(parts) != 2:
        return False
    name, title = parts[0].strip(), parts[1].strip()
    if not title or len(title) > 40 or len(title.split()) > 4:
        return False
    return is_signoff_name_line(name)


def _is_signoff_name_like(text: str) -> bool:
    """A plausible sign-off name line: bare name, or name + title/affiliation."""
    return is_signoff_name_line(text) or _is_signoff_name_title_line(text)


def _signoff_name_index(lines: list[str], i: int) -> int | None:
    """If a sign-off opens at ``lines[i]``, return the index of its name line.

    Both forms are recognized: a salutation line whose name follows on the next
    non-blank line ("Best,\\n\\nSongbo" → the "Songbo" index), and the one-line
    form ("Cheers, Peter" → ``i`` itself). The name may carry a title/affiliation
    tail ("Thi Nguyen-Huu | CEO"). ``None`` when ``lines[i]`` is not a sign-off
    with a plausible name.
    """
    if _SIGNOFF_LINE_RE.match(lines[i]):
        j = i + 1
        n = len(lines)
        while j < n and not lines[j].strip():
            j += 1
        if j < n and _is_signoff_name_like(lines[j]):
            return j
        return None
    m = _SIGNOFF_INLINE_RE.match(lines[i])
    if m and _is_signoff_name_like(m.group("name")):
        return i
    return None


def find_signoff_boundary(lines: list[str]) -> int | None:
    """Return the index just past a sign-off (salutation + name) that is directly
    followed by an attribution line, or ``None``.

    All three parts are required, so neither a mid-thread "Thanks." (no name), a
    sign-off at the true end of a message (nothing after), nor a sign-off
    followed by authored content (a "Ps." block) ever truncates. Interleaved
    replies are safe too: there the attribution precedes ``>``-quoted text and
    the sign-off comes last.
    """
    n = len(lines)
    for i in range(n):
        name_idx = _signoff_name_index(lines, i)
        if name_idx is None:
            continue
        k = name_idx + 1
        while k < n and not lines[k].strip():
            k += 1
        if k >= n:
            continue
        if is_attribution_line(lines[k]) or _wrapped_attribution_end(lines, k) is not None:
            return name_idx + 1
    return None


def strip_after_signoff_boundary(lines: list[str]) -> list[str]:
    """Truncate everything after a sign-off that an attribution line follows.

    This removes the *quoted thread* the sign-off precedes — the sign-off line
    itself is kept in stage 1 and removed later by :mod:`cleaning`.
    """
    idx = find_signoff_boundary(lines)
    return lines if idx is None else lines[:idx]


# --- forwarded / top-post quote-header blocks ---------------------------------

# Outlook, and forwarding clients generally, introduce quoted content with a
# block of pseudo-RFC5322 headers instead of "> " markers — e.g.
#
#     From: Wesley Eddy via Datatracker <noreply@example.org>
#     Sent: Tuesday, June 23, 2026 3:45 PM
#     To: tsv-art@example.org
#     Subject: [Int-area] … Tsvart review
#
# followed by the entire quoted message with *no* leading ``>``. Everything from
# such a block to the end of the body is quoted content the author did not write.

# The block always opens with a ``From:`` line.
_FROM_LINE_RE = re.compile(r"^[ \t]*From:[ \t]*\S")

# Any RFC5322-looking header field ("Name: …" / "Name:"). Used to (a) recognize
# that a ``From:`` sits *inside* an existing header run — as with the pasted
# header "evidence" in the threadstarter-rfc2047-header fixture, where every
# From: is preceded by another header line, so it is not the top of a genuine
# quote header — and (b) walk the run once a real block has started. Requiring
# whitespace or end-of-line after the colon keeps "https://…" and other
# scheme-like prose (no space after the colon) from matching.
_HEADER_FIELD_RE = re.compile(r"^[ \t]*[A-Za-z][A-Za-z-]{0,40}:(?:[ \t]|$)")

# The fields that mark a run of headers as an email quote header (rather than a
# stray "Foo:" line). Structured as an alternation so more clients / languages
# are easy to add (German: Gesendet/An/Betreff).
_QUOTE_HEADER_SIGNAL_RE = re.compile(
    r"^[ \t]*(?:Sent|Date|To|Cc|Bcc|Reply-To|Subject"
    r"|Gesendet|An|Betreff)[ \t]*:(?:[ \t]|$)",
    re.IGNORECASE,
)


def find_quote_header_block(lines: list[str]) -> int | None:
    """Return the index of the ``From:`` line that opens a forwarded/quote-header
    block, or ``None`` if the body has no such block.

    A qualifying block: a ``From:`` line that is the *top* of its header run
    (the line above it is blank, absent, or not itself a header field), followed
    — allowing folded continuation lines — by at least one more header line, at
    least one of which is a quote-header signal field (``Sent:``/``Date:``/
    ``To:``/``Cc:``/``Subject:``/localized). The "top of the run" condition is
    what distinguishes a real Outlook header (``From:`` first, after a blank line)
    from pasted header *evidence* embedded in prose, where a ``From:`` is
    preceded by ``Message-ID:``/``References:``/``In-Reply-To:`` — mirroring the
    honorific-anchored "contact vs pasted From: header" distinction in
    :data:`_CONTACT_RE`.
    """
    n = len(lines)
    for i, line in enumerate(lines):
        if not _FROM_LINE_RE.match(line):
            continue
        # The From: must open the run: the previous line must not be a header.
        if i > 0 and _HEADER_FIELD_RE.match(lines[i - 1]):
            continue
        header_lines = 1
        has_signal = False
        j = i + 1
        while j < n:
            nxt = lines[j]
            if _HEADER_FIELD_RE.match(nxt):
                header_lines += 1
                if _QUOTE_HEADER_SIGNAL_RE.match(nxt):
                    has_signal = True
                j += 1
            elif nxt[:1] in (" ", "\t") and nxt.strip():
                # Folded continuation of the preceding header field.
                j += 1
            else:
                break
        if header_lines >= 2 and has_signal:
            return i
    return None


def strip_after_quote_header_block(lines: list[str]) -> list[str]:
    """Truncate everything from a forwarded/quote-header block to the end."""
    idx = find_quote_header_block(lines)
    return lines if idx is None else lines[:idx]


# --- custom cleanup pass ------------------------------------------------------


# --- "Original message" / "Forwarded message" dividers -------------------------

# The old-fashioned dashed divider some clients (Android Mail, Samsung, and
# Outlook's "-----Original Message-----") put above the quoted message instead
# of ``>`` markers or a bare header block. Everything from the divider on is the
# quoted message. Matched with ``search`` rather than ``match``: HTML-to-text
# conversion sometimes flattens the whole quoted mail onto ONE line, gluing the
# divider to the end of the author's text ("…questions.-------- Original
# message --------From: …"), so the divider can sit mid-line — the author's
# prefix before it is kept. Dashes are required on BOTH sides, so prose that
# merely mentions an "original message" never matches.
_ORIGINAL_MESSAGE_DIVIDER_RE = re.compile(
    r"-{2,}[ \t]*(?:Original|Forwarded)[ \t]+Message[ \t]*-{2,}",
    re.IGNORECASE,
)


def strip_after_original_message_divider(lines: list[str]) -> list[str]:
    """Truncate everything from a dashed "Original/Forwarded message" divider.

    The line holding the divider is dropped too, except that a non-empty author
    prefix glued before a mid-line divider survives.
    """
    for i, line in enumerate(lines):
        m = _ORIGINAL_MESSAGE_DIVIDER_RE.search(line)
        if m is None:
            continue
        out = lines[:i]
        prefix = line[: m.start()].rstrip()
        if prefix:
            out.append(prefix)
        return out
    return lines


def custom_clean(text: str) -> str:
    """Apply the stage-1 quote/attribution cleanup to ``text``.

    Composable steps, in order: truncate at a forwarded/quote-header block, an
    "Original message" divider, or a sign-off boundary (which must run while the
    attribution evidence is still present), drop attribution lines (incl.
    wrapped forms), then filter out quoted lines. Signature blocks, greetings
    and sign-offs are **kept** — they are the author's own furniture and are
    removed later by :mod:`cleaning`. Blank-line structure is left as-is; the
    tolerant test comparison ignores it.
    """
    lines = text.split("\n")
    lines = strip_after_quote_header_block(lines)
    lines = strip_after_original_message_divider(lines)
    lines = strip_after_signoff_boundary(lines)
    lines = strip_attribution_lines(lines)
    lines = [line for line in lines if not is_quote_line(line)]
    # Trim per-line trailing whitespace and blank edge lines, but preserve the
    # first content line's indentation (significant for digest tables).
    lines = [line.rstrip() for line in lines]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


# --- over-strip guard ---------------------------------------------------------

#: If the primary path keeps fewer than this fraction of the body's plainly
#: unquoted content lines, treat it as an over-strip and fall back. Calibrated so
#: the dashed-separator digests and the pasted-headers fixture fall back while
#: genuinely quote-heavy replies do not.
_GUARD_MIN_RETAINED = 0.6


def count_unquoted_content_lines(body: str) -> int:
    """Count non-blank body lines that are plainly authored content.

    This is the denominator of the over-strip guard, so it must mirror what
    stage 1 now keeps: it excludes quoted lines, attribution lines, and
    everything after a forwarded/quote-header block or a sign-off boundary — but
    it **no longer** excludes the signature region, greetings or sign-offs,
    because stage 1 keeps those (ERP's signature fragments are retained, and the
    ``-- `` truncation moved to :mod:`cleaning`). Mirroring the numerator this way
    keeps the guard from misfiring on ordinary signed mail while still catching
    the dashed-separator digests, where ERP truncates most of the body.
    """
    lines = strip_after_quote_header_block(body.split("\n"))
    lines = strip_after_original_message_divider(lines)
    lines = strip_after_signoff_boundary(lines)
    count = 0
    for line in lines:
        if not line.strip():
            continue
        if is_quote_line(line) or is_attribution_line(line):
            continue
        count += 1
    return count


def _nonblank_line_count(text: str) -> int:
    return sum(1 for line in text.split("\n") if line.strip())


# --- parent-diff assist -------------------------------------------------------

# Some clients top-post a reply above the *entire* previous message reproduced
# with no ``>`` markers, no attribution, and no forwarded-header block — the
# quote/attribution/header filters have nothing to grab. When the thread parent
# is on hand, we can instead prove which child lines came from it by diffing.
#
# stdlib ``difflib.SequenceMatcher`` is exactly the right tool here — line- and
# word-level longest-matching-block detection over two sequences — so no
# third-party diff dependency is warranted. The bar for calling a line "parent
# content" is deliberately high: a matching block must carry real substance
# (see the two thresholds below), so short coincidental echoes that also appear
# in the parent — greetings ("Hi all,"), the author's own sign-off salutation
# and name line ("Best," / "Songbo"), list courtesy phrases — are NOT deleted.
# The author's sign-off in particular is guaranteed to also sit inside the
# parent's nested quotes, and it must survive.

#: An aligned run of matching lines is parent content only with this much
#: substance: the whole block totals at least this many normalized words ...
_PARENT_DIFF_MIN_BLOCK_WORDS = 10
#: ... OR the block contains at least one line of at least this many words. The
#: same per-line floor gates the rewrap rule (a single long line is strong
#: evidence on its own).
_PARENT_DIFF_MIN_LINE_WORDS = 8


def _normalize_line_for_diff(line: str) -> str:
    """Normalize one line for parent/child comparison.

    Strip a leading quote-marker prefix (via :data:`_QUOTE_RE`, so a quoted copy
    of a parent line compares equal to the bare parent line), then collapse all
    whitespace runs to single spaces and strip the edges. Blank/whitespace-only
    lines normalize to the empty string and are ignored by callers.
    """
    return " ".join(_QUOTE_RE.sub("", line).split())


def strip_parent_content(text: str, parent_body: str) -> str:
    """Remove lines of ``text`` that provably came from ``parent_body``.

    Pure and I/O-free; never raises (empty or whitespace-only inputs yield a
    sensibly trimmed result). Both sides are run through :func:`normalize_body`
    and then :func:`_normalize_line_for_diff` per line; blank lines are ignored
    when matching. A child line is marked as parent content by either rule:

    a) **Aligned-run rule** — over the normalized non-blank line sequences,
       :class:`difflib.SequenceMatcher` (``autojunk=False``) finds matching
       blocks; a block marks its child lines only when it carries substance:
       at least :data:`_PARENT_DIFF_MIN_BLOCK_WORDS` words in total, or at least
       one line of :data:`_PARENT_DIFF_MIN_LINE_WORDS` words. Short coincidental
       echoes (greetings, the author's own sign-off + name) fall below both and
       survive.
    b) **Rewrap rule** — quoting clients re-wrap long paragraphs at a different
       width, so line-level matching misses them. Any not-yet-marked child line
       of at least :data:`_PARENT_DIFF_MIN_LINE_WORDS` words whose normalized
       text is a substring of the parent's normalized word-stream (all parent
       lines joined by single spaces) is marked too.

    Marked lines are dropped; the survivors are ``rstrip``-ed, any run of 2+
    blank lines left by removals collapses to one, and leading/trailing blank
    lines are trimmed (matching :func:`custom_clean`'s edge handling).
    """
    child_raw = normalize_body(text).split("\n")
    child_norm = [_normalize_line_for_diff(line) for line in child_raw]
    parent_norm = [
        _normalize_line_for_diff(line) for line in normalize_body(parent_body).split("\n")
    ]

    # Non-blank normalized sequences; child keeps its original-line index so a
    # match can be mapped back to the raw line to drop.
    child_pairs = [(i, norm) for i, norm in enumerate(child_norm) if norm]
    child_seq = [norm for _, norm in child_pairs]
    parent_seq = [norm for norm in parent_norm if norm]

    marked: set[int] = set()

    # Rule (a): aligned runs of matching lines with enough substance.
    matcher = difflib.SequenceMatcher(a=child_seq, b=parent_seq, autojunk=False)
    for start, _b, size in matcher.get_matching_blocks():
        if size == 0:
            continue
        block = child_seq[start : start + size]
        total_words = sum(len(line.split()) for line in block)
        max_line_words = max(len(line.split()) for line in block)
        if (
            total_words >= _PARENT_DIFF_MIN_BLOCK_WORDS
            or max_line_words >= _PARENT_DIFF_MIN_LINE_WORDS
        ):
            for k in range(start, start + size):
                marked.add(child_pairs[k][0])

    # Rule (b): a long child line re-wrapped from the parent (line-level match
    # missed it) but still present verbatim in the parent's word-stream.
    parent_stream = " ".join(parent_seq)
    for orig_i, norm in child_pairs:
        if orig_i in marked:
            continue
        if len(norm.split()) >= _PARENT_DIFF_MIN_LINE_WORDS and norm in parent_stream:
            marked.add(orig_i)

    survivors = [line.rstrip() for i, line in enumerate(child_raw) if i not in marked]

    # Collapse 2+ consecutive blank lines (left by removals) into one, then trim
    # blank edges the same way custom_clean does.
    collapsed: list[str] = []
    for line in survivors:
        if not line and collapsed and not collapsed[-1]:
            continue
        collapsed.append(line)
    while collapsed and not collapsed[0]:
        collapsed.pop(0)
    while collapsed and not collapsed[-1]:
        collapsed.pop()
    return "\n".join(collapsed)


# --- public entry point -------------------------------------------------------


def _core_extract(body: str, parent_body: str | None) -> tuple[str, str]:
    """Run the ERP + custom + over-strip-guard + parent-diff pipeline.

    ``body`` is a non-blank decoded body (from ``text/plain`` or, for the HTML
    paths, the HTML's novel text). Returns ``(text, method)`` where ``method`` is
    unprefixed (``extract_new_text`` adds any ``html-`` prefix). Pure; the caller
    owns the try/except and the empty/status decision.
    """
    normalized = normalize_body(body)

    # Primary: join every ERP fragment that is not quoted — *including*
    # fragments ERP labels as a signature, which stage 1 must keep (stage 2
    # removes them). This is why we read fragments directly instead of
    # ``parse_reply``, which drops signature fragments. Forwarded quote-header
    # blocks arrive as non-quoted "header" fragments here; the custom pass
    # (``strip_after_quote_header_block`` / sign-off boundary) truncates them.
    message = EmailReplyParser.read(normalized)
    erp_out = "\n".join(frag.content for frag in message.fragments if not frag.quoted)
    cleaned = custom_clean(erp_out)

    # Over-strip guard: dashed-separator digests make ERP truncate at the
    # first "---" rule. If the primary path dropped most of the body's
    # plainly-unquoted lines, redo the cleanup on the whole body.
    unquoted = count_unquoted_content_lines(normalized)
    if unquoted > 0 and _nonblank_line_count(cleaned) < _GUARD_MIN_RETAINED * unquoted:
        fallback = custom_clean(normalized)
        if fallback.strip():
            text = fallback
            method = "custom-fallback"
        else:
            text = cleaned
            method = "erp+custom" if cleaned != erp_out.strip() else "erp"
    else:
        text = cleaned
        method = "erp+custom" if cleaned != erp_out.strip() else "erp"

    # Parent-diff assist: runs *after* guard resolution (so it never
    # perturbs the over-strip guard's denominator). We adopt the assisted text
    # (and the "+parent-diff" suffix) only when it actually dropped a content
    # line: strip_parent_content also collapses internal blank runs, so a plain
    # string comparison would fire — mutating the text and mislabeling the
    # method — on a parent that removed nothing.
    if parent_body is not None and text.strip():
        assisted = strip_parent_content(text, parent_body)
        if _nonblank_line_count(assisted) < _nonblank_line_count(text):
            text = assisted
            method += "+parent-diff"

    return text, method


#: Minimum non-blank lines the HTML quoted-text oracle must remove from a plain
#: extraction before its result is adopted (path (c) of :func:`extract_new_text`).
#: The oracle targets an entire quoted message leaked into the plain part with no
#: ``>`` markers — a large removal. A 1–2 line removal is almost always a
#: coincidental match (an author's interleaved reply that a client wrapped inside
#: the quote container, or an inline citation of a sentence that also appears
#: verbatim in a real ``<blockquote>``), so requiring a block keeps such author
#: content — content the hand-labeled corpus keeps — from being stripped.
_HTML_QUOTE_MIN_REMOVED = 3


def _is_degenerate_plain(body: str, html_novel: str) -> bool:
    """True when ``body`` looks like a flattened plain part best replaced by HTML.

    The heuristic (see path (b) of :func:`extract_new_text`): fewer than 4
    non-blank lines, at least one line over 400 chars (a client that dropped the
    newlines glued paragraphs into one giant line), and the HTML's novel text has
    at least 3× as many non-blank lines to fall back to.
    """
    lines = [line for line in body.split("\n") if line.strip()]
    if not lines or len(lines) >= 4:
        return False
    if not any(len(line) > 400 for line in lines):
        return False
    return _nonblank_line_count(html_novel) >= 3 * len(lines)


def extract_new_text(
    body: str | None,
    parent_body: str | None = None,
    html_body: str | None = None,
) -> ExtractionResult:
    """Extract the author's newly written text from a decoded ``text/plain`` body.

    Pure and I/O-free. ``None`` or a blank body with no usable HTML yields an
    empty result. Any unexpected error is caught and reported as
    :data:`STATUS_FAILED` rather than raised, so a single bad message never
    stalls the pipeline.

    When ``parent_body`` (the thread parent's raw body, resolved by the caller
    from ``In-Reply-To``) is supplied, the parent-diff assist runs after the
    primary/guard logic has chosen the text and removes content that provably
    came from the parent (see :func:`strip_parent_content`), appending
    ``"+parent-diff"`` to the method. The default ``None`` is a behavioral no-op.

    When ``html_body`` (the decoded ``text/html`` part) is supplied, the HTML
    structure serves as an oracle in one of three ways — HTML-only, degenerate
    plain fallback, or quoted-text oracle assist — described in the module
    docstring. The default ``None`` leaves the plain-only behavior unchanged, so
    every existing caller and the whole no-HTML corpus are unaffected.
    """
    try:
        parts = None
        if html_body is not None and html_body.strip():
            parts = split_html_parts(html_body)

        # (a) HTML-only: no usable plain body, but an HTML part to derive from.
        if body is None or not body.strip():
            if parts is not None and parts.novel_text.strip():
                text, method = _core_extract(parts.novel_text, parent_body)
                return _result("html-" + method, text)
            return ExtractionResult(text="", method="none", status=STATUS_EMPTY)

        # (b) Degenerate plain: a flattened plain body the HTML can replace.
        if parts is not None and _is_degenerate_plain(body, parts.novel_text):
            text, method = _core_extract(parts.novel_text, parent_body)
            return _result("html-" + method, text)

        # Normal plain path.
        text, method = _core_extract(body, parent_body)

        # (c) Oracle assist: remove plain-body content provably present in the
        # HTML's quoted text. As with the parent-diff assist, the trigger is the
        # non-blank-line count (never a string compare, which incidental blank-run
        # reformatting would trip), but here it must clear a block threshold so an
        # isolated coincidental match never strips genuine author content.
        if parts is not None and text.strip() and parts.quoted_text.strip():
            assisted = strip_parent_content(text, parts.quoted_text)
            if (
                _nonblank_line_count(text) - _nonblank_line_count(assisted)
                >= _HTML_QUOTE_MIN_REMOVED
            ):
                text = assisted
                method += "+html-quote"

        return _result(method, text)
    except Exception:  # noqa: BLE001 - never let one message stall the pipeline
        return ExtractionResult(text="", method="failed", status=STATUS_FAILED)


def _result(method: str, text: str) -> ExtractionResult:
    """Wrap resolved ``(method, text)`` in a result with the right empty status.

    A legitimately empty extraction (e.g. an exact re-send of the parent) keeps
    its resolved ``method`` but reports :data:`STATUS_EMPTY`.
    """
    if not text.strip():
        return ExtractionResult(text="", method=method, status=STATUS_EMPTY)
    return ExtractionResult(text=text, method=method, status=STATUS_OK)
