"""Reader delivery gates use fresh checks without rewriting saved versions."""
import copy

from fastapi.testclient import TestClient

from sourceloom import processor
from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.store import Store


def native_table(tmp_path):
    store = Store(tmp_path/'data')
    p = processor.create(store, 'Native table read-only checks')
    p = processor.prepare(store, p['id'], [('source.html',
        b'<article><h1>Original</h1><table><tr><th>Item</th><th>Value</th></tr>'
        b'<tr><td>row</td><td>0.768</td></tr></table></article>')])
    md = '# Result\n\n| Item | Value |\n| --- | --- |\n| row | 0.768 |\n'
    p = processor.save_result(store, p['id'], md)
    return store, p, md


def stale_check(store, pid, passed):
    def change(p):
        v = processor.active_version(p)
        v.update(mechanical_pass=passed, checks=[] if passed else [
            dict(severity='error', code='OMITTED_RESOURCE', message='Old compiler result')])
        p['processor']['checks'] = copy.deepcopy(v['checks'])
    return store.change(pid, change)


def test_reader_projection_rechecks_native_table_without_persisting(tmp_path):
    store, p, md = native_table(tmp_path)
    stale_check(store, p['id'], False)
    before = store.get(p['id'])
    vid = before['processor']['active_version']
    assert not processor.active_version(before)['mechanical_pass']
    config = load_config()
    config.update(data_dir=str(store.root), auth_mode='local', external_worker=True)
    route = '/api/processor/projects/'+p['id']
    with TestClient(create_app(config)) as client:
        response = client.get(route)
        assert response.status_code==200
        detail = response.json()
        active = processor.active_version(detail)
        assert active['mechanical_pass']
        assert not any(c['severity']=='error' for c in active['checks'])
        assert detail['processor']['checks']==active['checks']
        version = client.get(route+'/versions/'+vid).json()
        assert version==active
        assert version['id']==vid and version['markdown']==md
        assert version['digest']==processor.active_version(before)['digest']
        assert version['semantic_status']=='not_reviewed'
        assert version['representations']==processor.active_version(before)['representations']
        assert client.get(route+'/export').status_code==200
    assert store.get(p['id'])==before


def test_reader_projection_does_not_launder_old_pass_or_missing_resource(tmp_path):
    store, p, md = native_table(tmp_path)
    p = processor.save_result(store, p['id'], '# Result\n\nThe original table is absent.\n',
                              base_version=processor.active_version(p)['id'])
    stale_check(store, p['id'], True)
    before = store.get(p['id'])
    view = processor.version_view(before)
    assert not view['mechanical_pass']
    assert any(c['code']=='OMITTED_RESOURCE' for c in view['checks'])
    assert view['semantic_status']=='not_reviewed'
    assert store.get(p['id'])==before


def test_reading_old_version_uses_its_own_snapshot_without_selecting_it(tmp_path):
    store, p, md = native_table(tmp_path)
    old = copy.deepcopy(processor.active_version(p))
    p = processor.save_result(store, p['id'], '# Result\n\nNo table here.\n', base_version=old['id'])
    before = store.get(p['id'])
    assert processor.version_view(before, old['id'])['mechanical_pass']
    assert not processor.version_view(before)['mechanical_pass']
    assert before['processor']['active_version']!=old['id']
    assert store.get(p['id'])==before


def test_projection_preserves_explicit_scope_limit_on_a_non_active_version(tmp_path):
    store, p, md = native_table(tmp_path)
    old = copy.deepcopy(processor.active_version(p))
    p = processor.save_result(store, p['id'], md+'\nAdditional sentence.\n', base_version=old['id'])
    def exclude(current):
        prior = processor.active_version(current, old['id'])
        prior['scope_exclusions'] = [dict(source_ids=['excluded-part'])]
        prior['mechanical_pass'] = True
    store.change(p['id'], exclude)
    before = store.get(p['id'])
    view = processor.version_view(before, old['id'])
    assert not view['mechanical_pass']
    assert any(c['code']=='EXPLICIT_SCOPE_REDUCTION' for c in view['checks'])
    assert processor.version_view(before)['mechanical_pass']
    assert store.get(p['id'])==before


def test_projection_keeps_unverified_image_positions_unresolved(tmp_path):
    from io import BytesIO
    from PIL import Image
    raw = BytesIO()
    Image.new('RGB', (12, 12), 'black').save(raw, 'PNG')
    store = Store(tmp_path/'data')
    p = processor.create(store, 'Unverified image positions')
    p = processor.prepare(store, p['id'], [('image.png', raw.getvalue())])
    image = next(r for r in p['processor']['resources'] if r['kind']=='image')
    def repeated(current):
        next(r for r in current['processor']['resources'] if r['id']==image['id'])['placement_count']=2
    store.change(p['id'], repeated)
    p = processor.save_result(store, p['id'], '# Result\n\n'+image['marker'])
    stale_check(store, p['id'], True)
    before = store.get(p['id'])
    view = processor.version_view(before)
    assert not view['mechanical_pass']
    assert any(c['code']=='PLACEMENT_RELATION_UNVERIFIED' for c in view['checks'])
    assert view['semantic_status']=='not_reviewed'
    assert store.get(p['id'])==before


def test_returned_version_mutation_cannot_pollute_reused_read_projections(tmp_path, monkeypatch):
    store, p, md = native_table(tmp_path)
    md += '\n合成检查句。\n'
    p = processor.save_result(store, p['id'], md, base_version=processor.active_version(p)['id'])
    before = store.get(p['id'])
    vid = before['processor']['active_version']
    compile_original = processor.compile_result
    calls = []

    def counted(*args, **kwargs):
        calls.append(args[1])
        return compile_original(*args, **kwargs)

    monkeypatch.setattr(processor, 'compile_result', counted)
    config = load_config() | {'data_dir': str(store.root), 'auth_mode': 'local', 'external_worker': True}
    app = create_app(config)
    route = '/api/processor/projects/'+p['id']
    version_endpoint = next(r.endpoint for r in app.routes
                            if getattr(r, 'path', '') == '/api/processor/projects/{pid}/versions/{vid}')
    with TestClient(app) as client:
        full = client.get(route).json()
        reading = client.get(route+'?reading=true').json()
        version = client.get(route+'/versions/'+vid).json()
        preview = client.get(route+'/preview').content
        assert version['checks'] and '0.768' in version['markdown']
        assert full['processor']['resources']
        assert len(calls) == 1

        # Mutate the actual route's server-side return, before JSON serialization:
        # mutating client-decoded JSON alone would not exercise alias isolation.
        returned = version_endpoint(p['id'], vid)
        returned['checks'][0]['message'] = 'Caller mutation must remain private'
        returned['checks'].append({'severity': 'error', 'code': 'CALLER_ONLY'})
        returned['representations'].append({'source_ids': ['caller-only']})
        returned['source_map'].clear()
        assert version_endpoint(p['id'], vid) == version
        assert client.get(route).json() == full
        assert client.get(route+'?reading=true').json() == reading
        assert client.get(route+'/versions/'+vid).json() == version
        assert client.get(route+'/preview').content == preview
        assert len(calls) == 1
        assert store.get(p['id']) == before

        # A real saved version change still invalidates the borrowed compilation.
        response = client.post(route+'/result', json={
            'markdown': md+'\nNew synthetic version.\n', 'base_version': vid},
            headers={'origin': 'http://testserver', 'x-sourceloom': '1'})
        assert response.status_code == 200
        new_version_calls = len(calls)
        assert new_version_calls > 1 and calls[-1] == md+'\nNew synthetic version.\n'
        fresh = client.get(route+'?reading=true').json()['reading_version']
        assert fresh['id'] != vid and '0.768' in fresh['markdown']
        assert fresh['representations'] == version['representations']
        assert not any(c.get('code') == 'CALLER_ONLY' for c in fresh['checks'])
        assert len(calls) == new_version_calls
        assert client.get(route+'/versions/'+vid).json() == version
