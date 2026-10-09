"""Connection diagnostics read a parent, never import or reveal its content."""
import httpx
import pytest
from fastapi.testclient import TestClient

from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.readweave import connection_status


CONFIG = {'readweave_url': 'https://notes.example', 'readweave_token': 'synthetic-test-value',
          'readweave_parent': 'parent'}


@pytest.mark.parametrize('code,body,expected', [
    (200, {'noteId': 'parent', 'title': 'private title'}, 'connected'),
    (200, {'noteId': 'another'}, 'invalid_response'),
    (401, {}, 'unauthorized'), (403, {}, 'unauthorized'),
    (404, {}, 'parent_missing'), (500, {}, 'unavailable'),
])
def test_connection_status_does_not_publish_parent_or_write(monkeypatch, code, body, expected):
    calls = []

    def get(url, **options):
        calls.append((url, options))
        return httpx.Response(code, json=body)

    monkeypatch.setattr('sourceloom.readweave.httpx.get', get)
    assert connection_status(CONFIG) == {'status': expected}
    assert len(calls) == 1
    assert calls[0][0] == 'https://notes.example/etapi/notes/parent'
    assert calls[0][1]['follow_redirects'] is False
    assert calls[0][1]['timeout'] == 5


def test_connection_timeout_and_login_html_are_not_success(monkeypatch):
    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout('synthetic timeout')

    monkeypatch.setattr('sourceloom.readweave.httpx.get', timeout)
    assert connection_status(CONFIG) == {'status': 'timeout'}
    monkeypatch.setattr('sourceloom.readweave.httpx.get', lambda *a, **k:
                        httpx.Response(200, text='<html>login</html>',
                                       headers={'content-type': 'text/html'}))
    assert connection_status(CONFIG) == {'status': 'invalid_response'}


def test_unconfigured_diagnostics_do_not_contact_upstream(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('unconfigured connection contacted an upstream')

    monkeypatch.setattr('sourceloom.readweave.httpx.get', unexpected)
    config = load_config() | {'data_dir': str(tmp_path), 'provider': 'manual',
                             'auth_mode': 'local', 'readweave_token': '', 'readweave_url': '',
                             'readweave_parent': ''}
    with TestClient(create_app(config)) as client:
        assert client.get('/api/processor/readweave-connection').json() == {'status': 'not_configured'}
        cap = client.get('/api/processor/capabilities').json()
        assert cap['auth_mode'] == 'local'
        assert cap['readweave_target'] is None
        assert 'readweave_token' not in cap


def test_invalid_target_does_not_contact_upstream(monkeypatch):
    monkeypatch.setattr('sourceloom.readweave.httpx.get', lambda *a, **k:
                        pytest.fail('invalid target contacted an upstream'))
    assert connection_status(CONFIG | {'readweave_url': 'https://user:pass@notes.example'}) == {
        'status': 'invalid_configuration'}
