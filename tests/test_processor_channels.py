import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest

from sourceloom import processor_channels as channels
from sourceloom.store import Conflict, Store, digest


@pytest.fixture
def material(tmp_path):
    store = Store(tmp_path)
    pid = store.create('processor test', budget=10)['id']
    config = {'provider': 'router', 'model': 'chatgpt-web.thinking', 'api_key': 'business-test',
              'execution_channel': 'chatgpt_web',
              'base_url': 'https://model.example/v1', 'protocol': 'responses',
              'call_timeout': 1, 'max_output_tokens': 100,
              'pricing_cny': {'input': 1, 'output': 2}}
    pack = {'prompt': 'Rewrite faithfully into Markdown.', 'source_text': 'An original sentence.',
            'attachments': [], 'requires_visual': False}
    return store, pid, {'processor_router': config}, pack


def response(body, status=200):
    return httpx.Response(status, json=body, request=httpx.Request('POST', 'https://model.example'))


def success():
    return response({'id': 'original-upstream'})


def terminal(markdown='# 完整正文\n\n阅读稿。', status='completed', **extra):
    return response({'id': 'original-upstream', 'status': status, 'output': markdown,
                     'task': {'executionChannel': 'chatgpt_web', 'chatgptWeb': {'mode': 'chat'}},
                     'route': {'provider': 'chatgpt_web'},
                     'usage': {'inputTokens': 10, 'outputTokens': 20, 'attemptCount': 1, 'retryCount': 0}, **extra})


@pytest.fixture(autouse=True)
def successful_web_query(monkeypatch):
    monkeypatch.setattr(channels.httpx, 'get', lambda *a, **kw: terminal())


def test_plain_markdown_no_planning_or_json_envelope(material, monkeypatch):
    store, pid, config, pack = material
    wires = []
    monkeypatch.setattr(channels, 'post_before_deadline', lambda url, headers, wire, deadline: wires.append(wire) or success())
    result = channels.generate(store, config, pid, pack, request_id='request-one')
    assert result['status'] == 'SUCCESS'
    assert result['markdown'].startswith('# 完整正文')
    assert len(wires) == 1 and 'text' not in wires[0] and 'response_format' not in wires[0]
    assert 'concept_ledger' not in json.dumps(wires[0])
    assert result['network_attempt_count'] == 1
    assert result['cost']['local_estimate_cny'] is None
    assert result['execution_channel'] == 'chatgpt_web'
    assert wires[0]['task']['chatgptWeb']['mode'] == 'chat'
    assert 'effort' not in wires[0]['task']
    assert channels.generate(store, config, pid, pack, request_id='request-one') == result
    assert len(wires) == 1


@pytest.mark.parametrize('failure', [httpx.ReadError('disconnected'), httpx.DecodingError('bad framing')])
def test_unknown_survives_restart_no_second_dispatch(material, monkeypatch, failure):
    store, pid, config, pack = material
    calls = []
    def send(*args):
        calls.append(args)
        raise failure
    monkeypatch.setattr(channels, 'post_before_deadline', send)
    receipt = channels.generate(store, config, pid, pack, request_id='uncertain-original')
    assert receipt['status'] == 'UNKNOWN'
    assert store.costs(pid)[0]['status'] == 'unknown'
    reopened = Store(store.root)
    assert channels.generate(reopened, config, pid, pack, request_id='uncertain-original')['status'] == 'UNKNOWN'
    assert len(calls) == 1
    changed = dict(pack, source_text='changed source')
    with pytest.raises(Conflict):
        channels.generate(reopened, config, pid, changed, request_id='uncertain-original')


@pytest.mark.parametrize('http_status, expected', [(524, 'UNKNOWN'), (403, 'KNOWN_FAILURE'), (429, 'KNOWN_FAILURE')])
def test_delivery_based_failure_classes(material, monkeypatch, http_status, expected):
    store, pid, config, pack = material
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *args: response({'error': 'failure'}, http_status))
    assert channels.generate(store, config, pid, pack)['status'] == expected


def test_connection_before_send_is_known_failure(material, monkeypatch):
    store, pid, config, pack = material
    def fail(*args):
        raise httpx.ConnectError('before send')
    monkeypatch.setattr(channels, 'post_before_deadline', fail)
    result = channels.generate(store, config, pid, pack)
    assert result['status'] == 'KNOWN_FAILURE' and result['cost']['local_estimate_cny'] == 0


def test_visual_unsupported_rejected_before_dispatch(material, monkeypatch):
    store, pid, config, pack = material
    pack['requires_visual'] = True
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *args: pytest.fail('must not send'))
    result = channels.generate(store, config, pid, pack)
    assert result['status'] == 'KNOWN_FAILURE' and result['network_attempt_count'] == 0
    assert not store.costs(pid)


def test_exact_image_bytes_stay_archived_when_web_attachment_is_unsupported(material, monkeypatch):
    store, pid, config, pack = material
    raw = b'original image bytes'
    sha = store.blob(raw)
    pack.update(requires_visual=True, attachments=[{'sha256': sha, 'mime': 'image/png', 'name': 'page1.png', 'kind': 'page'}])
    config['processor_router']['processor_supports_images'] = True
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *a: pytest.fail('must not dispatch'))
    result = channels.generate(store, config, pid, pack)
    assert result['status'] == 'KNOWN_FAILURE'
    assert result['network_attempt_count'] == 0 and store.read_blob(sha) == raw


def test_response_without_terminal_status_does_not_accept_text(material, monkeypatch):
    store, pid, config, pack = material
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *args: success())
    monkeypatch.setattr(channels.httpx, 'get', lambda *a, **kw: terminal('partial', status='running'))
    config['processor_router']['call_timeout'] = .01
    result = channels.generate(store, config, pid, pack)
    assert result['status'] == 'UNKNOWN' and result['markdown'] is None


def test_no_configured_price_is_unknown_not_free(material, monkeypatch):
    store, pid, config, pack = material
    config['processor_router'].pop('pricing_cny')
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *args: success())
    receipt = channels.generate(store, config, pid, pack)
    assert receipt['status'] == 'SUCCESS'
    assert receipt['cost']['local_estimate_cny'] is None


def test_concurrent_same_request_only_dispatches_once(material, monkeypatch):
    store, pid, config, pack = material
    started, finish = Event(), Event()
    calls = []
    def send(*args):
        calls.append(args)
        started.set()
        finish.wait(3)
        return success()
    monkeypatch.setattr(channels, 'post_before_deadline', send)
    with ThreadPoolExecutor(max_workers=2) as executor:
        original = executor.submit(channels.generate, store, config, pid, pack, request_id='same')
        assert started.wait(3)
        duplicate = executor.submit(channels.generate, store, config, pid, pack, request_id='same')
        assert duplicate.result()['status'] == 'UNKNOWN'
        finish.set()
        assert original.result()['status'] == 'SUCCESS'
    assert len(calls) == 1


def test_router_original_id_get_recovery_never_post(material, monkeypatch):
    store, pid, config, pack = material
    config['processor_router'].update(call_timeout=.01)
    posts = []
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *args: posts.append(args) or success())
    monkeypatch.setattr(channels.httpx, 'get', lambda *args, **kw: terminal(status='running'))
    receipt = channels.generate(store, config, pid, pack, channel='router', request_id='router-one')
    assert receipt['status'] == 'UNKNOWN' and receipt['upstream_id'] == 'original-upstream'
    monkeypatch.setattr(channels.httpx, 'get', lambda *args, **kw: terminal('# 完成'))
    result = channels.query_result(store, config, pid, 'router-one')
    assert result['status'] == 'SUCCESS' and result['markdown'] == '# 完成'
    assert len(posts) == 1
    assert len(store.events(pid)) == 2


def test_router_attachment_claim_cannot_enable_unsupported_route(material):
    store, pid, config, pack = material
    config['processor_router'].update(processor_supports_images=True, processor_supports_pdf=True)
    pack['requires_visual'] = True
    assert channels.capabilities(config)['router']['attachments'] is False
    result = channels.generate(store, config, pid, pack, channel='router')
    assert result['status'] == 'KNOWN_FAILURE' and result['network_attempt_count'] == 0


def test_router_poll_connect_error_cannot_prove_original_delivery_failed(material, monkeypatch):
    store, pid, config, pack = material
    config['processor_router']['provider'] = 'router'
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *args: success())
    def failed_query(*args, **kwargs):
        raise httpx.ConnectError('poll connection unavailable')
    monkeypatch.setattr(channels.httpx, 'get', failed_query)
    result = channels.generate(store, config, pid, pack, channel='router')
    assert result['status'] == 'UNKNOWN' and result['upstream_id'] == 'original-upstream'
    assert store.costs(pid)[0]['status'] == 'unknown'


def test_subscription_api_cannot_replace_saved_web_business_route(material, monkeypatch):
    store, pid, config, pack = material
    config['processor_provider'] = {'provider': 'openai-compatible', 'api_key': 'other-test',
                                     'base_url': 'https://opencode.ai/zen/go/v1'}
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *a: pytest.fail('must not dispatch'))
    receipt = channels.generate(store, config, pid, pack, channel='api', request_id='processor-independent-session')
    assert receipt['status'] == 'KNOWN_FAILURE' and receipt['network_attempt_count'] == 0
    assert not store.costs(pid)
    assert 'x-opencode-session' not in channels._headers({'api_key': 'test'})


def test_canonical_task_pack_digest_is_distinct_from_full_handoff_identity(material, monkeypatch):
    store, pid, config, pack = material
    pack['digest'] = digest(pack)
    assert digest(pack) != pack['digest']
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *args: success())
    receipt = channels.generate(store, config, pid, pack, request_id='canonical-pack')
    assert receipt['pack_digest'] == pack['digest']
    assert receipt['handoff_payload_digest'] == digest(pack)
    job = store.job(receipt['job_id'])
    assert job['task_pack_digest'] == pack['digest']
    assert job['pack_digest'] == digest(pack)
    assert channels.generate(store, config, pid, pack, request_id='canonical-pack') == receipt
    changed = dict(pack, source_text='Changed content with stale canonical digest')
    with pytest.raises(Conflict):
        channels.generate(store, config, pid, changed, request_id='canonical-pack')


@pytest.fixture(autouse=True)
def pooled_http_client_uses_synthetic_get(monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args, **kwargs): return channels.httpx.get(*args, **kwargs)
    monkeypatch.setattr(channels, '_router_poll_client', Client)
