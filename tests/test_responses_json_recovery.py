import json

import pytest
from pydantic import BaseModel

from sourceloom.active_composition import ActiveComposition, repair_one_missing_json_object_closer
from sourceloom.durable import Queue
from sourceloom.production import Production
from sourceloom.providers import Provider
from sourceloom.store import Conflict
from tests.test_production import prepared, skill


class Answer(BaseModel):
    answer: str


def _uncertain_responses_job(tmp_path, skill, response, response_status="completed"):
    store, _, project, bundle = prepared(tmp_path, skill)
    queue = Queue(store, pipeline="active_composition_v2")
    job = queue.enqueue(project["id"], bundle)
    key = "active-write-p7-n-002-turn-0"
    call = dict(id="saved-responses", role="active_write", status=response_status,
        step_key=key, channel="openai-compatible", provider_id="kuafu-responses",
        protocol="responses", upstream_base="https://api.kuafushe.cc/v1",
        finish_reason=response.get("status"), response_blob=store.blob(
            json.dumps(response, ensure_ascii=False).encode()), dispatch_started=True)
    job.update(status="uncertain", stage="active_write", pending=key,
        current_step_key=key, calls=[call], results={"prior-checkpoint": {"kept": True}})
    store.put_job(job)
    store.change(project["id"], lambda p: p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?", (job["id"],))
        cx.execute("INSERT INTO spending(id,project,reserved,actual,status,body) VALUES(?,?,?,?,?,?)",
            (call["id"], project["id"], .02, .01, "settled",
             json.dumps({"status": "settled", "actual_cny": .01, "usage": {"input_tokens": 10}})))
    return store, queue, project, job, key, call


def _responses_config():
    route = dict(provider="openai-compatible", provider_id="kuafu-responses",
        model="deepseek-v4.1-flash", protocol="responses", endpoint="/responses",
        base_url="https://api.kuafushe.cc/v1", enabled=True)
    return dict(provider_routes={"kuafu-responses": route},
        provider_credentials={"kuafu-responses": "test-key"},
        role_providers={"active_write": route | {"api_key": "test-key"}})


def test_single_missing_final_object_closer_is_added_without_touching_content():
    text = '{"answer":"' + ("x" * 6800) + '"'
    repaired = repair_one_missing_json_object_closer(text, allow_final_root=True)
    assert repaired == text + "}"
    assert json.loads(repaired) == {"answer": "x" * 6800}
    assert repair_one_missing_json_object_closer('{"answer":"x"', allow_final_root=True) == '{"answer":"x"}'
    assert repair_one_missing_json_object_closer('{"answer":"x}') is None
    assert repair_one_missing_json_object_closer('{"answer":}') is None


@pytest.mark.parametrize("job_status", ["uncertain", "failed"])
def test_saved_completed_responses_json_is_resumed_and_repaired_without_resending_source(
        tmp_path, skill, monkeypatch, job_status):
    raw = '{"answer":"' + ("x" * 6800) + '"'
    response = {"status": "completed", "output_text": raw,
        "usage": {"input_tokens": 100, "output_tokens": 3000}}
    store, queue, project, job, key, call = _uncertain_responses_job(tmp_path, skill, response)
    job["status"] = job_status
    store.put_job(job)

    resumed = queue.retry_validation(job["id"], _responses_config())
    assert resumed["status"] == "queued"
    assert resumed["pending"] == key
    assert resumed["calls"][-1]["id"] == call["id"]
    assert resumed["results"]["prior-checkpoint"] == {"kept": True}
    assert resumed["logical_requests"][job["id"] + ":" + key]["status"] == "KNOWN_FAILURE"
    with store.connect() as cx:
        bill = cx.execute("SELECT actual,status,body FROM spending WHERE id=?", (call["id"],)).fetchone()
        assert bill["actual"] == .01 and bill["status"] == "settled"
        assert json.loads(bill["body"])["usage"]["input_tokens"] == 10

    attempted = []

    def no_new_generation(provider, *args, **kwargs):
        attempted.append(args)
        raise AssertionError("saved Responses recovery must not submit another model request")

    monkeypatch.setattr(Provider, "call", no_new_generation)
    engine = ActiveComposition(Production(store, _responses_config()))
    monkeypatch.setattr(engine.queue, "cancelled", lambda *args: False)
    result = engine.call(resumed, key, "active_write", {"must": "not be resent"}, Answer)
    assert result == {"answer": "x" * 6800}
    assert resumed["results"][key] == result
    assert resumed["active_json_syntax_repairs"][-1]["operation"] == "insert_one_missing_object_closer"
    assert resumed["calls"][-1]["status"] == "recovered"
    assert not attempted


def test_completed_but_otherwise_malformed_artifact_uses_protocol_repair_and_never_returns_source(
        tmp_path, skill, monkeypatch):
    response = {"status": "completed", "output_text": '{"answer": }', "usage": {}}
    store, queue, project, job, key, call = _uncertain_responses_job(tmp_path, skill, response)
    resumed = queue.retry_validation(job["id"], _responses_config())
    attempted = []

    def protocol_repair_fails(provider, *args, **kwargs):
        attempted.append(args)
        raise ValueError("bounded protocol repair failed")

    monkeypatch.setattr(Provider, "call", protocol_repair_fails)
    engine = ActiveComposition(Production(store, _responses_config()))
    monkeypatch.setattr(engine.queue, "cancelled", lambda *args: False)
    with pytest.raises(ValueError, match="bounded protocol repair failed"):
        engine.call(resumed, key, "active_write", {"source": "PRIVATE SOURCE SHOULD NOT BE OUTPUT"}, Answer)
    assert attempted
    assert key not in resumed["results"]
    assert resumed["calls"][-1]["id"] == call["id"]
    assert resumed["calls"][-1]["status"] == "recovered"


def test_incomplete_responses_body_cannot_resume_or_be_mistaken_for_generated_output(
        tmp_path, skill):
    response = {"status": "incomplete", "output_text": '{"answer":"source"', "usage": {}}
    store, queue, project, job, key, call = _uncertain_responses_job(
        tmp_path, skill, response, response_status="incomplete")
    with pytest.raises(Conflict):
        queue.retry_validation(job["id"], _responses_config())
    provider = Provider(store, _responses_config())
    with pytest.raises(Conflict, match="incomplete"):
        provider.recover(store.job(job["id"]))
    current = store.job(job["id"])
    assert current["status"] == "uncertain"
    assert key not in current["results"]
    assert current["calls"][-1]["id"] == call["id"]
