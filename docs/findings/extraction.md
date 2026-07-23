# Phase 0.2 — Extraction-libraries spike

Head-to-head comparison of **Talon** (`talon` 1.4.4) and **email-reply-parser**
(`email-reply-parser` 0.5.12) on real mailing-list mail, to pick the Phase 3
new-text extraction strategy.

Spike code, venv, and corpus live in `spikes/extraction/` (gitignored). All
messages are from the development reference archive's public web archive; no
secrets are involved.

## TL;DR

- Both libraries install and run on Python 3.13. Talon needs a documented
  workaround (its `cchardet` dep does not build on Python ≥ 3.12; its ML
  signature model is unusable on modern scikit-learn — see below).
- On the characteristic **interleaved bottom-posts, Talon fails**: it returns
  the entire message (quotes and all) on 9 of 12 interleaved messages and leaks
  quoted text on all 12. **email-reply-parser handles interleaved replies
  correctly** (all author text kept, quotes stripped) on 10 of 12, with the
  other 2 being a whitespace/BOM edge case that also defeats Talon.
- email-reply-parser additionally strips signatures; `talon.quotations` does
  not, and Talon's ML signature module is broken on this Python.
- **Recommendation: email-reply-parser as the primary extractor, plus a small
  custom quote-stripping / normalization pass.** Talon is not used.

---

## 1. Install verdicts (Python 3.11 compatibility)

Environment note: this machine has **no Python 3.11 or 3.12** available — only
3.13.14 and 3.14.6 (Homebrew). The venv was built on **Python 3.13.14**
(`spikes/extraction/.venv`), the closest available to the ≥ 3.11 target. The
compatibility problems found are structural (removed CPython headers, removed
sklearn modules) and would behave identically on 3.12; 3.11 is the last version
where the naïve `pip install talon` might work off prebuilt wheels, but that is
untested here.

### email-reply-parser — clean

```
pip install email-reply-parser   # → email-reply-parser 0.5.12
```

Pure Python, zero dependencies. Installs without issues.

### Talon — installable only with workarounds

`pip install talon` **fails** on Python 3.13. Two independent problems:

1. **`cchardet` (hard dependency) does not compile.** Its C extension includes
   `longintrepr.h`, a CPython header made private/removed in Python 3.12:
   `fatal error: 'longintrepr.h' file not found`. `cchardet` is unmaintained.
   - *Workaround:* install `faust-cchardet` (a maintained fork that ships
     prebuilt wheels and provides the same `import cchardet` module), then
     install Talon with `--no-deps` and supply the remaining deps explicitly.

2. **Talon's ML signature classifier is unusable on modern scikit-learn.**
   `talon.signature` does `from sklearn.externals import joblib` (removed in
   scikit-learn ≥ 0.23). Even after shimming that import, the *pickled* model
   ships references to `sklearn.svm.classes` (a module path removed in
   scikit-learn ≥ 0.22), so `talon.init()` throws
   `ModuleNotFoundError: No module named 'sklearn.svm.classes'`. The pickle
   cannot be loaded against any current scikit-learn. This is effectively
   unfixable without an old scikit-learn (≈ 0.19) pinned into the environment.

**What actually works on 3.13:** the core quotation stripper, which needs
neither `init()` nor the ML stack:

```python
from talon import quotations
quotations.extract_from_plain(body)          # OK — no init(), no ML
from talon.signature.bruteforce import extract_signature  # OK — regex, no ML
```

Working install recipe used for this spike:

```
pip install faust-cchardet
pip install --no-deps talon
pip install lxml regex numpy scipy scikit-learn chardet cssselect six html5lib
```

Resulting versions: `talon 1.4.4`, `faust-cchardet 3.0.0`, `lxml 6.1.1`,
`regex 2026.7.19`, `scikit-learn 1.9.0`, `scipy 1.18.0`, `numpy 2.5.1`,
`chardet 7.4.3`, `cssselect 1.4.0`, `html5lib 1.1`, `six 1.17.0`.

> Note: `numpy`/`scipy`/`scikit-learn` are only pulled in by Talon's (broken)
> ML signature path. If Phase 3 uses `talon.quotations` only, they are
> unused — Talon's quotation stripper depends only on `lxml`, `regex`,
> `cssselect`, `html5lib`, and a `cchardet` module.

**Verdict:** email-reply-parser is fully compatible. Talon is compatible
*only* for `talon.quotations` (+ regex `bruteforce` signature) and *only* after
swapping in `faust-cchardet` and installing with `--no-deps`. Its ML signature
extraction is unusable on Python ≥ 3.12.

---

## 2. Corpus

88 raw RFC 5322 messages downloaded from four active lists (`httpbis`, `last-call`,
`tls`, `quic`) via the archive's public per-message `/download/` endpoint
(`spikes/extraction/harvest.py`; raw messages in `spikes/extraction/corpus/`).
The archive's mbox/bulk export requires login, but individual message download
is public.

All 88 messages had a `text/plain` part — **no HTML-only messages were
encountered** in this sample, so HTML-only handling remains untested here and
should be handled defensively in Phase 3 regardless.

Structural mix (auto-classified by `compare.py categorize`, spot-checked by
hand):

| Category | Count | Description |
|---|---:|---|
| no-quote | 45 | thread starters, announcements, bot digests |
| top-posted | 22 | new text above one trailing quote block |
| bottom-posted | 9 | greeting + one quote block + reply/sig below |
| interleaved | 12 | **the critical case** — new text between multiple quoted blocks |

---

## 3. Grading

Each message's output was scored on two reliable, objective signals plus
hand review:

- **leaked quote lines** — lines still beginning with `>` in the output
  (UNDER-STRIPPED signal).
- **no-op** — Talon returned the input essentially unchanged (total strip
  failure).
- **author-text retention** — hand-verified on the interleaved/bottom cases and
  a sample of the rest. (An automated retention proxy exists in `grade.py` but
  *understates* email-reply-parser because it counts correctly-stripped
  signatures/footers as "lost" text — see §4.)

Grades: **KEEP-ALL** (all author text kept, quotes + signatures removed),
**TRUNCATED** (author text lost), **UNDER-STRIPPED** (quoted text leaked),
**FAILED** (exception/empty). No message produced an exception in either library.

### Per-category results

| Category (n) | Talon | email-reply-parser |
|---|---|---|
| **interleaved (12)** | **UNDER-STRIPPED 12/12.** Returns whole message unchanged on 9/12; avg **86 leaked quote lines** per message. Author text present but buried in quotes. | **KEEP-ALL 10/12.** Zero leaked quotes, all interleaved author text kept. 2/12 UNDER-STRIPPED by a *few* lines from a shared edge case (indented `>`, leading BOM). |
| **bottom-posted (9)** | KEEP-ALL 7/9 (quotes); 2 leak 1–2 indented-quote lines. **Does not strip signatures** (leaves `-- ` blocks / confidentiality notices). | **KEEP-ALL 9/9.** Zero leaked quotes; also strips signatures. Strictly ≥ Talon here. |
| **top-posted (22)** | KEEP-ALL 22/22 (quotes). Signatures not stripped. | KEEP-ALL 22/22. Signatures stripped. Tie on quotes, ERP better on sigs. |
| **no-quote (45)** | Keeps whole body (correct); leaves signatures/footers in. | Keeps human prose + strips signatures (correct). **TRUNCATED on ~4** bot-generated digest/table mails with `----` separator lines (see §4). |

**Summary:** on the interleaved case — the most important category —
**email-reply-parser is correct 10/12 (83%) vs Talon 0/12 (0%).** On top/bottom
posts the two tie on quote removal, with email-reply-parser additionally
removing signatures.

---

## 4. Illustrative examples (interleaved behavior)

### Example A — `quic__Mwo6Zb…` : greeting + one interleaved comment

Original (abridged):

```
Hi

On Tue, Jul 7, 2026, at 03:25, Dan Wing wrote:
> ...I would lean heavily towards your solution (2)...
> > On Jul 6, 2026, Stefano Duo wrote:
> > > Has this scenario been discussed before?...
You might also be interested in Marco and Marten's draft https://…/
> > Thank you,
> > Stefano
```

- **Talon** → returns the **entire message unchanged** (every `>` line leaked).
- **email-reply-parser** → exactly:
  ```
  Hi
  You might also be interested in Marco and Marten's draft https://…/
  ```
  The greeting and the interleaved comment are kept; all quotes are removed.

### Example B — `last-call__E22oZy…` : 6-way interleaved point-by-point reply

Original has six quoted questions, each answered inline, then a `Dino` sign-off.

- **Talon** → whole message unchanged, 59 leaked quote lines.
- **email-reply-parser** → all six replies + sign-off, zero quotes:
  ```
  Some quick replies.
  Thanks.
  These would be different domains that don't connect to each other...
  If there are no group boundaries...
  Right, a tradeoff between plug-and-play and secure communication.
  I thought we had something like that. I'll leave this for Mike and Stig...

  Dino
  ```

### Example C — `last-call__RLYSMc…` : bottom-post with a confidentiality signature

Original: attribution + one quote block + a one-line reply + a `-- `
confidentiality-notice signature.

- **Talon** → keeps the reply **but also keeps the entire `-- ` confidentiality
  notice** (no signature stripping).
- **email-reply-parser** → keeps only the reply, strips the signature.

### The one place email-reply-parser truncates — `general__6JNhAv…` (bot digest)

A machine-generated stats table whose second line is `----+----+----`.
email-reply-parser reads that dashed line as a signature boundary and drops
everything after it. Talon keeps the whole table. This failure only occurs on
**dashed-separator machine mail** (NomCom stats, GitHub/qlog digests, session
schedulers) — never on human prose in the corpus — and such messages are not
AI-scoring targets. A guard is still required (§6).

### Shared weakness — `last-call__QqHZZ0…` (indented quotes)

Quotes are indented (`    > text`, `>` not in column 0). **Both** libraries fail
to strip them (6 leaked lines each). Same root cause as the leading-BOM case in
`tls__yUDZ0…`. A small custom pass fixes both (§6).

---

## 5. Recommendation

**Use email-reply-parser as the primary extractor, complemented by a small
custom quote-stripping + normalization pass. Do not use Talon.**

Rationale:

- **Interleaved replies are the most important category, and Talon scores 0/12
  there** — it returns quotes-and-all whenever new text is interspersed with
  quoted blocks, because its algorithm looks for one contiguous quoted tail.
  email-reply-parser's fragment model scores 10/12.
- On top/bottom posts the two **tie** on quote removal, so Talon adds nothing
  email-reply-parser doesn't already do — and email-reply-parser also removes
  signatures, which `talon.quotations` does not and Talon's ML signature module
  *cannot* on this Python.
- Talon also carries cost: a fragile install (`faust-cchardet` +
  `--no-deps`) and a large, otherwise-unused numpy/scipy/scikit-learn stack.
- email-reply-parser's only failure (dashed-line bot digests) is on
  non-human content and is guarded with a simple check.

A "both merged" or "Talon fallback" design was considered and rejected: the only
place Talon out-performs email-reply-parser is the machine-generated digests
that are not scoring targets, and Talon fails the most important case.

### Sketch of the Phase 3 pipeline

```
def extract_new_text(body: str) -> tuple[str, str]:   # (text, method)
    # 1. Normalize — this alone fixes 2 of the 12 interleaved misses.
    body = body.lstrip("﻿")              # strip leading BOM
    body = body.replace("\r\n", "\n")

    # 2. Primary: email-reply-parser visible (non-quoted, non-sig) fragments.
    text = EmailReplyParser.parse_reply(body)
    method = "erp"

    # 3. Complementary regex cleanup on the result (catches ERP's residual
    #    leaks: indented quotes and attribution variants it doesn't match).
    text = drop_lines(text, matching=[
        r"^\s*>+",                             # any-indent quoted line
        r"^\s*On\b.*\bwrote:\s*$",             # "On <date>, X wrote:"
        r"^.*<[^>]+@[^>]+>\s+wrote:\s*$",      # "Name <a@b> wrote:"
        r"^\s*(--\s*$)",                       # signature delimiter (redundant safeguard)
    ])
    text = drop_mailing_list_footer(text)      # "____ mailing list", "To unsubscribe…"

    # 4. Over-strip guard (protects against the dashed-line digest truncation):
    #    if ERP collapsed the body but there was nothing quote-like to strip,
    #    fall back to the de-signatured full body.
    if len(text.strip()) < 0.25 * len(body.strip()) and not has_quote_markers(body):
        text = drop_mailing_list_footer(strip_signature(body))
        method = "fallback-full"

    return text.strip(), method
```

- `talon.quotations` need not be imported at all. If a future corpus shows
  email-reply-parser missing quote forms that Talon catches, Talon can be added
  as a *secondary* signal behind step 3 — but this corpus gives no reason to.
- Record `method` in `extractions.method` (per Phase 1 schema) so `erp` vs
  `fallback-full` outputs are distinguishable.
- Keep handling for empty result, signature-only message, and non-UTF-8 /
  HTML-only bodies (untested here — no HTML-only messages appeared in the
  sample) as required by task 3.1.

---

## 6. Fixtures to promote to Phase 2/3 tests

All under `spikes/extraction/corpus/`. These pin the key behaviors:

| Fixture | Behavior pinned |
|---|---|
| `last-call__E22oZyg-pUNhqi6lF32vpCz0st4.eml` | Reference interleaved case: 6-way point-by-point reply. The primary regression guard. |
| `quic__Mwo6ZbD6sOL8cUhlWOXJ5Jbpw7c.eml` | Interleaved: greeting + one comment between deep-nested quotes. |
| `last-call__ku5TSOfHj-95ntPHqjeiKBADRrc.eml` | Interleaved with an author elision marker `[...]` that must be kept. |
| `tls__yUDZ03qD8njrMQtD0oR5nUYDcUM.eml` | Interleaved **with leading BOM** — pins the normalization step. |
| `last-call__QqHZZ04_1yZyE7JRVG97jEmAdCg.eml` | **Indented `>` quotes** — pins the custom quote-stripping pass (both libraries leak without it). |
| `tls__ukx10TSS3MfyprVOMlrJZkCqBUA.eml` | Large (345-line) interleaved thread — pins deep-thread handling / no truncation. |
| `tls__2-0cajV_2mXbTf0HDC1tzmToTc8.eml` | Top-post where author text (a `Ps.`) appears *after* a `~ signature` line — must be kept. |
| `quic__DRgTBE4FMhivJpOQ5B61TmpQuQw.eml` | Clean bottom-post: greeting + quote + reply + sign-off. |
| `last-call__RLYSMc1S5Ct6cMnUR1nP3FypTro.eml` | Bottom-post with a `-- ` confidentiality-notice signature that must be stripped. |
| `general__Zg5hbqTSpMQaCuKN6U8Ykkvrc_c.eml` | No-quote human prose ending in a `-- ` signature — keep prose, strip sig. |
| `general__6JNhAvFgxOH_qQ30g9kPY7jYAkw.eml` | Dashed-separator bot digest — pins the over-strip guard (email-reply-parser truncates it without the guard). |

---

## 7. Reproducing

```
cd spikes/extraction
python3.13 -m venv .venv
.venv/bin/pip install faust-cchardet email-reply-parser
.venv/bin/pip install --no-deps talon
.venv/bin/pip install lxml regex numpy scipy scikit-learn chardet cssselect six html5lib

.venv/bin/python harvest.py            # re-download corpus (public archive)
.venv/bin/python compare.py categorize # structural summary
.venv/bin/python compare.py dump       # per-message original vs both outputs -> output/
.venv/bin/python compare.py run <file.eml>
.venv/bin/python grade.py              # objective leak / retention metrics
```
