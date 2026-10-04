"""Processor closure tests use real PDF/image parsers and native ZIP output."""
import copy
import json
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from pypdf import PdfReader

from sourceloom import processor
from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.store import Conflict, Store, digest


def pictured_pdf():
    """Controlled fixture, explicitly synthetic rather than a fidelity claim."""
    picture = Image.new('RGB', (90, 50), 'blue')
    output = BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(60, 760, 'Synthetic test: diagram and two original pages')
    pdf.drawImage(ImageReader(picture), 60, 640, width=180, height=100)
    pdf.drawString(60, 610, 'Figure 1. Blue rectangle, retained original image.')
    pdf.showPage()
    pdf.drawString(60, 760, 'Page two: conditions remain part of the source.')
    pdf.save()
    return output.getvalue()


@pytest.fixture
def prepared(tmp_path):
    store = Store(tmp_path)
    project = processor.create(store, 'Controlled pictured PDF')
    raw = pictured_pdf()
    project = processor.prepare(store, project['id'], [('original.pdf', raw),
        ('code.md', b'## Original code\n\n```python\nvalue = "Original"\nprint(value)\n```\n')])
    return store, project, raw


def complete_markdown(project):
    return '# Synthetic processor acceptance\n\nA controlled result used only to test material handling.\n\n' + '\n\n'.join(
        row['marker'] for row in project['processor']['resources'])


def test_real_pdf_preparation_preserves_bytes_pages_and_embedded_image(prepared):
    store, project, original = prepared
    original_entry = next(o for o in project['inventory']['originals'] if o['name']=='original.pdf')
    assert store.read_blob(original_entry['sha256']) == original
    assert original_entry['sha256'] == digest(original)
    resources = project['processor']['resources']
    assert len([r for r in resources if r['kind']=='page']) == 2
    images = [r for r in resources if r['kind']=='image']
    assert len(images) == 1
    image = Image.open(BytesIO(store.read_blob(images[0]['sha256'])))
    assert image.size == (90, 50)
    assert any('page[1]' in row['locator'] for row in project['processor']['source_map'])
    assert 'plan' not in project or not project['plan']
    assert not store.jobs(project['id'])
    pack = processor.task_pack(store, project['id'])
    assert pack['requires_visual'] is True
    assert any(a['kind']=='original' and a['name']=='original.pdf' for a in pack['attachments'])
    with zipfile.ZipFile(BytesIO(processor.pack_zip(store, project['id']))) as archive:
        assert archive.read('attachments/001-original.pdf') == original
        assert 'manifest.json' in archive.namelist()
        assert pack['source_text'] in archive.read('task.md').decode()
    with pytest.raises(Conflict):
        processor.prepare(store, project['id'], [('replacement.txt', b'New material')])


def test_manual_result_compiles_native_package_with_original_code_and_source_map(prepared):
    store, project, original = prepared
    before = copy.deepcopy(project['inventory'])
    project = processor.save_result(store, project['id'], complete_markdown(project))
    version = processor.active_version(project)
    assert version['mechanical_pass'] and version['semantic_status']=='not_reviewed'
    compiled = processor.compile_result(project, version['markdown'])
    code = next(o['text'] for o in project['inventory']['objects'] if o['kind']=='code')
    assert BeautifulSoup(compiled['html'], 'html.parser').find('pre').get_text() == code
    assert project['inventory'] == before
    with zipfile.ZipFile(BytesIO(processor.export_package(store, project))) as archive:
        note = json.loads(archive.read('!!!meta.json'))['files'][0]
        attachments = {a['title']:a for a in note['attachments']}
        assert archive.read(attachments['original.pdf']['dataFileName']) == original
        source_map = json.loads(archive.read(attachments['source-map.json']['dataFileName']))
        assert source_map['source_digest'] == project['processor']['source_digest']
        assert any(row['source_ids'] for row in source_map['blocks'])
        soup = BeautifulSoup(archive.read('material.html'), 'html.parser')
        for image in soup.find_all('img'):
            assert image['src'] in archive.namelist()
        assert soup.find('pre').get_text() == code
        assert any(a['name']=='sourceloomSemanticStatus' and a['value']=='not_reviewed' for a in note['attributes'])


@pytest.mark.parametrize('mutation,expected', [
    ('unknown', 'UNKNOWN_MARKER'), ('missing', 'OMITTED_RESOURCE'),
    ('path', 'UNRESOLVED_IMAGE'), ('duplicate', 'DUPLICATE_RESOURCE'),
    ('changed-code', 'OMITTED_RESOURCE')])
def test_incomplete_or_unsafe_resource_result_stays_candidate_and_blocks_export(prepared, mutation, expected):
    store, project, _ = prepared
    markdown = complete_markdown(project)
    image = next(r for r in project['processor']['resources'] if r['kind']=='image')
    code = next(r for r in project['processor']['resources'] if r['kind']=='code')
    if mutation=='unknown':
        markdown += '\n\n{{source:invented}}'
    elif mutation=='missing':
        markdown = markdown.replace(image['marker'], '')
    elif mutation=='path':
        markdown += '\n\n![escape](assets/../../private.txt)'
    elif mutation=='duplicate':
        # Images may legitimately be referenced at multiple distinct places;
        # copying a protected code object is still an unsafe duplicate.
        markdown += '\n\n'+code['marker']
    else:
        markdown = markdown.replace(code['marker'], '```python\nvalue = "Changed"\n```')
    saved = processor.save_result(store, project['id'], markdown)
    version = processor.active_version(saved)
    assert expected in {c['code'] for c in version['checks']}
    assert saved['state']=='candidate'
    with pytest.raises(Conflict):
        processor.export_package(store, saved)


def test_html_is_sanitized_without_rewriting_paragraph_rhythm(prepared):
    _, project, _ = prepared
    markdown = complete_markdown(project) + '\n\n一句。两句。\n\n<script>alert(1)</script>\n<p onclick="alert(1)">Visible</p>\n<a href="javascript:alert(1)">Bad URL</a>'
    compiled = processor.compile_result(project, markdown)
    soup = BeautifulSoup(compiled['html'], 'html.parser')
    assert soup.find('script') is None
    assert not soup.select('[onclick]')
    assert not soup.select('a[href^="javascript:"]')
    assert any(p.get_text()=='一句。两句。' for p in soup.find_all('p'))


def test_manual_takeover_preserves_unknown_identity_and_version_history(prepared):
    store, project, _ = prepared
    unknown = dict(id='unknown-original', status='UNKNOWN', logical_request_id='unknown-original', cost_status='unknown')
    store.change(project['id'], lambda p:p['processor']['requests'].append(unknown))
    project = processor.save_result(store, project['id'], complete_markdown(project))
    first = processor.active_version(project)
    changed = processor.save_result(store, project['id'], first['markdown']+'\n\nManual revision', base_version=first['id'])
    assert len(changed['processor']['versions'])==2
    assert changed['processor']['requests']==[unknown]
    assert processor.active_version(changed, first['id'])['markdown']==first['markdown']
    with pytest.raises(Conflict):
        processor.save_result(store, project['id'], 'Concurrent stale edit', base_version=first['id'])


def test_corrupted_original_is_never_exported_as_preserved_source(prepared):
    store, project, _ = prepared
    project = processor.save_result(store, project['id'], complete_markdown(project))
    original = next(o for o in project['inventory']['originals'] if o['name']=='original.pdf')
    (store.root/'blobs'/original['sha256']).write_bytes(b'corrupted original')
    with pytest.raises(Conflict):
        processor.export_package(store, project)


def test_cmyk_jpeg_pdf_image_retains_extracted_original_bytes(tmp_path):
    jpeg = BytesIO()
    Image.new('CMYK', (100, 60), (0, 150, 200, 20)).save(jpeg, 'JPEG')
    pdf_raw = BytesIO()
    pdf = canvas.Canvas(pdf_raw)
    pdf.drawString(60, 760, 'Controlled CMYK original image')
    pdf.drawImage(ImageReader(BytesIO(jpeg.getvalue())), 60, 620, width=200, height=120)
    pdf.save()
    raw = pdf_raw.getvalue()
    original_image = PdfReader(BytesIO(raw)).pages[0].images[0].data
    store = Store(tmp_path)
    project = processor.create(store, 'CMYK image')
    project = processor.prepare(store, project['id'], [('cmyk.pdf', raw)])
    figure = next(r for r in project['processor']['resources'] if r['kind']=='image')
    assert store.read_blob(figure['sha256']) == original_image
    stored = next(r for r in project['inventory']['resources'] if r['id']==figure['sha256'])
    assert stored['mime']=='image/jpeg'
    assert Image.open(BytesIO(original_image)).mode=='CMYK'


def test_frozen_source_digest_describes_final_inventory(prepared):
    _, project, _ = prepared
    inventory = project['inventory']
    assert inventory['frozen'] is True
    expected = digest({k:v for k,v in inventory.items() if k!='digest'})
    assert inventory['digest']==expected==project['processor']['source_digest']


def test_same_request_result_commit_is_transactionally_idempotent(prepared):
    store, project, _ = prepared
    markdown = complete_markdown(project)
    before_revision = project['revision']
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: processor.save_result(store, project['id'], markdown,
            origin='api', request_id='single-dispatch-identity'), range(4)))
    saved = store.get(project['id'])
    assert len(saved['processor']['versions'])==1
    assert saved['revision']==before_revision+1
    assert all(len(p['processor']['versions'])==1 for p in results)


def test_async_result_binds_dispatch_pack_not_later_preferences(prepared, monkeypatch):
    store, project, _ = prepared
    started, finish = threading.Event(), threading.Event()
    sent_pack = {}
    from sourceloom import processor_channels
    def handoff(store, config, pid, pack, channel, request_id):
        sent_pack.update(pack)
        started.set()
        assert finish.wait(10)
        return dict(status='SUCCESS', markdown=complete_markdown(project),
                    logical_request_id=request_id, pack_digest=pack['digest'])
    monkeypatch.setattr(processor_channels, 'generate', handoff)
    config = load_config()
    config.update(data_dir=str(store.root), auth_mode='local', external_worker=True)
    config['processor_router'] = dict(provider='router', api_key='synthetic-business-key',
        base_url='https://router.example.test', model='chatgpt-web.auto', execution_channel='chatgpt_web')
    with TestClient(create_app(config)) as client:
        headers = {'x-sourceloom':'1'}
        route = f'/api/processor/projects/{project["id"]}'
        response = client.post(route+'/generate', json={'request_id':'dispatch-pack-001','channel':'router'}, headers=headers)
        assert response.status_code==202
        assert started.wait(10)
        replacement = client.post(route+'/pack', json={'preferences':'Changed after actual dispatch'}, headers=headers)
        assert replacement.status_code==200
        assert replacement.json()['digest'] != sent_pack['digest']
        finish.set()
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            current = client.get(route).json()
            if current['processor']['versions']:
                break
            time.sleep(.02)
        version = processor.active_version(current)
        assert version['pack_digest']==sent_pack['digest']
        assert version['request_id']=='dispatch-pack-001'


def test_restart_recovers_completed_receipt_without_model_or_new_version(prepared, monkeypatch):
    store, project, _ = prepared
    from sourceloom import processor_channels
    def forbidden(*args, **kwargs):
        raise AssertionError('Restart must not generate again')
    monkeypatch.setattr(processor_channels, 'generate', forbidden)
    rid = 'saved-success-001'
    pack = processor.task_pack(store, project['id'])
    store.change(project['id'], lambda p:p['processor']['requests'].append(
        dict(id=rid,status='RUNNING',channel='router',pack_digest=pack['digest'])))
    markdown = complete_markdown(project)
    store.put_job(dict(id=rid, project=project['id'], role='processor', channel='router', status='completed', created=time.time(),
                       receipt=dict(status='SUCCESS',markdown=markdown,logical_request_id=rid,
                                    execution_channel='chatgpt_web', execution_mode='chat')))
    config = load_config()
    config.update(data_dir=str(store.root), auth_mode='local', external_worker=True)
    for _ in range(2):
        with TestClient(create_app(copy.deepcopy(config))) as client:
            result = client.get(f'/api/processor/projects/{project["id"]}').json()
            assert len(result['processor']['versions'])==1
            assert processor.active_version(result)['markdown']==markdown
            assert processor.active_version(result)['pack_digest']==pack['digest']
            assert result['processor']['requests'][0]['status']=='SUCCESS'
    assert len(store.jobs(project['id']))==1


@pytest.mark.parametrize('channel', ['api', 'codex', 'cli', 'runner'])
def test_http_rejects_unapproved_channel_before_claim_or_handoff(prepared, monkeypatch, channel):
    store, project, _ = prepared
    from sourceloom import processor_channels
    monkeypatch.setattr(processor_channels, 'generate', lambda *a, **k: pytest.fail('No upstream dispatch'))
    config = load_config() | dict(data_dir=str(store.root), auth_mode='local', external_worker=True)
    before = store.get(project['id'])
    with TestClient(create_app(config)) as client:
        response = client.post(f'/api/processor/projects/{project["id"]}/generate',
            json={'channel': channel, 'request_id': 'disallowed-channel'}, headers={'x-sourceloom': '1'})
        assert response.status_code == 400
    assert store.get(project['id']) == before
    assert not store.jobs(project['id'])


def test_restart_and_query_preserve_historical_codex_without_import(prepared, monkeypatch):
    store, project, _ = prepared
    from sourceloom import processor_channels
    monkeypatch.setattr(processor_channels.httpx, 'get', lambda *a, **k: pytest.fail('No historical upstream query'))
    rid = 'historical-codex-success'
    original = dict(id=rid, project=project['id'], role='processor', channel='router',
        status='completed', created=time.time(), calls=[{'router_output_file': 'article.md'}],
        receipt=dict(status='SUCCESS', markdown=complete_markdown(project), logical_request_id=rid))
    store.put_job(original)
    store.change(project['id'], lambda p:p['processor']['requests'].append(
        dict(id=rid, status='RUNNING', channel='router')))
    config = load_config() | dict(data_dir=str(store.root), auth_mode='local', external_worker=True)
    with TestClient(create_app(config)) as client:
        path = f'/api/processor/projects/{project["id"]}'
        response = client.post(path+'/requests/'+rid+'/query', headers={'x-sourceloom': '1'})
        assert response.status_code == 200
        saved = response.json()['processor']
        assert not saved['versions']
        assert saved['requests'][0]['historical_readonly'] is True
    assert store.job(rid) == original


def test_legacy_mutation_routes_cannot_reopen_processor_source(prepared):
    store, project, _ = prepared
    config = load_config()
    config.update(data_dir=str(store.root), auth_mode='local', external_worker=True)
    before = copy.deepcopy(project['inventory'])
    with TestClient(create_app(config)) as client:
        headers = {'x-sourceloom':'1'}
        route = f'/api/projects/{project["id"]}'
        response = client.post(route+'/revise', headers=headers, json=dict(
            revision=project['revision'],inventory_digest=before['digest'],reason='Attempt old reopen'))
        assert response.status_code==409
        response = client.post(route+'/upload', headers=headers, files={'files':('replacement.txt',b'Unrelated')})
        assert response.status_code==409
        response = client.post(route+'/run/plan', headers=headers)
        assert response.status_code==409
    assert store.get(project['id'])['inventory']==before
    assert not store.jobs(project['id'])


def test_new_api_closure_bypasses_old_article_pipeline(tmp_path, monkeypatch):
    from sourceloom.pipeline import Pipeline
    def forbidden(*args, **kwargs):
        raise AssertionError('New processor must not dispatch old Plan/Writer roles')
    monkeypatch.setattr(Pipeline, 'submit', forbidden, raising=False)
    config = load_config()
    config.update(data_dir=str(tmp_path), auth_mode='local', external_worker=True)
    app = create_app(config)
    with TestClient(app) as client:
        headers = {'x-sourceloom':'1'}
        created = client.post('/api/processor/projects', json={'title':'PDF closure'}, headers=headers)
        assert created.status_code==200
        pid = created.json()['id']
        raw = pictured_pdf()
        response = client.post(f'/api/processor/projects/{pid}/upload', files={'files':('test.pdf', raw, 'application/pdf')}, headers=headers)
        assert response.status_code==200, response.text
        project = response.json()
        pack = client.get(f'/api/processor/projects/{pid}/pack').json()
        original = next(a for a in pack['attachments'] if a['kind']=='original')
        assert client.get(original['url']).content==raw
        assert client.post(f'/api/processor/projects/{pid}/result', json={'markdown':complete_markdown(project)}, headers=headers).status_code==200
        preview = client.get(f'/api/processor/projects/{pid}/preview')
        assert preview.status_code==200
        soup = BeautifulSoup(preview.text, 'html.parser')
        assert soup.find('img')
        for img in soup.find_all('img'):
            assert client.get(img['src']).status_code==200
        package = client.get(f'/api/processor/projects/{pid}/export')
        assert package.status_code==200
        assert zipfile.is_zipfile(BytesIO(package.content))
        assert app.state.store.jobs(pid)==[]


def test_inline_reference_and_code_name_remain_in_reading_material_without_fetch(tmp_path):
    store = Store(tmp_path)
    p = processor.create(store,'Inline reference')
    p = processor.prepare(store,p['id'],[('article.html',b'<article><h1>Original topic</h1><p>Use <code>mode</code> with the <a href="https://example.org/reference">original rule</a>. This sentence already gives its meaning.</p></article>')])
    pack = processor.task_pack(store,p['id'])
    assert '`mode`' in pack['source_text']
    assert '[original rule](https://example.org/reference)' in pack['source_text']
    assert 'This sentence already gives its meaning.' in pack['source_text']
    assert 'https://example.org/reference' in pack['prompt']
    assert not store.jobs(p['id'])
    assert len(p['inventory']['originals'])==1


def test_native_source_map_keeps_original_hashes_without_visible_bookmark_widgets(prepared):
    store, p, _ = prepared
    p = processor.save_result(store,p['id'],complete_markdown(p))
    with zipfile.ZipFile(BytesIO(processor.export_package(store,p))) as archive:
        note = json.loads(archive.read('!!!meta.json'))['files'][0]
        source_map = next(a for a in note['attachments'] if a['title']=='source-map.json')
        mapping = json.loads(archive.read(source_map['dataFileName']))
        assert mapping['originals']==p['inventory']['originals']
        assert mapping['source_digest']==p['inventory']['digest']
        doc = BeautifulSoup(archive.read('material.html'),'html.parser')
        assert not doc.select('a[id^="loom-source-"]')
        assert len(doc.select('[data-readweave-anchor-id]'))==len(mapping['blocks'])
