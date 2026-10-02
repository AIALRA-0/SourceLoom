import json
import time

import httpx
import pytest

from sourceloom.durable import Queue
from sourceloom.providers import Provider, logical_request_id
from sourceloom.store import Conflict
from tests.test_production import prepared


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


_SCHEMA={
    'type':'object',
    'properties':{'answer':{'type':'string'}},
    'required':['answer'],
    'additionalProperties':False,
}


def _route(provider_id,base_url,protocol='chat_completions',backup_provider_id=None):
    route={
        'provider':'openai-compatible','provider_id':provider_id,
        'model':'round2-fake-model','protocol':protocol,
        'base_url':base_url,'enabled':True,
    }
    if protocol=='responses':
        route['endpoint']='/responses'
    if backup_provider_id:
        route['backup_provider_id']=backup_provider_id
    return route


def _config(primary,backup=None):
    routes={primary['provider_id']:primary}
    credentials={primary['provider_id']:'test-primary-key'}
    if backup:
        routes[backup['provider_id']]=backup
        credentials[backup['provider_id']]='test-backup-key'
    primary_role=primary|{'api_key':credentials[primary['provider_id']]}
    return {
        'provider':'openai-compatible','provider_id':primary['provider_id'],
        'model':primary['model'],'protocol':primary['protocol'],
        'base_url':primary['base_url'],'api_key':credentials[primary['provider_id']],
        'endpoint':primary.get('endpoint','/responses'),
        'provider_routes':routes,'provider_credentials':credentials,
        'role_providers':{'active_plan':primary_role},
        'call_timeout':5,'kuafu_stream_timeout':5,
        'max_output_tokens':128,'max_input_bytes':100000,
        'input_price':0,'output_price':0,
        'pricing_cny':{'input':0,'cached_input':0,'output':0},
        'daily_budget_usd':None,'daily_call_limit':80,
        'kuafu_streaming':False,'kuafu_responses_streaming':False,
    }


def _job(tmp_path,skill):
    store,_,project,bundle=prepared(tmp_path,skill)
    job=Queue(store,pipeline='active_composition_v2').enqueue(project['id'],bundle)
    assert job['core_chain_version']==1
    return store,project,job


def _install_httpx_handler(monkeypatch,handler):
    original=httpx.AsyncClient

    def client_factory(*args,**kwargs):
        kwargs['transport']=httpx.MockTransport(handler)
        return original(*args,**kwargs)

    monkeypatch.setattr('sourceloom.providers.httpx.AsyncClient',client_factory)


def _install_http_client_handler(monkeypatch,handler):
    original=httpx.Client

    def client_factory(*args,**kwargs):
        kwargs['transport']=httpx.MockTransport(handler)
        return original(*args,**kwargs)

    monkeypatch.setattr('sourceloom.providers.httpx.Client',client_factory)


def _chat_response(answer='ok'):
    return httpx.Response(200,json={
        'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'answer':answer})}}],
        'usage':{'prompt_tokens':8,'completion_tokens':2,'total_tokens':10},
    })


def _responses_sse(response):
    event={'type':'response.completed','response':response}
    return ('event: response.completed\n'
            +'data: '+json.dumps(event)+'\n\n'
            +'data: [DONE]\n\n')


def test_confirmed_connect_failure_uses_backup_with_one_stable_logical_id(
        tmp_path,skill,monkeypatch):
    store,project,job=_job(tmp_path,skill)
    key='active-plan-round2-connect-failure'
    primary=_route('primary','https://primary.example/v1',backup_provider_id='backup')
    backup=_route('backup','https://backup.example/v1')
    config=_config(primary,backup)
    requests=[]

    def handler(request):
        requests.append(str(request.url))
        if request.url.host=='primary.example':
            raise httpx.ConnectError('synthetic connection failure',request=request)
        return _chat_response('backup completed')

    _install_httpx_handler(monkeypatch,handler)
    result=Provider(store,config).generate(
        project['id'],'active_plan',{'source':'fake'},_SCHEMA,job,key)

    request_id=logical_request_id(job,key)
    assert result.status=='SUCCESS'
    assert result.value=={'answer':'backup completed'}
    assert requests==[
        'https://primary.example/v1/chat/completions',
        'https://backup.example/v1/chat/completions',
    ]
    assert [call['logical_request_id'] for call in job['calls']]==[request_id,request_id]
    assert job['calls'][0]['dispatch_state']=='confirmed_not_sent'
    assert job['calls'][1]['status']=='completed'
    assert job['logical_requests'][request_id]['attempt_count']==2
    assert job['logical_requests'][request_id]['recovery_count']==1


def test_core_v1_unknown_is_not_rerouted_or_replayed(tmp_path,skill,monkeypatch):
    store,project,job=_job(tmp_path,skill)
    key='active-plan-round2-unknown'
    primary=_route('kuafu-chat','https://api.kuafushe.cc/v1',
                   protocol='chat_completions',backup_provider_id='kuafu-responses')
    backup=_route('kuafu-responses','https://api.kuafushe.cc/v1',protocol='responses')
    config=_config(primary,backup)
    requests=[]

    def handler(request):
        requests.append(str(request.url))
        raise httpx.ReadTimeout('synthetic timeout after dispatch',request=request)

    _install_httpx_handler(monkeypatch,handler)
    provider=Provider(store,config)
    first=provider.generate(project['id'],'active_plan',{'source':'fake'},_SCHEMA,job,key)
    second=provider.generate(project['id'],'active_plan',{'source':'fake'},_SCHEMA,job,key)

    request_id=logical_request_id(job,key)
    assert first.status=='UNKNOWN' and second.status=='UNKNOWN'
    assert len(requests)==1
    assert len(job['calls'])==1
    assert job['calls'][0]['status']=='uncertain'
    assert job['calls'][0]['logical_request_id']==request_id
    assert job.get('route_switches',[])==[]
    assert job['logical_requests'][request_id]['attempt_count']==1
    assert job['logical_requests'][request_id]['recovery_count']==1


def test_core_v1_incomplete_responses_stream_keeps_original_unknown(
        tmp_path,skill,monkeypatch):
    store,project,job=_job(tmp_path,skill)
    key='active-plan-round2-incomplete-stream'
    primary=_route('kuafu-responses','https://api.kuafushe.cc/v1',
                   protocol='responses',backup_provider_id='kuafu-chat')
    backup=_route('kuafu-chat','https://api.kuafushe.cc/v1')
    config=_config(primary,backup)
    config['kuafu_responses_streaming']=True
    requests=[]

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200,headers={'content-type':'text/event-stream'},
            content='event: response.output_text.delta\n'
                    'data: {"type":"response.output_text.delta","delta":"partial"}\n\n')

    _install_httpx_handler(monkeypatch,handler)
    result=Provider(store,config).generate(
        project['id'],'active_plan',{'source':'fake'},_SCHEMA,job,key)

    assert result.status=='UNKNOWN'
    assert requests==['https://api.kuafushe.cc/v1/responses']
    assert len(job['calls'])==1
    assert job['calls'][0]['logical_request_id']==logical_request_id(job,key)
    assert job.get('route_switches',[])==[]


def test_core_v1_cancel_before_dispatch_is_known_and_costs_no_attempt(
        tmp_path,skill,monkeypatch):
    store,project,job=_job(tmp_path,skill)
    key='active-plan-round2-cancelled'
    route=_route('primary','https://primary.example/v1')
    config=_config(route)
    requests=[]
    _install_httpx_handler(monkeypatch,lambda request: requests.append(request))

    result=Provider(store,config).generate(
        project['id'],'active_plan',{'source':'fake'},_SCHEMA,job,key,
        cancelled=lambda:True)

    assert result.status=='KNOWN_FAILURE'
    assert requests==[]
    assert job['logical_requests'][logical_request_id(job,key)]['attempt_count']==0


def test_success_records_sse_first_byte_and_logical_timing(tmp_path,skill,monkeypatch):
    store,project,job=_job(tmp_path,skill)
    key='active-plan-round2-sse-timing'
    route=_route('kuafu-responses','https://api.kuafushe.cc/v1',protocol='responses')
    config=_config(route)
    config['kuafu_responses_streaming']=True
    response={
        'id':'resp-round2','status':'completed','output_text':'{"answer":"timed"}',
        'usage':{'input_tokens':8,'output_tokens':2,'total_tokens':10},
    }
    requests=[]

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200,headers={'content-type':'text/event-stream'},
                              content=_responses_sse(response))

    _install_httpx_handler(monkeypatch,handler)
    result=Provider(store,config).generate(
        project['id'],'active_plan',{'source':'fake'},_SCHEMA,job,key)

    request_id=logical_request_id(job,key)
    logical=job['logical_requests'][request_id]
    attempt=job['calls'][0]['network_attempts'][0]
    assert result.status=='SUCCESS' and result.value=={'answer':'timed'}
    assert requests==['https://api.kuafushe.cc/v1/responses']
    assert logical['attempt_count']==1 and logical['recovery_count']==0
    assert logical['logical_total_ms']>=0
    assert logical['logical_timing_basis']=='monotonic_same_process'
    for field in ('prepare_ms','queue_or_recovery_wait_ms','dispatch_to_first_byte_ms',
                  'first_byte_to_complete_ms','parse_ms','total_attempt_ms'):
        assert isinstance(attempt[field],(int,float)) and attempt[field]>=0
    assert attempt['dispatch_to_first_byte_ms'] is not None
    assert attempt['first_byte_to_complete_ms'] is not None


def test_known_http_rejection_is_known_failure_with_unavailable_first_byte(
        tmp_path,skill,monkeypatch):
    store,project,job=_job(tmp_path,skill)
    key='active-plan-round2-known-failure'
    route=_route('primary','https://primary.example/v1')
    config=_config(route)
    requests=[]

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(400,json={'error':{'code':'invalid_request'}})

    _install_httpx_handler(monkeypatch,handler)
    result=Provider(store,config).generate(
        project['id'],'active_plan',{'source':'fake'},_SCHEMA,job,key)

    request_id=logical_request_id(job,key)
    logical=job['logical_requests'][request_id]
    attempt=job['calls'][0]['network_attempts'][0]
    assert result.status=='KNOWN_FAILURE'
    assert requests==['https://primary.example/v1/chat/completions']
    assert job['calls'][0]['status']=='rejected'
    assert logical['attempt_count']==1 and logical['recovery_count']==0
    assert isinstance(logical['logical_total_ms'],(int,float))
    assert attempt['http_status']==400
    assert attempt['dispatch_to_first_byte_ms'] is None
    assert attempt['first_byte_to_complete_ms'] is None
    assert attempt['prepare_ms']>=0
    assert attempt['queue_or_recovery_wait_ms']>=0
    assert attempt['parse_ms']>=0
    assert attempt['total_attempt_ms']>=0


def test_core_v1_worker_reclaim_marks_unknown_without_queuing_a_backup(
        tmp_path,skill):
    store,project,job=_job(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    key='active-plan-round2-worker-reclaim'
    request_id=logical_request_id(job,key)
    call={
        'id':'interrupted-call','logical_request_id':request_id,
        'role':'active_plan','status':'submitted','step_key':key,
        'channel':'openai-compatible','provider_id':'kuafu-chat',
        'protocol':'chat_completions','upstream_base':'https://api.kuafushe.cc/v1',
        'dispatch_started':True,'dispatch_state':'started',
    }
    job.update(status='running',stage='active_plan',pending=key,current_step_key=key,
        calls=[call],logical_requests={request_id:{'step_key':key,'role':'active_plan',
            'started_at':time.time(),'status':'running'}})
    store.put_job(job)
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='running',owner='dead-worker',lease_until=0 WHERE id=?",
                   (job['id'],))
        cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body) VALUES(?,?,?,?,?,?)',
            (call['id'],project['id'],.02,None,'reserved',json.dumps({'status':'reserved'})))

    routes={
        'kuafu-chat':_route('kuafu-chat','https://api.kuafushe.cc/v1',
            protocol='chat_completions',backup_provider_id='kuafu-responses'),
        'kuafu-responses':_route('kuafu-responses','https://api.kuafushe.cc/v1',protocol='responses'),
    }
    recovered=queue.claim('replacement-worker',recovery_config=_config(routes['kuafu-chat'],routes['kuafu-responses']))

    assert recovered['calls'][0]['status']=='uncertain'
    assert recovered['calls'][0]['logical_request_id']==request_id
    assert len(recovered['calls'])==1
    assert recovered.get('transport_recovery_routes',{})=={}
    assert recovered.get('route_switches',[])==[]
    assert recovered['pending']==key


def test_core_v1_retry_validation_queries_original_without_reissuing(tmp_path,skill):
    store,project,job=_job(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    key='active-plan-round2-retry-validation'
    request_id=logical_request_id(job,key)
    call={
        'id':'unknown-call','logical_request_id':request_id,
        'role':'active_plan','status':'uncertain','step_key':key,
        'channel':'openai-compatible','provider_id':'kuafu-chat',
        'protocol':'chat_completions','upstream_base':'https://api.kuafushe.cc/v1',
        'dispatch_started':True,'dispatch_state':'started',
    }
    job.update(status='uncertain',stage='active_plan',pending=key,current_step_key=key,
        calls=[call],logical_requests={request_id:{'step_key':key,'role':'active_plan',
            'started_at':time.time(),'status':'UNKNOWN'}})
    store.put_job(job)
    store.change(project['id'],lambda saved:saved.update(active_job=None,state='uncertain'))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='uncertain',owner=NULL,lease_until=0 WHERE id=?",
                   (job['id'],))

    config=_config(
        _route('kuafu-chat','https://api.kuafushe.cc/v1',
            protocol='chat_completions',backup_provider_id='kuafu-responses'),
        _route('kuafu-responses','https://api.kuafushe.cc/v1',protocol='responses'))
    resumed=queue.retry_validation(job['id'],config)

    assert resumed['status']=='uncertain'
    saved=store.job(job['id'])
    assert len(saved['calls'])==1
    assert saved['calls'][0]['id']=='unknown-call'
    assert saved['logical_requests'][request_id]['status']=='UNKNOWN'
    assert saved.get('transport_recovery_routes',{})=={}
    assert saved.get('route_switches',[])==[]


def test_core_v1_manual_resume_uses_provider_for_saved_responses(
        tmp_path,skill,monkeypatch):
    store,project,job=_job(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    key='active-plan-saved-responses'
    request_id=logical_request_id(job,key)
    body={'status':'completed','output_text':json.dumps({'answer':'saved'})}
    call={'id':'saved-response','logical_request_id':request_id,
          'role':'active_plan','step_key':key,'status':'completed',
          'channel':'openai-compatible','protocol':'responses',
          'provider_id':'kuafu-responses','dispatch_started':True,
          'response_blob':store.blob(json.dumps(body).encode())}
    job.update(status='uncertain',stage='active_plan',pending=key,calls=[call])
    store.put_job(job)
    store.change(project['id'],lambda saved:saved.update(active_job=None,state='uncertain'))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='uncertain',owner=NULL,lease_until=0 WHERE id=?",
                   (job['id'],))
    monkeypatch.setattr(queue,'_queue_saved_responses_artifact',
                        lambda *_args:pytest.fail('新版任务进入了旧 Responses 恢复逻辑'))
    config=_config(_route('kuafu-responses','https://api.kuafushe.cc/v1',protocol='responses'))

    resumed=queue.retry_validation(job['id'],config)

    assert resumed['status']=='queued'
    saved=store.job(job['id'])
    assert saved['results'][key]=={'answer':'saved'}
    assert saved['logical_requests'][request_id]['status']=='SUCCESS'
    assert len(saved['calls'])==1


def test_core_v1_failed_manual_resume_cannot_enter_legacy_validation(
        tmp_path,skill,monkeypatch):
    store,project,job=_job(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    job.update(status='failed',stage='active_plan',pending='active-plan-failed')
    store.put_job(job)
    monkeypatch.setattr(queue,'_queue_saved_responses_artifact',
                        lambda *_args:pytest.fail('新版任务进入了旧 Responses 恢复逻辑'))

    with pytest.raises(Conflict,match='缺少与当前步骤对应的原调用身份'):
        queue.retry_validation(job['id'],_config(_route('primary','https://primary.example/v1')))

    assert store.job(job['id'])['status']=='failed'


def test_core_v1_saved_524_stays_unknown_across_restore_then_recovers_same_call(
        tmp_path,skill,monkeypatch):
    store,project,job=_job(tmp_path,skill)
    key='active-plan-524-recovery'
    route=_route('kuafu-responses','https://api.kuafushe.cc/v1',protocol='responses')
    config=_config(route)
    requests=[]

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(524,json={'error':{'type':'gateway_timeout'}})

    _install_httpx_handler(monkeypatch,handler)
    first=Provider(store,config).generate(
        project['id'],'active_plan',{'source':'fake'},_SCHEMA,job,key)
    assert first.status=='UNKNOWN'
    request_id=logical_request_id(job,key)
    call_id=job['calls'][0]['id']
    assert job['calls'][0]['status']=='uncertain'

    monkeypatch.setattr('sourceloom.providers._TIMING_PROCESS_ID','simulated-restarted-worker')
    restored=store.job(job['id'])
    second=Provider(store,config).generate(
        project['id'],'active_plan',{'source':'fake'},_SCHEMA,restored,key)
    assert second.status=='UNKNOWN'
    assert restored['logical_requests'][request_id]['logical_timing_basis']=='wall_across_process_or_legacy'
    assert len(requests)==1 and len(restored['calls'])==1
    assert restored['calls'][0]['id']==call_id

    completed={'status':'completed','output_text':json.dumps({'answer':'original request recovered'})}
    restored['calls'][0]['response_blob']=store.blob(json.dumps(completed).encode())
    restored['calls'][0]['http_status']=200
    store.put_job(restored)
    final=Provider(store,config).generate(
        project['id'],'active_plan',{'source':'fake'},_SCHEMA,store.job(job['id']),key)
    assert final.status=='SUCCESS'
    assert final.value=={'answer':'original request recovered'}
    assert len(requests)==1
    saved=store.job(job['id'])
    assert len(saved['calls'])==1 and saved['calls'][0]['id']==call_id
    assert saved['logical_requests'][request_id]['status']=='SUCCESS'


def test_core_v1_recovery_conflict_without_terminal_evidence_stays_unknown(
        tmp_path,skill,monkeypatch):
    store,project,job=_job(tmp_path,skill)
    key='active-plan-uncertain-conflict'
    request_id=logical_request_id(job,key)
    job.update(pending=key,calls=[{
        'id':'unresolved-original','logical_request_id':request_id,
        'role':'active_plan','step_key':key,'status':'uncertain',
        'dispatch_started':True,'dispatch_state':'started',
        'channel':'openai-compatible','protocol':'responses',
    }])
    store.put_job(job)
    monkeypatch.setattr(Provider,'recover',lambda *_args:(_ for _ in ()).throw(
        Conflict('恢复数据不一致，无法确认上游终态')))

    result=Provider(store,_config(_route('primary','https://primary.example/v1'))).generate(
        project['id'],'active_plan',None,None,job,key)

    assert result.status=='UNKNOWN'
    assert job['logical_requests'][request_id]['status']=='UNKNOWN'
    assert len(job['calls'])==1 and job['calls'][0]['id']=='unresolved-original'


def test_core_v1_unreadable_saved_response_is_unknown_not_proven_failure(
        tmp_path,skill):
    store,project,job=_job(tmp_path,skill)
    key='active-plan-unreadable-original'
    request_id=logical_request_id(job,key)
    job.update(pending=key,calls=[{
        'id':'saved-but-unreadable','logical_request_id':request_id,
        'role':'active_plan','step_key':key,'status':'completed',
        'dispatch_started':True,'dispatch_state':'started',
        'channel':'openai-compatible','protocol':'responses',
        'response_blob':store.blob(b'<html>gateway error</html>'),
    }])
    store.put_job(job)

    result=Provider(store,_config(_route('primary','https://primary.example/v1'))).generate(
        project['id'],'active_plan',None,None,job,key)

    assert result.status=='UNKNOWN'
    assert len(job['calls'])==1
    assert job['calls'][0]['id']=='saved-but-unreadable'


def test_core_v1_manual_resume_corrects_prior_false_failure_to_unknown(
        tmp_path,skill):
    store,project,job=_job(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    key='active-plan-prior-false-failure'
    request_id=logical_request_id(job,key)
    job.update(status='failed',stage='active_plan',pending=key,calls=[{
        'id':'original-524','logical_request_id':request_id,
        'role':'active_plan','step_key':key,'status':'uncertain',
        'dispatch_started':True,'dispatch_state':'started',
        'channel':'openai-compatible','protocol':'responses','http_status':524,
        'response_blob':store.blob(json.dumps({'error':'gateway timeout'}).encode()),
    }])
    store.put_job(job)
    store.change(project['id'],lambda saved:saved.update(active_job=None,state='failed'))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='failed',owner=NULL,lease_until=0 WHERE id=?",
                   (job['id'],))

    resumed=queue.retry_validation(job['id'],
        _config(_route('primary','https://primary.example/v1',protocol='responses')))

    assert resumed['status']=='uncertain'
    assert resumed['logical_requests'][request_id]['status']=='UNKNOWN'
    assert len(resumed['calls'])==1 and resumed['calls'][0]['id']=='original-524'
    with store.connect() as cx:
        control=cx.execute('SELECT status FROM production_control WHERE id=?',(job['id'],)).fetchone()
    assert control['status']=='uncertain'
    assert store.get(project['id'])['state']=='uncertain'


def test_core_v1_explicit_terminal_responses_failure_is_known(tmp_path,skill):
    store,project,job=_job(tmp_path,skill)
    key='active-plan-confirmed-terminal-failure'
    request_id=logical_request_id(job,key)
    body={'status':'failed','error':{'type':'invalid_request'}}
    job.update(pending=key,calls=[{
        'id':'confirmed-failed-original','logical_request_id':request_id,
        'role':'active_plan','step_key':key,'status':'uncertain',
        'dispatch_started':True,'dispatch_state':'started',
        'channel':'openai-compatible','protocol':'responses','http_status':200,
        'response_blob':store.blob(json.dumps(body).encode()),
    }])
    store.put_job(job)

    result=Provider(store,_config(_route('primary','https://primary.example/v1'))).generate(
        project['id'],'active_plan',None,None,job,key)

    assert result.status=='KNOWN_FAILURE'
    assert len(job['calls'])==1 and job['calls'][0]['status']=='invalid'


def test_round1_queued_unknown_recovery_is_not_posted_after_core_v1_upgrade(
        tmp_path,skill,monkeypatch):
    store,project,job=_job(tmp_path,skill)
    key='active-plan-round1-queued-recovery'
    old_call={
        'id':'round1-original-call','role':'active_plan','status':'uncertain',
        'step_key':key,'channel':'openai-compatible','provider_id':'kuafu-chat',
        'protocol':'chat_completions','upstream_base':'https://api.kuafushe.cc/v1',
        'dispatch_started':True,'dispatch_state':'started',
    }
    job.update(status='queued',stage='active_plan',current_step_key=key,
        calls=[old_call],logical_requests={},transport_recovery_routes={key:{
            'status':'queued','provider_id':'kuafu-responses','protocol':'responses',
            'original_call_id':old_call['id'],'original_provider_id':'kuafu-chat',
            'attempts':0,'reason':'round1 unknown recovery was queued before upgrade'}})
    job.pop('pending',None)
    store.put_job(job)

    primary=_route('kuafu-chat','https://api.kuafushe.cc/v1',
                   protocol='chat_completions',backup_provider_id='kuafu-responses')
    backup=_route('kuafu-responses','https://api.kuafushe.cc/v1',protocol='responses')
    config=_config(primary,backup)
    requests=[]

    def handler(request):
        requests.append((request.method,str(request.url)))
        return _chat_response('must not duplicate the old unknown request')

    _install_httpx_handler(monkeypatch,handler)
    result=Provider(store,config).generate(
        project['id'],'active_plan',{'source':'same saved step'},_SCHEMA,job,key)

    assert requests==[]
    assert result.status=='UNKNOWN'
    assert len(job['calls'])==1
    assert job['transport_recovery_routes'][key]['status']=='queued'


def test_core_v1_pending_round1_call_gets_stable_logical_request_id_on_recovery(
        tmp_path,skill):
    store,project,job=_job(tmp_path,skill)
    key='active-plan-round1-pending-call'
    route=_route('primary','https://primary.example/v1')
    original_response={
        'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'answer':'saved result'})}}],
        'usage':{'prompt_tokens':8,'completion_tokens':2,'total_tokens':10},
    }
    old_call={
        'id':'round1-pending-call','role':'active_plan','status':'uncertain',
        'step_key':key,'channel':'openai-compatible','provider_id':'primary',
        'protocol':'chat_completions','upstream_base':'https://primary.example/v1',
        'dispatch_started':True,'dispatch_state':'started','finish_reason':'stop',
        'response_blob':store.blob(json.dumps(original_response).encode()),
    }
    job.update(status='running',stage='active_plan',pending=key,current_step_key=key,
        calls=[old_call],logical_requests={})
    store.put_job(job)

    outcome=Provider(store,_config(route)).generate(
        project['id'],'active_plan',{'source':'same saved step'},_SCHEMA,job,key)

    request_id=logical_request_id(job,key)
    assert outcome.status=='SUCCESS' and outcome.value=={'answer':'saved result'}
    assert job['calls'][0]['logical_request_id']==request_id
    assert job['logical_requests'][request_id]['attempt_count']==1
    assert job['logical_requests'][request_id]['recovery_count']==1


def test_public_recover_original_updates_logical_status_and_query_count(
        tmp_path,skill,monkeypatch):
    store,project,job=_job(tmp_path,skill)
    queue=Queue(store,pipeline='active_composition_v2')
    key='active-plan-public-result-query'
    request_id=logical_request_id(job,key)
    upstream_id='router-result-123'
    call={
        'id':'router-original-call','logical_request_id':request_id,
        'role':'active_plan','status':'uncertain','step_key':key,
        'channel':'router','provider_id':'router-primary','protocol':'responses',
        'upstream_id':upstream_id,'upstream_base':'https://router.example/v1',
        'dispatch_started':True,'dispatch_state':'started',
    }
    job.update(status='uncertain',stage='active_plan',pending=key,current_step_key=key,
        calls=[call],logical_requests={request_id:{
            'step_key':key,'role':'active_plan','started_at':time.time(),
            'status':'UNKNOWN','attempt_count':1,'recovery_count':0,'result_query_count':0,
        }})
    store.put_job(job)
    store.change(project['id'],lambda saved:saved.update(active_job=None,state='uncertain'))
    with store.connect() as cx:
        cx.execute("UPDATE production_control SET status='uncertain',owner=NULL,lease_until=0 WHERE id=?",
                   (job['id'],))

    query_url='https://router.example/v1/api/v1/jobs/'+upstream_id
    requests=[]
    original_client=httpx.Client

    def handler(request):
        requests.append((request.method,str(request.url)))
        return httpx.Response(200,json={
            'id':upstream_id,'status':'succeeded',
            'output':{'structured':{'answer':'queried original result'}},
        })

    def client_factory(*args,**kwargs):
        kwargs['transport']=httpx.MockTransport(handler)
        return original_client(*args,**kwargs)

    monkeypatch.setattr('sourceloom.providers.httpx.Client',client_factory)
    config={'provider':'router','base_url':'https://router.example/v1',
            'api_key':'fake-router-key','role_providers':{}}
    recovered=queue.recover_original(job['id'],config)

    assert recovered=={'recovered':True,'status':'queued','received_service_error':False}
    assert requests==[('GET',query_url)]
    saved=store.job(job['id'])
    logical=saved['logical_requests'][request_id]
    assert logical['status']=='SUCCESS'
    assert logical['result_query_count']==1
    assert logical['attempt_count']==1 and logical['recovery_count']==1
    assert saved['results'][key]=={'answer':'queried original result'}
    attempt=saved['calls'][0]['network_attempts'][0]
    assert attempt['http_status']==200
    for field in ('prepare_ms','queue_or_recovery_wait_ms','dispatch_to_complete_ms',
                  'parse_ms','total_attempt_ms'):
        assert isinstance(attempt[field],(int,float)) and attempt[field]>=0
    assert attempt['dispatch_to_first_byte_ms'] is None
    assert attempt['first_byte_to_complete_ms'] is None


def test_core_v1_router_submit_and_poll_share_network_timing_timeline(
        tmp_path,skill,monkeypatch):
    store,project,job=_job(tmp_path,skill)
    key='active-plan-router-submit-poll-timing'
    route=_route('fake-router','https://router.example/v1')|{
        'provider':'router','api_key':'fake-router-key'}
    config=_config(route)
    config.update(provider='router',provider_id='fake-router',protocol='chat_completions',
                  base_url=route['base_url'],api_key=route['api_key'],effort='medium')
    config['role_providers']={'active_plan':route}
    requests=[]
    poll_count=0

    def handler(request):
        nonlocal poll_count
        requests.append((request.method,str(request.url)))
        if request.method=='POST':
            return httpx.Response(200,json={'id':'router-job-456'})
        poll_count+=1
        if poll_count==1:
            return httpx.Response(200,json={'id':'router-job-456','status':'running'})
        return httpx.Response(200,json={
            'id':'router-job-456','status':'succeeded',
            'output':{'structured':{'answer':'router completed'}},
        })

    _install_http_client_handler(monkeypatch,handler)
    monkeypatch.setattr('sourceloom.providers.time.sleep',lambda _seconds:None)
    outcome=Provider(store,config).generate(
        project['id'],'active_plan',{'source':'fake'},_SCHEMA,job,key)

    request_id=logical_request_id(job,key)
    attempts=job['calls'][0]['network_attempts']
    assert outcome.status=='SUCCESS' and outcome.value=={'answer':'router completed'}
    assert requests==[
        ('POST','https://router.example/v1/api/v1/jobs'),
        ('GET','https://router.example/v1/api/v1/jobs/router-job-456'),
        ('GET','https://router.example/v1/api/v1/jobs/router-job-456'),
    ]
    assert job['calls'][0]['logical_request_id']==request_id
    assert len(attempts)==3
    assert job['logical_requests'][request_id]['attempt_count']==3
    assert job['logical_requests'][request_id]['recovery_count']==0
    assert [attempt['http_status'] for attempt in attempts]==[200,200,200]
    for attempt in attempts:
        for field in ('prepare_ms','queue_or_recovery_wait_ms','dispatch_to_complete_ms',
                      'parse_ms','total_attempt_ms'):
            assert isinstance(attempt[field],(int,float)) and attempt[field]>=0
        assert attempt['dispatch_to_first_byte_ms'] is None
        assert attempt['first_byte_to_complete_ms'] is None
