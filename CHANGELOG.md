# Changelog

All notable changes to `mailing-list-ai-check`, newest release first. The
project follows [semantic versioning](https://semver.org/); the bump policy is
recorded in `CLAUDE.md`.

## Format

The file is machine-readable. Every release is one section with exactly three
kinds of line, in this order:

```
## [<version>] - <date>

Summary: <one-line summary of the release>

- <one-line description of an individual change>
- <one-line description of an individual change>
```

Extraction rules (skip lines inside fenced code blocks — the template above is
itself a fenced block, and its placeholder header would otherwise parse as a
release):

- Release header: `^## \[(?P<version>[^\]]+)\] - (?P<date>\S+)$`
  `version` is a semantic version (`MAJOR.MINOR.PATCH`). `date` is either an
  ISO-8601 date (`YYYY-MM-DD`) or the literal `unreleased` for a version that
  is bumped in the source but not yet committed as a release.
- Summary: `^Summary: (?P<summary>.+)$` — exactly one per release, the first
  non-blank line after the release header.
- Change: `^- (?P<change>.+)$` — zero or more per release, each a single line
  (no wrapped continuation lines, no nested bullets).

Any other line (headings above level 2, blank lines, prose in this Format
section) is not part of a release record and can be ignored. Outside fenced
code blocks, no release section appears before the first `## [` header.

No 1.1.0 release exists: the version went from 1.0.5 to 1.2.0.

## [1.2.4] - 2026-07-25

Summary: Detect stored text derived by an older extraction routine and offer to re-process the affected messages.

- Add schema migration 008: an `extractions.pipeline_version` column recording the app version that produced each extraction's text, backfilled from `messages.pipeline_version`.
- Stamp `extractions.pipeline_version` on insert and rewrite it on re-extraction; scoring never touches it, so it identifies the routine behind the stored text.
- Add `Store.extracted_message_ids`, `Store.extraction_version_counts`, `Store.set_extraction_version`, `Store.replace_extraction` and `Store.delete_score_for_extraction`.
- Add `extraction_generation`, comparing versions by their `(major, minor)` pair — the granularity at which extraction changes are released.
- Add the `staleness` module: `check` compares recorded versions, `diff` re-derives every stored extraction and reports the ones that differ, `reextract` rewrites chosen rows.
- Stamp extractions that re-derive identically with the running version, so a check that finds no difference stops the prompt returning.
- Delete the score of a re-extracted message only when its cleaned (scored) text changed, leaving verdicts that still apply in place.
- Add `GET /api/staleness`, reporting whether any stored extraction predates the current routine, with per-version counts.
- Add `POST /api/staleness/check`, re-deriving every stored extraction and returning the affected messages.
- Add `POST /api/staleness/reextract` and `POST /api/staleness/rescore`, both taking up to 1000 message ids.
- Add a `message_ids` filter to `run_score`, restricting a scoring run to given messages' extractions.
- Open a prompt at dashboard start-up when stored text may be out of date, with the affected messages in a scrolling table (total, character counts before and after, what changed) and a "Run process ($)" button that re-extracts and re-scores only those messages.
- Add an alert icon beside the header's info button while any extraction predates the running version, reopening the same prompt.
- Document in the README how schema migrations are applied on database open, that they are one-way, and that the front end needs a separate rebuild.

## [1.2.3] - 2026-07-25

Summary: Add an in-dashboard documentation viewer, opened by an info button in the header.

- Add `GET /api/docs`, listing the servable documentation files (`README.md`, `CHANGELOG.md`, and the Markdown files at the top level of `docs/`) with each file's first level-1 heading as its title.
- Add `GET /api/docs/<path>`, returning one file's raw Markdown; a path that is not in the index is a 404, so no request path reaches the filesystem.
- Add a `DOCS_ROOT` app config key and a `docs_root` argument to `create_app`, defaulting to the repository root.
- Add an ⓘ button beside the app name in the header that opens the documentation panel.
- Add `DocsDrawer.vue`: a panel sliding in from the left of the screen with the file index in the left column and the rendered document in the right.
- Render the Markdown with `marked` (new front-end dependency), including GFM tables, fenced code blocks and inline code.
- Rewrite links in a rendered document: repository paths the API does not serve are shown as plain text, links to another listed document switch the viewer, and external links open in a new tab.
- Close the documentation panel on the Close button, Escape, or a click on the backdrop.

## [1.2.2] - 2026-07-25

Summary: Align the dashboard with Pangram's three-category vocabulary and repaint it in the Observable 10 palette.

- Fold the stored four-band label into Pangram's three `prediction_short` buckets (Human / Mixed / AI) for every aggregate view; `AI-Assisted` merges into `Mixed`.
- Add `foldToPrediction`, `predictionShort` and `PRED_ORDER` to the shared label vocabulary, and drop `MIX_CAPTION`.
- Replace the Okabe-Ito palette with Observable 10: blue for Human, orange for Mixed/AI-Assisted, red for AI, grey for unscored, with matching tints behind the percent pills.
- Serve `prediction_short` and Pangram's free-text `headline` from the messages API, parsed out of the stored raw response with a fallback derived from the stored label.
- Select `scores.raw_response` in the message-row query so the API can read the headline and prediction without a second lookup.
- Match both `Mixed` and `AI-Assisted` rows when the dashboard filters on `Mixed`, so the filter agrees with the folded bars and pills.
- Replace the Score column with two columns: Analysis (prediction pill plus headline) and AI Score (Confidence).
- Generalise `MixBar` with `order`, `fold`, `phrases`, `colors` and `show-counts` props so one component draws every aggregate breakdown.
- Show message counts alongside percentages in the aggregate mix-bar hover popup.
- Measure the hover popup off-screen before positioning it, so it is no longer pinned to the left edge on first hover, and clamp it to the viewport width.
- Remove the redundant native `title` tooltip from the mix bar.
- Reduce the label filter to the three prediction buckets and remove the min/max fraction-AI inputs.
- Rename the lists-pane mix column header to "Aggregate analysis".
- Delete `ScoreCell.vue`, whose job the new Analysis and AI Score cells now do.
- Remove `LABEL_MUTED`, the muted mid-tone palette the previous per-message score bar used.
- Add `docs/pangram-output.md`, a reference for every field Pangram returns, its observed value domain, and how each maps to the dashboard.
- Remove the loaded-count footer bar from the messages pane.
- Stack the messages-pane filter controls two rows deep, so the Date column holds one full-width date input per row instead of two half-width ones, and narrow Date from 176px to 120px while widening List from 100px to 156px.
- Return each Pangram window's `ai_assistance_score` and `confidence` from the messages API, so the list can show per-window scores.
- Show the `prediction_short` bucket as the Analysis pill (Human blue, Mixed orange, AI red) with Pangram's headline beside it as plain uncoloured text, the pill in a fixed slot so the headlines all start at the same offset.
- Replace the AI Score column with "AI Score (Confidence)", listing every window's score to two decimal places with its confidence abbreviated to H / M / L, clipped to the column width with a trailing ellipsis.
- Show the window count and every window's score and confidence when hovering the AI Score column.
- Remove the Extraction column from the messages pane, moving its scored / unscored filter under AI Score (Confidence).
- Speak the three bucket names in the mix-bar hover popups instead of Pangram's headline phrases, and remove `PRED_PHRASE`.
- Extract the hover-popup positioning into `lib/hoverPop.js` and the popup styling into a shared `.hover-pop` class, shared by the mix bars and the score cells.
- Locate each Pangram window in the extracted text, reporting its `{line, col}` start and end from the message detail API alongside its label, characters and word count.
- Rename the drawer's score card to "Analysis" and show the prediction pill, the headline and the analysis engine and version ("Pangram detector 3.3.2").
- Replace the drawer's three fraction bars with a table of every window's number, characters, score, confidence and label.
- Combine the drawer's extracted-text and raw-body cards into one text card holding either view: with "Show ignored" off (the default) it shows only the text the checking service saw, and on it shows the whole message with everything else dimmed, drawing no distinction between what extraction removed and what post-processing removed.
- Align the extracted text against the raw body line by line to build the whole-message view, falling back to the extracted text with a note when the two do not align (a message whose text came from the HTML part).
- Mark each window in the drawer text with a numbered box at its first character and a bracket down a right-hand wire gutter spanning its lines, labelled with the same numbered box; where one window ends and the next begins on a line, both brackets share it.
- Draw every window number as an Observable 10 grey box — in the analysis table, inline in the text and beside the bracket — and the brackets in the same grey.
- Light up a window's number boxes and its bracket in the palette's light blue while any of them is hovered, and make the table's box the link that jumps to the window in the text.
- Hover a window's number box anywhere for its score, confidence, label and size, one field per line.
- Show a `too_short` extraction's text in the drawer instead of "(no extracted text)".
- Add `windowBucket` to map Pangram's per-window labels onto the three prediction buckets, colouring the analysis table's label swatches by verdict.

## [1.2.1] - 2026-07-24

Summary: Run the pull pipeline from a staged progress modal in the dashboard.

- Rename the Go and Fetch-and-check buttons to "Run process ($)".
- Drive fetch, extract and check as three sequential API calls with per-stage progress in a centred modal (`RunProcessModal.vue`).
- Add the `/pull/fetch`, `/pull/range/fetch`, `/extract` and `/score` endpoints that back the individual stages.
- Close the originating form or popover when a run starts.
- Replace the list-stats "Pull 50 newest" button with the Add popover's footer button.

## [1.2.0] - 2026-07-24

Summary: Exclude localized quote headers and custom signature blocks from extracted text.

- Recognize Chinese quote-header blocks (发件人 / 发送时间 / 收件人 / 抄送 / 主题) with ASCII or full-width colons, including U+3000-padded 主　题, as produced by Alibaba Mail and Chinese Outlook.
- Drop the dashed divider Alibaba Mail draws above such a quote-header block.
- Recognize the Chinese "Original Message" dividers `-----邮件原件-----` and `-----原始邮件-----`.
- Recognize Japanese attribution lines ("<date>、<who>のメール:", Spark / Apple Mail), anchored on a leading year.
- Truncate at a custom punctuation-rule signature divider ("========") only when the line above is blank and a name line follows, so Markdown heading underlines and authored section breaks do not qualify.
- Allow short capitalized prefixes in identifier-keyword debris lines ("VSO BLOG:") and add D-U-N-S to the keyword set.
- Treat postal-address lines containing a digit ("Tokyo Office: … 150-0021 …") as per-line debris.
- Treat "Label: URL" lines inside a sign-off's trailing block as debris.
- Reprocessed 16 stored extractions and scores; three verdicts moved to AI 1.0 once leaked quoted text and signature furniture were stripped.

## [1.0.5] - 2026-07-24

Summary: Add per-list message preview and ranged fetch to the lists pane.

- Add an "Add" button per list row opening a two-tab popover: "New since last fetch" and "Before last fetch".
- Preview candidate messages server-side (sender, subject, date) before pulling anything.
- Add `POST /api/lists/preview`, a read-only header fetch used by the preview tabs.
- Add `POST /api/pull/range`, a directional pull that caps "all" at 1000 messages and never regresses the incremental cursor.
- Fetch, extract and score the chosen range from the popover.
- Replace the Show active / Show all button pair with a "Show All" switch.
- Rename and restyle the lists-pane header buttons to match export/import.

## [1.0.4] - 2026-07-23

Summary: Rewrite the documentation in a factual, impersonal style.

- Remove opinionated flourishes, colloquialisms and first-person voice from the README and everything under `docs/`.
- Add a hard rule to `CLAUDE.md` requiring simple, factual, impersonal documentation.

## [1.0.3] - 2026-07-23

Summary: Add app favicons and a detection-bar hover popup.

- Add `favicon.svg`, `favicon-32.png` and `apple-touch-icon.png`, served from `frontend/public` to the `dist` root, and reference them from `index.html`.
- Show a popup on mix-bar hover giving every label's share ("Human (x%) · Mixed (x%) · Assisted (x%) · AI (x%)").
- Teleport the popup to `<body>` with fixed positioning so scroll-clipping panes cannot cut it off.
- Tighten the shared `MIX_CAPTION` header to "Human·Mixed·Assisted·AI" so the uppercased column header fits on one line.

## [1.0.2] - 2026-07-23

Summary: Add export/import to the dashboard and document the CLI commands.

- Add export and import buttons to the Messages pane header, right of the filter controls.
- Export the active list filter's list, or every list with messages when no filter is set, as the gzip JSON Lines export format.
- Import an uploaded export file and show a compact result digest (inserted / skipped / updated, body mismatches when nonzero) or the server's error, then refresh the pane.
- Add `GET /api/export[?list=<name>]`, streaming the export as an attachment named `mlac-export-<list>-<date>.jsonl.gz`.
- Add `POST /api/import`, taking a multipart upload with an optional `dry_run` and returning the import summary as JSON, or 400 with the reason on validation failure.
- Add an "Exporting and importing lists" section to the README covering `mail-ai-export` and `mail-ai-import`.
- Stop hardcoding the version in the README; it points at `mailing_list_ai_check.__version__`.

## [1.0.1] - 2026-07-23

Summary: Improve the dashboard filters and the sender pane.

- Offer only lists that have messages in the list filter dropdown, instead of every list in the IMAP index.
- Offer every sender in the displayed list(s) — linked persons and unlinked addresses — in the From filter, scoped live to the list filter.
- Set the address filter when an unlinked address is picked; "anyone" clears both sender filters.
- Show an unlinked sender's display name above the email address on the sender detail card, instead of the bare address as the title.

## [1.0.0] - 2026-07-23

Summary: First versioned release, adding export/import of lists with their full pipeline state.

- Add `mail-ai-export` and `mail-ai-import`, moving a list row, pull cursor, senders/persons, messages, extractions and Pangram scores between databases as one JSON Lines file (gzip via a `.gz` suffix).
- Document the format and design in `docs/export-import.md`.
- Store extraction text as a full-body marker or a character span into the static `raw_body`, inlining it only when it is not a contiguous substring, always with a SHA-256 the importer verifies.
- Carry what was sent to Pangram as its stored `text_sha256` plus the verbatim `raw_response`.
- Make import all-or-nothing: one transaction, rolled back on any error, with `--dry-run` running the same path and rolling back.
- Dedupe messages on (list, Message-ID) so re-imports are no-ops, skipping existing rows and their embedded extraction and score.
- Warn about, and never overwrite, a skipped message whose stored body differs from the file copy.
- Refresh an existing message's extraction and score when the file copy carries a later pipeline version and the derived data differs; otherwise advance only its version stamp.
- Introduce semantic versioning at 1.0.0, sourced solely from `mailing_list_ai_check.__version__`, with `pyproject.toml` reading it dynamically.
- Add migration 007 (`messages.pipeline_version`), stamped by pull, extract and score so each message records the version that last processed it.
- Record the app version in the export header and each message's pipeline version in its record.
- Document the bump policy in `CLAUDE.md` and the README: minor for extraction and post-extraction processing changes, patch for everything else.
