import json

import httpx
import pytest
from pydantic import BaseModel

from sourceloom.active_composition import ActiveComposition
from sourceloom.durable import Queue
from sourceloom.production import (Production, queue_kuafu_responses_stream_recovery,
                                  retryable_v2_gateway_timeout)
from sourceloom.providers import Provider, Uncertain
from sourceloom.store import Conflict
from tests.test_production import prepared


class _Answer(BaseModel):
    answer: str


@pytest.fixture
def skill(tmp_path):
    root=tmp_path/'writing-skill'
    (root/'references').mkdir(parents=True)
    (root/'constitution').mkdir()
    (root/'assets').mkdir()
    (root/'SKILL.md').write_text(
        '[format](references/format-rules.md)\n[explain](references/explanation-framework.md)\n'
        '[formula](references/formula-explanation.md)\n`constitution/principles.md`',encoding='utf-8')
    for name in ('format-rules','explanation-framework','formula-explanation'):
        (root/'references'/f'{name}.md').write_text(f'FULL-{name}\nTAIL-{name}',encoding='utf-8')
    (root/'constitution'/'principles.md').write_text('FULL-CONSTITUTION',encoding='utf-8')
    (root/'assets'/'asset.bin').write_bytes(b'complete package asset')
    return root


def _routes():
    chat={
        'provider':'openai-compatible','provider_id':'kuafu-chat',
        'model':'deepseek-v4.1-flash','protocol':'chat_completions',
        'base_url':'https://api.kuafushe.cc/v1','enabled':True,
        'backup_provider_id':'kuafu-responses',
    }
    responses={
        'provider':'openai-compatible','provider_id':'kuafu-responses',
        'model':'deepseek-v4.1-flash','protocol':'responses','endpoint':'/responses',
        'base_url':'https://api.kuafushe.cc/v1','enabled':True,
        'backup_provider_id':'kuafu-chat',
    }
    return {'kuafu-chat':chat,'kuafu-responses':responses}


def _config(routes):
    return {
        'provider_routes':routes,
        'provider_credentials':{'kuafu-chat':'test-chat-key','kuafu-responses':'test-responses-key'},
        'role_providers':{'active_write':routes['kuafu-chat']|{'api_key':'test-chat-key'}},
        'call_timeout':5,'kuafu_stream_timeout':5,'max_output_tokens':128,'max_input_bytes':100000,
        'input_price':0,'output_price':0,'pricing_cny':{'input':0,'output':0},
        'kuafu_streaming':False,
    }


def _legacy_v2_job(queue, project_id, bundle):
    """Create a pre-cutover v2 job for tests of the retained recovery path."""
    job=queue.enqueue(project_id,bundle)
    job['core_chain_version']=0
    queue.store.put_job(job)
    return job


def _uncertain_pair(store,key):
    gateway=store.blob(b'{"error":{"code":"upstream_service_error"}}')
    return [
        dict(id='chat-524-a',role='active_write',status='uncertain',step_key=key,
             channel='openai-compatible',provider_id='kuafu-chat',protocol='chat_completions',
             upstream_base='https://api.kuafushe.cc/v1',http_status=524,
             error_code='upstream_service_error',response_blob=gateway,streaming=True,
             dispatch_started=True,dispatch_state='started'),
        dict(id='responses-524-b',role='active_write',status='uncertain',step_key=key,
             channel='openai-compatible',provider_id='kuafu-responses',protocol='responses',
             upstream_base='https://api.kuafushe.cc/v1',http_status=524,
             error_code='upstream_service_error',response_blob=gateway,streaming=False,
             dispatch_started=True,dispatch_state='started'),
    ]


def _sse_event(kind,response):
    payload=json.dumps({'type':kind,'response':response})
    return f'event: {kind}\ndata: {payload}\n\n'


def test_sse_recovery_is_durably_claimed_before_real_provider_stream_and_completes(
        tmp_path,skill,monkeypatch):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=_legacy_v2_job(queue,project['id'],bundle)
    key='active-plan-p6-turn-0'
    routes=_routes()
    job.update(stage='active_plan',pending=key,current_step_key=key,
        results={'saved-checkpoint':{'kept':True}},calls=_uncertain_pair(store,key))
    assert queue_kuafu_responses_stream_recovery(job,_config(routes))
    state=job['transport_recovery_routes'][key]
    store.put_job(job)

    response={'id':'resp-test','status':'completed','output_text':'{"answer":"SSE recovered"}',
        'usage':{'input_tokens':9,'output_tokens':3,'total_tokens':12}}
    sse=_sse_event('response.completed',response)+'data: [DONE]\n\n'
    captured={}
    real_client=httpx.AsyncClient

    def client_factory(*args,**kwargs):
        def handler(request):
            captured['wire']=json.loads(request.content)
            captured['url']=str(request.url)
            return httpx.Response(200,headers={'content-type':'text/event-stream'},content=sse)
        kwargs['transport']=httpx.MockTransport(handler)
        return real_client(*args,**kwargs)

    monkeypatch.setattr('sourceloom.providers.httpx.AsyncClient',client_factory)
    engine=ActiveComposition(Production(store,_config(routes)))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)

    assert engine.call(job,key,'active_write',{'source':'test'},_Answer)=={'answer':'SSE recovered'}
    assert captured['url']=='https://api.kuafushe.cc/v1/responses'
    assert captured['wire']['stream'] is True
    assert state['status']=='completed'
    assert state['attempts']==1
    call=job['calls'][-1]
    assert call['streaming'] is True
    assert call['dispatch_started'] is True
    assert call['step_dispatch_ordinal']==3
    assert job['step_dispatch_budgets'][key]['dispatch_count']==3
    assert job['results'][key]=={'answer':'SSE recovered'}
    assert job['results']['saved-checkpoint']=={'kept':True}


def test_http_200_responses_failed_terminal_is_rejected_and_not_failed_over(
        tmp_path,skill,monkeypatch):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=_legacy_v2_job(queue,project['id'],bundle)
    key='active-plan-p6-turn-0'
    routes=_routes()
    config=_config(routes)
    job.update(stage='active_plan',pending=key,current_step_key=key,
        results={'saved-checkpoint':{'kept':True}},calls=_uncertain_pair(store,key))
    assert queue_kuafu_responses_stream_recovery(job,config)
    state=job['transport_recovery_routes'][key]
    store.put_job(job)

    # The gateway HTTP status is successful, but the terminal Responses event
    # itself reports an upstream generation failure. It must not be accepted
    # as a candidate or mistaken for a transport-unknown result.
    failed={'id':'resp-failed','status':'failed',
        'error':{'code':'upstream_error','message':'upstream generation failed'},
        'output_text':'{"answer":"must not be accepted"}',
        'usage':{'input_tokens':9,'output_tokens':0,'total_tokens':9}}
    stream=_sse_event('response.failed',failed)+'data: [DONE]\n\n'
    captured={'requests':[]}
    real_client=httpx.AsyncClient

    def client_factory(*args,**kwargs):
        def handler(request):
            captured['requests'].append((str(request.url),json.loads(request.content)))
            return httpx.Response(200,headers={'content-type':'text/event-stream'},content=stream)
        kwargs['transport']=httpx.MockTransport(handler)
        return real_client(*args,**kwargs)

    monkeypatch.setattr('sourceloom.providers.httpx.AsyncClient',client_factory)
    engine=ActiveComposition(Production(store,config))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)

    with pytest.raises(ValueError,match='Responses 通道未完成'):
        engine.call(job,key,'active_write',{'source':'test'},_Answer)

    assert len(captured['requests'])==1
    url,wire=captured['requests'][0]
    assert url=='https://api.kuafushe.cc/v1/responses'
    assert wire['stream'] is True
    assert state['status']=='failed'
    assert state['attempts']==1
    assert key not in job['results']
    assert job['results']['saved-checkpoint']=={'kept':True}
    assert len(job['calls'])==3
    call=job['calls'][-1]
    assert call['provider_id']=='kuafu-responses'
    assert call['protocol']=='responses'
    assert call['streaming'] is True
    assert call['status']=='invalid'
    saved=json.loads(store.read_blob(call['response_blob']))
    assert saved['status']=='failed'
    assert saved['error']['code']=='upstream_error'
    assert saved['usage']['input_tokens']==9

    # The terminal failure has known usage and is settled, while previous
    # uncertain calls remain separate and are never overwritten.
    with store.connect() as cx:
        receipt=cx.execute('SELECT actual,status,body FROM spending WHERE id=?',
                            (call['id'],)).fetchone()
    assert receipt['actual']==0 and receipt['status']=='settled'
    assert json.loads(receipt['body'])['usage']['total_tokens']==9


def test_legacy_five_dispatch_claim_queues_only_one_sse_recovery_and_keeps_unknown_costs(
        tmp_path,skill):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=_legacy_v2_job(queue,project['id'],bundle)
    key='active-plan-p6-turn-0'
    gateway=store.blob(b'{"error":{"code":"upstream_service_error"}}')
    calls=[]
    for index,(provider_id,protocol) in enumerate((
            ('kuafu-chat','chat_completions'),('kuafu-responses','responses'),
            ('kuafu-chat','chat_completions'),('kuafu-responses','responses'))):
        calls.append(dict(id=f'old-{index+1}',role='active_plan',status='uncertain',step_key=key,
            channel='openai-compatible',provider_id=provider_id,protocol=protocol,
            upstream_base='https://api.kuafushe.cc/v1',http_status=524,
            error_code='upstream_service_error',response_blob=gateway,
            streaming=protocol=='chat_completions',dispatch_started=True,dispatch_state='started'))
    calls.append(dict(id='submitted-primary',role='active_plan',status='submitted',step_key=key,
        channel='openai-compatible',provider_id='kuafu-chat',protocol='chat_completions',
        upstream_base='https://api.kuafushe.cc/v1',streaming=True,
        dispatch_started=True,dispatch_state='started'))
    job.update(status='running',stage='active_plan',pending=key,current_step_key=key,
        results={'saved-checkpoint':{'kept':True}},calls=calls)
    store.put_job(job)
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='running',owner='dead-worker',lease_until=0 WHERE id=?",
                   (job['id'],))
        for call in calls:
            unknown=call['status']=='uncertain'
            cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body) VALUES(?,?,?,?,?,?)',
                (call['id'],project['id'],.02,None,'unknown' if unknown else 'reserved',
                 json.dumps({'status':'unknown' if unknown else 'reserved','call_id':call['id']})))

    recovered=queue.claim('replacement-worker',lease=45,recovery_config=_config(_routes()))

    state=recovered['transport_recovery_routes'][key]
    assert len(recovered['calls'])==5
    assert recovered['calls'][-1]['status']=='uncertain'
    assert state['status']=='queued'
    assert state['recovery_protocol_version']=='kuafu-responses-sse-v1'
    assert state['legacy_excess'] is True
    assert state['failed_kuafu_call_ids']==[call['id'] for call in calls]
    assert not recovered.get('pending')
    assert recovered['results']['saved-checkpoint']=={'kept':True}
    assert not any(item.get('type')=='interrupted_kuafu_dispatch' for item in recovered.get('internal_recoveries',[]))
    with store.connect() as cx:
        rows=cx.execute('SELECT id,actual,status,body FROM spending ORDER BY id').fetchall()
    receipts={row['id']:row for row in rows}
    for call in calls[:-1]:
        assert receipts[call['id']]['actual'] is None and receipts[call['id']]['status']=='unknown'
    assert receipts['submitted-primary']['actual'] is None
    assert receipts['submitted-primary']['status']=='unknown'
    assert json.loads(receipts['submitted-primary']['body'])['status']=='unknown'


@pytest.mark.parametrize('is_sse',[False,True])
def test_worker_crash_before_dispatch_claim_releases_unsent_call_and_recovers(
        tmp_path,skill,monkeypatch,is_sse):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=_legacy_v2_job(queue,project['id'],bundle)
    key='active-plan-p6-turn-0'
    config=_config(_routes())
    prior=_uncertain_pair(store,key) if is_sse else []
    if is_sse:
        job.update(pending=key,current_step_key=key,calls=prior)
        assert queue_kuafu_responses_stream_recovery(job,config)
        recovery=job['transport_recovery_routes'][key]
        recovery.update(status='dispatching',attempts=1,started_at=123)
    call=dict(id='saved-before-claim',role='active_write',status='submitted',step_key=key,
        channel='openai-compatible',provider_id='kuafu-responses' if is_sse else 'kuafu-chat',
        protocol='responses' if is_sse else 'chat_completions',
        upstream_base='https://api.kuafushe.cc/v1',streaming=is_sse,
        dispatch_started=False)
    job.update(status='running',stage='active_plan',pending=key,current_step_key=key,
        calls=prior+[call],results={'saved-checkpoint':{'kept':True}})
    store.put_job(job)
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='running',owner='dead-worker',lease_until=0 WHERE id=?",
                   (job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body) VALUES(?,?,?,?,?,?)',
            (call['id'],project['id'],.02,None,'reserved',json.dumps({'status':'reserved'})))

    recovered=queue.claim('replacement-worker',lease=45,recovery_config=config)
    assert recovered['calls'][-1]['status']=='unavailable'
    assert recovered['calls'][-1]['dispatch_state']=='confirmed_not_sent'
    assert not recovered.get('pending')
    assert recovered['results']['saved-checkpoint']=={'kept':True}
    if is_sse:
        state=recovered['transport_recovery_routes'][key]
        assert state['status']=='queued' and state['attempts']==0
    with store.connect() as cx:
        receipt=cx.execute('SELECT actual,status,body FROM spending WHERE id=?',(call['id'],)).fetchone()
    assert receipt['actual']==0 and receipt['status']=='settled'
    assert json.loads(receipt['body'])['status']=='rejected'

    if is_sse:
        response={'id':'resp-after-preclaim-crash','status':'completed',
            'output_text':'{"answer":"resumed"}',
            'usage':{'input_tokens':9,'output_tokens':3,'total_tokens':12}}
        stream=_sse_event('response.completed',response)+'data: [DONE]\n\n'
        real_client=httpx.AsyncClient
        def client_factory(*args,**kwargs):
            kwargs['transport']=httpx.MockTransport(lambda request:
                httpx.Response(200,headers={'content-type':'text/event-stream'},content=stream))
            return real_client(*args,**kwargs)
        monkeypatch.setattr('sourceloom.providers.httpx.AsyncClient',client_factory)
    else:
        def answer(url,headers,body,deadline):
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop',
                'message':{'content':'{"answer":"resumed"}'}}],
                'usage':{'prompt_tokens':9,'completion_tokens':3,'total_tokens':12}})
        monkeypatch.setattr('sourceloom.providers.post_before_deadline',answer)

    engine=ActiveComposition(Production(store,config))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)
    assert engine.call(recovered,key,'active_write',{'source':'test'},_Answer)=={'answer':'resumed'}
    assert recovered['calls'][-1]['id']!=call['id']
    assert len([item for item in recovered['calls'] if item.get('dispatch_started')])==(3 if is_sse else 1)


@pytest.mark.parametrize('has_prior_call',[False,True])
def test_lease_recovery_never_uses_prior_step_response_for_unrecorded_pending_step(
        tmp_path,skill,has_prior_call):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=queue.enqueue(project['id'],bundle)
    pending='active-plan-next'
    old_response=store.blob(json.dumps({'choices':[{'finish_reason':'stop',
        'message':{'content':'{"answer":"prior step"}'}}]}).encode())
    prior=[dict(id='prior-complete',role='active_write',status='completed',
        step_key='active-plan-prior',channel='openai-compatible',
        provider_id='kuafu-chat',protocol='chat_completions',
        upstream_base='https://api.kuafushe.cc/v1',response_blob=old_response,
        dispatch_started=True)] if has_prior_call else []
    job.update(status='running',stage='active_plan',pending=pending,
        current_step_key=pending,calls=prior)
    store.put_job(job)
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='running',owner='dead-worker',lease_until=0 WHERE id=?",
                   (job['id'],))

    recovered=queue.claim('replacement-worker',lease=45,recovery_config=_config(_routes()))
    assert not recovered.get('pending')
    assert recovered['calls']==prior
    assert recovered['internal_recoveries'][-1]['type']=='interrupted_before_call_record'
    assert recovered['internal_recoveries'][-1]['step']==pending


@pytest.mark.parametrize('pair_complete',[False,True])
def test_lease_recovery_queues_backup_or_one_sse_after_saved_524(
        tmp_path,skill,pair_complete):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=_legacy_v2_job(queue,project['id'],bundle)
    key='active-plan-p6-turn-0'
    calls=_uncertain_pair(store,key)
    if not pair_complete:
        calls=calls[:1]
    job.update(status='running',stage='active_plan',pending=key,
        current_step_key=key,calls=calls,results={'saved-checkpoint':{'kept':True}})
    store.put_job(job)
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='running',owner='dead-worker',lease_until=0 WHERE id=?",
                   (job['id'],))

    recovered=queue.claim('replacement-worker',lease=45,recovery_config=_config(_routes()))
    assert not recovered.get('pending')
    assert recovered['results']['saved-checkpoint']=={'kept':True}
    state=recovered['transport_recovery_routes'][key]
    assert state['status']=='queued'
    assert state['provider_id']=='kuafu-responses'
    assert state.get('recovery_protocol_version')==('kuafu-responses-sse-v1' if pair_complete else None)
    assert len(recovered['calls'])==len(calls)


def test_rejected_reservation_preserves_budget_reason_without_persisting_call(
        tmp_path,skill,monkeypatch):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=queue.enqueue(project['id'],bundle)
    key='active-plan-p6-turn-0'
    job.update(stage='active_plan',pending=key,current_step_key=key)
    store.put_job(job)
    def reject(*args,**kwargs):
        raise Conflict('test budget exhausted before dispatch')
    monkeypatch.setattr(store,'reserve',reject)
    with pytest.raises(Conflict,match='test budget exhausted before dispatch'):
        Provider(store,_config(_routes())).call(project['id'],'active_write',
            {'source':'test'},{'type':'object'},job,lambda:False)
    assert not job.get('pending')
    assert not job['calls']
    with store.connect() as cx:
        assert cx.execute('SELECT count(*) FROM spending WHERE project=?',
            (project['id'],)).fetchone()[0]==0


def test_reservation_and_call_record_commit_together_before_dispatch(
        tmp_path,skill,monkeypatch):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=queue.enqueue(project['id'],bundle)
    key='active-plan-p6-turn-0'
    job.update(stage='active_plan',pending=key,current_step_key=key)
    store.put_job(job)
    def interrupted(*args,**kwargs):
        raise SystemExit('simulated crash before dispatch claim')
    monkeypatch.setattr(store,'begin_provider_dispatch',interrupted)
    with pytest.raises(SystemExit,match='simulated crash'):
        Provider(store,_config(_routes())).call(project['id'],'active_write',
            {'source':'test'},{'type':'object'},job,lambda:False)
    saved=store.job(job['id'])
    call=saved['calls'][-1]
    assert call['step_key']==key and call['dispatch_started'] is False
    with store.connect() as cx:
        assert cx.execute('SELECT count(*) FROM spending WHERE id=?',
            (call['id'],)).fetchone()[0]==1
        cx.execute("UPDATE production_control SET status='running',owner='dead-worker',lease_until=0 WHERE id=?",
                   (job['id'],))
    recovered=queue.claim('replacement-worker',lease=45,recovery_config=_config(_routes()))
    assert recovered['calls'][-1]['dispatch_state']=='confirmed_not_sent'
    assert not recovered.get('pending')
    with store.connect() as cx:
        assert cx.execute('SELECT actual FROM spending WHERE id=?',
            (call['id'],)).fetchone()[0]==0


def test_reservation_refuses_worker_that_lost_its_lease(tmp_path,skill):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=queue.enqueue(project['id'],bundle)
    job.update(worker_owner='old-worker',status='running',pending='new-step')
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='running',owner='new-worker',lease_until=? WHERE id=?",
                   (9999999999,job['id']))
    with pytest.raises(Conflict,match='后台执行权已变化'):
        store.reserve(project['id'],'stale-call',.02,{'channel':'openai-compatible'},
                      job_record=job)
    with store.connect() as cx:
        assert cx.execute('SELECT count(*) FROM spending WHERE id=?',('stale-call',)).fetchone()[0]==0
    assert store.job(job['id']).get('pending')!='new-step'


def test_primary_and_backup_524_queue_sse_without_a_third_normal_dispatch(tmp_path,skill,monkeypatch):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=_legacy_v2_job(queue,project['id'],bundle)
    key='active-plan-p6-turn-0'
    routes=_routes()
    # Preserve the recovery case for jobs created before KuaFu Responses SSE
    # became the default: both original attempts were non-streaming.
    routes['kuafu-responses']['kuafu_responses_streaming']=False
    config=_config(routes)
    job.update(stage='active_plan',pending=key,current_step_key=key)
    store.put_job(job)
    sent=[]

    def timeout(url,headers,body,deadline):
        sent.append((url,body))
        return httpx.Response(524,json={'error':{'code':'upstream_service_error','message':'gateway timeout'}})

    monkeypatch.setattr('sourceloom.providers.post_before_deadline',timeout)
    monkeypatch.setattr('sourceloom.providers.post_responses_stream_before_deadline',timeout)
    with pytest.raises(Uncertain) as failure:
        Provider(store,config).call(project['id'],'active_write',{'source':'test'},
            {'type':'object'},job,lambda:False)

    assert len(job['calls'])==2
    assert [call['provider_id'] for call in job['calls']]==['kuafu-chat','kuafu-responses']
    assert len(sent)==2
    assert all(call['status']=='uncertain' and call['http_status']==524 for call in job['calls'])
    assert retryable_v2_gateway_timeout(job,failure.value,config=config) is True
    assert len(job['calls'])==2
    assert job['transport_recovery_routes'][key]['status']=='queued'
    assert job['transport_recovery_routes'][key]['recovery_protocol_version']=='kuafu-responses-sse-v1'


@pytest.mark.parametrize('responses_call_index',[0,1])
def test_dispatched_responses_sse_524_is_not_automatically_replayed(
        tmp_path,skill,responses_call_index):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=queue.enqueue(project['id'],bundle)
    key='active-plan-p6-turn-0'
    calls=_uncertain_pair(store,key)
    # Exercise either route ordering: Responses may be the primary or backup.
    if responses_call_index==0:
        calls[0],calls[1]=calls[1],calls[0]
    calls[responses_call_index]['streaming']=True
    job.update(stage='active_plan',pending=key,current_step_key=key,calls=calls)
    store.put_job(job)

    assert retryable_v2_gateway_timeout(
        job,Uncertain('Responses SSE ended after dispatch without a terminal event'),config=_config(_routes())) is False
    assert key not in job.get('transport_recovery_routes',{})
    assert len(job['calls'])==2


def test_failed_sse_keeps_unknown_cost_and_cannot_be_requeued(tmp_path,skill,monkeypatch):
    store,_,project,bundle=prepared(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job=_legacy_v2_job(queue,project['id'],bundle)
    key='active-plan-p6-turn-0'
    routes=_routes()
    config=_config(routes)
    job.update(stage='active_plan',pending=key,current_step_key=key,
        calls=_uncertain_pair(store,key))
    assert queue_kuafu_responses_stream_recovery(job,config)
    state=job['transport_recovery_routes'][key]
    store.put_job(job)

    real_client=httpx.AsyncClient
    def client_factory(*args,**kwargs):
        def handler(request):
            return httpx.Response(524,json={'error':{'code':'upstream_service_error','message':'gateway timeout'}})
        kwargs['transport']=httpx.MockTransport(handler)
        return real_client(*args,**kwargs)
    monkeypatch.setattr('sourceloom.providers.httpx.AsyncClient',client_factory)
    engine=ActiveComposition(Production(store,config))
    monkeypatch.setattr(engine.queue,'cancelled',lambda *args:False)

    with pytest.raises(Uncertain) as failure:
        engine.call(job,key,'active_write',{'source':'test'},_Answer)

    recovery_call=job['calls'][-1]
    assert state['status']=='failed'
    assert recovery_call['streaming'] is True and recovery_call['step_dispatch_ordinal']==3
    assert len(job['calls'])==3
    assert retryable_v2_gateway_timeout(job,failure.value,config=config) is False
    assert len(job['calls'])==3
    assert state['status']=='failed'
    with store.connect() as cx:
        ledger=cx.execute('SELECT actual,status,body FROM spending WHERE id=?',
                          (recovery_call['id'],)).fetchone()
    assert ledger['actual'] is None and ledger['status']=='unknown'
    assert json.loads(ledger['body'])['status']=='upstream_service_error'
