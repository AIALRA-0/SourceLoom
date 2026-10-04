"""Offline source metadata must bind existing bytes, never supply article facts."""
import copy
import hashlib
import json
from io import BytesIO
import zipfile

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from PIL import Image

from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.ingest import intake
from sourceloom.source_manifest import resolve_manifest
from sourceloom.store import Store


def test_verified_primary_inside_zip_uses_existing_dom_scope_rules(tmp_path):
    from sourceloom import processor
    body = (b'<html><body><header><a href="/">Site home</a></header>'
            b'<nav><ul><li><a href="/tools">Site tools</a></li></ul></nav>'
            b'<main><h1>Scientific article</h1><p>'+b'Actual article evidence. '*20+
            b'</p><table><tr><th>Value</th></tr><tr><td>17</td></tr></table></main>'
            b'<footer><p>Site footer</p></footer></body></html>')
    manifest=dict(schema='sourceloom-source-manifest/1',
                  source=dict(path='snapshot.html',sha256=sha(body),url='https://example.org/article/'),assets=[])
    zipped=archive({'snapshot.html':body},manifest)
    direct_store=Store(tmp_path/'direct');bundle_store=Store(tmp_path/'bundled')
    direct=processor.create(direct_store,'Direct source')
    direct=processor.prepare(direct_store,direct['id'],[('snapshot.html',body)],source_url=manifest['source']['url'])
    bundled=processor.create(bundle_store,'Offline source')
    bundled=processor.prepare(bundle_store,bundled['id'],[('frozen.zip',zipped)])
    assert bundled['inventory']['originals'][0]['sha256']==sha(zipped)
    assert bundle_store.read_blob(sha(body))==body
    assert bundled['inventory']['objects']==direct['inventory']['objects']
    assert 'Site tools' not in bundled['processor']['source_text']
    assert 'Site footer' not in bundled['processor']['source_text']
    assert 'Actual article evidence.' in bundled['processor']['source_text']
    assert '17' in bundled['processor']['source_text']
    assert bundled['processor']['source_text']==direct['processor']['source_text']


def test_unverified_embedded_document_cannot_create_archive_scope(tmp_path):
    from sourceloom.source_context import classify_web_chrome
    store=Store(tmp_path)
    body=b'<html><body><nav><p>Example navigation</p></nav></body></html>'
    resource=store.blob(body)
    source=dict(originals=[dict(name='transport.zip',sha256='0'*64)],
                resources=[dict(name='snapshot.html',sha256=resource)],
                objects=[dict(id='src-1',kind='text',text='Example navigation',raw='<p>Example navigation</p>',locator='snapshot.html/node[1]/p[1]')],
                source_manifest=dict(source=dict(path='snapshot.html',sha256=resource)),unknown=[])
    result=classify_web_chrome(store,source)
    assert result['objects']==source['objects']


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def bundle(fmt='md'):
    output = BytesIO()
    Image.new('RGB', (12, 8), 'blue').save(output, 'PNG')
    image = output.getvalue()
    source = (b'# Frozen source\n\nA stated condition.\n\n'
              b'![absolute](https://example.org/source/figure.png)\n\n'
              b'![relative](other.png)\n\n```python\nvalue = 7\n```\n')
    if fmt == 'html':
        source = (b'<article><h1>Frozen source</h1><p>A stated condition.</p>'
                  b'<img alt="absolute" src="https://example.org/source/figure.png">'
                  b'<img alt="relative" src="other.png">'
                  b'<pre><code>value = 7\n</code></pre></article>')
    source_name = 'snapshot.'+fmt
    files = {source_name: source, 'web-assets/figure.png': image,
             'web-assets/other.png': image}
    manifest = dict(schema='sourceloom-source-manifest/1',
                    source=dict(path=source_name, sha256=sha(source),
                                url='https://example.org/source/article.md'),
                    assets=[dict(path=name, sha256=sha(image), url=url) for name, url in
                            [('web-assets/figure.png', 'https://example.org/source/figure.png'),
                             ('web-assets/other.png', 'https://example.org/source/other.png')]])
    return files, manifest


def archive(files, manifest):
    output = BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        for name, raw in files.items():
            z.writestr(name, raw)
        z.writestr('SOURCE_MANIFEST.json', json.dumps(manifest).encode())
    return output.getvalue()


@pytest.mark.parametrize('fmt', ['md', 'html'])
def test_normal_upload_restores_frozen_aliases_without_fetch_or_metadata_prose(tmp_path, monkeypatch, fmt):
    def forbidden_fetch(*args, **kwargs):
        raise AssertionError('An offline source manifest cannot trigger a fetch')
    monkeypatch.setattr('sourceloom.network.fetch', forbidden_fetch)
    monkeypatch.setattr('sourceloom.network.fetch_bundle', forbidden_fetch)
    files, manifest = bundle(fmt)
    raw = archive(files, manifest)
    config = load_config()
    config.update(data_dir=str(tmp_path/'api'), auth_mode='local', external_worker=True)
    headers = {'x-sourceloom': '1'}
    with TestClient(create_app(config)) as client:
        project = client.post('/api/processor/projects', json={'title': 'Offline source'}, headers=headers).json()
        route = '/api/processor/projects/'+project['id']
        response = client.post(route+'/upload', files=[('files', ('source.zip', raw, 'application/zip'))], headers=headers)
        assert response.status_code == 200, response.text
        project = response.json()
        inv = project['inventory']
        assert inv['source_url'] == manifest['source']['url']
        assert inv['source_manifest']['external_reads'] == 0
        assert inv['source_manifest']['verification'] == 'uploaded_bytes_sha256'
        assert inv['originals'][0]['sha256'] == sha(raw)
        resources = project['processor']['resources']
        images = [r for r in resources if r['kind'] == 'image']
        assert len(images) == 2 and all(r['sha256'] == sha(files['web-assets/figure.png']) for r in images)
        assert 'SOURCE_MANIFEST' not in project['processor']['source_text']
        assert not any(o['locator'] == 'SOURCE_MANIFEST.json' for o in inv['objects'])
        expected = intake(Store(tmp_path/'expected'), list(files.items()), manifest['source']['url'],
                          {a['url']:a['path'] for a in manifest['assets']})
        assert inv['objects'] == expected['objects']
        returned = '# 中文阅读稿\n\n原件中的条件。\n\n' + '\n\n'.join(r['marker'] for r in resources)
        saved = client.post(route+'/result', json={'markdown':returned, 'origin':'manual', 'base_version':None}, headers=headers)
        assert saved.status_code == 200, saved.text
        assert saved.json()['processor']['versions'][0]['markdown'] == returned
        preview = client.get(route+'/preview')
        assert preview.status_code == 200
        rendered_images = BeautifulSoup(preview.text, 'html.parser').find_all('img')
        assert len(rendered_images) == 2
        assert all(image['src'].endswith('/'+sha(files['web-assets/figure.png'])) for image in rendered_images)
        export = client.get(route+'/export')
        assert export.status_code == 200, export.text
        with zipfile.ZipFile(BytesIO(client.get(route+'/pack.zip').content)) as z:
            assert z.read('attachments/001-source.zip') == raw


def test_manifest_not_present_keeps_existing_intake_behavior(tmp_path):
    files, _ = bundle()
    inv = intake(Store(tmp_path), list(files.items()))
    assert inv['source_url'] is None and 'source_manifest' not in inv
    assert all(not o.get('resource_id') for o in inv['objects'] if o['kind']=='image')


def test_normal_upload_with_wrong_hash_does_not_freeze_a_partial_inventory(tmp_path):
    files, manifest = bundle()
    manifest['assets'][0]['sha256'] = '0'*64
    config = load_config()
    config.update(data_dir=str(tmp_path), auth_mode='local', external_worker=True)
    headers = {'x-sourceloom':'1'}
    with TestClient(create_app(config)) as client:
        project = client.post('/api/processor/projects', json={'title':'Invalid offline input'}, headers=headers).json()
        route = '/api/processor/projects/'+project['id']
        response = client.post(route+'/upload', files=[('files', ('source.zip', archive(files, manifest), 'application/zip'))], headers=headers)
        assert response.status_code == 400
        assert 'SHA-256' in response.text
        assert not client.get(route).json().get('inventory')


@pytest.mark.parametrize('path', ['../snapshot.md', '/snapshot.md', 'a\\snapshot.md',
                                 'C:/snapshot.md', 'a//snapshot.md', './snapshot.md', 'a\x00.md',
                                 'CON.md', 'a/LPT1.png', 'trailing.md '])
def test_manifest_rejects_unsafe_paths(path):
    files, manifest = bundle()
    manifest['source']['path'] = path
    files['SOURCE_MANIFEST.json'] = json.dumps(manifest).encode()
    with pytest.raises(ValueError, match='路径'):
        resolve_manifest(files)


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'https://user:secret@example.org/a',
                                'https://example.org/a#section', 'https://example.org/a\n',
                                'https://example.org:invalid/a', 'https://'])
def test_manifest_rejects_invalid_source_urls(url):
    files, manifest = bundle()
    manifest['source']['url'] = url
    files['SOURCE_MANIFEST.json'] = json.dumps(manifest).encode()
    with pytest.raises(ValueError, match='URL|地址'):
        resolve_manifest(files)


@pytest.mark.parametrize('kind', ['source', 'asset'])
def test_manifest_rejects_hash_mismatch_before_any_inventory_can_commit(tmp_path, kind):
    files, manifest = bundle()
    target = manifest['source'] if kind=='source' else manifest['assets'][0]
    target['sha256'] = '0'*64
    with pytest.raises(ValueError, match='SHA-256 不匹配'):
        intake(Store(tmp_path), [('source.zip', archive(files, manifest))])


def test_manifest_rejects_missing_assets_duplicate_urls_and_conflicting_existing_aliases():
    files, manifest = bundle()
    files['SOURCE_MANIFEST.json'] = json.dumps(manifest).encode()
    missing = dict(files)
    del missing['web-assets/figure.png']
    with pytest.raises(ValueError, match='不存在'):
        resolve_manifest(missing)
    with pytest.raises(ValueError, match='地址冲突'):
        resolve_manifest(files, source_url='https://example.org/different.md')
    with pytest.raises(ValueError, match='别名冲突'):
        resolve_manifest(files, asset_aliases={manifest['assets'][0]['url']:'different.png'})
    duplicate = copy.deepcopy(manifest)
    duplicate['assets'].append(copy.deepcopy(duplicate['assets'][0]))
    files['SOURCE_MANIFEST.json'] = json.dumps(duplicate).encode()
    with pytest.raises(ValueError, match='重复或冲突'):
        resolve_manifest(files)


def test_manifest_rejects_nested_duplicate_or_invalid_json_fields():
    files, manifest = bundle()
    raw = json.dumps(manifest).encode()
    for extra in [{'nested/SOURCE_MANIFEST.json':raw},
                  {'SOURCE_MANIFEST.json':raw, 'source_manifest.json':raw},
                  {'SOURCE_MANIFEST.json':b'{"schema":1,"schema":2}'}]:
        with pytest.raises(ValueError):
            resolve_manifest(files | extra)
