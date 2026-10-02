import json

import pytest
from pydantic import BaseModel

from sourceloom.active_composition import ActiveComposition
from sourceloom.durable import Queue
from sourceloom.production import Production
from sourceloom.store import Conflict
from tests.test_production import prepared, skill


class _Answer(BaseModel):
    answer: str


def _config(with_go=True):
    routes={
        'kuafu-chat':{'provider':'openai-compatible','provider_id':'kuafu-chat',
            'model':'deepseek-v4.1-flash','protocol':'chat_completions',
            'base_url':'https://api.kuafushe.cc/v1','backup_provider_id':'kuafu-responses','enabled':True},
        'kuafu-responses':{'provider':'openai-compatible','provider_id':'kuafu-responses',
            'model':'deepseek-v4.1-flash','protocol':'responses','endpoint':'/responses',
            'base_url':'https://api.kuafushe.cc/v1','backup_provider_id':'kuafu-chat','enabled':True},
    }
    config={'provider_routes':routes,
        'provider_credentials':{'kuafu-chat':'chat-test-key','kuafu-responses':'responses-test-key'},
        'role_providers':{'active_write':routes['kuafu-chat']|{'api_key':'chat-test-key'}}}
    if with_go:
        config['fallback_providers']={'active_write':{
            'provider':'openai-compatible','provider_id':'opencode-go','model':'deepseek-v4.1-flash',
            'protocol':'chat_completions','base_url':'https://opencode.ai/zen/go/v1','api_key':'go-test-key',
            'headers':{'x-opencode-session':'session-test'}}}
    return config


def _failed_pair(store,job,project,key):
    chat_error=store.blob(json.dumps({'error':{'code':'upstream_service_error'}}).encode())
    calls=[
        dict(id='chat-524',role='active_write',status='uncertain',step_key=key,
            channel='openai-compatible',provider_id='kuafu-chat',protocol='chat_completions',
            upstream_base='https://api.kuafushe.cc/v1',http_status=524,
            error_code='upstream_service_error',response_blob=chat_error,dispatch_started=True),
        dict(id='responses-incomplete',role='active_write',status='uncertain',step_key=key,
            channel='openai-compatible',provider_id='kuafu-responses',protocol='responses',streaming=True,
            upstream_base='https://api.kuafushe.cc/v1',http_status=200,
            error_code='incomplete_sse',response_blob=None,upstream_id=None,dispatch_started=True),
    ]
    job.update(core_chain_version=0,status='uncertain',stage='active_write',pending=key,current_step_key=key,
        results={'completed-prior-stage':{'kept':['a','b']}},calls=calls)
    store.put_job(job)
    store.change(project['id'],lambda p:p.update(active_job=None,state='uncertain'))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='uncertain',owner=NULL,lease_until=0 WHERE id=?",
                   (job['id'],))
        for call in calls:
            cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body) VALUES(?,?,?,?,?,?)',
                (call['id'],project['id'],.02,None,'unknown',json.dumps({'status':'unknown','call_id':call['id']})))
    return calls


def test_failed_reciprocal_pair_queues_go_once_and_preserves_ledger_and_checkpoints(
        tmp_path,skill,monkeypatch):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=queue.enqueue(project['id'],bundle)
    key='active-write-p1-n-001-turn-0'
    prior_calls=_failed_pair(store,job,project,key)
    config=_config()

    resumed=queue.retry_validation(job['id'],config)
    state=resumed['transport_recovery_routes'][key]
    assert resumed['status']=='queued' and resumed.get('pending') is None
    assert state['status']=='queued' and state['attempts']==0
    assert state['recovery_protocol_version']=='kuafu-opencode-go-third-v1'
    assert state['original_call_ids']==[call['id'] for call in prior_calls]
    assert state['unknown_call_ids']==[call['id'] for call in prior_calls]
    assert resumed['results']=={'completed-prior-stage':{'kept':['a','b']}}
    assert len(resumed['calls'])==2
    with store.connect() as cx:
        receipts={row['id']:(row['actual'],row['status']) for row in
                  cx.execute('SELECT id,actual,status FROM spending').fetchall()}
    assert receipts['chat-524']==(None,'unknown')
    assert receipts['responses-incomplete']==(None,'unknown')

    seen={}
    def go_call(provider,pid,role,payload,schema,claimed,cancelled):
        seen.update(role=role,provider_id=provider.config['provider_id'],
            base_url=provider.config['base_url'],backup=provider.config.get('backup_provider_id'),
            quota_fallback=provider.config.get('quota_fallback'),
            key=provider.config.get('api_key'),headers=provider.config.get('headers'),
            thinking=provider.config['role_options'][role]['thinking'])
        claimed['calls'].append(dict(id='go-call',role=role,status='completed',step_key=key,
            channel='openai-compatible',provider_id='opencode-go',protocol='chat_completions',
            upstream_base='https://opencode.ai/zen/go/v1',dispatch_started=True))
        return {'answer':'complete Go response'}
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',go_call)
    engine=ActiveComposition(Production(store,config))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    assert engine.call(resumed,key,'active_write',{'source':'same frozen step'},_Answer)=={
        'answer':'complete Go response'}
    assert seen=={'role':'active_write__fallback','provider_id':'opencode-go',
        'base_url':'https://opencode.ai/zen/go/v1','backup':None,'quota_fallback':None,
        'key':'go-test-key','headers':{'x-opencode-session':'session-test'},
        'thinking':{'type':'disabled'}}
    assert state['status']=='completed' and state['attempts']==1
    assert resumed['results'][key]=={'answer':'complete Go response'}
    assert resumed['results']['completed-prior-stage']=={'kept':['a','b']}
    assert resumed['route_switches'][-1]['reason']=='kuafu_pair_failure_opencode_go_third_route'



def test_unknown_go_dispatch_is_preserved_and_never_queued_again(tmp_path,skill):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=queue.enqueue(project['id'],bundle)
    key='active-write-p1-n-001-turn-0'
    _failed_pair(store,job,project,key)
    config=_config()
    queued=queue.retry_validation(job['id'],config)
    state=queued['transport_recovery_routes'][key]
    state.update(status='dispatching',attempts=1)
    queued['calls'].append(dict(id='go-unknown',role='active_write__fallback',status='uncertain',
        step_key=key,channel='openai-compatible',provider_id='opencode-go',protocol='chat_completions',
        upstream_base='https://opencode.ai/zen/go/v1',dispatch_started=True))
    queued.update(status='uncertain',pending=key)
    store.put_job(queued)
    store.change(project['id'],lambda p:p.update(active_job=None,state='uncertain'))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='uncertain',owner=NULL,lease_until=0 WHERE id=?",
                   (job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body) VALUES(?,?,?,?,?,?)',
            ('go-unknown',project['id'],.03,None,'unknown',json.dumps({'status':'unknown','call_id':'go-unknown'})))
    with pytest.raises(Conflict):
        queue.retry_validation(job['id'],config)
    persisted=store.job(job['id'])
    assert persisted['transport_recovery_routes'][key]['attempts']==1
    assert persisted['transport_recovery_routes'][key]['status']=='dispatching'
    assert len([call for call in persisted['calls'] if call.get('provider_id')=='opencode-go'])==1
    with store.connect() as cx:
        row=cx.execute('SELECT actual,status FROM spending WHERE id=?',('go-unknown',)).fetchone()
    assert row['actual'] is None and row['status']=='unknown'


def _failed_billed_go_reasoning(store,job,project,key,*,thinking='enabled',content='',
                                refusal=None,bill_status='settled',actual=.1744065,
                                call_id='go-reasoning-empty'):
    import json
    message={'content':content,'tool_calls':[]}
    if refusal is not None:message['refusal']=refusal
    response={'choices':[{'finish_reason':'length','message':message}],
        'usage':{'completion_tokens':16382,
            'completion_tokens_details':{'reasoning_tokens':16382}}}
    wire={'model':'deepseek-v4.1-flash','max_tokens':16384,
          'thinking':{'type':thinking}}
    go=dict(id=call_id,role='active_write__fallback',status='reasoning_exhausted',
        step_key=key,channel='openai-compatible',provider_id='opencode-go',protocol='chat_completions',
        upstream_base='https://opencode.ai/zen/go/v1',dispatch_started=True,finish_reason='length',
        response_blob=store.blob(json.dumps(response).encode()),
        wire_request_blob=store.blob(json.dumps(wire).encode()))
    job=store.job(job['id'])
    job['calls'].append(go)
    job['transport_recovery_routes'][key].update(status='failed',attempts=1)
    job.update(status='failed',stage='active_write',pending=key,
        results={'completed-prior-stage':{'kept':['a','b']}})
    store.put_job(job)
    store.change(project['id'],lambda p:p.update(active_job=None,state='failed'))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed',owner=NULL,lease_until=0 WHERE id=?",
                   (job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body) VALUES(?,?,?,?,?,?)',
            (go['id'],project['id'],.72465516,actual,bill_status,json.dumps({
                'status':bill_status,'model':'deepseek-v4.1-flash','usage':response['usage']})))
    return job,go,response


def test_billed_empty_go_reasoning_result_queues_one_same_job_disabled_retry_and_preserves_costs(
        tmp_path,skill,monkeypatch):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=queue.enqueue(project['id'],bundle)
    key='active-write-p1-n-001-turn-0'
    _failed_pair(store,job,project,key)
    config=_config()
    queued=queue.retry_validation(job['id'],config)
    first_route=queued['transport_recovery_routes'][key]
    assert first_route['recovery_protocol_version']=='kuafu-opencode-go-third-v1'

    failed,go,response=_failed_billed_go_reasoning(store,queued,project,key)
    before={row['id']:(row['actual'],row['status']) for row in store.costs(project['id'])}
    resumed=queue.retry_validation(job['id'],config)
    retry=resumed['transport_recovery_routes'][key]
    assert resumed['status']=='queued' and resumed.get('pending') is None
    assert resumed['results']=={'completed-prior-stage':{'kept':['a','b']}}
    assert len(resumed['calls'])==3
    assert retry['status']=='queued' and retry['attempts']==0
    assert retry['previous_go_call_id']==go['id']
    assert retry['thinking_mode']=='disabled'
    assert retry['recovery_protocol_version']=='kuafu-opencode-go-thinking-disabled-v1'
    assert resumed['transport_recovery_history'][key][-1]['recovery_protocol_version']=='kuafu-opencode-go-third-v1'
    assert json.loads(store.read_blob(go['response_blob']))==response
    after_queue={row['id']:(row['actual'],row['status']) for row in store.costs(project['id'])}
    assert after_queue==before

    seen={}
    def recovered(provider,pid,role,payload,schema,claimed,cancelled):
        seen.update(role=role,provider_id=provider.config['provider_id'],
            thinking=provider.config['role_options'][role]['thinking'])
        claimed['calls'].append(dict(id='go-disabled-retry',role=role,status='completed',
            step_key=key,channel='openai-compatible',provider_id='opencode-go',
            protocol='chat_completions',upstream_base='https://opencode.ai/zen/go/v1',
            dispatch_started=True))
        return {'answer':'completed with thinking disabled'}
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',recovered)
    engine=ActiveComposition(Production(store,config))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    assert engine.call(resumed,key,'active_write',{'source':'same frozen step'},_Answer)=={
        'answer':'completed with thinking disabled'}
    assert seen=={'role':'active_write__fallback','provider_id':'opencode-go',
        'thinking':{'type':'disabled'}}
    assert retry['status']=='completed' and retry['attempts']==1
    assert len([call for call in resumed['calls'] if call.get('provider_id')=='opencode-go'])==2


def test_go_thinking_retry_refuses_refusal_unknown_unbilled_missing_key_and_repeated_disabled_attempt(
        tmp_path,skill):
    import pytest
    from sourceloom.production import queue_go_thinking_disabled_retry
    from sourceloom.store import Conflict

    def failed_case(tmp_path,*,content='',thinking='enabled',refusal=None,bill_status='settled',actual=.1,
                    with_key=True,version='kuafu-opencode-go-third-v1',response_present=True):
        store,_,project,bundle=prepared(tmp_path,skill)
        job=Queue(store,pipeline='active_composition_v2').enqueue(project['id'],bundle)
        key='active-write-p1-n-001-turn-0'
        _failed_pair(store,job,project,key)
        config=_config()
        queued=Queue(store,pipeline='active_composition_v2').retry_validation(job['id'],config)
        saved,go,_response=_failed_billed_go_reasoning(store,queued,project,key,
            thinking=thinking,content=content,refusal=refusal,bill_status=bill_status,actual=actual)
        if not with_key:
            config['fallback_providers']['active_write'].pop('api_key',None)
            config['provider_credentials'].pop('opencode-go',None)
        if version!='kuafu-opencode-go-third-v1':
            saved['transport_recovery_routes'][key].update(
                status='failed',recovery_protocol_version=version)
            store.put_job(saved)
        billing={'actual':actual,'status':bill_status}
        if billing['status']!='settled':billing['actual']=None
        if not response_present:
            go['response_blob']=None
            saved['calls'][-1]=go
            store.put_job(saved)
        return store,saved,go,config,key,billing

    cases=[
        dict(content='I cannot help with that request'),
        dict(refusal='I cannot help with that request'),
        dict(bill_status='unknown',actual=None),
        dict(with_key=False),
        dict(thinking='disabled'),
        dict(version='kuafu-opencode-go-thinking-disabled-v1'),
        dict(response_present=False,bill_status='unknown',actual=None),
    ]
    for index,options in enumerate(cases):
        case_root=tmp_path/str(index)
        case_root.mkdir()
        store,job,go,config,key,billing=failed_case(case_root,**options)
        history=(job.get('transport_recovery_history') or {}).get(key,[])
        assert not queue_go_thinking_disabled_retry(job,config,store,billing)
        assert job['transport_recovery_routes'][key]['recovery_protocol_version']==options.get(
            'version','kuafu-opencode-go-third-v1')
        assert (job.get('transport_recovery_history') or {}).get(key,[])==history
        assert job['calls'][-1]['id']==go['id']
        if options.get('version')=='kuafu-opencode-go-thinking-disabled-v1':
            with pytest.raises(Conflict):
                Queue(store,pipeline='active_composition_v2').retry_validation(job['id'],config)
            terminal=store.job(job['id'])
            assert terminal['status']=='failed'
            assert len(terminal['calls'])==3


def test_second_billed_go_reasoning_exhaustion_gets_one_pro_escalation(tmp_path,skill,monkeypatch):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=queue.enqueue(project['id'],bundle)
    key='active-write-p1-n-001-turn-0'
    _failed_pair(store,job,project,key)
    config=_config()
    config['provider_options']={'reasoning_effort':'low'}
    config['role_options']={'active_write__fallback':{'reasoning_effort':'low'}}

    first_route=queue.retry_validation(job['id'],config)['transport_recovery_routes'][key]
    first_failed,first,_=_failed_billed_go_reasoning(store,job,project,key,
        call_id='go-first-empty',thinking='enabled')
    disabled_route=queue.retry_validation(job['id'],config)['transport_recovery_routes'][key]
    assert disabled_route['recovery_protocol_version']=='kuafu-opencode-go-thinking-disabled-v1'
    assert disabled_route['previous_go_call_id']==first['id']

    second_failed,second,_=_failed_billed_go_reasoning(store,first_failed,project,key,
        call_id='go-second-empty',thinking='disabled')
    costs_before={row['id']:(row['actual'],row['status']) for row in store.costs(project['id'])}
    resumed=queue.retry_validation(job['id'],config)
    escalation=resumed['transport_recovery_routes'][key]
    assert escalation['status']=='queued' and escalation['attempts']==0
    assert escalation['recovery_protocol_version']=='kuafu-opencode-go-pro-escalation-v1'
    assert escalation['model']=='deepseek-v4-pro'
    assert escalation['previous_go_call_id']==second['id']
    assert resumed['transport_recovery_history'][key][-1]['recovery_protocol_version']==\
        'kuafu-opencode-go-thinking-disabled-v1'
    assert len(resumed['calls'])==4
    assert {row['id']:(row['actual'],row['status']) for row in store.costs(project['id'])}==costs_before
    assert first_route['recovery_protocol_version']=='kuafu-opencode-go-third-v1'
    assert len([call for call in resumed['calls'] if call.get('provider_id')=='opencode-go'])==2

    seen={}
    def pro_call(provider,pid,role,payload,schema,claimed,cancelled):
        seen.update(role=role,provider_id=provider.config['provider_id'],
            model=provider.config['model'],
            provider_options=provider.config.get('provider_options'),
            role_options=provider.config['role_options'][role])
        claimed['calls'].append(dict(id='go-pro-result',role=role,status='completed',step_key=key,
            channel='openai-compatible',provider_id='opencode-go',protocol='chat_completions',
            upstream_base='https://opencode.ai/zen/go/v1',dispatch_started=True))
        return {'answer':'completed with Pro'}
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',pro_call)
    engine=ActiveComposition(Production(store,config))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    assert engine.call(resumed,key,'active_write',{'source':'same frozen step'},_Answer)=={
        'answer':'completed with Pro'}
    assert seen=={'role':'active_write__fallback','provider_id':'opencode-go',
        'model':'deepseek-v4-pro','provider_options':{},
        'role_options':{'thinking':{'type':'disabled'}}}
    assert escalation['status']=='completed' and escalation['attempts']==1
    assert resumed['results'][key]=={'answer':'completed with Pro'}
    assert len([call for call in resumed['calls'] if call.get('provider_id')=='opencode-go'])==3


@pytest.mark.parametrize('failure',[
    'content','unknown_billing','thinking_enabled','wrong_version','wrong_configured_model',
    'missing_key','extra_go_call','first_unknown_billing',
])
def test_pro_escalation_refuses_every_non_exact_case(tmp_path,skill,failure):
    from sourceloom.production import queue_go_pro_reasoning_escalation

    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=queue.enqueue(project['id'],bundle)
    key='active-write-p1-n-001-turn-0'
    _failed_pair(store,job,project,key)
    config=_config()
    route=queue.retry_validation(job['id'],config)['transport_recovery_routes'][key]
    first_failed,first,_=_failed_billed_go_reasoning(store,job,project,key,
        call_id='go-first-empty',thinking='enabled')
    queue.retry_validation(job['id'],config)
    second_failed,second,response=_failed_billed_go_reasoning(store,first_failed,project,key,
        call_id='go-second-empty',thinking='disabled')
    billing={'status':'settled','actual':0.17}
    if failure=='content':
        response['choices'][0]['message']['content']='partial prose'
        second_failed['calls'][-1]['response_blob']=store.blob(json.dumps(response).encode())
        store.put_job(second_failed)
    elif failure=='unknown_billing':
        billing={'status':'unknown','actual':None}
    elif failure=='thinking_enabled':
        wire=json.loads(store.read_blob(second['wire_request_blob']))
        wire['thinking']={'type':'enabled'}
        second_failed['calls'][-1]['wire_request_blob']=store.blob(json.dumps(wire).encode())
        store.put_job(second_failed)
    elif failure=='wrong_version':
        second_failed['transport_recovery_routes'][key]['recovery_protocol_version']='other-v1'
        store.put_job(second_failed)
    elif failure=='wrong_configured_model':
        config['fallback_providers']['active_write']['model']='deepseek-v4-pro'
    elif failure=='missing_key':
        config['fallback_providers']['active_write'].pop('api_key',None)
        config.setdefault('provider_credentials',{}).pop('opencode-go',None)
    elif failure=='extra_go_call':
        extra=dict(second_failed['calls'][-1])|{'id':'extra-go-call'}
        second_failed['calls'].insert(-1,extra)
        store.put_job(second_failed)
    elif failure=='first_unknown_billing':
        with store.connect() as cx:
            cx.execute("UPDATE spending SET actual=NULL,status='unknown' WHERE id=?",(first['id'],))

    current=store.job(job['id'])
    current['pending']=key
    state_before=json.loads(json.dumps(current['transport_recovery_routes'][key]))
    history_before=list((current.get('transport_recovery_history') or {}).get(key,[]))
    assert not queue_go_pro_reasoning_escalation(current,config,store,billing)
    assert current['transport_recovery_routes'][key]==state_before
    assert (current.get('transport_recovery_history') or {}).get(key,[])==history_before
    assert second['id'] in [call['id'] for call in current['calls']]
    assert first['id']==state_before['previous_go_call_id']


def test_missing_go_fallback_or_existing_complete_artifact_does_not_queue_go(tmp_path,skill):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=queue.enqueue(project['id'],bundle)
    key='active-write-p1-n-001-turn-0'
    _failed_pair(store,job,project,key)
    config=_config(with_go=False)
    with pytest.raises(Conflict):
        queue.retry_validation(job['id'],config)
    assert 'transport_recovery_routes' not in store.job(job['id'])

    # A saved current-step artifact is never replaced with new provider output.
    job=store.job(job['id'])
    job['results'][key]={'answer':'saved complete output'}
    store.put_job(job)
    with pytest.raises(Conflict):
        queue.retry_validation(job['id'],_config())
    persisted=store.job(job['id'])
    assert persisted['results'][key]=={'answer':'saved complete output'}
    assert 'transport_recovery_routes' not in persisted
