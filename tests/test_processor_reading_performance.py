"""Reuse identical checked projections without changing persisted drafts."""
import copy

from fastapi.testclient import TestClient

from sourceloom import processor
from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.processor_api import _ReadingDerivatives
from sourceloom.store import Store


def test_parent_reader_defers_images_without_changing_independent_preview_or_export(tmp_path):
    import io
    import zipfile
    from PIL import Image
    from bs4 import BeautifulSoup
    image = io.BytesIO()
    Image.new('RGB', (30, 20), 'blue').save(image, format='PNG')
    store = Store(tmp_path/'data')
    p = processor.create(store, 'Synthetic image reading')
    p = processor.prepare(store, p['id'], [('figure.png', image.getvalue()), ('source.txt', b'A synthetic fact.')])
    pack = processor.task_pack(store, p['id'])
    p = processor.save_result(store, p['id'], '# 合成材料\n\n'+
                              '\n\n'.join(r['marker'] for r in pack['resources']))
    before = copy.deepcopy(store.get(p['id']))
    config = load_config() | {'data_dir':str(store.root), 'auth_mode':'local', 'external_worker':True}
    route = '/api/processor/projects/'+p['id']
    with TestClient(create_app(config)) as client:
        standalone = client.get(route+'/preview')
        deferred = client.get(route+'/preview?reader=true')
        direct = BeautifulSoup(standalone.text, 'html.parser').select('img')
        waiting = BeautifulSoup(deferred.text, 'html.parser').select('img')
        assert direct and len(waiting) == len(direct)
        assert [i['data-reader-src'] for i in waiting] == [i['src'] for i in direct]
        assert all('src' not in i.attrs and i.get('width') and i.get('height') for i in waiting)
        assert 'allow-scripts' not in deferred.headers['content-security-policy']
        assert client.get(route+'/preview').text == standalone.text
        with zipfile.ZipFile(io.BytesIO(client.get(route+'/export').content)) as z:
            html = z.read('material.html').decode()
            assert 'data-reader-src' not in html
            assert BeautifulSoup(html, 'html.parser').select('img[src]')
    assert store.get(p['id']) == before


def test_bounded_cache_copies_and_evicts():
    cache = _ReadingDerivatives(limit=2)
    first = cache.get('a', lambda: {'rows': [1]})
    first['rows'].append(2)
    assert cache.get('a', lambda: None) == {'rows': [1]}
    cache.get('b', lambda: 2)
    cache.get('c', lambda: 3)
    assert list(cache.values) == ['b', 'c']


def test_active_reader_reuses_validation_and_invalidates_new_inputs(tmp_path, monkeypatch):
    store = Store(tmp_path/'data')
    project = processor.create(store, 'Frozen source')
    project = processor.prepare(store, project['id'], [('source.txt', b'Original fact.')])
    project = processor.save_result(store, project['id'], '# Old\n\nOld version retained.')
    project = processor.save_result(store, project['id'], '# Current\n\nCurrent fact.')
    before = store.get(project['id'])
    compile_original = processor.compile_result
    calls = []
    def counted(*args, **kwargs):
        calls.append(args[1])
        return compile_original(*args, **kwargs)
    monkeypatch.setattr(processor, 'compile_result', counted)
    config = load_config() | {'data_dir':str(store.root), 'auth_mode':'local', 'external_worker':True}
    route = '/api/processor/projects/'+project['id']
    vid = project['processor']['active_version']
    with TestClient(create_app(config)) as client:
        view = client.get(route+'?reading=true').json()
        assert view['reading_version']['markdown'] == '# Current\n\nCurrent fact.'
        assert all('markdown' not in row for row in view['processor']['versions'])
        assert 'objects' not in view['inventory']
        assert view['processor']['source_text'] == before['processor']['source_text']
        assert client.get(route+'/versions/'+vid).json() == view['reading_version']
        assert client.get(route+'/preview?version='+vid).status_code == 200
        assert len(calls) == 1
        assert store.get(project['id']) == before
        # Any input affecting checks must invalidate reuse; old receipts remain saved.
        store.change(project['id'], lambda p: processor.active_version(p).setdefault('scope_exclusions', []).append({'source_ids':['x']}))
        fresh = client.get(route+'?reading=true').json()['reading_version']
        assert any(c['code']=='EXPLICIT_SCOPE_REDUCTION' for c in fresh['checks'])
        assert len(calls) == 2


def test_detail_receipt_enrichment_cannot_mutate_cached_read_snapshot(tmp_path, monkeypatch):
    store = Store(tmp_path/'data')
    p = processor.create(store, 'Frozen source')
    p = processor.prepare(store, p['id'], [('source.txt', b'Original fact.')])
    store.change(p['id'], lambda value:value['processor']['requests'].append({'id':'r1','status':'SUCCESS','channel':'manual'}))
    p = processor.save_result(store, p['id'], '# Current\n\nOriginal fact.')
    from sourceloom.processor_library import LibraryStore
    monkeypatch.setattr(LibraryStore, 'jobs', lambda self,pid:[{'id':'r1','status':'SUCCESS'}])
    config = load_config() | {'data_dir':str(store.root), 'auth_mode':'local', 'external_worker':True}
    route = '/api/processor/projects/'+p['id']
    before = store.get(p['id'])
    with TestClient(create_app(config)) as client:
        assert 'call_receipt' in client.get(route).json()['processor']['requests'][0]
        assert 'call_receipt' not in client.get(route+'?reading=true').json()['processor']['requests'][0]
    assert store.get(p['id']) == before


def test_full_detail_skips_duplicate_encoder_and_preserves_complete_saved_fields(tmp_path, monkeypatch):
    import io
    import fastapi.routing
    from PIL import Image
    image = io.BytesIO()
    Image.new('RGB', (30, 20), 'blue').save(image, format='PNG')
    store = Store(tmp_path/'data')
    p = processor.create(store, 'Complete synthetic detail')
    p = processor.prepare(store, p['id'], [('source.txt', b'Original fact.'), ('figure.png', image.getvalue())])
    markers = '\n\n'.join(r['marker'] for r in processor.task_pack(store, p['id'])['resources'])
    p = processor.save_result(store, p['id'], '# First\n\nOriginal fact.\n\n'+markers)
    p = processor.save_result(store, p['id'], '# Second\n\nOriginal fact.\n\n'+markers)
    config = load_config() | {'data_dir':str(store.root), 'auth_mode':'local', 'external_worker':True}
    before = store.get(p['id'])
    calls = []
    original = fastapi.routing.jsonable_encoder
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(fastapi.routing, 'jsonable_encoder', counted)
    with TestClient(create_app(config)) as client:
        result = client.get('/api/processor/projects/'+p['id'])
        assert result.status_code == 200 and result.headers['cache-control'] == 'no-store'
        value = result.json()
        assert not calls  # No Python recursive encoder before JSON serialization.
        assert value['inventory'] == before['inventory']
        assert value['processor']['versions'] == before['processor']['versions']
        assert value['processor']['source_text'] == before['processor']['source_text']
        for actual, expected in zip(value['processor']['resources'], before['processor']['resources']):
            assert {k:actual[k] for k in expected} == expected
            assert actual['url'].endswith('/files/'+expected['sha256'])
        assert value['costs'] == []
        assert store.get(p['id']) == before


def test_full_detail_observes_live_receipts_costs_and_new_saved_version(tmp_path):
    import time
    store = Store(tmp_path/'data')
    p = processor.create(store, 'Live synthetic detail')
    p = processor.prepare(store, p['id'], [('source.txt', b'Original fact.')])
    p = processor.save_result(store, p['id'], '# First\n\nOriginal fact.')
    store.change(p['id'], lambda value:value['processor']['requests'].append(
        {'id':'r1','status':'UNKNOWN','channel':'router'}))
    job = dict(id='r1', project=p['id'], role='processor', channel='router',
               status='uncertain', created=time.time(), receipt={'status':'UNKNOWN'})
    store.put_job(job)
    store.reserve(p['id'], 'cost-one', .1, {'role':'processor', 'channel':'router'})
    config = load_config() | {'data_dir':str(store.root), 'auth_mode':'local', 'external_worker':True}
    route = '/api/processor/projects/'+p['id']
    with TestClient(create_app(config)) as client:
        first = client.get(route).json()
        assert first['processor']['requests'][0]['call_receipt']['receipt']['status'] == 'UNKNOWN'
        assert first['costs'][0]['status'] == 'reserved'
        frozen_project = store.get(p['id'])
        # Job/cost changes do not change the project snapshot; they must still
        # be read afresh, rather than hidden by a cached full response.
        job.update(status='failed', receipt={'status':'KNOWN_FAILURE', 'error':'synthetic refusal'})
        store.put_job(job)
        store.settle('cost-one', .05, {'status':'KNOWN_FAILURE'})
        second = client.get(route).json()
        assert second['processor']['requests'][0]['call_receipt']['receipt']['status'] == 'KNOWN_FAILURE'
        assert second['costs'][0]['status'] == 'settled' and second['costs'][0]['actual'] == .05
        assert store.get(p['id']) == frozen_project
        saved = client.post(route+'/result', json={'markdown':'# Second\n\nOriginal fact.'},
                            headers={'x-sourceloom':'1'})
        assert saved.status_code == 200
        current = client.get(route).json()
        assert len(current['processor']['versions']) == 2
        assert processor.active_version(current)['markdown'] == '# Second\n\nOriginal fact.'
        assert current['processor']['versions'][0]['markdown'] == '# First\n\nOriginal fact.'


def test_compressed_long_detail_preserves_every_decoded_byte(tmp_path):
    import gzip
    store = Store(tmp_path/'data')
    p = processor.create(store, 'Synthetic long saved material')
    raw = ('Original text with numbers 2024, 1.25 and source relationships.\n' * 100).encode()
    p = processor.prepare(store, p['id'], [('source.txt', raw)])
    p = processor.save_result(store, p['id'], '# Saved\n\n'+raw.decode())
    config = load_config() | {'data_dir':str(store.root), 'auth_mode':'local', 'external_worker':True}
    route = '/api/processor/projects/'+p['id']
    with TestClient(create_app(config)) as client:
        identity = client.get(route, headers={'accept-encoding':'identity'})
        with client.stream('GET', route, headers={'accept-encoding':'gzip'}) as compressed:
            wire = b''.join(compressed.iter_raw())
            assert compressed.status_code == identity.status_code == 200
            assert compressed.headers['content-encoding'] == 'gzip'
            assert compressed.headers['cache-control'] == identity.headers['cache-control'] == 'no-store'
        assert gzip.decompress(wire) == identity.content
        assert identity.json()['processor']['versions'][0]['markdown'] == '# Saved\n\n'+raw.decode()
        assert store.get(p['id']) == p


def test_delivery_status_reuse_checks_every_receipt_byte(tmp_path, monkeypatch):
    import os
    from sourceloom import readweave
    store = Store(tmp_path/'data')
    p = processor.create(store, 'Frozen source')
    calls = []
    def check(store, config, pid):
        calls.append(pid)
        return {'status':'not_submitted', 'attempt':len(calls)}
    monkeypatch.setattr(readweave, 'import_status', check)
    config = load_config() | {'data_dir':str(store.root), 'auth_mode':'local', 'external_worker':True}
    route = '/api/processor/projects/'+p['id']+'/readweave-status'
    with TestClient(create_app(config)) as client:
        assert client.get(route).json()['attempt'] == 1
        assert client.get(route).json()['attempt'] == 1
        path = store.root/'readweave'/(p['id']+'-receipt.json')
        path.parent.mkdir(exist_ok=True)
        path.write_text('first', encoding='utf8')
        assert client.get(route).json()['attempt'] == 2
        stamp = path.stat()
        path.write_text('other', encoding='utf8')
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert client.get(route).json()['attempt'] == 3
        store.change(p['id'], lambda p:p.update(title='Changed management title'))
        assert client.get(route).json()['attempt'] == 4
        config['readweave_parent'] = 'different-configured-parent'
        assert client.get(route).json()['attempt'] == 5


def test_frozen_asset_cache_policy_and_trash_readonly_projection(tmp_path):
    store = Store(tmp_path/'data')
    project = processor.create(store, 'Frozen source')
    project = processor.prepare(store, project['id'], [('source.txt', b'Original bytes.')])
    project = processor.save_result(store, project['id'], '# Current\n\nOriginal fact.')
    sha = project['inventory']['originals'][0]['sha256']
    config = load_config() | {'data_dir':str(store.root), 'auth_mode':'local', 'external_worker':True}
    route = '/api/processor/projects/'+project['id']
    store.change(project['id'], lambda p: p.update(trashed=True))
    before = copy.deepcopy(store.get(project['id']))
    with TestClient(create_app(config)) as client:
        view = client.get(route+'?reading=true').json()
        assert view['trashed'] and view['library']['readonly']
        assert not client.get('/api/processor/projects').json()
        file = client.get(route+'/files/'+sha)
        assert file.content == b'Original bytes.'
        assert file.headers['cache-control'] == 'private, max-age=31536000, immutable'
        assert file.headers['content-encoding'] == 'identity'
        assert client.get(route+'?reading=true').headers['cache-control'] == 'no-store'
        assert client.get(route+'/preview').headers['cache-control'] == 'no-store'
        assert client.post(route+'/result', json={'markdown':'changed'}, headers={'x-sourceloom':'1'}).status_code == 409
    assert store.get(project['id']) == before
