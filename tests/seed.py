"""Deterministic seed data for the store-query and webapp tests.

Not a test module (no ``test_`` prefix, so pytest will not collect it). Both
``test_store_query.py`` and ``test_webapp.py`` build the same representative
database through :func:`seed` and assert against hand-computed aggregates, so the
numbers in the tests and the layout here must be read together.

Shape: 3 lists, 6 addresses (2 grouped into 2 persons, 4 unassigned but note two
addresses share the display name "Alice Smith" → one merge suggestion), 15
messages spread across 2026-01/02/03, extractions in every status
(ok/empty/too_short/failed plus two messages with no extraction row at all), and
scores across all four Pangram labels (AI / AI-Assisted / Human / Mixed) with one
``ok`` extraction deliberately left unscored.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mailing_list_ai_check.store import Store, sha256_text

# --- Address book -------------------------------------------------------------
# key -> (email, display_name)
_ADDRESSES = {
    "a1": ("alice@example.org", "Alice Smith"),
    "a2": ("alice@work.example", "Alice Smith"),  # shares display name with a1
    "a3": ("bob@example.org", "Bob Jones"),
    "a4": ("carol@example.org", "Carol"),
    "a5": ("dave@example.org", "Dave"),
    "a6": ("eve@example.org", "Eve"),
}

# --- Message specs ------------------------------------------------------------
# (key, list, address, date, subject, ext_status, label, fraction_ai, reply_to)
# ext_status None      -> no extraction row at all
# label None + ok      -> extracted but not scored
# label not None       -> scored with that label / fraction_ai
_MESSAGES = [
    ("m1", "announce", "a1", "2026-01-05T10:00:00", "Intro to draft", "ok", "AI", 0.95, None),
    ("m2", "announce", "a1", "2026-01-15T10:00:00", "Re: Intro to draft", "ok", "Human", 0.02, "m1"),
    ("m3", "announce", "a3", "2026-01-20T10:00:00", "Comments", "ok", "AI-Assisted", 0.55, None),
    ("m4", "announce", "a4", "2026-02-03T10:00:00", "Question", "ok", "Human", 0.10, None),
    ("m5", "announce", "a5", "2026-02-10T10:00:00", "Short note", "too_short", None, None, None),
    ("m6", "announce", "a6", "2026-02-25T10:00:00", "Empty msg", "empty", None, None, None),
    ("m7", "announce", "a2", "2026-03-05T10:00:00", "Another draft", "ok", "Mixed", 0.40, None),
    ("m8", "last-call", "a3", "2026-01-08T10:00:00", "Last call review", "ok", "AI", 0.88, None),
    ("m9", "last-call", "a4", "2026-02-12T10:00:00", "Objection", "ok", "Human", 0.05, None),
    ("m10", "last-call", "a5", "2026-02-18T10:00:00", "Support", "failed", None, None, None),
    (
        "m11",
        "last-call",
        "a1",
        "2026-03-10T10:00:00",
        "Re: Last call review",
        "ok",
        "AI-Assisted",
        0.60,
        None,
    ),
    ("m12", "last-call", "a6", "2026-03-20T10:00:00", "No extraction yet", None, None, None, None),
    ("m13", "quic", "a3", "2026-01-25T10:00:00", "QUIC perf", "ok", None, None, None),  # unscored
    ("m14", "quic", "a4", "2026-03-15T10:00:00", "QUIC question", "ok", "AI", 0.97, None),
    ("m15", "quic", "a2", "2026-03-28T10:00:00", "QUIC summary", None, None, None, None),
]

_LISTS = ["announce", "last-call", "quic"]


@dataclass
class Seed:
    """Database ids for the seeded rows, keyed by the spec keys above."""

    lists: dict[str, int] = field(default_factory=dict)
    persons: dict[str, int] = field(default_factory=dict)
    addresses: dict[str, int] = field(default_factory=dict)
    messages: dict[str, int] = field(default_factory=dict)
    message_ids: dict[str, str] = field(default_factory=dict)


def _extracted_text(status: str, subject: str) -> str:
    if status == "ok":
        return f"Body of {subject}"
    if status == "too_short":
        return "tiny"
    return ""  # empty / failed


def seed(store: Store) -> Seed:
    """Populate ``store`` with the fixture data and return the created ids."""
    s = Seed()

    for name in _LISTS:
        s.lists[name] = store.upsert_list(name, f"Shared Folders/{name}").id

    # Two persons; a1+a2 -> P1, a3 -> P2. a4/a5/a6 stay unassigned.
    s.persons["P1"] = store.create_person("Alice Smith").id
    s.persons["P2"] = store.create_person("Bob Jones").id

    for key, (email, display) in _ADDRESSES.items():
        s.addresses[key] = store.upsert_address(email, display).id
    store.assign_address_to_person(s.addresses["a1"], s.persons["P1"])
    store.assign_address_to_person(s.addresses["a2"], s.persons["P1"])
    store.assign_address_to_person(s.addresses["a3"], s.persons["P2"])

    for key, lst, addr, date, subject, status, label, frac, reply in _MESSAGES:
        message_id = f"<{key}@test>"
        s.message_ids[key] = message_id
        in_reply_to = s.message_ids[reply] if reply else None
        result = store.upsert_message(
            message_id=message_id,
            list_id=s.lists[lst],
            address_id=s.addresses[addr],
            subject=subject,
            date=date,
            in_reply_to=in_reply_to,
            raw_body=f"RAW {subject}",
            uid=None,
        )
        msg_id = result.message.id
        s.messages[key] = msg_id

        if status is None:
            continue
        text = _extracted_text(status, subject)
        extraction = store.insert_extraction(
            message_id=msg_id,
            extracted_text=text,
            method="email-reply-parser",
            status=status,
        )
        if label is None:
            continue
        store.insert_score(
            extraction_id=extraction.id,
            text_sha256=sha256_text(text),
            fraction_ai=frac,
            fraction_ai_assisted=(frac if label == "AI-Assisted" else 0.0),
            fraction_human=(1.0 - frac if frac is not None else None),
            label=label,
            detector_version="v3",
            raw_response={"prediction_short": label, "fraction_ai": frac, "windows": []},
        )

    return s
