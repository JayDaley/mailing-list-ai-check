# Auto-generated mail on IETF lists — survey findings and exclusion rules

Findings from a survey of the IETF IMAP archive run on 2026-08-12, in
preparation for scoring all public-list mail received since 2025-07-01. The
goal was to identify which lists and which per-message patterns carry
machine-generated text, so that only human-authored mail is sent to the
detector. The rules derived here are implemented in
`src/mailing_list_ai_check/autogen.py` (from app version 1.5.0) and applied at
fetch time; flagged messages are stored but excluded from extraction and
scoring.

## Method

- Enumerated all 1,376 list folders on the archive server.
- Counted messages with `UID SEARCH SINCE 01-Jul-2025` per folder: 400 lists
  had traffic, totalling 142,826 messages.
- Fetched headers only (`BODY.PEEK[HEADER.FIELDS (...)]`) for a stratified
  sample: up to 35 messages per active list, evenly spaced across each list's
  UID range — 9,772 messages over all 400 active lists. No bodies were
  transferred. Fields sampled: From, Sender, Reply-To, Subject, Date,
  Message-ID, In-Reply-To, Auto-Submitted, Precedence,
  X-Auto-Response-Suppress, User-Agent, X-Mailer, X-GitHub-Reason.
- Classified senders and subject shapes, then verified the residuals by
  fetching seven full headers (ballot positions and datatracker notifications)
  to find a header-level discriminator for IESG ballot mail.

Weighted by real per-list counts, the rules below exclude an estimated 30% of
the period's traffic (~43,000 of 142,826 messages).

## Header signals

- `Auto-Submitted: auto-generated` (RFC 3834) marks 12.8% of the sample. All
  datatracker and secretariat tooling sets it: `internet-drafts@ietf.org`
  (I-D Action), `noreply@ietf.org` (state changes, review assignments, ballot
  positions), `iesg-secretary@ietf.org`, `agenda@ietf.org`,
  `session-request@ietf.org`, `ietf-secretariat@ietf.org`,
  `ietf-ipr@ietf.org`, `statements@ietf.org`, and datatracker-mediated
  liaison and NomCom mail.
- `Precedence: bulk` without `Auto-Submitted` identifies exactly one further
  class: IANA ticket-system mail (`drafts-expert-review-comment@iana.org`,
  `iana-prot-param@iana.org`, and similar `@iana.org` ticket addresses).
- Several automated senders set neither header and need sender rules:
  - `rfc-editor@rfc-editor.org` — RFC announcements and the errata workflow
    (`[Technical/Editorial Errata Reported/Verified/Rejected/Held]`), which
    lands on many WG lists.
  - `noreply@github.com` / `notifications@github.com` — GitHub issue/PR
    mirrors. Some discussion lists are heavily affected (`ocm` ~54% of
    traffic, `ccamp` ~34%).
  - `do_not_reply@mnot.net` — "Weekly github digest" posts, observed on 43
    WG lists.
  - `mailer-daemon@*` — stray DMARC/bounce mail.

## Lists excluded entirely

Lists whose whole traffic is machine-generated or secretariat broadcast, with
message counts since 2025-07-01:

| List | Messages | Content |
|---|---|---|
| `dmarc-report` | 15,336 | DMARC aggregate reports |
| `i-d-announce` | 11,139 | I-D Action announcements |
| `ietf-announce` | 1,258 | Last Calls, actions, RFC announcements |
| `new-wg-docs` | 320 | internet-drafts notifications |
| `ipr-announce` | 224 | IPR disclosure notifications |
| `rfc-dist` | 222 | RFC announcements (no auto headers) |
| `netmod-ver-dt` | 156 | GitHub notification mirror |
| `quic-issues` | 103 | GitHub issue/PR mirror |
| `iesg-agenda-dist` | 49 | Telechat agendas |
| `irtf-announce` | 45 | Announcement-only |
| `NNNall`, `NNNattendees`, `NNN-newparticipants`, `recentattendees` | ~200 | Per-meeting secretariat broadcasts |

`--all-lists` pulls skip these (override with `--include-excluded-lists`);
explicitly named lists are always pulled, and their messages are still
classified per-message.

## Per-message rules

Applied to every fetched message, in order (`autogen.classify_message`):

1. **IESG ballot carve-out** — datatracker mail (`From: noreply@ietf.org`)
   addressed `To: iesg@ietf.org` is kept. Ballot positions ("X's Discuss/No
   Objection on draft-…", 116 in the sample) are delivered by the datatracker
   with `Auto-Submitted: auto-generated`, but the ballot text is written by
   the balloting Area Director; the `To: The IESG` address is unique to them
   among datatracker mail (verified against full headers; scheduling and
   state-change notifications go to the list or the document's aliases).
2. `Auto-Submitted` with any value other than `no` → `auto-submitted`.
3. `Precedence: bulk` (or `junk`/`auto_reply`) → `precedence-bulk`.
4. A known robot sender or any `mailer-daemon@` address → `robot-sender`.
5. A non-reply whose subject contains "New Version Notification for" →
   `nvn-forward`. These are human forwards of the datatracker template
   (~0.6% of the sample); the body is dominated by template text. Replies
   (with `In-Reply-To`) are never matched.

Subject-based matching beyond rule 5 is deliberately avoided: human replies
inherit automated subjects ("Re: [Technical Errata Reported]…", "Re: X's
Discuss on…") and are 60% of the sample's reply traffic to automated threads.
Subjects also carry a leading `[listname] ` tag, so any future subject pattern
must anchor after the tag.

## Decisions recorded

- **IESG ballot positions are kept** (rule 1). Completed directorate reviews
  (Gen-ART, secdir, tsvart, and similar), which the datatracker also delivers
  with `Auto-Submitted: auto-generated` from `noreply@ietf.org`, remain
  excluded: they were not carved out. They can be added later by matching
  their review-completion subject shapes if wanted.
- **`auth48archive` is included** (5,807 messages, 6% automated): AUTH48
  correspondence is human-written editing discussion.
- **Forwarded "New Version Notification" posts are excluded** (rule 5).
- Messages fetched before the classification existed cannot be reclassified
  locally: only a limited field set is stored per message, not the full
  headers. A re-pull into a fresh store classifies them.

## Residual caveats

- The IANA ticket rule (`Precedence: bulk`) also drops ticket replies written
  by humans through the ticket system (expert-review comments); the template
  share dominates, and the two are not separable by header.
- Ballot-position bodies open with a datatracker preamble before the
  human-written DISCUSS/COMMENT sections; extraction quality for these
  messages has not been separately measured.
- The sample caps at 35 messages per list; per-list automation shares for
  high-volume lists carry sampling error of roughly ±8 percentage points at
  95% confidence.
