import copy
import json
import pytest
from bs4 import BeautifulSoup
from sourceloom.media import fold_media
from sourceloom.store import Conflict
from tests.test_production import skill,prepared


def test_every_image_disclosure_is_collapsed_without_changing_its_content():
    raw='<figure><img src="assets/a" alt="Original"><figcaption>Caption</figcaption></figure><table><tr><td><img src="assets/b"></td><td>Cell</td></tr></table><details open><summary>Page</summary><img src="assets/c"></details>'
    doc=BeautifulSoup(fold_media(raw),'html.parser')
    assert len(doc.select('details[data-media]'))==3
    assert not doc.select('details[open]')
    assert [i['src'] for i in doc.select('img')]==['assets/a','assets/b','assets/c']
    assert doc.figcaption.text=='Caption' and doc.select('td')[1].text=='Cell'
    assert fold_media(str(doc))==str(doc)


def test_inline_media_does_not_create_invalid_paragraph_nesting_or_hide_surrounding_text():
    doc=BeautifulSoup(fold_media('<p><img src="a"></p><p>Before <img src="b"> after</p>'),'html.parser')
    assert not doc.select('p details')
    assert doc.select_one('details p img')['src']=='a'
    mixed=doc.select_one('.media-paragraph')
    assert mixed.contents[0]=='Before ' and mixed.contents[-1]==' after'
    assert not mixed.details.get_text(strip=True).endswith('after')
    assert fold_media(str(doc))==str(doc)


def test_linked_image_disclosure_does_not_turn_its_toggle_into_navigation():
    raw='<p>Before <a href="https://example.org/figure"><img src="a" alt="Figure"></a> after</p>'
    doc=BeautifulSoup(fold_media(raw),'html.parser')
    assert not doc.select('a summary,p details')
    assert doc.select_one('details a')['href']=='https://example.org/figure'
    assert doc.select_one('details img')['alt']=='Figure'
    assert doc.select_one('.media-paragraph').contents[0]=='Before '
    assert doc.select_one('.media-paragraph').contents[-1]==' after'


@pytest.mark.parametrize('upstream_status,output,allowed',[('running',None,False),('succeeded',{'answer':'saved'},False),('failed',{'answer':'partial'},False),('failed',None,True)])
def test_explicit_fidelity_reassessment_preserves_unknown_call_and_guards_live_results(tmp_path,skill,monkeypatch,upstream_status,output,allowed):
    from sourceloom.durable import Queue
    from sourceloom.config import load_config
    store,queue,project,bundle=prepared(tmp_path,skill)
    job=queue.enqueue(project['id'],bundle)
    call={'id':'original-call','role':'fidelity','channel':'router','upstream_id':'upstream-job','upstream_base':'https://router.example','step_key':'fidelity-0','status':'uncertain'}
    job.update(status='uncertain',stage='fidelity',pending='fidelity-0',calls=[call],draft={'blocks':[]})
    store.put_job(job)
    with store.connect() as cx:
        p=store.get(project['id']);p['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p),p['id']))
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,**kwargs):
            import httpx
            return httpx.Response(200,json={'id':'upstream-job','status':upstream_status,'output':output},request=httpx.Request('GET',url))
    monkeypatch.setattr('httpx.Client',Client)
    config=load_config()|{'base_url':'https://router.example','api_key':'synthetic'}
    if allowed:
        queue.recheck_failed_fidelity(job['id'],config)
        current=store.job(job['id'])
        assert current['calls']==[call] and current['repair_rounds']==0
        assert current['status']=='queued' and 'pending' not in current
        assert current['fidelity_reassessments'][0]['original_call']=='original-call'
        with pytest.raises(Conflict):queue.recheck_failed_fidelity(job['id'],config)
    else:
        with pytest.raises(Conflict):queue.recheck_failed_fidelity(job['id'],config)
        assert store.job(job['id'])['pending']=='fidelity-0'


def test_indexed_fidelity_covers_every_object_and_preserves_negative_judgments():
    from sourceloom.fidelity_parts import partitions,decode,merge
    from sourceloom.production import fidelity_issues
    from jsonschema import ValidationError
    source={'objects':[{'id':'s1','text':'We may use **x**.'},{'id':'s2','text':'Never use y.'}]}
    facts={'facts':[{'id':'f1','source_id':'s1'},{'id':'f2','source_id':'s2'}]}
    draft={'blocks':[{'id':'b1','markdown':'我们可以使用 x'},{'id':'b2','markdown':'可以使用 y'}]}
    original=copy.deepcopy((source,facts,draft));parts=[]
    for assigned in partitions(source,facts,draft,limit=2):
        raw={'source_checks':{sid:{'missing_information':[],'explanation':'Full object assessed'} for sid in assigned['source_ids']},
             'fact_checks':{fid:{'block_id':'b1' if fid=='f1' else 'b2','status':'preserved' if fid=='f1' else 'changed',
                 'person_preserved':True,'referents_preserved':True,'explanation':'Negation lost' if fid=='f2' else 'Same person and modality'} for fid in assigned['fact_ids']},
             'reverse_checks':{bid:{'kind':'source','source_ids':['s1' if bid=='b1' else 's2'],
                 'status':'supported' if bid=='b1' else 'unsupported','explanation':'Compared exact input'} for bid in assigned['block_ids']},'findings':[]}
        parts.append(decode(raw,assigned,source,facts,draft))
        invalid=copy.deepcopy(raw);invalid['source_checks']['invented']={'missing_information':[],'explanation':'Made up'}
        with pytest.raises(ValidationError):decode(invalid,assigned,source,facts,draft)
    review=merge(parts)
    assert review['assessed_source_ids']==['s1','s2']
    assert [row['fact_id'] for row in review['fact_checks']]==['f1','f2']
    assert review['fact_checks'][0]['source_quote']=='We may use **x**.'
    assert review['fact_checks'][1]['status']=='changed'
    assert len(fidelity_issues(review,facts,draft,source))==2
    assert original==(source,facts,draft)


@pytest.mark.parametrize('second_error,no_response',[(False,False),(True,False),(False,True)])
def test_returned_subscription_error_has_one_separate_retry_and_keeps_unknown_spending(tmp_path,skill,monkeypatch,second_error,no_response):
    import httpx
    from sourceloom.production import Production
    from sourceloom.production_contracts import TermPreparation
    from sourceloom.providers import Uncertain
    from sourceloom.config import load_config
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle);captured=[]
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,**kwargs):
            captured.append(kwargs)
            if no_response:raise httpx.ReadTimeout('Unknown delivery')
            if len(captured)==1 or second_error:return httpx.Response(500,json={'type':'error','error':{'message':'Internal server error'}})
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"terms":[]}'}}],'usage':{'prompt_tokens':100,'completion_tokens':10}})
    monkeypatch.setattr('sourceloom.providers.httpx.AsyncClient',Client)
    config=load_config()|{'provider':'openai-compatible','base_url':'https://opencode.ai/zen/go/v1','api_key':'synthetic',
        'billing_mode':'subscription','role_providers':{},'max_input_bytes':100000,'writing_skill_dir':str(skill),'structured_output':'json_object'}
    engine=Production(store,config);monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    if second_error or no_response:
        with pytest.raises(Uncertain):engine._call(job,'test-repair','term_preparation',{},TermPreparation)
    else:assert engine._call(job,'test-repair','term_preparation',{},TermPreparation)=={'terms':[]}
    assert len(captured)==(1 if no_response else 2)
    assert len({c['id'] for c in job['calls']})==len(captured)
    assert job['calls'][0]['status']=='uncertain'
    assert ('test-repair' in job.get('service_error_retries',{})) is not no_response
    with store.connect() as cx:rows=[dict(r) for r in cx.execute('SELECT * FROM spending ORDER BY created')]
    assert rows[0]['actual'] is None
    if second_error:
        with pytest.raises(Uncertain):engine._call(job,'test-repair','term_preparation',{},TermPreparation)
        assert len(captured)==2


def test_article_styles_arrive_with_body_and_only_owned_assets_are_cached(tmp_path):
    from fastapi.testclient import TestClient
    from sourceloom.app import create_app
    from sourceloom.config import load_config
    from sourceloom.export import reading_page
    doc=BeautifulSoup(reading_page('<p>Saved article</p>'),'html.parser')
    assert doc.style and not doc.select('link[rel=stylesheet]') and doc.p.text=='Saved article'
    config=load_config()|{'data_dir':str(tmp_path),'auth_mode':'local'}
    app=create_app(config);store=app.state.store;p=store.create('One');other=store.create('Other')
    key=store.blob(b'immutable image bytes')
    store.change(p['id'],lambda p:p.update(inventory={'resources':[{'id':key,'mime':'image/png'}]}))
    store.change(other['id'],lambda p:p.update(inventory={'resources':[]}))
    with TestClient(app) as client:
        result=client.get('/api/projects/'+p['id']+'/assets/'+key)
        assert result.content==b'immutable image bytes'
        assert result.headers['cache-control']=='private, max-age=31536000, immutable'
        missing=client.get('/api/projects/'+other['id']+'/assets/'+key)
        assert missing.status_code==404 and missing.headers['cache-control']=='no-store'
        assert client.get('/api/projects').headers['cache-control']=='no-store'
    protected=create_app(config|{'auth_mode':'proxy','allowed_subject':'synthetic-owner'})
    with TestClient(protected) as client:
        assert client.get('/api/projects/'+p['id']+'/assets/'+key).status_code==401


@pytest.mark.parametrize('failure',[TimeoutError('Temporary network failure'),ValueError('Rejected response')])
def test_web_fetch_retries_only_transport_failure_on_a_validated_address(monkeypatch,failure):
    from sourceloom import network
    attempts=[];closed=[]
    monkeypatch.setattr(network,'public_addresses',lambda host:['first-public-address','second-public-address'])
    class Connection:
        def __init__(self,host,address,timeout):self.address=address;attempts.append((host,address,timeout))
        def request(self,*args,**kwargs):
            if len(attempts)==1:raise failure
        def getresponse(self):return self
        status=200
        def getheader(self,*args):return 'text/plain'
        def read(self,*args):return b'Complete original'
        def close(self):closed.append(self.address)
    monkeypatch.setattr(network,'PinnedHTTPS',Connection)
    if isinstance(failure,ValueError):
        with pytest.raises(ValueError,match='Rejected'):network.fetch('https://example.org/source')
        assert len(attempts)==1
    else:
        assert network.fetch('https://example.org/source')[0]==b'Complete original'
        assert len(attempts)==2 and attempts[0][2]<=4 and attempts[1][2]<=12
    assert len(closed)==len(attempts)


def test_web_fetch_percent_encodes_literal_spaces_in_asset_path(monkeypatch):
    from sourceloom import network
    observed={}
    monkeypatch.setattr(network,'public_addresses',lambda host:['public-address'])
    class Connection:
        def __init__(self,*args):pass
        def request(self,method,path,headers=None):observed.update(method=method,path=path)
        def getresponse(self):return self
        status=200
        def getheader(self,*args):return 'image/jpeg'
        def read(self,*args):return b'image'
        def close(self):pass
    monkeypatch.setattr(network,'PinnedHTTPS',Connection)
    raw,mime,_=network.fetch('https://example.org/gallery/Named Image.jpg?size=large image',
        allowed_types={'image/jpeg'})
    assert raw==b'image' and mime=='image/jpeg'
    assert observed['path']=='/gallery/Named%20Image.jpg?size=large%20image'


def test_plan_repair_receives_only_independently_unresolved_claims(tmp_path,skill,monkeypatch):
    from sourceloom.production import Production
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    sid=job['source']['objects'][0]['id'];plan={'units':[{'id':'u'}]}
    review={'status':'needs_repair','issues':['Rejected allegation','Actual defect'],'optional_source_limits':[],'essential_missing_sources':[]}
    job.update(stage='plan_review',plan=plan,facts={'facts':[]},inventory=job['source'],unit_limit=1)
    engine=Production(store,{});monkeypatch.setattr(engine,'validate_plan',lambda job,value:value)
    def call(job,key,role,payload,schema):
        if role=='plan_review':return review
        if role=='plan_decision':return {'decisions':[{'claim_index':n,'verdict':verdict,'evidence':[{'source_id':sid}],'reason':'Compared the actual source and plan'} for n,verdict in enumerate(['not_error','confirmed_error'])]}
        assert role=='planner_repair' and payload['review']['issues']==['Actual defect']
        return plan
    monkeypatch.setattr(engine,'_call',call)
    assert engine.step(job)=='queued' and job['plan_repairs']==1
    assert job['plan_review']['issues']==['Rejected allegation','Actual defect']


@pytest.mark.parametrize('mime,url,expected',[
    ('text/plain','https://example.org/source.md','snapshot.md'),
    ('text/markdown','https://example.org/chapter','snapshot.md'),
    ('application/vnd.openxmlformats-officedocument.wordprocessingml.document','https://example.org/download','snapshot.docx')])
def test_document_links_keep_their_parseable_format(monkeypatch,mime,url,expected):
    from sourceloom import network
    monkeypatch.setattr(network,'fetch',lambda address:(b'unchanged original',mime,url))
    assert network.fetch_bundle(url)[0]==[(expected,b'unchanged original')]


@pytest.mark.parametrize('extra_status',['pass','not_applicable','fail','unknown'])
def test_unassigned_positive_review_cannot_override_assigned_verdicts(extra_status):
    from sourceloom.style_parts import decode_indexed
    from jsonschema import ValidationError
    payload={'rule_catalog':['FMT-009'],'rule_definitions':{'FMT-009':'Punctuation','FMT-121':'Names'},
        'mechanical_findings':[],'mechanical_candidates':[],
        'draft':{'blocks':[{'id':'b','markdown':'Real text'}]},
        'evidence_catalog':{'e1':{'block_id':'b','text':'Real text'}}}
    result={'findings':[{'id':'defect','severity':'error','block_id':'b','obligation_id':'',
        'message':'Keep this real defect','expected':'Fix exactly this'}],
        'rules_by_id':{'FMT-009':{'status':'fail','reason':'Actual defect','evidence_ids':['e1']},
                       'FMT-121':{'status':extra_status,'reason':'Unassigned observation','evidence_ids':['e1']}}}
    before=copy.deepcopy(result)
    if extra_status in {'fail','unknown'}:
        with pytest.raises(ValidationError):decode_indexed(result,payload)
    else:
        parsed=decode_indexed(result,payload)
        assert len(parsed['assessments'])==1 and parsed['assessments'][0]['status']=='fail'
        assert parsed['findings'][0]['message']=='Keep this real defect'
    assert result==before


def test_term_background_preserves_returned_evidence_and_reuses_shared_reference(tmp_path):
    from sourceloom.terminology import retrieve_background
    from sourceloom.store import Store
    store=Store(tmp_path);calls=[]
    url='https://docs.example.org/concepts'
    raw=b'<html><nav>Menu</nav><main>Alpha describes an identifier. Beta names its record.</main></html>'
    prepared={'terms':[{'zh':'概念甲','en':'Alpha','reference_urls':[url]},
                       {'zh':'概念乙','en':'Beta','reference_urls':[url]}]}
    def fetch(url,**options):calls.append(url);return raw,'text/html',url
    result=retrieve_background(store,prepared,fetch)
    assert len(calls)==1 and len(result)==1 and result[0]['status']=='retrieved'
    assert store.read_blob(result[0]['snapshot_blob'])==raw
    assert result[0]['terms']==['概念甲','概念乙']
    assert 'Alpha describes an identifier.' in result[0]['excerpts'][0]
    assert 'Menu' not in result[0]['excerpts'][0]
    assert retrieve_background(store,prepared,fetch)==result and len(calls)==1
    assert prepared['terms'][0]['reference_urls']==[url]


def test_failed_reference_is_not_a_verified_definition(tmp_path):
    from sourceloom.terminology import retrieve_background
    from sourceloom.store import Store
    def denied(url,**options):raise ValueError('Rejected address')
    prepared={'terms':[{'zh':'概念','en':'Concept','reference_urls':['http://127.0.0.1/private']}]}
    result=retrieve_background(Store(tmp_path),prepared,denied)
    assert result[0]['status']=='lookup_failed' and 'excerpts' not in result[0]


def test_repair_review_receives_actual_changes_only_when_saved_files_match(tmp_path):
    from sourceloom.review_context import execution_evidence
    from sourceloom.store import digest
    before=b'Exact old text';after=b'Exact new text'
    changes=[{'node_id':'LINE-0001','old_text':before.decode(),'new_text':after.decode(),
              'scope':'sentence','reason':'Confirmed local defect'}]
    job={'repair_rounds':1,'repair_commits':[{'round':1,'before':digest(before),'after':digest(after)}],
         'draft':{'blocks':[{'markdown':after.decode()}]}}
    assert execution_evidence(job,{})['local-committer']['status']=='unknown'
    folder=tmp_path/'repair-1';folder.mkdir();(folder/'before.md').write_bytes(before);(folder/'after.md').write_bytes(after)
    (folder/'patch.json').write_text(json.dumps({'document_sha256':digest(before),'edits':changes}),encoding='utf8')
    record=execution_evidence(job,{},tmp_path)['local-committer']
    assert record['status']=='pass' and record['commits'][0]['changes']==changes
    assert 'changes' not in job['repair_commits'][0]
    altered=copy.deepcopy(changes);altered[0]['new_text']='An invented change'
    (folder/'patch.json').write_text(json.dumps({'document_sha256':digest(before),'edits':altered}),encoding='utf8')
    assert execution_evidence(job,{},tmp_path)['local-committer']['status']=='unknown'
    (folder/'patch.json').write_text(json.dumps({'document_sha256':digest(before),'edits':changes}),encoding='utf8')
    (folder/'after.md').write_bytes(b'A different output')
    assert execution_evidence(job,{},tmp_path)['local-committer']['status']=='unknown'


def test_user_authorized_bounded_repair_limit_is_visible_to_style_review():
    from sourceloom.review_context import execution_evidence
    job={'repair_rounds':4,'repair_commits':[],'draft':{'blocks':[{'markdown':'Candidate'}]}}
    default=execution_evidence(job,{})['repair-limit']
    authorized=execution_evidence(job,{},repair_limit=4)['repair-limit']
    assert default['status']=='fail' and default['limit']==2
    assert authorized['status']=='pass' and authorized['limit']==4
    assert 'explicitly authorized' in authorized['authority']


def test_one_missing_json_separator_recovers_only_complete_object():
    from sourceloom.providers import parse_json
    assert parse_json('{"a":1 "b":{"c":2}}')=={'a':1,'b':{'c':2}}
    assert parse_json('{"rows":[{"id":1},"}{"id":2}]}')=={'rows':[{'id':1},{'id':2}]}
    assert parse_json('{"text":"保留"长期支持"原文"}')=={'text':'保留"长期支持"原文'}
    with pytest.raises(json.JSONDecodeError):parse_json('{"a":1 "b":2 "c":3}')


def test_group_level_fidelity_contract_failure_reassesses_only_that_partition():
    from sourceloom.fidelity_parts import invalid_assignments
    assigned={'source_ids':['s1','s2'],'fact_ids':['f1'],'block_ids':['b1']}
    source={'objects':[{'id':'s1','text':'one'},{'id':'s2','text':'two'}]}
    draft={'blocks':[{'id':'b1','markdown':'candidate'}]}
    malformed={'source_checks':{'missing_information':['wrong shape'],'explanation':'wrong'},
        'fact_checks':{},'reverse_checks':{},'findings':[],'notes':'extra'}
    retained,missing=invalid_assignments(malformed,assigned,source,draft)
    assert retained=={'source_checks':{},'fact_checks':{},'reverse_checks':{},'findings':[]}
    assert missing==assigned


def test_missing_contract_keys_preserve_valid_reassessment_rows():
    from sourceloom.fidelity_parts import invalid_assignments,merge_raw_reassessment
    assigned={'source_ids':['s1'],'fact_ids':['f1'],'block_ids':['b1']}
    source={'objects':[{'id':'s1','text':'one'}]};draft={'blocks':[{'id':'b1','markdown':'candidate'}]}
    partial={'source_checks':{},
        'fact_checks':{'f1':{'status':'preserved','block_id':'b1','person_preserved':True,
            'referents_preserved':True,'explanation':'kept'}},
        'reverse_checks':{'b1':{'kind':'source','status':'supported','source_ids':['s1'],'explanation':'kept'}},
        'findings':[]}
    retained,missing=invalid_assignments(partial,assigned,source,draft)
    assert missing=={'source_ids':['s1'],'fact_ids':[],'block_ids':[]}
    final={'source_checks':{'s1':{'missing_information':[],'explanation':'checked'}},
        'fact_checks':{},'reverse_checks':{},'findings':[]}
    combined=merge_raw_reassessment(retained,final,assigned,source,draft)
    assert list(combined['source_checks'])==['s1'] and list(combined['fact_checks'])==['f1']


def test_layout_repair_can_replace_only_unknown_type_label_without_losing_text():
    from sourceloom.writing import validate_layout_repair
    base={'encoding':'flat_nodes_v1','blocks':[{'id':'b','unit_id':'u','kind':'explanation',
        'content':[{'type':'list_node_placeholder_none','node_id':'n','parent_id':'','text':'Exact retained text'}],
        'obligation_ids':[],'object_ids':[],'evidence':[]}]}
    fixed=copy.deepcopy(base);fixed['blocks'][0]['content'][0].update(type='paragraph',parent_id='another-structural-parent')
    assert validate_layout_repair(base,fixed)==fixed
    changed=copy.deepcopy(fixed);changed['blocks'][0]['content'][0]['text']='Changed'
    with pytest.raises(ValueError):validate_layout_repair(base,changed)


def test_compose_preserves_nested_heading_only_chain_for_completion(tmp_path,skill):
    from sourceloom.writing import compose
    (skill/'runtime').mkdir()
    (skill/'runtime'/'composition.py').write_text(
        "def _text(value, location): return value.strip().rstrip('。；')\n"
        "def render_document(*args, **kwargs): raise AssertionError('heading-only path expected')\n",
        encoding='utf-8')
    store,queue,p,bundle=prepared(tmp_path,skill)
    response={'encoding':'flat_nodes_v1','blocks':[{'id':'b','unit_id':'u','kind':'explanation',
        'obligation_ids':['f1'],'object_ids':['h1'],'evidence':[{'source_id':'h1','quote':'Top'}],
        'content':[{'type':'section','node_id':'s1','parent_id':'','heading':'顶层'},
                   {'type':'section','node_id':'s2','parent_id':'s1','heading':'下层'}]}]}
    inventory={'objects':[{'id':'h1','kind':'heading','text':'Top','raw':'Top'}],
               'obligations':[{'id':'f1','object_id':'h1'}]}
    result=compose(bundle,response,inventory)
    assert result['blocks'][0]['markdown']=='## 顶层\n\n### 下层'


def test_compose_binds_exact_text_when_evidence_quotes_inner_html(tmp_path,skill):
    from sourceloom.writing import compose
    (skill/'runtime').mkdir()
    (skill/'runtime'/'composition.py').write_text(
        "def _text(value, location): return value.strip().rstrip('。；')\n"
        "def render_document(doc, sources=None): return '\\n\\n'.join(x['text'] for x in doc['blocks'])+'\\n'\n",
        encoding='utf-8')
    store,queue,p,bundle=prepared(tmp_path,skill)
    quote='Keep <em>only</em> this'
    response={'encoding':'flat_nodes_v1','blocks':[{'id':'b','unit_id':'u','kind':'explanation',
        'obligation_ids':['f1'],'object_ids':['s1'],'evidence':[{'source_id':'s1','quote':quote}],
        'content':[{'type':'paragraph','node_id':'p','parent_id':'','text':'保留这项要求'}]}]}
    inventory={'objects':[{'id':'s1','kind':'text','text':'Keep only this','raw':'<p>'+quote+'</p>'}],
               'obligations':[{'id':'f1','object_id':'s1'}]}
    result=compose(bundle,response,inventory)
    assert result['blocks'][0]['evidence']==[{'source_id':'s1','quote':'Keep only this'}]


def test_claim_ignores_stale_control_row_and_finish_cannot_change_active_project(tmp_path,skill):
    from sourceloom.durable import Queue
    from sourceloom.store import Store
    store,queue,p,bundle=prepared(tmp_path,skill)
    current=queue.enqueue(p['id'],bundle)
    stale=dict(current,id='stale',created=current['created']-1,status='queued')
    with store.connect() as cx:
        cx.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',
                   (stale['id'],p['id'],'production','queued',stale['created'],json.dumps(stale)))
        cx.execute('INSERT INTO production_control(id,project,status,created) VALUES(?,?,?,?)',
                   (stale['id'],p['id'],'queued',stale['created']))
    claimed=queue.claim('worker')
    assert claimed['id']==current['id']
    queue.finish(claimed,'worker','queued')
    stale['worker_owner']='old-worker'
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='running',owner='old-worker' WHERE id='stale'")
    queue.finish(stale,'old-worker','failed')
    project=store.get(p['id'])
    assert project['active_job']==current['id'] and project['state']=='queued'


def test_style_stage_uses_configured_repair_limit(tmp_path,skill,monkeypatch):
    from sourceloom.production import Production
    from sourceloom.checks import freeze
    from sourceloom.review_context import execution_evidence as real_evidence
    store,queue,p,bundle=prepared(tmp_path,skill)
    job=queue.enqueue(p['id'],bundle)
    saved={}
    def capture(job,bundle,root,limit):
        saved['limit']=limit
        return real_evidence(job,bundle,root,limit)
    monkeypatch.setattr('sourceloom.review_context.execution_evidence',capture)
    monkeypatch.setattr('sourceloom.production.scan',lambda *args:{'format':{'findings':[],'candidates':[]}})
    engine=Production(store,{'max_repair_rounds':4})
    job.update(stage='style',status='running',inventory=freeze(job['source']),
        facts={'facts':[]},plan={'units':[]},draft={'blocks':[{'id':'b','unit_id':'u','kind':'prose','markdown':'Candidate'}]},
        results={},repair_rounds=4,repair_commits=[])
    monkeypatch.setattr(engine,'_call',lambda *args,**kwargs:{'findings':[],'rules_by_id':{},'checks_by_id':{}})
    # The call may stop on the deliberately minimal style fixture after evidence
    # creation; only the configured authority handoff is under test here.
    try: engine.step(job)
    except (ValueError,KeyError): pass
    assert saved['limit']==4


def test_known_quality_failure_starts_only_one_separate_generation(tmp_path,skill,monkeypatch):
    from sourceloom.production import Production
    from sourceloom.checks import freeze
    store,queue,p,bundle=prepared(tmp_path,skill);original=queue.enqueue(p['id'],bundle)
    engine=Production(store,{'quality_fallback_providers':{'writer':{'provider':'router'}}})
    def stopped(job):
        inventory=freeze(job['source']);inventory['inventory_review']={'passed':True}
        job.update(stage='fidelity',repair_rounds=2,inventory=inventory,facts={'facts':[{'id':'f'}]},
            plan={'units':[]},draft={'blocks':[{'id':'b','markdown':'Saved candidate'}]},
            quality_issues=['An actual unresolved defect'],calls=[{'id':'old-call','status':'completed'}])
        return 'needs_attention'
    monkeypatch.setattr(engine,'step',stopped)
    assert engine.run_once()
    old=store.job(original['id']);current=store.get(p['id']);next_job=store.job(current['active_job'])
    assert old['status']=='needs_attention' and old['repair_rounds']==2 and old['calls'][0]['id']=='old-call'
    assert next_job['quality_fallback_of']==old['id'] and next_job['prior_repair_rounds']==2
    assert next_job['repair_rounds']==0 and next_job['calls']==[]
    assert current['draft']==old['draft'] and current['budget_usd']==p['budget_usd']
    assert engine.run_once()
    final=store.get(p['id']);assert final['active_job'] is None
    assert store.job(next_job['id'])['status']=='needs_attention'
    with pytest.raises(Conflict):queue.rewrite_existing(p['id'],bundle,quality_parent=next_job['id'])


def test_returned_optional_reference_lists_are_not_review_verdicts_or_new_requests():
    from sourceloom.providers import recover_reference_lists,strict_schema
    from sourceloom.production_contracts import TermPreparation
    term={'zh':'概念','en':'Concept','abbr':'','source_ids':['s'],
          **{k:'Original prepared explanation' for k in ['what','purpose','mechanism','when','boundary']}}
    body={'status':'failed','errorCode':'validation_failed','webExecution':{'observed':True},
          'validation':{'messages':["schema:/terms/0:must have required property 'reference_urls'"],'testsFailed':0},
          'output':{'terms':[term]}}
    schema=strict_schema(TermPreparation.model_json_schema());before=copy.deepcopy(body)
    result=recover_reference_lists(body,schema,'term_preparation')
    assert result=={'terms':[term|{'reference_urls':[]}]}
    assert body==before and recover_reference_lists(body,schema,'fidelity') is None
    for change in [{'validation':{'messages':["schema:/terms/0:must have required property 'source_ids'"]}},
                   {'output':{'terms':[]}}, {'status':'running'},
                   {'validation':body['validation']|{'testsFailed':1}}]:
        assert recover_reference_lists(body|change,schema,'term_preparation') is None
    incomplete=copy.deepcopy(body);del incomplete['output']['terms'][0]['mechanism']
    assert recover_reference_lists(incomplete,schema,'term_preparation') is None


def test_recovery_uses_original_quality_channel_without_resubmitting(tmp_path,monkeypatch):
    from sourceloom.providers import Provider
    from sourceloom.store import Store
    import httpx
    store=Store(tmp_path);seen=[]
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,**kwargs):
            seen.append((url,kwargs['headers']['Authorization']))
            return httpx.Response(200,json={'status':'succeeded','output':{'terms':[]}},request=httpx.Request('GET',url))
    monkeypatch.setattr('httpx.Client',Client)
    monkeypatch.setattr(store,'put_job',lambda job:None)
    job={'quality_fallback_of':'prior','calls':[{'role':'term_preparation','channel':'router',
        'upstream_id':'existing','upstream_base':'https://review.example','status':'uncertain'}]}
    config={'provider':'openai-compatible','base_url':'https://primary.example','api_key':'primary',
        'role_providers':{'term_preparation':{'model':'primary-model'}},
        'quality_fallback_providers':{'term_preparation':{'provider':'router','base_url':'https://review.example','api_key':'review'}}}
    assert Provider(store,config).recover(job)=={'terms':[]}
    assert seen==[('https://review.example/api/v1/jobs/existing','Bearer review')]
    assert len(job['calls'])==1 and job['calls'][0]['status']=='recovered'


def test_writer_fact_table_preserves_complete_rows_and_original_payload():
    from sourceloom.providers import compact_review_tables
    facts=[{'id':str(i),'meaning':'An exact original assertion','conditions':['Only if x'],
            'negations':['Never y'],'pronouns':['we'],'quote':'Exact quoted text'} for i in range(12)]
    original={'facts':facts,'other':'unchanged'};before=copy.deepcopy(original)
    result=compact_review_tables(original);table=result['facts']
    assert [dict(zip(table['columns'],row)) for row in table['rows']]==facts
    assert original==before and result['other']=='unchanged'
    assert compact_review_tables(result)==result


def test_style_context_is_exactly_reconstructible_with_duplicate_lines_and_endings():
    from sourceloom.providers import compact_style_context
    from sourceloom.style_parts import evidence_catalog
    draft={'blocks':[{'id':'b1','markdown':'## Title\r\n\r\nRepeated\nRepeated\n'},
                     {'id':'b2','markdown':'Final line without newline'}]}
    payload={'draft':draft,'evidence_catalog':evidence_catalog(draft),
        'rule_definitions':{'FMT-001':{'file':'rules.md','text':'Exact rule'},
                            'FORMULA-001':{'file':'formulas.md','text':'Retain this mapped bullet'}}}
    before=copy.deepcopy(payload);result=compact_style_context(payload,'- `FMT-001` Exact rule')
    assert payload==before and result['rule_definitions']['FMT-001']['skill_rule_id']=='FMT-001'
    assert result['rule_definitions']['FORMULA-001']==payload['rule_definitions']['FORMULA-001']
    for original,encoded in zip(draft['blocks'],result['draft']['blocks']):
        assert ''.join(row[1] for row in encoded['markdown_lines'])==original['markdown']
    for eid,ref in result['evidence_catalog'].items():
        block=next(b for b in result['draft']['blocks'] if b['id']==ref['block_id'])
        row=block['markdown_lines'][ref['line']-1]
        assert row[0]==eid and row[1].splitlines()[0]==payload['evidence_catalog'][eid]['text']
    assert compact_style_context(payload,'No matching rule')['rule_definitions']==payload['rule_definitions']


def test_style_review_gets_whole_candidate_and_every_displayed_source_literal_without_duplicate_full_source():
    from sourceloom.production import style_review_payload
    source={'objects':[{'id':'s1','kind':'link','text':'Example','target':'https://example.com'}]}
    draft={'blocks':[{'id':'b1','unit_id':'u1','kind':'prose','markdown':'Before [Example](https://example.com) after'}]}
    job={'draft':draft,'verified_terminology':[],'terminology_background':{}}
    report={'format':{'findings':[],'candidates':[]},'execution_evidence':{'skill-delivery':{'status':'pass'}}}
    payload=style_review_payload(job,source,{'FMT-001':{'file':'rules','text':'Read all'}},report)
    assert 'source' not in payload
    assert payload['draft']['blocks'][0]['markdown']==draft['blocks'][0]['markdown']
    assert payload['protected_originals']==[{'source_id':'s1','kind':'link','literal':'[Example](https://example.com)','block_ids':['b1']}]
    assert payload['evidence_catalog']['e1-1']['text']==draft['blocks'][0]['markdown']


def test_large_fidelity_schema_retains_all_required_verdicts_with_shared_rules():
    from sourceloom.fidelity_parts import FidelitySchema
    from sourceloom.providers import strict_schema
    from jsonschema import validate,ValidationError
    assigned={'source_ids':[],'fact_ids':['fact-'+str(i) for i in range(45)],'block_ids':[]}
    schema=strict_schema(FidelitySchema(assigned,{'objects':[{'id':'source'}]},
        {'blocks':[{'id':'block-'+str(i)} for i in range(50)]}).model_json_schema())
    assert len(json.dumps(schema))<15000
    result={'source_checks':{},'fact_checks':{fid:{'block_id':'block-0','status':'unknown',
        'person_preserved':False,'referents_preserved':False,'explanation':'Insufficient evidence'}
        for fid in assigned['fact_ids']},'reverse_checks':{},'findings':[]}
    validate(result,schema)
    for changed in ['missing','unknown_id','bad_verdict']:
        invalid=copy.deepcopy(result)
        if changed=='missing':del invalid['fact_checks']['fact-44']
        elif changed=='unknown_id':invalid['fact_checks']['fact-44']['block_id']='invented'
        else:invalid['fact_checks']['fact-44']['status']='assumed'
        with pytest.raises(ValidationError):validate(invalid,schema)


def test_partial_returned_style_review_keeps_failures_and_requires_missing_verdicts():
    from sourceloom.providers import recover_partial_style_review,strict_schema
    from jsonschema import validate,ValidationError
    row={'type':'object','properties':{'status':{'enum':['pass','fail','unknown']}},'required':['status'],'additionalProperties':False}
    schema=strict_schema({'type':'object','properties':{'findings':{'type':'array','items':{'type':'string'}},
        'rules_by_id':{'type':'object','properties':{'a':row,'b':row},'required':['a','b'],'additionalProperties':False}},
        'required':['findings','rules_by_id'],'additionalProperties':False})
    body={'status':'failed','errorCode':'validation_failed','webExecution':{'observed':True},
          'output':{'findings':['Actual defect'],'rules_by_id':{'a':{'status':'fail'}}}}
    before=copy.deepcopy(body);retained=recover_partial_style_review(body,schema,'style')
    assert retained==body['output'] and body==before
    assert recover_partial_style_review(body,schema,'style__fallback')==body['output']
    with pytest.raises(ValidationError):validate(retained,schema)
    for output in [{'rules_by_id':{}}, {'findings':[],'rules_by_id':{'a':{}}},
                   {'findings':[],'rules_by_id':{'unexpected':{'status':'pass'}}}]:
        assert recover_partial_style_review(body|{'output':output},schema,'style') is None
    for status in ['running','expired']:
        assert recover_partial_style_review(body|{'status':status},schema,'style') is None
    assert recover_partial_style_review(body,schema,'writer') is None
    refusal=body|{'output':'Unable to return this assignment in one response',
                  'validation':{'messages':['schema:/:must be object']}}
    marker=recover_partial_style_review(refusal,schema,'style')
    assert marker=={'_incomplete_style_review':True}
    with pytest.raises(ValidationError):validate(marker,schema)


def test_returned_style_refusal_splits_once_without_rewriting_or_skipping_checks(tmp_path,skill,monkeypatch):
    from sourceloom.production import Production
    from sourceloom.style_parts import decode_indexed
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    payload={'rule_catalog':['rule-'+str(i) for i in range(41)],'rule_definitions':{},
        'mechanical_findings':[],'mechanical_candidates':[]}
    engine=Production(store,{});calls=[]
    def call(job,key,role,part,schema):
        calls.append((key,list(part['rule_catalog'])))
        if key=='review':return {'_incomplete_style_review':True}
        return {'findings':[],'rules_by_id':{rid:{'status':'unknown','reason':'No evidence',
            'block_ids':[],'quotes':[],'execution_ids':[]} for rid in part['rule_catalog']}}
    monkeypatch.setattr(engine,'_call',call)
    result=engine._indexed_style_result(job,'review',payload)
    assert set(result['rules_by_id'])==set(payload['rule_catalog'])
    assert [len(ids) for key,ids in calls]==[41,20,20,1]
    assert all(r['status']=='unknown' for r in result['rules_by_id'].values())
    assert len(decode_indexed(result,payload)['assessments'])==41
    calls.clear()
    with pytest.raises(ValueError):engine._indexed_style_result(job,'review',payload|{'rule_catalog':['one']})
    assert len(calls)==1


@pytest.mark.parametrize('status,output,allowed',[('running',None,False),('failed',{'partial':'retain'},False),('failed',None,True)])
@pytest.mark.parametrize('role',['writer','term_preparation'])
def test_writer_channel_continuation_preserves_saved_units_and_unknown_original(tmp_path,skill,monkeypatch,status,output,allowed,role):
    import httpx
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    call={'id':'returned-job','role':role,'channel':'router','upstream_id':'upstream',
          'upstream_base':'https://review.example','step_key':'writer-u2','status':'uncertain'}
    saved={'blocks':[{'id':'u1-b1','markdown':'Already saved exact text'}]}
    job.update(status='uncertain',stage='writer',quality_fallback_of='prior',pending='writer-u2',
               calls=[call],draft=saved,unit_index=1,repair_rounds=1)
    if role=='term_preparation':
        job.update(writer_use_primary=True,content_generation=1,
            writer_reassessments=[{'original_call':'prior-writer'}],
            term_preparation_reassessments=[{'original_call':'different-failed-term'}])
    store.put_job(job)
    with store.connect() as cx:
        p=store.get(p['id']);p['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p),p['id']))
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,**kwargs):return httpx.Response(200,json={'id':'upstream','status':status,'output':output},request=httpx.Request('GET',url))
    monkeypatch.setattr('httpx.Client',Client)
    config={'provider':'openai-compatible','base_url':'https://primary.example','api_key':'synthetic','call_timeout':10,
        'resume_failed_chat_writer_with_primary':True,
        'quality_fallback_providers':{role:{'provider':'router','base_url':'https://review.example'}}}
    if not allowed:
        with pytest.raises(Conflict):queue.recheck_failed_fidelity(job['id'],config,role=role)
        assert store.job(job['id'])['pending']=='writer-u2'
        return
    queue.recheck_failed_fidelity(job['id'],config,role=role);actual=store.job(job['id'])
    assert actual['draft']==saved and actual['unit_index']==1 and actual['repair_rounds']==1
    assert actual['calls']==[call] and actual['writer_use_primary'] and actual['content_generation']==1
    assert actual[role+'_reassessments'][-1]['original_step']=='writer-u2'
    with pytest.raises(Conflict):queue.recheck_failed_fidelity(job['id'],config,role=role)


@pytest.mark.parametrize('status,output,allowed',[('running',None,False),('failed',{'partial':'retain'},False),('failed',None,True)])
@pytest.mark.parametrize('role,stage',[('style','style'),('line_repair','repair')])
def test_style_channel_continuation_uses_primary_for_only_the_failed_assignment(tmp_path,skill,monkeypatch,status,output,allowed,role,stage):
    import httpx
    from sourceloom.production import Production
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    call={'id':'failed-style-call','role':role,'channel':'router','upstream_id':'upstream',
          'upstream_base':'https://review.example','step_key':'style-0-part-2','status':'uncertain'}
    saved={'blocks':[{'id':'b1','markdown':'Saved exact text'}]}
    job.update(status='uncertain',stage=stage,quality_fallback_of='prior',pending='style-0-part-2',
               calls=[call],draft=saved)
    store.put_job(job)
    with store.connect() as cx:
        p=store.get(p['id']);p['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p),p['id']))
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,**kwargs):return httpx.Response(200,json={'id':'upstream','status':status,'output':output},request=httpx.Request('GET',url))
    monkeypatch.setattr('httpx.Client',Client)
    config={'provider':'openai-compatible','base_url':'https://primary.example','api_key':'synthetic','call_timeout':10,
        'resume_failed_chat_review_with_primary':True,
        'quality_fallback_providers':{role:{'provider':'router','base_url':'https://review.example'}}}
    if not allowed:
        with pytest.raises(Conflict):queue.recheck_failed_fidelity(job['id'],config,role=role)
        assert store.job(job['id'])['pending']=='style-0-part-2'
        return
    queue.recheck_failed_fidelity(job['id'],config,role=role);actual=store.job(job['id'])
    assert actual['draft']==saved and actual['calls']==[call]
    assert actual['primary_continuation_steps']==['style-0-part-2']
    assert actual[role+'_reassessments'][0]['original_call']=='failed-style-call'
    engine=Production(store,config);seen=[]
    def fake_call(self,pid,role,payload,schema,current,cancelled):
        seen.append(self.config['base_url']);return {'findings':[]}
    monkeypatch.setattr('sourceloom.providers.Provider.call',fake_call)
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    schema=type('Schema',(),{'model_json_schema':staticmethod(lambda:{})})
    engine._call(actual,'style-0-part-2',role,{},schema)
    assert seen==['https://primary.example']


def test_failed_independent_fidelity_can_continue_on_primary_without_changing_candidate(tmp_path,skill,monkeypatch):
    import httpx
    from sourceloom.production import Production
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    call={'id':'failed-fidelity-call','role':'fidelity','channel':'router','upstream_id':'upstream',
          'upstream_base':'https://review.example','step_key':'fidelity-0-indexed-1','status':'uncertain'}
    saved={'blocks':[{'id':'b1','markdown':'Saved exact text'}]}
    job.update(status='uncertain',stage='fidelity',quality_fallback_of='prior',pending='fidelity-0-indexed-1',
               calls=[call],draft=saved)
    store.put_job(job)
    with store.connect() as cx:
        p=store.get(p['id']);p['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p),p['id']))
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,**kwargs):return httpx.Response(200,json={'id':'upstream','status':'failed','output':None},request=httpx.Request('GET',url))
    monkeypatch.setattr('httpx.Client',Client)
    config={'provider':'openai-compatible','base_url':'https://primary.example','api_key':'synthetic','call_timeout':10,
        'resume_failed_chat_review_with_primary':True,
        'role_providers':{'fidelity':{'provider':'router','base_url':'https://review.example'}}}
    queue.recheck_failed_fidelity(job['id'],config,role='fidelity');actual=store.job(job['id'])
    assert actual['draft']==saved and actual['primary_base_steps']==['fidelity-0-indexed-1']
    assert actual['primary_base_roles']==['fidelity']
    engine=Production(store,config);seen=[]
    def fake_call(self,pid,role,payload,schema,current,cancelled):
        seen.append(self.config['base_url']);return {'findings':[]}
    monkeypatch.setattr('sourceloom.providers.Provider.call',fake_call)
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    schema=type('Schema',(),{'model_json_schema':staticmethod(lambda:{})})
    engine._call(actual,'fidelity-0-reassessment-1-indexed-1','fidelity',{},schema)
    engine._call(actual,'fidelity-0-reassessment-1-indexed-2','fidelity',{},schema)
    assert seen==['https://primary.example','https://primary.example']


def test_unqueryable_subscription_gets_one_distinct_continuation_per_logical_step(tmp_path,skill):
    import time
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    saved={'blocks':[{'id':'b1','markdown':'Saved exact text'}]}
    call={'id':'unknown-primary-call','role':'term_preparation','channel':'openai-compatible',
        'upstream_base':'https://opencode.ai/zen/go/v1','step_key':'terms-u6-primary','status':'submitted',
        'dispatch_started':True,'deadline_at':time.time()-1}
    job.update(status='uncertain',stage='writer',quality_fallback_of='prior',writer_use_primary=True,
        pending='terms-u6-primary',calls=[call],draft=saved,unit_index=5)
    store.put_job(job)
    with store.connect() as cx:
        p=store.get(p['id']);p['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p),p['id']))
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
    config={'provider':'openai-compatible','base_url':'https://opencode.ai/zen/go/v1','billing_mode':'subscription',
        'resume_unqueryable_subscription_once':True,
        'role_providers':{'term_preparation':{'provider':'openai-compatible','base_url':'https://opencode.ai/zen/go/v1','billing_mode':'subscription'}}}
    result=queue.continue_unqueryable_subscription(job['id'],config);actual=store.job(job['id'])
    assert result['status']=='queued' and actual['draft']==saved and actual['unit_index']==5
    assert actual['calls']==[call] and 'pending' not in actual
    assert actual['unqueryable_subscription_continuations'][0]['original_call']=='unknown-primary-call'
    replacement=call|{'id':'another-call'};actual.update(status='uncertain',pending='terms-u6-primary',calls=[call,replacement])
    store.put_job(actual)
    with store.connect() as cx:
        p=store.get(p['id']);p['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p),p['id']))
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
    with pytest.raises(Conflict):queue.continue_unqueryable_subscription(job['id'],config)


def test_unqueryable_router_without_identity_gets_one_primary_continuation(tmp_path,skill):
    from sourceloom.production import Production
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    saved={'blocks':[{'id':'b1','markdown':'Saved exact text'}]}
    call={'id':'unknown-router-call','role':'style','channel':'router','upstream_base':'https://review.example',
        'step_key':'style-0-part-3','status':'uncertain','dispatch_started':True}
    job.update(status='uncertain',stage='style',quality_fallback_of='prior',pending='style-0-part-3',
        calls=[call],draft=saved,unit_index=6)
    store.put_job(job)
    with store.connect() as cx:
        p=store.get(p['id']);p['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p),p['id']))
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
    config={'provider':'openai-compatible','base_url':'https://primary.example','api_key':'synthetic','call_timeout':10,
        'resume_unqueryable_router_once':True,
        'role_providers':{'style':{'provider':'openai-compatible','base_url':'https://primary.example'}},
        'quality_fallback_providers':{'style':{'provider':'router','base_url':'https://review.example'}}}
    result=queue.continue_unqueryable_router(job['id'],config);actual=store.job(job['id'])
    assert result['status']=='queued' and actual['draft']==saved and actual['unit_index']==6
    assert actual['calls']==[call] and 'pending' not in actual
    assert actual['primary_continuation_steps']==['style-0-part-3']
    assert actual['unqueryable_router_continuations'][0]['original_call']=='unknown-router-call'
    engine=Production(store,config);seen=[]
    def fake_call(self,pid,role,payload,schema,current,cancelled):
        seen.append(self.config['base_url']);return {'findings':[]}
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr('sourceloom.providers.Provider.call',fake_call)
        monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
        schema=type('Schema',(),{'model_json_schema':staticmethod(lambda:{})})
        engine._call(actual,'style-0-part-3','style',{},schema)
    finally:monkeypatch.undo()
    assert seen==['https://primary.example']
    actual.update(status='uncertain',pending='style-0-part-3',calls=[call,call|{'id':'another'}])
    store.put_job(actual)
    with store.connect() as cx:
        p=store.get(p['id']);p['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p),p['id']))
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
    with pytest.raises(Conflict):queue.continue_unqueryable_router(job['id'],config)


def test_quality_fidelity_local_input_rejection_retries_on_primary_without_new_review(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    saved={'blocks':[{'id':'b1','markdown':'Saved exact text'}]};step='fidelity-0-indexed-1'
    call={'id':'local-reject','role':'fidelity','channel':'router','step_key':step,
        'status':'rejected','dispatch_started':False}
    job.update(status='failed',stage='fidelity',quality_fallback_of='prior',pending=step,calls=[call],draft=saved)
    store.put_job(job)
    with store.connect() as cx:
        p=store.get(p['id']);p['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p),p['id']))
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)',
            ('local-reject',p['id'],0,0,1,json.dumps({'status':'rejected','reason':'local_preflight_before_dispatch'})))
    resumed=queue.retry_validation(job['id'])
    assert resumed['status']=='queued' and resumed['draft']==saved and 'pending' not in resumed
    assert resumed['primary_base_steps']==[step] and resumed['primary_base_roles']==['fidelity']


def test_quality_style_input_rejection_routes_remaining_partitions_to_primary(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    step='style-2-part-6-indexed-v2'
    call={'id':'style-local-reject','role':'style','channel':'router','step_key':step,
        'status':'rejected','dispatch_started':False}
    job.update(status='failed',stage='style',quality_fallback_of='prior',pending=step,calls=[call],
        draft={'blocks':[{'id':'b1','markdown':'Saved exact text'}]})
    store.put_job(job)
    with store.connect() as cx:
        current=store.get(p['id']);current['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(current),p['id']))
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)',
            ('style-local-reject',p['id'],0,0,1,json.dumps({'status':'rejected','reason':'local_preflight_before_dispatch'})))
    resumed=queue.retry_validation(job['id'])
    assert resumed['primary_continuation_steps']==[step]
    assert resumed['primary_continuation_roles']==['style']


@pytest.mark.parametrize('role',['writer','writer__fallback'])
def test_quality_regeneration_writer_input_rejection_routes_to_primary(tmp_path,skill,role):
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    step='writer-unit-1-revision-1'
    call={'id':'writer-local-reject','role':role,'channel':'router','step_key':step,
        'status':'rejected','dispatch_started':False}
    job.update(status='failed',stage='writer',quality_fallback_of='prior',pending=step,calls=[call],
        draft={'blocks':[{'id':'b1','markdown':'Saved exact text'}]})
    store.put_job(job)
    with store.connect() as cx:
        current=store.get(p['id']);current['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(current),p['id']))
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)',
            ('writer-local-reject',p['id'],0,0,1,json.dumps({'status':'rejected','reason':'local_preflight_before_dispatch'})))
    resumed=queue.retry_validation(job['id'])
    assert resumed['writer_use_primary'] and resumed['primary_continuation_steps']==[step]
    assert resumed['primary_continuation_roles']==['writer']


def test_quality_line_repair_input_rejection_routes_all_later_repairs_to_primary(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    step='line_repair-13';saved={'blocks':[{'id':'b','markdown':'saved'}]}
    call={'id':'line-local-reject','role':'line_repair','channel':'router','step_key':step,
        'status':'rejected','dispatch_started':False}
    job.update(status='failed',stage='repair',pending=step,calls=[call],draft=saved,
        quality_fallback_of='old')
    store.put_job(job)
    with store.connect() as cx:
        current=store.get(p['id']);current['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(current),p['id']))
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)',
            ('line-local-reject',p['id'],0,0,1,json.dumps({'status':'rejected','reason':'local_preflight_before_dispatch'})))
    resumed=queue.retry_validation(job['id'])
    assert resumed['primary_continuation_steps']==[step]
    assert resumed['primary_continuation_roles']==['line_repair']


def test_worker_automatically_continues_zero_dispatch_quality_rejection(tmp_path,skill,monkeypatch):
    from sourceloom.production import Production
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    job['quality_fallback_of']='prior-job';store.put_job(job)
    engine=Production(store,{'data_dir':str(tmp_path)})
    def reject_before_dispatch(claimed):
        step='style-1-part-2-indexed-v2'
        call={'id':'automatic-local-reject','role':'style','channel':'router','step_key':step,
              'status':'rejected','dispatch_started':False}
        claimed.update(stage='style',pending=step,calls=[call],error='local input limit')
        with store.connect() as cx:
            cx.execute('INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)',
                (call['id'],p['id'],0,0,1,json.dumps({'status':'rejected','reason':'local_preflight_before_dispatch'})))
        raise ValueError('local input limit')
    monkeypatch.setattr(engine,'step',reject_before_dispatch)
    assert engine.run_once()
    resumed=store.job(job['id'])
    assert resumed['status']=='queued' and 'pending' not in resumed
    assert resumed['primary_continuation_steps']==['style-1-part-2-indexed-v2']
    assert resumed['primary_continuation_roles']==['style']


def test_worker_automatically_continues_rejected_repair_plan_within_limit(tmp_path,skill,monkeypatch):
    from sourceloom.production import Production
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    engine=Production(store,{'data_dir':str(tmp_path),'max_plan_repairs':3})
    def reject_plan(claimed):
        draft={'blocks':[{'id':'b1','unit_id':'u1','kind':'explanation','markdown':'Saved candidate'}]}
        claimed.update(stage='teaching_replan_review',repair_rounds=1,draft=draft,
            inventory=claimed['source'],plan={'units':[{'id':'u1'}]},quality_issues=['定义晚于首次使用'],
            regeneration={'unit_ids':['u1'],'original_plan':{'units':[{'id':'u1'}]},
                          'original_draft':draft,'findings':[]})
        claimed['results']['teaching-replan-review-1-route-v2']={'status':'needs_repair'}
        return 'needs_attention'
    monkeypatch.setattr(engine,'step',reject_plan)
    assert engine.run_once()
    resumed=store.job(job['id']);project=store.get(p['id'])
    assert resumed['status']=='queued' and resumed['stage']=='teaching_replan_repair'
    assert resumed['teaching_replan_attempts']==1
    assert project['active_job']==job['id'] and project['revision']==1


def test_regular_generation_can_continue_one_unqueryable_subscription_step(tmp_path,skill):
    import time
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    call={'id':'unknown-planner','role':'planner','channel':'openai-compatible',
        'upstream_base':'https://opencode.ai/zen/go/v1','step_key':'planner','status':'submitted',
        'dispatch_started':True,'deadline_at':time.time()-1}
    job.update(status='uncertain',stage='planner',pending='planner',calls=[call])
    store.put_job(job)
    with store.connect() as cx:
        p=store.get(p['id']);p['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p),p['id']))
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
    config={'provider':'openai-compatible','base_url':'https://opencode.ai/zen/go/v1','billing_mode':'subscription',
        'resume_unqueryable_subscription_once':True,
        'role_providers':{'planner':{'provider':'openai-compatible','base_url':'https://opencode.ai/zen/go/v1','billing_mode':'subscription'}}}
    result=queue.continue_unqueryable_subscription(job['id'],config)
    actual=store.job(job['id'])
    assert result['status']=='queued' and actual['calls']==[call] and 'pending' not in actual
    assert actual['unqueryable_subscription_continuations'][0]['logical_step']=='planner'


def test_primary_style_continuation_uses_subscription_provider_for_unknown_response(tmp_path,skill):
    import time
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    step='style-2-part-8-indexed-v2'
    call={'id':'unknown-primary','role':'style','channel':'openai-compatible','step_key':step,
        'status':'submitted','dispatch_started':True,'upstream_base':'https://opencode.ai/zen/go/v1',
        'deadline_at':time.time()-1}
    job.update(status='uncertain',stage='style',pending=step,calls=[call],draft={'blocks':[]},
        primary_continuation_roles=['style'])
    store.put_job(job)
    with store.connect() as cx:
        current=store.get(p['id']);current['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(current),p['id']))
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
    config={'provider':'openai-compatible','base_url':'https://opencode.ai/zen/go/v1',
        'billing_mode':'subscription','resume_unqueryable_subscription_once':True,
        'role_providers':{'style':{'provider':'router','base_url':'https://review.example'}}}
    result=queue.continue_unqueryable_subscription(job['id'],config)
    assert result['status']=='queued' and store.job(job['id'])['status']=='queued'


def test_unqueryable_fidelity_router_continues_on_base_provider(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    step='fidelity-0-indexed-1';call={'id':'unknown-review','role':'fidelity','channel':'router',
        'upstream_base':'https://review.example','step_key':step,'status':'uncertain','dispatch_started':True}
    job.update(status='uncertain',stage='fidelity',pending=step,
        calls=[call],draft={'blocks':[{'id':'b1','markdown':'Saved'}]})
    store.put_job(job)
    with store.connect() as cx:
        p=store.get(p['id']);p['active_job']=None
        cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p),p['id']))
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
    config={'provider':'openai-compatible','base_url':'https://primary.example','resume_unqueryable_router_once':True,
        'role_providers':{'fidelity':{'provider':'router','base_url':'https://review.example'}},
        'quality_fallback_providers':{'fidelity':{'provider':'router','base_url':'https://review.example'}}}
    queue.continue_unqueryable_router(job['id'],config);actual=store.job(job['id'])
    assert actual['primary_base_steps']==[step] and actual['primary_base_roles']==['fidelity']
    assert actual.get('primary_continuation_steps') in (None,[])


def test_fidelity_decoder_accepts_only_empty_explanation_note():
    from sourceloom.fidelity_parts import FidelitySchema,decode
    from jsonschema import ValidationError
    source={'objects':[{'id':'s1','text':'Original'}]};facts={'facts':[{'id':'f1','source_id':'s1'}]}
    draft={'blocks':[{'id':'b1','markdown':'Candidate'}]};assigned={'source_ids':[],'fact_ids':['f1'],'block_ids':[]}
    row={'block_id':'b1','status':'preserved','person_preserved':True,'referents_preserved':True,
        'explanation':'Exact fact remains','explanation_note':''}
    result={'source_checks':{},'fact_checks':{'f1':row},'reverse_checks':{},'findings':[]}
    assert decode(result,assigned,source,facts,draft)['fact_checks'][0]['status']=='preserved'
    assert result['fact_checks']['f1']['explanation_note']==''
    changed=json.loads(json.dumps(result));changed['fact_checks']['f1']['explanation_note']='extra claim'
    with pytest.raises(ValidationError):decode(changed,assigned,source,facts,draft)


def test_fidelity_reassessment_keeps_valid_rows_and_replaces_only_malformed_row():
    from sourceloom.fidelity_parts import invalid_assignments,merge_reassessment
    source={'objects':[{'id':'s1','text':'First'},{'id':'s2','text':'Second'}]}
    facts={'facts':[]};draft={'blocks':[]};assigned={'source_ids':['s1','s2'],'fact_ids':[],'block_ids':[]}
    original={'source_checks':{
        's1':{'missing_information':[],'explanation':'Valid'},
        's2':{'missing_information':[],'explanation':'Contradictory','note':'extra'}},
        'fact_checks':{},'reverse_checks':{},'findings':[{'id':'x','severity':'error','block_id':'',
            'obligation_id':'','message':'Keep','expected':'Retain this finding'}]}
    retained,missing=invalid_assignments(original,assigned,source,draft)
    assert list(retained['source_checks'])==['s1'] and missing['source_ids']==['s2']
    supplement={'source_checks':{'s2':{'missing_information':['Lost detail'],'explanation':'Rechecked'}},
        'fact_checks':{},'reverse_checks':{},'findings':[]}
    merged=merge_reassessment(retained,supplement,assigned,source,facts,draft)
    assert merged['assessed_source_ids']==['s1','s2']
    assert merged['missing_inventory_information']==['s2: Lost detail']
    assert merged['findings'][0]['message']=='Keep'


def test_production_repair_limit_is_configurable_and_bounded(tmp_path):
    from sourceloom.production import Production
    from sourceloom.store import Store
    store=Store(tmp_path/'data')
    assert Production(store,{}).repair_limit()==2
    assert Production(store,{'max_repair_rounds':4}).repair_limit()==4
    assert Production(store,{'max_repair_rounds':99}).repair_limit()==32


def test_empty_extracted_text_uses_retained_raw_object_when_writer_places_source(tmp_path):
    from sourceloom.writing import compose
    runtime=tmp_path/'runtime';runtime.mkdir()
    (runtime/'composition.py').write_text(
        'def render_document(body, sources):\n return "> " + next(iter(sources.values())) + "\\n"\n',
        encoding='utf8')
    raw='<w:drawing><a:blip r:embed="rId1"/></w:drawing>'
    inventory={'objects':[{'id':'xml','kind':'text','text':'','raw':raw,'locator':'word/document.xml'}],
        'obligations':[{'id':'fact','object_id':'xml'}]}
    response={'blocks':[{'id':'b','unit_id':'u','kind':'object','obligation_ids':['fact'],
        'object_ids':['xml'],'evidence':[{'source_id':'xml','quote':raw}],
        'content':[{'type':'source','id':'xml'}]}]}
    result=compose({'root':str(tmp_path)},response,inventory)['blocks'][0]
    assert raw in result['markdown'] and result['evidence']==[{'source_id':'xml','quote':raw}]


def test_fidelity_decoder_ignores_only_unassigned_placeholder_identity():
    from sourceloom.fidelity_parts import decode
    from jsonschema import ValidationError
    source={'objects':[{'id':'s1','text':'Original'}]};facts={'facts':[]};draft={'blocks':[]}
    assigned={'source_ids':['s1'],'fact_ids':[],'block_ids':[]}
    base={'source_checks':{'s1':{'missing_information':[],'explanation':'Checked'}},
        'fact_checks':{'typo':{'block_id':'','status':'unknown','person_preserved':True,
            'referents_preserved':True,'explanation':'占位'}},'reverse_checks':{},'findings':[]}
    assert decode(base,assigned,source,facts,draft)['assessed_source_ids']==['s1']
    changed=json.loads(json.dumps(base));changed['fact_checks']['typo']['explanation']='Possible actual loss'
    with pytest.raises(ValidationError):decode(changed,assigned,source,facts,draft)


def test_fidelity_decoder_discards_only_reverse_row_for_nonexistent_old_block():
    from sourceloom.fidelity_parts import decode
    source={'objects':[{'id':'s1','text':'Original'}]};facts={'facts':[]}
    draft={'blocks':[{'id':'current','markdown':'Candidate'}]}
    assigned={'source_ids':['s1'],'fact_ids':[],'block_ids':['current']}
    result={'source_checks':{'s1':{'missing_information':[],'explanation':'Checked'}},
        'fact_checks':{},'reverse_checks':{
            'current':{'kind':'source','status':'supported','source_ids':['s1'],'explanation':'Checked'},
            'old-block':{'kind':'source','status':'supported','source_ids':['s1'],'explanation':'Stale row'}},
        'findings':[]}
    assert [x['block_id'] for x in decode(result,assigned,source,facts,draft)['reverse_checks']]==['current']


def test_published_unresolved_job_can_continue_repairs_without_rewriting_saved_draft(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    draft={'blocks':[{'id':'b1','markdown':'Exact saved draft'}]}
    job.update(status='needs_attention',stage='fidelity',repair_rounds=2,draft=draft,
        quality_issues=['Located defect'],base_revision=0)
    store.put_job(job)
    store.change(p['id'],lambda p:p.update(active_job=None,revision=1,draft=draft,
        production={'job':job['id'],'status':'needs_attention'}))
    with store.connect() as cx:cx.execute("UPDATE production_control SET status='needs_attention' WHERE id=?",(job['id'],))
    continued=queue.continue_quality_repairs(job['id'],{'max_repair_rounds':4})
    assert continued['id']!=job['id'] and continued['quality_continuation_of']==job['id']
    assert continued['draft']==draft and continued['repair_rounds']==2 and continued['stage']=='repair'
    assert continued['calls']==[] and continued['base_revision']==1
    assert store.job(job['id'])['status']=='needs_attention'


def test_style_partition_contract_gets_one_bounded_second_correction(tmp_path,skill,monkeypatch):
    from jsonschema import ValidationError
    from sourceloom.production import Production
    from sourceloom.style_parts import decode_indexed
    from tests.test_production import prepared
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    payload={'rule_catalog':['FMT-001'],'rule_definitions':{'FMT-001':{'text':'Rule'}},
        'mechanical_findings':[],'mechanical_candidates':[]}
    invalid={'findings':[],'rules_by_id':{
        'FMT-001':{'status':'fail','block_ids':[],'reason':'Real defect'},
        'FMT-999':{'status':'unknown','block_ids':[],'reason':'Unassigned row'}}}
    valid={'findings':[],'rules_by_id':{
        'FMT-001':{'status':'fail','block_ids':[],'reason':'Real defect'}}}
    calls=[]
    def call(job,key,role,request,schema):
        calls.append((key,role,request['received_review']))
        return invalid if key.endswith('-contract') else valid
    engine=Production(store,{});monkeypatch.setattr(engine,'_call',call)
    error=ValidationError('extra unassigned row')
    repaired=engine._repair_indexed_style_part(job,'style-1-part-1-indexed-v2',payload,invalid,error)
    assert [row[0] for row in calls]==[
        'style-1-part-1-indexed-v2-contract','style-1-part-1-indexed-v2-contract-2']
    assert calls[1][2]==invalid
    assert decode_indexed(repaired,payload)['assessments'][0]['status']=='fail'


def test_execution_evidence_follows_quality_continuation_artifacts_and_current_limit(tmp_path):
    from sourceloom.review_context import execution_evidence
    from sourceloom.store import digest
    parent=tmp_path/'production'/'parent'/'repair-20';parent.mkdir(parents=True)
    before=b'alpha\n';after=b'beta\n'
    patch={'document_sha256':digest(before),'edits':[{
        'node_id':'LINE-1','old_text':'alpha','new_text':'beta','reason':'Exact correction'}]}
    (parent/'before.md').write_bytes(before);(parent/'after.md').write_bytes(after)
    (parent/'patch.json').write_text(json.dumps(patch),encoding='utf8')
    commit={'round':20,'before':digest(before),'after':digest(after),'blocks':['b'],'edits':1}
    job={'repair_rounds':20,'repair_commits':[commit],'quality_history':[{'job':'parent'}],
        'draft':{'blocks':[{'id':'b','markdown':'beta'}]}}
    result=execution_evidence(job,{'package_digest':'p','instructions':{}},
        tmp_path/'production'/'continued',32)
    assert result['local-committer']['status']=='pass'
    assert result['local-committer']['commits'][0]['artifact_job']=='parent'
    assert result['repair-limit']['status']=='pass' and result['repair-limit']['limit']==32


def test_exhausted_quality_job_can_start_one_audited_epoch_after_system_fix(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    draft={'blocks':[{'id':'b1','markdown':'Exact saved draft'}]}
    job.update(status='needs_attention',stage='style',repair_rounds=4,draft=draft,
        quality_issues=['Verifier used the old limit'],base_revision=0,
        results={'planner':{'kept':True},'style-4-part-1':{'stale':True},
                 'fidelity-4':{'stale':True},'line_repair-4':{'stale':True}})
    store.put_job(job)
    store.change(p['id'],lambda p:p.update(active_job=None,revision=1,draft=draft,
        production={'job':job['id'],'status':'needs_attention'}))
    with store.connect() as cx:cx.execute(
        "UPDATE production_control SET status='needs_attention' WHERE id=?",(job['id'],))
    continued=queue.continue_after_quality_system_fix(job['id'],{
        'max_repair_rounds':4,'max_quality_system_continuations':1},'cross-job-evidence-v1')
    assert continued['quality_system_continuation_of']==job['id']
    assert continued['quality_system_fixes']==['cross-job-evidence-v1']
    assert continued['repair_rounds']==0 and continued['prior_repair_rounds']==4
    assert continued['draft']==draft and continued['stage']=='style' and continued['calls']==[]
    assert continued['results']=={'planner':{'kept':True}}
    assert store.job(job['id'])['status']=='needs_attention'


def test_line_repair_can_delete_an_exact_unprotected_line():
    from sourceloom.production_contracts import LineRepair,LocalRepair
    line=LineRepair.model_validate({'document_digest':'d','edits':[{'line_id':'b:2','replacement':'',
        'reason':'Remove an independently confirmed duplicate'}]})
    assert line.edits[0].replacement==''
    local=LocalRepair.model_validate({'document_digest':'d','edits':[{'block_id':'b','old_text':'duplicate',
        'new_text':'','reason':'Remove an independently confirmed duplicate'}]})
    assert local.edits[0].new_text==''


def test_deterministic_list_spacing_removes_only_same_level_gaps():
    from sourceloom.writing import tighten_list_spacing
    draft={'blocks':[{'id':'b','markdown':'- first\n\n- second\n\n  - child\n\n```text\n- literal\n\n- stays\n```'}]}
    fixed,changed=tighten_list_spacing(draft)
    assert changed==['b']
    assert fixed['blocks'][0]['markdown']=='- first\n- second\n\n  - child\n\n```text\n- literal\n\n- stays\n```'
    assert draft['blocks'][0]['markdown'].startswith('- first\n\n- second')


def test_deterministic_block_edge_trim_keeps_words_and_internal_spacing():
    from sourceloom.writing import trim_block_edges
    draft={'blocks':[{'id':'b','markdown':'\n\nFirst\n\nSecond\n\n'}]}
    fixed,changed=trim_block_edges(draft)
    assert changed==['b'] and fixed['blocks'][0]['markdown']=='First\n\nSecond'
    assert draft['blocks'][0]['markdown'].startswith('\n\n')


def test_protected_object_repositioning_uses_unit_regeneration(tmp_path):
    from sourceloom.production import Production
    from sourceloom.store import Store
    source={'objects':[{'id':'link','kind':'link','text':'CVEs','target':'https://example.test/cve'}]}
    literal='[CVEs](https://example.test/cve)'
    draft={'blocks':[{'id':'b','unit_id':'u','markdown':'Context\n\n'+literal}]}
    job={'teaching_version':2,'repair_rounds':1,'draft':draft,'plan':{'units':[{'id':'u'}]},
        'style':{'findings':[]},'fidelity':{'findings':[{'block_id':'b','message':'链接独立成行，位置错误',
        'expected':'放回原句'}]}}
    engine=Production(Store(tmp_path),{'max_repair_rounds':12})
    assert engine._regenerate_protected_structure(job,source)
    assert job['stage']=='teaching_replan' and job['repair_rounds']==2
    assert job['regeneration']['unit_ids']==['u'] and job['regeneration']['findings'][0]['block_id']=='b'


def test_false_literal_absence_claim_does_not_regenerate_complete_unit(tmp_path):
    from sourceloom.production import Production
    from sourceloom.store import Store
    literal='[CVEs](https://example.test/cve)'
    source={'objects':[{'id':'link','kind':'link','text':'CVEs','target':'https://example.test/cve'}]}
    job={'teaching_version':2,'repair_rounds':3,
        'draft':{'blocks':[{'id':'b','unit_id':'u','markdown':'Example '+literal}]},
        'plan':{'units':[{'id':'u'}]},'fidelity':{'findings':[]},
        'style':{'findings':[{'block_id':'b','message':'受保护原文字面量未逐字出现，当前位置需要确认'}]}}
    engine=Production(Store(tmp_path),{'max_repair_rounds':24})
    assert not engine._regenerate_protected_structure(job,source)
    assert job['repair_rounds']==3 and 'regeneration' not in job


def test_unrelated_source_attribution_word_does_not_regenerate_link_unit(tmp_path):
    from sourceloom.production import Production
    from sourceloom.store import Store
    source={'objects':[{'id':'link','kind':'link','text':'CVEs','target':'https://example.test/cve'}]}
    draft={'blocks':[{'id':'b','unit_id':'u','markdown':'Example [CVEs](https://example.test/cve)'}]}
    job={'teaching_version':2,'repair_rounds':3,'draft':draft,'plan':{'units':[{'id':'u'}]},
        'fidelity':{'findings':[]},'style':{'findings':[{'block_id':'b',
        'message':'两个报告动作应分行','expected':'分行并保持来源归属'}]}}
    assert not Production(Store(tmp_path),{'max_repair_rounds':24})._regenerate_protected_structure(job,source)


def test_permission_stop_after_recorded_router_continuation_can_resume_without_new_identity(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    call={'id':'unknown','role':'style','channel':'router','step_key':'style-1','status':'uncertain'}
    job.update(status='failed',stage='style',error='PermissionError',calls=[call],draft={'blocks':[]},
        unqueryable_router_continuations=[{'original_call':'unknown','logical_step':'style-1'}])
    store.put_job(job);store.change(p['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
    resumed=queue.retry_validation(job['id'])
    assert resumed['status']=='queued' and resumed['calls']==[call] and not resumed.get('pending')


def test_billed_truncated_layout_repair_can_use_configured_full_response_fallback(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    blob=store.blob(b'{"partial":true}');step='writer-u1-layout'
    call={'id':'layout-cut','role':'layout_repair','channel':'openai-compatible','step_key':step,
        'status':'truncated','finish_reason':'length','response_blob':blob}
    job.update(status='failed',stage='writer',pending=step,calls=[call],draft={'blocks':[]})
    store.put_job(job);store.change(p['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)',
            ('layout-cut',p['id'],0,0,1,'{}'))
    resumed=queue.retry_validation(job['id'])
    assert resumed['fallbacks'][step]['original_call']=='layout-cut' and not resumed.get('pending')


def test_billed_truncated_teaching_replan_can_use_larger_full_response_fallback(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    blob=store.blob(b'{"partial":true}');step='teaching-replan-17'
    call={'id':'replan-cut','role':'teaching_replan','channel':'openai-compatible','step_key':step,
        'status':'truncated','finish_reason':'length','response_blob':blob}
    job.update(status='failed',stage='teaching_replan',pending=step,calls=[call],draft={'blocks':[]})
    store.put_job(job);store.change(p['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)',
            ('replan-cut',p['id'],0,0,1,'{}'))
    resumed=queue.retry_validation(job['id'])
    assert resumed['fallbacks'][step]['original_call']=='replan-cut' and not resumed.get('pending')


def test_billed_truncated_fidelity_review_can_use_configured_full_response_fallback(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    blob=store.blob(b'{"partial":true}');step='fidelity-2-indexed-4'
    call={'id':'fidelity-cut','role':'fidelity','channel':'openai-compatible','step_key':step,
        'status':'truncated','finish_reason':'length','response_blob':blob}
    job.update(status='failed',stage='fidelity',pending=step,calls=[call],draft={'blocks':[]})
    store.put_job(job);store.change(p['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)',
            ('fidelity-cut',p['id'],0,0,1,'{}'))
    resumed=queue.retry_validation(job['id'])
    assert resumed['fallbacks'][step]['original_call']=='fidelity-cut' and not resumed.get('pending')


def test_billed_reasoning_exhausted_fallback_clears_saved_pending_before_resume(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    base='teaching-replan-decision-3';step=base+'__fallback'
    blob=store.blob(json.dumps({'choices':[{'finish_reason':'length','message':{'content':'',
        'reasoning_content':'analysis only'}}],'usage':{'completion_tokens':200,
        'completion_tokens_details':{'reasoning_tokens':200}}}).encode())
    call={'id':'reasoning-cut','role':'plan_decision__fallback','channel':'openai-compatible',
        'step_key':step,'status':'reasoning_exhausted','finish_reason':'length','response_blob':blob}
    job.update(status='failed',stage='teaching_replan_review',pending=step,calls=[call],
        fallbacks={base:{'original_call':'primary-cut'}},draft={'blocks':[]})
    store.put_job(job);store.change(p['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)',
            ('reasoning-cut',p['id'],0,0,1,'{}'))
    resumed=queue.retry_validation(job['id'])
    assert resumed['status']=='queued' and not resumed.get('pending')
    assert resumed['calls'][-1]['status']=='reasoning_exhausted'


def test_large_plan_adjudication_is_partitioned_without_losing_claim_indices(tmp_path,monkeypatch):
    from sourceloom.production import Production
    from sourceloom.store import Store
    engine=Production(Store(tmp_path),{});seen=[];job={'results':{}}
    monkeypatch.setattr(engine.store,'put_job',lambda job:None)
    claims=[{'claim_index':n,'claim':f'issue {n}'} for n in range(29)]
    def call(job,key,role,payload,schema):
        seen.append((key,[c['claim_index'] for c in payload['claims']]))
        return {'decisions':[{'claim_index':c['claim_index'],'verdict':'not_error',
            'evidence':[{'source_id':'s'}],'reason':'checked'} for c in payload['claims']]}
    monkeypatch.setattr(engine,'_call',call)
    result=engine._plan_decisions(job,'decision',{'claims':claims,'source':{'objects':[]}})
    assert [len(indices) for _,indices in seen]==[4,4,4,4,4,4,4,1]
    assert [d['claim_index'] for d in result['decisions']]==list(range(29))
    assert job['results']['decision']==result


def test_plan_adjudication_repairs_only_rows_with_invalid_source_ids(tmp_path,monkeypatch):
    from sourceloom.production import Production
    from sourceloom.store import Store
    engine=Production(Store(tmp_path),{});seen=[];job={'results':{}}
    source={'objects':[{'id':'src-1','text':'one'},{'id':'src-2','text':'two'}]}
    claims=['first claim','second claim']
    replies=[{'decisions':[
        {'claim_index':0,'verdict':'not_error','evidence':[{'source_id':'src-1'}],'reason':'valid'},
        {'claim_index':1,'verdict':'confirmed_error','evidence':[{'source_id':'inventory-uuid'}],'reason':'invalid'}]},
        {'decisions':[{'claim_index':1,'verdict':'confirmed_error',
            'evidence':[{'source_id':'src-2'}],'reason':'rechecked'}]}]
    def decide(job,key,payload):
        seen.append((key,payload))
        return replies.pop(0)
    monkeypatch.setattr(engine,'_plan_decisions',decide)
    monkeypatch.setattr(engine.store,'put_job',lambda job:None)
    result,passed=engine._checked_plan_decisions(job,'decision',
        {'source':source,'claims':[{'claim_index':n,'claim':c} for n,c in enumerate(claims)]},
        claims,source)
    assert not passed
    assert [c['claim_index'] for c in seen[1][1]['claims']]==[1]
    assert seen[1][1]['allowed_source_ids']==['src-1','src-2']
    assert result['decisions'][0]==seen[0][1].get('valid_decisions',[result['decisions'][0]])[0]
    assert [d['claim_index'] for d in result['decisions']]==[0,1]


def test_identical_reader_snapshot_is_rendered_once(monkeypatch):
    import json
    import sourceloom.library_api as api
    calls=[]
    monkeypatch.setattr(api,'render',lambda project,asset_url:calls.append(project) or '<p>body</p>')
    api.cached_output.cache_clear()
    project={'title':'sample','heading_numbering':'preserve',
        'draft':{'blocks':[{'id':'b1','markdown':'body'}]}}
    snapshot=json.dumps(project,ensure_ascii=False,separators=(',',':'))
    first=api.cached_output('project',None,'html',snapshot)
    second=api.cached_output('project',None,'html',snapshot)
    assert first==second and b'body' in first
    assert len(calls)==1


def test_dedicated_worker_cannot_starve_an_older_queued_project(tmp_path,skill):
    from sourceloom.durable import Queue
    from sourceloom.store import Store
    from tests.test_production import prepared
    store,queue,older,bundle=prepared(tmp_path,skill)
    older_job=queue.enqueue(older['id'],bundle)
    newer=store.create('newer')
    newer=store.change(newer['id'],lambda project:project.update(
        inventory=store.get(older['id'])['inventory'],state='frozen'))
    newer_job=queue.enqueue(newer['id'],bundle)
    assert queue.claim('newer-dedicated',project=newer['id']) is None
    assert queue.claim('generic')['id']==older_job['id']
    assert queue.claim('newer-dedicated',project=newer['id'])['id']==newer_job['id']


def test_worker_concurrency_is_configurable_and_bounded(tmp_path):
    from sourceloom.durable import Queue
    from sourceloom.store import Store
    store=Store(tmp_path)
    assert Queue(store,0).max_running==1
    assert Queue(store,3).max_running==3
    assert Queue(store,99).max_running==4


def test_truncated_large_plan_decision_can_resume_as_partitions(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    blob=store.blob(b'{"partial":true}');step='teaching-replan-decision-3__fallback'
    call={'id':'plan-cut','role':'plan_decision__fallback','channel':'openai-compatible',
        'step_key':step,'status':'truncated','finish_reason':'length','response_blob':blob}
    job.update(status='failed',stage='teaching_replan_review',pending=step,calls=[call],draft={'blocks':[]})
    store.put_job(job);store.change(p['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)',
            ('plan-cut',p['id'],0,0,1,'{}'))
    resumed=queue.retry_validation(job['id'])
    assert resumed['status']=='queued' and not resumed.get('pending')
    assert resumed['partitioned_plan_decision_continuations'][0]['original_call']=='plan-cut'


def test_expired_interrupted_subscription_resume_records_unknown_then_continues_once(tmp_path,skill):
    import time
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    step='style-2-part-6-indexed-v2'
    call={'id':'interrupted','role':'style','channel':'openai-compatible','step_key':step,
        'status':'submitted','dispatch_started':True,'deadline_at':time.time()-1,
        'upstream_base':'https://opencode.ai/zen/go/v1'}
    job.update(status='failed',stage='style',pending=step,calls=[call])
    store.put_job(job);store.change(p['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,status,created,body) VALUES(?,?,?,?,?,?,?)',
            ('interrupted',p['id'],0,None,'reserved',1,'{}'))
    cfg={'resume_unqueryable_subscription_once':True,'provider':'openai-compatible',
        'base_url':'https://opencode.ai/zen/go/v1','billing_mode':'subscription'}
    result=queue.retry_validation(job['id'],cfg);resumed=store.job(job['id'])
    assert result['status']=='queued' and resumed['status']=='queued' and not resumed.get('pending')
    assert resumed['unqueryable_subscription_continuations'][0]['original_call']=='interrupted'
    assert resumed['calls'][-1]['status']=='uncertain'
    with store.connect() as cx:
        spending=cx.execute('SELECT status,actual FROM spending WHERE id=?',('interrupted',)).fetchone()
    assert spending['status']=='unknown' and spending['actual'] is None
