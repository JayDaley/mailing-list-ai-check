"""Offline client, queue, isolation and export tests. No paid API calls."""

import io
import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
import requests

from mailing_list_ai_check.config import Config
from mailing_list_ai_check.store import Store
from mailing_list_ai_check.stylome import MODEL, StylomeClient, StylomeError
from mailing_list_ai_check.stylome_pipeline import (
    current_scores,
    export_stylome,
    main,
    run_stylome,
)
from mailing_list_ai_check.webapp import create_app

TEXT = " ".join(f"word{i}" for i in range(60))


def response(point="0.01%", evaluation="eval_12_002", label="ai_edited"):
    return {
        "id": "scan_fixture",
        "mock": False,
        "model_version": MODEL,
        "operating_point": point,
        "evaluation_version": evaluation,
        "response_version": "stylome-public-v2",
        "label": label,
        "confidence": "medium",
        "content_hash": "0" * 64,
        "created_at": "2026-11-19T00:00:00Z",
        "receipt": {"id": "fixture", "share_token": "private capability"},
    }


def client(point="0.01%", evaluation="eval_12_002"):
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = response(point, evaluation)
    return StylomeClient(
        "fixture", operating_point=point, evaluation_version=evaluation, session=session
    )


def seed(store, text=TEXT, mid="<fixture@example.invalid>"):
    mlist = store.upsert_list("fixture", "Shared Folders/fixture")
    msg = store.upsert_message(
        message_id=mid,
        list_id=mlist.id,
        address_id=None,
        subject="fixture",
        date=None,
        in_reply_to=None,
        raw_body=text,
        uid=None,
    ).message
    return store.insert_extraction(
        message_id=msg.id, extracted_text=text, method="custom", status="ok"
    )


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "fixture.db") as store:
        yield store


def run(store, api=None, **kwargs):
    api = api or client()
    return run_stylome(
        store,
        api,
        operating_point=api.operating_point,
        evaluation_version=api.evaluation_version,
        **kwargs,
    )


def test_public_contract_and_receipt_removal():
    api = client()
    result = api.predict("  " + TEXT + "\n\t")
    args = api.session.post.call_args
    assert args.args == ("https://stylome.io/api/v1/score",)
    assert args.kwargs["json"] == {"text": TEXT, "model": MODEL, "operating_point": "0.01%"}
    assert args.kwargs["allow_redirects"] is False
    assert result.any_ai and "receipt" not in result.raw
    key = args.kwargs["headers"]["Idempotency-Key"]
    api.predict(TEXT)
    assert api.session.post.call_args.kwargs["headers"]["Idempotency-Key"] == key
    other = client("0.1%")
    other.predict(TEXT)
    assert other.session.post.call_args.kwargs["headers"]["Idempotency-Key"] != key


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_version", "other"),
        ("evaluation_version", "eval_12_003"),
        ("operating_point", "0.1%"),
        ("label", "Mixed"),
        ("mock", True),
        ("confidence", "99%"),
        ("response_version", "other"),
    ],
)
def test_contract_mismatch_fails_closed(field, value):
    api = client()
    api.session.post.return_value.json.return_value[field] = value
    with pytest.raises(StylomeError, match="contract mismatch"):
        api.predict(TEXT)


def test_transport_error_has_no_retry_or_credential_text():
    api = client()
    api.session.post.side_effect = requests.ReadTimeout("secret body and credential")
    with pytest.raises(StylomeError) as exc:
        api.predict(TEXT)
    assert "secret" not in str(exc.value)
    assert api.session.post.call_count == 1


@pytest.mark.parametrize("status", [302, 400, 401, 402, 409, 429, 500])
def test_http_error_not_retried(status):
    api = client()
    api.session.post.return_value.status_code = status
    with pytest.raises(StylomeError, match=f"HTTP {status}"):
        api.predict(TEXT)
    assert api.session.post.call_count == 1


@pytest.mark.parametrize("text", ["x" * 199, "x" * 10_001])
def test_client_length_gate(text):
    api = client()
    with pytest.raises(StylomeError):
        api.predict(text)
    api.session.post.assert_not_called()


def test_queue_cap_cache_rerun_and_pangram_isolation(store):
    one = seed(store)
    seed(store, mid="<duplicate@example.invalid>")
    seed(store, text=TEXT + " different", mid="<other@example.invalid>")
    pangram = store.insert_score(
        extraction_id=one.id,
        text_sha256="fixture",
        fraction_ai=0.9,
        fraction_ai_assisted=0.1,
        fraction_human=0.0,
        label="AI",
        detector_version="4.0",
        raw_response={"fixture": True},
    )
    api = client()
    summary = run(store, api, limit=1)
    assert (summary.scored, summary.cache_hits, summary.api_calls) == (1, 1, 1)
    assert store.score_for_extraction(one.id) == pangram
    assert current_scores(store, one)[0]["label"] == "ai_edited"
    summary = run(store, api, limit=1)
    assert (summary.scored, summary.already_scored, summary.api_calls) == (1, 2, 1)
    assert run(store, api).api_calls == 0
    assert api.session.post.call_count == 2


def test_cache_separates_points_and_evaluation_versions(store):
    seed(store)
    assert run(store).api_calls == 1
    assert run(store, client("0.1%")).api_calls == 1
    assert run(store, client(evaluation="fixture_next_evaluation")).api_calls == 1
    assert store.conn.execute("SELECT count(*) FROM stylome_scores").fetchone()[0] == 3


def test_input_change_hides_stale_score_and_export_marks_it(store):
    ext = seed(store)
    run(store)
    store.conn.execute(
        "UPDATE extractions SET extracted_text=? WHERE id=?", (TEXT + " new", ext.id)
    )
    store.conn.commit()
    ext = store.get_extraction(ext.id)
    assert current_scores(store, ext) == []
    output = io.StringIO()
    assert export_stylome(store, output) == 1
    row = json.loads(output.getvalue())
    assert row["current_input"] is False and row["any_ai"] is True
    assert row["input_text"] == TEXT and row["message_id"] == "<fixture@example.invalid>"
    assert "receipt" not in row["response"]
    assert run(store).api_calls == 1
    assert len(current_scores(store, ext)) == 1


def test_gates_no_truncation_no_status_mutation_and_failure_stops(store):
    short = seed(store, text="x " * 50)
    long = seed(store, text="word " * 2100, mid="<long@example.invalid>")
    seed(store, mid="<ok@example.invalid>")
    seed(store, text=TEXT + " other", mid="<other@example.invalid>")
    api = client()
    api.session.post.return_value.status_code = 402
    summary = run(store, api)
    assert (summary.too_short, summary.too_long, summary.failed, summary.api_calls) == (1, 1, 1, 1)
    assert store.get_extraction(short.id).status == store.get_extraction(long.id).status == "ok"
    assert store.conn.execute("SELECT count(*) FROM stylome_scores").fetchone()[0] == 0


def test_dry_run_does_not_score_write_or_repeat_billed_duplicate(store):
    seed(store)
    seed(store, mid="<duplicate@example.invalid>")
    summary = run_stylome(
        store, None, operating_point="0.01%", evaluation_version="eval_12_002", dry_run=True
    )
    assert (summary.scored, summary.cache_hits, summary.api_calls) == (1, 1, 0)
    assert store.conn.execute("SELECT count(*) FROM stylome_scores").fetchone()[0] == 0


def test_automated_mail_excluded(store):
    ext = seed(store)
    store.conn.execute("UPDATE messages SET auto_generated=1 WHERE id=?", (ext.message_id,))
    store.conn.commit()
    assert run(store).api_calls == 0


def test_detail_reports_stylome_without_changing_pangram_slot(store):
    ext = seed(store)
    run(store)
    config = replace(Config.load(), database_path=store.path)
    app = create_app(config, frontend_dist=None)
    detail = app.test_client().get(f"/api/messages/{ext.message_id}").get_json()
    assert detail["score"] is None
    assert detail["stylome_scores"][0]["label"] == "ai_edited"
    assert "private capability" not in json.dumps(detail)


def test_cli_export_needs_no_key_and_cannot_overwrite(store, tmp_path, monkeypatch):
    seed(store)
    run(store)
    monkeypatch.setattr(Config, "load", lambda: replace(config, stylome_api_key=""))
    config = Config(
        imap_host="",
        imap_port=993,
        imap_username="",
        imap_password="",
        pangram_api_key="",
        database_path=store.path,
        log_level="INFO",
        flask_host="127.0.0.1",
        flask_port=8050,
    )
    output = tmp_path / "export.jsonl"
    assert main(["--export", str(output)]) == 0
    with pytest.raises(FileExistsError):
        main(["--export", str(output)])


def test_upgrade_from_019_preserves_pangram(store):
    ext = seed(store)
    previous = store.insert_score(
        extraction_id=ext.id,
        text_sha256="fixture",
        fraction_ai=0.0,
        fraction_ai_assisted=0.0,
        fraction_human=1.0,
        label="Human",
        detector_version="4.0",
        raw_response={"fixture": True},
    )
    store.conn.execute("DROP TABLE stylome_scores")
    store.conn.execute("DELETE FROM schema_version WHERE version=20")
    store.conn.commit()
    with Store(store.path) as upgraded:
        assert upgraded.score_for_extraction(ext.id) == previous
        assert upgraded.conn.execute("SELECT count(*) FROM stylome_scores").fetchone()[0] == 0
