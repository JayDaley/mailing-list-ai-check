# Extraction test fixtures

Hand-labeled corpus for the Phase 3 new-text extraction quality tests
(`PLAN.md` task 3.3). Each fixture is a raw RFC 5322 message from the **public**
IETF mail archive (<https://mailarchive.ietf.org>), promoted from the Phase 0
extraction spike (`docs/findings/extraction.md`). Including these verbatim is
fine — they are public-archive list mail (established in `PLAN.md`).

## Layout

- `*.eml` — the raw message, exactly as downloaded from the archive's public
  per-message `/download/` endpoint. Descriptive kebab-case names, prefixed by
  structural category.
- `expected/<same-basename>.txt` — the hand-labeled ground truth: **exactly the
  text that is sent to Pangram**, i.e. the message body with quoted text,
  attribution lines ("On … wrote:"), and signatures/greetings/sign-offs removed,
  and **all** substantive new text kept — including text that appears *between*
  or *below* quoted blocks.

  Since extraction was split from cleaning, these files represent the
  **composite of both stages** — `clean_for_scoring(extract_new_text(body).text)`
  — not stage 1 alone. Stage 1 (`extraction.py`) keeps the author's greetings,
  sign-offs and signature furniture as novel content; stage 2 (`cleaning.py`)
  strips that furniture for scoring. The expected files did not change in the
  split: they are, and have always been, the final scored text. The corpus test
  therefore compares `clean_for_scoring(extract_new_text(body).text).text`
  against these files (see `tests/test_extraction.py`).

## How the expected files were derived

Each message's `text/plain` part was decoded (quoted-printable / base64 / BOM /
CRLF resolved; for `multipart/*` the `text/plain` alternative was used) and
read by hand. From that decoded body:

- **Removed:** lines beginning with `>` (any indentation); attribution lines
  ("On <date>, X wrote:", "Name <a@b> wrote:", and non-English equivalents);
  signature blocks (`-- ` delimited, `~ Name` sign-off lines, contact/PGP
  blocks, confidentiality notices); and mailing-list footers.
- **Removed (scoring policy, 2026-07-22):** one opening greeting line
  ("Hi X, all,") and one closing salutation(+name) sign-off ("Best, / Songbo",
  "Cheers, Peter", or a bare trailing "Cheers"). These are author-typed but
  formulaic framing, and a scored ablation showed they materially mask
  AI-generated content in the Pangram check.
- **Kept:** all other prose the author typed, including interleaved
  point-by-point replies, author elision markers the author inserted into a
  quote (`[...]`, `(..)`), greetings/valedictions appearing mid-text, and bare
  trailing name sign-offs with no salutation ("Dino", "—Daniel", "Nick") —
  those are not distinguishable from content with enough confidence.
- **Whitespace normalization** applied to the expected text: trailing
  whitespace stripped per line; runs of blank lines collapsed to a single blank
  line; leading/trailing blank lines removed. Non-ASCII characters the author
  wrote (curly quotes, em dashes, `§`, `ā`, `ß`, emoji) are preserved verbatim.
  Tests should compare with a tolerant whitespace normalization rather than
  byte-exact equality, except where a quirk note says the output is pinned.

The chosen Phase 3 strategy is **email-reply-parser (primary) + a small custom
quote-stripping / normalization pass** (Talon dropped — see the findings doc).
The "known quirks" column below records where **raw email-reply-parser** (no
custom pass) diverges from the ground truth; those are exactly the cases the
custom pass and over-strip guard must fix, so they make the loudest regression
guards.

## Fixtures

| Fixture (`.eml`) | Category | Author / provenance | Archive URL | Known quirks (raw email-reply-parser) |
|---|---|---|---|---|
| `interleaved-pointbypoint-lastcall-01` | interleaved | Dino Farinacci (last-call) | https://mailarchive.ietf.org/arch/msg/last-call/E22oZyg-pUNhqi6lF32vpCz0st4 | **Gold case** — 6-way point-by-point reply. ERP correct (all 6 replies + "Dino" kept, zero leaked quotes). The single most important regression guard. |
| `interleaved-greeting-quic-01` | interleaved | Lucas Pardue (quic) | https://mailarchive.ietf.org/arch/msg/quic/Mwo6ZbD6sOL8cUhlWOXJ5Jbpw7c | Greeting + one comment between deep-nested quotes. ERP correct. |
| `interleaved-elision-lastcall-01` | interleaved | Peter Thomassen (last-call) | https://mailarchive.ietf.org/arch/msg/last-call/ku5TSOfHj-95ntPHqjeiKBADRrc | Author elision marker `[...]` between two quote blocks **must be kept**. `format=flowed`. ERP correct. |
| `interleaved-bom-tls-01` | interleaved | Uri Blumenthal (tls) | https://mailarchive.ietf.org/arch/msg/tls/yUDZ03qD8njrMQtD0oR5nUYDcUM | **Leading BOM** (`﻿`) — pins the normalization step (one of the 2/12 ERP interleaved misses without it). `multipart/signed`. |
| `interleaved-indented-quotes-lastcall-01` | interleaved | Michael Richardson (last-call) | https://mailarchive.ietf.org/arch/msg/last-call/QqHZZ04_1yZyE7JRVG97jEmAdCg | **Indented `> ` quotes** (`    > …`) — ERP leaks 6 quote lines; pins the custom indented-quote pass. |
| `interleaved-deep-thread-tls-01` | interleaved | Markku-Juhani O. Saarinen (tls) | https://mailarchive.ietf.org/arch/msg/tls/ukx10TSS3MfyprVOMlrJZkCqBUA | Large (345-line) deep thread — pins no-truncation across many interleaved paragraphs. Includes author elision `(..)`. ERP keeps the trailing `Dr. … <mjos@iki.fi>` contact-sig line (removed in ground truth). |
| `toppost-ps-after-signature-tls-01` | top-post | Christian Veenman (tls) | https://mailarchive.ietf.org/arch/msg/tls/2-0cajV_2mXbTf0HDC1tzmToTc8 | Author text (a `Ps.` block) appears **after** a `~ Christian Veenman (NCSC-NL)` signature line — the Ps. must be kept, the `~` line dropped. ERP keeps the `~` sig line. `multipart/alternative`. |
| `toppost-flowed-ietf-01` | top-post | Brian E Carpenter (ietf) | https://mailarchive.ietf.org/arch/msg/ietf/4TiqV2i5KR1nxVFc01a2i0qGCGI | `format=flowed`; new text above/between two short quote blocks. Macron `ā` in "Ngā mihi" sign-off. ERP retains some elided/quoted fragments — custom quote pass needed. |
| `toppost-plain-part-only-tls-01` | top-post | John Mattsson (tls) | https://mailarchive.ietf.org/arch/msg/tls/ru2n3qOjIEsyHMyjuQh1ou0SJlE | Clean reply whose `text/plain` alternative contains **only** the new text (quotes live in the HTML part). `ß` in "Preuß". ERP correct. |
| `toppost-multipart-alt-tls-01` | top-post | Daniel Apon (tls) | https://mailarchive.ietf.org/arch/msg/tls/AnBYnw-nagHdhvtQWwTzWE37fMs | `multipart/alternative`; em-dash sign-off "—Daniel". ERP correct. |
| `toppost-signoff-unquoted-wimse-01` | top-post | Songbo Bu (wimse) | https://mailarchive.ietf.org/arch/msg/wimse/XZe8sN-M03tqqN0J359RjX3gOcE | **Gmail-style fully top-posted reply** (typical AI-generated shape): new text ends at a "Best, / Songbo" sign-off, then a *wrapped* attribution ("On <date>, … \n wrote:") introduces ~1780 lines of quoted thread with **no `>` markers**. ERP keeps the attribution and much of the quote; before the sign-off-boundary rule the pipeline shipped the whole quoted thread. Pins the sign-off boundary in both the custom pass and the guard denominator. |
| `toppost-originalmsg-divider-agent2agent-01` | top-post | Chris (agent2agent) | https://mailarchive.ietf.org/arch/msg/agent2agent/V_6sOOpe-2E_OLrQAAJNkcvI_ko | **Dashed "Original message" divider, flattened HTML**: the whole body is 2 giant lines — line 1 is the author's new text, line 2 is the entire quoted thread glued behind `-------- Original message --------` (HTML-to-text lost the line breaks, so the quote-header block detector cannot fire). Pins the divider rule, including the mid-line/glued form where the author's prefix before the divider is kept. |
| `toppost-outlook-quoteheader-lastcall-01` | top-post | Nate Karstens (last-call) — **synthetic** | — (Phase 7.2 live-E2E defect; Message-ID `<BY5PR04MB69141…@…outlook.com>`, faithful reproduction — the 2026-dated message is not in the live archive) | **Outlook top-post**: 4 author lines, then an Outlook quote-header block (`From:`/`Sent:`/`To:`/`Cc:`/`Subject:`) introducing the quoted Tsvart review with **no `>` markers**. ERP is correct (author lines only), but the over-strip guard previously counted the un-prefixed quoted review as authored content, misfired, and shipped the whole 1.7 KB review via `custom-fallback`. Pins quote-header-block detection in both the custom pass and the guard denominator. `multipart/alternative`. |
| `bottompost-clean-quic-01` | bottom-post | Olivier Bonaventure (quic) | https://mailarchive.ietf.org/arch/msg/quic/DRgTBE4FMhivJpOQ5B61TmpQuQw | Clean bottom-post: greeting + digest quote + reply + "Olivier" sign-off. ERP correct. |
| `bottompost-confidentiality-sig-lastcall-01` | bottom-post | Brian Campbell (last-call) | https://mailarchive.ietf.org/arch/msg/last-call/RLYSMc1S5Ct6cMnUR1nP3FypTro | One-line reply below quote, followed by a `-- ` confidentiality-notice signature that must be stripped. `multipart/alternative`. ERP correct (strips the notice). |
| `bottompost-multipart-alt-ietf-01` | bottom-post | Nicolas Giard (ietf) | https://mailarchive.ietf.org/arch/msg/ietf/7TjZy8W_EFYr8QbmQsoHefsSbNw | `multipart/alternative` bottom-post; "Nick" sign-off. ERP correct. |
| `signature-corporate-contact-wimse-01` | signature-heavy | Thi Nguyen-Huu (wimse) | https://mailarchive.ietf.org/arch/msg/wimse/d0Ua5jt7Ekkya9AiwUtTi99o1kY | **Corporate contact block** with no `-- ` delimiter: below a "Cheers" sign-off, a "Name \| Title" line then phone / piped address+URL lines ("Tel: +1 …", "WinMagic Corp. \| 11-80 Galaxy Blvd."), then an Outlook quote-header block. The contact lines must be dropped (they are not authored prose) while "Thi Nguyen-Huu \| CEO" and everything above is kept. Pins the corporate-contact line classifier; the cue rules deliberately spare digest table rows ("count \| bytes \| Name <a@b>"). |
| `signature-identifier-links-wimse-01` | signature-heavy | Kunal Ghosh (wimse) | https://mailarchive.ietf.org/arch/msg/wimse/cJ5Zg2I9p5i2f9cRw4TCcneBT1g | **Personal-identifier signature** with no `-- ` delimiter: a long review ends "Regards, / Kunal Ghosh" then an `ORCID: 0009-…` line and a bare personal domain (`kghoshworkid.github.io`). The identifier line is dropped anywhere; the link-only line is dropped because it (plus other debris) makes up the entire tail after the sign-off. Prose mentioning "GitHub issues" and URLs pasted mid-prose must be kept. Pins the identifier-line classifier and the trailing-sign-off-debris rule. |
| `signature-pgp-german-attribution-tls-01` | signature-heavy | Erwin Hoffmann (tls) | https://mailarchive.ietf.org/arch/msg/tls/rBFeM0LLgqlJdpHoNTaPV_FqfM0 | Top new text + `-- ` PGP/contact signature at the very bottom. **German attribution line** ("Am … schrieb Dennis Jackson:") — ERP leaks it because it only detects English "wrote:"; pins a non-English attribution rule. |
| `noquote-prose-signature-ietf-01` | no-quote | Brian Campbell (ietf) | https://mailarchive.ietf.org/arch/msg/ietf/Zg5hbqTSpMQaCuKN6U8Ykkvrc_c | Thread-starter prose (incl. a bracketed meta note that is kept) ending in a `-- ` confidentiality signature. `multipart/alternative`. ERP correct. |
| `threadstarter-announcement-quic-01` | thread-starter | Louis Navarre (quic) | https://mailarchive.ietf.org/arch/msg/quic/FiX3_mLcfntBGQ846XHPmCDhVF8 | Human announcement, no quotes; `us-ascii`. Entire body is new text incl. sign-off. ERP correct. |
| `threadstarter-rfc2047-header-ietf-01` | thread-starter / RFC 2047 | Timo Gerke (ietf) | https://mailarchive.ietf.org/arch/msg/ietf/aKAi_RHUTjiAFXPtaS55DYQgDfw | **RFC 2047-encoded Subject** (multi-word `=?UTF-8?Q?…?=` encoding `§`). `multipart/signed`. Body pastes email headers as "evidence" — ERP truncates that block (treats pasted headers as a boundary); ground truth keeps it. |
| `digest-dashsep-nomcom-ietf-01` | digest | NomCom stats bot / John Levine (ietf) | https://mailarchive.ietf.org/arch/msg/ietf/6JNhAvFgxOH_qQ30g9kPY7jYAkw | **Dashed-separator digest** (`----+----`). ERP truncates to 32 chars at the dashed line; pins the over-strip guard (full table must be kept). |
| `digest-github-multipart-quic-01` | digest | Repository Activity Summary Bot (quic) | https://mailarchive.ietf.org/arch/msg/quic/2HPjO1BgwPdLtiUDhw88yWbNEHQ | GitHub weekly digest, `multipart/alternative`, several `------` section rules + trailing `-- ` footer. ERP truncates at the first `------`; pins the over-strip guard. Contains emoji. |

## Category counts

| Category | Count | Fixtures |
|---|---:|---|
| interleaved | 6 | pointbypoint, greeting, elision, bom, indented-quotes, deep-thread |
| top-post | 7 | ps-after-signature, flowed, plain-part-only, multipart-alt, outlook-quoteheader, signoff-unquoted, originalmsg-divider |
| bottom-post | 3 | clean, confidentiality-sig, multipart-alt |
| signature-heavy | 3 | pgp-german-attribution, corporate-contact, identifier-links |
| no-quote / thread-starter | 3 | noquote-prose-signature, threadstarter-announcement, threadstarter-rfc2047-header |
| digest (dashed-separator) | 2 | dashsep-nomcom, github-multipart |
| **Total** | **24** | (11 promoted from the spike + 8 added for category coverage + 1 Phase 7.2 defect + 4 extraction/cleaning defects reported from live runs) |

Cross-cutting coverage: `multipart/alternative` (8 fixtures), `multipart/signed`
(3), `format=flowed` (2), RFC 2047-encoded headers (1 dedicated + several with
encoded `[List]` Subject prefixes), leading BOM (1), non-English attribution (1),
non-ASCII body content (`§ ā ß “” — 💬`, several).
