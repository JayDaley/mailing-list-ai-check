"""Auto-generated mail detection: list exclusions and per-message classification.

The rules come from a survey of the IETF IMAP archive (2026-08-12) over all
messages received since 2025-07-01 — 400 active lists, 142,826 messages, 9,772
sampled headers. Method and per-rule numbers are recorded in
``docs/findings/auto-generated.md``.

Two layers:

- **List exclusions** (:func:`is_excluded_list`): lists that carry only
  machine-generated or broadcast traffic. ``--all-lists`` pulls skip them;
  explicitly named lists are always honoured.
- **Message classification** (:func:`classify_message`): header rules applied
  to every fetched message. The result (a reason slug, or ``None`` for human
  mail) is stored on the message row; flagged messages are excluded from
  extraction and therefore never scored.

IESG ballot positions are deliberately *not* flagged even though the
datatracker delivers them with ``Auto-Submitted: auto-generated``: the ballot
text is written by the balloting Area Director. They are the only datatracker
mail addressed to ``iesg@ietf.org``, which is what the carve-out keys on.
"""

from __future__ import annotations

import re
from email.message import Message
from email.utils import getaddresses, parseaddr

#: Reason slugs stored in ``messages.auto_generated``.
REASON_AUTO_SUBMITTED = "auto-submitted"
REASON_PRECEDENCE_BULK = "precedence-bulk"
REASON_ROBOT_SENDER = "robot-sender"
REASON_NVN_FORWARD = "nvn-forward"

#: Lists whose entire traffic is machine-generated announcements or mirrors.
EXCLUDED_LISTS = frozenset(
    {
        "dmarc-report",  # DMARC aggregate reports from mail servers
        "i-d-announce",  # datatracker I-D Action announcements
        "ietf-announce",  # Last Calls, actions, RFC announcements
        "irtf-announce",  # announcement-only
        "rfc-dist",  # RFC announcements
        "new-wg-docs",  # internet-drafts@ietf.org notifications
        "ipr-announce",  # IPR disclosure notifications
        "iesg-agenda-dist",  # telechat agendas
        "netmod-ver-dt",  # GitHub notification mirror
        "quic-issues",  # GitHub issue/PR mirror
    }
)

#: Per-meeting broadcast lists (secretariat mail, not discussion): ``123all``,
#: ``124attendees``, ``125-newparticipants``, and the rolling
#: ``recentattendees``.
_MEETING_LIST_RE = re.compile(r"^\d{2,4}(all|attendees|-newparticipants)$")

#: Senders whose mail is machine-generated but carries no RFC 3834 or
#: Precedence header (measured in the survey): the RFC Editor's announcement
#: and errata tooling, GitHub notification mail, and the weekly GitHub digest
#: service posting to WG lists.
ROBOT_SENDERS = frozenset(
    {
        "rfc-editor@rfc-editor.org",
        "noreply@github.com",
        "notifications@github.com",
        "do_not_reply@mnot.net",
    }
)

_BULK_PRECEDENCE = frozenset({"bulk", "junk", "auto_reply"})

_IESG_ADDRESS = "iesg@ietf.org"
_DATATRACKER_SENDER = "noreply@ietf.org"


def is_excluded_list(name: str) -> bool:
    """True when list ``name`` should be skipped by an ``--all-lists`` pull."""
    lowered = name.lower()
    if lowered in EXCLUDED_LISTS or lowered == "recentattendees":
        return True
    return _MEETING_LIST_RE.match(lowered) is not None


def _header(msg: Message, name: str) -> str:
    value = msg.get(name)
    return str(value).strip() if value is not None else ""


def _is_iesg_ballot(msg: Message, from_email: str) -> bool:
    """True for a datatracker-delivered IESG ballot position.

    Ballot positions are the only datatracker (``noreply@ietf.org``) mail
    addressed to ``The IESG <iesg@ietf.org>``; the ballot text itself is
    human-written, so these are kept for scoring.
    """
    if from_email != _DATATRACKER_SENDER:
        return False
    recipients = getaddresses([_header(msg, "To")]) if msg.get("To") else []
    return any(addr.strip().lower() == _IESG_ADDRESS for _name, addr in recipients)


def classify_message(msg: Message) -> str | None:
    """Classify a parsed message: a reason slug when auto-generated, else ``None``.

    Rules, in order (the ballot carve-out must precede the header rules
    because ballot mail also carries ``Auto-Submitted: auto-generated``):

    1. IESG ballot positions are kept (see :func:`_is_iesg_ballot`).
    2. ``Auto-Submitted`` with any value other than ``no`` (RFC 3834).
    3. ``Precedence: bulk`` (or ``junk``/``auto_reply``) — IANA ticket mail.
    4. A known robot sender, or any ``mailer-daemon@`` address.
    5. A non-reply whose subject carries a forwarded datatracker
       "New Version Notification for" template.
    """
    from_email = parseaddr(_header(msg, "From"))[1].strip().lower()

    if _is_iesg_ballot(msg, from_email):
        return None

    auto_submitted = _header(msg, "Auto-Submitted").lower()
    if auto_submitted and auto_submitted != "no" and not auto_submitted.startswith("no "):
        return REASON_AUTO_SUBMITTED

    precedence = _header(msg, "Precedence").lower()
    if precedence in _BULK_PRECEDENCE:
        return REASON_PRECEDENCE_BULK

    if from_email in ROBOT_SENDERS or from_email.startswith("mailer-daemon@"):
        return REASON_ROBOT_SENDER

    if not msg.get("In-Reply-To"):
        subject = _header(msg, "Subject").lower()
        if "new version notification for" in subject:
            return REASON_NVN_FORWARD

    return None
