"""HTML-as-oracle: recover visible text and quote/signature structure from the
``text/html`` part of a message.

The extraction pipeline (:mod:`extraction`) works on the ``text/plain`` body, but
the HTML alternative frequently carries **structure the plain part has lost**, and
that structure is a reliable oracle for what is quoted, what is the author's
signature, and what is genuinely new:

- ``tests/fixtures/toppost-signoff-unquoted-wimse-01``: the plain part reproduces
  a ~1780-line quoted thread with **no** ``>`` markers, while the HTML wraps that
  same thread in 895 ``<blockquote>`` elements — so the HTML says exactly which
  lines are quoted.
- ``tests/fixtures/toppost-originalmsg-divider-agent2agent-01``: the plain part is
  three giant glued lines (the client dropped the newlines), while the HTML keeps
  the paragraph structure (28 ``<div>``, 29 ``<br>``).
- Several messages mark quoted/signature regions with the well-known
  ``gmail_quote`` / ``gmail_signature`` / ``OutlookMessageHeader`` / ``WordSection``
  class hooks even where no ``>`` marker survives.
- A number of stored messages are HTML-only and otherwise unscoreable, because
  stage 1 has no plain body to work from.

This module is **pure and stdlib-only** — it uses :mod:`html.parser`, which is
lenient with malformed markup, and it additionally guards itself so a broken
document never raises. Two public entry points:

- :func:`html_to_text` renders the visible text with block structure preserved as
  newlines (``<br>`` and block-level open/close), skipping script/style/head and
  comments, unescaping entities, and collapsing whitespace.
- :func:`split_html_parts` renders the same text but split into three streams —
  novel, quoted, and signature — by tracking which container each run of text
  falls inside. Callers use ``novel_text`` as a fresh body for stage 1
  (HTML-only / degenerate-plain cases), ``quoted_text`` as a parent-diff oracle,
  and ``signature_text`` as a stage-2 signature-stripping hint.

Container rules (see :func:`split_html_parts`).
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser

# --- categories ---------------------------------------------------------------

#: Text outside any recognized quoted/signature container — the author's own.
_NOVEL = "novel"
#: Text inside a quoted container (quoted message, forwarded header, cite prefix).
_QUOTED = "quoted"
#: Text inside a signature container that is not itself inside a quoted one.
_SIGNATURE = "signature"

# --- tag sets -----------------------------------------------------------------

#: Block-level tags whose open and close each force a line break, so paragraphs,
#: list items and table rows do not glue together in the rendered text.
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "tr",
        "table",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "pre",
        "ul",
        "ol",
    }
)

#: Void / empty elements that never have an end tag and so are never pushed onto
#: the container stack. ``<br>`` among them forces a line break.
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

#: Elements whose text content is never visible and must be dropped entirely.
_SKIP_TAGS = frozenset({"script", "style", "head"})

# --- container hooks ----------------------------------------------------------
# Structured as sets so more clients / hooks are easy to add. A quoted container
# nests: everything inside it (including a signature) counts as quoted, because a
# quoted message's own signature belongs to the quote, not to this author.

#: ``class`` tokens that mark a quoted container.
_QUOTED_CLASSES = frozenset(
    {
        "gmail_quote",  # Gmail's quoted-message wrapper
        "gmail_quote_container",  # newer Gmail wrapper
        "moz-cite-prefix",  # Thunderbird "On <date> X wrote:" prefix
        "OutlookMessageHeader",  # Outlook forwarded/quoted header block
    }
)
#: ``id`` values that mark a quoted container (Outlook web reply/forward anchors).
_QUOTED_IDS = frozenset({"divRplyFwdMsg", "appendonsend"})
#: ``class`` tokens that mark a signature container.
_SIGNATURE_CLASSES = frozenset(
    {
        "gmail_signature",  # Gmail signature block
        "moz-signature",  # Thunderbird signature block
        "Signature",  # Outlook signature (usually via id; see below)
    }
)
#: ``id`` value Outlook uses for its signature ``<div>``.
_SIGNATURE_ID = "Signature"


@dataclass(frozen=True)
class HtmlParts:
    """The visible text of an HTML part split by container category.

    Each field is rendered with the same block-structure and whitespace rules as
    :func:`html_to_text`, over only the runs of text in that category:

    - ``novel_text``: everything outside any quoted or signature container.
    - ``quoted_text``: everything inside a quoted container (nesting included).
    - ``signature_text``: everything inside a signature container that is not
      itself inside a quoted container.
    """

    novel_text: str
    quoted_text: str
    signature_text: str


# --- parser -------------------------------------------------------------------


class _Segmenter(HTMLParser):
    """Collect a flat list of text/newline segments, each tagged with its active
    container category, from an HTML document.

    Newline segments are category-less structural breaks (emitted for block open
    and close and for ``<br>``); when a single category is later rendered they
    still flush the current line, so text separated by a differently-categorized
    run never glues together. The container stack is defended against unclosed
    and stray tags so malformed markup cannot corrupt the depth counters.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # Each entry: (tag, category-or-None, is_skip).
        self._stack: list[tuple[str, str | None, bool]] = []
        self._quoted_depth = 0
        self._signature_depth = 0
        self._skip_depth = 0
        #: ``("nl",)`` or ``("text", category, string)``.
        self.segments: list[tuple[str, ...]] = []

    # -- category bookkeeping -------------------------------------------------

    def _active_category(self) -> str:
        if self._quoted_depth > 0:
            return _QUOTED
        if self._signature_depth > 0:
            return _SIGNATURE
        return _NOVEL

    @staticmethod
    def _container_category(tag: str, attrs: list[tuple[str, str | None]]) -> str | None:
        d = dict(attrs)
        classes = (d.get("class") or "").split()
        el_id = d.get("id") or ""
        if (
            tag == "blockquote"
            or any(c in _QUOTED_CLASSES for c in classes)
            or el_id in _QUOTED_IDS
        ):
            return _QUOTED
        if any(c in _SIGNATURE_CLASSES for c in classes) or el_id == _SIGNATURE_ID:
            return _SIGNATURE
        return None

    # -- HTMLParser hooks -----------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            self._stack.append((tag, None, True))
            return
        if tag in _VOID_TAGS:
            if tag == "br" and self._skip_depth == 0:
                self.segments.append(("nl",))
            return
        category = self._container_category(tag, attrs)
        self._stack.append((tag, category, False))
        if category == _QUOTED:
            self._quoted_depth += 1
        elif category == _SIGNATURE:
            self._signature_depth += 1
        # Emit the block break *after* pushing so the break is attributed to this
        # block's own region (irrelevant to text content, but keeps structure
        # tidy). Category-less newlines flush every stream anyway.
        if tag in _BLOCK_TAGS and self._skip_depth == 0:
            self.segments.append(("nl",))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_TAGS:
            return
        # Find the nearest matching open frame; ignore a stray close with none.
        idx = None
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                idx = i
                break
        if idx is None:
            return
        # Pop that frame and any unclosed inner frames, adjusting the counters.
        for _tag, category, is_skip in self._stack[idx:]:
            if is_skip:
                self._skip_depth -= 1
            elif category == _QUOTED:
                self._quoted_depth -= 1
            elif category == _SIGNATURE:
                self._signature_depth -= 1
        del self._stack[idx:]
        if tag in _BLOCK_TAGS and self._skip_depth == 0:
            self.segments.append(("nl",))

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        self.segments.append(("text", self._active_category(), data))


# --- rendering ----------------------------------------------------------------


def _render(segments: list[tuple[str, ...]], wanted: str | None) -> str:
    """Render ``segments`` to text, keeping only ``wanted``-category text runs.

    ``wanted=None`` keeps every category. Newline segments always flush the
    current line, so runs of a different category leave a break rather than
    gluing neighboring kept text together. Each line collapses its internal
    whitespace, runs of blank lines collapse to one, and blank edges are trimmed.
    """
    lines: list[str] = []
    buf: list[str] = []
    for seg in segments:
        if seg[0] == "nl":
            lines.append("".join(buf))
            buf = []
        else:
            _kind, category, text = seg
            if wanted is None or category == wanted:
                buf.append(text)
    lines.append("".join(buf))

    collapsed: list[str] = []
    for line in lines:
        line = " ".join(line.split())
        if not line and collapsed and not collapsed[-1]:
            continue
        collapsed.append(line)
    while collapsed and not collapsed[0]:
        collapsed.pop(0)
    while collapsed and not collapsed[-1]:
        collapsed.pop()
    return "\n".join(collapsed)


def _segment(html: str) -> list[tuple[str, ...]]:
    """Parse ``html`` into category-tagged segments; never raise on bad markup."""
    parser = _Segmenter()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - html.parser is lenient, but stay defensive
        pass
    return parser.segments


# --- public API ---------------------------------------------------------------


def html_to_text(html: str) -> str:
    """Return the visible text of ``html`` with block structure as newlines.

    ``<br>`` and block-level element open/close (``p``, ``div``, ``li``, ``tr``,
    ``table``, ``h1``–``h6``, ``blockquote``, ``pre``, ``ul``, ``ol``) become
    line breaks; ``script`` / ``style`` / ``head`` content and comments are
    dropped; character/entity references are unescaped; each line has its
    internal whitespace collapsed, blank runs collapse to one, and blank edges
    are trimmed. Never raises on malformed HTML (empty/blank input yields ``""``).
    """
    if not html or not html.strip():
        return ""
    return _render(_segment(html), None)


def split_html_parts(html: str) -> HtmlParts:
    """Split ``html`` into novel / quoted / signature visible-text streams.

    Rendering rules match :func:`html_to_text`; the difference is *which* text is
    kept in each stream, decided by the innermost enclosing container:

    - **Quoted** — everything inside a ``<blockquote>``; any element whose
      ``class`` list contains one of ``gmail_quote``, ``gmail_quote_container``,
      ``moz-cite-prefix``, ``OutlookMessageHeader``; or whose ``id`` is
      ``divRplyFwdMsg`` or ``appendonsend``. Quoted containers nest: text inside
      one is quoted even when a signature container sits between it and the text.
    - **Signature** — everything inside an element whose ``class`` list contains
      ``gmail_signature``, ``moz-signature`` or ``Signature`` (Outlook's
      ``<div id="Signature">`` matches on the id) — but only when it is **not**
      inside a quoted container, since a quoted message's signature is part of
      the quote.
    - **Novel** — everything else.

    Never raises on malformed HTML; empty/blank input yields three empty strings.
    """
    if not html or not html.strip():
        return HtmlParts(novel_text="", quoted_text="", signature_text="")
    segments = _segment(html)
    return HtmlParts(
        novel_text=_render(segments, _NOVEL),
        quoted_text=_render(segments, _QUOTED),
        signature_text=_render(segments, _SIGNATURE),
    )
