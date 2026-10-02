import json

from sourceloom.active_composition import ActiveComposition
from sourceloom.durable import Queue
from sourceloom.production import Production
from tests.test_production import prepared, skill


def _legacy_v2_job(store, queue, pid, bundle):
    job=queue.enqueue(pid,bundle)
    job['core_chain_version']=0
    store.put_job(job)
    return job


def test_go_reasoning_exhaustion_queues_one_untried_kuafu_route_and_dispatches_it(
        tmp_path,skill,monkeypatch):
    from pydantic import BaseModel
    from sourceloom.production import queue_kuafu_response_recovery
    from sourceloom.providers import ReasoningExhausted, Uncertain
    class Shape(BaseModel):
        answer:str
    store,_,project,bundle=prepared(tmp_path,skill)
    job=_legacy_v2_job(store,Queue(store,pipeline='active_composition_v2'),project['id'],bundle)
    key='active-write-p7-n-002-turn-0'
    job.update(stage='active_write',pending=key,current_step_key=key,
        transport_fallback_steps={key:'kuafu-chat-call'},
        transient_gateway_retries={key:{'attempts':1,'previous_call':'kuafu-chat-call'}})
    job['calls']=[
        dict(id='kuafu-chat-call',role='active_write',status='uncertain',step_key=key,
             channel='openai-compatible',provider_id='kuafu-chat',protocol='chat_completions',
             upstream_base='https://api.kuafushe.cc/v1',dispatch_started=True),
        dict(id='go-call',role='active_write__fallback',status='reasoning_exhausted',step_key=key,
             channel='openai-compatible',provider_id='opencode-go',protocol='chat_completions',
             upstream_base='https://opencode.ai/zen/go/v1',dispatch_started=True),
    ]
    config={'provider_routes':{
        'kuafu-chat':{'provider':'openai-compatible','provider_id':'kuafu-chat',
            'model':'deepseek-v4.1-flash','protocol':'chat_completions',
            'base_url':'https://api.kuafushe.cc/v1','backup_provider_id':'kuafu-responses','enabled':True},
        'kuafu-responses':{'provider':'openai-compatible','provider_id':'kuafu-responses',
            'model':'deepseek-v4.1-flash','protocol':'responses','endpoint':'/responses',
            'base_url':'https://api.kuafushe.cc/v1','backup_provider_id':'kuafu-chat','enabled':True},
    },'provider_credentials':{'kuafu-chat':'chat-key','kuafu-responses':'responses-key'}}
    assert queue_kuafu_response_recovery(job,ReasoningExhausted('no artifact'),config)
    state=job['transport_recovery_routes'][key]
    assert state['status']=='queued' and state['provider_id']=='kuafu-responses'
    assert state['original_call_id']=='kuafu-chat-call' and state['exhausted_fallback_call_id']=='go-call'

    observed={}
    def recovered(provider,pid,role,payload,schema,claimed,cancelled):
        observed.update(role=role,provider_id=provider.config['provider_id'],
                        protocol=provider.config['protocol'],backup=provider.config.get('backup_provider_id'))
        claimed['calls'].append(dict(id='responses-recovery-call',role=role,status='completed',
            step_key=key,channel='openai-compatible',provider_id=provider.config['provider_id'],
            protocol=provider.config['protocol'],upstream_base=provider.config['base_url']))
        return {'answer':'recovered'}
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',recovered)
    engine=ActiveComposition(Production(store,config|{
        'role_providers':{'active_write':{'provider':'openai-compatible','provider_id':'kuafu-chat',
            'model':'deepseek-v4.1-flash','protocol':'chat_completions',
            'base_url':'https://api.kuafushe.cc/v1','api_key':'chat-key'}},
        'fallback_providers':{'active_write':{'provider':'openai-compatible','provider_id':'opencode-go',
            'model':'deepseek-v4.1-flash','protocol':'chat_completions','base_url':'https://opencode.ai/zen/go/v1',
            'api_key':'go-key'}}}))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    assert engine.call(job,key,'active_write',{},Shape)=={'answer':'recovered'}
    assert observed=={'role':'active_write','provider_id':'kuafu-responses','protocol':'responses','backup':None}
    assert state['status']=='completed' and state['attempts']==1
    assert state['attempt_call_id']=='responses-recovery-call'
    assert job['route_switches'][-1]['recovery_call_id']=='responses-recovery-call'
    assert job['calls'][0]['status']=='uncertain' and job['calls'][1]['status']=='reasoning_exhausted'

    # A transport or reasoning failure on this last route cannot requeue itself.
    state['status']='failed'
    job['pending']=key
    job['calls'][-1].update(status='uncertain',channel='openai-compatible',upstream_id=None,response_blob=None)
    assert not __import__('sourceloom.production',fromlist=['retryable_v2_gateway_timeout']).retryable_v2_gateway_timeout(
        job,Uncertain('recovery also uncertain'))


def test_failed_pdf_job_retry_validation_queues_recovery_without_job_body_edits(tmp_path,skill,monkeypatch):
    import json
    from pydantic import BaseModel
    class Shape(BaseModel):
        answer:str
    store,queue,project,bundle=prepared(tmp_path,skill)
    job=_legacy_v2_job(store,Queue(store,pipeline='active_composition_v2'),project['id'],bundle)
    key='active-write-p7-n-002-turn-0'
    response={'choices':[{'finish_reason':'length','message':{'content':'','tool_calls':[]}}],
        'usage':{'completion_tokens':90,'completion_tokens_details':{'reasoning_tokens':90}}}
    original=dict(id='kuafu-chat-call',role='active_write',status='uncertain',step_key=key,
        channel='openai-compatible',provider_id='kuafu-chat',protocol='chat_completions',
        upstream_base='https://api.kuafushe.cc/v1',dispatch_started=True)
    fallback=dict(id='go-call',role='active_write__fallback',status='reasoning_exhausted',
        finish_reason='length',response_blob=store.blob(json.dumps(response).encode()),step_key=key,
        channel='openai-compatible',provider_id='opencode-go',protocol='chat_completions',
        upstream_base='https://opencode.ai/zen/go/v1',dispatch_started=True)
    job.update(status='failed',stage='active_write',pending=key,calls=[original,fallback],
        transport_fallback_steps={key:original['id']},
        transient_gateway_retries={key:{'attempts':1,'previous_call':original['id']}})
    store.put_job(job);store.change(project['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body) VALUES(?,?,?,?,?,?)',
            (original['id'],project['id'],0,None,'unknown',json.dumps({'status':'unknown'})))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body) VALUES(?,?,?,?,?,?)',
            (fallback['id'],project['id'],0,.03,'settled',json.dumps({'model':'deepseek-v4.1-flash'})))
    routes={
        'kuafu-chat':{'provider':'openai-compatible','provider_id':'kuafu-chat',
            'model':'deepseek-v4.1-flash','protocol':'chat_completions',
            'base_url':'https://api.kuafushe.cc/v1','backup_provider_id':'kuafu-responses','enabled':True},
        'kuafu-responses':{'provider':'openai-compatible','provider_id':'kuafu-responses',
            'model':'deepseek-v4.1-flash','protocol':'responses','endpoint':'/responses',
            'base_url':'https://api.kuafushe.cc/v1','backup_provider_id':'kuafu-chat','enabled':True},
    }
    config={'provider_routes':routes,'provider_credentials':{'kuafu-chat':'chat-key','kuafu-responses':'responses-key'},
        'role_providers':{'active_write':routes['kuafu-chat']|{'api_key':'chat-key'}},
        'fallback_providers':{'active_write':{'provider':'openai-compatible','provider_id':'opencode-go',
            'model':'deepseek-v4.1-flash','protocol':'chat_completions',
            'base_url':'https://opencode.ai/zen/go/v1','api_key':'go-key'}}}
    resumed=queue.retry_validation(job['id'],config)
    state=resumed['transport_recovery_routes'][key]
    assert resumed['status']=='queued' and not resumed.get('pending')
    assert state['status']=='queued' and state['provider_id']=='kuafu-responses'
    assert resumed['calls'][0]['status']=='uncertain' and resumed['calls'][1]['status']=='reasoning_exhausted'

    seen={}
    def recover(provider,pid,role,payload,schema,claimed,cancelled):
        seen.update(role=role,provider_id=provider.config['provider_id'],protocol=provider.config['protocol'],
                    backup=provider.config.get('backup_provider_id'))
        claimed['calls'].append(dict(id='recovery-call',role=role,status='completed',step_key=key,
            channel='openai-compatible',provider_id=provider.config['provider_id'],
            protocol=provider.config['protocol'],upstream_base=provider.config['base_url']))
        return {'answer':'recovered'}
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',recover)
    engine=ActiveComposition(Production(store,config))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    assert engine.call(resumed,key,'active_write',{},Shape)=={'answer':'recovered'}
    assert seen=={'role':'active_write','provider_id':'kuafu-responses','protocol':'responses','backup':None}
    assert resumed['transport_recovery_routes'][key]['status']=='completed'


def _kuafu_pair_config():
    routes={
        'kuafu-chat':{'provider':'openai-compatible','provider_id':'kuafu-chat',
            'model':'deepseek-v4.1-flash','protocol':'chat_completions',
            'base_url':'https://api.kuafushe.cc/v1','backup_provider_id':'kuafu-responses','enabled':True},
        'kuafu-responses':{'provider':'openai-compatible','provider_id':'kuafu-responses',
            'model':'deepseek-v4.1-flash','protocol':'responses','endpoint':'/responses',
            'base_url':'https://api.kuafushe.cc/v1','backup_provider_id':'kuafu-chat','enabled':True},
    }
    return {'provider_routes':routes,'provider_credentials':{'kuafu-chat':'chat-key','kuafu-responses':'responses-key'},
        'role_providers':{'active_write':routes['kuafu-chat']|{'api_key':'chat-key'}}}


def test_reclaim_interrupted_kuafu_dispatch_atomically_marks_unknown_and_queues_backup_once(
        tmp_path,skill):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=_legacy_v2_job(store,queue,project['id'],bundle)
    key='active-write-p7-n-002-turn-0'
    job.update(status='running',stage='active_write',pending=key,current_step_key=key,
        results={'saved-plan-checkpoint':{'kept':True}},calls=[dict(id='crashed-call',role='active_write',
            status='submitted',step_key=key,channel='openai-compatible',provider_id='kuafu-chat',
            protocol='chat_completions',upstream_base='https://api.kuafushe.cc/v1',dispatch_started=True)])
    store.put_job(job)
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='running',owner='dead-worker',lease_until=0 WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body) VALUES(?,?,?,?,?,?)',
            ('crashed-call',project['id'],.02,None,'reserved',json.dumps({'status':'reserved'})))
    config=_kuafu_pair_config()
    resumed=queue.claim('replacement-worker',recovery_config=config)
    assert resumed['calls'][0]['status']=='uncertain'
    assert resumed['transport_recovery_routes'][key]['status']=='queued'
    assert store.job(job['id'])['transport_recovery_routes'][key]['provider_id']=='kuafu-responses'
    assert resumed['results']=={'saved-plan-checkpoint':{'kept':True}}
    with store.connect() as cx:
        ledger=cx.execute('SELECT actual,status,body FROM spending WHERE id=?',('crashed-call',)).fetchone()
        assert ledger['actual'] is None and ledger['status']=='unknown'
        receipt=json.loads(ledger['body'])
        assert receipt['call_id']=='crashed-call' and receipt['status']=='unknown'
    # A second worker reclaim must not create another recovery record or alter the receipt.
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET lease_until=0 WHERE id=?",(job['id'],))
    reclaimed=queue.claim('second-worker',recovery_config=config)
    assert reclaimed['transport_recovery_routes'][key]['provider_id']=='kuafu-responses'
    assert len([item for item in reclaimed['internal_recoveries']
                if item.get('type')=='interrupted_kuafu_dispatch' and item.get('step')==key])==1
    with store.connect() as cx:
        ledger=cx.execute('SELECT actual,status FROM spending WHERE id=?',('crashed-call',)).fetchone()
        assert ledger['actual'] is None and ledger['status']=='unknown'


def test_resume_uncertain_web_job_preserves_checkpoint_and_routes_only_to_configured_backup(tmp_path,skill):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=_legacy_v2_job(store,queue,project['id'],bundle)
    key='active-write-p7-n-002-turn-0'
    job.update(status='uncertain',stage='active_write',pending=key,current_step_key=key,
        results={'saved-web-checkpoint':{'paragraphs':['kept']}},calls=[dict(id='web-call',role='active_write',
            status='uncertain',step_key=key,channel='openai-compatible',provider_id='kuafu-chat',
            protocol='chat_completions',upstream_base='https://api.kuafushe.cc/v1',dispatch_started=True)])
    store.put_job(job);store.change(project['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body) VALUES(?,?,?,?,?,?)',
            ('web-call',project['id'],.02,None,'reserved',json.dumps({'status':'reserved'})))
    resumed=queue.retry_validation(job['id'],_kuafu_pair_config())
    assert resumed['status']=='queued' and not resumed.get('pending')
    assert store.job(job['id'])['results']=={'saved-web-checkpoint':{'paragraphs':['kept']}}
    assert store.job(job['id'])['transport_recovery_routes'][key]['provider_id']=='kuafu-responses'
    with store.connect() as cx:
        ledger=cx.execute('SELECT actual,status FROM spending WHERE id=?',('web-call',)).fetchone()
        assert ledger['actual'] is None and ledger['status']=='unknown'


def test_resume_pdf_524_queues_single_versioned_responses_stream_attempt(tmp_path,skill,monkeypatch):
    from pydantic import BaseModel
    class Shape(BaseModel):
        answer:str
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=_legacy_v2_job(store,queue,project['id'],bundle)
    key='active-write-p7-n-002-turn-0'
    gateway=store.blob(b'{"error":{"code":"upstream_service_error","message":"gateway timeout"}}')
    call=dict(id='pdf-responses-524',role='active_write',status='uncertain',step_key=key,
        channel='openai-compatible',provider_id='kuafu-responses',protocol='responses',
        upstream_base='https://api.kuafushe.cc/v1',http_status=524,error_code='upstream_service_error',
        response_blob=gateway,dispatch_started=True)
    job.update(status='uncertain',stage='active_write',pending=key,current_step_key=key,
        results={'saved-plan-checkpoint':{'kept':True}},calls=[call],
        transport_recovery_routes={key:{'status':'failed','provider_id':'kuafu-responses',
            'attempts':1,'error_type':'Uncertain','attempt_call_id':call['id']}})
    store.put_job(job);store.change(project['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body) VALUES(?,?,?,?,?,?)',
            (call['id'],project['id'],.02,None,'unknown',json.dumps({'status':'unknown','call_id':call['id']})))
    config=_kuafu_pair_config()
    resumed=queue.retry_validation(job['id'],config)
    state=resumed['transport_recovery_routes'][key]
    assert resumed['status']=='queued' and not resumed.get('pending')
    assert resumed['results']=={'saved-plan-checkpoint':{'kept':True}}
    assert state['status']=='queued' and state['provider_id']=='kuafu-responses'
    assert state['recovery_protocol_version']=='kuafu-responses-sse-v1' and state['streaming'] is True
    assert resumed['transport_recovery_history'][key][0]['attempts']==1
    assert resumed['kuafu_responses_protocol_retries'][key]['previous_call_id']==call['id']
    assert store.read_blob(gateway).startswith(b'{"error"')

    seen={}
    def recover(provider,pid,role,payload,schema,claimed,cancelled):
        seen.update(provider_id=provider.config['provider_id'],stream=provider.config.get('kuafu_responses_streaming'))
        claimed['calls'].append(dict(id='pdf-responses-sse',role=role,status='completed',step_key=key,
            channel='openai-compatible',provider_id=provider.config['provider_id'],protocol='responses',
            upstream_base=provider.config['base_url'],streaming=True))
        return {'answer':'recovered over SSE'}
    monkeypatch.setattr('sourceloom.active_composition.Provider.call',recover)
    engine=ActiveComposition(Production(store,config))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    assert engine.call(resumed,key,'active_write',{},Shape)=={'answer':'recovered over SSE'}
    assert seen=={'provider_id':'kuafu-responses','stream':True}
    assert state['status']=='completed' and state['attempts']==1
    assert resumed['route_switches'][-1]['reason']=='kuafu_responses_versioned_stream_retry'

    # Once the versioned SSE attempt exists, a later /resume cannot queue it again.
    state.update(status='failed',error_type='Uncertain')
    resumed.update(status='uncertain',pending=key)
    store.put_job(resumed);store.change(project['id'],lambda p:p.update(active_job=None))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='uncertain' WHERE id=?",(job['id'],))
    import pytest
    from sourceloom.store import Conflict
    with pytest.raises(Conflict):
        queue.retry_validation(job['id'],config)
    retry=store.job(job['id'])
    assert retry['kuafu_responses_protocol_retries'][key]['fix_id']=='kuafu-responses-sse-v1'
    assert retry['transport_recovery_routes'][key]['status']=='failed'
    assert len(retry['transport_recovery_history'][key])==1
