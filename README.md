# mailing-list-ai-check

A tool for checking mailing-list mail for AI-generated content. Works against
any IMAP-accessible mailing-list archive. It pulls
list mail over IMAP, extracts the new text each author actually wrote (stripping
quotes and signatures), scores that text with the [Pangram](https://www.pangram.com/)
AI-detection API, and presents the results in a searchable web dashboard.

The pipeline runs as three idempotent, re-runnable stages over a local SQLite
database: **pull** (fetch messages) → **extract** (isolate each author's new
text) → **score** (Pangram verdict). A Flask + Vue dashboard reads the results.

### Limitations

- AI detectors are probabilistic: Pangram returns a likelihood, not proof, and
  can be wrong in either direction.
- Short texts are not scored: anything under 50 words is marked `too_short`
  rather than sent, because detection is unreliable below that length.
- Extraction of an author's new text is heuristic: quote and signature stripping
  can fail on unusual formatting.

## Requirements

- Python ≥ 3.14. The export/import format is compressed with zstd, which entered
  the standard library in 3.14 as `compression.zstd`; requiring that release
  keeps compression a standard-library concern and adds no third-party
  dependency. Earlier interpreters are not supported.
- Node.js (only to build the dashboard front end)

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Build the dashboard front end (needs Node)
make install-frontend   # npm install
make build              # npm run build -> frontend/dist
```

## Configuration

Copy the template and edit as needed:

```bash
cp .env.example .env
```

- **IMAP** — set `IMAP_HOST` to your mailing-list archive's IMAP server (there
  is no default); `IMAP_PORT` defaults to `993` (implicit TLS). If the server
  offers anonymous or guest access, set `IMAP_USERNAME` / `IMAP_PASSWORD` to the
  guest login documented by that server; otherwise use your own credentials.
- **`PANGRAM_API_KEY`** — required only for the scoring stage. Pulling,
  extraction, and the dashboard all work without it. Get a key from
  <https://www.pangram.com/>.
- **`DATABASE_PATH`** — SQLite file, defaults to `./data/mail.db`.

`.env` is gitignored; never commit it. See `.env.example` for the full list of
keys and defaults.

## Database and schema migrations

The database is a single SQLite file at `DATABASE_PATH`. There is no migration
command: the schema is brought up to date automatically whenever the database is
opened.

Each schema change is one numbered SQL script in `store.py`, and the applied
numbers are recorded in the database's own `schema_version` table. Opening the
database compares the two and runs only the scripts that are missing, each
committed in turn. Every entry point — the three pipeline commands, the export
and import commands, and the web app (one connection per request) — opens the
database the same way, so whichever runs first after an upgrade performs the
migration before doing anything else. Re-running against an already-current
database does nothing.

Two consequences:

- **Migrations are one-way.** There is no downgrade path, and a migration may
  rewrite rows as well as add columns. Copy the database file before running a
  new version against it for the first time; include the `-wal` and `-shm`
  side-files if they are present.
- **An older app version is not guarded against a newer database.** It reads the
  columns it knows about and ignores the rest, so downgrading the code without
  restoring the matching database copy can produce results that look valid but
  are derived from a partial view of the schema.

Upgrading the front end is a separate step: `mail-ai-web` serves whatever is in
`frontend/dist`, so run `make build` after pulling a new version or the dashboard
stays at the built version while the API moves on.

## Usage

The pipeline is three commands, run in order. Each is idempotent — it only
processes rows that lack its output — so runs resume cleanly after an interrupt.

### `mail-ai-pull` — fetch mail

Fetch messages from one or more lists into the store. Name lists as positional
arguments, or use `--all-lists` (touches ~1374 folders).

```bash
# 200 most recent messages from one list
mail-ai-pull last-call --count 200

# Messages since a date, from two lists
mail-ai-pull quic tls --since 2026-01-01

# Last 30 days across every list
mail-ai-pull --all-lists --days 30

# Resume from where the last pull left off (per-list cursor, UIDVALIDITY-aware)
mail-ai-pull last-call --incremental

# Only mail from particular senders (server-side FROM filter, repeatable/OR-ed)
mail-ai-pull tls --from alice@example.com --from bob@example.com

# See what would match without fetching or storing anything
mail-ai-pull tls --since 2026-06-01 --dry-run
```

Depth is one of `--count N`, `--since YYYY-MM-DD`, `--days N`, or
`--incremental`. `--limit N` is a hard cap on messages fetched this run — use
`--limit 10` when testing (see Costs and usage limits).

### `mail-ai-extract` — isolate each author's new text

```bash
mail-ai-extract              # process every message without an extraction
mail-ai-extract --limit 50   # stop after 50 messages
```

Runs email-reply-parser plus a custom cleanup pass (normalization, attribution
lines, indented quotes, signatures, digest over-strip guard). No credentials or
network needed.

### `mail-ai-score` — Pangram AI detection

```bash
mail-ai-score                # default: at most 10 API calls
mail-ai-score --limit 500    # a production run
mail-ai-score --dry-run      # show what would be scored / gated / cached
```

Requires `PANGRAM_API_KEY`. Extractions under 50 words are marked `too_short`
and never sent. Identical text is served from the score cache without an API
call. `--limit N` caps Pangram API calls per run (cache hits are free and
uncapped) and **defaults to 10** to limit accidental spending — pass a larger
value for production runs. Pangram costs roughly **$0.05 per 1,000 words**.

### `mail-ai-web` — the dashboard

```bash
mail-ai-web    # serves the built dashboard + API at http://127.0.0.1:8050
```

For a production view, build the front end (`make build`) first; `mail-ai-web`
then serves `frontend/dist` directly. For front-end development, use the
two-terminal workflow (see `make dev`).

The dashboard shares a single filter bar (list, person/address, date range,
Pangram label, likelihood range, free-text search) across every view, and that
filter state lives in the URL query string — so every view is a shareable link.

- **Overview** — headline counts, score distribution, flagged-share-over-time
  chart, and top flagged senders/lists; each element drills into the message
  explorer with that filter applied.
- **Messages** — a paginated, sortable table of messages under the current
  filter; click a row for detail. The last column, Chars/min, carries the
  reply-timing rate (see below), with a filter taking a minimum and/or a
  maximum rate.
- **Detail** — one message: metadata, the extracted new text highlighted within
  the full body, the Pangram score/label with a raw-response toggle, and a link
  to the thread.
- **People** — group multiple email addresses into a single person, with
  auto-suggested groupings (matching display names) and merge/detach controls,
  so one contributor's mail is analyzed together.
- **Lists** — per-list summary strips.

The ⓘ button beside the app name in the header opens a documentation panel: a
file list on the left, the rendered Markdown on the right. It shows `README.md`,
`CHANGELOG.md` and the Markdown files at the top level of `docs/`, read from the
checkout at request time. Files in sub-directories of `docs/` are not included.

### Reply-timing analysis

Every reply whose parent message is also in the store is classified by its
implied composition rate: the character count of its extracted new text divided
by the interval between the parent message's date and the reply's. That
interval is an upper bound on the time the author had to read the parent and
compose the reply, so the rate is a lower bound on the writing speed the reply
implies.

The rate is stored in the messages table (`timing_cpm`) alongside the band it
falls in (`timing`); the two are always written together. The Messages table's
Chars/min column shows the rate, and the message detail shows the band:

- **implausible** — at or above 250 characters per minute.
- **suspicious** — at or above 100 characters per minute.
- **normal** — below 100 characters per minute.
- empty — not computable: the message is not a reply, its parent is not in the
  store, a date is missing or malformed, the interval is not positive, or the
  message has no extraction with authored text (status `ok` or `too_short`).

The Chars/min column filters on the stored rate: the `cpm_min` and `cpm_max`
query parameters are inclusive bounds in characters per minute, and either one
excludes every message whose rate is not computable.

From 100 characters per minute up, the Chars/min cell is tinted in ten steps of
one purple, one step per hundred characters per minute (100–199 the lightest
through 1000 and above the strongest); lower rates and empty cells are untinted.

The signal is one-sided: a high rate shows the text was not composed within the
interval, while a low rate shows nothing. It is not by itself evidence of AI
generation — pasting a previously drafted passage, or replying to a message
first seen through another channel, produces the same rate. Both dates come
from the sender-set `Date:` header, so the interval depends on the senders'
clocks. The classification is recomputed after every pull, extract, re-extract
and import.

### Re-processing text derived by an older extraction routine

The routine that derives an author's new text carries its own version number,
separate from the app's. It is incremented whenever a change could alter the
extracted text, the text sent to Pangram, or an extraction's status, and every
extraction records the number of the routine that produced its text. An
extraction recorded against a lower number may hold text the current routine
would not produce. A release that does not change the routine leaves the number
alone, so upgrading the app does not by itself make stored text out of date.

The dashboard compares those numbers on load. When any extraction predates the
running routine it opens a prompt reporting how many, and offers to identify the
affected messages:

- **Show affected messages** re-runs the current extraction and post-processing
  over every stored message and compares the result with what is stored. This is
  local work only: no text is rewritten, no score is changed, and nothing is sent
  to Pangram. Messages whose text would change are listed in a table with a
  total, showing the character counts before and after and what moved (the
  extracted text, the text that gets scored, or the extraction status).
  Extractions that come out identical are stamped with the running routine's
  number, which is what stops the same prompt appearing again.
- **Run process ($)** re-extracts the listed messages and re-scores them. A
  message keeps its stored score unless the *scored* text changed, since only
  then was the verdict reached on text that no longer exists; each message that
  does need a new verdict is one paid Pangram call unless its new text is already
  in the score cache. Both stages report their counts as they run.
- **Not now** leaves everything untouched. An alert icon then sits beside the ⓘ
  button for as long as any extraction predates the running routine; it reopens
  the same prompt.

An extraction recorded against a *higher* number than the running routine — a
database written by a newer version of the app, then opened by an older one — is
not reported, so an older build never offers to replace text it cannot
reproduce.

### Exporting and importing lists

Move a list's messages and their full pipeline state (extractions and Pangram
scores) between databases as a single portable file — for backup, sharing, or
seeding another checkout. Neither command touches IMAP or Pangram; both are pure
local database operations.

```bash
# Export named lists to a file (writes export.jsonl.zst)
mail-ai-export announce last-call -o export.jsonl

# Export every list that has at least one message
mail-ai-export --all-lists -o all-lists.jsonl

# Export without compression (writes plain-lists.jsonl as given)
mail-ai-export --all-lists -o plain-lists.jsonl --no-compress

# Import into another database
mail-ai-import export.jsonl.zst

# Preview an import without writing anything
mail-ai-import export.jsonl.zst --dry-run
```

`mail-ai-export` takes one or more list names, or `--all-lists` (not both), and
requires `-o/--output`. The file is zstd-compressed and `.zst` is appended to
the output path unless it is already there; the summary line reports the path
actually written. `--no-compress` writes plain JSON Lines to the path as given.

`mail-ai-import` needs no flag for compression: it identifies the container from
the file's leading bytes rather than its name, so zstd, gzip and uncompressed
input are all accepted under any suffix, and exports produced before zstd became
the default still import. A corrupt or truncated file is rejected like any other
malformed input, and nothing is written.

Import is **idempotent and collision-safe**: a message already present in the
target (same Message-ID on the same list) is skipped along with its
extraction and score, so importing the same file twice — or back into the
database it came from — is a no-op. The whole import is **all-or-nothing** (one
transaction, rolled back on any error), and `--dry-run` runs the identical path
but rolls back, so its report is exact. Exports carry the app version that
produced them; when an imported message was processed by a **later** pipeline
version than the target's copy, its extraction and score are refreshed from the
file (the message body itself is never overwritten). Each exported extraction
also carries the version of the extraction routine that produced its text, and an
imported extraction keeps that number rather than being credited to the importing
build; for files written before the field existed it is inferred from the app
version in the file.

Export and import are also available from the dashboard's **Messages** pane, via
its Export and Import buttons.

## Costs and usage limits

- **Pangram spend** is controlled three ways: the score cache never pays twice
  for identical text, the 50-word gate skips text too short to score reliably,
  and `--limit` (default 10) caps calls per run. Use `--dry-run` to preview.
- **The archive IMAP server is a shared public service.** When testing or
  experimenting, pull no more than **10 messages** per run (and send no more
  than 10 texts to Pangram). These are project conventions, not enforced limits.

## Development

```bash
make test     # pytest
make lint     # ruff check
make dev      # prints the two-terminal (Vite + Flask) dev workflow
```

Layout:

- `src/mailing_list_ai_check/` — package (src layout): `config.py`, `store.py`
  (SQLite schema + typed API), `imap_client.py` / `fetcher.py` (pull),
  `extraction.py`, `pangram.py`, `cli.py` (the three CLI entry points), and
  `webapp/` (Flask API + SPA serving).
- `frontend/` — Vue 3 + Vite dashboard; `make build` emits `frontend/dist`.
- `tests/` — pytest suite, including `tests/fixtures/` (a hand-labeled corpus of
  real public-archive messages with expected extractions, used to grade the
  extractor).
- `docs/findings/` — the Phase 0 spike findings (IMAP, extraction, Pangram) that
  the design is built on, including the rationale for the main design decisions
  (email-reply-parser over Talon, stdlib `sqlite3` over an ORM, the Pangram
  contract).

### Secret-scanning guardrail

This repo is public and users supply their own credentials. Two guards keep
secrets out of commits:

- **Local (pre-commit):** install once per clone — `pip install pre-commit &&
  pre-commit install`.
- **CI:** [`gitleaks`](.github/workflows/gitleaks.yml) runs on every push and
  pull request.

## Versioning

The app follows [semantic versioning](https://semver.org/); the current version
lives in `mailing_list_ai_check.__version__` (`pyproject.toml` reads it
dynamically). The major version is bumped for a breaking change, the minor
version for a new feature or any other user-visible change (a raised Python floor
included), and the patch version for a fix or an internal change. Each message
records the app version that last processed it, and importing an export made by a
later version refreshes that message's extraction and score data.

The text-extraction routine carries a separate version number of its own,
incremented whenever a change could alter the extracted text, the text sent to
Pangram, or an extraction's status. It is what the dashboard compares to detect
stored text derived by an older routine (see "Re-processing text derived by an
older extraction routine"), so an app release that leaves the routine alone never
marks stored text out of date.

## License

MIT — see [LICENSE](LICENSE).
