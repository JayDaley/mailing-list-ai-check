# Export / import design

> **Includes the app/pipeline versioning scheme** (see "Versioning" below):
> the app carries a semantic version, every message records the pipeline
> version that last processed it, exports carry both, and imports from a later
> pipeline version refresh a message's extraction/score without touching the
> message itself.

Move everything related to a list's messages — the messages themselves, what
was extracted from each, and what Pangram was sent and returned — between
databases as a single portable file, without ever corrupting the target
database on re-import.

## Goals

- Export all data for one or more named lists: list row, pull cursor, sender
  addresses and person groupings, messages, extractions, scores.
- Keep the file small: messages are static, so derived text is stored as
  **pointers into the message body** where possible, not as duplicate text.
- Import is **idempotent and collision-safe**: a message already present in
  the target (same RFC 5322 Message-ID on the same list) is skipped, along
  with its extraction/score, so importing the same file twice — or importing
  into the database it came from — is a no-op.
- Import is **all-or-nothing**: one transaction, rolled back on any error, so
  a truncated or malformed file can never leave a half-imported database.

## What "everything related to that message" means

| Data | Exported as |
|---|---|
| `lists` row | full row (keyed by `folder`) |
| `pull_state` row | full row (imported only if the target has none for the list) |
| `messages` rows | full rows incl. `raw_body`, `raw_html` |
| sender `addresses` | full rows (keyed by `email`) for addresses referenced by exported messages |
| `persons` | canonical name + membership, for persons referenced by those addresses |
| `extractions` | metadata + a text **pointer** (see below) |
| `scores` | full row: fractions, label, detector version, `raw_response` (the complete Pangram JSON), `text_sha256`, timestamps |

What was *sent* to Pangram is the cleaned text, which the pipeline derives on
the fly and never persists; the database (and therefore the export) carries
its SHA-256 (`scores.text_sha256`) — a stable pointer to it — plus the full
raw response. The cleaned text itself is reproducible from the extraction via
`cleaning.clean_for_scoring`.

## Versioning

The app uses [semantic versioning](https://semver.org/), starting at
**1.0.0**. The single source of truth is
`mailing_list_ai_check.__version__` (``__init__.py``); `pyproject.toml`
reads it dynamically (`dynamic = ["version"]` +
`[tool.setuptools.dynamic]`), so the two can never drift.

Bump policy (also recorded in `CLAUDE.md` and the README):

- **minor** (1.0.0 → 1.1.0): any change to extraction or post-extraction
  processing — `extraction.py`, `cleaning.py`, `html_text.py`, or the
  scoring pipeline logic — i.e. anything that could change what text is
  derived from a message or what is sent to Pangram.
- **patch** (1.0.0 → 1.0.1): every other change, for now.

### Per-message pipeline version

`messages.pipeline_version` (TEXT, nullable; migration 007) records the app
version that last ran a pipeline stage end-to-end against the message:

- stamped with the current version when the message is inserted
  (`Store.upsert_message`, `pipeline_version` parameter defaulting to the
  package version — tests may pass an explicit value);
- re-stamped to the current version by `Store.insert_extraction` and
  `Store.insert_score` (each updates the owning message's row), so after a
  full pull → extract → score run the column holds the version that
  completed the process, and re-running a stage under a newer version
  bumps it.
- `NULL` (legacy rows from before the column existed) sorts **older than
  every real version**.

Version strings compare as parsed `(major, minor, patch)` integer tuples —
never lexically. An unparsable or missing version compares as `(0, 0, 0)`.

### Versions in the export format

- The `header` record carries `"app_version"`: the exporter's package
  version.
- Every `message` record carries its `"pipeline_version"` (may be null).
- This is a breaking format change, so `FORMAT_VERSION` is **2**; version-1
  files are rejected (none exist in the wild — the format shipped
  unreleased).

### Version-aware import of existing messages

When an imported message already exists in the target (same list folder +
Message-ID), the message row itself is still **never** modified (subject,
body, dates etc. stay authoritative; the body-mismatch warning still
applies). But its derived data may now be refreshed:

1. Compare the file message's `pipeline_version` with the target row's
   (tuple comparison, NULL oldest).
2. If the file's version is **not later**, behave exactly as before: skip the
   message and its embedded extraction/score (`messages_skipped`).
3. If the file's version **is later**, compare derived data:
   - extraction equality: `sha256` of the text, `method`, `status`,
     `char_count`;
   - score equality: `text_sha256`, all three fractions, `label`,
     `detector_version`, `raw_response`.
   If everything (including presence/absence on both sides) is equal, the
   later pipeline has validated exactly what the target already holds, so the
   message's `pipeline_version` is advanced to the file's value (counted as
   `versions_bumped`) and nothing else changes. If anything differs,
   **replace** the target's derived state with
   the file's: delete the existing extraction row (the score cascades),
   insert the file's extraction (pointer-reconstructed and hash-verified as
   usual) and its score when present, and update the message's
   `pipeline_version` to the file's value. A file message whose extraction is
   null clears the target's extraction/score (the later pipeline produced
   nothing — that is the newer truth). Counted as `extractions_updated` and
   `scores_updated` in the summary (the message still counts in
   `messages_skipped` — its own row was not inserted).

`ImportSummary` gains `extractions_updated: int = 0`,
`scores_updated: int = 0` and `versions_bumped: int = 0`.

## File format

A UTF-8 [JSON Lines](https://jsonlines.org/) stream, one record per line, in a
fixed order: `header`, then per list `list` → `pull_state?`, then `person`s,
then `address`es, then `message`s (extraction and score **embedded** in the
message record so a skipped message atomically skips its children), then
`trailer`. An output path ending in `.gz` is gzip-compressed transparently.

Cross-references use **natural keys**, never local integer ids:
list → `folder`, address → `email`, message → `(folder, message_id)`.
Persons have no natural key, so each gets a file-scoped synthetic key
(`"p<local id>"`) referenced by its addresses.

### Records

```jsonc
{"type": "header", "format": "mlac-export", "format_version": 2,
 "app_version": "1.0.0", "exported_at": "<UTC ISO-8601>",
 "schema_version": 7, "folders": ["ietf.announce"]}

{"type": "list", "name": "announce", "folder": "ietf.announce",
 "last_synced_at": null, "removed_from_server_at": null, "last_message_at": null}

{"type": "pull_state", "folder": "ietf.announce", "uidvalidity": 123, "last_uid": 456}
// present only when the source has a cursor for the list

{"type": "person", "person_key": "p3", "canonical_name": "Alice Smith"}

{"type": "address", "email": "alice@example.org", "display_name": "Alice Smith",
 "person_key": "p3"}   // person_key null when unlinked

{"type": "message", "folder": "ietf.announce", "message_id": "<m1@example.org>",
 "email": "alice@example.org",          // null when the message has no sender address
 "subject": "…", "date": "…", "in_reply_to": null,
 "raw_body": "…", "raw_html": null, "uid": 101, "fetched_at": "…",
 "pipeline_version": "1.0.0",       // null for legacy rows

 "extraction": {                         // null when the message has no extraction row
   "method": "reply_parser", "char_count": 42, "status": "ok", "created_at": "…",
   "text": {"kind": "span", "start": 0, "length": 42},   // see "Text pointers"
   "sha256": "<sha256 of extracted_text>",
   "score": {                            // null when the extraction has no score row
     "fraction_ai": 0.95, "fraction_ai_assisted": 0.03, "fraction_human": 0.02,
     "label": "AI", "detector_version": "…",
     "raw_response": "<verbatim stored JSON string, may be null>",
     "text_sha256": "<sha256 of the cleaned text sent to Pangram>",
     "scored_at": "…"
   }
 }}

{"type": "trailer", "lists": 1, "messages": 250, "extractions": 240, "scores": 200}
```

### Text pointers

`extraction.text` is one of, chosen by the exporter in this order:

1. `{"kind": "full_body"}` — `extracted_text == raw_body`.
2. `{"kind": "span", "start": S, "length": L}` — `extracted_text` occurs
   verbatim in `raw_body` at character offset `S` (first occurrence via
   `str.find`); `L = len(extracted_text)`.
3. `{"kind": "inline", "value": "<full text>"}` — fallback whenever the text
   is not a contiguous substring of the body (or `raw_body` is null).

`extraction.sha256` is always present. The importer reconstructs the text,
recomputes the hash and **aborts the import on mismatch** — a pointer that no
longer resolves means the file is corrupt.

## Export semantics

- Lists are selected by `lists.name` (`--all-lists` = every list that has at
  least one message). A name may match several rows (name is not unique);
  all matches are exported. An unknown name is an error (`ValueError`).
- Only persons/addresses actually referenced by the exported messages are
  included, each once (deduplicated across lists in the same file).
- Purely a local database read: no IMAP, no Pangram, no caps involved.

## Import semantics

One pass over the stream, inside a single explicit transaction on the store's
connection (raw SQL, not the per-call-committing `Store` methods); `COMMIT` at
the end, `ROLLBACK` on any error. `--dry-run` runs the identical code path and
rolls back instead of committing, so its report is exact.

Per record type:

- **header** — must be first; `format`/`format_version` must match, else error.
- **list** — insert if the `folder` is new; an existing row is left untouched
  (its metadata is not overwritten).
- **pull_state** — inserted only when the target has no cursor for that list;
  an existing cursor always wins (it reflects the target's own sync state).
- **person / address** — addresses upsert by `email` with the same
  display-name backfill rule as `Store.upsert_address`. If the address already
  belongs to a person in the target, that link is kept. Otherwise the file's
  `person_key` is resolved: the first address of a group that already has a
  target person recruits that person for the whole group; if none does, a new
  person is created with the exported `canonical_name` (created lazily — only
  for persons actually needed).
- **message** — the collision guard. `INSERT … ON CONFLICT(list_id,
  message_id) DO NOTHING`; when the row already existed the message counts as
  *skipped* and its embedded extraction/score are **not** imported (the
  existing message's pipeline state is authoritative) — unless the file
  message carries a **later** `pipeline_version`, in which case the derived
  data is refreshed per "Version-aware import of existing messages" above
  (the message row itself still isn't touched). On skip, the stored
  `raw_body` is compared with the file's; a difference is logged as a warning
  and counted (`body_mismatches`) — never overwritten.
  For inserted messages: extraction text is reconstructed from its pointer,
  verified against `sha256`, and inserted; then the score, if present.
- **trailer** — must be last; its counts must equal the records actually seen,
  else the file is truncated/corrupt → error (rollback).

Any unknown `type`, out-of-order record, forward reference (message naming an
unseen folder/email, address naming an unseen `person_key`), invalid
extraction status, or JSON parse failure → error, rollback, non-zero exit.

## Public API — `src/mailing_list_ai_check/export_import.py`

```python
FORMAT_NAME = "mlac-export"
FORMAT_VERSION = 1

@dataclass(frozen=True)
class ExportSummary:
    lists: int
    messages: int
    extractions: int
    scores: int
    path: str

    def as_line(self) -> str: ...

@dataclass(frozen=True)
class ImportSummary:
    lists_created: int = 0
    lists_existing: int = 0
    pull_states_created: int = 0
    persons_created: int = 0
    addresses_upserted: int = 0
    messages_inserted: int = 0
    messages_skipped: int = 0
    body_mismatches: int = 0
    extractions_inserted: int = 0
    scores_inserted: int = 0
    dry_run: bool = False

    def as_line(self) -> str: ...

class ImportError_(Exception): ...   # module-specific error (name TBD by implementer,
                                     # e.g. ExportImportError), raised for every
                                     # validation failure described above

def export_lists(
    store: Store, list_names: Sequence[str] | None, out_path: str | Path,
    *, all_lists: bool = False,
) -> ExportSummary: ...

def import_file(
    store: Store, in_path: str | Path, *, dry_run: bool = False,
) -> ImportSummary: ...
```

## CLI

Two new console scripts in `pyproject.toml`, entry points in `cli.py`
following the existing `mail-ai-pull` / `mail-ai-extract` / `mail-ai-score`
patterns (argparse, `Config.load()` for the default `--db`, logging setup,
summary via the module logger, return `0`/`1`):

```
mail-ai-export LIST [LIST…] -o FILE [--all-lists] [--db PATH]
mail-ai-import FILE [--db PATH] [--dry-run]
```

- `mail-ai-export`: list names or `--all-lists` (not both, mirroring
  `mail-ai-pull` validation); `-o/--output` required; `.gz` suffix ⇒ gzip.
- `mail-ai-import`: positional file; `--dry-run` reports without writing;
  import validation errors log the reason and exit `1`.

Message bodies are never logged (matching the existing convention).
