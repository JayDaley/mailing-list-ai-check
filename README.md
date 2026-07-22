# mailing-list-ai-check

A tool for checking mailing-list mail for AI-generated content. Works against
any IMAP-accessible mailing-list archive. It pulls
list mail over IMAP, extracts the new text each author actually wrote (stripping
quotes and signatures), scores that text with the [Pangram](https://www.pangram.com/)
AI-detection API, and presents the results in a searchable web dashboard.

The pipeline runs as three idempotent, re-runnable stages over a local SQLite
database: **pull** (fetch messages) → **extract** (isolate each author's new
text) → **score** (Pangram verdict). A Flask + Vue dashboard reads the results.

### Honest caveats

- AI detectors are probabilistic: Pangram returns a likelihood, not proof, and
  can be wrong in either direction.
- Short texts are not scored: anything under 50 words is marked `too_short`
  rather than sent, because detection is unreliable below that length.
- Extraction of an author's new text is heuristic: quote and signature stripping
  is very good on typical mailing-list mail but not perfect, especially on unusual
  formatting.

## Requirements

- Python ≥ 3.11
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
`--limit 10` when testing (see Cost & courtesy).

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
uncapped) and **defaults to 10** so a stray run cannot spend much — pass a
larger value for real runs. Pangram costs roughly **$0.05 per 1,000 words**.

### `mail-ai-web` — the dashboard

```bash
mail-ai-web    # serves the built dashboard + API at http://127.0.0.1:8050
```

For a production view, build the front end (`make build`) first; `mail-ai-web`
then serves `frontend/dist` directly. For front-end development, use the
two-terminal workflow (see `make dev`).

The dashboard shares a single filter bar (list, person/address, date range,
Pangram label, likelihood range, free-text search) across every view, and that
filter state lives in the URL query string — so any view you are looking at is
a shareable link.

- **Overview** — headline counts, score distribution, flagged-share-over-time
  chart, and top flagged senders/lists; each element drills into the message
  explorer with that filter applied.
- **Messages** — a paginated, sortable table of messages under the current
  filter; click a row for detail.
- **Detail** — one message: metadata, the extracted new text highlighted within
  the full body, the Pangram score/label with a raw-response toggle, and a link
  to the thread.
- **People** — group multiple email addresses into a single person, with
  auto-suggested groupings (matching display names) and merge/detach controls,
  so one contributor's mail is analyzed together.
- **Lists** — per-list summary strips.

## Cost & courtesy

- **Pangram spend** is controlled three ways: the score cache never pays twice
  for identical text, the 50-word gate skips text too short to score reliably,
  and `--limit` (default 10) caps calls per run. Use `--dry-run` to preview.
- **The archive IMAP server is a shared public service.** Be gentle: when testing
  or experimenting, pull no more than **10 messages** per run (and send no more
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
  the design is built on, including why things are the way they are
  (email-reply-parser over Talon, stdlib `sqlite3` over an ORM, the Pangram
  contract).

### Secret-scanning guardrail

This repo is public and users supply their own credentials. Two guards keep
secrets out of commits:

- **Local (pre-commit):** install once per clone — `pip install pre-commit &&
  pre-commit install`.
- **CI:** [`gitleaks`](.github/workflows/gitleaks.yml) runs on every push and
  pull request.

## License

MIT — see [LICENSE](LICENSE).
</content>
</invoke>
