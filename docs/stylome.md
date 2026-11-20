# Stylome scoring

`mail-ai-score-stylome` scores stored extractions through the Stylome 1.2
developer API. It uses the maintained [Stylome Python SDK](https://github.com/trentmkelly/stylome-python),
pinned to the tested 0.3.0 release commit. This app adapter owns mail selection,
normalization, evaluation-identity checks and result storage; the SDK owns HTTP,
authentication, typed responses and API errors. It is an optional command alongside `mail-ai-score` (Pangram).
Pull and extraction commands are shared. No Pangram key is needed for Stylome.

## Configuration and use

Install the package after checking out this version. Set `STYLOME_API_KEY` in
the local environment or the gitignored `.env`. Use a standard-retention
developer key: zero-retention keys do not support the idempotency header used
by this client. The key is sent only to `https://stylome.io/api/v1/score`.

The default model is `stylome-1.2-large`, operating point `0.01%`, evaluation
identity `eval_12_002`. Set `STYLOME_OPERATING_POINT` to `0.1%` to select the
other point. `STYLOME_EVALUATION_VERSION` pins the expected response identity;
a response with another identity stops the run. Review a service revision
before explicitly changing this value. These identities are part of the cache
key. The operating points are calibration FPR targets, not guarantees on mail.

Use a copy of the database for initial testing. Opening it applies schema
migrations, including during a dry run. Do not open a frozen measurement
database with a newer app version.

```bash
mail-ai-extract --db ./data/comparison.db
mail-ai-score-stylome --db ./data/comparison.db --dry-run --limit 10
mail-ai-score-stylome --db ./data/comparison.db --limit 10
mail-ai-score-stylome --db ./data/comparison.db --operating-point '0.1%' --limit 10
mail-ai-score-stylome --db ./data/comparison.db --export ./data/stylome-results.jsonl
```

The default cap is ten billed texts per invocation. Larger runs require an
explicit `--limit`. The dry run reports candidate calls, duplicates and length
gates without calling either detection service or changing extraction text.
Successful calls count normalized Unicode characters in the summary. Consult
the service's usage response for billed usage; this command does not estimate
cost from a word count. No live calls are part of the automated tests.

## Input and results

The command refreshes replies whose parent arrived after extraction, then uses
the shared `clean_for_scoring` function and HTML signature hint. Automatic mail
is excluded. The same 50-word floor is applied after cleaning; Stylome also
requires 200–10,000 characters after whitespace collapse and UTF-8 replacement
of lone surrogates. Short and long inputs are counted separately and left
unscored. They are not human verdicts. Long inputs are not truncated, split or
aggregated. A whole-message comparison must declare this denominator difference.

Each result keeps its native `human_written`, `ai_edited` or `ai_generated`
label and relative `confidence`. Confidence is not a probability of correctness.
The export derives `any_ai` as either AI label. It does not equate `ai_edited`
with Pangram's `Mixed` category. Neither service's verdict establishes authorship.
The public Stylome response has no Pangram-style text fractions, distance score
or per-window analysis; the integration does not manufacture them.

The `stylome_scores` table is independent of `scores`. Its cache key includes
the exact normalized cleaned-text hash, model, operating point and evaluation
identity. Duplicate inputs can reuse a verdict across messages. A change to
the cleaned input hides the old verdict from message detail and requires a new
score. Retained old rows remain in the dedicated export with `current_input`
false. Each export row includes list name, Message-ID, local message/extraction
IDs, date, exact submitted text, hash, native response and scoring time.
Receipt share tokens are removed before storage and export.

The message-detail drawer displays current Stylome results in separate cards.
The dashboard's aggregate charts, filters, scoring buttons, automatic score
stage, part checks, full portable export/import and statistics export continue
to use Pangram. Use the new CLI and its `--export` for Stylome. This first
integration does not merge the two detectors' aggregates or import Stylome
results through the Pangram interchange format.

## Failures and retention

Each request uses a deterministic idempotency key based on normalized text,
model, point and evaluation identity. Standard-retention API keys can replay
that request within 24 hours. The client sends one POST and does not follow
redirects or automatically retry. A transport error can mean billing completed
before the response was lost. The command stops on the first failed call and
returns a nonzero exit status; completed rows stay in the database. Preserve
the database before a manual rerun. After the replay window, a missing result
can incur another charge.

Message text goes to the selected external service only when the scoring
command is explicitly run. Source privacy, attribution and redistribution
restrictions still apply to the database and export. The command has no
permission to publish mail or its results.
