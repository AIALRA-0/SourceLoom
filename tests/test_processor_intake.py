"""Normal upload endpoints must acknowledge bytes before expensive preparation."""
import json
import time
import threading
import zipfile
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from sourceloom import processor
from sourceloom.app import create_app
from sourceloom.processor_tree import ProcessorTree
from sourceloom.store import identity


@pytest.fixture
def client(tmp_path):
    app = create_app(dict(data_dir=str(tmp_path), auth_mode='local', provider='manual', external_worker=True))
    with TestClient(app, headers={'X-SourceLoom': '1'}) as c:
        yield c, app.state.processor_library


def create(c, **kw):
    r = c.post('/api/processor/projects', json={'title': 'Intake', **kw})
    assert r.status_code == 200, r.text
    return r.json()['id']


def wait(c, pid):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        p = c.get(f'/api/processor/projects/{pid}/progress').json()
        if p['phase'] in ('complete', 'failed'):
            return p
        time.sleep(.01)
    raise AssertionError('Preparation did not reach a terminal state')


def upload(c, pid):
    return c.post(f'/api/processor/projects/{pid}/upload?background=true',
                  files=[('files', ('source.md', b'# Complete source\n\nPreserve every byte.', 'text/markdown'))])


def test_background_receipt_progress_and_unrelated_reads_do_not_wait_for_parser(client, monkeypatch):
    c, store = client
    entered, release = threading.Event(), threading.Event()
    real = processor.prepare
    def delayed(*a, **kw):
        entered.set()
        assert release.wait(5)
        return real(*a, **kw)
    monkeypatch.setattr(processor, 'prepare', delayed)
    pid = create(c)
    try:
        start = time.monotonic()
        r = upload(c, pid)
        assert r.status_code == 202 and time.monotonic() - start < 1
        assert len(r.content) < 1024 and entered.wait(1)
        assert c.get(f'/api/processor/projects/{pid}/progress').json()['phase'] == 'queued'
        assert c.get('/health').status_code == 200
        assert c.get('/api/processor/projects').status_code == 200
        assert upload(c, pid).status_code == 409
        assert c.post(f'/api/processor/projects/{pid}/upload/resume').json()['reused'] is True
    finally:
        release.set()
    assert wait(c, pid)['phase'] == 'complete'
    original = store.get(pid)['inventory']['originals'][0]
    assert store.read_blob(original['sha256']) == b'# Complete source\n\nPreserve every byte.'


def test_failure_resume_reuses_the_saved_original_and_project(client, monkeypatch):
    c, store = client
    real = processor.prepare
    monkeypatch.setattr(processor, 'prepare', lambda *a, **kw: (_ for _ in ()).throw(ValueError('controlled parse failure')))
    pid = create(c)
    assert upload(c, pid).status_code == 202
    failed = wait(c, pid)
    assert failed['phase'] == 'failed' and failed['resumable'] and 'controlled' in failed['detail']
    saved = store.get(pid)['processor']['intake_uploads']
    monkeypatch.setattr(processor, 'prepare', real)
    assert c.post(f'/api/processor/projects/{pid}/upload/resume').status_code == 202
    assert wait(c, pid)['phase'] == 'complete'
    assert store.get(pid)['processor']['intake_uploads'] == saved
    assert len(store.list()) == 1


def test_create_directly_in_nested_folder_and_reject_invalid_without_orphan(client):
    c, store = client
    tree = ProcessorTree(store)
    parent = tree.actions(dict(action='create_folder', title='Parent', request_id=identity()))['node']
    child = tree.actions(dict(action='create_folder', title='Child', parent_id=parent['id'], request_id=identity()))['node']
    pid = create(c, parent_id=child['id'])
    assert store.get(pid)['folder'] == child['id']
    assert tree.tree(parent_id=child['id'])['nodes'][0]['id'] == pid
    count = len(store.list())
    assert c.post('/api/processor/projects', json={'title': 'bad', 'parent_id': 'absent'}).status_code == 409
    assert c.post('/api/processor/projects', json={'title': 'Intake', 'parent_id': child['id']}).status_code == 409
    assert len(store.list()) == count


def test_pack_download_index_is_the_manifest_identity_and_stable_for_same_task(client):
    c, store = client
    pid = create(c)
    assert upload(c, pid).status_code == 202
    assert wait(c, pid)['phase'] == 'complete'
    pack = c.get(f'/api/processor/projects/{pid}/pack').json()
    r = c.get(f'/api/processor/projects/{pid}/pack.zip')
    assert r.status_code == 200
    assert r.headers['content-disposition'] == f'attachment; filename="SourceLoom-{pack["digest"]}.zip"'
    with zipfile.ZipFile(BytesIO(r.content)) as z:
        manifest = json.loads(z.read('manifest.json'))
        assert manifest['digest'] == pack['digest']
    assert c.get(f'/api/processor/projects/{pid}/pack.zip').headers['content-disposition'] == r.headers['content-disposition']
    other = create(c, title='Different')
    assert upload(c, other).status_code == 202
    assert wait(c, other)['phase'] == 'complete'
    assert c.get(f'/api/processor/projects/{other}/pack').json()['digest'] != pack['digest']


def test_bounded_queue_and_trashed_task_are_not_silently_resurrected(client, monkeypatch):
    c, store = client
    release = threading.Event()
    real = processor.prepare
    def delayed(*a, **kw):
        assert release.wait(5)
        return real(*a, **kw)
    monkeypatch.setattr(processor, 'prepare', delayed)
    ids = [create(c, title=f'Task {i}') for i in range(4)]
    try:
        assert [upload(c, p).status_code for p in ids] == [202, 202, 202, 409]
        store.change(ids[0], lambda p: p.update(trashed=True))
    finally:
        release.set()
    assert wait(c, ids[0])['phase'] == 'failed'
    assert store.get(ids[0])['trashed'] and not store.get(ids[0]).get('inventory')
    for pid in ids[1:3]:
        assert wait(c, pid)['phase'] == 'complete'


def test_restart_marks_interrupted_upload_resumable_without_sending_a_model(client):
    c, store = client
    from sourceloom.processor_intake import MaterialIntake
    pid = create(c)
    sha = store.blob(b'# saved source')
    store.change(pid, lambda p: p['processor'].update(intake_uploads=[dict(name='saved.md', sha256=sha, size=14)], intake_progress={'phase': 'pdf_pages'}))
    worker = MaterialIntake(store)
    try:
        assert store.get(pid)['processor']['intake_progress']['resumable']
        assert store.get(pid)['processor']['requests'] == []
        worker.submit(pid, resume=True)
        assert wait(c, pid)['phase'] == 'complete'
    finally:
        worker.close()
