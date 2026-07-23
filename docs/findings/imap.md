# IMAP spike findings (Phase 0, task 0.1)

Spike proving programmatic access to the archive server used during development
(Isode M-Box), so the Phase 2
fetcher can be designed against verified facts. All work was **read-only**
(`LIST`, `EXAMINE`, `UID SEARCH`, `UID FETCH` with `BODY.PEEK` — never `SELECT`
read-write, never a write/flag/delete command).

Scratch scripts live in `spikes/imap/` (gitignored):
`probe_auth.py`, `explore.py`, `probe_limits.py`, `fetch_snippet.py`.

Server: `imap.<archive-host>:993` (implicit TLS). Greeting identifies it as
**Isode M-Box IMAP4rev1 (19.0v26)**. Advertised capabilities include
`IMAP4REV1 UIDPLUS NAMESPACE ESEARCH LIST-EXTENDED IDLE CONDSTORE QRESYNC
COMPRESS=DEFLATE MOVE WITHIN CONTEXT=SEARCH SEARCHRES SPECIAL-USE` and
`AUTH=PLAIN`.

## 1. Authentication — anonymous access works

Anonymous access is open and required no credentials. The working login:

```python
M = imaplib.IMAP4_SSL("imap.<archive-host>", 993)
M.login("anonymous", "anonymous@example.com")   # any email-style password accepted
```

- Plain `LOGIN anonymous <email-style-string>` succeeds and returns full
  read capabilities (`RIGHTS=kxet` on selected folders — read-oriented ACL).
- SASL `ANONYMOUS` was **not** needed; the first attempt (plain `LOGIN`)
  succeeded, so the fallback paths in `probe_auth.py` were not exercised.
- No credentials exist in this repo and none were created. The Phase 2 config
  should still expose `IMAP_USER`/`IMAP_PASSWORD` so an authenticated
  account can be used if a private list is ever needed, but for
  every public list archive, anonymous is sufficient.

## 2. Folder naming scheme

- One flat namespace, hierarchy separator `/`, single top-level `\Noselect`
  node `Shared Folders`. Every mailing list is a direct child:
  **`Shared Folders/<listname>`**.
- `M.list()` returned **1375 entries**: 1 `\Noselect` root + **1374
  selectable list folders**. All 1374 share the `Shared Folders/` prefix.
- Enumerate with `M.list()` and keep entries whose flags lack `\Noselect`;
  strip the surrounding quotes from the mailbox name. Names contain characters
  needing quoting on `SELECT`/`EXAMINE`, so always wrap the folder in `"..."`.
- Target folders confirmed:
  - `tls`       → `Shared Folders/tls`
  - `last-call` → `Shared Folders/last-call`
- Names are the raw list slugs (e.g. `quic`, `100-newcomers`, `106all`). The
  `<meeting-number>all/attendees/companions/newcomers` folders inflate the
  count; real WG/area lists are a subset.

## 3. Server-side UID SEARCH — supported

All searches ran server-side and returned UID sets fast. Verified on
`Shared Folders/last-call` (16,950 messages):

| Search | Result |
|---|---|
| `UID SEARCH SINCE 01-Jan-2025` | OK, 4242 hits |
| `UID SEARCH SINCE 01-Jun-2025` | OK, 2854 hits |
| `UID SEARCH SENTSINCE 01-Jan-2025` | OK, 4242 hits |
| `UID SEARCH FROM "iesg"` | OK, 3 hits |
| `UID SEARCH SINCE 01-Jan-2025 FROM "<archive-domain>"` | OK, 1097 hits (combination AND-ed) |

`FROM` filtering (RFC 3501 substring match against the `From:` header,
case-insensitive) is effective and matches domain, address, or display name:

| `FROM` term | hits on last-call |
|---|---|
| `huawei.com` | 338 |
| `noreply@<archive-domain>` | 4093 |
| `Italo Busi` | 37 |

**Conclusion:** date filtering (`SINCE`) and sender filtering (`FROM`) can both
be done server-side, and combined in one `UID SEARCH`. The fetcher should push
`--since`/`--from` down to the server rather than filtering client-side. Note
`FROM` is a substring match, so a bare domain also matches display names that
happen to contain it — sufficient for narrowing, but confirm the parsed address
client-side if exactness matters.

## 4. UIDVALIDITY

Stable per-folder values:

| Folder | UIDVALIDITY | EXISTS | UIDNEXT |
|---|---|---|---|
| `Shared Folders/<big-list>` | 1455297825 | 146312 | 146390 |
| `Shared Folders/last-call` | 1571671002 | 16950 | 16999 |
| `Shared Folders/quic` | 1462706285 | 12091 | 12145 |

`UIDVALIDITY` reads as a fixed epoch-like value (folder creation time). `UIDNEXT`
sits slightly above `EXISTS` (e.g. `<big-list>` 146390 vs 146312) — expected: some UIDs
were expunged over the folder's life, so UIDs are sparse. The fetcher must not
assume UIDs are contiguous or that `count == max UID`.

## 5. Message counts (EXISTS)

- `Shared Folders/<big-list>` — **146,312** (large; the main list)
- `Shared Folders/last-call` — **16,950**
- `Shared Folders/quic` — **12,091**

## 6. Fetch throughput

Batched `UID FETCH <comma-list> (BODY.PEEK[])` in a single round-trip, timed:

| Folder | Batch | Time | Rate | Volume |
|---|---|---|---|---|
| last-call | 50 bodies | 0.52 s | ~96 msg/s | 884 KiB |
| last-call | 50 headers only | 0.23 s | ~217 msg/s | — |
| `<big-list>` | 50 bodies | 0.39 s | ~127 msg/s | 1.0 MiB |
| `<big-list>` | 200 bodies | 0.75 s | ~268 msg/s | 3.1 MiB |

Throughput is high and improves with larger batches (per-round-trip overhead
amortizes). No rate limiting, throttling, or connection drops observed.
**Connection limit test:** opened **12 simultaneous** anonymous logged-in
connections with no rejection (test stopped at 12; the real ceiling is higher).

Implications: batch UIDs into one `UID FETCH` (a few hundred per command);
`BODY.PEEK[]` (not `BODY[]`) to avoid setting `\Seen` and keep the session
strictly read-only. Modest parallelism is available if ever needed, but a
single connection already saturates throughput for the volumes involved.

## 7. Message format notes (for the Phase 3 parser)

Content-Type distribution from a 20-message sample of `last-call`:

| Count | Content-Type |
|---|---|
| 10 | `text/plain` |
| 8 | `multipart/alternative` (text/plain + text/html) |
| 1 | `multipart/signed` (text/plain + application/pgp-signature) |
| 1 | `multipart/mixed` (text/plain + text/html) |

- Every sampled message had a usable `text/plain` part — the parser should
  prefer `text/plain` and only fall back to stripping HTML when a message is
  HTML-only (none seen in this sample, but expect some on other lists).
- Charsets seen: `utf-8` on `text/plain` singles; multiparts declared charset
  at the part level (top-level `get_content_charset()` was `None`) — decode per
  MIME part, not from the top-level header.
- Headers of interest are present and clean: `From`, `Subject`, `Date`,
  `Message-ID`, `In-Reply-To`. RFC 2047-encoded words appear in `From`/`Subject`
  (e.g. `Ionuț Mihalcea`) — decode with `email.header.decode_header`.
- Automated list notices come from a bot-style sender
  (`<Name> via <Notifier> <noreply@...>`) and carry no `In-Reply-To`
  (thread roots); human replies carry `In-Reply-To` and real addresses.

## 8. Things the Phase 2 fetcher must handle

- **Long server lines.** Set `imaplib._MAXLINE` high (10 MB in the spike);
  the 10 KB default overflows on large `LIST`/`FETCH` responses.
- **Quote folder names** in `EXAMINE`/`SELECT` (contain `/` and hyphens).
- **Read-only discipline:** `select(..., readonly=True)` (EXAMINE) and
  `BODY.PEEK[...]`, never `BODY[...]` or `SELECT` read-write.
- **Sparse, non-contiguous UIDs** — drive incremental pulls off actual UID sets
  from `UID SEARCH`, never off EXISTS counts.
- **Large folders** (`<big-list>` ≈ 146k) — always constrain with `SINCE`/UID ranges;
  never fetch a whole folder blindly.
- **~1374 list folders** — an `--all-lists` run touches many mailboxes; enumerate
  once via `LIST` and let the user select.
- Multiple simultaneous connections are tolerated (≥12), but one connection is
  enough; reuse it across folders (re-`EXAMINE` per folder).

## Decision inputs (for Phase 2 design and PLAN Decisions)

- **Auth mode:** **Anonymous** — `LOGIN anonymous <any-email-string>` over
  `imap.<archive-host>:993` TLS. No secret needed for public list archives. Keep
  `IMAP_USER`/`IMAP_PASSWORD` config optional for future private-list access.
- **Sender filtering:** **Server-side.** `UID SEARCH FROM "<addr-or-domain>"`
  works and combines with `SINCE`. Push `--from` to the server (substring match;
  optionally re-verify the parsed address client-side for exactness).
- **Date filtering:** **Server-side** via `UID SEARCH SINCE <DD-Mon-YYYY>`
  (or `SENTSINCE` — identical results here).
- **Incremental-pull mechanism:** store **(UIDVALIDITY, last_uid)** per folder
  (the `pull_state` table). On each pull: `EXAMINE` the folder, read
  `UIDVALIDITY`; if unchanged, fetch new mail with
  `UID SEARCH UID <last_uid+1>:*` (or `UID FETCH <last_uid+1>:*`) and advance
  `last_uid` to the max UID seen; if `UIDVALIDITY` changed, treat the folder as
  reset and do a full resync (re-search by date), then rewrite the cursor.
- **Fetch strategy:** single connection, batched
  `UID FETCH <ids> (BODY.PEEK[])`, a few hundred UIDs per command
  (~100–270 msg/s observed, no throttling).

## Minimal working fetch snippet

Runnable copy: `spikes/imap/fetch_snippet.py` (verified working against the live
server). Connect → EXAMINE (read-only) → server-side search → batched fetch →
parse headers:

```python
import email
import imaplib
import socket
from email.header import decode_header, make_header

HOST, PORT = "imap.<archive-host>", 993
socket.setdefaulttimeout(60)
imaplib._MAXLINE = 10_000_000  # archive folders can return very long lines


def decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def main() -> None:
    M = imaplib.IMAP4_SSL(HOST, PORT)
    M.login("anonymous", "anonymous@example.com")  # anonymous: any email-ish pw

    folder = "Shared Folders/last-call"
    M.select(f'"{folder}"', readonly=True)          # EXAMINE — read-only
    uidvalidity = M.response("UIDVALIDITY")[1][0].decode()

    # Server-side filter: since a date AND from a given sender domain.
    typ, data = M.uid("SEARCH", None, "SINCE", "01-Jan-2026", "FROM", '"example.org"')
    uids = data[0].split()
    print(f"folder={folder} uidvalidity={uidvalidity} matched={len(uids)}")

    batch = b",".join(uids[-20:])                    # one round-trip
    typ, data = M.uid("FETCH", batch, "(BODY.PEEK[])")
    for item in data:
        if not isinstance(item, tuple):
            continue
        msg = email.message_from_bytes(item[1])
        print(decode(msg.get("From")), "|", decode(msg.get("Subject")))

    M.close()
    M.logout()


if __name__ == "__main__":
    main()
```
