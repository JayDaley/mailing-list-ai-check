# Pangram AI-detection API — Phase 0 spike findings (task 0.3)

> **Live verification DONE (2026-07-21)** — `spikes/pangram/verify_api.py` ran
> against the live service with a real key (3 samples, within the 10-call
> testing cap). The documented contract held exactly: endpoint
> `text.external-api.pangram.com`, `x-api-key` auth, async `POST /task` →
> poll `GET /task/{id}` to `STAGE_SUCCESS`, and every response field below.
> Detector `version: "3.3.2"`. Deltas / additions from the live run:
>
> - **Submit returns HTTP 200** (not 202) with `{task_id}`.
> - **The 50-word minimum is NOT enforced server-side**: a 10-word text was
>   accepted and scored with `confidence: "High"`. The too-short gate is
>   therefore entirely the client's responsibility (Phase 4 must enforce it).
> - `headline` strings observed: `"AI Generated"`, `"Human Written"`.
> - `windows[].token_length` exists (redacted by the script's sanitizer only
>   because its name contains "token" — it is a count, not a credential).
> - Caveat: both longer samples scored `fraction_ai: 1.0` — correctly, since
>   both were authored by an LLM sub-agent (including the one *styled* as
>   human). Behaviour on genuinely human text is still unvalidated until real
>   corpus messages are scored in Phase 4.

## Sources

- SDK source (authoritative for the wire contract): `pangramlabs/pangram-sdk`
  `pangram/text_classifier.py`, version **0.3.1** (released 2026-06-11)
  <https://github.com/pangramlabs/pangram-sdk>
- PyPI: <https://pypi.org/project/pangram-sdk/>
- REST API docs: <https://pangram.readthedocs.io/en/latest/api/rest.html>
- v3 migration guide: <https://www.pangram.com/blog/v3-api-migration-guide>
- Pricing / API product page: <https://www.pangram.com/solutions/api>
- Minimum word count rationale:
  <https://www.pangram.com/blog/why-does-pangram-have-a-minimum-word-count>

> Note on endpoint naming: the v3 migration blog post refers to
> `https://text.api.pangram.com/v3`, but the current SDK (0.3.1) and the
> readthedocs REST reference both use the **async task** hosts documented below
> (`text.external-api.pangram.com`). The SDK source is treated as canonical
> here. Confirm live which host answers before Phase 4 hard-codes it.

---

## 1. Endpoints and auth

All endpoints authenticate with a single request header:

```
x-api-key: <PANGRAM_API_KEY>
```

(No `Bearer`, no `Authorization` header.) JSON endpoints also send
`Content-Type: application/json`. The file-upload endpoint uses
`multipart/form-data` and sends only the `x-api-key` header.

| Purpose | Method | URL |
|---|---|---|
| Submit single-text task | `POST` | `https://text.external-api.pangram.com/task` |
| Poll task result | `GET` | `https://text.external-api.pangram.com/task/{task_id}` |
| Submit bulk job | `POST` | `https://text.external-api.pangram.com/bulk` |
| Bulk job status | `GET` | `https://text.external-api.pangram.com/bulk/{bulk_id}` |
| Bulk item metadata (paged) | `GET` | `https://text.external-api.pangram.com/bulk/{bulk_id}/items` |
| Bulk results (paged) | `GET` | `https://text.external-api.pangram.com/bulk/{bulk_id}/results` |
| File upload (docx/pdf/rtf) | `POST` | `https://file-external.api.pangram.com/` |
| Plagiarism check | `POST` | `https://plagiarism.api.pangram.com` |

**Single-text detection is asynchronous**, even though the SDK's `predict()`
hides it behind one blocking call: `POST /task` returns a `task_id`, then the
caller polls `GET /task/{task_id}` until `stage` is terminal. Phase 4 only needs the
single-text and (optionally) bulk endpoints; file-upload and plagiarism are out
of scope for scoring mailing-list text.

### Request schema — single text

`POST /task`:

```json
{ "text": "<text to classify>", "public_dashboard_link": false }
```

`public_dashboard_link` is optional (default `false`); when `true` the completed
result includes a `dashboard_link` URL to a hosted result page. Public dashboard
links must not be created for private mailing-list mail — leave it `false`.

There is **no sliding-window request option** in v3. The service windows the
text itself and returns per-window scores (see below). In v2 there were separate
`text-sliding`/`text-extended` endpoints with overlapping windows; v3 folds all
of that into the one endpoint and uses **non-overlapping** windows.

### Request schema — bulk

`POST /bulk` accepts exactly one of:

```json
{ "text": ["first text", "second text"] }
```
or
```json
{ "items": [ {"id": "row-001", "text": "..."}, {"id": "row-002", "text": "..."} ] }
```

`id` is an optional caller-defined key echoed back with the item's status and
result — useful for mapping results to `extractions.id`. `POST /bulk`
returns HTTP **202** with `bulk_id`, `status` (`"queued"`), `total_items`,
`accepted_items`, `failed_items`. Poll `GET /bulk/{bulk_id}` until `status` is
terminal (`succeeded` / `failed` / `partial`), then page through
`GET /bulk/{bulk_id}/results` (`offset`, `limit`; **limit max 1000**). Each
completed item carries a `result` object with the **same schema as a single-text
result**; still-running items have `result: null`; failures land in
`failed_items` with an `error`.

---

## 2. Response schema (single text and per-bulk-item `result`)

From the SDK docstring and README, a successful result dict contains:

| Field | Type | Meaning |
|---|---|---|
| `stage` | str | Terminal async stage; `"STAGE_SUCCESS"` on success, `"STAGE_FAILED"` on failure. |
| `text` | str | Echo of the input text. |
| `version` | str | API/model version identifier, e.g. `"3.0"`. |
| `headline` | str | Human-facing headline, e.g. `"Fully Human Written"`, `"AI Assisted"`. |
| `prediction` | str | Long-form explanation of the classification. |
| `prediction_short` | str | **Compact label** — one of `"AI"`, `"AI-Assisted"`, `"Human"`, `"Mixed"`. |
| `fraction_ai` | float 0–1 | Fraction of the document classified AI-written. |
| `fraction_ai_assisted` | float 0–1 | Fraction classified AI-assisted. |
| `fraction_human` | float 0–1 | Fraction classified human-written. |
| `num_ai_segments` | int | Count of segments classified AI. |
| `num_ai_assisted_segments` | int | Count of segments classified AI-assisted. |
| `num_human_segments` | int | Count of segments classified human. |
| `dashboard_link` | str | Present only when `public_dashboard_link=true`. |
| `windows` | list | Per-window sub-scores (see below). |

**The three `fraction_*` fields are mutually exclusive and sum to 1.0.** There
is no single scalar "AI likelihood" field in v3 — the closest analogue is
`fraction_ai` (or `fraction_ai + fraction_ai_assisted` if any AI involvement
is treated as positive). `prediction_short` is the categorical label.

### `windows[]` — per-window sub-scores

Each element:

| Field | Type | Meaning |
|---|---|---|
| `text` | str | The window's text. |
| `label` | str | Descriptive label, e.g. `"AI-Generated"`, `"Moderately AI-Assisted"`, `"Human"`. |
| `ai_assistance_score` | float 0–1 | AI-assistance level for the window; 0 = none, 1 = fully AI-generated. |
| `confidence` | str | `"High"` / `"Medium"` / `"Low"`. |
| `start_index` | int | Start char offset in the original text. |
| `end_index` | int | End char offset in the original text. |
| `word_count` | int | Words in the window. |
| `token_length` | int | Token length of the window. |

Windows are non-overlapping and allow the dashboard (Phase 6 detail view) to
highlight which passages drove the verdict.

### Error shape

The SDK treats any JSON body containing an `"error"` key as a failure and
raises. Documented HTTP error codes (file endpoint, likely shared): **400**
(bad request), **401** (bad/missing key), **402** (out of credits / payment
required), **413** (payload too large), **415** (unsupported media type), **422**
(unprocessable — e.g. too-short/empty text, to confirm), **500** (server).
Task-level failures come back as `stage: "STAGE_FAILED"` with a `headline` /
`detail` message rather than a non-200 code.

---

## 3. Text-length constraints

- **Minimum: 50 words.** Pangram enforces a 50-word minimum "to make a
  prediction that you can trust"; below that even humans can't tell AI from
  non-AI (their "delve" example). This is a *word* minimum, not a character
  minimum. Marketing copy separately claims it accepts text "as short as 50
  characters" — treat that as unverified and likely referring to a different
  (older/short) endpoint; **the reliable floor is 50 words.**
- **Maximum: not documented.** No explicit cap was found. Long texts are handled
  server-side via windowing rather than a hard limit; billing is per word, so
  cost (not a length error) is the practical constraint. Confirm behaviour on a
  very long message (e.g. a 5000-word thread digest) during live verification.

**Recommended "too short to score" threshold for Phase 4: < 50 words.**
Rationale: it matches Pangram's own documented reliability floor exactly, so no
verdict the vendor documents as unreliable is ever paid for or stored.
Brief mailing-list replies ("+1", "LGTM", "see inline") fall well under this and
should be marked `too_short` in `extractions.status` and never sent. (The PLAN's
"roughly under 100 chars" note is a looser proxy; prefer the 50-*word* rule and
keep a char guard too — see Decision inputs.)

---

## 4. Rate limits, pricing, batching

- **Pricing (realtime / single-text):** **$0.05 per 1,000 words.**
- **Pricing (bulk):** **$0.04 per 1,000 words** — a 20% discount.
- **Billing model:** prepaid developer credits ($5–$2,000, optional auto-refill);
  billed per word, so short texts are cheap but the 50-word floor means a typical
  scored reply costs a fraction of a cent.
- **Rate limit (realtime):** ~**5 QPS**. Bulk is metered differently
  ("1,000 billable units").
- **Higher limits / zero-retention / SOC 2:** enterprise contract only.

No per-call minimum charge was documented, but the 50-word floor effectively
sets the minimum billable unit.

---

## 5. SDK vs raw `requests`

**Recommendation: use raw `requests` in `pangram.py` (Phase 4), not the SDK.**

Reasons:
1. The project already depends on `requests` and deliberately keeps its
   dependency set tiny; `pangram-sdk` pulls in an extra package for what is a
   two-call POST/poll loop.
2. Phase 4 needs behaviour the SDK does **not** provide: retry with backoff and
   explicit 429/5xx handling. The SDK raises a plain `ValueError` on any
   non-2xx and does no retry — it would need wrapping anyway.
3. The key must only ever be read from `Config` and never logged; owning the
   HTTP layer makes that straightforward to audit.
4. The wire contract is small and stable enough to reimplement safely, using the
   SDK source as the reference (constants copied into `verify_api.py`).

Keep `spikes/pangram/verify_api.py` as the contract oracle: it mirrors the SDK's
endpoints/stages so a live run confirms the hand-rolled client matches.

---

## Decision inputs (for Fable → PLAN "Decisions" and Phase 4 design)

**Fields to store** (map onto the `scores` table in Phase 1):

- Normalized columns:
  - `ai_likelihood` ← store **`fraction_ai`** (primary scalar). Consider also
    persisting `fraction_ai_assisted` and `fraction_human` as their own columns,
    since "AI-assisted" is a distinct category
    — recommend adding `fraction_ai_assisted` and `fraction_human` to the schema
    rather than collapsing to one number.
  - `label` ← store **`prediction_short`** (`AI` / `AI-Assisted` / `Human` /
    `Mixed`). This is the clean categorical for filtering in the API/dashboard.
    *Correction (July 2026, observed in production):* `prediction_short` never
    actually emits `AI-Assisted` — assisted-dominated text (even
    `fraction_ai_assisted == 1.0`) comes back as `Mixed`, with only the
    free-text `headline` saying "AI Assisted". `PangramResult.label` now
    rebadges `Mixed` to `AI-Assisted` when the assisted fraction exceeds both
    others (store migration 003 backfilled existing rows).
  - `scored_at`, `text_sha256` (cache key), and the model `version` string.
- Raw JSON blob (`raw_response`): store the **entire** response, including
  `headline`, `prediction`, `num_*_segments`, and the full `windows` array. The
  windows power the Phase 6 highlight view; keeping them only in the blob avoids
  a second table now.
- Do **not** store `dashboard_link` (the client sends `public_dashboard_link=false`).

**Too-short threshold:** `< 50 words` → mark extraction `too_short`, never send.
As an additional guard, also skip empty text and anything `< ~250 chars` only if
it is also `< 50 words` (words is the authoritative gate; the char check avoids
edge cases like a 50-word run of single characters). Store the reason so the
dashboard can distinguish "too short" from "scored".

**Rate-limit / batching strategy:**

- Respect ~5 QPS on the realtime `/task` endpoint: a simple client-side limiter
  (≤ 5 in-flight per second) with exponential backoff on `429` and `5xx`.
- For catch-up / backfill runs over many messages, prefer the **Bulk API**
  (`/bulk`): 20% cheaper and designed for async fan-out. Use item `id` =
  `extractions.id` to reconcile results. For incremental day-to-day scoring of a
  handful of new messages, realtime `/task` is simpler.
- `predict()`-equivalent timeouts to copy: 10s per HTTP request, 300s overall
  task deadline, 0.5s poll interval (bulk: 3600s overall).

**Cost-control recommendations:**

- **Cache on `text_sha256`** — never pay twice for identical extracted
  text (already in the Phase 1 schema; this is the largest cost saving given
  repeated quoted/boilerplate content).
- Enforce the 50-word floor *before* any network call.
- Honour the PLAN's `--limit N` per run and print a running word/call total and
  estimated spend ($0.05/1k words realtime, $0.04/1k words bulk) at the end.
- Optionally cap per-text word count sent (very long threads cost proportionally
  more) — decide during Phase 4 once live windowing behaviour is confirmed.
