import json

import httpx
import pytest

from sourceloom.providers import (
    Uncertain,
    apply_stream_timeout,
    post_responses_stream_before_deadline,
    responses_body_from_sse,
    streaming_responses_enabled,
)


def _event(kind, response):
    payload = json.dumps({"type": kind, "response": response})
    return [f"event: {kind}", f"data: {payload}", ""]


def test_responses_stream_keeps_completed_artifact_and_usage():
    response = {
        "id": "resp_1",
        "status": "completed",
        "output_text": '{"ok":true}',
        "usage": {"input_tokens": 31, "output_tokens": 4, "total_tokens": 35},
    }
    lines = _event("response.completed", response) + ["data: [DONE]", ""]

    assert responses_body_from_sse(lines) == response


@pytest.mark.parametrize("kind,status", [("response.incomplete", "incomplete"),
                                           ("response.failed", "failed")])
def test_responses_stream_preserves_non_success_terminal_for_normal_failure_handling(kind, status):
    response = {"id": "resp_2", "status": status,
                "usage": {"input_tokens": 9, "output_tokens": 2, "total_tokens": 11},
                "error": {"message": "provider failure"}}
    assert responses_body_from_sse(_event(kind, response)) == response


def test_responses_stream_rejects_truncated_stream_and_bad_usage():
    with pytest.raises(Uncertain):
        responses_body_from_sse(["event: response.output_text.delta",
                                 'data: {"type":"response.output_text.delta","delta":"partial"}', ""])
    malformed = {"status": "completed", "output_text": "{}",
                 "usage": {"input_tokens": -1, "output_tokens": 2}}
    with pytest.raises(Uncertain):
        responses_body_from_sse(_event("response.completed", malformed))


def test_responses_stream_keeps_missing_usage_unknown():
    body = {"id": "resp_unknown", "status": "completed", "output_text": '{"ok":true}'}
    assert responses_body_from_sse(_event("response.completed", body)) == body


def test_responses_streaming_is_kuafu_only_and_uses_extended_call_timeout():
    route = {"base_url": "https://api.kuafushe.cc/v1", "protocol": "responses",
             "call_timeout": 90, "kuafu_stream_timeout": 240}
    assert streaming_responses_enabled(route, "responses")
    assert not streaming_responses_enabled({**route, "base_url": "https://api.example/v1"}, "responses")
    assert not streaming_responses_enabled(route, "chat_completions")
    assert apply_stream_timeout(route)["call_timeout"] == 240
    assert not streaming_responses_enabled({**route, "kuafu_responses_streaming": False}, "responses")
    assert apply_stream_timeout({**route, "kuafu_responses_streaming": False})["call_timeout"] == 90


def test_responses_stream_http_adapter_sends_stream_true_and_builds_final_json(monkeypatch):
    final = {"id": "resp_3", "status": "completed", "output_text": '{"ok":true}',
             "usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}}
    content = "\n".join(_event("response.completed", final) + ["data: [DONE]", ""])
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    original = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("sourceloom.providers.httpx.AsyncClient", client_factory)
    result = post_responses_stream_before_deadline(
        "https://api.kuafushe.cc/v1/responses", {"Authorization": "Bearer test"},
        {"model": "test-model", "stream": True}, deadline=10**10)

    assert seen["json"]["stream"] is True
    assert result.status_code == 200
    assert result.json() == final


def test_responses_stream_http_adapter_returns_524_without_parsing_as_sse(monkeypatch):
    original = httpx.AsyncClient

    def handler(request):
        return httpx.Response(524, json={"error": "upstream timeout"})

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    monkeypatch.setattr("sourceloom.providers.httpx.AsyncClient", client_factory)
    result = post_responses_stream_before_deadline(
        "https://api.kuafushe.cc/v1/responses", {}, {"stream": True}, deadline=10**10)
    assert result.status_code == 524
    assert result.json()["error"] == "upstream timeout"


def test_responses_stream_adapter_accepts_complete_terminal_before_transport_close(monkeypatch):
    final = {"id": "resp_4", "status": "completed", "output_text": '{"ok":true}',
             "usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6}}
    frame = ("\n".join(_event("response.completed", final) + ["data: [DONE]", ""])
             + "\n").encode()

    class CompletedThenDisconnect(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame
            raise httpx.ReadError("synthetic close after complete event")

        async def aclose(self):
            return None

    original = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: httpx.Response(200, stream=CompletedThenDisconnect(),
                headers={"content-type": "text/event-stream"}))
        return original(*args, **kwargs)

    monkeypatch.setattr("sourceloom.providers.httpx.AsyncClient", client_factory)
    result = post_responses_stream_before_deadline(
        "https://api.kuafushe.cc/v1/responses", {}, {"stream": True}, deadline=10**10)
    assert result.status_code == 200
    assert result.json() == final
