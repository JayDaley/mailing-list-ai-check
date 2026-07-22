# mailing-list-ai-check — Build Plan

A sequenced, multi-session plan for building an app that pulls mailing-list
mail over IMAP, extracts each author's newly written text with Talon and/or
email-reply-parser, scores it for AI-generated content with Pangram, and presents the results in a searchable
Flask + Vue dashboard.

## How this plan is executed

- **Fable is the director.** At the start of each session Fable reads this file,
  checks the *Progress* checklist and `git log`, and decides what to run next.
- **Opus sub-agents do the build work.** Fable dispatches tasks to Opus
  sub-agents (Agent tool, `model: opus`), running independent tasks in parallel.
  Each task below is written so it can be handed to a sub-agent as a
  self-contained brief.
- **Fable verifies, then commits.** After sub-agents return, Fable reviews the
  diff, runs `ruff` and `pytest`, ticks the checklist items below, and commits.
  The checklist in this file is the cross-session state — keep it accurate.
- **Session boundaries are phase boundaries.** Each phase is sized to fit
  comfortably in one session. If a phase must be split, tick the completed
  tasks and note the stopping point under *Session log*.

### Standing rules for every sub-agent brief

- This repo is public. Never commit secrets, never print credentials, never
  hard-code an example key. New settings go in `Config` and `.env.example`
  (empty value) — never read `os.environ` directly outside `config.py`.
- Python ≥ 3.11, `src/` layout, `ruff` clean, `pytest` for tests.
- Real public list mail is available, so real message fixtures are acceptable in
  tests, but keep fixtures small and only include what the test needs.
- Every phase that adds behavior adds tests for it in the same phase.
- **Hard testing limits:** when testing or experimenting, pull no more than
  **10 messages** from the IMAP server and send no more than **10 texts** to
  Pangram per run. These caps are defaults in test/spike code, not habits.
  Only an explicit user-requested production pull may exceed them.

## Progress

- [x] Phase 0 — Spikes: de-risk IMAP, extraction libraries, Pangram
- [x] Phase 1 — Data model and storage
- [x] Phase 2 — IMAP fetcher and pull CLI
- [x] Phase 3 — New-text extraction pipeline
- [x] Phase 4 — Pangram scoring pipeline
- [x] Phase 5 — Flask API
- [x] Phase 6 — Vue dashboard
- [x] Phase 7 — Person entities, polish, end-to-end verification

---

## Architecture overview

```
                 ┌────────────────────────────────────────────────┐
                 │                  pull CLI                       │
  archive IMAP  ─► IMAP fetcher ─► SQLite store ◄─ extraction ◄───┤
                 │   (Phase 2)      (Phase 1)      (Phase 3)      │
                 │                      ▲                          │
                 │                      └─ Pangram scorer (Ph 4)  │
                 └────────────────────────────────────────────────┘
                                        ▲
                              Flask API (Phase 5)
                                        ▲
                            Vue dashboard (Phase 6)
```

Pipeline stages are decoupled through the database: fetch stores raw messages,
extraction fills in extracted text, scoring fills in Pangram results. Each
stage is idempotent and re-runnable (it processes rows that lack its output),
so a failed or interrupted run resumes cleanly — important for multi-session
work and for API rate limits.

Key external facts (verified in Phase 0 before anything depends on them):

- **Archive IMAP**: the archive's IMAP server exposes every list archive as a
  folder (`Shared Folders/<listname>`). Own credentials or anonymous access.
- **Extraction libraries**: two candidates, compared head-to-head in Phase 0.
  **Talon** (mailgun/talon) — `talon.quotations.extract_from()` strips quoted
  text and signatures. **email-reply-parser** (zapier/email-reply-parser) —
  fragment-based: splits a message into fragments marked quoted/signature/
  hidden, which may suit interleaved replies better than reply-extraction.
  Mailing-list mail is typically bottom-posted/interleaved, the hard case for both —
  Phase 0 grades each on the same corpus and Phase 3 implements the winner
  (either alone, both merged, or with a custom quote-stripping fallback).
- **Pangram**: REST API, text in → AI-likelihood + label out. Short texts
  (roughly under 100 chars) score unreliably and must be flagged, not scored.

---

## Phase 0 — Spikes (Session 1)

Goal: prove the three external dependencies work before designing around them.
All three spikes are independent — **dispatch as three parallel Opus
sub-agents**. Spike code goes in `spikes/` (gitignored except for the written
findings), findings go in `docs/findings/`.

- [x] **0.1 IMAP spike.** Connect to the archive's IMAP server with the `.env`
  credentials (and test anonymous access too). Document: exact folder naming
  scheme, how to enumerate all list folders, whether `UID SEARCH SINCE` and
  `FROM` searches work server-side, UIDVALIDITY behavior, message counts on a
  couple of big lists (e.g. `last-call`, `tls`), and observed fetch throughput.
  Deliverable: `docs/findings/imap.md` + a minimal working fetch snippet.
- [x] **0.2 Extraction-libraries spike.** Compare **Talon** (mailgun/talon)
  and **email-reply-parser** (zapier/email-reply-parser) head-to-head. Verify
  both install on Python 3.11 (Talon has old dependencies; if it fails,
  document the workaround). Pull ~30 real messages from the public web archive,
  run both libraries on the same corpus, and manually
  grade each output on bottom-posted/interleaved replies: does it keep all
  new text? Does it drop interleaved comments below quotes? Does it strip
  signatures and "On …, X wrote:" attribution lines? Deliverable:
  `docs/findings/extraction.md` with a per-library quality verdict and a
  recommendation (Talon alone / email-reply-parser alone / both merged /
  either + custom quote stripper / custom only).
- [x] **0.3 Pangram spike.** Using `PANGRAM_API_KEY`, hit the Pangram API with
  known-human and known-AI samples. Document: endpoint(s), request/response
  schema, the fields worth storing (likelihood, label, window scores), rate
  limits, minimum/maximum text length, cost per call, and batch options.
  Deliverable: `docs/findings/pangram.md`.

**Fable at session end:** read the three findings, resolve the Phase 3
extraction strategy and Phase 4 API contract, and record decisions in the
*Decisions* section below. If any spike failed, the fix becomes task 0.4 next
session before Phase 1 begins.

---

## Phase 1 — Data model and storage (Session 2)

Goal: SQLite schema and a typed storage layer everything else builds on.
Single sub-agent task (schema needs one coherent author), informed by Phase 0
findings.

- [x] **1.1 Schema + storage module** (`src/mailing_list_ai_check/store.py`,
  SQLite via `sqlite3` or SQLAlchemy — sub-agent picks and justifies):

  | Table | Purpose | Key columns |
  |---|---|---|
  | `lists` | one row per mailing list | name, folder, last_synced_at |
  | `pull_state` | incremental-pull cursor per list | list_id, uidvalidity, last_uid |
  | `addresses` | one row per email address seen | email, display_name, person_id (nullable) |
  | `persons` | entity grouping multiple addresses | id, canonical_name |
  | `messages` | one row per fetched message | message_id (RFC 5322), list_id, address_id, subject, date, in_reply_to, raw body, fetch metadata |
  | `extractions` | Talon output per message | message_id, extracted_text, method, char_count, status |
  | `scores` | Pangram result per extraction | extraction_id, fraction_ai, fraction_ai_assisted, fraction_human, label, detector_version, raw_response JSON, text_sha256, scored_at |

  Constraints that matter: `messages.message_id` unique per list (dedupe on
  re-pull); `scores.text_sha256` indexed (cache — never pay Pangram twice for
  identical text); `extractions.status` distinguishes ok / empty / too-short /
  failed. Include migration bootstrapping (create-if-missing versioned schema)
  and full unit tests against a temp database.
- [x] **1.2 Config additions.** `DATABASE_PATH` (default `./data/mail.db`) in
  `Config` and `.env.example`; `data/` gitignored.

---

## Phase 2 — IMAP fetcher and pull CLI (Session 3)

Goal: `mail-ai-pull` fetches mail per the user's selection into the store.

- [x] **2.1 Fetcher module** (`imap_client.py` + `fetcher.py`): connect/login
  from `Config`, enumerate list folders, fetch by UID with batching, parse
  RFC 5322 messages (prefer `text/plain` part; note HTML-only messages),
  normalize addresses, upsert into the store. Selection parameters:
  - **Lists:** one list, several, or `--all-lists`.
  - **Depth:** `--count N` (most recent N per list) or `--since DATE` /
    `--days N`, or `--incremental` (from `pull_state`; UIDVALIDITY change
    forces a documented resync path).
  - **Senders:** `--from addr` repeatable, or all senders. Use server-side
    `UID SEARCH FROM` if the Phase 0 findings showed it works; otherwise
    filter client-side after fetch.
- [x] **2.2 CLI** (`cli.py`, exposed as a `[project.scripts]` entry point):
  argument parsing, progress output, summary line (fetched / skipped-duplicate
  / errors), `--dry-run`. Structured logging, no message bodies at INFO level.
- [x] **2.3 Tests:** fetcher unit tests against a fake IMAP server or a
  recorded-response fake (sub-agent decides; must not need network), CLI
  argument tests, incremental-cursor tests including UIDVALIDITY reset.

Tasks 2.1 and 2.2/2.3 can be one sub-agent; a **second parallel sub-agent**
downloads a real fixture set (50–100 messages across 2–3 lists from the public
archive) into `tests/fixtures/` for Phase 3.

---

## Phase 3 — New-text extraction (Session 4)

Goal: for every stored message, the text its author actually wrote.

- [x] **3.1 Extraction module** (`extraction.py`) implementing the decided
  strategy (see *Decisions*): **email-reply-parser as primary** plus the
  custom pass from `docs/findings/extraction.md` — BOM/CRLF normalization
  before parsing, a regex sweep for indented-`>` quotes and attribution lines
  ("On …, X wrote:"), and an over-strip guard for dashed-separator digest
  messages. Talon is not used. Record which method produced the output in
  `extractions.method`. Handle: empty results, signature-only messages,
  HTML-only messages, non-UTF-8 encodings.
- [x] **3.2 Pipeline command** (`mail-ai-pull extract` or `--extract` flag):
  process all messages without an extraction row; idempotent; summary stats.
- [x] **3.3 Quality tests:** the Phase 2 fixture set becomes a graded corpus —
  a second parallel sub-agent hand-labels expected extractions for ~25
  fixtures; tests assert ≥ agreed accuracy and pin exact output for the
  trickiest interleaved cases so regressions are loud.

---

## Phase 4 — Pangram scoring (Session 5)

Goal: every non-trivial extraction gets a Pangram verdict, cheaply and safely.

- [x] **4.1 Pangram client** (`pangram.py`) per the Phase 0 contract: retries
  with backoff, rate limiting, timeout handling. API key only ever from
  `Config`; never logged.
- [x] **4.2 Scoring pipeline** (`score` command): select extractions with
  status ok and length ≥ threshold (from Phase 0 findings; below it, mark
  `too_short` rather than scoring); skip if `text_sha256` already scored
  (cache hit); store normalized fields + raw JSON. `--limit N` to cap spend
  per run; running total of API calls printed at the end.
- [x] **4.3 Tests:** mocked-API unit tests (cache behavior, retry, threshold,
  idempotency). One tiny opt-in live test behind an env flag, excluded by
  default so CI never spends money or needs a key.

---

## Phase 5 — Flask API (Session 6)

Goal: a JSON API over the store that supports every dashboard view.

- [x] **5.1 App scaffold** (`webapp/` package): Flask app factory, blueprint
  structure, serves the built Vue bundle in production, CORS for the Vite dev
  server in development. New config: `FLASK_HOST`/`FLASK_PORT`.
- [x] **5.2 Query endpoints** — all list endpoints accept combinable filters
  (`list`, `address`, `person`, `date_from`, `date_to`, `label`,
  `min_likelihood`, `max_likelihood`, free-text `q` over subject + extracted
  text) plus pagination and sorting:
  - `GET /api/messages` and `GET /api/messages/<id>` (detail: raw body,
    extracted text, method, score).
  - `GET /api/summary` — aggregates for the overview: counts and score
    distributions grouped by list / person / month, top-N flagged senders.
  - `GET /api/lists`, `GET /api/addresses`, `GET /api/persons`.
  - Person management: `POST /api/persons` (create + assign addresses),
    `PUT /api/persons/<id>` (merge/rename/detach). Also
    `GET /api/persons/suggestions` — auto-grouping candidates by matching
    display name across addresses, for one-click confirmation in the UI.
- [x] **5.3 API tests** against a seeded temp database, covering filter
  combinations and pagination edges.

---

## Phase 6 — Vue dashboard (Session 7)

Goal: the drill-down UI. Vue 3 + Vite + vue-router + Pinia in `frontend/`;
plain fetch against the Phase 5 API. Two parallel sub-agents: one on scaffold
+ overview, one on the explorer/detail views (agree on route and component
names in the brief before dispatch).

- [x] **6.1 Scaffold + overview page:** Vite project, router, API client
  module, global filter bar (list, person/address, date range, Pangram label /
  likelihood range, free-text search) whose state drives every view via a
  Pinia store and is reflected in the URL query string (shareable views).
  Overview shows `/api/summary`: headline counts, score distribution,
  flagged-share-over-time chart, top flagged senders/lists — each element
  clickable to drill into the explorer with that filter applied.
- [x] **6.2 Explorer + detail views:** paginated, sortable message table under
  the same filter bar; row click opens message detail (metadata, extracted
  text highlighted within the full body, Pangram score + label + raw-response
  toggle, link to the thread via `in_reply_to`). Person and list pages: same
  table pre-filtered, plus that entity's summary strip.
- [x] **6.3 Person management UI:** page listing addresses, grouping
  suggestions from the API, and merge/detach controls.
- [x] **6.4 Build integration:** `npm run build` output served by Flask; a
  `make dev` / documented two-terminal dev workflow; frontend lint passes.

---

## Phase 7 — Persons polish, E2E, docs (Session 8)

- [x] **7.1 End-to-end verification (Fable-led, not delegated):** on a real
  `.env`, run pull → extract → score on one modest list with `--limit`,
  then drive the dashboard in the browser and verify drill-down, search,
  filters, and person merge against the real data.
- [x] **7.2 Fix round:** dispatch sub-agents for every defect found in 7.1.
- [x] **7.3 Docs:** README rewrite — setup, credentials, CLI examples for each
  pull mode, dashboard tour, cost notes (Pangram spend controls). Update
  CLAUDE.md layout section.
- [x] **7.4 Housekeeping:** `ruff` + full `pytest` green, gitleaks workflow
  passes, `.env.example` complete, version bump.

---

## Decisions

Record here as they are made (Fable, end of each session):

- **Extraction strategy (Session 1, from `docs/findings/extraction.md`):**
  **email-reply-parser (primary) + small custom pass; Talon is dropped.**
  On interleaved bottom-posted replies — the critical case —
  email-reply-parser was correct 10/12 vs Talon 0/12 (Talon only detects one
  contiguous quoted tail and returned the whole message unchanged on 9/12).
  Talon also no longer installs cleanly on modern Python (cchardet build
  break, unloadable pickled sklearn model) and its working subset adds no
  value over email-reply-parser. The custom pass: BOM/CRLF normalization,
  indented-`>` and attribution-line regex sweep, over-strip guard for
  dashed-separator digests (merge sketch in the findings doc).
- **Pangram contract (Session 1, live-verified, `docs/findings/pangram.md`):**
  raw `requests` (not the SDK); async `POST /task` → poll flow, `x-api-key`
  header. Store `fraction_ai`, `fraction_ai_assisted`, `fraction_human`,
  `prediction_short` (label), detector `version`, and full raw JSON (incl.
  `windows`). **Too-short gate: < 50 words → `too_short`, never sent — the
  server does NOT enforce this**, it scores 10-word texts. Bulk endpoint for
  backfill ($0.04/1k words), realtime ($0.05/1k, ~5 QPS) for incremental.
- **IMAP (Session 1, verified live, `docs/findings/imap.md`):** anonymous
  auth works (the archive IMAP server, login `anonymous` / any email-style
  password) — keep optional credential config for future private lists.
  Sender + date filtering is **server-side** (`UID SEARCH FROM`/`SINCE`,
  combinable). Incremental pull via per-folder `(UIDVALIDITY, last_uid)`.
  Fetcher must raise `imaplib._MAXLINE` and quote folder names.
- **Storage (Session 2): plain stdlib `sqlite3`, no ORM.** Single-writer CLI
  pipeline + read-mostly Flask API over one local file; the layer's
  correctness depends on exact SQL (`ON CONFLICT` upserts, hash-keyed score
  cache, PRAGMA tuning) that an ORM would obscure, and backend portability
  buys nothing. Typed frozen dataclasses give row ergonomics. Zero new deps.
  Naming caveat for Phase 3: `extractions.message_id` is an integer FK to
  `messages.id`; the RFC 5322 Message-ID string is `messages.message_id`.

## Session log

| Session | Date | Work done | Notes |
|---|---|---|---|
| 7 | 2026-07-21 | Phase 6 complete (two sequential agents): scaffold + URL-synced filters + Overview, then Explorer/Detail/Lists/People views. Agent 2 fixed agent 1's store→URL sync bug (getter called as function). Build 48 KB gz; e2e smoke vs seeded DB incl. full person-mutation cycle and both-direction URL sync; 246 Python tests unchanged. | Detail-view highlighter's match path not yet exercised on real data (seed bodies are synthetic) — check in Phase 7 E2E. |
| 9 | 2026-07-22 | Post-plan features: Anonymous mode switch (hides top senders, People tab, From column, Person dropdown; persisted), "+ Add list" runner on Lists tab backed by new `POST /api/pull` (pull→extract→score, validation, scoring skipped without key, threaded dev server), uniform-height filter controls. Verified live in browser incl. a real 5-message run on tools-discuss. 269 tests. | |
| 8 | 2026-07-21 | Phase 7 complete — **project done, all phases green.** E2E on live data (10 pulled, 10 extracted, 6 scored + 4 gated) and full browser drive. Two defects found and fixed: Outlook-style top-post mis-extraction (guard misfire; now 20/20 fixtures) and a renderer hang on person/list drill-in (dead sync guard + mutate-before-navigate). PANGRAM_API_KEY now optional except for scoring. README rewritten; CLAUDE.md layout updated; pre-commit hooks excluded from tests/fixtures (trailing-whitespace would corrupt raw .eml). v0.2.0. Total Pangram spend: 19 calls (~$0.17). | Preview server left running on :8050 against data/mail.db. Stale nit (not fixed): docs/findings/imap.md says IMAP_USER; shipped key is IMAP_USERNAME. |
| 6 | 2026-07-21 | Phase 5 complete: `webapp/` (app factory, /api blueprint, SPA serving + dev CORS via manual hook), filterable `query_messages`/`summary` in store.py, person CRUD + suggestions, `mail-ai-web` script, FLASK_HOST/PORT config. 246 tests green. | Flagged = labels AI + AI-Assisted. Per-request Store via Flask `g`. Config wart (PANGRAM_API_KEY required for web-only runs) still open for Phase 7. |
| 5 | 2026-07-21 | Phase 4 complete: `pangram.py` client (retry/backoff/throttle), `mail-ai-score` (50-word gate, sha256 cache, `--limit` default 10, dry-run, spend estimate). 169 tests + 1 opt-in live test. Live validation: 3 genuinely-human fixture texts all labeled Human, fraction_ai 0.0 (Phase 0 caveat closed). 3 API calls spent. | Total Pangram spend this project so far: 6 calls. |
| 4 | 2026-07-21 | Phase 3 complete: `extraction.py` (ERP + custom pass: normalization, multi-lang attribution, indented quotes, sig-vs-Ps handling, over-strip guard), `mail-ai-extract` command. **19/19 fixtures match ground truth, all 6 interleaved.** 137 tests green. | Separate `mail-ai-extract` script chosen over subcommand restructure. Upstream nit: email-reply-parser 0.5.12 emits a DeprecationWarning (positional re.MULTILINE); harmless. |
| 3 | 2026-07-21 | Phase 2 complete: `imap_client.py` + `fetcher.py` + `mail-ai-pull` CLI (count/since/incremental, `--from`, `--limit`, `--dry-run`); 75 tests green, network-free via FakeImapConn; live smoke: 5 msgs from last-call, dedupe + pull_state verified. 19 hand-labeled fixtures committed (8 known ERP quirks documented for Phase 3). | HTML-only messages stored with empty body + counted `html_only`; down-conversion deferred to Phase 3. Known wart: `Config.load()` requires PANGRAM_API_KEY even for pull-only runs — revisit in Phase 7 polish. Phase 2 agent hit a transient API error mid-run; resumed cleanly. |
| 2 | 2026-07-21 | Phase 1 complete: `store.py` (schema v1, migrations, WAL, typed Store API), config gains `DATABASE_PATH` + optional-IMAP-credentials with anonymous defaults, 30 tests green, ruff clean. Decision recorded: stdlib sqlite3, no ORM. | Sessions 2+ run back-to-back per user instruction ("continue through the rest of the phases"). Project venv now at `.venv` (Python 3.14). |
| 1 | 2026-07-21 | Plan written; Phase 0 complete: three parallel spikes, findings in `docs/findings/`, decisions recorded above. Pangram live-verified (3 calls). Hard testing limits added (≤10 IMAP msgs, ≤10 Pangram calls per test run). | Talon dropped; email-reply-parser chosen. Anonymous IMAP works — no credentials needed for public lists. 88-message corpus in gitignored `spikes/extraction/`; 11 fixtures flagged for promotion in Phase 2. Machine has Python 3.13/3.14 only (no 3.11) — fine for `requires-python >= 3.11`. |
