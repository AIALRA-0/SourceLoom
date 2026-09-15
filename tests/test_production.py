import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sourceloom.app import create_app
from sourceloom.config import load_config
from sourceloom.durable import Queue
from sourceloom.ingest import intake
from sourceloom.production import fidelity_issues, style_issues, validate_facts, bind_source_quotes
from sourceloom.providers import Provider
from sourceloom.skills import deploy_skill, full_prompt, load_bundle
from sourceloom.store import Store, Conflict


@pytest.fixture
def skill(tmp_path):
    root=tmp_path/'writing-skill'
    (root/'references').mkdir(parents=True)
    (root/'constitution').mkdir()
    (root/'assets').mkdir()
    (root/'SKILL.md').write_text('[format](references/format-rules.md)\n[explain](references/explanation-framework.md)\n[formula](references/formula-explanation.md)\n`constitution/principles.md`',encoding='utf-8')
    for name in ('format-rules','explanation-framework','formula-explanation'):
        (root/'references'/f'{name}.md').write_text('FULL-'+name+'\nTAIL-'+name,encoding='utf-8')
    (root/'constitution/principles.md').write_text('FULL-CONSTITUTION',encoding='utf-8')
    (root/'assets/not-a-prompt.bin').write_bytes(b'complete package asset')
    return root


def prepared(tmp_path,skill):
    store=Store(tmp_path/'data')
    p=store.create('Source',budget=1)
    inv=intake(store,[('source.txt',b'We assume x is positive.')])
    store.change(p['id'],lambda p:p.update(inventory=inv))
    bundle=deploy_skill(skill,store.root)
    return store,Queue(store),p,bundle


def test_publish_never_accepts_missing_or_stale_final_reviews(tmp_path,skill):
    from sourceloom.production import Production
    from sourceloom.store import digest
    from sourceloom.writing import canonical
    store,queue,p,bundle=prepared(tmp_path,skill)
    job=queue.enqueue(p['id'],bundle)
    job.update(stage='publish',draft={'blocks':[]},style={'assessments':[]},fidelity={'fact_checks':[]},scan={'format':{}},
               style_draft_digest='stale',fidelity_draft_digest=digest(canonical({'blocks':[]}).encode()))
    assert Production(store,{}).step(job)=='needs_attention'
    assert job['quality_issues']


def test_style_review_precedes_independent_fidelity_and_failure_never_publishes(tmp_path,skill,monkeypatch):
    from sourceloom.production import Production
    from sourceloom.checks import freeze
    store,queue,p,bundle=prepared(tmp_path,skill)
    job=queue.enqueue(p['id'],bundle)
    inv=freeze(store.get(p['id'])['inventory'])
    block=dict(id='b',unit_id='u',kind='explanation',markdown='我们假设 x 为正',obligation_ids=[],object_ids=[],evidence=[])
    job.update(stage='style',draft={'blocks':[block]},inventory=inv,plan={'units':[]})
    report={'format':{'findings':[],'candidates':[]}}
    monkeypatch.setattr('sourceloom.production.scan',lambda *a:report)
    monkeypatch.setattr('sourceloom.production.inspect_draft',lambda *a,**kw:[])
    monkeypatch.setattr('sourceloom.production.rule_catalog',lambda *a:{'FMT-001':{}})
    engine=Production(store,{})
    calls=[]
    response=dict(assessments=[dict(rule_ids=['FMT-001'],status='pass',block_ids=['b'],quotes=['我们假设'],reason='Exact source person')],mechanical_assessments=[],findings=[])
    def call(job,key,role,*args):calls.append(role);return response
    monkeypatch.setattr(engine,'_call',call)
    assert engine.step(job)=='queued' and job['stage']=='teaching' and calls==['style']
    job['stage']='style';job['repair_rounds']=2
    response['assessments'][0]['status']='unknown'
    job['joint_review_before_repair']=False
    assert engine.step(job)=='needs_attention'
    assert job['quality_issues']
    job['joint_review_before_repair']=True
    assert engine.step(job)=='queued' and job['stage']=='fidelity'
    assert job['repair_rounds']==2 and job['quality_issues']


def test_complete_skill_is_frozen_and_each_file_is_checked(tmp_path,skill):
    bundle=deploy_skill(skill,tmp_path/'data')
    assert len(bundle['files'])==6
    assert 'FULL-CONSTITUTION' in full_prompt(bundle)
    assert 'TAIL-formula-explanation' in full_prompt(bundle)
    (skill/'references/format-rules.md').write_text('later version',encoding='utf-8')
    assert 'TAIL-format-rules' in full_prompt(load_bundle(bundle['root'],bundle['package_digest']))
    Path(bundle['root'],'assets/not-a-prompt.bin').write_bytes(b'tampered')
    with pytest.raises(ValueError,match='校验失败'):
        load_bundle(bundle['root'],bundle['package_digest'])


@pytest.mark.parametrize('repaired',[True,False])
def test_duplicate_plan_verdict_gets_one_protocol_repair(tmp_path,skill,monkeypatch,repaired):
    from sourceloom.production import Production
    store,queue,p,bundle=prepared(tmp_path,skill)
    job=queue.enqueue(p['id'],bundle)
    job.update(stage='plan_review',plan={'units':[{'id':'u'}]},facts={'facts':[]},unit_limit=1)
    sid=job['source']['objects'][0]['id']
    verdict={'claim_index':0,'verdict':'not_error','evidence':[{'source_id':sid}],'reason':'原文保留相同条件'}
    calls=[]
    def call(job,key,role,payload,schema):
        calls.append(key)
        if role=='plan_review':return {'status':'ready','issues':['条件可能变化'],'optional_source_limits':[],'essential_missing_sources':[]}
        if key.endswith('-contract'):
            assert payload['invalid_decision']['decisions']==[verdict,verdict]
            return {'decisions':[verdict] if repaired else [verdict,verdict]}
        return {'decisions':[verdict,verdict]}
    engine=Production(store,{});monkeypatch.setattr(engine,'_call',call)
    if repaired:
        assert engine.step(job)=='queued' and job['stage']=='writer'
    else:
        with pytest.raises(ValueError,match='逐项'):engine.step(job)
    assert calls==['plan_review-0','plan_decision-0','plan_decision-0-contract']


def test_redeploy_frozen_skill_keeps_identical_complete_bundle(tmp_path,skill):
    first=deploy_skill(skill,tmp_path/'first')
    again=deploy_skill(first['root'],tmp_path/'first')
    copied=deploy_skill(first['root'],tmp_path/'second')
    assert again == first
    assert copied['files'] == first['files']
    assert copied['package_digest'] == first['package_digest']
    assert full_prompt(copied) == full_prompt(first)
    Path(first['root'],'assets/not-a-prompt.bin').write_bytes(b'tampered')
    with pytest.raises(ValueError,match='校验失败'):
        deploy_skill(first['root'],tmp_path/'third')


@pytest.mark.parametrize('verdict',['not_error','unknown','confirmed_error'])
def test_inventory_uncertainty_requires_independent_evidenced_decision(tmp_path,skill,monkeypatch,verdict):
    from sourceloom.production import Production
    store,queue,p,bundle=prepared(tmp_path,skill)
    job=queue.enqueue(p['id'],bundle)
    source=job['source']['objects'][0]
    fact=dict(id='f',source_id=source['id'],quote=source['text'],meaning=source['text'],
        person='first person',pronouns=['We'],referents=['speaker'],conditions=['x positive'],
        negations=[],quantities=[],modality='assumption',note='')
    job.update(stage='inventory_audit',inventory_semantic_repairs=2,
        facts=dict(facts=[fact],assessed_source_ids=[source['id']],unresolved=[]))
    audit=dict(assessed_source_ids=[source['id']],assessed_fact_ids=['f'],missing_facts=[],errors=[],
        unresolved=['The source does not identify the speaker by name'],uncertainty_assessments=[])
    roles=[]
    def call(job,key,role,payload,schema):
        roles.append(role)
        if role=='inventory_audit':return audit
        assert role=='inventory_decision' and len(payload['claims'])==1
        return dict(decisions=[dict(claim_index=0,verdict=verdict,
            evidence=[dict(source_id=source['id'])],reason='The original retains We without naming its speaker')])
    engine=Production(store,{});monkeypatch.setattr(engine,'_call',call)
    if verdict=='not_error':
        assert engine.step(job)=='queued' and job['stage']=='planner'
    else:
        with pytest.raises(ValueError,match='独立清单审核'):engine.step(job)
    assert roles==['inventory_audit','inventory_decision']


@pytest.mark.parametrize('role',['fact_inventory','writer','planner','term_preparation','line_repair','independent_review','style_contract_repair'])
def test_actual_api_request_contains_unabridged_skill_and_exact_receipt(tmp_path,skill,monkeypatch,role):
    store,queue,p,bundle=prepared(tmp_path,skill)
    job=queue.enqueue(p['id'],bundle)
    captured=[]
    class Response:
        status_code=200
        def json(self):
            return {'choices':[{'finish_reason':'stop','message':{'content':'{"facts":[]}'}}],
                    'usage':{'prompt_tokens':100,'completion_tokens':10}}
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,*args,**kwargs):captured.append(kwargs['json']);return Response()
    monkeypatch.setattr('sourceloom.providers.httpx.AsyncClient',Client)
    c=load_config()|{'provider':'openai-compatible','api_key':'test-not-a-secret','writing_skill_dir':str(skill),
                     'role_providers':{},'max_input_bytes':100000,'daily_budget_usd':1,'total_budget_usd':1}
    Provider(store,c).call(p['id'],role,{'source':'the source'},{'type':'object'},job)
    sent=captured[0]['messages'][0]['content']
    for name,text in bundle['instructions'].items():
        assert text in sent,name
    if role=='writer':
        assert 'You rewrite the supplied source faithfully' in sent
        assert 'For the first unit, begin with a concrete situation' not in sent
        assert 'A first technical definition must be a term node' in sent
    if role=='planner':
        assert 'You plan a faithful rewrite' in sent
        assert 'The first unit must bring the reader' not in sent
        assert 'The plan MUST NOT waive first-use definitions' in sent
    request=json.loads(store.read_blob(job['calls'][0]['request_blob']))
    assert request['system'] in sent
    assert request['payload']['writing_skill_receipt']['deployed_file_count']==6
    assert not job['calls'][0]['file_read_verified']
    assert job['calls'][0]['status']=='completed'


@pytest.mark.parametrize('official',[True,False])
def test_balance_rejection_is_known_only_for_documented_official_provider(tmp_path,skill,monkeypatch,official):
    from sourceloom.providers import Uncertain
    store,queue,p,bundle=prepared(tmp_path,skill);job=queue.enqueue(p['id'],bundle)
    class Response:
        status_code=402
        content=b'{"error":{"type":"insufficient_balance"}}'
    calls=[]
    def post(*args):calls.append(args);return Response()
    monkeypatch.setattr('sourceloom.providers.post_before_deadline',post)
    c=load_config()|{'provider':'openai-compatible','api_key':'test-not-a-secret',
        'base_url':'https://api.deepseek.com/v1' if official else 'https://example.invalid',
        'role_providers':{},'max_input_bytes':100000,'daily_budget_usd':1,'total_budget_usd':1}
    with pytest.raises(ValueError if official else Uncertain):
        Provider(store,c).call(p['id'],'writer',{}, {'type':'object'},job)
    assert len(calls)==1
    assert job['calls'][0]['status']==('rejected' if official else 'uncertain')
    assert job['calls'][0]['http_status']==402
    assert store.read_blob(job['calls'][0]['response_blob'])==Response.content
    assert store.costs(p['id'])[0]['actual']==(0 if official else None)


def test_two_workers_cannot_claim_same_document_and_stale_worker_cannot_commit(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill)
    queued=queue.enqueue(p['id'],bundle)
    with ThreadPoolExecutor(2) as pool:
        claims=list(pool.map(lambda owner:queue.claim(owner),['one','two']))
    assert sum(x is not None for x in claims)==1
    job=next(x for x in claims if x)
    with store.connect() as cx:
        cx.execute('UPDATE production_control SET lease_until=0 WHERE id=?',(job['id'],))
    recovered=Queue(Store(store.root)).claim('replacement')
    assert recovered['id']==queued['id'] and recovered['recovered_lease']
    with pytest.raises(Conflict,match='执行权'):
        store.put_job(job)
    with pytest.raises(Conflict,match='执行权'):
        queue.finish(job,'one','completed')
    queue.finish(recovered,'replacement','queued')


def test_cancel_persists_across_web_and_worker_restart(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill)
    job=queue.enqueue(p['id'],bundle)
    queue.cancel(job['id'])
    replacement=Queue(Store(store.root))
    claimed=replacement.claim('new-process')
    assert replacement.cancelled(claimed['id'],'new-process')
    replacement.finish(claimed,'new-process','cancelled')
    assert store.get(p['id'])['active_job'] is None


def test_trashing_active_document_cancels_without_destroying_source(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill)
    job=queue.enqueue(p['id'],bundle)
    queue.edit_document(p['id'],0,trashed=True)
    assert queue.cancelled(job['id'])
    assert queue.tree()['documents']==[]
    assert queue.tree(True)['documents'][0]['id']==p['id']
    original=store.get(p['id'])['inventory']['originals'][0]
    assert store.read_blob(original['sha256'])==b'We assume x is positive.'


def test_folder_moves_reject_cycles_and_stale_edits(tmp_path,skill):
    store,queue,p,bundle=prepared(tmp_path,skill)
    a=queue.folder('A');b=queue.folder('B',a['id'])
    with pytest.raises(Conflict,match='子文件夹'):
        queue.folder('A',b['id'],a['id'])
    queue.edit_document(p['id'],0,folder=b['id'],move=True)
    with pytest.raises(Conflict,match='已变化'):
        queue.edit_document(p['id'],0,title='stale overwrite')
    with pytest.raises(Conflict,match='先移动'):
        queue.delete_folder(b['id'])
    assert store.get(p['id'])['title']=='Source'


def test_total_budget_cannot_be_reset_by_date_or_project(tmp_path):
    store=Store(tmp_path)
    a,b=store.create('A',budget=1),store.create('B',budget=1)
    store.reserve(a['id'],'a',.6,{},total_budget=1)
    with store.connect() as cx:
        cx.execute('UPDATE spending SET created=0')
    with pytest.raises(Conflict,match='累计测试预算'):
        store.reserve(b['id'],'b',.6,{},total_budget=1)


def test_unknown_subscription_usage_is_not_zero_and_still_counts(tmp_path):
    store=Store(tmp_path)
    p=store.create('A')
    store.reserve(p['id'],'one',0,{'channel':'router'},subscription_calls=1)
    store.settle('one',None,{'channel':'subscription','billing_note':'unavailable'})
    assert store.costs(p['id'])[0]['actual'] is None
    with pytest.raises(Conflict,match='订阅测试请求'):
        store.reserve(p['id'],'two',0,{'channel':'router'},subscription_calls=1)


def test_person_change_fails_even_when_reviewer_claims_all_fact_ids():
    source={'objects':[{'id':'s','text':'We assume x is positive.'}]}
    facts={'facts':[{'id':'f','source_id':'s'}]}
    draft={'blocks':[{'id':'b','markdown':'原文假设 x 为正'}]}
    review={'assessed_source_ids':['s'],'missing_inventory_information':[],
        'fact_checks':[{'fact_id':'f','source_quote':'We assume','block_id':'b','output_quote':'原文假设',
                       'status':'preserved','person_preserved':False,'referents_preserved':True}],
        'reverse_checks':[{'block_id':'b','output_quote':'原文假设','status':'supported','kind':'source',
                           'evidence':[{'source_id':'s','quote':'We assume'}]}],'findings':[]}
    assert any('人称' in s for s in fidelity_issues(review,facts,draft,source))


def test_intrinsic_source_ambiguity_is_kept_for_independent_adjudication():
    source={'objects':[{'id':'s','text':'A couple of fixes were applied.'}]}
    body={'assessed_source_ids':['s'],'facts':[{'id':'f','source_id':'s','quote':'A couple of fixes'}],
          'unresolved':['The source does not give an exact number.']}
    validate_facts(body,source)
    assert body['unresolved']==['The source does not give an exact number.']
    body['assessed_source_ids'].append('not-a-source-file-hash')
    with pytest.raises(ValueError,match='源对象'):
        validate_facts(body,source)


def test_source_evidence_is_copied_by_id_without_rewriting_semantic_annotations():
    source={'objects':[{'id':'s','text':'Version V0.0.9 is unchanged.'}]}
    response={'facts':[{'id':'f','source_id':'s','quote':'Version is unchanged.','meaning':'an incorrect claim'}]}
    bound=bind_source_quotes(response,source)
    assert bound['facts'][0]['quote']==source['objects'][0]['text']
    assert bound['facts'][0]['meaning']=='an incorrect claim'
    assert response['facts'][0]['quote']=='Version is unchanged.'


def test_missing_rule_and_unresolved_scanner_candidate_block_formal_output():
    report={'format':{'findings':[],'candidates':[{'id':'c1'}]}}
    review={'assessments':[{'rule_ids':['FMT-046'],'status':'pass','block_ids':['b'],
                            'quotes':['术语'],'reason':'evidence'}], 'mechanical_assessments':[], 'findings':[]}
    issues=style_issues(review,{'FMT-046':{},'FMT-053':{}},{'blocks':[{'id':'b','markdown':'术语'}]},report)
    assert any('完整规则' in x for x in issues)
    assert any('尚未全部裁决' in x for x in issues)


def test_library_http_survives_new_app_instance(tmp_path,skill):
    c=load_config()|{'data_dir':str(tmp_path/'data'),'provider':'manual','auth_mode':'local','writing_skill_dir':str(skill)}
    headers={'X-SourceLoom':'1'}
    with TestClient(create_app(c)) as client:
        folder=client.post('/api/library/folders',json={'name':'Papers'},headers=headers).json()
        p=client.post('/api/projects',json={'title':'A paper'},headers=headers).json()
        assert client.patch('/api/library/documents/'+p['id'],headers=headers,
                            json={'library_revision':0,'folder':folder['id']}).status_code==200
    with TestClient(create_app(c)) as client:
        tree=client.get('/api/library').json()
        assert tree['documents'][0]['folder']==folder['id']
        assert 'library.js' in client.get('/').text


@pytest.mark.parametrize('thinking',[None,'enabled','disabled'])
def test_deepseek_strict_artifact_channel_never_executes_tools(tmp_path,skill,monkeypatch,thinking):
    from sourceloom.providers import deepseek_schema
    schema={'type':'object','properties':{'items':{'type':'array','minItems':2,'items':{'type':'string'}}}}
    assert 'minItems' not in deepseek_schema(schema)['properties']['items']
    assert schema['properties']['items']['minItems']==2
    store,queue,p,bundle=prepared(tmp_path,skill)
    job=queue.enqueue(p['id'],bundle)
    captured=[]
    class Response:
        status_code=200
        def json(self):
            return {'choices':[{'finish_reason':'tool_calls','message':{'tool_calls':[
                {'type':'function','function':{'name':'emit_artifact','arguments':'{"items":["one","two"]}'}}]}}],
                'usage':{'prompt_tokens':100,'completion_tokens':10}}
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,**kwargs):captured.append((url,kwargs['json']));return Response()
    monkeypatch.setattr('sourceloom.providers.httpx.AsyncClient',Client)
    config=load_config()|dict(provider='openai-compatible',base_url='https://api.deepseek.com/v1',
        structured_output='deepseek_strict_tool',api_key='test',role_providers={},max_input_bytes=100000,
        provider_options={} if thinking is None else {'thinking':{'type':thinking}})
    payload={'source':{'objects':[{'id':'s','text':'We preserve the condition.'}]},
             'received':{'evidence':[{'source_id':'s','quote':'We preserve the condition.'}]}}
    assert Provider(store,config).call(p['id'],'fact_inventory',payload,schema,job)=={'items':['one','two']}
    url,request=captured[0]
    assert url=='https://api.deepseek.com/beta/chat/completions'
    assert request['tools'][0]['function']['strict'] is True
    assert request['tool_choice']==({'type':'function','function':{'name':'emit_artifact'}} if thinking=='disabled' else 'auto')
    assert json.loads(request['messages'][1]['content'])['received']==payload['received']
    assert request['tools'][0]['function']['parameters']['required']==['items']
    assert 'response_format' not in request
    assert 'TAIL-format-rules' in request['messages'][0]['content']
    assert job['calls'][0]['status']=='completed'


def test_evidence_layout_binding_changes_only_whitespace_and_never_prose():
    from sourceloom.production import bind_evidence_layout,decision_evidence
    source={'objects':[{'id':'one','text':'We use\n  three items.'}]}
    assert bind_evidence_layout({'source_id':'one','quote':'We use three items.'},source)['quote']=='We use\n  three items.'
    assert bind_evidence_layout({'source_id':'one','quote':'They use three items.'},source)['quote']=='They use three items.'
    response={'decisions':[{'claim_index':0,'verdict':'not_error','reason':'Source keeps first person.','evidence':[{'source_id':'one'}]}]}
    bound=decision_evidence(response,source)
    assert bound[0]['evidence'][0]['quote']==source['objects'][0]['text']
    assert 'quote' not in response['decisions'][0]['evidence'][0]
    response['decisions'][0]['evidence'][0]['source_id']='absent'
    with pytest.raises(ValueError,match='不存在'):
        decision_evidence(response,source)


def test_original_preview_is_read_only_and_cannot_execute_uploaded_html(tmp_path):
    c=load_config()|{'data_dir':str(tmp_path/'data'),'provider':'manual'}
    with TestClient(create_app(c)) as client:
        p=client.post('/api/projects',json={'title':'Original'},headers={'X-SourceLoom':'1'}).json()
        raw=b'<h1>We</h1><script>window.evil=1</script><p onclick="evil()">Our source</p>'
        response=client.post('/api/projects/'+p['id']+'/upload',files={'files':('original.html',raw,'text/html')},headers={'X-SourceLoom':'1'})
        assert response.status_code==200
        original=response.json()['inventory']['originals'][0]
        view=client.get('/api/projects/'+p['id']+'/original-view/'+original['sha256'])
        assert '<script>' not in view.text and 'onclick=' not in view.text
        assert 'Our source' in view.text and 'sandbox' in view.headers['Content-Security-Policy']
        assert client.get('/api/projects/'+p['id']+'/original/'+original['sha256']).content==raw


def test_campaign_survives_restarts_and_stops_repeated_failure(tmp_path,skill):
    from sourceloom.trials import check,outcome
    store,queue,p,bundle=prepared(tmp_path,skill)
    c={'trial_campaign':{'id':'one','batch':'a','reason':'first implementation'}}
    check(store,c)
    outcome(store,c,{'stage':'writer','error':'invalid source'},'failed')
    check(Store(store.root),c)
    outcome(store,c,{'stage':'writer','error':'invalid source'},'failed')
    with pytest.raises(Conflict,match='连续发生两次'):
        check(Store(store.root),c)
    with store.connect() as cx:
        cx.execute('UPDATE trial_campaigns SET started=?',(time.time()-10801,))
    c['trial_campaign']['batch']='b'
    check(Store(store.root),c)  # User rejected the assistant-imposed campaign clock.
    c['trial_total_seconds']=100
    with pytest.raises(Conflict,match='明确配置'):
        check(Store(store.root),c)
