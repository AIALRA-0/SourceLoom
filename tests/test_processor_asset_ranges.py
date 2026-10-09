"""Serve authorised immutable bytes in ranges without relaxing integrity checks."""
import os

from fastapi.testclient import TestClient

from sourceloom import processor
from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.store import Store


def fixture(tmp_path):
    store = Store(tmp_path/'data')
    project = processor.create(store, 'Synthetic range source')
    raw = b'Frozen original fact 2024.\n' * 4096
    project = processor.prepare(store, project['id'], [('source.txt', raw)])
    key = project['inventory']['originals'][0]['sha256']
    config = load_config() | dict(data_dir=str(store.root), auth_mode='local', external_worker=True)
    return store, project, raw, key, TestClient(create_app(config))


def test_ranges_preserve_exact_bytes_and_existing_integrity_checks(tmp_path, monkeypatch):
    store, project, raw, key, client = fixture(tmp_path)
    before = store.get(project['id'])
    original = Store.read_blob
    reads = []
    def checked(self, blob_key):
        reads.append(blob_key)
        return original(self, blob_key)
    monkeypatch.setattr(Store, 'read_blob', checked)
    path = '/api/processor/projects/'+project['id']+'/files/'+key
    with client:
        first = client.get(path, headers={'range':'bytes=0-65535', 'accept-encoding':'gzip'})
        assert first.status_code == 206
        assert first.content == raw[:65536]
        assert first.headers['content-range'] == f'bytes 0-65535/{len(raw)}'
        assert first.headers['accept-ranges'] == 'bytes'
        assert first.headers['content-encoding'] == 'identity'
        assert first.headers['cache-control'] == 'private, max-age=31536000, immutable'
        tail = client.get(path, headers={'range':'bytes=-123'})
        assert tail.status_code == 206 and tail.content == raw[-123:]
        whole = client.get(path+'?download=true')
        assert whole.status_code == 200 and whole.content == raw
        assert whole.headers['content-disposition'].startswith('attachment;')
        matched = client.get(path, headers={'range':'bytes=3-9', 'if-range':first.headers['etag']})
        assert matched.status_code == 206 and matched.content == raw[3:10]
        mismatch = client.get(path, headers={'range':'bytes=3-9', 'if-range':'"old-etag"'})
        assert mismatch.status_code == 200 and mismatch.content == raw
        assert client.get(path, headers={'range':f'bytes={len(raw)}-'}).status_code == 416
        assert client.get(path, headers={'range':'bytes=invalid'}).status_code == 400
    assert reads == [key] * 7
    assert store.get(project['id']) == before


def test_ranges_require_each_material_ownership_and_login(tmp_path):
    store, project, raw, key, client = fixture(tmp_path)
    other = processor.create(store, 'Another synthetic material')
    path = '/api/processor/projects/{}/files/'+key
    with client:
        assert client.get(path.format(project['id']), headers={'range':'bytes=0-9'}).status_code == 206
        assert client.get(path.format(other['id']), headers={'range':'bytes=0-9'}).status_code == 404
    config = load_config() | dict(data_dir=str(store.root), auth_mode='proxy',
                                 allowed_subject='synthetic-user', external_worker=True)
    with TestClient(create_app(config)) as anonymous:
        assert anonymous.get(path.format(project['id']), headers={'range':'bytes=0-9'}).status_code == 401


def test_changed_file_is_rejected_even_with_restored_mtime(tmp_path):
    store, project, raw, key, client = fixture(tmp_path)
    path = store.root/'blobs'/key
    url = '/api/processor/projects/'+project['id']+'/files/'+key
    with client:
        assert client.get(url, headers={'range':'bytes=0-9'}).status_code == 206
        old = path.stat()
        path.write_bytes(b'X'+raw[1:])
        os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))
        corrupt = client.get(url, headers={'range':'bytes=0-9'})
        assert corrupt.status_code == 409
        assert '原件字节已经变化' in corrupt.json()['error']
