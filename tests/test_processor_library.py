"""Personal cleanup and sample isolation must preserve real data relationships."""
import copy
import json

from fastapi.testclient import TestClient
import pytest

from sourceloom import processor
from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.processor_library import LibraryStore, archived, set_archive
from sourceloom.store import Conflict, Store


def sample_catalog(tmp_path):
    source = Store(tmp_path / 'source')
    p = processor.create(source, 'Real complete sample')
    p = processor.prepare(source, p['id'], [('source.txt', b'Full real source paragraph.')])
    p = processor.save_result(source, p['id'], '# Complete example\n\nFull real source paragraph.')
    p['id'] = 'sample-S1'
    p['library'] = dict(kind='example', readonly=True, label='已有中文稿', format='txt', tags=['文字'],
                        range='完整来源', details='完整样例')
    p['readweave'] = dict(note_id='template-only', candidate='template-candidate')
    p['processor']['requests'] = [dict(id='template-request', status='SUCCESS')]
    p['processor']['versions'][0]['issue_action'] = dict(preview_id='template-preview', parent_version='old-version')
    p['processor']['last_issue_result'] = dict(preview_id='template-preview')
    personal = Store(tmp_path / 'personal')
    catalog = personal.root / 'library' / 'examples'
    catalog.mkdir(parents=True)
    (catalog / 'catalog.json').write_text(json.dumps(dict(projects=[p])), encoding='utf-8')
    (catalog / 'blobs').mkdir()
    for path in (source.root / 'blobs').iterdir():
        (catalog / 'blobs' / path.name).write_bytes(path.read_bytes())
    return personal, p


def test_readonly_dispatch_and_trial_have_independent_identities(tmp_path):
    personal, template = sample_catalog(tmp_path)
    library = LibraryStore(personal)
    assert personal.list() == []
    assert library.examples()[0]['id'] == 'sample-S1'
    with pytest.raises(Conflict, match='只读'):
        library.change('sample-S1', lambda p: p.update(title='changed'))
    first = library.trial('sample-S1')
    second = library.trial('sample-S1')
    assert first['id'] != second['id'] != template['id']
    assert first['processor']['active_version'] != second['processor']['active_version']
    assert first['processor']['active_version'] != template['processor']['active_version']
    assert first['processor']['versions'][0]['markdown'] == template['processor']['versions'][0]['markdown']
    assert first['processor']['requests'] == []
    assert 'readweave' not in first
    assert 'issue_action' not in first['processor']['versions'][0]
    assert 'last_issue_result' not in first['processor']
    assert library.get('sample-S1') == template
    # A trial has its own complete SHA pool, independent of the template folder.
    for row in first['inventory']['originals']:
        assert personal.read_blob(row['sha256']) == library.read_blob(row['sha256'])


def test_archive_restore_keeps_versions_edits_imports_and_unresolved_requests(tmp_path):
    store = Store(tmp_path)
    p = processor.create(store, 'Independent user edit')
    p = processor.prepare(store, p['id'], [('source.txt', b'Original immutable bytes.')])
    p = processor.save_result(store, p['id'], 'Independent edited full body.')
    p = store.change(p['id'], lambda row: row.update(readweave={'note_id': 'kept-import'}))
    before = copy.deepcopy(p)
    set_archive(store, p['id'], True)
    assert archived(store.get(p['id']))
    set_archive(store, p['id'], False)
    result = store.get(p['id'])
    result.pop('library')
    assert result.pop('library_revision') == 2  # organization has its own optimistic version
    assert result == before
    assert store.read_blob(p['inventory']['originals'][0]['sha256']) == b'Original immutable bytes.'


def test_example_language_is_explicit_and_trial_uses_selected_new_version(tmp_path):
    personal, template = sample_catalog(tmp_path)
    old = copy.deepcopy(template['processor']['versions'][0])
    current = copy.deepcopy(old)
    current.update(id='new-chinese-version', markdown='# 完整中文阅读稿\n\n完整原件的中文正文。')
    from sourceloom.store import digest
    current['digest'] = digest(current['markdown'].encode())
    template['processor'].update(versions=[old, current], active_version=current['id'])
    template['library'].update(reading_language='zh', content_kind='chinese_reading')
    path = personal.root / 'library' / 'examples' / 'catalog.json'
    path.write_text(json.dumps(dict(projects=[template])), encoding='utf-8')
    library = LibraryStore(personal)
    row = library.examples()[0]
    assert row['reading_language'] == 'zh' and row['content_kind'] == 'chinese_reading'
    copied = library.trial('sample-S1')
    assert processor.active_version(copied)['markdown'] == current['markdown']
    assert copied['processor']['versions'][0]['markdown'] == old['markdown']
    assert library.get('sample-S1') == template


def test_labels_alone_do_not_claim_a_chinese_reading_draft(tmp_path):
    personal, _ = sample_catalog(tmp_path)
    row = LibraryStore(personal).examples()[0]
    assert row['label'] == '已有中文稿'
    assert row['reading_language'] is None
    assert row['content_kind'] is None


def test_catalog_must_not_mask_a_corrupt_personal_source(tmp_path):
    personal, template = sample_catalog(tmp_path)
    library = LibraryStore(personal)
    key = template['inventory']['originals'][0]['sha256']
    personal.blob(library.read_blob(key))
    (personal.root / 'blobs' / key).write_bytes(b'corrupted personal bytes')
    with pytest.raises(Conflict, match='字节已经变化'):
        library.read_blob(key)


def test_api_sample_mutations_rejected_and_archived_deep_link_reads(tmp_path):
    personal, template = sample_catalog(tmp_path)
    config = load_config()
    config.update(data_dir=str(personal.root), auth_mode='local')
    with TestClient(create_app(config)) as client:
        headers = {'origin': 'http://testserver', 'x-sourceloom': '1'}
        assert len(client.get('/api/processor/examples').json()) == 1
        assert client.get('/api/processor/projects').json() == []
        for suffix, body in [('result', {'markdown': 'overwrite'}), ('generate', {}), ('readweave', {}), ('archive', {})]:
            response = client.post('/api/processor/projects/sample-S1/'+suffix, json=body, headers=headers)
            assert response.status_code == 409, response.text
        assert client.get('/api/processor/projects/sample-S1/preview').status_code == 200
        response = client.post('/api/processor/examples/sample-S1/trial', headers=headers)
        assert response.status_code == 200, response.text
        pid = response.json()['id']
        version = response.json()['processor']['active_version']
        assert client.post('/api/processor/projects/'+pid+'/archive', headers=headers).status_code == 200
        assert client.get('/api/processor/projects').json() == []
        assert client.get('/api/processor/archives').json()[0]['id'] == pid
        detail = client.get('/api/processor/projects/'+pid).json()
        assert detail['processor']['active_version'] == version
        assert client.get('/api/processor/projects/'+pid+'/preview').status_code == 200
        assert client.post('/api/processor/projects/'+pid+'/restore', headers=headers).status_code == 200
        assert client.get('/api/processor/projects').json()[0]['id'] == pid


def test_old_ui_routes_redirect_and_exclusive_assets_are_removed(tmp_path):
    config = load_config()
    config.update(data_dir=str(tmp_path), auth_mode='local')
    with TestClient(create_app(config)) as client:
        response = client.get('/legacy', follow_redirects=False)
        assert response.status_code == 307
        assert response.headers['location'] == '/?space=archives'
        response = client.get('/workbench', follow_redirects=False)
        assert response.status_code == 307
        assert response.headers['location'] == '/'
        response = client.get('/legacy')
        assert response.status_code == 200
        assert 'processor.js' in response.text
        assert '/static/library.js' not in response.text
        assert '/static/app.js' not in response.text
        for name in ('library.html', 'index.html', 'library.js', 'app.js', 'library.css',
                     'workspace.css', 'style.css', 'document-toolbar.js', 'media-controls.js',
                     'import-panel.js', 'production-progress.js', 'library-tree.js',
                     'location-picker.js', 'editor-panel.js', 'settings.js', 'vendor/editor.js'):
            assert client.get('/static/'+name).status_code == 404, name
        # Preserve independently used historical source navigation and the
        # article renderer while removing the obsolete workbench itself.
        assert client.get('/static/source-focus.js').status_code == 200
        assert client.get('/static/article.css').status_code == 200


def test_old_deep_link_retains_saved_historical_body_read_access(tmp_path):
    config = load_config()
    config.update(data_dir=str(tmp_path), auth_mode='local')
    with TestClient(create_app(config)) as client:
        headers = {'origin': 'http://testserver', 'x-sourceloom': '1'}
        personal = client.post('/api/processor/projects', json={'title': 'Current material'}, headers=headers).json()
        response = client.get('/legacy?document='+personal['id'], follow_redirects=False)
        assert response.headers['location'] == '/?material='+personal['id']
        historical = client.post('/api/demo', headers=headers).json()
        response = client.get('/legacy?document='+historical['id'], follow_redirects=False)
        assert response.status_code == 307
        assert response.headers['location'] == '/api/projects/'+historical['id']+'/output'
        assert client.get(response.headers['location']).status_code == 200
        assert client.get('/api/projects/'+historical['id']).status_code == 200
