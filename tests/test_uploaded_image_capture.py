"""Uploaded Markdown must keep linked images and continue when a link fails."""

import copy
from unittest.mock import patch

from sourceloom.active_composition import ActiveComposition
from sourceloom.durable import Queue
from sourceloom.ingest import intake
from sourceloom.network import fetch_uploaded_assets
from sourceloom.production import Production
from sourceloom.intake_jobs import IntakeQueue
from sourceloom.config import load_config
from sourceloom.store import Store
from tests.test_production import prepared, skill


def test_uploaded_markdown_collects_absolute_image_before_inventory(tmp_path, monkeypatch):
    source=b'# Article\n\n![A chart](https://example.org/chart.png)\n'
    image=b'\x89PNG\r\n\x1a\nimage-data'
    calls=[]

    def fetch(url, allowed_types, timeout):
        calls.append(url)
        return image, 'image/png', url

    monkeypatch.setattr('sourceloom.network.fetch', fetch)
    uploads, aliases, failures=fetch_uploaded_assets([('article.md', source)])
    assert calls==['https://example.org/chart.png'] and not failures
    assert len(uploads)==2
    from sourceloom.store import Store
    inventory=intake(Store(tmp_path), uploads, asset_aliases=aliases)
    picture=next(o for o in inventory['objects'] if o['kind']=='image')
    assert picture['resource_id'] and picture['target']=='https://example.org/chart.png'
    assert inventory['unknown']==[]


def test_unavailable_linked_image_does_not_discard_other_uploaded_text(tmp_path, skill):
    store, _, project, bundle=prepared(tmp_path, skill)
    job=Queue(store,pipeline='active_composition_v2').enqueue(project['id'],bundle)
    source=copy.deepcopy(job['source'])
    source['objects'].append(dict(id='linked-image',kind='image',text='A chart',
        locator='article.md/image[1]',target='https://example.org/chart.png',resource_id=None))
    source['unknown'].append(dict(id='gap-1',object_id='linked-image',
        reason='图片资源尚未取得，原地址已保留',locator='article.md/image[1]'))
    job.update(stage='active_visual',source=source)
    assert ActiveComposition(Production(store,{})).step(job)=='queued'
    assert job['stage']=='active_plan'
    assert job['material_acquisition_limits']==[dict(object_id='linked-image',
        target='https://example.org/chart.png',reason='图片资源尚未取得，原地址已保留')]
    assert any(o['kind']=='text' for o in job['source']['objects'])


def test_uploaded_html_uses_picture_srcset_and_keeps_original_reference(tmp_path, monkeypatch):
    source=(b'<main><h1>Measurements</h1><picture>'
            b'<source srcset="https://example.org/chart-large.png 2x">'
            b'<img src="https://example.org/chart-small.png" alt="Result chart">'
            b'</picture></main>')
    calls=[]
    def fetch(url, allowed_types, timeout):
        calls.append(url)
        return b'\x89PNG\r\n\x1a\nlarge', 'image/png', url
    monkeypatch.setattr('sourceloom.network.fetch',fetch)
    uploads,aliases,failures=fetch_uploaded_assets([('article.html',source)])
    assert calls==['https://example.org/chart-large.png'] and failures==[]
    assert aliases['https://example.org/chart-small.png']==aliases['https://example.org/chart-large.png']
    inventory=intake(Store(tmp_path),uploads,asset_aliases=aliases)
    image=next(obj for obj in inventory['objects'] if obj['kind']=='image')
    assert image['resource_id'] and image['text']=='Result chart'
    assert inventory['unknown']==[]


def test_malformed_image_url_cannot_discard_uploaded_document(tmp_path, monkeypatch):
    source=b'<main><p>Keep this text</p><img src="https://[bad/img.png" alt="Figure"></main>'
    monkeypatch.setattr('sourceloom.network.fetch',lambda *args: AssertionError('invalid URL must not be fetched'))
    uploads,aliases,failures=fetch_uploaded_assets([('article.html',source)])
    inventory=intake(Store(tmp_path),uploads,asset_aliases=aliases)
    assert len(failures)==1 and failures[0]['target']=='https://[bad/img.png'
    assert any('Keep this text' in item['text'] for item in inventory['objects'])
    assert len(inventory['unknown'])==1
    image=next(item for item in inventory['objects'] if item['kind']=='image')
    assert image['target']=='https://[bad/img.png' and not image['resource_id']


def test_upload_retry_uses_frozen_image_bytes_after_parser_restart(tmp_path, monkeypatch):
    store=Store(tmp_path)
    project=store.create('Frozen upload')
    config=load_config()|{'provider':'manual'}
    queue=IntakeQueue(store,config)
    receipt=queue.enqueue(project['id'],uploads=[('article.md',b'![Figure](https://example.org/chart.png)')])
    fetch_count=[]
    def fetch(url, allowed_types, timeout):
        fetch_count.append(url)
        return b'\x89PNG\r\n\x1a\noriginal', 'image/png', url
    monkeypatch.setattr('sourceloom.network.fetch',fetch)
    with patch('sourceloom.parse_worker.isolated_intake',side_effect=KeyboardInterrupt):
        try:queue.run_once()
        except KeyboardInterrupt:pass
        else:assert False, 'parser interruption must propagate'
    frozen=store.job(receipt['id'])
    assert frozen['upload_assets_collected'] and len(frozen['files'])==2
    assert fetch_count==['https://example.org/chart.png']
    with store.connect() as cx:
        cx.execute('UPDATE intake_control SET lease_until=0 WHERE id=?',(receipt['id'],))
    with patch('sourceloom.parse_worker.isolated_intake',side_effect=lambda s,files,**kw:intake(s,files,**kw)):
        assert queue.run_once()
    assert len(fetch_count)==1 and store.job(receipt['id'])['status']=='completed'
    assert any(item['kind']=='image' and item['resource_id'] for item in store.get(project['id'])['inventory']['objects'])
