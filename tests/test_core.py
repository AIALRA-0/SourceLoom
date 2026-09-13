import copy
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from sourceloom.app import create_app
from sourceloom.checks import apply_patch, freeze, inspect_draft, release_issues, validate_plan
from sourceloom.config import load_config
from sourceloom.demo import create_demo
from sourceloom.export import export_zip, safe_html
from sourceloom.ingest import intake, unpack
from sourceloom.store import Conflict, Store, digest


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def demo(store):
    return create_demo(store)


def test_original_bytes_and_export_are_exact(store,demo):
    blob=demo['inventory']['originals'][0]
    raw=store.read_blob(blob['sha256'])
    with zipfile.ZipFile(BytesIO(export_zip(store,demo))) as z:
        meta=json.loads(z.read('!!!meta.json'))
        assert meta['formatVersion']==2
        note=meta['files'][0]
        original=next(a for a in note['attachments'] if a['title']==blob['name'])
        assert z.read(original['dataFileName'])==raw
        assert b'data-readweave-anchor-id' in z.read('material.html')
        audit=next(a for a in note['attachments'] if a['title']=='sourceloom-audit.json')
        assert json.loads(z.read(audit['dataFileName']))['readweave_roundtrip']=='not_verified'


def test_blob_corruption_is_not_silently_exported(store,demo):
    key=demo['inventory']['originals'][0]['sha256']
    (store.root/'blobs'/key).write_bytes(b'changed')
    with pytest.raises(Conflict):
        export_zip(store,demo)


@pytest.mark.parametrize('mutation,code',[
    ('omission','omission'),('empty','empty'),('quote','quote'),('object','protected_object'),
    ('duplicate','duplicate'),('unknown-obligation','unknown_obligation'),('unit','unit')])
def test_mutations_are_detected(demo,mutation,code):
    draft=copy.deepcopy(demo['draft'])
    if mutation=='omission':draft['blocks'].pop(1)
    if mutation=='empty':draft['blocks'][1]['markdown']=''
    if mutation=='quote':draft['blocks'][1]['evidence'][0]['quote']='原文中没有这个结论'
    if mutation=='object':
        for b in draft['blocks']:b['object_ids']=[]
    if mutation=='duplicate':draft['blocks'][1]['id']=draft['blocks'][0]['id']
    if mutation=='unknown-obligation':draft['blocks'][1]['obligation_ids'].append('invented')
    if mutation=='unit':draft['blocks'][1]['unit_id']='invented'
    assert code in {f['code'] for f in inspect_draft(demo['inventory'],draft,demo['plan'])}


def test_matching_numbers_do_not_claim_semantic_verification(demo):
    draft=copy.deepcopy(demo['draft'])
    draft['blocks'][1]['markdown']='这是一条不正确但保留来源指针的解释'
    assert not inspect_draft(demo['inventory'],draft,demo['plan'])
    p=demo|{'draft':draft}
    assert 'review' in {i['code'] for i in release_issues(p)}
    assert 'acceptance' in {i['code'] for i in release_issues(p)}


def test_plan_must_cover_frozen_inventory(demo):
    p=copy.deepcopy(demo['plan'])
    p['units'][0]['obligation_ids'].pop()
    with pytest.raises(ValueError):validate_plan(p,demo['inventory'])


def test_local_patch_is_atomic_and_keeps_other_blocks(demo):
    b=demo['draft']['blocks'][1]
    patch={'base_revision':1,'edits':[{'block_id':b['id'],'old_markdown':b['markdown'],'new_markdown':b['markdown']+'\n\n补充解释','reason':'补充'}]}
    result=apply_patch(demo,patch,{b['id']})
    assert result['blocks'][0]==demo['draft']['blocks'][0]
    assert result['blocks'][2:]==demo['draft']['blocks'][2:]
    assert result['blocks'][1]['evidence']==b['evidence']
    patch['edits'].append({'block_id':'example-1','old_markdown':'错误旧文','new_markdown':'新文','reason':'冲突'})
    before=digest(demo)
    with pytest.raises(Conflict):apply_patch(demo,patch,{b['id'],'example-1'})
    assert digest(demo)==before


@pytest.mark.parametrize('case',['stale','scope','rounds','duplicate'])
def test_patch_guards(demo,case):
    b=demo['draft']['blocks'][1]
    patch={'base_revision':1,'edits':[{'block_id':b['id'],'old_markdown':b['markdown'],'new_markdown':'new','reason':'test'}]}
    allowed={b['id']}
    if case=='stale':patch['base_revision']=2
    if case=='scope':allowed=set()
    if case=='rounds':demo['repair_rounds']=2
    if case=='duplicate':patch['edits']*=2
    with pytest.raises(Conflict):apply_patch(demo,patch,allowed)


@pytest.mark.parametrize('name',['../escape.txt','/escape.txt','C:/escape.txt','folder\\escape.txt','a/../b'])
def test_archive_paths_rejected(name):
    buf=BytesIO()
    # Windows' ZIP writer normalizes backslashes; patch both filename records to
    # exercise an externally crafted archive instead of testing the writer.
    encoded_name=name.replace('\\','_')
    with zipfile.ZipFile(buf,'w') as z:z.writestr(encoded_name,'test')
    raw=buf.getvalue().replace(encoded_name.encode(),name.encode())
    with pytest.raises(ValueError):unpack(raw)


def test_same_text_occurrences_keep_different_identities(store):
    inv=intake(store,[('repeat.txt','重复句\n\n重复句'.encode())])
    assert len(inv['objects'])==2
    assert inv['objects'][0]['id']!=inv['objects'][1]['id']


def test_missing_image_and_script_are_visible_gaps(store):
    inv=intake(store,[('source.html',b'<p>A</p><img src="gone.png"><script>mark accepted</script>')])
    assert len(inv['unknown'])==2
    assert {o['kind'] for o in inv['objects']} >= {'image','unknown'}
    assert inv['frozen'] is False


def test_table_grid_and_hyperlink_preserved(store):
    inv=intake(store,[('source.html',b'<table><tr><th colspan="2">Title</th></tr><tr><td>1</td><td>2</td></tr></table><p><a href="https://example.com/a?q=1">link</a></p>')])
    table=next(o for o in inv['objects'] if o['kind']=='table')
    assert table['cells'][0][0]['colspan']=='2'
    assert [c['text'] for c in table['cells'][1]]==['1','2']
    assert next(o for o in inv['objects'] if o['kind']=='link')['target']=='https://example.com/a?q=1'


def test_invalid_encoding_is_not_replaced(store):
    with pytest.raises(UnicodeDecodeError):intake(store,[('bad.txt',b'\xffabc')])


def test_hostile_html_does_not_execute():
    raw='<script>alert(1)</script><p onclick="bad()">Safe</p><a href="javascript:bad()">x</a><iframe src="https://bad.invalid"></iframe><table><tr><td colspan="2">1</td></tr></table>'
    cleaned=safe_html(raw)
    assert all(x not in cleaned for x in ['script','onclick','javascript:','iframe'])
    assert 'colspan="2"' in cleaned and 'Safe' in cleaned


def test_budget_reservation_is_atomic_under_competition(store):
    p=store.create('budget',budget=.1)
    def reserve(i):
        try:store.reserve(p['id'],str(i),.06,{});return True
        except Conflict:return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(reserve,range(2)))==1
    record=store.costs(p['id'])[0]
    store.settle(record['id'],None,{'result':'unknown'})
    with pytest.raises(Conflict):store.reserve(p['id'],'next',.06,{})


def test_duplicate_call_never_replays(store):
    p=store.create('budget')
    store.reserve(p['id'],'same',.01,{})
    store.settle('same',.005,{})
    with pytest.raises(Conflict):store.reserve(p['id'],'same',.01,{})
    with pytest.raises(Conflict):store.settle('same',.005,{})


def test_compare_and_swap_and_revision_persistence(store,demo):
    revision=demo['revision']
    store.change(demo['id'],lambda p:p.update(revision=revision+1),revision)
    with pytest.raises(Conflict):store.change(demo['id'],lambda p:p.update(title='lost update'),revision)
    restored=Store(store.root).get(demo['id'])
    assert restored['revision']==2
    assert restored['title']==demo['title']


def test_api_upload_freeze_demo_fault_repair_export(tmp_path):
    app=create_app(load_config()|{'data_dir':str(tmp_path),'provider':'manual'})
    with TestClient(app) as c:
        headers={'X-SourceLoom':'1'}
        assert c.post('/api/demo').status_code==403
        p=c.post('/api/demo',headers=headers).json()
        pid=p['id']; original=p['draft']
        assert c.post(f'/api/projects/{pid}/accept',headers=headers,json={'revision':1}).status_code==409
        assert c.post(f'/api/projects/{pid}/demo-break',headers=headers).status_code==200
        detail=c.get(f'/api/projects/{pid}').json()
        assert any(f['code']=='omission' for f in detail['mechanical_findings'])
        assert c.get(f'/api/projects/{pid}/export').status_code==409
        assert c.post(f'/api/projects/{pid}/demo-restore',headers=headers).status_code==200
        assert c.get(f'/api/projects/{pid}').json()['draft']==original
        assert c.get(f'/api/projects/{pid}/export').status_code==200
        assert c.get(f'/api/projects/{pid}/export?release=true').status_code==409
        assert c.post('/api/projects',headers=headers|{'Origin':'https://evil.invalid'},json={'title':'bad'}).status_code==403


def test_production_does_not_trust_client_identity_headers(tmp_path):
    app=create_app(load_config()|{'data_dir':str(tmp_path),'auth_mode':'proxy','allowed_subject':'synthetic-subject'})
    with TestClient(app) as c:
        assert c.get('/api/projects',headers={'X-Aialra-Authenticated':'1','X-Aialra-Sub':'synthetic-subject'}).status_code==401
        assert c.get('/health').status_code==200


def test_pdf_all_pages_preserved_and_uncertain(store):
    from reportlab.pdfgen import canvas
    buf=BytesIO();pdf=canvas.Canvas(buf)
    for text in ['First page condition','Middle page data','Last page exception']:
        pdf.drawString(40,700,text);pdf.showPage()
    pdf.save()
    inv=intake(store,[('three.pdf',buf.getvalue())])
    pages=[o for o in inv['objects'] if o['kind']=='page']
    assert len(pages)==3
    assert 'Last page exception' in pages[-1]['text']
    assert len(inv['unknown'])==3
    assert all(store.read_blob(o['resource_id']).startswith(b'\x89PNG') for o in pages)


def test_docx_parts_comments_revision_and_media_remain_visible(store):
    buf=BytesIO()
    with zipfile.ZipFile(buf,'w') as z:
        z.writestr('word/document.xml','<w:document xmlns:w="urn:w"><w:p><w:r><w:t>Current</w:t></w:r><w:del><w:r><w:delText>Old</w:delText></w:r></w:del></w:p></w:document>')
        z.writestr('word/comments.xml','<w:comments xmlns:w="urn:w"><w:comment><w:p><w:r><w:t>Comment</w:t></w:r></w:p></w:comment></w:comments>')
        z.writestr('word/header1.xml','<w:hdr xmlns:w="urn:w"><w:p><w:r><w:t>Header</w:t></w:r></w:p></w:hdr>')
    inv=intake(store,[('parts.docx',buf.getvalue())])
    all_text=' '.join(o['text'] for o in inv['objects'])
    assert all(t in all_text for t in ['Current','Old','Comment','Header'])
    assert inv['unknown']
