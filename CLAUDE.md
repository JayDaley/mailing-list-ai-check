# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

`mailing-list-ai-check` — a Python app for AI-assisted checking of mailing-list mail.

## Layout

- `src/mailing_list_ai_check/` — package source (src layout)
  - `config.py` — loads credentials/settings from environment
  - `store.py` — SQLite schema, migrations, and typed storage API
  - `imap_client.py` / `fetcher.py` — IMAP connection and the pull pipeline
  - `extraction.py` — new-text extraction (email-reply-parser + custom pass)
  - `pangram.py` — Pangram AI-detection API client
  - `cli.py` — the CLI entry points (pull / extract / score)
  - `webapp/` — Flask API and built-dashboard serving
- `frontend/` — Vue 3 + Vite dashboard (`make build` emits `frontend/dist`)
- `tests/` — pytest suite, including `fixtures/` (hand-labeled extraction corpus)
- `docs/findings/` — Phase 0 spike findings (IMAP, extraction, Pangram)
- `pyproject.toml` — project metadata, dependencies, tooling config
- `Makefile` — dev/build/test/lint targets
- `.env.example` — template for required secrets (copy to `.env`)
- `CHANGELOG.md` — one section per release (see Changelog below)

## Secrets — important

This repo is **public on GitHub**. Users clone it and supply their own secrets:
IMAP credentials and a Pangram API key.

- Credentials live only in environment variables / a local `.env` file.
- `.env` and other credential files are gitignored. **Never** commit a secret,
  print one to logs, or paste one into source (even as a placeholder/example
  value). When adding a new secret, add its key to `.env.example` with an
  empty value so users know it exists.

## Hard testing limits

When testing or experimenting (spikes, manual runs, integration tests, demos —
anything that is not an explicit user-requested production pull):

- Pull **no more than 10 messages** from the IMAP server per run.
- Send **no more than 10 texts** to the Pangram API per run.

Pangram calls cost real money and IMAP is a shared public service. Bake these
caps into test code as defaults (e.g. `--limit 10`), don't rely on remembering.

## Documentation style — hard rule

All documentation (README, everything under `docs/`, and any other published
prose) must be written in a simple, factual, impersonal style. No
editorialising: no opinionated flourishes, colloquialisms, first-person
voice, or subjective commentary — state facts, measurements, and rationale
plainly.

## Searching the source — use CodeGraph, not grep

This repository is indexed by CodeGraph (`.codegraph/` at the repo root). To
find or understand code, call the `codegraph_explore` MCP tool **before**
reaching for `grep`, `rg`, `find`, or bulk `Read` calls — one call returns the
verbatim line-numbered source of the relevant symbols, the call paths between
them, and what depends on them.

- Name symbols or files in the query (e.g. `codegraph_explore` with
  `"extract_new_text clean_text pipeline_version"`), or ask a plain question
  about the code.
- If the tool is listed but deferred, load it by name via tool search first.
  The shell fallback is `codegraph explore "<symbols or question>"`.
- `grep` remains appropriate for non-source text: literal strings in fixtures,
  data files, docs, and lockfiles.

## Conventions

- Python >= 3.14 (the release that added `compression.zstd`), `src/` layout.
- Lint/format with `ruff`; test with `pytest`.
- Access configuration through `Config.load()`, not `os.environ` directly.

## Versioning

The app uses [semantic versioning](https://semver.org/); the current version is
**1.6.0**. The single source of truth is `mailing_list_ai_check.__version__`
(in `__init__.py`); `pyproject.toml` reads it dynamically, so the two never
drift.

Bump policy — ordinary semantic versioning, with no component reserved for any
one subsystem:

- **major** — a breaking change.
- **minor** — a new feature, or any other user-visible change, including a
  raised Python floor.
- **patch** — a bug fix, or an internal change with no user-visible effect.

Extraction changes are not tied to the app version; they carry their own number
(see "Extraction version" below).

## Extraction version

`EXTRACTION_VERSION` (an `int` in `extraction.py`) identifies the routine that
derives an extraction's text: `extraction.py`, `cleaning.py` and `html_text.py`
taken together. It is independent of the app version and is incremented
separately.

- Increment it by one, by hand, in the same commit as any change to those three
  modules that could alter the extracted text, the cleaned text sent to Pangram,
  or an extraction's status — a whitespace-only difference included. Do not
  increment it for comments, docstrings, type annotations or refactors that keep
  every output byte identical.
- It only ever increases. `staleness.check()` compares a stored stamp with the
  running value using `<`, so an older app opening a store written by a newer
  routine reads that store as current instead of offering to re-derive text it
  cannot reproduce.
- `tests/test_extraction_version.py` pins the routine's output over the fixture
  corpus as a single SHA-256. An increment requires re-recording `EXPECTED_DIGEST`
  and `DIGEST_EXTRACTION_VERSION` in that file in the same commit; the test fails
  otherwise, and it also fails when the output moves without an increment.
- Two generations exist: **1** (initial release) and **2** (from v1.2.0).

Version stamps in the database:

- `messages.pipeline_version` — the app version that last ran a pipeline stage
  end-to-end against the message; stamped on insert and re-stamped whenever its
  extraction or score is written.
- `extractions.pipeline_version` — the app version that wrote the extraction row.
  Provenance only: it names the release, and scoring never touches it.
- `extractions.extraction_version` — the generation of the routine that produced
  the text (migration 011, nullable, backfilled from `pipeline_version`). This is
  the only value the staleness check compares (see `staleness.py`); NULL reads as
  older than every generation.

## Changelog — maintain it

`CHANGELOG.md` records every release, newest first, and is parsed by scripts.
Its own "Format" section is the specification; keep to it exactly.

- Every version bump gets a section. Bumping `__version__` without adding or
  updating a `CHANGELOG.md` section is incomplete work.
- Section shape, in this order: `## [<version>] - <date>`, then one
  `Summary: <one line>` line, then one `- ` bullet per individual change.
- `<date>` is `YYYY-MM-DD` for a committed release, or the literal
  `unreleased` while the version is bumped in the source but not yet
  committed. Replace `unreleased` with the commit date when releasing.
- One line per bullet — no wrapped continuation lines, no nested bullets, no
  extra headings or prose inside a release section.
- Write bullets at the granularity of an individual change (a new endpoint, a
  renamed control, a new extraction rule), not per file touched.
- The documentation style rule above applies: factual and impersonal.
- Do not rewrite the history of released sections; add to the newest one, or
  start a new one.
