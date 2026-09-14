import copy
import json
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.demo import create_demo
from sourceloom.store import Store,Conflict
from sourceloom.durable import Queue
from sourceloom.export import render
from sourceloom.library_api import source_locations
from sourceloom.reading import presentation
from sourceloom.terminology import verified_terms
import pytest

def test_embedded_source_never_produces_an_empty_quote(tmp_path):
    p=create_demo(Store(tmp_path));b=p['draft']['blocks'][0]
    b.update(object_ids=['src-00001'],embedded_object_ids=['src-00001'])
    assert not [q for q in BeautifulSoup(render(p),'html.parser').find_all('blockquote') if not q.get_text(strip=True) and not q.find(['img','table','pre'])]

def test_heading_numbering_is_reversible_and_code_untouched(tmp_path):
    p=create_demo(Store(tmp_path));p['draft']['blocks'][0]['markdown']='## 主节\n\n### 子节\n\n```md\n# 不能改的代码\n```\n\n## 下节\n'
    original=copy.deepcopy(p)
    numbered=presentation(p,'numbered')
    text=numbered['draft']['blocks'][0]['markdown']
    assert '## 1 主节' in text and '### 1.1 子节' in text and '## 2 下节' in text
    assert '```md\n# 不能改的代码\n```' in text and p==original
    assert presentation(numbered,'none')['draft']['blocks'][0]['markdown']==p['draft']['blocks'][0]['markdown']

def test_source_locations_include_all_references(tmp_path):
    p=create_demo(Store(tmp_path));first,second=p['inventory']['objects'][:2]
    p['draft']['blocks'][0]['evidence']=[{'source_id':x['id'],'quote':x['text']} for x in (first,second)]
    result=source_locations(p)['block-1']
    assert result['source_id']==first['id']
    assert [l['source_id'] for l in result['alternatives']]==[first['id'],second['id']]

def test_reader_excludes_history_and_preview_does_not_save(tmp_path):
    config=load_config()|{'data_dir':str(tmp_path),'provider':'manual'}
    app=create_app(config);s=app.state.store;p=create_demo(s)
    with TestClient(app) as c:
        reader=c.get(f"/api/projects/{p['id']}/reader").json()
        assert reader['draft'] is True and 'jobs' not in reader and 'objects' not in reader['inventory']
        settings=c.patch(f"/api/projects/{p['id']}/reading-settings",json={'heading_numbering':'numbered'},headers={'X-SourceLoom':'1'})
        assert settings.status_code==200 and s.get(p['id'])['revision']==p['revision']
        preview=c.post(f"/api/projects/{p['id']}/preview-edit",json={'edits':[{'block_id':'example-1','markdown':'## 预览改动'}]},headers={'X-SourceLoom':'1'})
        assert preview.status_code==200 and '预览改动' in preview.json()['html']
        assert s.get(p['id'])['draft']==p['draft']

def test_official_term_lookup_is_separate_cached_and_evidenced(tmp_path):
    s=Store(tmp_path);calls=[]
    def fetch(url,**kw):
        calls.append(url);return b'<p>Internet Engineering Task Force (IETF)</p>','text/html',url
    source={'objects':[{'text':'IETF'}]}
    first=verified_terms(s,source,fetch)
    assert first[0]['en']=='Internet Engineering Task Force' and first[0]['quote']
    assert verified_terms(s,source,fetch)==first and len(calls)==1

def test_new_rewrite_retains_old_content_and_project_spending(tmp_path):
    s=Store(tmp_path);queue=Queue(s);p=create_demo(s);inv=p['inventory'];inv['inventory_review']={'passed':True}
    s.change(p['id'],lambda p:p.update(inventory=inv))
    old={'id':'previous','project':p['id'],'role':'production','status':'needs_attention',
         'inventory':inv,'source':inv,'facts':{'facts':[{'id':'f'}]},'calls':[]}
    with s.connect() as cx:cx.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',('previous',p['id'],'production','needs_attention',1,json.dumps(old)))
    bundle={'root':'saved','package_digest':'saved','instruction_digest':'saved'}
    job=queue.rewrite_existing(p['id'],bundle)
    assert job['transformation_mode']=='rewrite' and job['stage']=='planner'
    assert job['reused_inventory_job']=='previous' and job['base_revision']==p['revision']
    assert s.get(p['id'])['draft']==p['draft'] and s.get(p['id'])['max_calls']==p['max_calls']
    with pytest.raises(Conflict):queue.rewrite_existing(p['id'],bundle)


def test_duplicate_keeps_content_but_not_import_or_job_identity(tmp_path):
    app=create_app(load_config()|{'data_dir':str(tmp_path),'provider':'manual'})
    p=create_demo(app.state.store)
    with TestClient(app) as c:
        copied=c.post(f"/api/projects/{p['id']}/duplicate",headers={'X-SourceLoom':'1'}).json()
        assert copied['id']!=p['id'] and copied['draft']==p['draft']
        assert copied['inventory']==p['inventory'] and copied['state']=='generated'
        assert not copied.get('active_job') and not copied.get('readweave')
        assert copied['copied_from']['project']==p['id']


def test_pdf_text_preview_is_file_scoped_and_keeps_original_download(tmp_path):
    app=create_app(load_config()|{'data_dir':str(tmp_path),'provider':'manual'})
    s=app.state.store;p=create_demo(s);raw=b'%PDF-private-fixture';key=s.blob(raw)
    inv={'originals':[{'name':'one.pdf','sha256':key}], 'objects':[
        {'id':'one','locator':'one.pdf/page[1]','text':'First page'},
        {'id':'two','locator':'two.pdf/page[1]','text':'Not this file'}]}
    s.change(p['id'],lambda p:p.update(inventory=inv))
    with TestClient(app) as c:
        text=c.get(f"/api/projects/{p['id']}/original-view/{key}?view=text")
        assert 'First page' in text.text and 'Not this file' not in text.text
        assert 'data-source-id="one"' in text.text
        original=c.get(f"/api/projects/{p['id']}/original-view/{key}")
        assert original.content==raw and original.headers['content-type']=='application/pdf'


def test_external_term_and_generic_capitalization_survive_composition(tmp_path):
    from sourceloom.writing import compose
    runtime=tmp_path/'runtime';runtime.mkdir()
    (runtime/'composition.py').write_text('import json\ndef render_document(body,**kwargs):return json.dumps(body,ensure_ascii=False)')
    inv={'objects':[{'id':'s','kind':'paragraph','text':'IETF jargon anonymous function','locator':'s.txt'}],
         '_verified_terminology':[{'en':'Internet Engineering Task Force','url':'https://www.ietf.org/about/',
                                  'quote':'Internet Engineering Task Force (IETF)'}]}
    def term(en):return {'type':'term','zh':'名称','en':en,'definition':['定义','用途','边界']}
    response={'blocks':[{'id':'b','unit_id':'u','kind':'explanation','content':[
        term('Internet Engineering Task Force'),term('jargon'),term('anonymous function')]}]}
    text=compose({'root':str(tmp_path)},response,inv)['blocks'][0]['markdown']
    assert 'Internet Engineering Task Force' in text and 'Jargon' in text and 'Anonymous Function' in text
    assert inv['objects'][0]['text']=='IETF jargon anonymous function'


def test_keepalive_cannot_extend_whole_model_request_deadline(monkeypatch):
    import asyncio,time
    from sourceloom.providers import post_before_deadline,Uncertain
    events=[]
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):events.append('closed')
        async def post(self,*args,**kwargs):
            events.append('one submission')
            try:
                while True:await asyncio.sleep(.002)
            finally:events.append('cancelled')
    monkeypatch.setattr('sourceloom.providers.httpx.AsyncClient',Client)
    started=time.time()
    with pytest.raises(Uncertain,match='不自动重发'):
        post_before_deadline('https://example.invalid',{}, {},started+.08)
    assert time.time()-started<1 and events==['one submission','cancelled','closed']


def test_quota_continuation_rejects_uncertain_timeouts_and_same_channel(tmp_path):
    import time
    s=Store(tmp_path);q=Queue(s);p=create_demo(s)
    def response(code):return s.blob(json.dumps({'status':'failed','errorCode':code,'output':None}).encode())
    call={'id':'original-call','response_blob':response('network_timeout'),
          'wire_request_blob':s.blob(json.dumps({'task':{'executionChannel':'codex'}}).encode())}
    job={'id':'quota-job','created':time.time(),'project':p['id'],'role':'production','status':'uncertain','base_revision':p['revision'],
         'pending':'planner','calls':[call],'results':{}}
    with s.connect() as cx:
        cx.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(job['id'],p['id'],'production','uncertain',time.time(),json.dumps(job)))
        cx.execute('INSERT INTO production_control(id,project,status,created) VALUES(?,?,?,?)',(job['id'],p['id'],'uncertain',time.time()))
    with pytest.raises(Conflict):q.continue_after_quota_error(job['id'],'chatgpt_web')
    call['response_blob']=response('codex_quota_exhausted');s.put_job(job)
    with pytest.raises(Conflict):q.continue_after_quota_error(job['id'],'codex')
    resumed=q.continue_after_quota_error(job['id'],'chatgpt_web')
    assert resumed['calls']==[call] and resumed['quota_continuations']['planner']['previous_call']=='original-call'
    assert 'pending' not in resumed and s.get(p['id'])['draft']==p['draft']


def test_numbering_never_erases_dates_or_quantities_in_source_titles(tmp_path):
    p=create_demo(Store(tmp_path));p['draft']['blocks'][0]['markdown']='## 2026 年研究\n\n### 3 个条件\n'
    original=p['draft']['blocks'][0]['markdown']
    numbered=presentation(p,'numbered')
    assert numbered['draft']['blocks'][0]['markdown']==original
    assert presentation(numbered,'numbered')['draft']==numbered['draft']
    assert presentation(numbered,'none')['draft']==p['draft']
    assert presentation(p,'none')['draft']==p['draft']


def test_term_lookup_uses_official_fallback_after_connection_failure(tmp_path):
    s=Store(tmp_path);calls=[]
    def fetch(url,**kwargs):
        calls.append(url)
        if 'www.ietf.org' in url:raise ConnectionResetError('connection reset')
        return b'Internet Engineering Task Force (IETF)', 'text/plain', url
    terms=verified_terms(s,{'objects':[{'text':'IETF'}]},fetch)
    assert len(calls)==2 and terms[0]['url'].startswith('https://www.rfc-editor.org/')
    assert 'Internet Engineering Task Force' in terms[0]['quote']
    assert verified_terms(s,{'objects':[{'text':'IETF'}]},fetch)==terms and len(calls)==2
