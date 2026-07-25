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

- Python >= 3.11, `src/` layout.
- Lint/format with `ruff`; test with `pytest`.
- Access configuration through `Config.load()`, not `os.environ` directly.

## Versioning

The app uses [semantic versioning](https://semver.org/); the current version is
**1.2.4**. The single source of truth is `mailing_list_ai_check.__version__`
(in `__init__.py`); `pyproject.toml` reads it dynamically, so the two never
drift.

Bump policy (for now):

- **minor** — any change to extraction or post-extraction processing
  (`extraction.py`, `cleaning.py`, `html_text.py`, the scoring pipeline logic —
  anything that could change the derived text or what is sent to Pangram).
- **patch** — every other change.

Each message records the pipeline version that last processed it
(`messages.pipeline_version`), stamped on insert and re-stamped whenever its
extraction or score is written. Each extraction separately records the version
that produced its text (`extractions.pipeline_version`), stamped on insert and
rewritten only on re-extraction — scoring never touches it. Because a minor bump
is what an extraction change gets, comparing that stamp's `(major, minor)` pair
with the running version is how the app detects text derived by an older routine
(see `staleness.py`); keep the bump policy above exact, or that detection is
wrong.

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
