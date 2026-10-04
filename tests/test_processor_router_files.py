import base64
from copy import deepcopy
import hashlib

import httpx
import pytest

from sourceloom import processor, processor_channels as channels
from sourceloom.processor_router_files import (MAX_OUTPUT_FILE_BYTES, output_path,
                                              read_markdown_file)


def returned(markdown='# 中文成稿\r\n\r\n原始正文。\r\n', encoding='utf8', path='article.md'):
    raw = markdown.encode('utf8')
    return {'answer': 'Created the file; this answer is not the article.', 'files': [{
        'path': path, 'encoding': encoding, 'sizeBytes': len(raw),
        'sha256': hashlib.sha256(raw).hexdigest(),
        'content': markdown if encoding == 'utf8' else base64.b64encode(raw).decode('ascii')}]}


@pytest.mark.parametrize('encoding', ['utf8', 'base64'])
def test_file_bytes_and_markdown_remain_exact(encoding):
    output = returned(encoding=encoding)
    before = deepcopy(output)
    raw, markdown, receipt = read_markdown_file(output, 'article.md')
    assert raw == '# 中文成稿\r\n\r\n原始正文。\r\n'.encode('utf8')
    assert markdown.encode('utf8') == raw
    assert receipt['sha256'] == hashlib.sha256(raw).hexdigest()
    assert output == before


@pytest.mark.parametrize('path', ['', '/article.md', '../article.md', 'a/../article.md',
    'C:/article.md', 'a\\article.md', 'a//article.md', './article.md', 'a/article.md ',
    'a./article.md', 'NUL.md', 'a/COM1.md', 'a/arti\x00cle.md', 'a' * 513])
def test_invalid_paths_cannot_be_requested_or_accepted(path):
    with pytest.raises(ValueError):
        output_path(path)


@pytest.mark.parametrize('mutation', [
    lambda output: output.pop('files'),
    lambda output: output.update(files=[]),
    lambda output: output['files'].append(deepcopy(output['files'][0])),
    lambda output: output['files'][0].update(path='another.md'),
    lambda output: output['files'][0].update(sha256='0' * 64),
    lambda output: output['files'][0].update(sizeBytes=123),
    lambda output: output['files'][0].update(sizeBytes=True),
    lambda output: output['files'][0].update(encoding='unknown'),
    lambda output: output['files'][0].update(content=None),
    lambda output: output['files'][0].update(encoding='base64', content='not base64!'),
    lambda output: output['files'][0].update(path='../article.md'),
])
def test_invalid_file_cannot_fall_back_to_answer(mutation):
    output = returned()
    mutation(output)
    with pytest.raises(ValueError):
        read_markdown_file(output, 'article.md')


def test_file_output_is_not_limited_by_the_old_short_answer_size():
    markdown = '# 完整长稿\n\n' + '这是一段保留完整来源关系的正文。\n' * 8000
    raw, actual, receipt = read_markdown_file(returned(markdown, 'base64'), 'article.md')
    assert len(markdown) > 6000 * 4
    assert len(raw) < MAX_OUTPUT_FILE_BYTES
    assert actual == markdown
    assert receipt['size_bytes'] == len(raw)


def test_protocol_file_size_limit_does_not_truncate():
    with pytest.raises(ValueError, match='超过 1 MiB'):
        read_markdown_file(returned('a' * (MAX_OUTPUT_FILE_BYTES + 1)), 'article.md')


def test_base64_output_must_still_be_valid_utf8():
    raw = b'\xffnot-utf8'
    output = returned(encoding='base64')
    output['files'][0].update(content=base64.b64encode(raw).decode(), sizeBytes=len(raw),
                              sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError, match='有效 UTF-8'):
        read_markdown_file(output, 'article.md')


def test_bom_is_preserved_in_real_body_but_not_accepted_as_a_body():
    markdown = '\ufeff# 中文原字节\r\n'
    raw, actual, _ = read_markdown_file(returned(markdown), 'article.md')
    assert actual == markdown and raw == markdown.encode('utf8')
    with pytest.raises(ValueError, match='没有可用正文'):
        read_markdown_file(returned('\ufeff \r\n'), 'article.md')


@pytest.fixture
def material(tmp_path, monkeypatch):
    from sourceloom.store import Store
    store = Store(tmp_path / 'data')
    p = processor.create(store, 'Isolated Web handoff test')
    processor.prepare(store, p['id'], [('source.md', b'# Source\n\nFrozen source text.')])
    config = {'processor_router': dict(provider='router', model='chatgpt-web.thinking',
                  api_key='business-test-only', execution_channel='chatgpt_web',
                  base_url='https://router.invalid', call_timeout=1)}
    # These are isolated transport tests, not writing-policy or quality gates.
    # Keep source/resource identity real while using a compact synthetic task.
    original_pack = processor.task_pack
    def synthetic_pack(*args, **kwargs):
        pack = original_pack(*args, **kwargs)
        pack['prompt'] = 'Synthetic test: faithfully rewrite the supplied source.'
        from sourceloom.store import digest
        pack['digest'] = digest({key: value for key, value in pack.items() if key != 'digest'})
        return pack
    monkeypatch.setattr(processor, 'task_pack', synthetic_pack)
    return store, p['id'], config, processor.task_pack(store, p['id'])


def response(body):
    return httpx.Response(200, json=body, request=httpx.Request('GET', 'https://router.invalid'))


def terminal(text='# Web\n', **extra):
    return dict(id='original-task', status='completed', output={'text': text},
                task={'executionChannel': 'chatgpt_web', 'chatgptWeb': {'mode': 'chat'}},
                route={'provider': 'chatgpt_web'}, **extra)


def test_codex_file_permission_profile_is_rejected_before_request(material):
    store, pid, config, pack = material
    before = deepcopy(pack)
    route = config['processor_router'] | {'execution_channel': 'codex',
        'processor_router_output_file': 'article.md', 'processor_router_permission_preset': 'full'}
    with pytest.raises(ValueError, match='chatgpt_web'):
        channels._request(store, route, pack, 'router')
    assert pack == before
    assert channels.capabilities({'processor_router': route})['router']['output_files'] is False
    assert channels.capabilities({'processor_router': route})['router']['available'] is False


@pytest.mark.parametrize('override', [
    {'processor_router_permission_preset': 'full'},
    {'processor_router_permission_preset': 'confirm'},
    {'execution_channel': 'codex'},
    {'processor_router_output_file': '../article.md'},
])
def test_invalid_file_configuration_rejects_before_dispatch(material, monkeypatch, override):
    store, pid, config, pack = material
    config['processor_router'].update(override)
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *a: pytest.fail('Must not dispatch'))
    receipt = channels.generate(store, config, pid, pack, channel='router')
    assert receipt['status'] == 'KNOWN_FAILURE'
    assert receipt['network_attempt_count'] == 0
    assert not store.costs(pid)


def test_output_file_capability_does_not_enable_unproved_visual_input(material, monkeypatch):
    store, pid, config, pack = material
    pack['requires_visual'] = True
    config['processor_router'].update(processor_supports_pdf=True, processor_supports_images=True)
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *a: pytest.fail('Must not dispatch'))
    receipt = channels.generate(store, config, pid, pack, channel='router')
    assert receipt['status'] == 'KNOWN_FAILURE' and receipt['network_attempt_count'] == 0
    assert channels.capabilities(config)['router']['attachments'] is False


def test_web_router_output_text_remains_unchanged(material, monkeypatch):
    store, pid, config, pack = material
    captured = []
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *a: captured.append(a) or response({'id': 'original-task'}))
    monkeypatch.setattr(channels.httpx, 'get', lambda *a, **kw: response(terminal('# Web text')))
    receipt = channels.generate(store, config, pid, pack, channel='router')
    assert receipt['status'] == 'SUCCESS' and receipt['markdown'] == '# Web text'
    assert 'outputFiles' not in captured[0][2]['task']
    assert captured[0][2]['task']['permissions']['preset'] == 'restricted'
    assert captured[0][2]['task']['expectedOutput'] == 'Return only the complete Markdown article with the supplied resource markers.'


@pytest.mark.parametrize('filename', ['article.md', 'drafts/article.md', '中文稿.md'])
def test_rejected_file_delivery_does_not_change_article_or_pack(material, filename):
    store, pid, config, pack = material
    source_before = deepcopy(store.get(pid)['inventory'])
    pack_before = deepcopy(pack)
    with pytest.raises(ValueError, match='outputFiles'):
        channels._request(store, config['processor_router'] | {'processor_router_output_file': filename}, pack, 'router')
    assert store.get(pid)['inventory'] == source_before
    assert processor.task_pack(store, pid) == pack_before


def seed_saved_task(store, pid, pack, *, channel='codex', status='UNKNOWN', markdown=None):
    receipt = {'status': status, 'markdown': markdown, 'job_id': 'saved-request',
               'logical_request_id': 'saved-request', 'upstream_id': 'original-task', 'cost_status': 'unknown'}
    job = {'id': 'saved-request', 'project': pid, 'role': 'processor', 'status': 'uncertain',
           'created': 0, 'pack_digest': __import__('sourceloom.store', fromlist=['digest']).digest(pack),
           'channel': 'router', 'receipt': receipt,
           'calls': [{'upstream_id': 'original-task', 'upstream_base': 'https://router.invalid',
                      'execution_channel': channel, 'execution_mode': 'chat',
                      'router_output_file': 'article.md', 'network_attempts': []}]}
    store.put_job(job)
    return deepcopy(job)


def test_historical_missing_file_keeps_saved_unknown_without_query_or_repost(material, monkeypatch):
    store, pid, config, pack = material
    before = seed_saved_task(store, pid, pack)
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *a: pytest.fail('Must not dispatch'))
    monkeypatch.setattr(channels.httpx, 'get', lambda *a, **kw: pytest.fail('Historical Codex is local read-only'))
    receipt = channels.generate(store, config, pid, pack, channel='router', request_id='saved-request')
    again = channels.query_result(store, config, pid, 'saved-request')
    assert receipt['status'] == again['status'] == 'UNKNOWN'
    assert receipt['historical_readonly'] and receipt['markdown'] is None
    assert store.job('saved-request') == before
    assert store.get(pid)['processor']['versions'] == []


def test_unknown_web_query_uses_original_task_without_another_post(material, monkeypatch):
    store, pid, config, pack = material
    config['processor_router']['call_timeout'] = .01
    posts, gets = [], []
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *a: posts.append(a) or response({'id': 'original-task'}))
    monkeypatch.setattr(channels.httpx, 'get', lambda *a, **kw: response(terminal() | {'status': 'running'}))
    original = channels.generate(store, config, pid, pack, channel='router', request_id='file-original')
    assert original['status'] == 'UNKNOWN' and original['upstream_id'] == 'original-task'
    markdown = '# 完整新稿\r\n\r\n来源正文。\r\n'
    config['processor_router']['call_timeout'] = 1
    def completed(url, **kwargs):
        gets.append(url)
        return response(terminal(markdown, usage={'outputTokens': 9876}))
    monkeypatch.setattr(channels.httpx, 'get', completed)
    receipt = channels.query_result(store, config, pid, 'file-original')
    assert receipt['status'] == 'SUCCESS' and receipt['markdown'] == markdown
    assert store.read_blob(receipt['response_markdown_blob']) == markdown.encode('utf8')
    assert receipt['cost']['usage']['outputTokens'] == 9876
    assert len(posts) == 1 and len(gets) == 1
    assert gets[0].endswith('/api/v1/jobs/original-task')
    assert channels.query_result(store, config, pid, 'file-original') == receipt
    assert len(posts) == 1 and len(gets) == 1


@pytest.mark.parametrize('mutation', [
    lambda output: output.pop('files'),
    lambda output: output['files'][0].update(path='unexpected.md'),
    lambda output: output['files'][0].update(sha256='0' * 64),
])
def test_web_file_answer_cannot_fall_back_to_chat_article(material, monkeypatch, mutation):
    store, pid, config, pack = material
    output = returned()
    mutation(output)
    body = terminal() | {'output': output, 'usage': {'outputTokens': 9000}}
    posts = []
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *a: posts.append(a) or response({'id': 'original-task'}))
    monkeypatch.setattr(channels.httpx, 'get', lambda *a, **kw: response(body))
    receipt = channels.generate(store, config, pid, pack, channel='router', request_id='invalid-file')
    assert receipt['status'] == 'KNOWN_FAILURE' and receipt['markdown'] is None
    assert receipt['cost']['usage']['outputTokens'] == 9000
    import json
    assert json.loads(store.read_blob(store.job('invalid-file')['calls'][0]['response_blob'])) == body
    assert len(posts) == 1


def test_normal_api_query_saves_exact_long_web_text_once_and_keeps_source_identity(material, monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from sourceloom.app import create_app
    from sourceloom.config import load_config
    store, pid, route_config, pack = material
    markdown = '# 完整文件\r\n\r\n' + '这是用于合成测试的完整正文，不是质量验收样本。' * 2000
    raw = markdown.encode('utf8')
    posts, ready = [], False
    route_config['processor_router']['call_timeout'] = .01
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *a: posts.append(a) or response({'id': 'original-task'}))
    def get(*args, **kwargs):
        return response(terminal(markdown)) if ready else response(terminal() | {'status': 'running'})
    monkeypatch.setattr(channels.httpx, 'get', get)
    config = load_config()
    config.update(data_dir=str(store.root), auth_mode='local', external_worker=True, **route_config)
    path = '/api/processor/projects/' + pid
    headers = {'x-sourceloom': '1'}
    with TestClient(create_app(config)) as client:
        assert client.post(path + '/generate', json={'channel': 'router', 'request_id': 'ui-file-original'}, headers=headers).status_code == 202
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = client.get(path).json()
            if current['processor']['requests'][0]['status'] == 'UNKNOWN':
                break
            time.sleep(.01)
        assert current['processor']['requests'][0]['status'] == 'UNKNOWN'
        ready = True
        route_config['processor_router']['call_timeout'] = 1
        for _ in range(2):
            queried = client.post(path + '/requests/ui-file-original/query', headers=headers)
            assert queried.status_code == 200
        current = queried.json()
        assert len(current['processor']['versions']) == 1
        version = current['processor']['versions'][0]
        assert version['markdown'].encode('utf8') == raw
        assert version['source_digest'] == pack['source_digest']
        assert version['pack_digest'] == pack['digest']
        assert version['template_digest'] == pack['template_digest']
        assert version['request_id'] == 'ui-file-original'
        assert version['semantic_status'] == 'not_reviewed'
        assert len(posts) == 1
        assert store.read_blob(current['processor']['requests'][0]['response_markdown_blob']) == raw


@pytest.mark.parametrize('identity_fields', [
    {}, {'id': None}, {'id': 123}, {'id': 'foreign-task'},
    {'id': 'original-task', 'idempotencyKey': 'foreign-request'},
    {'id': 'original-task', 'idempotencyKey': None},
])
def test_strict_web_result_rejects_unknown_identity_and_queries_only_original(material, monkeypatch, identity_fields):
    import json
    store, pid, config, pack = material
    posts, gets = [], []
    body = terminal('# Other task\n', usage={'outputTokens': 12345})
    body.pop('id')
    body.update(identity_fields)
    monkeypatch.setattr(channels, 'post_before_deadline', lambda *a: posts.append(a) or response({'id': 'original-task'}))
    def get(url, **kwargs):
        gets.append(url)
        return response(body)
    monkeypatch.setattr(channels.httpx, 'get', get)
    receipt = channels.generate(store, config, pid, pack, channel='router', request_id='original-request')
    assert receipt['status'] == 'UNKNOWN' and receipt['markdown'] is None
    assert receipt['cost_status'] == 'unknown' and 'usage' not in receipt['cost']
    assert store.get(pid)['processor']['versions'] == []
    assert json.loads(store.read_blob(store.job('original-request')['calls'][0]['response_blob'])) == body
    assert channels.query_result(store, config, pid, 'original-request')['status'] == 'UNKNOWN'
    body.clear()
    body.update(terminal('# Original task\r\n', idempotencyKey='original-request'))
    recovered = channels.query_result(store, config, pid, 'original-request')
    assert recovered['status'] == 'SUCCESS' and recovered['markdown'] == '# Original task\r\n'
    assert len(posts) == 1 and len(gets) == 3
    assert all(url.endswith('/api/v1/jobs/original-task') for url in gets)


@pytest.fixture(autouse=True)
def pooled_http_client_uses_synthetic_get(monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args, **kwargs): return channels.httpx.get(*args, **kwargs)
    monkeypatch.setattr(channels, '_router_poll_client', Client)
