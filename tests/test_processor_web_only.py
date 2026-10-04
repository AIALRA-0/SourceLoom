"""Synthetic authorization and polling tests. No real upstream model executes."""

from copy import deepcopy
import json

import httpx
import pytest

from sourceloom import processor_channels as channels
from sourceloom.store import Store, digest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path)
    pid = store.create('Synthetic Web-only boundary', budget=10)['id']
    config = {'processor_router': {'provider': 'router', 'model': 'chatgpt-web.thinking',
        'api_key': 'designated-business-test-key', 'base_url': 'https://router.invalid',
        'execution_channel': 'chatgpt_web', 'call_timeout': 30, 'max_output_tokens': 100}}
    pack = {'prompt': 'Faithfully rewrite the complete source.', 'source_text': 'Frozen original.',
            'requires_visual': False, 'attachments': []}
    posts, clients = [], []
    def post(url, headers, wire, deadline):
        posts.append({'url': url, 'headers': headers, 'wire': wire})
        return response({'id': 'upstream-one'})
    class Client:
        def __init__(self, **kwargs): clients.append(self)
        def __enter__(self): return self
        def __exit__(self, *args): self.closed = True
        def get(self, *args, **kwargs): return channels.httpx.get(*args, **kwargs)
    monkeypatch.setattr(channels, 'post_before_deadline', post)
    monkeypatch.setattr(channels, '_router_poll_client', Client)
    monkeypatch.setattr(channels.httpx, 'get', lambda *a, **kw: response(terminal()))
    return store, pid, config, pack, posts, clients


def response(body, status=200, headers=None):
    return httpx.Response(status, json=body, headers=headers,
                          request=httpx.Request('GET', 'https://router.invalid'))


def terminal(**extra):
    return {'id': 'upstream-one', 'idempotencyKey': 'logical-one', 'status': 'succeeded',
            'task': {'executionChannel': 'chatgpt_web', 'chatgptWeb': {'mode': 'chat'}},
            'route': {'provider': 'chatgpt_web'}, 'output': {'text': '# Synthetic complete output'},
            'usage': {'attemptCount': 1, 'retryCount': 0, 'outputTokens': 10}, **extra}


def run(setup, **kwargs):
    store, pid, config, pack, _, _ = setup
    return channels.generate(store, config, pid, pack, request_id='logical-one', **kwargs)


@pytest.mark.parametrize('override', [
    {'execution_channel': 'codex'}, {'execution_channel': None},
    {'model': 'gpt-6-sol'}, {'model': 'chatgpt-web.pro', 'chatgpt_web_mode': 'agent'},
    {'processor_router_permission_preset': 'full'},
    {'processor_router_output_file': 'article.md'}, {'provider': 'openai-compatible'},
    {'api_key': ''},
])
def test_forbidden_configuration_cannot_reach_upstream(setup, override):
    store, pid, config, pack, posts, clients = setup
    config['processor_router'].update(override)
    receipt = run(setup)
    assert receipt['status'] == 'KNOWN_FAILURE'
    assert receipt['network_attempt_count'] == 0 and not posts and not clients
    assert receipt['cost']['local_estimate_cny'] == 0
    assert not store.costs(pid)
    assert not channels.capabilities(config)['router']['available']


@pytest.mark.parametrize('channel', ['api', 'codex', 'runner', 'cli'])
def test_other_channel_does_not_dispatch(setup, channel):
    store, pid, config, pack, posts, _ = setup
    if channel == 'api':
        assert run(setup, channel=channel)['status'] == 'KNOWN_FAILURE'
    else:
        with pytest.raises(ValueError): run(setup, channel=channel)
    assert not posts and not store.costs(pid)


def test_no_implicit_top_level_or_other_provider_credential(setup):
    store, pid, config, pack, posts, _ = setup
    config.update(provider='router', api_key='other-admin-test', model='chatgpt-web.thinking',
                  base_url='https://router.invalid', execution_channel='chatgpt_web',
                  processor_provider={'api_key': 'different-key'})
    config['processor_router'].pop('api_key')
    receipt = run(setup)
    assert receipt['status'] == 'KNOWN_FAILURE' and not posts
    assert 'api_key' not in channels._route(config, 'router')


def test_web_request_uses_only_explicit_key_and_web_parameters(setup):
    _, _, config, pack, posts, _ = setup
    config.update(api_key='legacy-admin-test', effort='xhigh')
    config['processor_router'].update(thinking_depth='extended', effort='xhigh')
    before = deepcopy(pack)
    receipt = run(setup)
    assert receipt['status'] == 'SUCCESS'
    assert posts[0]['headers']['Authorization'] == 'Bearer designated-business-test-key'
    task = posts[0]['wire']['task']
    assert task['executionChannel'] == 'chatgpt_web' and task['chatgptWeb']['mode'] == 'chat'
    assert task['chatgptWeb']['thinkingDepth'] == 'extended'
    assert 'effort' not in task and 'outputFiles' not in task
    assert task['permissions']['preset'] == 'restricted' and pack == before
    cap = channels.capabilities(config)
    assert not cap['api']['available']
    assert cap['channels'][1]['execution_channel'] == 'chatgpt_web'
    assert cap['channels'][1]['mode'] == 'chat'


@pytest.mark.parametrize('requires_visual,source', [(True, 'Complete original'), (False, 'X' * 100001)], ids=['visual-unsupported', 'long-input-unsupported'])
def test_unsupported_input_stays_complete_and_undispatched(setup, requires_visual, source):
    store, _, _, pack, posts, _ = setup
    pack.update(requires_visual=requires_visual, source_text=source)
    before = deepcopy(pack)
    receipt = run(setup)
    assert receipt['status'] == 'KNOWN_FAILURE' and receipt['network_attempt_count'] == 0
    assert pack == before and not posts


@pytest.mark.parametrize('change', [
    {'task': {'executionChannel': 'codex', 'chatgptWeb': {'mode': 'chat'}}},
    {'task': {'executionChannel': 'chatgpt_web', 'chatgptWeb': {'mode': 'agent'}}},
    {'route': {'provider': 'codex'}},
])
def test_actual_unauthorized_channel_is_unknown_and_halts_next_submission(setup, monkeypatch, change):
    store, pid, config, pack, posts, _ = setup
    body = terminal(**change)
    monkeypatch.setattr(channels.httpx, 'get', lambda *a, **kw: response(body))
    result = run(setup)
    assert result['status'] == 'UNKNOWN' and result['markdown'] is None
    assert 'usage' not in result['cost']
    call = store.job('logical-one')['calls'][0]
    assert call['route_authorization_mismatch']
    assert json.loads(store.read_blob(call['response_blob'])) == body
    second = channels.generate(store, config, pid, pack, request_id='independent-two')
    assert second['status'] == 'KNOWN_FAILURE' and len(posts) == 1
    assert second['network_attempt_count'] == 0


@pytest.mark.parametrize('change', [{'task': {}}, {'route': None}, {'route': {}}, {'id': 'another-job'}])
def test_missing_execution_or_identity_evidence_cannot_be_a_web_success(setup, monkeypatch, change):
    monkeypatch.setattr(channels.httpx, 'get', lambda *a, **kw: response(terminal(**change)))
    result = run(setup)
    assert result['status'] == 'UNKNOWN' and result['markdown'] is None
    assert result['cost_status'] == 'unknown'


@pytest.mark.parametrize('status,error_code,expected', [
    ('failed', 'chatgpt_login_required', 'KNOWN_FAILURE'),
    ('cancelled', 'approval_denied', 'KNOWN_FAILURE'),
    ('failed', 'chatgpt_decoding_uncertain', 'UNKNOWN'),
    ('timed_out', 'deadline_reached', 'UNKNOWN'),
    ('expired', None, 'UNKNOWN'),
])
def test_terminal_failure_and_transport_uncertainty_are_distinct(setup, monkeypatch, status, error_code, expected):
    monkeypatch.setattr(channels.httpx, 'get', lambda *a, **kw: response(terminal(status=status, errorCode=error_code)))
    result = run(setup)
    assert result['status'] == expected and result['markdown'] is None
    assert result['network_attempt_count'] == 1 and result['cost']['local_estimate_cny'] is None


def test_historical_codex_success_is_readonly_without_dispatch_or_original_mutation(setup, monkeypatch):
    store, pid, config, pack, posts, _ = setup
    original = {'id': 'logical-one', 'project': pid, 'role': 'processor', 'status': 'completed',
        'created': 0, 'pack_digest': digest(pack), 'channel': 'router',
        'calls': [{'execution_channel': 'codex', 'upstream_id': 'old-task'}],
        'receipt': {'status': 'SUCCESS', 'markdown': '# Accepted historical article'}}
    store.put_job(original)
    monkeypatch.setattr(channels.httpx, 'get', lambda *a, **kw: pytest.fail('No historical upstream query'))
    result = run(setup)
    assert result['status'] == 'SUCCESS' and result['historical_readonly']
    assert channels.query_result(store, config, pid, 'logical-one') == result
    assert not channels.receipt_is_web_chat(original)
    assert store.job('logical-one') == original and not posts


def test_one_pool_adaptive_polling_and_repeated_response_do_not_rewrite_job(setup, monkeypatch):
    store, pid, config, pack, posts, clients = setup
    clock = [1000.0]
    delays = []
    monkeypatch.setattr(channels.time, 'time', lambda: clock[0])
    monkeypatch.setattr(channels.time, 'perf_counter', lambda: clock[0])
    def sleep(seconds): delays.append(seconds); clock[0] += seconds
    monkeypatch.setattr(channels.time, 'sleep', sleep)
    bodies = [terminal(status='running')] * 3 + [terminal()]
    gets = []
    def get(url, **kwargs): gets.append(url); return response(bodies.pop(0))
    monkeypatch.setattr(channels.httpx, 'get', get)
    put = store.put_job
    writes = []
    def tracked(job):
        if job.get('calls') and any(a['kind'] == 'result_query' for a in job['calls'][-1].get('network_attempts', [])):
            writes.append(deepcopy(job))
        return put(job)
    monkeypatch.setattr(store, 'put_job', tracked)
    receipt = run(setup)
    assert receipt['status'] == 'SUCCESS' and delays == [1, 5, 10]
    assert len(posts) == 1 and len(clients) == 1 and clients[0].closed
    assert receipt['result_query_count'] == 4 and len(writes) == 3
    attempts = store.job('logical-one')['calls'][0]['network_attempts']
    assert len(attempts) == 5
    assert all('finished_at' in a and 'wall_time_seconds' in a and a['http_status'] == 200 for a in attempts)
    assert receipt['upstream_attempt_count'] == 1 and receipt['upstream_retry_count'] == 0


def test_transient_gets_honor_retry_after_without_another_post(setup, monkeypatch):
    store, _, config, _, posts, _ = setup
    clock, delays = [1000.0], []
    monkeypatch.setattr(channels.time, 'time', lambda: clock[0])
    monkeypatch.setattr(channels.time, 'perf_counter', lambda: clock[0])
    def sleep(seconds): delays.append(seconds); clock[0] += seconds
    monkeypatch.setattr(channels.time, 'sleep', sleep)
    answers = [response({'error': 'limited'}, 429, {'Retry-After': '7'}),
               response({'error': 'unavailable'}, 503), response(terminal())]
    monkeypatch.setattr(channels.httpx, 'get', lambda *a, **kw: answers.pop(0))
    receipt = run(setup)
    assert receipt['status'] == 'SUCCESS' and delays == [7, 2]
    assert receipt['network_attempt_count'] == len(posts) == 1
    assert receipt['result_query_count'] == 3
    attempts = store.job('logical-one')['calls'][0]['network_attempts']
    assert [a.get('http_status') for a in attempts] == [200, 429, 503, 200]
    assert attempts[1]['error_type'] == 'HTTPStatusError'


def test_result_queries_are_bounded_even_with_long_deadline(setup, monkeypatch):
    store, _, config, _, posts, _ = setup
    config['processor_router'].update(call_timeout=1000, processor_router_result_query_limit=2)
    monkeypatch.setattr(channels.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(channels.httpx, 'get', lambda *a, **kw: response(terminal(status='running')))
    receipt = run(setup)
    assert receipt['status'] == 'UNKNOWN' and receipt['result_query_count'] == 2
    assert len(posts) == 1 and not receipt.get('historical_readonly')


def test_post_failure_is_never_retried(setup, monkeypatch):
    store, _, _, _, posts, _ = setup
    attempts = []
    def fail(*args): attempts.append(args); raise httpx.DecodingError('uncertain submission')
    monkeypatch.setattr(channels, 'post_before_deadline', fail)
    receipt = run(setup)
    assert receipt['status'] == 'UNKNOWN' and len(attempts) == 1
    call = store.job('logical-one')['calls'][0]
    assert call['network_attempts'][0]['error_type'] == 'DecodingError'
    assert run(setup) == receipt and len(attempts) == 1
