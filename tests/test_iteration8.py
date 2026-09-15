import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.intake_jobs import IntakeQueue
from sourceloom.store import Store,Conflict
from sourceloom.ingest import intake
from sourceloom.progress import summary
from tests.test_production import skill


def test_unlimited_subscription_does_not_consume_paid_request_or_cash_limits(tmp_path):
    s=Store(tmp_path);p=s.create('Separate quotas',budget=.01)
    s.reserve(p['id'],'chat',0,{'channel':'router'},daily_budget=0,daily_calls=0,total_budget=0)
    s.settle('chat',None,{'channel':'subscription'})
    s.reserve(p['id'],'api',.01,{'channel':'openai-compatible'},daily_budget=.01,daily_calls=1,total_budget=.01)
    s.settle('api',.01,{})
    s.reserve(p['id'],'chat-again',0,{'channel':'router'},daily_budget=0,daily_calls=0,total_budget=0)
    with pytest.raises(Conflict):s.reserve(p['id'],'api-again',.01,{'channel':'openai-compatible'},daily_budget=.01,daily_calls=1)
    with pytest.raises(Conflict,match='订阅测试请求次数'):
        s.reserve(p['id'],'explicit-subscription-cap',0,{'channel':'router'},subscription_calls=2)


def test_labeled_chat_result_requires_exact_original_schema_and_only_known_wrapper():
    from sourceloom.providers import recover_labeled_chat_json
    schema={'type':'object','additionalProperties':False,'properties':{'quote':{'type':'string'}},'required':['quote']}
    value={'quote':'A "quoted" sentence\nnext line'}
    body=dict(status='failed',errorCode='validation_failed',webExecution={'accountId':'test'},
              validation={'messages':['schema:/:must be object']},output='JSON\n'+json.dumps(value))
    assert recover_labeled_chat_json(body,schema)==value
    assert body['status']=='failed' and body['output'].startswith('JSON\n')
    for changed in [dict(output='JSON\n{"wrong":true}'),dict(output=body['output']+' commentary'),
                    dict(output='JSON\n{"quote":"unescaped "quote""}'),dict(errorCode='chatgpt_delivery_uncertain'),
                    dict(validation={'messages':['a different validation failure']})]:
        assert recover_labeled_chat_json(body|changed,schema) is None


def test_packed_definition_unwraps_only_explicit_boundaries_without_editing_words():
    from sourceloom.writing import normalize_definition_encoding
    sentences=['是什么','用于什么','怎样工作','适用条件','边界']
    node={'type':'term','definition':['；'.join(sentences)]}
    raw={'blocks':[{'content':[node]}]}
    result=normalize_definition_encoding(raw)
    assert result['blocks'][0]['content'][0]['definition']==sentences
    assert node['definition']==['；'.join(sentences)]
    for content in ['只有一句','一；二','一；二；三；四；五；六','一；；三']:
        invalid={'blocks':[{'content':[node|{'definition':[content]}]}]}
        assert normalize_definition_encoding(invalid)==invalid


def test_original_page_attachment_is_once_only_and_cannot_mask_unassigned_facts():
    from sourceloom.writing import attach_original_pages,protected_objects
    inv={'objects':[{'id':'page','kind':'page','resource_id':'bytes','text':'Original page','locator':'p1'}],
         'obligations':[{'id':'f','object_id':'page'}]}
    b=dict(id='b',unit_id='u',kind='explanation',obligation_ids=['f'],content=[{'type':'paragraph','text':'Actual prose','node_id':'p','parent_id':''}])
    raw={'encoding':'flat_nodes_v1','blocks':[b]}
    result=attach_original_pages(raw,inv,'u',{'blocks':[]})
    assert result['blocks'][0]==b and len(raw['blocks'])==1 and len(result['blocks'])==2
    assert attach_original_pages(result,inv,'u',{'blocks':[]})==result
    no_binding=raw|{'blocks':[b|{'obligation_ids':[]}]}
    assert attach_original_pages(no_binding,inv,'u',{'blocks':[]})==no_binding
    prior={'blocks':[{'embedded_object_ids':['page'],'object_ids':['page'],'markdown':protected_objects(inv)['page']}]}
    assert attach_original_pages(raw,inv,'u',prior)==raw


def test_finishing_all_units_keeps_unapproved_metadata_in_place_without_new_generation(tmp_path):
    from sourceloom.production import Production
    s=Store(tmp_path);engine=Production(s,{})
    original=[{'id':'a','unit_id':'u','kind':'document_info','markdown':'Article content','evidence':[{'source_id':'text','quote':'Original'}]},
              {'id':'b','unit_id':'u','kind':'explanation','markdown':'Following content','evidence':[]}]
    job={'draft':{'blocks':original},'plan':{'units':[{'id':'u','document_info_ids':[]}]},'unit_index':1,'stage':'writer'}
    assert engine._finish_draft(job)=='queued' and job['stage']=='style'
    assert [b['markdown'] for b in job['draft']['blocks']]==['Article content','Following content']
    assert job['document_info_kept_in_body']==['a'] and original[0]['kind']=='document_info'
    assert job['draft']['blocks'][0]['kind']=='explanation'


def test_inline_reference_repair_cannot_rewrite_surrounding_conditions():
    from sourceloom.writing import apply_reference_patch,invalid_inline_references
    inv={'objects':[{'id':'page','kind':'page'}]}
    text='仅在条件成立时参见（{{source:page}}），其他情况不适用'
    raw={'blocks':[{'id':'b','content':[{'type':'paragraph','node_id':'n','text':text}]}]}
    target=invalid_inline_references(raw,inv)[0]
    new=text.replace('{{source:page}}','图 1')
    result=apply_reference_patch(raw,{'edits':[target|{'new_text':new}]},inv)
    assert result['blocks'][0]['content'][0]['text']==new and raw['blocks'][0]['content'][0]['text']==text
    removed=text.replace('（{{source:page}}）','')
    assert apply_reference_patch(raw,{'edits':[target|{'new_text':removed}]},inv)['blocks'][0]['content'][0]['text']==removed
    for changed in [new.replace('仅在条件成立时','总是'),new+'新增保证',new.replace('图 1','')]:
        with pytest.raises(ValueError):apply_reference_patch(raw,{'edits':[target|{'new_text':changed}]},inv)


def test_upload_acceptance_survives_browser_close_and_keeps_exact_original(tmp_path):
    config=load_config()|{'data_dir':str(tmp_path),'provider':'manual'}
    app=create_app(config);store=app.state.store;raw=b'# Input\n\nOriginal 1.25 and [link](https://example.org)\n'
    with patch('sourceloom.parse_worker.isolated_intake',side_effect=lambda s,files,**kw:intake(s,files)) as parser:
        with TestClient(app) as client:
            pid=client.post('/api/projects',json={'title':'Upload continuity'},headers={'X-SourceLoom':'1'}).json()['id']
            response=client.post(f'/api/projects/{pid}/upload?background=true',files={'files':('source.md',raw)},headers={'X-SourceLoom':'1'})
            assert response.status_code==202 and parser.call_count==0
            jid=response.json()['id']
        assert IntakeQueue(Store(tmp_path),config).run_once()
        assert parser.call_count==1 and not IntakeQueue(store,config).run_once()
    assert store.job(jid)['status']=='completed' and not store.get(pid)['active_job']
    original=store.get(pid)['inventory']['originals'][0]
    assert store.read_blob(original['sha256'])==raw


def test_upload_retry_after_lost_receipt_reuses_job_and_rejects_changed_content(tmp_path):
    config=load_config()|{'data_dir':str(tmp_path),'provider':'manual'}
    app=create_app(config);store=app.state.store
    with TestClient(app) as client:
        pid=client.post('/api/projects',json={'title':'Retry receipt'},headers={'X-SourceLoom':'1'}).json()['id']
        url=f'/api/projects/{pid}/upload?background=true&request_id=one-upload'
        first=client.post(url,files={'files':('real.txt',b'Original')},headers={'X-SourceLoom':'1'})
        again=client.post(url,files={'files':('real.txt',b'Original')},headers={'X-SourceLoom':'1'})
        assert first.status_code==again.status_code==202
        assert first.json()['id']==again.json()['id'] and again.json()['reused']
        changed=client.post(url,files={'files':('real.txt',b'Changed')},headers={'X-SourceLoom':'1'})
        assert changed.status_code==409
    with store.connect() as cx:
        assert cx.execute('SELECT count(*) FROM jobs WHERE project=?',(pid,)).fetchone()[0]==1


def test_failed_parse_exposes_download_not_private_job_and_other_project_cannot_read(tmp_path):
    config=load_config()|{'data_dir':str(tmp_path),'provider':'manual'};app=create_app(config);s=app.state.store
    with TestClient(app) as c:
        p=c.post('/api/projects',json={'title':'A'},headers={'X-SourceLoom':'1'}).json()
        other=c.post('/api/projects',json={'title':'B'},headers={'X-SourceLoom':'1'}).json()
        job=IntakeQueue(s,config).enqueue(p['id'],uploads=[('broken.pdf',b'original bytes')])
        with patch('sourceloom.parse_worker.isolated_intake',side_effect=ValueError('文件结构无法读取')):assert IntakeQueue(s,config).run_once()
        status=c.get(f"/api/projects/{p['id']}/production").json()
        assert status['status']=='failed' and status['progress']['active'] is False
        assert 'base_revision' not in status and 'files' not in status
        url=status['received_files'][0]['url']
        assert c.get(url).content==b'original bytes'
        assert c.get(url.replace(p['id'],other['id'])).status_code==404
        assert not s.get(p['id'])['active_job']


def test_cancelled_intake_cannot_generate_or_be_resurrected(tmp_path):
    c=load_config();s=Store(tmp_path);p=s.create('Cancel');queue=IntakeQueue(s,c)
    j=queue.enqueue(p['id'],uploads=[('source.txt',b'keep me')]);queue.cancel(j['id'])
    assert not queue.run_once() and s.job(j['id'])['status']=='cancelled'
    assert not s.get(p['id'])['active_job']
    assert s.read_blob(s.job(j['id'])['files'][0]['sha256'])==b'keep me'


def test_intake_checkpoint_recovers_without_parsing_or_appending_twice(tmp_path,skill):
    from sourceloom.durable import Queue
    config=load_config()|{'provider':'openai-compatible','writing_skill_dir':str(skill)}
    s=Store(tmp_path/'data');p=s.create('Resume');q=IntakeQueue(s,config)
    j=q.enqueue(p['id'],uploads=[('source.md',b'Complete original')],generate=True)
    with patch('sourceloom.parse_worker.isolated_intake',side_effect=lambda s,files,**kw:intake(s,files)),patch.object(Queue,'enqueue',side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):q.run_once()
    assert s.job(j['id'])['inventory_saved']
    before=s.get(p['id'])['inventory']
    with s.connect() as cx:cx.execute('UPDATE intake_control SET lease_until=0')
    with patch('sourceloom.parse_worker.isolated_intake',side_effect=AssertionError('Must not parse again')):
        assert IntakeQueue(s,config).run_once()
    assert s.get(p['id'])['inventory']==before
    job=s.job(j['id']);assert job['status']=='completed'
    assert s.get(p['id'])['active_job']==job['production_job']
    with s.connect() as cx:assert cx.execute("SELECT count(*) FROM jobs WHERE role='production'").fetchone()[0]==1


def test_progress_never_claims_review_pass_for_saved_candidate():
    p=summary({'role':'production','stage':'style','status':'needs_attention','repair_rounds':2})
    assert not p['completed'] and not p['active'] and p['current']==4
    p=summary({'role':'production','stage':'writer','status':'running','unit_index':1,'unit_count':3})
    assert p['label']=='正在改写第 2 / 3 部分'
    assert summary({'role':'production','stage':'publish','status':'completed'})['completed']
    assert not summary({'role':'intake','stage':'received','status':'completed'})['completed']


@pytest.mark.parametrize('corrected',[True,False])
def test_invalid_review_gets_one_protocol_correction_without_rewriting(tmp_path,skill,monkeypatch,corrected):
    import copy
    from tests.test_production import prepared
    from sourceloom.production import Production
    from sourceloom.checks import freeze
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    job.update(stage='style',draft={'blocks':[dict(id='b',unit_id='u',kind='explanation',markdown='原有正文',obligation_ids=[],object_ids=[],evidence=[])]},inventory=freeze(store.get(p['id'])['inventory']),plan={'units':[]})
    original=copy.deepcopy(job['draft']);engine=Production(store,{})
    monkeypatch.setattr('sourceloom.production.scan',lambda *a:{'format':{'findings':[],'candidates':[]}})
    monkeypatch.setattr('sourceloom.production.inspect_draft',lambda *a,**kw:[])
    monkeypatch.setattr('sourceloom.production.rule_catalog',lambda *a:{'FMT-001':{}})
    valid={'assessments':[dict(rule_ids=['FMT-001'],status='unknown',block_ids=[],reason='Missing evidence')],'mechanical_assessments':[],'findings':[]}
    invalid=copy.deepcopy(valid);invalid['unexpected']='must not be silently dropped';calls=[]
    def call(job,key,role,payload,schema):
        calls.append(key)
        if role=='style':return invalid
        from sourceloom.production import draft_text_view
        assert payload['received_review']==invalid and payload['draft']==draft_text_view(original)
        return valid if corrected else invalid
    monkeypatch.setattr(engine,'_call',call)
    if corrected:
        assert engine.step(job)=='queued'
        assert job['quality_issues'] and job['style']['assessments'][0]['status']=='unknown'
    else:
        with pytest.raises(ValueError):engine.step(job)
    assert calls==['style-0','style-0-contract'] and job['draft']==original


@pytest.mark.parametrize('bulk',[True,False])
def test_trashing_upload_cancels_it_without_generation(tmp_path,bulk):
    from sourceloom.durable import Queue
    s=Store(tmp_path);q=Queue(s);c=load_config();p=s.create('Trash')
    j=IntakeQueue(s,c).enqueue(p['id'],uploads=[('a.txt',b'original')])
    if bulk:q.library.apply('trash',[dict(kind='document',id=p['id'],revision=0)])
    else:q.edit_document(p['id'],0,trashed=True)
    assert s.job(j['id'])['status']=='cancelled'
    assert not IntakeQueue(s,c).run_once()
    assert s.get(p['id'])['trashed'] and not s.get(p['id'])['active_job']


def test_noop_lines_do_not_discard_other_actual_edits_or_relax_scope():
    from sourceloom.review_context import line_proposal
    lines=[dict(line_id='b:1',block_id='b',text='same'),dict(line_id='b:2',block_id='b',text='old')]
    proposal=dict(document_digest='hash',edits=[dict(line_id='b:1',replacement='same',reason='unchanged'),dict(line_id='b:2',replacement='new',reason='fix')])
    assert line_proposal(proposal,lines)['edits']==[dict(block_id='b',old_text='old',new_text='new',reason='fix')]
    proposal['edits'].append(proposal['edits'][0])
    with pytest.raises(Conflict):line_proposal(proposal,lines)


def test_only_proven_invisible_single_frame_images_resolve_visual_gap(tmp_path):
    from io import BytesIO
    from PIL import Image
    from sourceloom.visual_sources import classify_transparent
    s=Store(tmp_path);objects=[]
    for sid,alpha in [('blank',0),('visible',1)]:
        buf=BytesIO();Image.new('RGBA',(2,2),(192,192,192,alpha)).save(buf,'PNG')
        objects.append(dict(id=sid,kind='image',resource_id=s.blob(buf.getvalue()),text=''))
    source=dict(objects=objects,unknown=[dict(object_id=o['id'],reason='visual') for o in objects])
    result=classify_transparent(s,source)
    assert source['objects'][0]['text']=='' and len(source['unknown'])==2
    assert result['unknown']==[dict(object_id='visible',reason='visual')]
    assert result['objects'][0]['resource_id']==source['objects'][0]['resource_id']
    assert result['objects'][0]['visual_classification']['method']=='all_pixels_alpha_zero'
    assert 'visual_classification' not in result['objects'][1]


@pytest.mark.parametrize('role,key',[('style_contract_repair','style-1-contract'),('term_preparation','terms-u2'),('writer','writer-u1')])
def test_billed_truncated_review_resumes_only_through_named_fallback(tmp_path,skill,role,key):
    from tests.test_production import prepared
    s,q,p,bundle=prepared(tmp_path,skill);j=q.enqueue(p['id'],bundle)
    call=dict(id='paid-review',role=role,status='truncated',finish_reason='length',response_blob=s.blob(b'{}'))
    j.update(status='failed',stage='style',pending=key,calls=[call])
    s.put_job(j)
    with s.connect() as cx:
        cx.execute("INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)",('paid-review',p['id'],.1,.02,0,'{}'))
        cx.execute("UPDATE production_control SET status='failed'")
    s.change(p['id'],lambda p:p.update(active_job=None))
    result=q.retry_validation(j['id'])
    assert result['fallbacks'][key]['original_call']=='paid-review'
    assert not result.get('pending') and result['calls']==[call]
    result.update(status='failed',error='PermissionError');s.put_job(result)
    s.change(p['id'],lambda p:p.update(active_job=None))
    assert q.retry_validation(j['id'])['fallbacks']==result['fallbacks']


def test_chat_packet_keeps_nested_source_fences_without_an_extra_display_wrapper():
    import re
    from sourceloom.providers import literal_chat_packet
    text='## Full instructions\n- 中文 `code` **bold**\n````\n```json\n{"x":"a"}\n```\n````'
    assert literal_chat_packet(text)==text


def test_cross_block_sibling_sections_keep_their_common_active_ancestor():
    from sourceloom.writing import expand_response
    def block(bid,nodes):return dict(id=bid,unit_id='u',kind='explanation',content=nodes)
    def heading(nid,parent,title):return dict(type='section',node_id=nid,parent_id=parent,heading=title)
    def prose(nid,parent,text):return dict(type='paragraph',node_id=nid,parent_id=parent,text=text)
    response=dict(encoding='flat_nodes_v1',blocks=[
        block('a',[heading('main','','主标题'),prose('intro','main','引入')]),
        block('b',[heading('one','main','第一项'),prose('p1','one','第一项正文')]),
        block('c',[heading('two','main','第二项'),prose('p2','two','第二项正文')])])
    depths={};rendered=expand_response(response,depths)['blocks']
    assert depths=={'b':1,'c':1}
    assert rendered[0]['content'][0]['heading']=='主标题'
    assert rendered[1]['content'][0]['blocks']==[{'type':'paragraph','text':'第一项正文'}]
    assert rendered[2]['content'][0]['blocks']==[{'type':'paragraph','text':'第二项正文'}]
    assert response['blocks'][2]['content'][0]['parent_id']=='main'


def test_fully_billed_term_truncation_uses_only_one_configured_fallback(tmp_path,skill,monkeypatch):
    from tests.test_production import prepared
    from sourceloom.production import Production
    from sourceloom.production_contracts import TermPreparation
    from sourceloom.providers import Provider
    s,q,p,bundle=prepared(tmp_path,skill);j=q.enqueue(p['id'],bundle)
    engine=Production(s,{'provider':'manual','call_timeout':30,'fallback_providers':{'term_preparation':{}}})
    monkeypatch.setattr(engine.queue,'cancelled',lambda *a:False)
    roles=[]
    def call(self,pid,role,payload,schema,job,cancel):
        roles.append(role)
        record=dict(id='term-'+str(len(roles)),role=role,status='truncated',finish_reason='length',response_blob=s.blob(b'{}'))
        job['calls'].append(record)
        with s.connect() as cx:
            cx.execute('INSERT INTO spending(id,project,reserved,actual,created,body) VALUES(?,?,?,?,?,?)',(record['id'],pid,.1,.02,0,'{}'))
        raise ValueError('Truncated response')
    monkeypatch.setattr(Provider,'call',call)
    with pytest.raises(ValueError,match='Truncated'):
        engine._call(j,'terms-u','term_preparation',{},TermPreparation)
    assert roles==['term_preparation','term_preparation__fallback']
    assert j['fallbacks']['terms-u']['original_call']=='term-1'


def test_default_does_not_invent_subscription_authorization_limit(monkeypatch,tmp_path):
    monkeypatch.setenv('SOURCELOOM_CONFIG',str(tmp_path/'missing.json'))
    assert load_config()['subscription_call_limit'] is None


def test_prior_object_bytes_avoid_repeating_a_shared_page_but_never_cover_new_facts():
    import copy
    from sourceloom.checks import inspect_draft
    from sourceloom.writing import protected_objects
    inv={'frozen':True,'objects':[{'id':'s','kind':'code','text':'print(1)','fence_raw':'```python\nprint(1)\n```'}],
         'obligations':[{'id':'f','object_id':'s'}],'resources':[]}
    literal=protected_objects(inv)['s']
    earlier={'blocks':[{'id':'old','unit_id':'a','kind':'source','markdown':literal,'obligation_ids':[],
                        'object_ids':['s'],'embedded_object_ids':['s'],'evidence':[]}]}
    current={'blocks':[{'id':'new','unit_id':'b','kind':'explanation','markdown':'解释这次输出',
                        'obligation_ids':['f'],'object_ids':[],'evidence':[{'source_id':'s','quote':'print(1)'}]}]}
    assert any(i['code']=='protected_object' for i in inspect_draft(inv,current))
    assert inspect_draft(inv,current,prior_draft=earlier)==[]
    changed=copy.deepcopy(earlier);changed['blocks'][0]['markdown']='changed'
    assert any(i['code']=='protected_object' for i in inspect_draft(inv,current,prior_draft=changed))
    current['blocks'][0]['obligation_ids']=[]
    assert any(i['code']=='omission' for i in inspect_draft(inv,current,prior_draft=earlier))


def test_visual_audit_repairs_only_failed_page_then_rechecks_whole_batch(tmp_path,skill,monkeypatch):
    from io import BytesIO
    from PIL import Image
    from tests.test_production import prepared
    from sourceloom.production import Production
    s,q,p,b=prepared(tmp_path,skill);j=q.enqueue(p['id'],b)
    buf=BytesIO();Image.new('RGB',(10,10),'white').save(buf,'PNG');key=s.blob(buf.getvalue())
    j['source']['objects']=[dict(id=sid,kind='page',resource_id=key,locator=sid,text='original') for sid in ['a','b']]
    j['source']['unknown']=[];j['stage']='visual_audit'
    original_b=dict(source_id='b',source_markdown='untouched',figure_descriptions=[],unresolved=[])
    j['visual_candidate']={'pages':[dict(source_id='a',source_markdown='# running header',figure_descriptions=[],unresolved=[]),original_b]}
    engine=Production(s,{});calls=[]
    def call(job,key,role,payload,schema):
        calls.append(key)
        assert all(set(p)=={'id','kind','locator'} for p in payload['pages'])
        if role=='visual_extract':
            assert [p['id'] for p in payload['pages']]==['a']
            return {'pages':[dict(source_id='a',source_markdown='running header',figure_descriptions=[],unresolved=[])]}
        if key.endswith('recheck'):assert payload['extraction']['pages'][1]==original_b
        return {'pages':[dict(source_id=sid,checks=[dict(category=cat,status='changed' if sid=='a' and cat=='text' and not key.endswith('recheck') else 'preserved',region='whole image',observation='actual image comparison',transcript_excerpt='running header') for cat in ['text','reading_order','tables','formulas','figures','captions','footnotes']]) for sid in ['a','b']]}
    monkeypatch.setattr(engine,'_call',call)
    assert engine.step(j)=='queued' and j['stage']=='inventory'
    assert j['source']['objects'][1]['text']=='untouched'
    assert calls==['visual_audit-0-image-v2','visual_audit-0-image-v2-repair','visual_audit-0-image-v2-recheck']


def test_source_groups_preserve_objects_and_merge_same_local_fact_ids():
    from sourceloom.source_context import objects_view,inventory_groups,merge_inventories
    objects=[dict(id=str(i),text='x'*7000,raw='<original>',coordinates=[1,2],visual_audit={'private':'previous review'}) for i in range(3)]
    groups=inventory_groups(objects)
    assert [o for group in groups for o in group]==objects and len(groups)==3
    view=objects_view(objects)
    assert all(o['text']=='x'*7000 and o['raw']=='<original>' for o in view)
    assert all('coordinates' not in o and 'visual_audit' not in o for o in view)
    parts=[dict(facts=[dict(id='f1',source_id=str(i),meaning='exact')],assessed_source_ids=[str(i)],unresolved=['original ambiguity']) for i in range(3)]
    result=merge_inventories(parts)
    assert len({f['id'] for f in result['facts']})==3 and result['assessed_source_ids']==['0','1','2']
    assert result['unresolved']==['original ambiguity']*3


def test_progress_does_not_deserialize_large_model_history(tmp_path,monkeypatch):
    app=create_app(load_config()|{'data_dir':str(tmp_path),'provider':'manual'});s=app.state.store;p=s.create('Long source')
    s.put_job(dict(id='large-job',project=p['id'],role='production',status='running',created=1,stage='inventory',calls=[],results={'old':'x'*2000000},inventory_group_index=1,inventory_group_count=4))
    lengths=[];original=json.loads
    def counted(value,*args,**kwargs):
        lengths.append(len(value));return original(value,*args,**kwargs)
    monkeypatch.setattr(json,'loads',counted)
    with TestClient(app) as client:r=client.get('/api/projects/'+p['id']+'/production')
    assert r.status_code==200 and len(r.content)<3000
    assert max(lengths)<10000
    assert r.json()['progress']['label']=='正在清点第 2 / 4 组原文'


def test_inline_original_link_keeps_sentence_and_source_binding(tmp_path):
    from sourceloom.writing import compose
    runtime=tmp_path/'runtime';runtime.mkdir()
    (runtime/'composition.py').write_text('def render_document(body, sources):\n return body["blocks"][0]["text"]+"\\n"\n',encoding='utf8')
    inv={'objects':[{'id':'link','kind':'link','text':'CVEs','target':'https://cve.mitre.org/'}],
         'obligations':[{'id':'fact','object_id':'link'}]}
    body={'blocks':[{'id':'b','unit_id':'u','kind':'source','content':[
        {'type':'paragraph','text':'这些版本修复了 {{source:link}}，并同步发布'}]}]}
    result=compose({'root':str(tmp_path)},body,inv)['blocks'][0]
    assert result['markdown']=='这些版本修复了 [CVEs](https://cve.mitre.org/)，并同步发布'
    assert result['embedded_object_ids']==['link'] and result['obligation_ids']==['fact']
    assert result['evidence']==[{'source_id':'link','quote':'CVEs'}]
    body['blocks'][0]['content'][0]['text']='{{source:missing}}'
    with pytest.raises(ValueError,match='现有的单行链接'):compose({'root':str(tmp_path)},body,inv)


def test_budget_guard_identifies_document_setting_not_provider_balance(tmp_path):
    s=Store(tmp_path);p=s.create('Budget',budget=.1)
    with pytest.raises(Conflict,match='本篇费用保护暂停.*不是模型账户余额耗尽'):
        s.reserve(p['id'],'one',.2,{'channel':'openai-compatible'})
    s.change(p['id'],lambda p:p.update(max_calls=0))
    with pytest.raises(Conflict,match='本篇流程保护暂停.*不是订阅账户额度耗尽'):
        s.reserve(p['id'],'two',0,{'channel':'openai-compatible'})


def test_inventory_patch_preserves_untouched_facts_and_rejects_identity_conflicts():
    import copy
    from sourceloom.source_context import patch_inventory
    fact=dict(id='f',source_id='s',quote='original',meaning='incorrect annotation',person='we',pronouns=['we'],referents=[],conditions=['when'],negations=[],quantities=[],modality='may')
    original={'facts':[fact,fact|{'id':'untouched'}],'assessed_source_ids':['s'],'unresolved':['page break']}
    before=copy.deepcopy(original)
    patch=dict(replacements=[fact|{'meaning':'corrected annotation'}],additions=[],remove_ids=[],unresolved=[],rationale='Adjacent original pages complete this sentence')
    result=patch_inventory(original,patch)
    assert result['facts'][1]==original['facts'][1] and original==before and not result['unresolved']
    patch['remove_ids']=['f']
    with pytest.raises(ValueError,match='身份冲突'):patch_inventory(original,patch)


def test_explicit_inventory_delta_keeps_unknown_call_and_spending(tmp_path,skill):
    from tests.test_production import prepared
    s,q,p,bundle=prepared(tmp_path,skill);job=q.enqueue(p['id'],bundle)
    job.update(status='uncertain',stage='inventory_audit',pending='inventory_semantic_repair-1__fallback',facts={'facts':[{}]*81},results={'inventory_audit':{}},calls=[dict(id='unknown',role='inventory_repair__fallback',status='uncertain',step_key='inventory_semantic_repair-1__fallback')])
    s.put_job(job);s.change(p['id'],lambda p:p.update(active_job=None,state='uncertain'))
    s.reserve(p['id'],'unknown',.05,{'channel':'openai-compatible'});s.settle('unknown',None,{'error':'ReadError'})
    before=s.costs(p['id']);continued=q.continue_inventory_patch(job['id'])
    assert continued['calls']==job['calls'] and continued['results']==job['results'] and s.costs(p['id'])==before
    assert 'pending' not in continued and continued['inventory_patch_continuation']['original_call']=='unknown'
    with pytest.raises(Conflict):q.continue_inventory_patch(job['id'])


def test_image_detail_views_keep_original_and_label_overlapping_coordinates(tmp_path):
    from PIL import Image
    from io import BytesIO
    from sourceloom.visual_sources import image_resources
    s=Store(tmp_path);buf=BytesIO();Image.new('RGB',(1000,1000),'white').save(buf,'PNG');original=buf.getvalue();key=s.blob(original)
    pages=[dict(id='image',kind='image',resource_id=key,locator='original')]
    images=image_resources(s,pages,{},['image'])
    assert images[0]=={'source_id':'image','sha256':key} and len(images)==5
    assert s.read_blob(key)==original
    assert images[1]['pixel_bounds']==[0,0,600,600] and images[-1]['pixel_bounds']==[400,400,1000,1000]
    assert all(x['derived_from']==key and x['original_size']==[1000,1000] for x in images[1:])


def test_web_scripts_are_complete_inert_metadata_not_silently_executed_or_dropped():
    import copy
    from sourceloom.source_context import classify_inert_markup
    from sourceloom.writing import protected_objects
    raw='<script src="original.js">\nhtml();\n</script>'
    source={'objects':[dict(id='script',kind='unknown',text='html();',raw=raw),dict(id='canvas',kind='unknown',text='',raw='<canvas></canvas>')],
            'unknown':[dict(object_id='script',reason='script 未执行，需要独立解释或安全转换'),dict(object_id='canvas',reason='canvas 未执行，需要独立解释或安全转换')]}
    before=copy.deepcopy(source);result=classify_inert_markup(source)
    assert source==before and result['unknown']==source['unknown'][1:]
    assert result['objects'][0]['kind']=='metadata' and result['objects'][0]['text']==raw
    assert protected_objects(result)['script']=='```html\n'+raw+'\n```'
    assert result['objects'][1]==source['objects'][1]


def test_progress_snapshot_tracks_direct_queue_updates_without_reading_job_history(tmp_path):
    import sqlite3
    from sourceloom.progress import latest,FIELDS
    s=Store(tmp_path);p=s.create('Progress')
    job=dict(id='j',project=p['id'],role='production',status='running',created=1,stage='writer',calls=[],results={'large':'x'*2000000})
    s.put_job(job)
    with s.connect() as cx:
        job.update(status='failed',error='Exact failure')
        cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',('failed',json.dumps(job),'j'))
        def authorizer(action,table,column,*args):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and table=='jobs' and column=='body' else sqlite3.SQLITE_OK
        cx.set_authorizer(authorizer)
        result=dict(zip(FIELDS,json.loads(latest(cx,p['id'])[0])))
    assert result['status']=='failed' and result['error']=='Exact failure'


def test_unsaved_candidate_progress_does_not_read_model_history(tmp_path):
    import sqlite3
    from contextlib import contextmanager
    from sourceloom.progress import candidate
    from sourceloom.writing import canonical
    config=load_config()|{'data_dir':str(tmp_path),'provider':'manual'}
    app=create_app(config);s=app.state.store;p=s.create('Draft in progress')
    draft={'blocks':[{'id':'b','unit_id':'u','kind':'explanation','markdown':'Actual draft','obligation_ids':[],'object_ids':[],'evidence':[]}]}
    job=dict(id='j',project=p['id'],role='production',status='running',created=1,stage='style',calls=[],
             draft=draft,plan={'units':[{'id':'u'}]},results={'large':'x'*2000000})
    s.put_job(job)
    connect=s.connect
    @contextmanager
    def without_history():
        with connect() as cx:
            cx.set_authorizer(lambda action,table,column,*args: sqlite3.SQLITE_DENY if
                action==sqlite3.SQLITE_READ and table=='jobs' and column=='body' else sqlite3.SQLITE_OK)
            yield cx
    with patch.object(s,'connect',without_history),patch.object(IntakeQueue,'latest',return_value=None),TestClient(app) as c:
        response=c.get(f"/api/projects/{p['id']}/production")
        assert response.status_code==200
        assert response.json()['has_output'] and response.json()['output_chars']==len(canonical(draft))
    job['draft']['blocks'][0]['markdown']='New actual draft'
    with connect() as cx:
        cx.execute('UPDATE jobs SET body=? WHERE id=?',(json.dumps(job),'j'))
        assert json.loads(candidate(cx,'j')[0])==job['draft']
        cx.execute('DELETE FROM jobs WHERE id=?',('j',))
        assert cx.execute('SELECT count(*) FROM job_candidates').fetchone()[0]==0


def test_saved_missing_fact_replay_is_idempotent_and_conflicts_do_not_overwrite():
    from sourceloom.production import merge_inventory_additions
    original={'facts':[dict(id='a',source_id='s',quote='old parser view',meaning='one')], 'assessed_source_ids':['s'],'unresolved':[]}
    extra=dict(id='b',source_id='s',quote='',meaning='two')
    source={'objects':[dict(id='s',text='complete retained original')]}
    once=merge_inventory_additions(original,[extra],source)
    twice=merge_inventory_additions(once,[extra],source)
    assert once==twice and [f['id'] for f in twice['facts']]==['a','b']
    with pytest.raises(ValueError,match='未覆盖任何事实'):
        merge_inventory_additions(once,[extra|{'meaning':'changed'}],source)
    assert original['facts'][0]['quote']=='old parser view'


def test_empty_text_image_container_has_exact_empty_evidence_not_a_fabricated_quote():
    from sourceloom.checks import source_quote_matches
    from sourceloom.production_contracts import SourceFact
    from sourceloom.production import bind_source_quotes,validate_facts
    fact=dict(id='f',source_id='paragraph',quote='',meaning='Paragraph contains the separately retained image',person='none',pronouns=[],referents=[],conditions=[],negations=[],quantities=[],modality='structure')
    source={'objects':[dict(id='paragraph',kind='text',text='',raw='<p><image/></p>')]}
    body=bind_source_quotes({'facts':[fact],'assessed_source_ids':['paragraph']},source)
    SourceFact.model_validate(body['facts'][0]);validate_facts(body,source)
    assert source_quote_matches('','') and not source_quote_matches('','nonempty original')
    with pytest.raises(ValueError):validate_facts(body,{'objects':[{'id':'paragraph','text':'nonempty original'}]})
