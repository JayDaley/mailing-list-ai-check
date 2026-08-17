# Stats export design

A one-way export of scores and message metadata for statistical analysis
outside the app. It complements the full export (`docs/export-import.md`),
which moves complete databases between installs: the stats export is for
people who will analyse the results further — in a spreadsheet, pandas, R or
similar — and will not run extraction or AI detection themselves. It carries
no message text and cannot be imported.

## Goals

- Give an analyst every scored result and the metadata needed to aggregate it
  any way they choose: per list, per sender, per month, per thread, by timing
  band, by detector version.
- Ship the denominators, not only the hits: unscored and gated messages are
  included, because any share calculation needs them (the dashboard computes
  AI share over scored + too-short, and an export that omitted the gated
  messages could not reproduce the dashboard's numbers).
- Open everywhere: CSV files in a zip archive, readable by Excel, pandas and R
  without this app or any decompression tooling beyond the OS.
- Carry no message content: no bodies, no extracted text, no subjects, no raw
  headers, no raw detector responses. The export answers "what did the
  pipeline conclude", never "what did the message say".
- Support a pseudonymous variant for sharing beyond the operator.

## Non-goals

- Re-import. The file is an analysis artifact; the full export remains the
  only transfer format. Nothing in this format is read by the app.
- Filtering beyond the full export's selection (lists and a date range). The
  per-message rows are the primitive; consumers filter downstream.
- Reproducing the cleaned text or re-scoring. Consumers of this export do no
  AI analysis by definition; `text_sha256` and `raw_response` stay out.

## File format

A zip archive (stdlib `zipfile`, deflate) containing UTF-8 CSV files
(RFC 4180, header row, `\n` line endings) plus a manifest and a data
dictionary:

| Member | Contents |
|---|---|
| `messages.csv` | one row per message in scope — the primitive table |
| `lists.csv` | one row per exported list, aggregated over the scope |
| `senders.csv` | one row per sender with a message in scope, aggregated over the scope |
| `manifest.json` | machine-readable provenance and row counts |
| `README.md` | the column dictionary and interpretation caveats, self-contained |

CSV conventions: a NULL is an empty field; booleans are `true`/`false`;
dates are the stored UTC ISO-8601 strings; fractions are written at full
stored precision, unrounded. Column order is part of the format.

Zip rather than zstd because the audience is analysis tools, not this app:
every OS opens a zip, and the archive bundles the data dictionary with the
data. Without message text the rows are small (roughly 200 bytes each), so
compression ratio is not the constraint it is for the full export.

### `messages.csv`

One row per message in scope, scored or not.

| Column | Meaning |
|---|---|
| `message_key` | file-scoped key `m1`, `m2`, … in emission order |
| `list` | `lists.name` (not unique across lists) |
| `folder` | `lists.folder` — the unique list key; joins to `lists.csv` |
| `date` | `messages.date`, UTC ISO-8601, may be empty |
| `sender_key` | joins to `senders.csv`; empty when the message has no sender address (such a message gets no `senders.csv` row but still counts in `lists.csv`); see "Sender identity" |
| `is_reply` | whether the message carries an In-Reply-To header |
| `parent_key` | `message_key` of the parent when it is in the export, else empty |
| `auto_generated` | the stored auto-generated marker, empty when none |
| `timing` | reply-timing band: `normal`, `suspicious`, `implausible`, or empty |
| `timing_cpm` | the chars/minute rate behind the band, empty exactly where `timing` is |
| `extraction_status` | `ok`, `empty`, `too_short`, `failed`, or empty when never extracted |
| `extraction_method` | empty when never extracted |
| `extraction_chars` | `extractions.char_count`, empty when never extracted |
| `extraction_version` | generation of the extraction routine, may be empty |
| `pipeline_version` | app version that last processed the message, may be empty |
| `label` | Pangram `prediction_short` verbatim: `Human`, `Mixed`, `AI`; empty when unscored |
| `fraction_ai` | Pangram fraction in [0, 1], empty when unscored |
| `fraction_ai_assisted` | as above |
| `fraction_human` | as above |
| `detector_version` | empty when unscored |
| `scored_at` | UTC ISO-8601, empty when unscored |

Identified exports (the default) add two columns after `sender_key`:

| Column | Meaning |
|---|---|
| `email` | sender address, empty when the message has none |
| `sender_name` | the message's From name, falling back to the address's display name |

and two after `parent_key`:

| Column | Meaning |
|---|---|
| `message_id` | RFC 5322 Message-ID |
| `in_reply_to` | raw In-Reply-To value, empty when not a reply |

`parent_key` is resolved among the exported messages by the same normalized
In-Reply-To lookup the timing recompute applies, so thread analysis works in
both variants; `is_reply` is carried explicitly because a reply whose parent
is outside the scope has an empty `parent_key` either way.

### `lists.csv`

One row per exported list. All counts are over the messages in scope (the
date range applied), so they sum exactly to `messages.csv` — an analyst can
verify the aggregates against the primitive table.

| Column | Meaning |
|---|---|
| `list`, `folder` | as in `messages.csv` |
| `messages` | messages in scope |
| `scored` | messages with a score |
| `too_short` | messages gated under the reliability floor (`extraction_status = too_short`) |
| `human`, `mixed`, `ai` | scored messages by label |
| `ai_share` | `ai / (scored + too_short)`, empty when that denominator is 0 |
| `first_date`, `last_date` | oldest and newest `date` in scope, empty when none |

### `senders.csv`

One row per sender with at least one message in scope. A sender is a linked
person when the address belongs to one, otherwise the bare address — the same
grouping as the dashboard's Senders pane.

| Column | Meaning |
|---|---|
| `sender_key` | `p<person id>` or `a<address id>` (identified), `s1`, `s2`, … (pseudonymous) |
| `sender_type` | `person` or `address` |
| `messages`, `scored`, `too_short`, `human`, `mixed`, `ai`, `ai_share` | as in `lists.csv`, over the sender's messages in scope |
| `first_date`, `last_date` | as in `lists.csv` |

Identified exports add `name` (the person's canonical name, or the address's
display name) and `emails` (the person's addresses with a message in scope,
`;`-separated; a bare address has one).

`ai_share` uses the dashboard's definition (`ai_share()` in `store.py` /
`labels.js`): the AI count over scored plus too-short, so the values here
equal the shares the Senders pane and mix bars report.

### `manifest.json`

```jsonc
{"format": "mlac-stats", "stats_format_version": 1,
 "app_version": "1.10.1", "schema_version": 16,
 "exported_at": "<UTC ISO-8601>",
 "folders": ["ietf.announce"],
 "date_from": "2025-07-01",          // present only when given
 "date_to": "2026-08-01",            // present only when given
 "identified": true,                  // false for a pseudonymous export
 "rows": {"messages": 57435, "lists": 12, "senders": 3120},
 "labels": ["Human", "Mixed", "AI"],
 "timing_bands": ["normal", "suspicious", "implausible"],
 "detector_versions": ["…"],          // distinct values present in the file
 "extraction_versions": [1, 2]        // distinct values present in the file
}
```

`stats_format_version` is this format's own number, independent of the app
version and of the full export's `FORMAT_VERSION`. Since nothing imports the
file, the version exists for analysts and their scripts, not for rejection
logic; additive column changes do not bump it, and a removed or re-defined
column does.

### `README.md` (inside the archive)

The data dictionary, written for someone who has only the zip: every column
of every CSV, plus the caveats that stop the obvious misreadings —

- `label` is Pangram's `prediction_short` verbatim; the app derives nothing
  from it.
- Share calculations must include the too-short messages in the denominator
  to match the dashboard (the reliability floor gates messages under 50 words
  of authored text; they are never sent to the detector).
- Scores in one file may come from different detector versions and different
  extraction generations; both are per-row columns and listed in the manifest.
- The timing bands' thresholds, and that an empty band means the rate could
  not be computed, not that it was normal.
- The date-range edge: a bare `date_to` day excludes that day's messages,
  whose stored dates carry a time.
- A pseudonymous export is pseudonymous, not anonymous: list names, dates and
  thread shapes remain, and mailing-list archives are public.

## Sender identity

Two variants, chosen at export time; `identified` in the manifest says which
one a file is.

- **Identified** (default): sender addresses and names are included, and
  `message_id` / `in_reply_to` are real. `sender_key` is `p<person id>` /
  `a<address id>`, stable across exports from the same database.
- **Pseudonymous** (`--pseudonymous` / `pseudonymous=true`): the identity
  columns (`email`, `sender_name`, `name`, `emails`, `message_id`,
  `in_reply_to`) are omitted — not blanked, so a file's header row states
  what it holds. `sender_key` becomes `s1`, `s2`, … in first-seen order,
  assigned per export and deliberately unstable across exports. Keys are
  sequential, never hashes: a hash of a known address is reversible by
  dictionary. Thread analysis survives via `parent_key` and `is_reply`.

The dashboard's anonymous mode is a display preference and does not affect
either variant; the export dialog offers the choice explicitly.

## Selection

Identical to the full export, so the two dialogs and CLIs stay congruent:
lists by `lists.name` (or all lists with a message in scope) and an optional
inclusive `date_from` / `date_to` on `messages.date`, with the same lexical
comparison and the same `date_to` edge. Every message in scope is exported
whether or not it was scored; there are no further filters.

## Streaming and cost

Purely a local database read: no IMAP, no Pangram, no caps involved. The
message pass streams one row at a time into the zip member
(`ZipFile.open(name, "w")`), so peak memory is independent of the export's
size, matching the full export's discipline. The aggregates for `lists.csv`
and `senders.csv` are single GROUP BY queries over the same scope. A
pseudonymous export assigns sender keys in a pre-pass over `address_id`,
mirroring the full export's address pre-pass. Without text the output is
small — roughly 10 MB of CSV per 50,000 messages before compression.

## Public API — `src/mailing_list_ai_check/stats_export.py`

```python
STATS_FORMAT_NAME = "mlac-stats"
STATS_FORMAT_VERSION = 1

@dataclass(frozen=True)
class StatsExportSummary:
    lists: int
    senders: int
    messages: int
    scored: int
    path: str

    def as_line(self) -> str: ...

def export_stats(
    store: Store, list_names: Sequence[str] | None, out_path: str | Path,
    *, all_lists: bool = False, pseudonymous: bool = False,
    date_from: str | None = None, date_to: str | None = None,
) -> StatsExportSummary: ...   # '.zip' appended to out_path unless present;
                               # selection semantics identical to export_lists
```

Selection validation (names xor `all_lists`, unknown name is a `ValueError`)
is shared with `export_lists` rather than duplicated.

## CLI

```
mail-ai-export-stats LIST [LIST…] -o FILE [--all-lists] [--pseudonymous]
                                  [--date-from ISO] [--date-to ISO] [--db PATH]
```

A separate console script rather than a flag on `mail-ai-export`, because the
two commands produce different artifacts with different guarantees (one
re-importable archive, one analysis bundle) and share only their selection
arguments. Flags follow `mail-ai-export`: `-o/--output` required, dates
validated as ISO-8601 at parse time, summary via the module logger, exit
`0`/`1`.

## HTTP

`GET /api/export/stats` with the same selection params as `GET /api/export`
(`list` repeatable, `date_from`, `date_to`; unknown list and empty selection
are `404`, a bad date is `400`) plus `pseudonymous` (boolean, default false).
The attachment is named `mlac-stats-<slug>-<YYYYMMDD>.zip` with the same
`<slug>` rules as the full export, and is streamed through the same
build-to-unlinked-tempfile-then-chunk pattern.

## Dashboard

The export dialog gains a format choice above the list picker:

- **Full** — the existing re-importable archive (`.jsonl.zst`).
- **Stats** — this format (`.zip`), with a "Pseudonymous" checkbox shown only
  when selected.

List and date selection are shared between the two formats; the dialog's
summary line names the chosen format.

## Versioning and changelog

Adding the stats export changes nothing stored, so it is a **patch** bump
with a `CHANGELOG.md` section. `STATS_FORMAT_VERSION` starts at 1 and moves
independently, on the rules given under `manifest.json`.

## Testing

`tests/test_stats_export.py`:

- archive member set is exactly the five files, and `manifest.json` matches
  the CSV row counts;
- `messages.csv` rows equal the store's rows for the scope (including
  unscored and gated messages), read back with the stdlib `csv` module;
- `lists.csv` and `senders.csv` aggregates equal the same aggregates computed
  from `messages.csv`, and `ai_share` matches `ai_share()`;
- a pseudonymous export contains none of the identity columns, its
  `sender_key`s are dense `s<n>` values, and `parent_key` still links a reply
  to its in-scope parent;
- the date range selects the same messages as the full export's range;
- webapp: content type, attachment name, `404`/`400` cases, `pseudonymous`
  parsing.
