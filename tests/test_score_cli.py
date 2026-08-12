"""Tests for the mail-ai-score pipeline (run_score) against a temp database.

Uses a :class:`FakeClient` in place of :class:`PangramClient` so no network or
key is needed: it returns a canned verdict and counts calls, letting the tests
assert the 50-word gate, the score cache, the ``--limit`` cap, idempotency and
the summary tally. Also covers the two new Store methods.
"""

from __future__ import annotations

import pytest

from mailing_list_ai_check import cli
from mailing_list_ai_check.cleaning import clean_for_scoring
from mailing_list_ai_check.html_text import split_html_parts
from mailing_list_ai_check.pangram import PangramBulkOutcome, PangramError, PangramResult
from mailing_list_ai_check.store import Store, sha256_text

WORDS_60 = " ".join(f"word{i}" for i in range(60))
WORDS_10 = " ".join(f"word{i}" for i in range(10))


class FakeClient:
    """Stand-in for PangramClient: canned result, counts calls, optional error.

    ``model`` and ``version`` move together, as they do for the real client: the
    default selector returns a Pangram 3 verdict ("3.3.2"), ``pangram-4`` a
    Pangram 4 one ("4.0"). The score cache is filtered on that generation.
    ``predict_bulk`` returns the same canned verdict per item; ``bulk_errors``
    maps item ids (text hashes) that should fail to their error message.
    """

    def __init__(self, *, fail=False, model="default", version="3.3.2", bulk_errors=None):
        self.calls = 0
        self.bulk_calls = 0
        self.bulk_items = []
        self.model = model
        self.version = version
        self._fail = fail
        self._bulk_errors = bulk_errors or {}

    def _result(self):
        return PangramResult(
            fraction_ai=0.9,
            fraction_ai_assisted=0.1,
            fraction_human=0.0,
            prediction_short="AI",
            version=self.version,
            raw={"stage": "STAGE_SUCCESS", "fraction_ai": 0.9},
        )

    def predict(self, text):
        self.calls += 1
        if self._fail:
            raise PangramError("simulated failure")
        return self._result()

    def predict_bulk(self, items):
        self.bulk_calls += 1
        self.bulk_items.append(dict(items))
        if self._fail:
            raise PangramError("simulated bulk failure")
        results = {i: self._result() for i in items if i not in self._bulk_errors}
        errors = {i: self._bulk_errors[i] for i in items if i in self._bulk_errors}
        return PangramBulkOutcome(
            bulk_id="b1",
            status="partial" if errors else "succeeded",
            results=results,
            errors=errors,
        )


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "score.db") as s:
        yield s


def _seed_extraction(store, *, text, message_id, status="ok", raw_html=None):
    """Create the minimum rows for one scoreable extraction and return it."""
    mlist = store.upsert_list("l", "Shared Folders/l")
    addr = store.upsert_address("a@example.org", "A")
    up = store.upsert_message(
        message_id=message_id,
        list_id=mlist.id,
        address_id=addr.id,
        subject="s",
        date=None,
        in_reply_to=None,
        raw_body="raw",
        uid=None,
        raw_html=raw_html,
    )
    return store.insert_extraction(
        message_id=up.message.id,
        extracted_text=text,
        method="erp",
        status=status,
    )


# --- store methods ------------------------------------------------------------


def test_update_extraction_status(store):
    ext = _seed_extraction(store, text=WORDS_60, message_id="<a@x>")
    updated = store.update_extraction_status(ext.id, "too_short")
    assert updated is not None and updated.status == "too_short"


def test_update_extraction_status_rejects_bad_value(store):
    ext = _seed_extraction(store, text=WORDS_60, message_id="<a@x>")
    with pytest.raises(ValueError):
        store.update_extraction_status(ext.id, "bogus")


def test_update_extraction_status_missing_returns_none(store):
    assert store.update_extraction_status(999, "ok") is None


def test_iter_too_short_and_needing_score_partition(store):
    long_ext = _seed_extraction(store, text=WORDS_60, message_id="<long@x>")
    short_ext = _seed_extraction(store, text=WORDS_10, message_id="<short@x>")
    needing = [e.id for e in store.iter_extractions_needing_score(min_words=50)]
    too_short = [e.id for e in store.iter_too_short_extractions(min_words=50)]
    assert needing == [long_ext.id]
    assert too_short == [short_ext.id]


# --- pipeline -----------------------------------------------------------------


def test_short_text_marked_too_short_not_scored(store):
    ext = _seed_extraction(store, text=WORDS_10, message_id="<s@x>")
    client = FakeClient()
    summary = cli.run_score(store, client, limit=10)
    assert summary.too_short == 1
    assert summary.scored == 0
    assert client.calls == 0
    assert store.get_extraction(ext.id).status == "too_short"


def test_long_text_scored(store):
    ext = _seed_extraction(store, text=WORDS_60, message_id="<l@x>")
    client = FakeClient()
    summary = cli.run_score(store, client, limit=10)
    assert summary.scored == 1
    assert summary.api_calls == 1
    assert client.calls == 1
    assert summary.words_sent == 60
    # a score row exists for the extraction with sensible fields.
    row = store.conn.execute("SELECT * FROM scores WHERE extraction_id = ?", (ext.id,)).fetchone()
    assert row["label"] == "AI"
    assert row["fraction_ai"] == 0.9
    assert row["detector_version"] == "3.3.2"


def test_gating_uses_cleaned_word_count(store):
    # Raw text is well over 50 words, but once the greeting and the ``-- ``
    # signature block are removed the *cleaned* text is only 40 words — so it is
    # gated as too_short and never sent. The floor applies to the cleaned text.
    content = " ".join(f"word{i}" for i in range(40))
    debris = " ".join(f"sig{i}" for i in range(30))
    text = f"Hi team,\n{content}\n-- \n{debris}"
    assert len(text.split()) > 50  # raw text clears the floor
    ext = _seed_extraction(store, text=text, message_id="<cleanshort@x>")
    client = FakeClient()
    summary = cli.run_score(store, client, limit=10)
    assert summary.too_short == 1
    assert summary.scored == 0
    assert client.calls == 0
    assert store.get_extraction(ext.id).status == "too_short"


def test_scored_row_hashes_cleaned_text_not_raw(store):
    # A greeting + sign-off wrap 60 words of content; the cleaned text (60 words)
    # is what gets scored, counted, and hashed for the cache/score row.
    text = f"Hi all,\n{WORDS_60}\n\nBest,\nAlice"
    ext = _seed_extraction(store, text=text, message_id="<cleanscore@x>")
    client = FakeClient()
    summary = cli.run_score(store, client, limit=10)
    assert summary.scored == 1
    assert summary.words_sent == 60  # cleaned words, not the raw greeting+sign-off
    cleaned = clean_for_scoring(text).text
    row = store.conn.execute("SELECT * FROM scores WHERE extraction_id = ?", (ext.id,)).fetchone()
    assert row["text_sha256"] == sha256_text(cleaned)
    assert row["text_sha256"] != sha256_text(text)  # not the raw-extraction hash


def test_scored_text_uses_html_signature_hint(store):
    # A signature line with no "-- " delimiter and no recognizable contact shape
    # survives ordinary cleaning, but the message's HTML marks it as a signature.
    # run_score must clean with that hint, so the scored/hashed text excludes it.
    sig_line = "Jane Q Researcher Principal Engineer at Example Networks Limited"
    text = f"{WORDS_60}\n{sig_line}"
    raw_html = f'<div>{WORDS_60}</div><div class="gmail_signature">{sig_line}</div>'
    ext = _seed_extraction(store, text=text, message_id="<hint@x>", raw_html=raw_html)
    client = FakeClient()
    summary = cli.run_score(store, client, limit=10)
    assert summary.scored == 1
    # Scored word count is the content only (60), not content + signature line.
    assert summary.words_sent == 60
    hinted = clean_for_scoring(text, split_html_parts(raw_html).signature_text).text
    assert sig_line not in hinted
    row = store.conn.execute(
        "SELECT text_sha256 FROM scores WHERE extraction_id = ?", (ext.id,)
    ).fetchone()
    assert row["text_sha256"] == sha256_text(hinted)
    # And distinct from the un-hinted cleaning (which would keep the signature).
    assert row["text_sha256"] != sha256_text(clean_for_scoring(text).text)


def test_cache_hit_skips_api(store):
    # Two extractions with identical text: the first scores via API, the second
    # is served from the cache without a call.
    _seed_extraction(store, text=WORDS_60, message_id="<c1@x>")
    _seed_extraction(store, text=WORDS_60, message_id="<c2@x>")
    client = FakeClient()
    summary = cli.run_score(store, client, limit=10)
    assert summary.scored == 1
    assert summary.cache_hits == 1
    assert client.calls == 1  # only one real call for two identical texts
    # both extractions now have a score row.
    n = store.conn.execute("SELECT COUNT(*) AS c FROM scores").fetchone()["c"]
    assert n == 2


def test_cache_hit_requires_the_same_detector_generation(store):
    # The first extraction is scored by the default (Pangram 3) selector; the
    # second holds identical text but is run with a Pangram 4 client, so the
    # cached 3.3.2 verdict must not be served — the text is scored again.
    _seed_extraction(store, text=WORDS_60, message_id="<g1@x>")
    v3 = FakeClient()
    cli.run_score(store, v3, limit=10)

    _seed_extraction(store, text=WORDS_60, message_id="<g2@x>")
    v4 = FakeClient(model="pangram-4", version="4.0")
    summary = cli.run_score(store, v4, limit=10)

    assert summary.cache_hits == 0
    assert summary.scored == 1
    assert v4.calls == 1
    versions = [
        row["detector_version"]
        for row in store.conn.execute("SELECT detector_version FROM scores ORDER BY id")
    ]
    assert versions == ["3.3.2", "4.0"]


def test_cache_hit_within_the_same_generation_still_skips_the_api(store):
    # Same text, same generation: the second extraction is served from the cache.
    _seed_extraction(store, text=WORDS_60, message_id="<g3@x>")
    _seed_extraction(store, text=WORDS_60, message_id="<g4@x>")
    client = FakeClient(model="pangram-4", version="4.0")
    summary = cli.run_score(store, client, limit=10)
    assert summary.scored == 1
    assert summary.cache_hits == 1
    assert client.calls == 1


def test_limit_caps_api_calls(store):
    for i in range(5):
        _seed_extraction(store, text=f"{WORDS_60} unique{i}", message_id=f"<u{i}@x>")
    client = FakeClient()
    summary = cli.run_score(store, client, limit=2)
    assert summary.scored == 2
    assert client.calls == 2  # capped
    # remaining extractions are left unscored for a later run.
    assert len(list(store.iter_extractions_needing_score(min_words=50))) == 3


def test_idempotent_rerun_does_nothing(store):
    _seed_extraction(store, text=WORDS_60, message_id="<i@x>")
    client = FakeClient()
    cli.run_score(store, client, limit=10)
    second = cli.run_score(store, client, limit=10)
    assert second.scored == 0
    assert second.cache_hits == 0
    assert client.calls == 1  # no new call on the second run


def test_failed_call_counted_not_stored(store):
    _seed_extraction(store, text=WORDS_60, message_id="<f@x>")
    client = FakeClient(fail=True)
    summary = cli.run_score(store, client, limit=10)
    assert summary.failed == 1
    assert summary.scored == 0
    assert summary.api_calls == 1
    # no score row; the extraction stays scoreable for a later run.
    assert store.conn.execute("SELECT COUNT(*) AS c FROM scores").fetchone()["c"] == 0


def test_dry_run_makes_no_calls_or_writes(store):
    long_ext = _seed_extraction(store, text=WORDS_60, message_id="<d1@x>")
    short_ext = _seed_extraction(store, text=WORDS_10, message_id="<d2@x>")
    summary = cli.run_score(store, None, limit=10, dry_run=True)
    assert summary.scored == 1  # would be scored
    assert summary.too_short == 1  # would be gated
    # nothing written: no score rows, statuses unchanged.
    assert store.conn.execute("SELECT COUNT(*) AS c FROM scores").fetchone()["c"] == 0
    assert store.get_extraction(short_ext.id).status == "ok"
    assert store.get_extraction(long_ext.id).status == "ok"


def test_dry_run_respects_limit(store):
    for i in range(4):
        _seed_extraction(store, text=f"{WORDS_60} u{i}", message_id=f"<dr{i}@x>")
    summary = cli.run_score(store, None, limit=2, dry_run=True)
    assert summary.scored == 2


def test_summary_line_format(store):
    summary = cli.ScoreSummary(scored=3, cache_hits=1, too_short=2, api_calls=3, words_sent=180)
    line = summary.as_line()
    assert "scored=3" in line
    assert "cache_hits=1" in line
    assert "words_sent=180" in line
    # 180 words at the Pangram 4 realtime price of $0.05 per 100 words.
    assert "est_spend=$0.0900" in line


def test_spend_estimate_uses_the_active_generation_price(store):
    # A Pangram 3 run (the "default" selector) estimates at $0.05 per 1,000
    # words, a Pangram 4 run at $0.05 per 100 — run_score stamps the summary
    # with the active generation's price.
    _seed_extraction(store, text=WORDS_60, message_id="<p3@x>")
    summary = cli.run_score(store, FakeClient(model="default"), limit=10)
    assert summary.estimated_spend == pytest.approx(60 / 1000 * 0.05)

    _seed_extraction(store, text=WORDS_60 + " extra", message_id="<p4@x>")
    summary = cli.run_score(store, FakeClient(model="pangram-4", version="4.0"), limit=10)
    assert summary.estimated_spend == pytest.approx(61 / 100 * 0.05)


# --- bulk mode ------------------------------------------------------------


def _cleaned_sha(text):
    """The score-cache key run_score derives for ``text`` (no HTML hint)."""
    return sha256_text(clean_for_scoring(text).text)


def test_bulk_scores_the_run_in_one_job(store):
    exts = [
        _seed_extraction(store, text=f"{WORDS_60} unique{i}", message_id=f"<b{i}@x>")
        for i in range(3)
    ]
    client = FakeClient()
    summary = cli.run_score(store, client, limit=10, bulk=True)
    assert summary.scored == 3
    assert summary.api_calls == 3  # the billed unit: texts submitted
    assert client.bulk_calls == 1
    assert client.calls == 0  # no realtime calls in bulk mode
    # Items are keyed by the cleaned text's sha256, the score-cache key.
    assert set(client.bulk_items[0]) == {_cleaned_sha(f"{WORDS_60} unique{i}") for i in range(3)}
    for ext in exts:
        row = store.conn.execute(
            "SELECT * FROM scores WHERE extraction_id = ?", (ext.id,)
        ).fetchone()
        assert row["label"] == "AI"
        assert row["detector_version"] == "3.3.2"


def test_bulk_deduplicates_identical_text_within_the_run(store):
    _seed_extraction(store, text=WORDS_60, message_id="<bd1@x>")
    _seed_extraction(store, text=WORDS_60, message_id="<bd2@x>")
    client = FakeClient()
    summary = cli.run_score(store, client, limit=10, bulk=True)
    assert len(client.bulk_items[0]) == 1  # one item for two identical texts
    assert summary.scored == 1
    assert summary.cache_hits == 1
    assert summary.words_sent == 60  # billed once
    # both extractions still get a score row from the one verdict.
    assert store.conn.execute("SELECT COUNT(*) AS c FROM scores").fetchone()["c"] == 2


def test_bulk_respects_limit(store):
    for i in range(5):
        _seed_extraction(store, text=f"{WORDS_60} unique{i}", message_id=f"<bl{i}@x>")
    client = FakeClient()
    summary = cli.run_score(store, client, limit=2, bulk=True)
    assert summary.scored == 2
    assert summary.api_calls == 2
    assert len(client.bulk_items[0]) == 2
    # remaining extractions are left unscored for a later run.
    assert len(list(store.iter_extractions_needing_score(min_words=50))) == 3


def test_bulk_still_serves_the_persistent_cache(store):
    # The first run scores the text; a second, bulk run over identical new text
    # is served entirely from the score cache — no bulk job is submitted.
    _seed_extraction(store, text=WORDS_60, message_id="<bc1@x>")
    cli.run_score(store, FakeClient(), limit=10)
    _seed_extraction(store, text=WORDS_60, message_id="<bc2@x>")
    client = FakeClient()
    summary = cli.run_score(store, client, limit=10, bulk=True)
    assert summary.cache_hits == 1
    assert summary.api_calls == 0
    assert client.bulk_calls == 0


def test_bulk_item_failure_leaves_extraction_scoreable(store):
    ok_text = f"{WORDS_60} fine"
    bad_text = f"{WORDS_60} broken"
    _seed_extraction(store, text=ok_text, message_id="<bf1@x>")
    bad = _seed_extraction(store, text=bad_text, message_id="<bf2@x>")
    client = FakeClient(bulk_errors={_cleaned_sha(bad_text): "item failed"})
    summary = cli.run_score(store, client, limit=10, bulk=True)
    assert summary.scored == 1
    assert summary.failed == 1
    assert summary.api_calls == 2  # both texts were submitted
    # the failed item's extraction has no score row and stays in the queue.
    row = store.conn.execute(
        "SELECT COUNT(*) AS c FROM scores WHERE extraction_id = ?", (bad.id,)
    ).fetchone()
    assert row["c"] == 0
    assert [e.id for e in store.iter_extractions_needing_score(min_words=50)] == [bad.id]


def test_bulk_submit_failure_counts_every_text_failed(store):
    for i in range(3):
        _seed_extraction(store, text=f"{WORDS_60} u{i}", message_id=f"<bx{i}@x>")
    client = FakeClient(fail=True)
    summary = cli.run_score(store, client, limit=10, bulk=True)
    assert summary.failed == 3
    assert summary.scored == 0
    assert summary.api_calls == 3
    # nothing stored; every extraction stays scoreable for a later run.
    assert store.conn.execute("SELECT COUNT(*) AS c FROM scores").fetchone()["c"] == 0
    assert len(list(store.iter_extractions_needing_score(min_words=50))) == 3


def test_bulk_dry_run_makes_no_calls_or_writes(store):
    _seed_extraction(store, text=WORDS_60, message_id="<bdr1@x>")
    _seed_extraction(store, text=WORDS_60, message_id="<bdr2@x>")  # in-run duplicate
    _seed_extraction(store, text=f"{WORDS_60} more", message_id="<bdr3@x>")
    _seed_extraction(store, text=WORDS_10, message_id="<bdr4@x>")
    summary = cli.run_score(store, None, limit=10, dry_run=True, bulk=True)
    assert summary.scored == 2  # two unique texts would be submitted
    assert summary.cache_hits == 1  # the duplicate would ride along free
    assert summary.too_short == 1
    assert summary.words_sent == 60 + 61
    assert store.conn.execute("SELECT COUNT(*) AS c FROM scores").fetchone()["c"] == 0


def test_bulk_spend_estimate_applies_the_discount(store):
    # 60 words at the Pangram 4 price with the 20% bulk discount applied.
    _seed_extraction(store, text=WORDS_60, message_id="<bp@x>")
    client = FakeClient(model="pangram-4", version="4.0")
    summary = cli.run_score(store, client, limit=10, bulk=True)
    assert summary.estimated_spend == pytest.approx(60 / 100 * 0.05 * 0.8)


# --- score_main arg handling --------------------------------------------------


def test_score_main_rejects_bad_limit(monkeypatch):
    monkeypatch.setenv("PANGRAM_API_KEY", "test-key")
    with pytest.raises(SystemExit) as exc:
        cli.score_main(["--limit", "0"])
    assert exc.value.code == 2


def test_score_main_requires_key_for_real_runs(monkeypatch, tmp_path):
    """Without PANGRAM_API_KEY a non-dry-run exits with a clear error (code 2)."""
    monkeypatch.delenv("PANGRAM_API_KEY", raising=False)
    db = tmp_path / "nokey.db"
    with Store(db) as store:
        _seed_extraction(store, text=WORDS_60, message_id="<nokey@x>")
    with pytest.raises(SystemExit) as exc:
        cli.score_main(["--db", str(db)])
    assert exc.value.code == 2


def test_score_main_dry_run_works_without_key(monkeypatch, tmp_path):
    monkeypatch.delenv("PANGRAM_API_KEY", raising=False)
    db = tmp_path / "nokey-dry.db"
    with Store(db) as store:
        _seed_extraction(store, text=WORDS_60, message_id="<nokeydry@x>")
    assert cli.score_main(["--db", str(db), "--dry-run"]) == 0


def _recording_client(monkeypatch, built):
    """Replace cli.PangramClient with a FakeClient that records its arguments."""

    def _build(api_key, *, model=None, **kwargs):
        built.append({"api_key": api_key, "model": model})
        return FakeClient(model=model, version="4.0" if model == "pangram-4" else "3.3.2")

    monkeypatch.setattr(cli, "PangramClient", _build)


def test_score_main_uses_the_stored_model_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("PANGRAM_API_KEY", "test-key")
    db = tmp_path / "setting.db"
    with Store(db) as store:
        _seed_extraction(store, text=WORDS_60, message_id="<setting@x>")
        store.set_setting("pangram_model", "default")

    built: list[dict] = []
    _recording_client(monkeypatch, built)
    assert cli.score_main(["--db", str(db)]) == 0
    assert built == [{"api_key": "test-key", "model": "default"}]


def test_score_main_defaults_to_pangram_4_without_a_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("PANGRAM_API_KEY", "test-key")
    db = tmp_path / "nosetting.db"
    with Store(db) as store:
        _seed_extraction(store, text=WORDS_60, message_id="<nosetting@x>")

    built: list[dict] = []
    _recording_client(monkeypatch, built)
    assert cli.score_main(["--db", str(db)]) == 0
    assert built == [{"api_key": "test-key", "model": "pangram-4"}]


def test_score_main_model_flag_overrides_the_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("PANGRAM_API_KEY", "test-key")
    db = tmp_path / "override.db"
    with Store(db) as store:
        _seed_extraction(store, text=WORDS_60, message_id="<override@x>")
        store.set_setting("pangram_model", "default")

    built: list[dict] = []
    _recording_client(monkeypatch, built)
    assert cli.score_main(["--db", str(db), "--model", "pangram-4"]) == 0
    assert built == [{"api_key": "test-key", "model": "pangram-4"}]
    with Store(db) as store:
        version = store.conn.execute("SELECT detector_version AS v FROM scores").fetchone()["v"]
    # The flag is a per-run override; the stored setting is left as it was.
    assert version == "4.0"
    with Store(db) as store:
        assert store.get_setting("pangram_model") == "default"


def test_score_main_rejects_an_unknown_model(monkeypatch):
    monkeypatch.setenv("PANGRAM_API_KEY", "test-key")
    with pytest.raises(SystemExit) as exc:
        cli.score_main(["--model", "pangram-5"])
    assert exc.value.code == 2


def test_score_main_bulk_flag_uses_the_bulk_api(monkeypatch, tmp_path):
    monkeypatch.setenv("PANGRAM_API_KEY", "test-key")
    db = tmp_path / "bulk.db"
    with Store(db) as store:
        _seed_extraction(store, text=WORDS_60, message_id="<bulkmain@x>")

    clients: list[FakeClient] = []

    def _build(api_key, *, model=None, **kwargs):
        client = FakeClient(model=model)
        clients.append(client)
        return client

    monkeypatch.setattr(cli, "PangramClient", _build)
    assert cli.score_main(["--db", str(db), "--bulk"]) == 0
    assert clients[0].bulk_calls == 1
    assert clients[0].calls == 0
    with Store(db) as store:
        assert store.conn.execute("SELECT COUNT(*) AS c FROM scores").fetchone()["c"] == 1


def test_score_main_dry_run_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setenv("PANGRAM_API_KEY", "test-key")
    db = tmp_path / "e2e.db"
    with Store(db) as store:
        _seed_extraction(store, text=WORDS_60, message_id="<e@x>")
    # dry-run needs no client / real key beyond Config.load's requirement.
    rc = cli.score_main(["--db", str(db), "--dry-run"])
    assert rc == 0
    with Store(db) as store:
        assert store.conn.execute("SELECT COUNT(*) AS c FROM scores").fetchone()["c"] == 0
