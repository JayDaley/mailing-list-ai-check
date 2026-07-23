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

## Conventions

- Python >= 3.11, `src/` layout.
- Lint/format with `ruff`; test with `pytest`.
- Access configuration through `Config.load()`, not `os.environ` directly.

## Versioning

The app uses [semantic versioning](https://semver.org/); the current version is
**1.0.1**. The single source of truth is `mailing_list_ai_check.__version__`
(in `__init__.py`); `pyproject.toml` reads it dynamically, so the two never
drift.

Bump policy (for now):

- **minor** — any change to extraction or post-extraction processing
  (`extraction.py`, `cleaning.py`, `html_text.py`, the scoring pipeline logic —
  anything that could change the derived text or what is sent to Pangram).
- **patch** — every other change.

Each message records the pipeline version that last processed it
(`messages.pipeline_version`), stamped on insert and re-stamped whenever its
extraction or score is written.
