# Handoff: Single-screen dashboard redesign

## Overview
Redesign of the Mail AI Check web UI from a five-route app (Overview / Messages / People / Lists / Detail) into a **single dense, always-visible dashboard screen**: a filterable message table on top, a context pane (lists index / list stats / person card) and a senders pane below, and a slide-over drawer for message detail. Everything is cross-linked: clicking a list, sender, label, chart bar or mix-bar segment applies a filter, and every pane reacts to the same shared filter state.

## About the design file
The reference is `Mail AI Dashboard.dc.html` (an HTML prototype with self-contained mock data). It is a **design reference, not production code**. Recreate it in the existing frontend environment: **Vue 3 + Vite + Pinia + vue-router** (`frontend/`), talking to the existing Flask `/api` blueprint (`src/mailing_list_ai_check/webapp/api.py`). Reuse the existing `api.js` client, `filters` and `ui` Pinia stores, and `AsyncState.vue` where they fit.

## Fidelity
**High-fidelity.** Colors, spacing, typography and interactions are final. Recreate pixel-perfectly. The mock data generator, however, is throwaway — wire everything to the real API.

## Relationship to the current app
- The five routes collapse into one screen at `/`. Keep vue-router if convenient (e.g. drawer as `/messages/:id`), but the primary model is one page whose filter state syncs to the URL query (the filters store already does URL sync — keep that behavior).
- The global `FilterBar.vue` is replaced by the in-table column filter row + filter chips (see below).
- Anonymous mode remains a global toggle (ui store) but now also collapses layout (see Anonymous mode).

## Screen layout
Root: `100vh`, `overflow hidden`, column flex, `font-variant-numeric: tabular-nums`, page bg `#f4f5f7`.

1. **Header** — 40px, white, bottom border `#e2e5e9`, padding `0 16px`, gap 14px. Brand "Mail AI Check" (13px, 700, letter-spacing -0.01em) · header stat in mono 11px `#8a929b` (e.g. "1,412 msgs · 8 lists · db 2.1 MB" — from `/api/summary`) · spacer · "Anonymous" checkbox (11px 600 `#626a72`, accent-color `#2f6feb`).
2. **Content area** — padding `10px 16px 16px`, column flex.
   - **Messages pane** (card) — height = draggable `topPct` (default 58%, clamp 18–80%).
   - **12px horizontal drag handle** (`cursor: row-resize`).
   - **Lower row** (fills rest): **Context pane** (width = draggable `leftPct`, default 42%, clamp 20–75%) · **12px vertical drag handle** (`cursor: col-resize`) · **Senders pane** (flex 1).

All three panes share the card style: white bg, `1px solid #e2e5e9`, radius 6px, shadow `0 1px 2px rgba(20,24,28,0.05)`, `overflow hidden`, internal column flex with its own scroll region. Pane header bars: `#fafbfc` bg, bottom border `#e2e5e9`, padding `6px 12px`, title 12px 700, subtitle 11px `#8a929b`.

Persist `topPct` / `leftPct` (localStorage is fine).

## Messages pane

### Toolbar (in the pane header bar, wraps)
- Title "Messages" + "{n} shown" (mono 11.5px `#626a72`) — n = filtered total from API.
- **Detection-mix bar**: 200×10px, radius 2px, track `#eef0f3`; stacked segments Human→Mixed→AI-Assisted→AI proportional to scored messages *in the current filter*; each segment clickable → sets `label` filter; tooltip "Label: n of N scored (x%) — click to filter". Caption after it: "Human · Mixed · Assisted · AI" (mono 10px `#8a929b`).
- **Filter chips** — one per active filter: `key=value` (person shows name), mono 10.5px 600, bg `#eaf1fe`, text `#1f52bf`, border `#c9dbfa`, radius 3px, × button clears that filter.
- Right: "clear filters" text button (`#2f6feb`, 11px 600; opacity 0.4 + disabled when no filters).

### Table
Horizontal scroll with `min-width: 1080px`. Grid columns:
`176px 100px 170px minmax(240px,1fr) 140px 172px 64px`
= Date · List · From · Subject · Extraction · Score · Chars. (From column collapses to 0 in anonymous mode.)

**Header row** (sticky with the filter row, z-5, white bg): 10px 700 uppercase, letter-spacing 0.05em, `#626a72`. Date and Score are sortable (click toggles desc→asc, indicator " ▲"/" ▼", hover darkens to `#1c2024`). API: `sort=date|likelihood`, `order`.

**Column filter row** (bg `#fafbfc`): controls are 21px tall, radius 3px, border `#dfe3e8` idle / `#2f6feb` when that filter is active:
- Date: two `type=date` inputs (from/to, 50% each) → `date_from`/`date_to`.
- List: text combobox, placeholder "any list…"; on focus opens a dropdown (abs-positioned, shadow `0 8px 24px rgba(15,18,22,0.16)`, max-height 220px) listing "(all lists)" + lists with msg counts, filtered as you type; mousedown picks (blur closes after ~120ms).
- From: person `<select>` ("anyone" + persons, width 92px) + exact-email text input. Person and address filters are mutually exclusive — setting one clears the other.
- Subject: `type=search` "subject / text…" → `q`.
- Extraction: select any/scored/unscored → `has_score`.
- Score: label select (any/AI/AI-Asst/Mixed/Human) + min/max number inputs (0–1 step 0.05, 42px wide) → `label`, `min_likelihood`, `max_likelihood`.

**Rows**: bottom border `#f2f4f6`, hover `#f4f7fb`, cursor pointer, whole row opens the drawer. Cell padding `2px 10px` (compact) / `6px 10px` (comfortable density setting). 
- Date: mono 11px `#626a72`, `YYYY-MM-DD HH:mm`.
- List / From: links (500 weight) that apply the list / person-or-address filter (stopPropagation; From links to person when the address is linked, else exact address; tooltip shows the email(s)).
- Subject: ellipsized.
- Extraction: mono 11px `#8a929b`, `ok·bottom-post` style (status·method) or bare status.
- Score: right-aligned; **label badge** (white text, 10.5px 700, radius 3, padding 0 6px, bg = label color) + **percent pill** (mono 11px 700, min-width 36px, bg = band tint, text = band color). Unscored: "—" in `#b3b9c0`.
- Chars: mono 11px right-aligned, `toLocaleString()`, "—" if 0.

**Infinite scroll**: load 60 rows, append 60 more when within 240px of the bottom (map to `page`/`per_page` on `/api/messages`). Footer strip (bg `#fafbfc`, mono 10.5px `#8a929b`): "{loaded} of {total} loaded · scroll for more".

Empty state: centered "No messages match the current filters." in `#8a929b`.

## Context pane (left, lower)
Header title "Lists" + a subtitle describing the mode. Three mutually exclusive modes driven by filter state:

### 1. Lists index (default — no list/person/address filter)
Subtitle "lists index". Grid `1fr 44px 150px 88px`: List (mono link-blue 500) · Msgs (right, mono) · mix bar (9px tall, same segment logic/colors as elsewhere) · Synced (right, mono 10.5px `#8a929b`). Row click filters to the list. Column caption row: 9.5px 700 uppercase `#8a929b`, caption for the bar is "Human · Mixed · Assisted · AI".
Below: **"+ Add list"** primary button (opens inline form: list-name text input mono + count number input (default 50, 1–1000) + "Go" primary + "✕" cancel, in a `#f7f8fa` box, with the note "Scoring sends extracted text to the paid Pangram API." 10px `#8a929b`) → `POST /api/pull {list, count}`; and **"Regenerate index"** secondary button → `POST /api/lists/regenerate`. Status text (mono 10.5px `#626a72`) shows progress ("pulling and scoring…") then the result summary returned by the API.

### 2. List stats (a `list` filter is active)
Subtitle "per-list aggregates". Card: list name (mono 14px 700) + × to clear the filter; "last synced …" 10.5px `#8a929b`.
- 4 stat tiles (bg `#f7f8fa`, radius 4, padding 5px 8px; value 13.5px 700 mono, caption 9px 700 uppercase `#8a929b`): Msgs, Scored, Avg AI (value colored by band), Flagged (= AI + AI-Assisted count).
- Two-column section: **Labels** — horizontal bars per label (row: 64px label 11px `#626a72`, 7px track `#eef0f3` with fill in label color, width ∝ count/max, count right mono; click sets `label` filter). **By month** — 52px-tall mini bar chart, one bar per month (bar `#2f6feb`, empty `#e2e5e9`, min height 3px, 8.5px month captions; click sets that month as the date range).
- **Top senders on this list** (hidden in anonymous mode): up to 6 rows, grid `1fr 44px 56px`: name (link-blue) · count · avg-AI pill (band tint/color). Click filters to that person/address.
- Footer: "Pull 50 newest" secondary button (+ mono status text) → `POST /api/pull`.

### 3. Person card (a `person` or `address` filter is active, not anonymous)
Subtitle "sender across all lists". Name (14px 700) + × to clear; all attached emails (mono 10.5px `#626a72`). 3 stat tiles: Posts, Avg AI (band color), Flagged. **Activity by list**: rows list (mono link-blue) · count · avg pill; click filters to list. **Labels**: same bar rows as list stats.

## Senders pane (right, lower — hidden entirely in anonymous mode)
Header: "Senders" + "by emails sent · manage address links with ⇄" + right-aligned search input (150px) filtering by name or email.
Sticky column header, grid `minmax(0,1fr) minmax(0,1.2fr) 58px 150px 34px`: Sender (sortable, name asc default direction) · Address · Emails (sortable, default sort, desc) · mix caption · Link.
Rows are one entry per **person** (linked group) or **unlinked address**:
- Sender: link-blue, click applies the person/address filter; linked persons get a "⇄ n" suffix (10px `#8a929b`).
- Address: all emails comma-joined, mono 10.5px, ellipsized, full list in tooltip.
- Emails: count, right mono.
- Mix bar: 9px, per-sender label mix.
- **⇄ button** (bordered, blue when linked / grey when not) opens a 300px popover (abs-positioned right, flips above for rows near the bottom; a fixed full-screen click-catcher closes it):
  - *Linked person*: "Linked sender · n addresses" caption; rename input (commits on blur via `PUT /api/persons/:id`); address chips each with a × detach; "Unlink all addresses" danger button (`confirm()`, then `DELETE /api/persons/:id`).
  - *Unlinked address*: shows the email; if other unlinked addresses share the display name, a primary "Link with n same-name address(es)" button (creates a person from them — `POST /api/persons`; see also `GET /api/persons/suggestions`); a "add to existing sender…" select + "Link" button (`PUT` to add the address); "New sender from this address" secondary button.

Infinite scroll like the messages table. Empty state: "No senders match this search."

## Detail drawer
Opens on row click. Fixed right panel `min(760px, 92vw)`, full height, shadow `-8px 0 32px rgba(15,18,22,0.18)`, behind it a `rgba(15,18,22,0.35)` overlay that closes on click.
- Top bar: ↑ / ↓ buttons stepping through the **current filtered+sorted result set** (disabled at ends), "{i}/{n} in view" mono, spacer, "Close ✕".
- Subject h2 (15px). Metadata grid `88px 1fr`: List (link → filters + closes drawer), From (name + `<email>` mono — hidden in anonymous mode), Date (full UTC), Message-ID (mono, break-all).
- **Pangram score card** (bordered box): "Pangram score" + label badge + "detector v… · scored …" mono meta; three fraction bars (AI / AI-Assisted / Human): 76px key, 8px track, fill in label color, right-aligned percent. Unscored: "Not scored ({extraction status})."
- **Extracted text card**: meta line "status · method · n chars · k lines excluded from AI analysis"; line-numbered mono block (bg `#fbfdff`, 11px/1.55; number gutter 3ch right-aligned, `#b3b9c0`, separated by a border). Lines the cleaner excludes (greeting/sign-off/signature — `ignored_lines` from `/api/messages/:id`) render greyed: bg `#eef0f3`, text `#8a929b`, opacity .75, tooltip "Excluded from AI analysis (greeting/sign-off/signature)".
- **Raw body card**: same line-numbered treatment on bg `#f7f8fa`; lines that made it into the extraction are highlighted `#fff3bf`.

## Anonymous mode
Toggling on: From column width → 0 (and its header/filter cells), person select hidden, Senders pane and its drag handle hidden (context pane takes the full lower width), person card mode disabled, From row in the drawer hidden, "Top senders" section hidden. Any active person filter must be cleared on enable (current `App.vue` already does this — keep it). Everything restores on toggle off.

## State management
One shared filter state (extend the existing `filters` Pinia store): `list, person, address, label, q, minAi, maxAi, dateFrom, dateTo, scored` — all synced to URL query. UI store: `anonymous`, `topPct`, `leftPct`, `openPopover`, `drawerId`, sort states (messages: field+order; senders: field+order), visible-row counts. Invariants: `person` ⟂ `address` (setting one clears the other); any filter change resets pagination.

## API mapping
Existing endpoints cover most of it — `GET /api/messages` (list, address, person, date_from, date_to, label, q, has_score, min_likelihood, max_likelihood, sort, order, page, per_page), `GET /api/messages/:id` (extraction incl. `ignored_lines` + `scored_word_count`, score, thread parent), `GET /api/summary`, `GET /api/lists`, `POST /api/pull`, `POST /api/lists/regenerate`, `GET /api/addresses`, `GET/POST/PUT/DELETE /api/persons`, `GET /api/persons/suggestions`.

Likely gaps to add (verify against `/api/summary` first):
1. **Label-mix + count per list** (lists index bars) and **per-month counts for one list** (mini chart).
2. **Per-sender aggregates**: email count + label mix per person/unlinked address, sortable, searchable (senders pane).
3. **Person/list aggregate cards**: totals, scored, avg fraction_ai, flagged, by-list breakdown for a person, top senders for a list.
4. **Raw body lines** in `/api/messages/:id` if not already returned, plus which raw lines were extracted (or compute the match client-side as the prototype does: trimmed-line equality, ignoring quoted `>` lines).
5. Detection-mix bar in the toolbar needs label counts *under the current filter* — either extend `/api/messages` to return them alongside pagination, or add a filtered summary endpoint.

## Design tokens
Colors: page `#f4f5f7`; surface `#ffffff`; toolbar/tile `#fafbfc` / `#f7f8fa`; borders `#e2e5e9` (strong), `#f2f4f6` (row), `#dfe3e8` (idle input); text `#1c2024`, secondary `#626a72`, muted `#8a929b`, faint `#b3b9c0`; accent `#2f6feb`, hover `#1f52bf`; chip `#eaf1fe`/`#c9dbfa`/`#1f52bf`; hover row `#f4f7fb`; track `#eef0f3`; danger `#b23636` (border `#e3c2c2`); highlight `#fff3bf`.

Label colors (Okabe-Ito, used for badges + all mix/label bars): AI `#d55e00`, AI-Assisted `#e69f00`, Mixed `#56b4e9`, Human `#009e73`, unscored `#c7ccd1`.

Score bands (by `fraction_ai`) for percent pills / Avg-AI values — text on tint: Human < 0.3 `#00734f` on `#dff1ea`; Mixed 0.3–0.5 `#2f7fae` on `#e4f2fb`; AI-Assisted 0.5–0.8 `#9c6c00` on `#f9efda`; AI ≥ 0.8 `#b34f00` on `#fae4d6`; null `#c7ccd1` on `#f0f1f3`.

Type: system sans stack (`-apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif`), base 13px; data/mono in `ui-monospace, Menlo, Consolas, monospace`; `tabular-nums` globally. Links `#2f6feb`, hover `#1f52bf` underline. Radii: cards 6px, tiles/inputs 3–4px. Scrollbars: 10px, thumb `#d3d8de` radius 6 with 2px page-color border.

Settings (prototype exposes as props — implement as user prefs or constants): `density` compact|comfortable (row padding 2px vs 6px vertical), `anonymousDefault`.

## Assets
None — no images or icon fonts. Glyphs are text: ⇄ ▲ ▼ ↑ ↓ × ✕.

## Files
- `Mail AI Dashboard.dc.html` — the full prototype (markup + interaction logic; mock data in `gen()`/`bodyFor()` is throwaway).
