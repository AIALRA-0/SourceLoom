"""Persistent work ownership; browser lifetime has no execution authority."""

import json
import copy
import math
import re
import time
from pathlib import Path

from .store import Conflict, identity, digest
from .writing import canonical
from .active_policy import (EVIDENCE_GAP_POLICY_REFRESH_VERSION,
                            missing_evidence_gap_policy)


def _refresh_short_rewrite_writer_policy(job, step_key):
    """Version one writer-rule correction for the bounded glossary retry."""
    policy=job.get('role_policy')
    if not isinstance(policy,dict) or 'active_write' not in policy:
        raise Conflict('短篇改写质量重试缺少冻结的 active_write 角色策略')
    if digest(policy)!=job.get('role_policy_digest'):
        raise Conflict('冻结的角色策略校验失败，未排队短篇改写质量重试')
    updated=Path(__file__).parent/'roles'/'active_write.md'
    new_text=updated.read_text(encoding='utf-8')
    old_text=policy['active_write']
    previous_policy_digest=job['role_policy_digest']
    receipt=dict(version='active-write-short-rewrite-coverage-v1',role='active_write',
        step=step_key,previous_text=old_text,previous_role_digest=digest(old_text),
        previous_policy_digest=previous_policy_digest,
        updated_role_digest=digest(new_text),changed=old_text!=new_text,at=time.time(),
        reason='resolve conflicting short-rewrite definition-list instruction for one bounded fresh turn')
    if old_text!=new_text:
        policy['active_write']=new_text
        job['role_policy_digest']=digest(policy)
    receipt['updated_policy_digest']=job['role_policy_digest']
    job.setdefault('versioned_role_policy_refreshes',[]).append(receipt)
    return receipt


def _kuafu_unanswered_call(call, pending):
    from urllib.parse import urlsplit
    return (call.get('status') in {'submitted','uncertain'}
        and call.get('channel')=='openai-compatible' and call.get('dispatch_started')
        and call.get('step_key')==pending and not call.get('response_blob')
        and not call.get('http_status') and not call.get('upstream_id')
        and urlsplit(call.get('upstream_base') or '').hostname=='api.kuafushe.cc')


def _active_patch_unknown_sse_call(call, pending):
    """Exact expired KuaFu Responses SSE shape; no artifact means no recovery query."""
    from urllib.parse import urlsplit
    return (bool(pending) and call.get('step_key')==pending
        and call.get('role')=='active_patch' and call.get('channel')=='openai-compatible'
        and call.get('protocol')=='responses' and call.get('streaming') is True
        and call.get('status')=='uncertain' and call.get('dispatch_started') is True
        and call.get('http_status')==200 and not call.get('response_blob')
        and not call.get('upstream_id') and float(call.get('deadline_at') or 0)<=time.time()
        and urlsplit(str(call.get('upstream_base') or '')).hostname=='api.kuafushe.cc')


def _kuafu_responses_gateway_error(call, pending):
    """A saved 52x gateway error page is not a model artifact and is retryable once via SSE."""
    from urllib.parse import urlsplit
    return (call.get('channel')=='openai-compatible' and call.get('step_key')==pending
        and call.get('provider_id') and call.get('protocol')=='responses'
        and call.get('status')=='uncertain' and call.get('http_status') in {520,521,522,523,524}
        and call.get('response_blob') and call.get('error_code')=='upstream_service_error'
        and urlsplit(call.get('upstream_base') or '').hostname=='api.kuafushe.cc')


def _kuafu_saved_gateway_error(call, pending):
    from urllib.parse import urlsplit
    return (call.get('channel')=='openai-compatible' and call.get('step_key')==pending
        and call.get('status')=='uncertain' and call.get('dispatch_started')
        and call.get('http_status') in {520,521,522,523,524}
        and call.get('response_blob') and call.get('error_code')=='upstream_service_error'
        and urlsplit(str(call.get('upstream_base') or '')).hostname=='api.kuafushe.cc')


def _mark_call_unknown(cx, job, call, reason):
    """Atomically and repeatably preserve an unanswered dispatched call as unknown."""
    call['status']='uncertain'
    call['error_code']='worker_interrupted_after_dispatch'
    call['uncertain_reason']=reason
    call['uncertain_at']=call.get('uncertain_at') or time.time()
    spending=cx.execute('SELECT actual,status,body FROM spending WHERE id=?',(call['id'],)).fetchone()
    prior=json.loads(spending['body']) if spending else {}
    receipt=prior|{'status':'unknown','reason':reason,'call_id':call['id']}
    if spending and spending['actual'] is not None and spending['status']=='settled':
        # Delivery can be uncertain even when billing is known; never erase a settled receipt.
        receipt=prior|{'delivery_state':'unknown','delivery_reason':reason,'call_id':call['id']}
        cx.execute('UPDATE spending SET body=? WHERE id=?',(json.dumps(receipt),call['id']))
    elif spending:
        cx.execute("UPDATE spending SET actual=NULL,status='unknown',body=? WHERE id=?",
                   (json.dumps(receipt),call['id']))
    else:
        cx.execute('INSERT INTO spending(id,project,reserved,actual,status,body,created) VALUES(?,?,?,?,?,?,?)',
                   (call['id'],job['project'],0,None,'unknown',json.dumps(receipt),time.time()))


def _mark_call_not_sent(cx, job, call):
    """Release a call saved before the durable dispatch claim, without sending it."""
    call.update(status='unavailable',error_code='worker_interrupted_before_dispatch',
                dispatch_state='confirmed_not_sent')
    spending=cx.execute('SELECT body FROM spending WHERE id=?',(call['id'],)).fetchone()
    if spending:
        receipt=json.loads(spending['body'])|{
            'status':'rejected','reason':'worker_interrupted_before_dispatch',
            'call_id':call['id'],'actual_cny':0}
        cx.execute("UPDATE spending SET actual=0,status='settled',body=? WHERE id=?",
                   (json.dumps(receipt),call['id']))
    recovery=(job.get('transport_recovery_routes') or {}).get(call.get('step_key'))
    if (recovery and recovery.get('status')=='dispatching'
            and recovery.get('provider_id')==call.get('provider_id')):
        recovery.update(status='queued',attempts=0)
        recovery.pop('started_at',None)
    job.pop('pending',None)


def _split_active_plan_group_once(source, source_ids):
    """Split one ordered source group at a structural boundary near its midpoint."""
    by_id={obj.get('id'):obj for obj in source.get('objects',[])}
    objects=[by_id.get(source_id) for source_id in source_ids]
    if len(objects)<2 or any(obj is None for obj in objects):
        return None

    def container(obj):
        locator=str(obj.get('locator') or '')
        matches=list(re.finditer(r'/(?:li|p|td|th|figure|figcaption|pre|blockquote)\[\d+\]',locator))
        return locator[:matches[-1].end()] if matches else ''

    weights=[len(str(obj.get('text') or '')) for obj in objects]
    total=sum(weights)
    candidates=[]
    max_side_objects=max(1,math.ceil(len(objects)*2/3))
    for boundary in range(1,len(objects)):
        if max(boundary,len(objects)-boundary)>max_side_objects:
            continue
        left=container(objects[boundary-1]);right=container(objects[boundary])
        if left and left==right:
            continue
        left_weight=sum(weights[:boundary])
        # Keep both sides operationally small first; use text size as a tie-breaker.
        candidates.append((abs(len(objects)-2*boundary),abs(total-2*left_weight),boundary))
    if not candidates:
        return None
    boundary=min(candidates)[2]
    left=list(source_ids[:boundary]);right=list(source_ids[boundary:])
    if not left or not right:
        return None
    return left,right


def _exhausted_active_plan_triplet(job, call, read_blob):
    """Identify only the bounded KuaFu primary, backup, and one SSE history."""
    from urllib.parse import urlsplit
    step=call.get('step_key')
    if not isinstance(step,str) or not step.startswith('active-plan-'):
        return None
    rows=[item for item in job.get('calls',[])
        if item.get('step_key')==step and item.get('dispatch_started') is True
        and item.get('channel')=='openai-compatible'
        and urlsplit(str(item.get('upstream_base') or '')).hostname=='api.kuafushe.cc']
    if (len(rows)!=3 or rows[-1].get('id')!=call.get('id')
            or any(str(item.get('role') or '').removesuffix('__fallback')!='active_plan'
                   for item in rows)):
        return None
    first,second,third=rows
    no_receipt=lambda item: (not item.get('response_blob') and not item.get('http_status')
                             and not item.get('upstream_id'))
    first_saved_gateway=(first.get('status')=='uncertain'
        and first.get('http_status') in {520,521,522,523,524}
        and first.get('error_code')=='upstream_service_error' and bool(first.get('response_blob')))
    if (first.get('protocol')!='chat_completions'
            or first.get('status') not in {'submitted','uncertain'}
            or not (no_receipt(first) or first_saved_gateway)
            or second.get('protocol')!='responses'
            or second.get('status')!='uncertain'
            or second.get('streaming') is not False
            or second.get('http_status') not in {520,521,522,523,524}
            or second.get('error_code')!='upstream_service_error'
            or not second.get('response_blob')
            or third.get('protocol')!='responses'
            or third.get('streaming') is not True
            or third.get('provider_id')!=second.get('provider_id')):
        return None
    if (third.get('status') in {'submitted','uncertain'} and no_receipt(third)
            and float(third.get('deadline_at') or 0)<=time.time()
            and job.get('status')=='uncertain' and job.get('pending')==step):
        return 'unknown_sse'
    if (third.get('status')=='invalid' and third.get('finish_reason')=='failed'
            and third.get('http_status') in (None,200) and third.get('response_blob')):
        try:
            response=json.loads(read_blob(third['response_blob']))
        except (KeyError,TypeError,ValueError,OSError):
            return None
        error=response.get('error') if isinstance(response,dict) else None
        error_code=(error.get('code') or error.get('type')) if isinstance(error,dict) else ''
        if response.get('status')=='failed' and error_code=='upstream_error':
            return 'failed_sse'
    return None


def _queue_reciprocal_kuafu_route(job, call, config, reason):
    """Queue only the route explicitly named by the interrupted KuaFu route."""
    key=call.get('step_key')
    if not key or not (_kuafu_unanswered_call(call,key)
                       or _kuafu_saved_gateway_error(call,key)):
        return None
    recoveries=job.setdefault('transport_recovery_routes',{})
    if key in recoveries:
        return recoveries[key]
    routes=config.get('provider_routes') if isinstance(config,dict) else None
    credentials=config.get('provider_credentials') if isinstance(config,dict) else None
    if not isinstance(routes,dict) or not isinstance(credentials,dict):
        return None
    primary=routes.get(call.get('provider_id'))
    if not isinstance(primary,dict) or primary.get('provider')!='openai-compatible':
        return None
    backup_id=str(primary.get('backup_provider_id') or primary.get('backupProviderId') or '')
    if not backup_id:
        return None
    backup=routes.get(backup_id)
    attempted={item.get('provider_id') for item in job.get('calls',[])
               if item.get('step_key')==key and item.get('provider_id')}
    from urllib.parse import urlsplit
    valid=(isinstance(backup,dict) and backup_id not in attempted
        and backup.get('enabled') is not False and backup.get('provider')=='openai-compatible'
        and backup.get('model')==primary.get('model')
        and urlsplit(str(backup.get('base_url') or '')).hostname=='api.kuafushe.cc'
        and backup.get('protocol') in {'chat_completions','responses'}
        and call.get('protocol') in {'chat_completions','responses'}
        and bool(credentials.get(backup_id)))
    if valid:
        state=dict(status='queued',provider_id=backup_id,protocol=backup['protocol'],
            original_call_id=call['id'],original_provider_id=call.get('provider_id'),
            attempts=0,reason=reason)
    else:
        state=dict(status='failed',provider_id=None,original_call_id=call['id'],
            original_provider_id=call.get('provider_id'),attempts=0,
            reason='KuaFu dispatch is uncertain; its configured reciprocal route is unavailable or already tried')
    recoveries[key]=state
    rows=job.setdefault('internal_recoveries',[])
    if not any(row.get('type')=='interrupted_kuafu_dispatch' and row.get('step')==key for row in rows):
        rows.append(dict(stage=job.get('stage'),step=key,type='interrupted_kuafu_dispatch',
            original_call_id=call['id'],provider_id=state.get('provider_id'),at=time.time()))
    job.pop('pending',None)
    return state


def _kuafu_pair_is_failed_without_artifact(job, config, key, read_blob):
    """Match one failed active-v2 KuaFu pair, with no usable result to resume."""
    from urllib.parse import urlsplit
    if (job.get('role')!='production' or job.get('pipeline')!='active_composition_v2'
            or not key or job.get('pending')!=key or key in (job.get('results') or {})):
        return None
    calls=[item for item in job.get('calls',[])
        if item.get('step_key')==key and item.get('dispatch_started') is True
        and item.get('dispatch_state')!='confirmed_not_sent']
    kuafu_calls=[item for item in calls
        if urlsplit(str(item.get('upstream_base') or '')).hostname=='api.kuafushe.cc']
    roles={str(item.get('role') or '').removesuffix('__fallback') for item in kuafu_calls}
    if len(roles)!=1:
        return None
    role=next(iter(roles))
    role_cfg=(config.get('role_providers') or {}).get(role,{})
    if not isinstance(role_cfg,dict) or not role:
        return None
    primary_id=str(role_cfg.get('provider_id') or '')
    routes=config.get('provider_routes')
    credentials=config.get('provider_credentials')
    if not primary_id or not isinstance(routes,dict) or not isinstance(credentials,dict):
        return None
    primary=routes.get(primary_id)
    if (not isinstance(primary,dict) or primary.get('enabled') is False
            or primary.get('provider')!='openai-compatible'
            or urlsplit(str(primary.get('base_url') or '')).hostname!='api.kuafushe.cc'
            or not credentials.get(primary_id)):
        return None
    backup_id=str(primary.get('backup_provider_id') or primary.get('backupProviderId') or
                  role_cfg.get('backup_provider_id') or '')
    backup=routes.get(backup_id)
    if (not backup_id or not isinstance(backup,dict) or backup.get('enabled') is False
            or backup.get('provider')!='openai-compatible'
            or backup.get('backup_provider_id',backup.get('backupProviderId'))!=primary_id
            or backup.get('model')!=primary.get('model')
            or urlsplit(str(backup.get('base_url') or '')).hostname!='api.kuafushe.cc'
            or backup.get('protocol') not in {'chat_completions','responses'}
            or primary.get('protocol') not in {'chat_completions','responses'}
            or not credentials.get(backup_id)):
        return None
    expected={primary_id,backup_id}
    if (not kuafu_calls or any(item.get('provider_id') not in expected
            or str(item.get('role') or '').removesuffix('__fallback')!=role
            for item in kuafu_calls)
            or not expected<={item.get('provider_id') for item in kuafu_calls}):
        return None
    # A complete candidate, even one rejected by a later content validator,
    # must be resumed/repaired instead of being silently replaced by Go output.
    for item in calls:
        if item.get('status') in {'completed','recovered'}:
            return None
        blob=item.get('response_blob')
        if not blob:
            continue
        try:
            body=json.loads(read_blob(blob))
        except (KeyError,TypeError,ValueError,OSError):
            body=None
        if not isinstance(body,dict):
            continue
        if item.get('protocol')=='responses':
            if body.get('status') in {'completed','complete','succeeded'}:
                from .providers import _responses_text
                if _responses_text(body):
                    return None
        else:
            choices=body.get('choices')
            choice=choices[0] if isinstance(choices,list) and choices and isinstance(choices[0],dict) else {}
            message=choice.get('message') if isinstance(choice.get('message'),dict) else {}
            if (choice.get('finish_reason') in {'stop','tool_calls'}
                    and (message.get('content') or message.get('tool_calls'))):
                return None
    allowed={'uncertain','unavailable','rejected','truncated','incomplete','reasoning_exhausted','invalid'}
    for item in kuafu_calls:
        status=item.get('status')
        if status not in allowed:
            return None
        if status=='invalid' and item.get('finish_reason')!='failed':
            return None
        # Queryable upstream work has its own recovery path; do not fork it.
        if status=='uncertain' and item.get('upstream_id'):
            return None
    return dict(role=role,primary_id=primary_id,backup_id=backup_id,
                call_ids=[item.get('id') for item in kuafu_calls],
                unknown_call_ids=[item.get('id') for item in kuafu_calls
                                  if item.get('status')=='uncertain'])


def planning_policy_digest():
    roles=Path(__file__).parent/'roles'
    names=('rewrite_planner','rewrite_plan_review','rewrite_planner_repair','rewrite_scope','plan_decision')
    return digest({name:(roles/(name+'.md')).read_text(encoding='utf-8') for name in names})


def material_signature(source):
    """Identify immutable supplied material across later audit annotations.

    Published fallback inventories add obligations, review receipts and visual
    text to the same source.  Those derived fields must not force the original
    images through the vision model again.  Image identity comes from its saved
    bytes; authored text remains part of the signature.
    """
    objects=[]
    for obj in source.get('objects',[]):
        row={key:obj.get(key) for key in ('id','kind','locator','resource_id','target','raw')}
        if obj.get('kind') not in {'image','page','media'}:
            row['text']=obj.get('text','')
        objects.append(row)
    originals=[{key:item.get(key) for key in ('name','sha256','size')}
               for item in source.get('originals',[])]
    resources=[{key:item.get(key) for key in ('id','name','sha256','size','mime')}
               for item in source.get('resources',[])]
    return digest(dict(source_url=source.get('source_url'),originals=originals,
                       resources=resources,objects=objects))


class Queue:
    def __init__(self, store, max_running=2, pipeline='legacy'):
        self.store = store
        self.pipeline = pipeline
        self.max_running=max(1,min(4,int(max_running)))
        with store.connect() as cx:
            cx.executescript('''
                CREATE TABLE IF NOT EXISTS production_control(
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, status TEXT NOT NULL,
                    owner TEXT, lease_until REAL NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS library_folders(
                    id TEXT PRIMARY KEY, parent TEXT, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS library_versions(
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, created REAL NOT NULL,
                    reason TEXT NOT NULL, body TEXT NOT NULL);
            ''')
        from .library_store import Library
        self.library=Library(store)

    @staticmethod
    def estimate(inventory, max_calls=24, pipeline='legacy'):
        pages=sum(o['kind'] in {'page','image'} and bool(o.get('resource_id')) for o in inventory['objects'])
        visual_batches=math.ceil(pages/3)
        minimum_calls=8+visual_batches*2
        source_chars=sum(len(o.get('text','')) for o in inventory['objects'])
        if pipeline in {'active_composition_v1','active_composition_v2'}:
            visual_calls=math.ceil(pages/(6 if pipeline=='active_composition_v2' else 3))
            return dict(source_chars=source_chars,source_objects=len(inventory['objects']),
                visual_batches=visual_calls,minimum_calls=3+visual_calls,max_calls=None,feasible=True,
                estimate_kind='baseline_only',note='实际调用取决于材料分组、必要查证与局部修复，不承诺固定次数')
        return dict(source_chars=source_chars,source_objects=len(inventory['objects']),
                    visual_batches=visual_batches,minimum_calls=minimum_calls,
                    max_calls=max_calls,feasible=minimum_calls<=max_calls)

    def enqueue(self, pid, bundle, expected_intake=None):
        if self.pipeline == 'active_composition_v1':
            raise Conflict('内部错误：旧逐批流程只允许续接已标记的存量任务')
        now = time.time()
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row = cx.execute('SELECT body FROM projects WHERE id=?', (pid,)).fetchone()
            if not row:
                raise KeyError(pid)
            p = json.loads(row[0])
            if p.get('trashed'):
                raise Conflict('先恢复材料，再开始生成')
            incoming=None
            if expected_intake:
                prior=cx.execute("SELECT body FROM jobs WHERE id=? AND project=? AND role='intake'",(expected_intake,pid)).fetchone()
                incoming=json.loads(prior[0]) if prior else {}
                if (incoming.get('status')!='running' or not incoming.get('inventory_saved') or
                        p.get('active_job')!=expected_intake):raise Conflict('接入任务已停止或材料已变化')
            if p.get('active_job') and not expected_intake:
                return self.store.job(p['active_job'])
            if not p.get('inventory'):
                raise Conflict('先上传文件或导入网页')
            estimate=self.estimate(p['inventory'],pipeline=self.pipeline)
            if self.pipeline not in {'active_composition_v1','active_composition_v2'} and not estimate['feasible']:
                raise Conflict(f"原件至少需要 {estimate['minimum_calls']} 次调用完成基本视觉与正文流程，超过单篇 {estimate['max_calls']} 次上限，请先缩小本次材料范围")
            if p.get('draft'):
                raise Conflict('已有正文，请建立材料副本或修改当前版本')
            if cx.execute("SELECT 1 FROM production_control WHERE project=? AND status='uncertain'", (pid,)).fetchone():
                raise Conflict('原请求结果尚不确定，请查询原任务，不能重新生成')
            if cx.execute("SELECT 1 FROM production_control WHERE project=?",(pid,)).fetchone():
                raise Conflict('本篇已有处理记录，请继续原任务或明确建立新的材料副本，不能重置本篇限制')
            jid = identity()
            job = dict(id=jid,project=pid,role='production',status='queued',created=now,calls=[],pipeline=self.pipeline,
                       joint_review_before_repair=True,fidelity_review_version=2,source_snapshot_digest=digest(p['inventory']),
                       stage='visual_extract' if any(o['kind'] in {'page','image'} and o.get('resource_id') for o in p['inventory']['objects']) else 'inventory',
                       results={},repair_rounds=0,planning_policy_digest=planning_policy_digest(),teaching_version=2,writing_contract_version=7,review_order='style_first',transformation_mode=p.get('mode','rewrite'),base_revision=p['revision'],
                       source_digest=p['inventory']['digest'],source=p['inventory'],goal=p['goal'],project_goal=p['goal'],
                       writing_skill={k:bundle[k] for k in ('root','package_digest','instruction_digest')})
            if self.pipeline == 'active_composition_v2':
                job['core_chain_version'] = 1
            if self.pipeline in {'active_composition_v1','active_composition_v2'}:
                from .active_composition import initialize
                initialize(job)
            cx.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',
                       (jid,pid,'production','queued',now,json.dumps(job,ensure_ascii=False)))
            cx.execute('INSERT INTO production_control(id,project,status,created) VALUES(?,?,?,?)', (jid,pid,'queued',now))
            if incoming:
                incoming.update(status='completed',production_job=jid,finished=now)
                cx.execute("UPDATE jobs SET status='completed',body=? WHERE id=?",(json.dumps(incoming,ensure_ascii=False),expected_intake))
            p.update(active_job=jid,state='queued',max_calls=24)
            if self.pipeline in {'active_composition_v1','active_composition_v2'}:p['max_calls']=None
            cx.execute('UPDATE projects SET body=? WHERE id=?', (json.dumps(p,ensure_ascii=False),pid))
        return job

    def claim(self, owner, lease=45, project=None, recovery_config=None):
        now = time.time()
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            if cx.execute("SELECT count(*) FROM production_control WHERE status='running' AND lease_until>=?",(now,)).fetchone()[0]>=self.max_running:
                return None
            # A continuation can leave older control rows behind. Only the job
            # named by the project's active pointer may run, otherwise an old
            # queued row can publish after a newer continuation and restore a
            # stale draft. The project pointer and claim update are checked in
            # this same immediate transaction. Dedicated workers may help a
            # project, but cannot repeatedly bypass an older queued material.
            row = cx.execute("""SELECT control.* FROM production_control AS control
                JOIN projects AS project_row ON project_row.id=control.project
                WHERE (control.status='queued' OR (control.status='running' AND control.lease_until<?))
                  AND json_extract(project_row.body,'$.active_job')=control.id
                ORDER BY control.created LIMIT 1""", (now,)).fetchone()
            if not row or project is not None and row['project']!=project:
                return None
            cx.execute("UPDATE production_control SET status='running',owner=?,lease_until=? WHERE id=?", (owner,now+lease,row['id']))
            job = json.loads(cx.execute('SELECT body FROM jobs WHERE id=?',(row['id'],)).fetchone()[0])
            job['recovered_lease'] = row['status']=='running'
            job.setdefault('started',now)
            job['status']='running'
            job['worker_owner']=owner
            if job['recovered_lease']:
                call=(job.get('calls') or [{}])[-1]
                pending=job.get('pending')
                if pending and call.get('step_key')!=pending:
                    # The worker stopped while preparing or reserving the next
                    # request. The prior step's completed response must never
                    # be interpreted as the pending step's output.
                    job.setdefault('internal_recoveries',[]).append(dict(
                        stage=job.get('stage'),step=pending,
                        type='interrupted_before_call_record',at=time.time()))
                    job.pop('pending',None)
                if (call.get('status')=='submitted' and call.get('channel')=='openai-compatible'
                        and call.get('dispatch_started') is False and call.get('step_key')==pending
                        and not call.get('response_blob') and not call.get('http_status')
                        and not call.get('upstream_id')):
                    _mark_call_not_sent(cx,job,call)
                if (call.get('status')=='submitted' and call.get('channel')=='openai-compatible'
                        and call.get('dispatch_started') and call.get('step_key')==pending
                        and not call.get('response_blob') and not call.get('http_status')
                        and not call.get('upstream_id')):
                    reason='worker lease expired after dispatch; no response or queryable upstream identity was saved'
                    _mark_call_unknown(cx,job,call,reason)
                    if (job.get('pipeline')=='active_composition_v2'
                            and job.get('core_chain_version')==0):
                        from urllib.parse import urlsplit
                        if urlsplit(call.get('upstream_base') or '').hostname=='api.kuafushe.cc':
                            recovery=(job.get('transport_recovery_routes') or {}).get(pending)
                            if (recovery and recovery.get('recovery_protocol_version')=='kuafu-responses-sse-v1'
                                    and recovery.get('status')=='dispatching'):
                                recovery.update(status='failed',error='worker_interrupted_during_sse',
                                    attempt_call_id=call.get('id'),finished_at=time.time())
                                job.pop('pending',None)
                            else:
                                from .production import queue_kuafu_responses_stream_recovery
                                if not queue_kuafu_responses_stream_recovery(
                                        job,recovery_config or {},legacy_excess=True):
                                    _queue_reciprocal_kuafu_route(job,call,recovery_config or {},
                                        'original KuaFu call delivery is unknown after worker restart; its configured reciprocal route is queued once')
                elif (pending and job.get('core_chain_version')==0
                      and _kuafu_saved_gateway_error(call,pending)):
                    from .production import queue_kuafu_responses_stream_recovery
                    if not queue_kuafu_responses_stream_recovery(
                            job,recovery_config or {}):
                        _queue_reciprocal_kuafu_route(job,call,recovery_config or {},
                            'saved KuaFu 52x response had no model artifact; its configured reciprocal route is queued once')
            cx.execute("UPDATE jobs SET status='running',body=? WHERE id=?", (json.dumps(job,ensure_ascii=False),job['id']))
        return job

    def rewrite_existing(self,pid,bundle,quality_parent=None):
        """Reuse verified source inventory, while recording a new rewrite of the saved version."""
        if self.pipeline in {'active_composition_v1','active_composition_v2'} and not quality_parent:
            return self.rewrite_active(pid,bundle)
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM projects WHERE id=?',(pid,)).fetchone()
            if not row:raise KeyError(pid)
            p=json.loads(row[0])
            if p.get('active_job') or p.get('trashed'):
                raise Conflict('当前材料仍在处理中或已在回收站')
            prior=cx.execute("SELECT body FROM jobs WHERE project=? AND role='production' ORDER BY created DESC LIMIT 1",(pid,)).fetchone()
            if not prior and p.get('copied_from',{}).get('inventory_job'):
                prior=cx.execute("SELECT body FROM jobs WHERE id=? AND role='production'",(p['copied_from']['inventory_job'],)).fetchone()
            old=json.loads(prior[0]) if prior else {}
            if quality_parent and (old.get('id')!=quality_parent or old.get('status')!='needs_attention'
                    or old.get('stage') not in {'style','fidelity'} or old.get('quality_fallback_of')
                    or old.get('pending') or not old.get('quality_issues')
                    or not old.get('calls') or old['calls'][-1].get('status') not in {'completed','recovered'}
                    or (p.get('production') or {}).get('job')!=quality_parent
                    or (p.get('production') or {}).get('revision')!=p['revision']
                    or p.get('draft')!=old.get('draft') or p['goal']!=old.get('project_goal')):
                raise Conflict('当前版本不符合一次加强改写的条件，原记录保持不变')
            if not p.get('draft') and not (old.get('status')=='failed' and old.get('stage') in {'planner','plan_review','writer'}
                    and old.get('unit_index',0)==0 and not old.get('draft',{}).get('blocks')
                    and old.get('calls') and old['calls'][-1].get('status') in {'completed','invalid','truncated','reasoning_exhausted'}):
                raise Conflict('当前没有可重新改写的正文或已明确结束的写作准备')
            if not p.get('draft') and not p['inventory'].get('frozen'):
                # The first attempt can finish source verification before its
                # writer returns anything. Reuse that receipt only for the exact
                # unchanged input, never by file name or an absent digest alone.
                unchanged=(digest(p['inventory'])==old['source_snapshot_digest'] if old.get('source_snapshot_digest') else
                    bool(p['inventory'].get('objects')) and all(p['inventory'].get(k)==old.get('source',{}).get(k) for k in ('objects','originals','resources')))
                if not unchanged or p['revision']!=old.get('base_revision'):
                    raise Conflict('原件在上次核对后发生变化，需要重新清点')
                p['inventory']=copy.deepcopy(old.get('inventory',{}))
            if (old.get('status') in {'uncertain','running','queued'} or not old.get('facts') or
                    old.get('inventory',{}).get('digest')!=p.get('inventory',{}).get('digest')):
                raise Conflict('当前原件与已核对清单不一致，需要重新清点')
            if not p['inventory'].get('frozen') or not p['inventory'].get('inventory_review'):
                raise Conflict('原件清单尚未完成独立核对')
            jid=identity();now=time.time()
            job=dict(id=jid,project=pid,role='production',status='queued',created=now,calls=[],
                joint_review_before_repair=True,fidelity_review_version=2,source_snapshot_digest=digest(p['inventory']),
                stage='planner',results={},repair_rounds=0,planning_policy_digest=planning_policy_digest(),teaching_version=2,writing_contract_version=7,review_order='style_first',transformation_mode='rewrite',
                base_revision=p['revision'],source_digest=p['inventory']['digest'],source=old['source'],
                inventory=p['inventory'],facts=old['facts'],reused_inventory_job=old['id'],
                project_goal=p['goal'],
                goal='保持原文主旨、作者意图与人称，完整保留信息，改善逻辑与可读性，不自行设计课程、情境、练习或拓展',
                writing_skill={k:bundle[k] for k in ('root','package_digest','instruction_digest')})
            if quality_parent:
                job.update(quality_fallback_of=quality_parent,
                    prior_repair_rounds=old.get('prior_repair_rounds',0)+old.get('repair_rounds',0),
                    style_partition_limit=40)
            # A fresh, explicitly requested rewrite can reuse preparation that
            # reached the writer but produced no draft. Keep the original job,
            # reviews and cumulative spending; never reuse uncertain execution.
            if (old.get('status')=='failed' and old.get('stage')=='writer' and old.get('unit_index')==0 and
                    not old.get('draft',{}).get('blocks') and old.get('plan') and
                    old.get('goal')==job['goal'] and old.get('project_goal')==p['goal'] and
                    old.get('transformation_mode')=='rewrite' and
                    old.get('planning_policy_digest')==job['planning_policy_digest'] and
                    old.get('writing_skill',{}).get('package_digest')==bundle['package_digest'] and
                    old.get('calls') and old['calls'][-1].get('status') in {'invalid','truncated','reasoning_exhausted'}):
                from .pedagogy import validate_teaching_plan
                job.update(plan=validate_teaching_plan(old['plan'],p['inventory'],'rewrite'),
                    stage='writer',unit_index=0,draft={'blocks':[]},terminology=[],
                    reused_plan_job=old['id'],verified_terminology=old.get('verified_terminology',[]),
                    optional_source_limits=old.get('optional_source_limits',[]))
            cx.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(jid,pid,'production','queued',now,json.dumps(job,ensure_ascii=False)))
            cx.execute('INSERT INTO production_control(id,project,status,created) VALUES(?,?,?,?)',(jid,pid,'queued',now))
            p.update(active_job=jid,state='queued')
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),pid))
        return job

    def rewrite_active(self,pid,bundle):
        """Explicit new generation keeps historical drafts and cumulative spending."""
        if self.pipeline == 'active_composition_v1':
            raise Conflict('内部错误：旧逐批流程只允许续接已标记的存量任务')
        from .active_composition import initialize
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM projects WHERE id=?',(pid,)).fetchone()
            if not row:raise KeyError(pid)
            p=json.loads(row[0])
            if p.get('active_job') or p.get('trashed') or not p.get('inventory'):
                raise Conflict('材料仍在处理中、已删除或尚未取得原件')
            if cx.execute("SELECT 1 FROM production_control WHERE project=? AND status='uncertain'",(pid,)).fetchone():
                raise Conflict('原请求仍未确定，不能借重新改写重复提交')
            jid=identity();now=time.time()
            job=initialize(dict(id=jid,project=pid,role='production',status='queued',created=now,calls=[],pipeline=self.pipeline,
                core_chain_version=1 if self.pipeline=='active_composition_v2' else 0,
                results={},repair_rounds=0,base_revision=p['revision'],source_digest=p['inventory']['digest'],
                source_snapshot_digest=digest(p['inventory']),source=copy.deepcopy(p['inventory']),
                goal=p['goal'],project_goal=p['goal'],transformation_mode=p.get('mode','rewrite'),
                writing_skill={k:bundle[k] for k in ('root','package_digest','instruction_digest')}))
            from .visual_sources import decorative_resource
            previous=cx.execute("SELECT body FROM jobs WHERE project=? AND role='production' ORDER BY created DESC",(pid,)).fetchall()
            history=[json.loads(row[0]) for row in previous]
            # A cancelled retry can be newer than the last complete visual pass
            # Search history for the newest exact, complete visual checkpoint
            for old in history:
                if old.get('core_chain_version')!=job.get('core_chain_version'):
                    continue
                cards=old.get('visual_cards',[])
                visual_ids={o['id'] for o in old.get('source',{}).get('objects',[])
                    if o['kind'] in {'image','page','media'} and o.get('resource_id')}
                expected={o['id'] for o in old.get('source',{}).get('objects',[])
                    if o['id'] in visual_ids and not decorative_resource(o)}
                card_ids={c['source_id'] for c in cards}
                if (expected and material_signature(old.get('source',{}))==material_signature(job['source'])
                        and old.get('writing_skill',{}).get('package_digest')==bundle['package_digest']
                        and expected<=card_ids<=visual_ids
                        and not any(c.get('blocking_uncertainty',c.get('uncertainty',[])) for c in cards)
                        and not old.get('source',{}).get('unknown')):
                    from .source_context import classify_inert_markup
                    job.update(source=classify_inert_markup(old['source']),visual_cards=copy.deepcopy(cards),
                        visual_index=len(cards),visual_count=len(cards),stage='active_visual',reused_visual_job=old['id'])
                    break
            # Reuse only plans that already passed the full validator. Failed
            # partition sessions and their raw responses remain historical, while
            # the new job resumes at the first unvalidated source group
            plan_candidates=[]
            current_archived={o['id'] for o in job.get('source',{}).get('objects',[])
                              if decorative_resource(o)}
            for old in history:
                if old.get('core_chain_version')!=job.get('core_chain_version'):
                    continue
                completed=old.get('active_plans',[]);index=old.get('active_partition_index',0)
                groups=old.get('active_groups',[])
                old_archived=(set(old['archived_layout_source_ids'])
                              if 'archived_layout_source_ids' in old else current_archived)
                if (job.get('reused_visual_job') and completed and index==len(completed)
                        and index<=len(groups)
                        and old_archived==current_archived
                        and material_signature(old.get('source',{}))==material_signature(job['source'])
                        and old.get('writing_skill',{}).get('package_digest')==bundle['package_digest']
                        and old.get('role_policy_digest')==job.get('role_policy_digest')):
                    checkpoints=old.get('active_checkpoints',[])
                    checkpoint_nodes={point.get('node_id') for point in checkpoints}
                    checkpoint_draft={'blocks':[copy.deepcopy(block)
                        for block in old.get('draft',{}).get('blocks',[])
                        if block.get('unit_id') in checkpoint_nodes]}
                    checkpoint_bytes_valid=all(
                        digest(canonical({'blocks':[block for block in checkpoint_draft['blocks']
                            if block.get('unit_id')==point.get('node_id')]}).encode())==point.get('draft_digest')
                        for point in checkpoints)
                    reusable_checkpoints=(len(checkpoints)==int(old.get('unit_index',0))
                        and checkpoint_bytes_valid
                        and all(not point.get('unresolved_content_findings')
                            and not point.get('unresolved_revision')
                            and not point.get('unresolved_format',{}).get('findings')
                            and not point.get('unresolved_format',{}).get('candidates')
                            for point in checkpoints))
                    plan_candidates.append((index==len(groups),index,
                        len(checkpoints) if reusable_checkpoints else 0,
                        float(old.get('created',0)),old,groups))
            if plan_candidates:
                _,index,checkpoint_count,_,old,groups=max(plan_candidates,key=lambda item:item[:4])
                completed=old['active_plans']
                job.update(active_groups=copy.deepcopy(groups),
                    active_plans=copy.deepcopy(completed),active_partition_index=index,
                    active_partition_count=len(groups),stage='active_plan',
                    archived_layout_source_ids=copy.deepcopy(old.get('archived_layout_source_ids',[])),
                    web_chrome_scope_version=old.get('web_chrome_scope_version',2),
                    reused_plan_job=old['id'])
                if index==len(groups) and old.get('writing_batches') and old.get('plan') and old.get('inventory'):
                    job['reused_writing_preparation_job']=old['id']
                    if checkpoint_count and checkpoint_count<=int(len(old['writing_batches'])):
                        checkpoint_nodes={point['node_id'] for point in old['active_checkpoints']}
                        checkpoint_draft={'blocks':[copy.deepcopy(block)
                            for block in old.get('draft',{}).get('blocks',[])
                            if block.get('unit_id') in checkpoint_nodes]}
                        job.update(stage=('active_deliver' if checkpoint_count==len(old['writing_batches'])
                                         else 'active_write'),unit_index=checkpoint_count,
                            writing_batches=copy.deepcopy(old['writing_batches']),
                            inventory=copy.deepcopy(old['inventory']),plan=copy.deepcopy(old['plan']),
                            draft=checkpoint_draft,
                            active_checkpoints=copy.deepcopy(old['active_checkpoints']),
                            knowledge_memory=copy.deepcopy(old.get('knowledge_memory',[])),
                            generated_resources=copy.deepcopy(old.get('generated_resources',{})),
                            reused_writing_checkpoint_job=old['id'])
            cx.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(jid,pid,'production','queued',now,json.dumps(job,ensure_ascii=False)))
            cx.execute('INSERT INTO production_control(id,project,status,created) VALUES(?,?,?,?)',(jid,pid,'queued',now))
            p.update(active_job=jid,state='queued',max_calls=None)
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),pid))
        return job

    def continue_quality_repairs(self, jid, config):
        """Create an auditable continuation from a published unresolved draft."""
        limit=max(2,min(32,int(config.get('max_repair_rounds',2))))
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            old=json.loads(row[0]);project=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(old['project'],)).fetchone()[0])
            receipt=project.get('production') or {}
            if (old.get('role')!='production' or old.get('status')!='needs_attention'
                    or old.get('repair_rounds',0)>=limit or not old.get('quality_issues')
                    or project.get('active_job') or project.get('trashed') or project.get('draft')!=old.get('draft')
                    or receipt.get('job')!=jid or receipt.get('status')!='needs_attention'
                    or project.get('revision')!=old.get('base_revision',0)+1):
                raise Conflict('当前没有可继续的已发布质量修复')
            now=time.time();new=copy.deepcopy(old);new_id=identity()
            for key in ('pending','active_repair_round','finished','worker_owner','error'):
                new.pop(key,None)
            new.update(id=new_id,status='queued',stage='repair',created=now,started=now,calls=[],
                base_revision=project['revision'],quality_continuation_of=jid)
            new.setdefault('quality_history',[]).append({'job':jid,'issues':copy.deepcopy(old['quality_issues']),
                'repair_rounds':old['repair_rounds'],'draft_digest':digest(old['draft'])})
            cx.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(new_id,old['project'],'production','queued',now,json.dumps(new,ensure_ascii=False)))
            cx.execute('INSERT INTO production_control(id,project,status,created) VALUES(?,?,?,?)',(new_id,old['project'],'queued',now))
            project.update(active_job=new_id,state='queued')
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(project,ensure_ascii=False),old['project']))
        return new

    def continue_after_quality_system_fix(self, jid, config, fix_id):
        """Start one fresh bounded review epoch after a recorded harness correction.

        The exhausted job, every response, every repair commit and cumulative
        attempt count remain immutable. This is an explicit recovery path for
        a diagnosed verifier defect, not an automatic way to evade limits.
        """
        fix_id=str(fix_id).strip()
        if not fix_id or len(fix_id)>120:
            raise ValueError('质量系统修复标识需为 1 到 120 个字符')
        limit=max(2,min(32,int(config.get('max_repair_rounds',2))))
        max_epochs=max(1,min(3,int(config.get('max_quality_system_continuations',1))))
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            old=json.loads(row[0])
            project=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(old['project'],)).fetchone()[0])
            receipt=project.get('production') or {}
            fixes=list(old.get('quality_system_fixes',[]))
            if (old.get('role')!='production' or old.get('status')!='needs_attention'
                    or old.get('stage') not in {'style','fidelity','repair'}
                    or old.get('repair_rounds',0)<limit or not old.get('quality_issues')
                    or project.get('active_job') or project.get('trashed')
                    or project.get('draft')!=old.get('draft')
                    or receipt.get('job')!=jid or receipt.get('status')!='needs_attention'
                    or project.get('revision')!=old.get('base_revision',0)+1):
                raise Conflict('当前不是已用完有界修复且可在系统修正后续跑的版本')
            if fix_id in fixes or len(fixes)>=max_epochs:
                raise Conflict('该系统修复已续跑，或系统修复续跑次数达到上限')
            now=time.time();new=copy.deepcopy(old);new_id=identity()
            for key in ('pending','active_repair_round','finished','worker_owner','error','error_type',
                        'style','scan','fidelity','style_draft_digest','fidelity_draft_digest'):
                new.pop(key,None)
            discarded_prefixes=('style-','fidelity-','line_repair-','local_repair-')
            new['results']={key:value for key,value in old.get('results',{}).items()
                if not key.startswith(discarded_prefixes)}
            new.update(id=new_id,status='queued',stage='style',created=now,started=now,calls=[],
                base_revision=project['revision'],repair_rounds=0,
                prior_repair_rounds=old.get('prior_repair_rounds',0)+old.get('repair_rounds',0),
                quality_system_fixes=fixes+[fix_id],quality_system_continuation_of=jid)
            new.setdefault('quality_history',[]).append({'job':jid,'issues':copy.deepcopy(old['quality_issues']),
                'repair_rounds':old['repair_rounds'],'draft_digest':digest(old['draft']),
                'continuation_reason':'quality_system_fix','fix_id':fix_id})
            cx.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',
                (new_id,old['project'],'production','queued',now,json.dumps(new,ensure_ascii=False)))
            cx.execute('INSERT INTO production_control(id,project,status,created) VALUES(?,?,?,?)',
                (new_id,old['project'],'queued',now))
            project.update(active_job=new_id,state='queued')
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(project,ensure_ascii=False),old['project']))
        return new

    def heartbeat(self, jid, owner, lease=45):
        with self.store.connect() as cx:
            return cx.execute("UPDATE production_control SET lease_until=? WHERE id=? AND owner=? AND status='running'", (time.time()+lease,jid,owner)).rowcount==1

    def continue_after_quota_error(self,jid,execution_channel):
        """Explicit continuation on a different configured channel after terminal quota rejection."""
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0]);call=job.get('calls',[])[-1] if job.get('calls') else {}
            response=json.loads(self.store.read_blob(call['response_blob'])) if call.get('response_blob') else {}
            request=json.loads(self.store.read_blob(call['wire_request_blob'])) if call.get('wire_request_blob') else {}
            old_channel=request.get('task',{}).get('executionChannel','codex')
            key=job.get('pending')
            if (job['status']!='uncertain' or response.get('status')!='failed' or
                    response.get('errorCode')!='codex_quota_exhausted' or response.get('output') or
                    execution_channel!='chatgpt_web' or old_channel==execution_channel or not key or
                    key in job.get('quota_continuations',{})):
                raise Conflict('只有已明确额度拒绝、没有输出且已另配通道的请求可以继续；未知提交不能重发')
            p=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if p.get('active_job') or p.get('trashed') or p['revision']!=job['base_revision']:
                raise Conflict('原版本已变化或材料不可继续处理')
            job.setdefault('quota_continuations',{})[key]={'previous_call':call['id'],'from':old_channel,'to':execution_channel}
            job.pop('pending');job.update(status='queued',error=None)
            p.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        return job

    def cancelled(self, jid, owner=None):
        with self.store.connect() as cx:
            row = cx.execute('SELECT cancel_requested,owner FROM production_control WHERE id=?',(jid,)).fetchone()
        return not row or bool(row['cancel_requested']) or (owner is not None and row['owner']!=owner)

    def cancel(self, jid):
        if self.store.job(jid)['role']=='intake':
            from .intake_jobs import IntakeQueue
            return IntakeQueue(self.store,{}).cancel(jid)
        with self.store.connect() as cx:
            if not cx.execute('UPDATE production_control SET cancel_requested=1 WHERE id=?',(jid,)).rowcount:
                raise KeyError(jid)
        return {'cancel_requested':True}

    def continue_preprocessing(self, jid):
        """Resume a failed zero-call intake after missing assets were supplied.

        The original document bytes and job identity stay fixed; this cannot
        replay a generation or reset a paid call budget.
        """
        from .active_composition import initialize
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0])
            project_row=cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()
            project=json.loads(project_row[0])
            old=job.get('inventory') or job.get('source',{})
            new=project.get('inventory',{})
            old_originals={(item['name'],item['sha256']) for item in old.get('originals',[])}
            new_originals={(item['name'],item['sha256']) for item in new.get('originals',[])}
            if (job.get('pipeline') not in {'active_composition_v1','active_composition_v2'}
                    or job.get('core_chain_version') not in {0,1} or job['status']!='failed'
                    or job.get('calls') or job.get('pending') or job.get('draft',{}).get('blocks')
                    or job.get('stage') not in {'active_index','active_visual'}
                    or project.get('active_job') or project.get('draft')
                    or not old.get('originals') or not new.get('originals')
                    or not old_originals.issubset(new_originals)
                    or len(new.get('unknown',[]))>=len(old.get('unknown',[]))):
                raise Conflict('只允许同一原件补齐资源后续接尚未发出模型请求的接入任务')
            job.update(inventory=new,source=new,source_digest=new['digest'],
                       source_snapshot_digest=digest(new),status='queued',error=None)
            job.pop('error_type',None);job.pop('finished',None)
            initialize(job)
            project.update(active_job=jid,state='queued')
            cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',
                       ('queued',json.dumps(job,ensure_ascii=False),jid))
            cx.execute('UPDATE projects SET body=? WHERE id=?',
                       (json.dumps(project,ensure_ascii=False),project['id']))
            cx.execute('UPDATE production_control SET status=?,owner=NULL,lease_until=0,cancel_requested=0 WHERE id=?',
                       ('queued',jid))
        return job

    def retry_validation(self, jid, config=None):
        """Continue a known returned artifact after a code fix, retaining all limits."""
        snapshot=self.store.job(jid)
        if snapshot.get('core_chain_version')==1:
            if snapshot.get('status') in {'uncertain','failed'} and config is not None:
                self.recover_original(jid,config)
                return self.store.job(jid)
            raise Conflict('新版任务只能通过 Provider 查询原请求，不能进入旧版续接流程')
        last=(snapshot.get('calls') or [{}])[-1]
        if self._queue_saved_responses_artifact(jid,snapshot,last):
            return self.store.job(jid)
        if (config and snapshot.get('status') in {'uncertain','failed'}
                and snapshot.get('pipeline')=='active_composition_v2'
                and snapshot.get('core_chain_version')==0
                and self._queue_kuafu_go_third_route(jid,config,snapshot,last)):
            return self.store.job(jid)
        if (config and snapshot.get('core_chain_version')==0
                and snapshot.get('status')=='failed'
                and snapshot.get('stage')=='active_plan'
                and last.get('protocol')=='responses'
                and last.get('status')=='invalid'
                and last.get('finish_reason')=='failed'
                and last.get('response_blob')):
            return self.continue_failed_active_plan_context(jid)
        if (config and snapshot.get('core_chain_version')==0
                and snapshot.get('status')=='uncertain'
                and snapshot.get('pipeline')=='active_composition_v2'
                and snapshot.get('stage')=='active_plan'
                and _exhausted_active_plan_triplet(snapshot,last,self.store.read_blob)=='unknown_sse'):
            return self.continue_failed_active_plan_context(jid)
        if (config and snapshot.get('core_chain_version')==0
                and snapshot.get('status')=='uncertain'
                and snapshot.get('pipeline')=='active_composition_v2'
                and self._queue_kuafu_responses_stream_retry(jid,config,snapshot,last)):
            return self.store.job(jid)
        if (config and snapshot.get('core_chain_version')==0
                and snapshot.get('status')=='uncertain'
                and snapshot.get('pipeline')=='active_composition_v2'
                and _kuafu_unanswered_call(last,snapshot.get('pending'))):
            return self.continue_interrupted_kuafu(jid,config)
        if (config and snapshot.get('core_chain_version')==0
                and snapshot.get('status')=='uncertain'
                and snapshot.get('pipeline')=='active_composition_v2'
                and snapshot.get('stage')=='active_revision'
                and _active_patch_unknown_sse_call(last,snapshot.get('pending'))):
            return self.continue_active_patch_unknown_sse(jid)
        if (config and snapshot.get('core_chain_version')==0
                and snapshot.get('status')=='failed' and snapshot.get('pending')==last.get('step_key')
                and last.get('status')=='submitted' and last.get('dispatch_started')
                and not last.get('response_blob') and not last.get('http_status')
                and last.get('deadline_at',0)<=time.time()):
            self._mark_interrupted_subscription_uncertain(jid,last['id'])
            return self.continue_unqueryable_subscription(jid,config)
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:
                raise KeyError(jid)
            job=json.loads(row[0])
            if job['role']!='production' or job['status']!='failed':
                raise Conflict('只能继续已经收到完整响应的校验失败任务')
            call=job['calls'][-1] if job['calls'] else {}
            call_role=str(call.get('role') or '').removesuffix('__fallback')
            # One bounded quality escalation for a completed short-rewrite
            # glossary correction that exhausted its scoped repair. Queue the
            # next turn of that same active writer session on its configured
            # KuaFu primary; retain all prior results and checkpoints.
            escalation_session=None
            fresh_turn_retry_session=None
            policy_refresh_session=None
            policy_refresh=None
            exhausted_policy_refresh=False
            if (config and job.get('pipeline')=='active_composition_v2'
                    and job.get('stage')=='active_plan' and not job.get('pending')
                    and call.get('role')=='active_plan'
                    and call.get('status') in {'completed','recovered'}
                    and call.get('response_blob')):
                gap_policy=missing_evidence_gap_policy(job.get('error'))
                step_key=call.get('step_key','')
                saved=(job.get('results') or {}).get(step_key)
                repair_limit=max(1,min(2,int(config.get('max_plan_repairs',2))))
                for session_key,session in (job.get('active_sessions') or {}).items():
                    turn_prefix=session_key+'-turn-'
                    if not step_key.startswith(turn_prefix):
                        continue
                    marker=(job.get('active_plan_policy_refreshes') or {}).get(session_key,{})
                    if (marker.get('version')==EVIDENCE_GAP_POLICY_REFRESH_VERSION
                            and marker.get('next_step_key')==step_key and gap_policy):
                        exhausted_policy_refresh=True
                        break
                    if (not gap_policy or marker or int(session.get('corrections',0))<repair_limit
                            or not isinstance(saved,dict) or not isinstance(saved.get('result'),dict)):
                        continue
                    try:
                        failed_turn=int(step_key[len(turn_prefix):])
                    except ValueError:
                        continue
                    if failed_turn!=int(session.get('round',-1)):
                        continue
                    declared_ids=set(session.get('declared_gap_ids',[]))
                    returned_ids={item.get('id') for item in saved['result'].get('evidence_gaps',[])
                                  if isinstance(item,dict) and item.get('id')}
                    missing_ids=set(gap_policy['gap_ids'])
                    action_gap_ids={entry.get('action',{}).get('gap_id')
                        for entry in session.get('action_history',[])
                        if entry.get('action',{}).get('kind') in {'search','page','image'}}
                    previously_corrected=any(
                        receipt.get('step')==session_key and receipt.get('role')=='active_plan'
                        and (previous:=missing_evidence_gap_policy(receipt.get('error')))
                        and set(previous['gap_ids'])==missing_ids
                        for receipt in job.get('active_correction_receipts',[]))
                    if (not missing_ids or not missing_ids<=declared_ids
                            or not missing_ids<=action_gap_ids
                            or missing_ids & returned_ids or previously_corrected):
                        continue
                    policy_refresh_session=session_key
                    policy_refresh=dict(error=gap_policy['error'],
                                        instruction=gap_policy['instruction'],
                                        gap_ids=sorted(missing_ids),
                                        previous_call_id=call.get('id'),
                                        previous_step_key=step_key,
                                        next_step_key=session_key+'-turn-'+str(failed_turn+1))
                    session['round']=max(int(session.get('round',0)),failed_turn+1)
                    session['correction']=dict(error=policy_refresh['error'],
                                               instruction=policy_refresh['instruction'])
                    break
            if exhausted_policy_refresh:
                raise Conflict('本阶段一次性 EvidenceGap 策略刷新已用完；已保留结果，未重复请求模型')
            if (config and job.get('pipeline')=='active_composition_v2'
                    and job.get('stage')=='active_write' and not job.get('pending')
                    and call_role=='active_write' and str(call.get('role','')).endswith('__fallback')
                    and call.get('status') in {'completed','recovered'} and call.get('response_blob')
                    and '短篇普通改写被扩成术语表' in str(job.get('error') or '')):
                from .providers import apply_route_override
                primary_cfg=(config.get('role_providers') or {}).get('active_write',{})
                primary=apply_route_override(config,primary_cfg)
                primary_id=primary.get('provider_id')
                from urllib.parse import urlsplit
                primary_host=urlsplit(primary.get('base_url') or '').hostname
                primary_key=(primary.get('api_key') or
                    (config.get('provider_credentials') or {}).get(primary_id))
                fallback_cfg=(config.get('fallback_providers') or {}).get('active_write',{})
                fallback=apply_route_override(config,fallback_cfg)
                if (primary.get('provider')=='openai-compatible' and primary_host=='api.kuafushe.cc'
                        and primary_key and primary_id and primary_id!=call.get('provider_id')
                        and call.get('provider_id')==fallback.get('provider_id')):
                    repair_limit=max(1,min(2,int(config.get('active_structure_correction_limit',2))))
                    for session_key,session in (job.get('active_sessions') or {}).items():
                        if (call.get('step_key','').startswith(session_key+'-turn-')
                                and session.get('short_rewrite_glossary_repair_used') is True
                                and int(session.get('corrections',0))>=repair_limit+1
                                and session.get('correction')
                                and session_key not in job.get('active_glossary_quality_escalations',{})):
                            escalation_session=session_key
                            # Keep the complete failed artifact as correction context,
                            # then resume at a fresh turn so the cached response cannot
                            # immediately fail validation again.
                            result=(job.get('results') or {}).get(call.get('step_key'))
                            if isinstance(result,dict) and isinstance(result.get('result'),dict):
                                session['previous_invalid_result']=copy.deepcopy(result['result'])
                            try:
                                failed_turn=int(call.get('step_key','').rsplit('-turn-',1)[1])
                            except (IndexError,ValueError):
                                failed_turn=int(session.get('round',0))
                            session['round']=max(int(session.get('round',0)),failed_turn+1)
                            session['quality_escalation_queued']=True
                            break
            # If the one primary quality escalation also returned a completed
            # but invalid glossary artifact, grant one more fresh writer turn.
            # The cached artifact stays as correction context; this marker can
            # never authorize a replay of its step or a provider fallback.
            if (not escalation_session and config and job.get('pipeline')=='active_composition_v2'
                    and job.get('stage')=='active_write' and not job.get('pending')
                    and call_role=='active_write' and call.get('role')=='active_write'
                    and call.get('status') in {'completed','recovered'} and call.get('response_blob')
                    and '短篇普通改写被扩成术语表' in str(job.get('error') or '')):
                repair_limit=max(1,min(2,int(config.get('active_structure_correction_limit',2))))
                for session_key,session in (job.get('active_sessions') or {}).items():
                    first=(job.get('active_glossary_quality_escalations') or {}).get(session_key,{})
                    result=(job.get('results') or {}).get(call.get('step_key'))
                    if (not call.get('step_key','').startswith(session_key+'-turn-')
                            or session.get('short_rewrite_glossary_repair_used') is not True
                            or int(session.get('corrections',0))<repair_limit+1
                            or not session.get('correction')
                            or first.get('version')!='active-short-rewrite-glossary-kuafu-v1'
                            or first.get('status')!='completed' or first.get('attempts')!=1
                            or first.get('attempt_call_id')!=call.get('id')
                            or first.get('attempt_step')!=call.get('step_key')
                            or call.get('provider_id')!=first.get('primary_provider_id')
                            or not isinstance(result,dict) or not isinstance(result.get('result'),dict)
                            or session_key in job.get('active_glossary_quality_retries',{})):
                        continue
                    fresh_turn_retry_session=session_key
                    session['previous_invalid_result']=copy.deepcopy(result['result'])
                    try:
                        failed_turn=int(call.get('step_key','').rsplit('-turn-',1)[1])
                    except (IndexError,ValueError):
                        failed_turn=int(session.get('round',0))
                    session['round']=max(int(session.get('round',0)),failed_turn+1)
                    session['quality_fresh_turn_retry_queued']=True
                    break
            # Migrate the known old checkpoint bug: the trial circuit rejected
            # the request before Provider created any call or dispatched bytes.
            no_call_preflight=(not job['calls'] and (
                job.get('error')=='本批同类失败已连续发生两次，先修正原因，未发送新请求' or
                (job.get('stage')=='active_visual' and
                 job.get('error')=='当前角色通道不能读取原始图片，未降级为仅文字审核' and
                 (config or {}).get('role_providers',{}).get('active_visual',{}).get('provider')=='openai-compatible')))
            if (job.get('pending') and call.get('step_key')!=job['pending'] and
                    job.get('error')=='本批同类失败已连续发生两次，先修正原因，未发送新请求'):
                job.setdefault('preflight_stops',[]).append(dict(key=job['pending'],dispatched=False,
                    reason='legacy_trial_circuit_before_provider_dispatch'))
                job.pop('pending',None)
            billing=cx.execute('SELECT actual,status,body FROM spending WHERE id=?',(call.get('id',''),)).fetchone()
            rejected=bool(billing and billing['actual']==0 and (
                json.loads(billing['body']).get('status')=='rejected' or
                json.loads(billing['body']).get('status')=='quota_rejected' and
                call.get('status')=='rejected' and call.get('error_code')=='subscription_limit_exceeded'))
            if rejected:
                key=job.get('pending')
                local_preflight=json.loads(billing['body']).get('reason') in {
                    'local_preflight_before_dispatch','schema_rejected_before_generation'}
                if not key:
                    raise Conflict('已明确拒绝的该阶段缺少原阶段身份，不能继续')
                if key in job.get('rejected_resubmissions',{}) and not local_preflight:
                    prior=[row for row in job.get('calls',[]) if row.get('step_key')==key
                           and row.get('status')=='rejected']
                    candidate=(config or {}).get('role_providers',{}).get(call_role,{})
                    last_bill=json.loads(billing['body']) if billing else {}
                    if (len(prior)>=4 or not candidate
                        or (candidate.get('provider')==call.get('channel')
                            and candidate.get('model')==last_bill.get('model'))):
                        raise Conflict('只允许在改用新的已授权通道后继续；该阶段已有多次明确拒单，或新配置仍是相同通道')
                if not local_preflight:
                    job.setdefault('rejected_resubmissions',{})[key]=call['id']
                elif job.get('quality_fallback_of') and call_role in {
                        'writer','term_preparation','style','style_contract_repair','fidelity','line_repair','teaching_review'}:
                    # No bytes left this process, so retry the exact saved step
                    # on its primary channel instead of repeating an input cap.
                    if call_role=='fidelity':
                        if key not in job.setdefault('primary_base_steps',[]):job['primary_base_steps'].append(key)
                        if 'fidelity' not in job.setdefault('primary_base_roles',[]):job['primary_base_roles'].append('fidelity')
                    else:
                        if call_role in {'writer','term_preparation'}:job['writer_use_primary']=True
                        if key not in job.setdefault('primary_continuation_steps',[]):
                            job['primary_continuation_steps'].append(key)
                    if call_role!='fidelity':
                        # Every later request for this role still carries the
                        # same complete skill and source-sized context. A proven
                        # local input rejection therefore applies to the role,
                        # without rejecting one zero-cost request on every round.
                        if call_role not in job.setdefault('primary_continuation_roles',[]):
                            job['primary_continuation_roles'].append(call_role)
                job.pop('pending',None)
            returned_invalid=call.get('status')=='invalid' and call.get('finish_reason') in {'stop','tool_calls'} and bool(call.get('response_blob'))
            if (job.get('stage')=='style' and not job.get('style_parts_enabled') and
                    call.get('role') in {'style__fallback','style_contract_repair__fallback'} and
                    call.get('status')=='truncated' and call.get('finish_reason')=='length' and
                    call.get('response_blob') and billing and billing['actual'] is not None):
                job['style_partition_continuation']={'original_call':call['id'],'original_step':job.get('pending'),
                    'reason':'Complete billed review exceeded output capacity; continue once as disjoint bounded review assignments, preserving all prior charges'}
                job['style_parts_enabled']=True
                job.pop('pending',None);returned_invalid=True
            # A fully received, billed review cut off at its output limit has no
            # delivery uncertainty. The engine permits only its configured one-shot fallback.
            if (call.get('role') in {'style','style_contract_repair','term_preparation','writer','layout_repair','fidelity','teaching_replan'} and
                    call.get('status')=='truncated' and call.get('finish_reason')=='length' and
                    call.get('response_blob') and billing and billing['actual'] is not None):
                key=job.get('pending') or next((k for k,v in job.get('fallbacks',{}).items() if v.get('original_call')==call['id']),None)
                if not key:raise Conflict('截断审核缺少原阶段身份，未重新提交')
                job.setdefault('fallbacks',{})[key]=dict(original_call=call['id'],reason='known_truncated_review')
                job.pop('pending',None)
                returned_invalid=True
            if (call_role=='plan_decision' and call.get('status')=='truncated'
                    and call.get('finish_reason')=='length' and call.get('response_blob')
                    and billing and billing['actual'] is not None):
                job.setdefault('partitioned_plan_decision_continuations',[]).append({
                    'original_call':call['id'],'original_step':job.get('pending'),
                    'reason':'complete adjudication exceeded output capacity; continue as disjoint claim groups'})
                job.pop('pending',None)
                returned_invalid=True
            if (call_role=='active_plan' and call.get('status')=='truncated'
                    and call.get('finish_reason')=='length' and call.get('response_blob')
                    and billing and billing['actual'] is not None):
                key=job.get('pending')
                previous_bill=json.loads(billing['body'])
                replacement=(config or {}).get('role_providers',{}).get('active_plan',{})
                retries=job.setdefault('known_plan_truncation_retries',{})
                if (key and key not in retries and replacement.get('model')
                        and replacement['model']!=previous_bill.get('model')):
                    retries[key]=dict(original_call=call['id'],
                        original_model=previous_bill.get('model'),
                        replacement_model=replacement['model'],
                        reason='received and billed plan exceeded output capacity')
                    job.pop('pending',None)
                    returned_invalid=True
            if (call.get('status') in {'truncated','reasoning_exhausted'} and billing and
                    billing['actual'] is not None and call.get('response_blob')):
                from .providers import reasoning_exhausted
                if reasoning_exhausted(json.loads(self.store.read_blob(call['response_blob']))):
                    call['status']='reasoning_exhausted'
                    job.pop('pending',None)
                    returned_invalid=True
                    if config:
                        from .providers import ReasoningExhausted
                        from .production import (queue_go_pro_reasoning_escalation,
                                                 queue_go_thinking_disabled_retry,
                                                 queue_kuafu_response_recovery)
                        # A previously failed job can be resumed through the
                        # same one-shot KuaFu route selection as a live worker;
                        # no manual job-body edits are needed.
                        job['pending']=call.get('step_key')
                        route=(job.get('transport_recovery_routes') or {}).get(call.get('step_key'))
                        if route and route.get('recovery_protocol_version')=='kuafu-opencode-go-third-v1':
                            queued=queue_go_thinking_disabled_retry(
                                job,config,self.store,dict(billing))
                            if not queued:
                                raise Conflict('Go 的一次性 thinking-disabled 重试不符合完整计费空响应条件，或已使用；旧调用与费用记录已保留')
                        elif route and route.get('recovery_protocol_version')=='kuafu-opencode-go-thinking-disabled-v1':
                            queued=queue_go_pro_reasoning_escalation(
                                job,config,self.store,dict(billing))
                            if not queued:
                                raise Conflict('Go 的一次性 Pro 升级不符合第二次完整计费空响应条件，或已使用；旧调用与费用记录已保留')
                        else:
                            queue_kuafu_response_recovery(
                                job,ReasoningExhausted('已确认备用通道耗尽推理输出'),config)
                        job.pop('pending',None)
            continuation_rows=job.get('unqueryable_router_continuations',[])+job.get('unqueryable_subscription_continuations',[])
            software_resume=(not job.get('pending') and job.get('error')=='PermissionError'
                and any(item.get('original_call')==call.get('id') for item in continuation_rows))
            if not rejected and not no_call_preflight and not software_resume and ((job.get('pending') and not returned_invalid) or (call.get('status') not in {'completed','recovered'} and not returned_invalid)):
                raise Conflict('原调用没有完整结果，不能重新发送')
            p=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if p['active_job'] or p.get('trashed') or p['revision']!=job['base_revision']:
                raise Conflict('原版本已变化或材料不可继续处理')
            job.setdefault('validation_stops',[]).append(dict(error=job.get('error'),at=job.get('finished')))
            if escalation_session:
                marker=dict(version='active-short-rewrite-glossary-kuafu-v1',
                    role='active_write',logical_step=escalation_session,
                    previous_call_id=call.get('id'),fallback_provider_id=call.get('provider_id'),
                    primary_provider_id=primary_id,attempts=0,status='queued',queued_at=time.time(),
                    reason='one primary-provider quality escalation after scoped glossary correction exhaustion')
                job.setdefault('active_glossary_quality_escalations',{})[escalation_session]=marker
                job.setdefault('quality_escalation_history',[]).append(copy.deepcopy(marker))
            if fresh_turn_retry_session:
                fresh_turn_key=(fresh_turn_retry_session+'-turn-'+str(
                    job['active_sessions'][fresh_turn_retry_session]['round']))
                _refresh_short_rewrite_writer_policy(
                    job,fresh_turn_key)
                marker=dict(version='active-short-rewrite-glossary-fresh-turn-v1',
                    role='active_write',logical_step=fresh_turn_retry_session,
                    previous_call_id=call.get('id'),
                    next_step_key=fresh_turn_key,
                    primary_provider_id=call.get('provider_id'),attempts=0,status='queued',
                    queued_at=time.time(),
                    reason='one bounded fresh writer turn after completed primary glossary escalation')
                job.setdefault('active_glossary_quality_retries',{})[
                    fresh_turn_retry_session]=marker
                job.setdefault('quality_escalation_history',[]).append(copy.deepcopy(marker))
            if policy_refresh_session:
                marker=dict(version=EVIDENCE_GAP_POLICY_REFRESH_VERSION,
                    role='active_plan',logical_step=policy_refresh_session,
                    previous_call_id=policy_refresh['previous_call_id'],
                    previous_step_key=policy_refresh['previous_step_key'],
                    next_step_key=policy_refresh['next_step_key'],
                    gap_ids=policy_refresh['gap_ids'],attempts=0,status='queued',
                    queued_at=time.time(),
                    reason='one bounded fresh plan turn after the saved EvidenceGap policy became actionable')
                job.setdefault('active_plan_policy_refreshes',{})[
                    policy_refresh_session]=marker
                job.setdefault('active_policy_refresh_history',[]).append(copy.deepcopy(marker))
            job.update(status='queued',error=None)
            p.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        return job

    def _queue_kuafu_go_third_route(self,jid,config,snapshot,last):
        """Queue one configured OpenCode Go attempt after both KuaFu routes fail."""
        from urllib.parse import urlsplit
        key=snapshot.get('pending')
        if not key or last.get('step_key')!=key:
            return False
        matched=_kuafu_pair_is_failed_without_artifact(snapshot,config,key,self.store.read_blob)
        if not matched:
            return False
        role=matched['role']
        fallback=(config.get('fallback_providers') or {}).get(role)
        if not isinstance(fallback,dict) or fallback.get('enabled') is False:
            return False
        from .providers import apply_route_override
        effective=apply_route_override(config,fallback)
        fallback_id=str(effective.get('provider_id') or '')
        credential=effective.get('api_key') or (config.get('provider_credentials') or {}).get(fallback_id)
        if (effective.get('provider')!='openai-compatible' or not fallback_id or not credential
                or urlsplit(str(effective.get('base_url') or '')).hostname!='opencode.ai'
                or effective.get('protocol','chat_completions') not in {'chat_completions','responses'}
                or not effective.get('model')):
            return False
        step_calls=[item for item in snapshot.get('calls',[]) if item.get('step_key')==key]
        if any(item.get('provider_id')==fallback_id for item in step_calls):
            return False
        old=(snapshot.get('transport_recovery_routes') or {}).get(key)
        if old and (old.get('status')!='failed'
                    or old.get('recovery_protocol_version')=='kuafu-opencode-go-third-v1'):
            return False
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:
                return False
            job=json.loads(row[0])
            call=(job.get('calls') or [{}])[-1]
            project_row=cx.execute('SELECT body FROM projects WHERE id=?',(job.get('project'),)).fetchone()
            if not project_row:
                return False
            project=json.loads(project_row[0])
            if (job.get('status') not in {'uncertain','failed'}
                    or job.get('pipeline')!='active_composition_v2'
                    or job.get('pending')!=key or call.get('id')!=last.get('id')
                    or project.get('active_job') or project.get('trashed')
                    or project.get('revision')!=job.get('base_revision')
                    or _kuafu_pair_is_failed_without_artifact(job,config,key,self.store.read_blob)!=matched):
                return False
            step_calls=[item for item in job.get('calls',[]) if item.get('step_key')==key]
            if any(item.get('provider_id')==fallback_id for item in step_calls):
                return False
            current=(job.get('transport_recovery_routes') or {}).get(key)
            if current and (current.get('status')!='failed'
                    or current.get('recovery_protocol_version')=='kuafu-opencode-go-third-v1'):
                return False
            if current:
                job.setdefault('transport_recovery_history',{}).setdefault(key,[]).append(copy.deepcopy(current))
            state=dict(status='queued',provider_id=fallback_id,
                protocol=effective.get('protocol','chat_completions'),
                original_provider_id=matched['primary_id'],
                original_call_ids=list(matched['call_ids']),
                unknown_call_ids=list(matched['unknown_call_ids']),
                original_provider_ids=[matched['primary_id'],matched['backup_id']],
                attempts=0,recovery_protocol_version='kuafu-opencode-go-third-v1',
                reason='both configured reciprocal KuaFu routes failed on this exact active-v2 step with no complete artifact; one OpenCode Go fallback is queued')
            job.setdefault('transport_recovery_routes',{})[key]=state
            job.setdefault('internal_recoveries',[]).append(dict(stage=role,step=key,
                type='kuafu_opencode_go_third_route',provider_id=fallback_id,
                original_call_ids=list(matched['call_ids']),unknown_call_ids=list(matched['unknown_call_ids']),
                at=time.time()))
            job.pop('pending',None)
            job.update(status='queued',error=None)
            job.pop('error_type',None);job.pop('finished',None)
            project.update(active_job=jid,state='queued')
            cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',
                ('queued',json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0,cancel_requested=0 WHERE id=?",
                (jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',
                (json.dumps(project,ensure_ascii=False),project['id']))
        return True

    def continue_failed_active_plan_context(self,jid):
        """Continue one exhausted KuaFu active-plan failure on the same job.

        The previous responses and every charge remain in the job and ledger.
        A three-dispatch KuaFu failure gets one structural split of the current
        source group; older saved failures retain the existing context retry.
        """
        from urllib.parse import urlsplit

        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0])
            if (job.get('role')!='production' or job.get('pipeline')!='active_composition_v2'
                    or job.get('status') not in {'failed','uncertain'} or job.get('stage')!='active_plan'):
                raise Conflict('Continuation requires a terminal failed or uncertain active-plan job')
            call=(job.get('calls') or [{}])[-1]
            if job.get('pending') not in (None,call.get('step_key')):
                raise Conflict('Pending step does not match the saved failed call')
            split_kind=_exhausted_active_plan_triplet(job,call,self.store.read_blob)
            unknown_sse=split_kind=='unknown_sse'
            valid_failed_artifact=(call.get('protocol')=='responses' and call.get('status')=='invalid'
                and call.get('finish_reason')=='failed' and call.get('http_status') in (None,200)
                and call.get('dispatch_started') is True and bool(call.get('response_blob'))
                and str(call.get('role') or '').removesuffix('__fallback')=='active_plan'
                and urlsplit(str(call.get('upstream_base') or '')).hostname=='api.kuafushe.cc')
            if not (unknown_sse or valid_failed_artifact):
                raise Conflict('Last call is not a saved KuaFu Responses upstream failure')
            if not unknown_sse:
                try:
                    response=json.loads(self.store.read_blob(call['response_blob']))
                except (OSError,ValueError,TypeError):
                    raise Conflict('Saved Responses failure receipt cannot be read') from None
                if not isinstance(response,dict):
                    raise Conflict('Saved Responses failure receipt has an invalid structure')
                error=response.get('error') if isinstance(response,dict) else None
                error_code=(error.get('code') or error.get('type')) if isinstance(error,dict) else ''
                if response.get('status')!='failed' or error_code!='upstream_error':
                    raise Conflict('Saved Responses receipt is not an upstream_error')
            control=cx.execute('SELECT status,owner,lease_until FROM production_control WHERE id=?',
                               (jid,)).fetchone()
            project_row=cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()
            if not control or not project_row:
                raise Conflict('Job or project execution record is missing')
            project=json.loads(project_row[0])
            if (control['status'] not in {'failed','uncertain'} or control['owner'] is not None
                    or project.get('active_job') or project.get('trashed')
                    or project.get('revision')!=job.get('base_revision')
                    or project.get('inventory',{}).get('digest')!=job.get('source_digest')):
                raise Conflict('Project version, ownership, or source inventory changed')
            source=job.get('source') or {}
            original=project.get('inventory') or {}
            saved_objects=source.get('objects') or []
            original_objects=original.get('objects') or []
            immutable_fields=('id','locator','resource_id','target','raw')
            if (job.get('source_snapshot_digest')!=digest(original)
                    or source.get('source_url')!=original.get('source_url')
                    or source.get('originals')!=original.get('originals')
                    or source.get('resources')!=original.get('resources')
                    or len(saved_objects)!=len(original_objects)
                    or any(any(saved.get(field)!=base.get(field) for field in immutable_fields)
                           for saved,base in zip(saved_objects,original_objects))):
                # Vision and inert-markup classification legitimately change
                # derived text/kind while the uploaded bytes and addresses stay
                # fixed. The source snapshot and immutable object identities
                # protect this continuation without rejecting those changes.
                raise Conflict('Saved source no longer matches the current project inventory')
            group_index=int(job.get('active_partition_index',0))
            groups=job.get('active_groups') or []
            plans=job.get('active_plans') or []
            if (group_index<0 or group_index>=len(groups) or len(plans)!=group_index
                    or not groups[group_index]):
                raise Conflict('Failed partition does not follow all prior validated plans')
            from .visual_sources import decorative_resource
            expected=[obj['id'] for obj in source.get('objects',[])
                      if not decorative_resource(obj)]
            assigned=[sid for group in groups for sid in group]
            if (assigned!=expected or len(assigned)!=len(set(assigned))):
                raise Conflict('Saved partitions do not exactly cover source objects in order')
            policy=job.get('role_policy') or {}
            if not policy or digest(policy)!=job.get('role_policy_digest'):
                raise Conflict('Frozen role policy digest is invalid')
            skill=job.get('writing_skill') or {}
            if not skill.get('root') or not skill.get('package_digest'):
                raise Conflict('Frozen complete writing-skill package is unavailable')
            from .skills import load_bundle
            try:
                load_bundle(skill['root'],skill['package_digest'])
            except (OSError,ValueError,KeyError):
                raise Conflict('Frozen writing-skill package digest is invalid') from None
            prefixes=list(job.get('active_group_prefixes') or
                          ['p'+str(index+1) for index in range(len(groups))])
            if len(prefixes)!=len(groups):
                raise Conflict('Saved partition prefix count does not match partition count')
            old_prefix=prefixes[group_index]
            old_step_prefix='active-plan-'+old_prefix+'-turn-'
            if not str(call.get('step_key','')).startswith(old_step_prefix):
                raise Conflict('Failed call does not belong to the current planning partition')
            old_session_key='active-plan-'+old_prefix
            old_session=(job.get('active_sessions') or {}).get(old_session_key)
            if not isinstance(old_session,dict) or old_session.get('complete'):
                raise Conflict('Failed partition has no resumable resource and research checkpoint')

            # A bounded split is allowed only for the known two-route plus one
            # Responses-SSE history.  The SSE can end in an upstream_error
            # artifact or have unknown delivery after the worker loses its
            # response; neither case is replayed.  Any other history keeps the
            # legacy context continuation behavior below.
            step_calls=[item for item in job.get('calls',[])
                if item.get('step_key')==call.get('step_key')
                and item.get('dispatch_started') is True
                and urlsplit(str(item.get('upstream_base') or '')).hostname=='api.kuafushe.cc']
            split_recovery=_exhausted_active_plan_triplet(job,call,self.store.read_blob) in {
                'unknown_sse','failed_sse'}
            recoveries=job.get('active_plan_context_recoveries',[])
            if (not split_recovery and
                    any(int(item.get('partition_index',-1))==group_index for item in recoveries)):
                raise Conflict('The one-time context recovery was already used for this partition')
            if split_recovery:
                if job.get('active_plan_split_recoveries'):
                    raise Conflict('当前任务的一次性规划分组拆分恢复已经使用')
                split_groups=_split_active_plan_group_once(source,groups[group_index])
                if not split_groups:
                    raise Conflict('当前规划分组没有安全的原对象边界可供一次性拆分；原任务与费用记录保持不变')
                recovery_prefixes=[old_prefix+'-split1a',old_prefix+'-split1b']
                recovery_sessions=['active-plan-'+prefix for prefix in recovery_prefixes]
                if (any(prefix in prefixes for prefix in recovery_prefixes)
                        or any(key in job.get('active_sessions',{}) for key in recovery_sessions)):
                    raise Conflict('版本化分组拆分身份已存在')
                now=time.time()
                receipt=dict(version='active-plan-single-split-v1',partition_index=group_index,
                    original_prefix=old_prefix,recovery_prefixes=recovery_prefixes,
                    original_source_ids=list(groups[group_index]),
                    split_source_ids=[list(group) for group in split_groups],
                    previous_call_ids=[item['id'] for item in step_calls],
                    previous_step_key=call['step_key'],prior_session_digest=digest(old_session),
                    reused_validated_partition_count=group_index,status='queued',queued_at=now,
                    reason=('one same-job source-object split after two saved KuaFu 52x responses and one Responses SSE with unknown delivery'
                            if unknown_sse else
                            'one same-job source-object split after two saved KuaFu 52x responses and one failed Responses SSE; all prior source, plan and call records remain immutable'))
                if unknown_sse:
                    route=(job.get('transport_recovery_routes') or {}).get(call.get('step_key'))
                    if route and route.get('status') in {'queued','dispatching'}:
                        route.update(status='failed',error='unknown_delivery_superseded_by_bounded_source_split',
                                     finished_at=now)
                groups[group_index:group_index+1]=split_groups
                prefixes[group_index:group_index+1]=recovery_prefixes
                job['active_groups']=groups
                job['active_group_prefixes']=prefixes
                job['active_partition_count']=len(groups)
                job.setdefault('active_plan_split_recoveries',[]).append(receipt)
                job.setdefault('validation_stops',[]).append(dict(
                    error=job.get('error'),at=job.get('finished'),
                    recovery='one_same_job_active_plan_source_group_split'))
                job.update(status='queued',error=None)
                job.pop('pending',None)
                job.pop('error_type',None);job.pop('finished',None)
                project.update(active_job=jid,state='queued')
                cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',
                           ('queued',json.dumps(job,ensure_ascii=False),jid))
                cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0,cancel_requested=0 WHERE id=?",
                           (jid,))
                cx.execute('UPDATE projects SET body=? WHERE id=?',
                           (json.dumps(project,ensure_ascii=False),project['id']))
                return job

            recovery_prefix=old_prefix+'-ctx1'
            new_session_key='active-plan-'+recovery_prefix
            if (recovery_prefix in prefixes or new_session_key in job.get('active_sessions',{})):
                raise Conflict('Versioned recovery identity already exists')

            now=time.time()
            receipt=dict(version='active-plan-historical-catalog-compaction-v1',
                partition_index=group_index,original_prefix=old_prefix,
                recovery_prefix=recovery_prefix,source_ids=list(groups[group_index]),
                previous_call_id=call['id'],previous_step_key=call['step_key'],
                reused_validated_partition_count=group_index,
                preserved_unknown_dispatches=sum(
                    1 for item in job.get('calls',[]) if item.get('status')=='uncertain'
                    and item.get('dispatch_started')),
                status='queued',queued_at=now,
                reason='One explicit same-job continuation after a saved upstream_error; active-plan prompt omits image_refs for unopened archived external resources')
            prefixes[group_index]=recovery_prefix
            job['active_group_prefixes']=prefixes
            job.setdefault('active_sessions',{})[new_session_key]=copy.deepcopy(old_session)
            job.setdefault('active_plan_context_recoveries',[]).append(receipt)
            job.setdefault('validation_stops',[]).append(dict(
                error=job.get('error'),at=job.get('finished'),
                recovery='one_explicit_versioned_active_plan_context_continuation'))
            job.update(status='queued',error=None)
            job.pop('pending',None)
            job.pop('error_type',None);job.pop('finished',None)
            project.update(active_job=jid,state='queued')
            cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',
                       ('queued',json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0,cancel_requested=0 WHERE id=?",
                       (jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',
                       (json.dumps(project,ensure_ascii=False),project['id']))
        return job

    def _queue_saved_responses_artifact(self,jid,snapshot,last):
        """Resume a received Responses artifact without sending the source again."""
        key=snapshot.get('pending')
        if (snapshot.get('status') not in {'uncertain','failed'} or snapshot.get('pipeline')!='active_composition_v2'
                or not key or last.get('step_key')!=key or last.get('channel')!='openai-compatible'
                or last.get('protocol')!='responses' or last.get('status') not in {'completed','recovered'}
                or not last.get('response_blob') or last.get('http_status',0)>=400):
            return False
        try:
            from .providers import _responses_text
            body=json.loads(self.store.read_blob(last['response_blob']))
            if body.get('status') not in {'completed','complete','succeeded'} or not _responses_text(body):
                return False
        except (ValueError,TypeError,KeyError):
            return False
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:return False
            job=json.loads(row[0]);call=(job.get('calls') or [{}])[-1]
            project_row=cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()
            if not project_row:return False
            project=json.loads(project_row[0])
            if (job.get('status') not in {'uncertain','failed'} or job.get('pending')!=key or call.get('id')!=last.get('id')
                    or call.get('protocol')!='responses' or call.get('status') not in {'completed','recovered'}
                    or project.get('active_job') or project.get('trashed')
                    or project.get('revision')!=job.get('base_revision')):
                return False
            # If an earlier local repair checkpoint already captured the text,
            # re-enter that repair directly; otherwise keep pending so Provider
            # can read the saved response blob and extract its text.
            if key in job.get('active_invalid_json',{}):
                job.pop('pending',None)
            job.setdefault('internal_recoveries',[]).append(dict(stage=job.get('stage'),step=key,
                type='resume_saved_responses_artifact',original_call_id=call['id'],at=time.time()))
            job.update(status='queued',error=None);job.pop('error_type',None)
            project.update(active_job=jid,state='queued')
            cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',
                ('queued',json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0,cancel_requested=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(project,ensure_ascii=False),project['id']))
        return True

    def continue_interrupted_kuafu(self, jid, config):
        """Resume one uncertain KuaFu v2 step on its configured reciprocal route.

        The original request remains unknown, its cost is preserved as unknown,
        and the existing saved plan/writer checkpoints are not reset.
        """
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0]);call=(job.get('calls') or [{}])[-1]
            if (job.get('role')!='production' or job.get('pipeline')!='active_composition_v2'
                    or job.get('status')!='uncertain' or not _kuafu_unanswered_call(call,job.get('pending'))
                    or job.get('pending') in job.get('transport_recovery_routes',{})):
                raise Conflict('当前任务没有可续接的 KuaFu 未知调用')
            project=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if (project.get('active_job') or project.get('trashed')
                    or project['revision']!=job['base_revision']):
                raise Conflict('原材料已变化或仍在处理中，未建立续接')
            _mark_call_unknown(cx,job,call,
                'existing uncertain KuaFu call resumed through the configured reciprocal route; original usage remains unknown')
            key=job['pending']
            state=_queue_reciprocal_kuafu_route(job,call,config,
                'existing KuaFu call delivery is unknown; configured reciprocal route queued once')
            if not state or state.get('status')!='queued':
                job.update(status='uncertain',error='配置的夸父社互备线路不可用或该步骤已尝试，原调用费用仍为未知')
                cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',
                    ('uncertain',json.dumps(job,ensure_ascii=False),jid))
                cx.execute("UPDATE production_control SET status='uncertain',owner=NULL,lease_until=0 WHERE id=?",(jid,))
                return {'status':'uncertain','queued':False,'original_call_preserved':call['id']}
            job.update(status='queued',error=None)
            job.pop('error_type',None)
            project.update(active_job=jid,state='queued')
            cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',
                ('queued',json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0,cancel_requested=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(project,ensure_ascii=False),project['id']))
        return {'status':'queued','queued':True,'original_call_preserved':call['id'],
                'recovery_provider_id':state['provider_id'],'step':key}

    def continue_active_patch_unknown_sse(self, jid):
        """Queue one new active-patch session after an expired, artifact-free KuaFu SSE.

        The original step and its unknown spend remain intact. The new session
        key is versioned and distinct; project/source/skill snapshots are
        checked before the worker can submit it.
        """
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0]);call=(job.get('calls') or [{}])[-1]
            pending=job.get('pending')
            if (job.get('role')!='production' or job.get('pipeline')!='active_composition_v2'
                    or job.get('status')!='uncertain' or job.get('stage')!='active_revision'
                    or not _active_patch_unknown_sse_call(call,pending)):
                raise Conflict('当前任务没有满足条件的 active_patch 未知 SSE 调用')
            project_row=cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()
            if not project_row:raise Conflict('项目执行记录不存在')
            project=json.loads(project_row[0]);inventory=project.get('inventory') or {}
            if (project.get('active_job') or project.get('trashed')
                    or project.get('revision')!=job.get('base_revision')
                    or inventory.get('digest')!=job.get('source_digest')
                    or job.get('source_snapshot_digest')!=digest(inventory)):
                raise Conflict('项目版本或冻结原始材料已变化，未建立续接')
            source=job.get('source') or {}
            if (source.get('source_url')!=inventory.get('source_url')
                    or source.get('originals')!=inventory.get('originals')
                    or source.get('resources')!=inventory.get('resources')
                    or len(source.get('objects') or [])!=len(inventory.get('objects') or [])):
                raise Conflict('保存的来源快照与项目原始材料不一致')
            immutable=('id','locator','resource_id','target','raw')
            if any(any(saved.get(field)!=original.get(field) for field in immutable)
                   for saved,original in zip(source['objects'],inventory['objects'])):
                raise Conflict('保存的来源对象身份与冻结原始材料不一致')
            policy=job.get('role_policy') or {}
            if (not policy or digest(policy)!=job.get('role_policy_digest')
                    or 'active_patch' not in policy):
                raise Conflict('冻结的 active_patch 角色策略校验失败')
            skill=job.get('writing_skill') or {}
            if not skill.get('root') or not skill.get('package_digest'):
                raise Conflict('冻结的完整写作技能包不可用')
            from .skills import load_bundle
            try:load_bundle(skill['root'],skill['package_digest'])
            except (OSError,ValueError,KeyError):
                raise Conflict('冻结的完整写作技能包校验失败') from None
            nodes=job.get('writing_batches') or [n for part in job.get('active_plans',[])
                                                  for n in part.get('nodes',[])]
            index=int(job.get('unit_index',0));candidate=job.get('active_candidate') or {}
            if index<0 or index>=len(nodes) or not candidate.get('draft'):
                raise Conflict('当前局部修订单元或原稿不存在')
            from .active_composition import (claim_unknown_patch_continuation,
                patchable_block_ids,retarget_protected_findings)
            from .active_composition import writing_batch_contract
            node=nodes[index];attempt=len(candidate.get('patch_history',[]))
            contract=writing_batch_contract(job['active_plans'][0]['contract'],node,job.get('goal',''))
            base='active-patch-'+node['id']+'-'+str(attempt)+'-'+digest([
                canonical(candidate['draft']),contract])[:16]
            if not pending.startswith(base+'-turn-'):
                raise Conflict('未知调用不属于当前冻结的 active_patch 修订步骤')
            retarget_protected_findings(candidate.setdefault('revision_issues',{}),candidate['draft'])
            candidate['revision_blocks']=list(dict.fromkeys(
                finding['block_id'] for finding in candidate['revision_issues'].get('findings',[])))
            if not set(candidate['revision_blocks']) & patchable_block_ids(candidate['draft']):
                raise Conflict('受保护材料附近没有可定位的作者解释段落，不能安全续接')
            suffix=digest([pending,call.get('id'),candidate['draft'],candidate['revision_issues']])[:12]
            continuation_session=base+'-unknown-sse-v1-'+suffix
            marker=claim_unknown_patch_continuation(job,pending,continuation_session)
            if not marker:
                raise Conflict('该未知 active_patch 调用不满足唯一续接条件，或续接已使用')
            now=time.time()
            marker['queued_at']=now
            job.setdefault('internal_recoveries',[]).append(dict(
                stage='active_revision',step=pending,type='active_patch_unknown_sse_continuation',
                original_call_id=call['id'],continuation_session=continuation_session,
                original_result='unknown_not_replayed',at=now))
            job.update(status='queued',error=None);job.pop('error_type',None);job.pop('finished',None)
            project.update(active_job=jid,state='queued')
            cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',
                ('queued',json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0,cancel_requested=0 WHERE id=?",
                (jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',
                (json.dumps(project,ensure_ascii=False),project['id']))
        return {'status':'queued','queued':True,'original_call_preserved':call['id'],
                'continuation_session':continuation_session,'original_result':'unknown_not_replayed'}

    def _queue_kuafu_responses_stream_retry(self,jid,config,snapshot,last):
        """Queue exactly one code-versioned SSE retry after a saved KuaFu 52x page.

        The gateway page is retained as evidence; it is not fed to the model as output.
        The retry uses the same explicitly configured Responses route with streaming enabled.
        """
        if not _kuafu_responses_gateway_error(last,snapshot.get('pending')):
            return False
        key=snapshot['pending']
        routes=config.get('provider_routes') if isinstance(config,dict) else None
        credentials=config.get('provider_credentials') if isinstance(config,dict) else None
        if not isinstance(routes,dict) or not isinstance(credentials,dict):return False
        route=routes.get(last.get('provider_id'))
        if (not isinstance(route,dict) or route.get('provider')!='openai-compatible'
                or route.get('enabled') is False or route.get('protocol')!='responses'
                or not credentials.get(last.get('provider_id'))):return False
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:return False
            job=json.loads(row[0]);call=(job.get('calls') or [{}])[-1]
            markers=job.setdefault('kuafu_responses_protocol_retries',{})
            project_row=cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()
            if not project_row:return False
            project=json.loads(project_row[0])
            if (job.get('status')!='uncertain' or job.get('pending')!=key
                    or call.get('id')!=last.get('id') or not _kuafu_responses_gateway_error(call,key)
                    or key in markers or project.get('active_job') or project.get('trashed')
                    or project.get('revision')!=job.get('base_revision')):
                return False
            old=job.setdefault('transport_recovery_routes',{}).get(key)
            if not isinstance(old,dict) or old.get('status')!='failed' or old.get('provider_id')!=call.get('provider_id'):
                return False
            # Preserve the failed non-stream attempt before installing a distinct one-shot protocol retry.
            job.setdefault('transport_recovery_history',{}).setdefault(key,[]).append(dict(old))
            state=dict(status='queued',provider_id=call['provider_id'],protocol='responses',
                original_call_id=call['id'],original_provider_id=call['provider_id'],attempts=0,
                reason='saved KuaFu 524 gateway error had no model artifact; one versioned Responses SSE retry is queued',
                recovery_protocol_version='kuafu-responses-sse-v1',streaming=True)
            job['transport_recovery_routes'][key]=state
            markers[key]=dict(fix_id='kuafu-responses-sse-v1',previous_call_id=call['id'],queued_at=time.time())
            job.setdefault('internal_recoveries',[]).append(dict(stage=job.get('stage'),step=key,
                type='kuafu_responses_stream_retry',original_call_id=call['id'],
                provider_id=call['provider_id'],http_status=call.get('http_status'),at=time.time()))
            # Keep saved plan/results intact, but clear the in-flight marker so this
            # exact stage is dispatched through the versioned route on worker resume.
            job.pop('pending',None)
            job.update(status='queued',error=None);job.pop('error_type',None)
            project.update(active_job=jid,state='queued')
            cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',
                ('queued',json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0,cancel_requested=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(project,ensure_ascii=False),project['id']))
        return True

    def _mark_interrupted_subscription_uncertain(self,jid,call_id):
        """Record a killed synchronous request as unknown only after its deadline."""
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0]);call=(job.get('calls') or [{}])[-1]
            project=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if (job.get('status')!='failed' or call.get('id')!=call_id
                    or job.get('pending')!=call.get('step_key') or call.get('status')!='submitted'
                    or not call.get('dispatch_started') or call.get('response_blob') or call.get('http_status')
                    or call.get('deadline_at',0)>time.time() or project.get('active_job')
                    or project.get('trashed') or project['revision']!=job['base_revision']):
                raise Conflict('当前没有已过期且响应未知的同步请求')
            call['status']='uncertain'
            job.update(status='uncertain',error='后台进程中断，原同步请求在截止时间前没有留下可查询响应')
            spending=cx.execute('SELECT body FROM spending WHERE id=?',(call_id,)).fetchone()
            if spending:
                body=json.loads(spending['body'])|{'status':'unknown','reason':'worker_interrupted_after_dispatch'}
                cx.execute('UPDATE spending SET actual=NULL,status=?,body=? WHERE id=?',
                    ('unknown',json.dumps(body,ensure_ascii=False),call_id))
            cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',
                ('uncertain',json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='uncertain',owner=NULL,lease_until=0 WHERE id=?",(jid,))

    def continue_inventory_patch(self, jid):
        """Explicitly continue with a different small patch task; never replay an unknown request."""
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0]);call=(job.get('calls') or [{}])[-1]
            if (job.get('role')!='production' or job.get('status')!='uncertain'
                    or job.get('stage')!='inventory_audit' or job.get('inventory_patch_continuation')
                    or call.get('role') not in {'inventory_repair','inventory_repair__fallback'}
                    or call.get('status')!='uncertain' or call.get('step_key')!=job.get('pending')
                    or len(job.get('facts',{}).get('facts',[]))<=80
                    or not any(k.startswith('inventory_audit') for k in job.get('results',{}))):
                raise Conflict('当前没有可改用局部清单补丁的长文任务')
            p=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if p.get('active_job') or p.get('trashed') or p['revision']!=job['base_revision'] or p['inventory']['digest']!=job['source_digest']:
                raise Conflict('原件版本已变化，不能接续原清单')
            job['inventory_patch_continuation']=dict(original_call=call['id'],original_step=job['pending'],
                reason='Explicit continuation with a different bounded delta task after full-inventory repair transport failure; unknown delivery and reserved cost remain recorded')
            job.pop('pending');job.update(status='queued',error=None)
            p.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        return job

    def retry_visual_details(self, jid):
        """One explicit detailed-image retry of a completely received failed visual review."""
        job=self.store.job(jid)
        if (job.get('status')!='failed' or job.get('stage')!='visual_audit'
                or job.get('visual_detail_retry') or job.get('pending')):
            raise Conflict('当前没有可补充图片细节的已返回视觉审核')
        reviews=[(k,v) for k,v in job['results'].items() if k.startswith('visual_audit') and k.endswith('recheck')]
        if not reviews:raise Conflict('缺少已返回的原始图片审核')
        key,result=reviews[-1]
        ids=[p['source_id'] for p in result['pages'] if any(c['status'] not in {'preserved','not_applicable'} for c in p.get('checks',[]))]
        if not ids:raise Conflict('没有定位到需要补充细节的原件')
        # Set the retry scope before retry_validation releases the queue lease.
        job.update(visual_detail_ids=ids,visual_detail_retry={'review':key,'source_ids':ids})
        self.store.put_job(job)
        return self.retry_validation(jid)

    def recheck_inventory(self,jid):
        """One explicit review after a diagnosed reviewer/configuration correction."""
        job=self.store.job(jid)
        if (job.get('status')!='failed' or job.get('stage')!='inventory_audit'
                or job.get('inventory_review_recheck') or job.get('pending')):
            raise Conflict('当前没有可重新核对的已返回清单审核')
        round=job.get('inventory_semantic_repairs',0)
        job['inventory_review_recheck']={'round':round,'reason':'Explicit bounded recheck after correcting review interpretation; all prior facts, reviews and spending remain retained'}
        job['inventory_semantic_limit']=round+1
        self.store.put_job(job)
        return self.retry_validation(jid)

    def recheck_plan_review(self, jid):
        """Re-evaluate a saved repair plan after fixing old-prose review confusion."""
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0])
            old_key=f"teaching-replan-review-{job.get('repair_rounds')}"
            if (job.get('role')!='production' or job.get('status')!='needs_attention' or
                    job.get('stage')!='teaching_replan_review' or old_key not in job.get('results',{}) or
                    job.get('pending')):
                raise Conflict('当前没有可按新规则重新核对的已保存修复规划')
            if job.get('plan_review_rechecks'):
                result=job['results'].get(old_key+'-route-v2',{})
                decision_key=old_key.replace('review','decision')
                if job.get('plan_claim_recheck'):
                    decisions=job['results'].get(decision_key,{}).get('decisions',[])
                    if (job.get('plan_original_recheck') or result.get('status')!='ready' or
                            not decisions or not any(d['verdict']=='unknown' for d in decisions) or
                            any(d['verdict']=='confirmed_error' for d in decisions) or
                            f"teaching-replan-original-decision-{job['repair_rounds']}" in job['results']):
                        raise Conflict('当前没有可对原件字符重新裁决的未知项')
                    job['plan_original_recheck']={'prior_decision':decision_key,
                                                 'unknown_indices':[d['claim_index'] for d in decisions if d['verdict']=='unknown']}
                else:
                    if (result.get('status')!='ready' or not result.get('issues') or decision_key in job['results']):
                        raise Conflict('当前没有可逐条裁决的正面审核记录')
                    job['plan_claim_recheck']={'old_key':old_key+'-route-v2','old_issues':result['issues']}
            row=cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()
            if not row:raise KeyError(job['project'])
            project=json.loads(row[0])
            if project.get('active_job') or project.get('trashed') or project['goal']!=job.get('project_goal',job['goal']):
                raise Conflict('材料已变化，不能重新核对原规划')
            if project['revision']!=job['base_revision']:
                receipt=project.get('production') or {}
                if (project['revision']!=job['base_revision']+1 or receipt.get('job')!=jid or
                        receipt.get('status')!='needs_attention' or receipt.get('revision')!=project['revision'] or
                        project.get('draft')!=job.get('draft') or
                        project.get('inventory',{}).get('digest')!=job.get('inventory',{}).get('digest')):
                    raise Conflict('已保存候选或原件发生变化，不能继续原任务')
                continuation=dict(revision=job['base_revision'],candidate_revision=project['revision'],
                                  original_source_digest=job['source_digest'])
                job.setdefault('continued_candidate_revisions',[]).append(continuation)
                job['continued_from_saved_candidate']=continuation
                job['base_revision']=project['revision']
                job['source_digest']=project['inventory']['digest']
            elif project.get('inventory',{}).get('digest')!=job['source_digest']:
                raise Conflict('原件已变化，不能重新核对原规划')
            if not job.get('plan_review_rechecks'):
                job['plan_review_rechecks']=[{'old_key':old_key,'old_issues':job.get('quality_issues',[])}]
            job['status']='queued'
            project.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(project,ensure_ascii=False),project['id']))
        return job

    def recheck_teaching_review(self, jid, fresh=False):
        """Recheck a saved review, or request one fresh bounded review for invalid evidence."""
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0])
            key=f"teaching-contract-{job.get('content_generation',0)}-{job.get('repair_rounds',0)}"
            if (job.get('role')!='production' or job.get('status')!='needs_attention' or
                    job.get('stage')!='teaching' or key not in job.get('results',{}) or
                    job.get('pending') or job.get('teaching_contract_rechecks') or
                    not job.get('quality_issues') or
                    not all(issue.startswith('教学审查证据仍无法核对：') for issue in job['quality_issues'])):
                raise Conflict('当前没有可按新证据规则复核的完整教学审核')
            row=cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()
            if not row:raise KeyError(job['project'])
            project=json.loads(row[0])
            if project.get('active_job') or project.get('trashed') or project['goal']!=job.get('project_goal',job['goal']):
                raise Conflict('材料已变化，不能重新核对原审核')
            if project['revision']!=job['base_revision']:
                receipt=project.get('production') or {}
                if (project['revision']!=job['base_revision']+1 or receipt.get('job')!=jid or
                        receipt.get('status')!='needs_attention' or receipt.get('revision')!=project['revision'] or
                        project.get('draft')!=job.get('draft') or
                        project.get('inventory',{}).get('digest')!=job.get('inventory',{}).get('digest')):
                    raise Conflict('已保存候选或原件发生变化，不能继续原任务')
                continuation=dict(revision=job['base_revision'],candidate_revision=project['revision'],
                                  original_source_digest=job['source_digest'])
                job.setdefault('continued_candidate_revisions',[]).append(continuation)
                job['continued_from_saved_candidate']=continuation
                job['base_revision']=project['revision']
                job['source_digest']=project['inventory']['digest']
            elif project.get('inventory',{}).get('digest')!=job['source_digest']:
                raise Conflict('原件已变化，不能重新核对原审核')
            job['teaching_contract_rechecks']=[{'saved_key':key,'old_issues':job['quality_issues'],
                                                'fresh':fresh}]
            if fresh:
                job['teaching_evidence_retry']=1
                job.pop('use_saved_teaching_contract',None)
            else:
                job['use_saved_teaching_contract']=True
            job['status']='queued'
            job['quality_issues']=[]
            project.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(project,ensure_ascii=False),project['id']))
        return job

    def repair_teaching_replan(self, jid, config=None):
        """Continue a rejected repair plan within the configured bounded limit."""
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0])
            attempts=int(job.get('teaching_replan_attempts',0))
            limit=max(1,min(6,int((config or {}).get('max_plan_repairs',2))))
            suffix=f"-repair-{attempts}" if attempts else ''
            key=f"teaching-replan-review-{job.get('repair_rounds')}-route-v2{suffix}"
            if (job.get('role')!='production' or job.get('status')!='needs_attention' or
                    job.get('stage')!='teaching_replan_review' or
                    not job.get('regeneration') or not job.get('quality_issues') or
                    attempts>=limit or key not in job.get('results',{}) or
                    job.get('pending')):
                raise Conflict('当前没有可按独立意见再次修正的教学规划')
            row=cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()
            if not row:raise KeyError(job['project'])
            project=json.loads(row[0])
            if project.get('active_job') or project.get('trashed') or project['goal']!=job.get('project_goal',job['goal']):
                raise Conflict('材料已变化，不能继续原规划')
            if project['revision']!=job['base_revision']:
                receipt=project.get('production') or {}
                if (project['revision']!=job['base_revision']+1 or receipt.get('job')!=jid or
                        receipt.get('status')!='needs_attention' or
                        receipt.get('revision')!=project['revision'] or
                        project.get('draft')!=job.get('draft') or
                        project.get('inventory',{}).get('digest')!=job.get('inventory',{}).get('digest')):
                    raise Conflict('已保存候选或原件发生变化，不能继续原任务')
                job['base_revision']=project['revision']
                job['source_digest']=project['inventory']['digest']
            elif project.get('inventory',{}).get('digest')!=job['source_digest']:
                raise Conflict('原件已变化，不能继续原规划')
            job['teaching_replan_attempts']=attempts+1
            job['teaching_replan_repair_issues']=copy.deepcopy(job['quality_issues'])
            job.setdefault('teaching_replan_repair_history',[]).append({
                'attempt':attempts+1,'review':key,'issues':copy.deepcopy(job['quality_issues'])})
            job.update(status='queued',stage='teaching_replan_repair',quality_issues=[])
            project.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(project,ensure_ascii=False),project['id']))
        return job

    def retry_truncated_inventory(self, jid, new_output_limit=None):
        """One revised-cap or partitioned continuation after a billed truncation."""
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0])
            call=job['calls'][-1] if job.get('calls') else {}
            if (job.get('role')!='production' or job.get('status')!='failed' or
                    job.get('stage')!='inventory' or job.get('pending')!='inventory' or
                    job.get('inventory_cap_retry') or job.get('inventory_partition_retry') or call.get('status')!='truncated' or
                    call.get('finish_reason')!='length' or not call.get('wire_request_blob')):
                raise Conflict('当前没有可按增大输出上限重试的已知截断清单')
            previous=json.loads(self.store.read_blob(call['wire_request_blob']))['max_tokens']
            if new_output_limit is None:
                from .source_context import inventory_groups
                objects=job['source']['objects']
                if len(inventory_groups(objects))<2 or sum(len(o.get('text','')) for o in objects)<=12000:
                    raise Conflict('当前原件不满足按完整对象分组清点的条件')
            elif new_output_limit<=previous:raise Conflict('新的输出上限必须高于上次截断上限')
            paid=cx.execute('SELECT actual FROM spending WHERE id=?',(call['id'],)).fetchone()
            if not paid or paid['actual'] is None:raise Conflict('上次用量尚未结算，不能重新提交')
            row=cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()
            if not row:raise KeyError(job['project'])
            project=json.loads(row[0])
            if project.get('active_job') or project.get('trashed') or project['revision']!=job['base_revision']:
                raise Conflict('材料已变化，不能继续原任务')
            if new_output_limit is None:
                job['inventory_partition_retry']={'prior_call':call['id'],'prior_limit':previous,'method':'complete_source_object_groups'}
            else:job['inventory_cap_retry']={'prior_call':call['id'],'prior_limit':previous,'new_limit':new_output_limit}
            job.pop('pending',None)
            job.update(status='queued',error=None)
            project.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(project,ensure_ascii=False),project['id']))
        return job

    def recover_original(self, jid, config):
        """Only query the original result. Resume requires the existing source version."""
        from .providers import Provider,Uncertain,returned_subscription_service_error
        job=self.store.job(jid)
        core_v1=job.get('core_chain_version')==1
        if (job['role']!='production' or not job.get('pending')
                or job['status'] not in ({'uncertain','failed'} if core_v1 else {'uncertain'})):
            raise Conflict('当前任务没有等待查询的原请求')
        if core_v1 and (not job.get('calls') or job['calls'][-1].get('step_key')!=job['pending']):
            raise Conflict('新版任务缺少与当前步骤对应的原调用身份，不能建立新的投递')
        resume_error=False
        repair_invalid=False
        if core_v1:
            call=job['calls'][-1]
            provider=Provider(self.store,config)
            outcome=provider.generate(
                job['project'],call['role'],None,None,job,job['pending'])
            if outcome.status=='UNKNOWN':
                if job['status']=='failed':
                    with self.store.connect() as cx:
                        cx.execute('BEGIN IMMEDIATE')
                        row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
                        current=json.loads(row[0]) if row else {}
                        if (current.get('status')!='failed' or current.get('pending')!=job['pending']
                                or (current.get('calls') or [{}])[-1].get('id')!=call['id']):
                            raise Conflict('原请求状态已变化，不能覆盖当前处理记录')
                        current.update(status='uncertain',error=str(outcome.error),error_type='Uncertain')
                        cx.execute("UPDATE jobs SET status='uncertain',body=? WHERE id=?",
                                   (json.dumps(current,ensure_ascii=False),jid))
                        cx.execute("UPDATE production_control SET status='uncertain',owner=NULL,lease_until=0 WHERE id=?",
                                   (jid,))
                        project_row=cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()
                        if project_row:
                            project=json.loads(project_row[0])
                            if (not project.get('active_job') and not project.get('trashed')
                                    and project.get('revision')==job['base_revision']):
                                project['state']='uncertain'
                                cx.execute('UPDATE projects SET body=? WHERE id=?',
                                           (json.dumps(project,ensure_ascii=False),job['project']))
                return {'recovered':False,'status':'uncertain'}
            if outcome.status=='KNOWN_FAILURE':
                if not isinstance(outcome.error,json.JSONDecodeError):
                    raise outcome.error
                provider.saved_complete_text(job)
                repair_invalid=True
            result=outcome.value
        else:
            try:result=Provider(self.store,config).recover(job)
            except Uncertain:
                if job['pending'] in job.get('service_error_retries',{}) or not returned_subscription_service_error(self.store,job['calls'][-1]):raise
                result=None;resume_error=True
        if result is None and not resume_error and not repair_invalid:
            return {'recovered':False,'status':'uncertain'}
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            current=json.loads(cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()[0])
            p=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if (current['status'] not in ({'uncertain','failed'} if core_v1 else {'uncertain'})
                    or p['active_job'] or p.get('trashed') or p['revision']!=job['base_revision']):
                raise Conflict('原请求已取回，但当前材料状态不允许继续；没有覆盖新版本')
            if not resume_error and not repair_invalid:
                job['results'][job.pop('pending')]=result
            job.update(status='queued',error=None)
            p.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        return {'recovered':not resume_error,'status':'queued',
                'received_service_error':resume_error}

    def recheck_failed_fidelity(self, jid, config, role='fidelity'):
        """Explicit new review after a terminal upstream failure, never a replay.

        The original call and its unknown consumption remain in the ledger.
        Running, unqueryable and successful upstream jobs cannot use this path.
        """
        import httpx
        job=self.store.job(jid);call=(job.get('calls') or [{}])[-1]
        stages={'writer':'writer','term_preparation':'writer','style':'style','style_contract_repair':'style',
                'fidelity':'fidelity','line_repair':'repair','teaching_review':'teaching'}
        if role not in stages:raise Conflict('没有对应的独立续接流程')
        stage=stages[role]
        if (job.get('status')!='uncertain' or job.get('stage')!=stage
                or call.get('channel')!='router' or not call.get('upstream_id')
                or call.get('role')!=role
                or call.get('step_key')!=job.get('pending')):
            raise Conflict('当前没有可重新核对的聊天任务')
        c=config|config.get('role_providers',{}).get(call['role'],{})
        reassessments=job.get(role+'_reassessments',[])
        if any(row.get('original_call')==call.get('id') for row in reassessments):
            raise Conflict('当前原请求已经建立过独立续接')
        primary_review=(role in {'style','style_contract_repair','fidelity','line_repair','teaching_review'} and job.get('quality_fallback_of')
            and config.get('resume_failed_chat_review_with_primary'))
        if role in {'writer','term_preparation'} or primary_review:
            enabled=(config.get('resume_failed_chat_writer_with_primary') if role in {'writer','term_preparation'} else primary_review)
            primary_provider=config.get('provider') if role=='fidelity' else c.get('provider')
            if (not enabled or primary_provider!='openai-compatible' or not job.get('quality_fallback_of')):
                raise Conflict('当前没有已配置且尚未使用的不同写作通道')
            if role=='term_preparation' and not job.get('writer_use_primary'):
                raise Conflict('只有已切换的旧准备阶段可以接续到相同主通道')
            c=c|config.get('quality_fallback_providers',{}).get(role,{})
        original=config.get('upstream_origin_aliases',{}).get(call.get('upstream_base'),call.get('upstream_base'))
        if original!=c['base_url']:raise Conflict('原核对通道已变化，不能确认原任务状态')
        with httpx.Client(timeout=20,follow_redirects=False) as client:
            response=client.get(c['base_url'].rstrip('/')+'/api/v1/jobs/'+call['upstream_id'],headers={'Authorization':'Bearer '+c['api_key']})
            response.raise_for_status();body=response.json()
        if body.get('id',body.get('jobId'))!=call['upstream_id'] or body.get('status') not in {'failed','cancelled','expired'} or body.get('output'):
            raise Conflict('原核对尚未确认结束且无结果，请继续查询原请求')
        receipt=self.store.blob(json.dumps(body,ensure_ascii=False).encode())
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            current=json.loads(cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()[0])
            p=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if (current.get('status')!='uncertain' or current.get('pending')!=job['pending']
                    or (current.get('calls') or [{}])[-1].get('id')!=call['id']
                    or p['active_job'] or p.get('trashed') or p['revision']!=job['base_revision']):
                raise Conflict('材料或任务状态已变化，未重新提交')
            original_step=current.pop('pending')
            if role=='fidelity':reason='Separate review of the saved candidate'
            elif role in {'style','style_contract_repair','line_repair','teaching_review'}:reason='One distinct primary-provider review of the unfinished assigned checks; saved candidate remains unchanged'
            else:reason='One distinct primary-provider continuation of unfinished units; saved units remain unchanged'
            current.setdefault(role+'_reassessments',[]).append({'original_call':call['id'],'original_step':original_step,
                'terminal_receipt':receipt,'draft_digest':digest(current['draft']),
                'reason':reason+' after confirmed upstream terminal failure; original delivery and spending remain unchanged'})
            if role=='writer':
                current['content_generation']=current.get('content_generation',0)+1
                current['writer_use_primary']=True
            elif role in {'style','style_contract_repair','line_repair','teaching_review'}:
                current.setdefault('primary_continuation_steps',[]).append(original_step)
            elif role=='fidelity' and primary_review:
                current.setdefault('primary_base_steps',[]).append(original_step)
                if role not in current.setdefault('primary_base_roles',[]):current['primary_base_roles'].append(role)
            current.update(status='queued',error=None);p.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(current,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        return {'status':'queued','original_call_preserved':call['id']}

    def continue_unqueryable_subscription(self, jid, config):
        """Create one distinct continuation after a synchronous call lost its response.

        The original call stays unknown and is never relabeled or reused. One
        logical step can take this path only once, which prevents a retry loop.
        """
        from urllib.parse import urlsplit
        job=self.store.job(jid);call=(job.get('calls') or [{}])[-1];role=call.get('role');step=call.get('step_key')
        primary_base=step in job.get('primary_base_steps',[]) or role in job.get('primary_base_roles',[])
        c=config if primary_base else config|config.get('role_providers',{}).get(role,{})
        primary_continuation=(role in {'writer','term_preparation'} and job.get('writer_use_primary')) or step in job.get('primary_continuation_steps',[]) or role in job.get('primary_continuation_roles',[]) or primary_base
        if primary_continuation:
            c=config
        if job.get('quality_fallback_of') and not primary_continuation:
            c=c|config.get('quality_fallback_providers',{}).get(role,{})
        previous=job.get('unqueryable_subscription_continuations',[])
        if (not config.get('resume_unqueryable_subscription_once') or not role or job.get('status')!='uncertain'
                or job.get('pending')!=step or call.get('channel')!='openai-compatible'
                or not call.get('dispatch_started') or call.get('response_blob') or call.get('http_status')
                or call.get('deadline_at',0)>time.time() or urlsplit(call.get('upstream_base') or '').hostname!='opencode.ai'
                or urlsplit(c.get('base_url') or '').hostname!='opencode.ai' or c.get('billing_mode')!='subscription'
                or any(row.get('logical_step')==step for row in previous)):
            raise Conflict('当前没有可建立的一次性订阅续接')
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            current=json.loads(cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()[0])
            project=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if (current.get('status')!='uncertain' or current.get('pending')!=step
                    or (current.get('calls') or [{}])[-1].get('id')!=call.get('id')
                    or project.get('active_job') or project.get('trashed') or project['revision']!=job['base_revision']):
                raise Conflict('材料或任务状态已变化，未建立续接')
            current.setdefault('unqueryable_subscription_continuations',[]).append({
                'original_call':call['id'],'logical_step':step,'role':role,
                'draft_digest':digest(current.get('draft',{'blocks':[]})),
                'saved_units':current.get('unit_index',0),'response_state':'unknown_no_query_interface',
                'reason':'One distinct subscription continuation; original delivery and subscription use remain unknown'})
            current.pop('pending',None);current.update(status='queued',error=None)
            project.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(current,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(project,ensure_ascii=False),project['id']))
        return {'status':'queued','original_call_preserved':call['id'],'logical_step':step}

    def continue_unqueryable_router(self, jid, config):
        """Continue once when a router submission returned no queryable identity.

        The unknown original delivery remains in the ledger. The continuation
        uses the normal primary role provider and is limited to one per logical
        step, so an unavailable router cannot create an automatic retry loop.
        """
        job=self.store.job(jid);call=(job.get('calls') or [{}])[-1];role=call.get('role');step=call.get('step_key')
        previous=job.get('unqueryable_router_continuations',[])
        review_roles={'style','style_contract_repair','fidelity','line_repair','teaching_review'}
        supported=review_roles|{'writer','term_preparation'}
        c=config|config.get('role_providers',{}).get(role,{})
        if job.get('quality_fallback_of'):
            c=c|config.get('quality_fallback_providers',{}).get(role,{})
        if (not config.get('resume_unqueryable_router_once')
                or (not job.get('quality_fallback_of') and role!='fidelity')
                or role not in supported or job.get('status')!='uncertain' or job.get('pending')!=step
                or call.get('channel')!='router' or call.get('upstream_id') or not call.get('dispatch_started')
                or call.get('response_blob') or call.get('http_status') or c.get('provider')!='router'
                or any(row.get('logical_step')==step for row in previous)):
            raise Conflict('当前没有可建立的一次性转发续接')
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            current=json.loads(cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()[0])
            project=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if (current.get('status')!='uncertain' or current.get('pending')!=step
                    or (current.get('calls') or [{}])[-1].get('id')!=call.get('id')
                    or project.get('active_job') or project.get('trashed') or project['revision']!=job['base_revision']):
                raise Conflict('材料或任务状态已变化，未建立续接')
            current.setdefault('unqueryable_router_continuations',[]).append({
                'original_call':call['id'],'logical_step':step,'role':role,
                'draft_digest':digest(current.get('draft',{'blocks':[]})),
                'saved_units':current.get('unit_index',0),'response_state':'unknown_without_upstream_identity',
                'reason':'One distinct primary-provider continuation; original router delivery and use remain unknown'})
            if role=='fidelity':
                if step not in current.setdefault('primary_base_steps',[]):current['primary_base_steps'].append(step)
                if role not in current.setdefault('primary_base_roles',[]):current['primary_base_roles'].append(role)
            elif step not in current.setdefault('primary_continuation_steps',[]):
                current['primary_continuation_steps'].append(step)
            current.pop('pending',None);current.update(status='queued',error=None)
            project.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(current,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(project,ensure_ascii=False),project['id']))
        return {'status':'queued','original_call_preserved':call['id'],'logical_step':step}

    def retry_rejected_contract(self, jid):
        """One repair of a provably impossible schema after confirmed terminal failure."""
        from .providers import impossible_closed_schema
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            job=json.loads(cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()[0])
            call=job['calls'][-1]
            body=json.loads(self.store.read_blob(call.get('response_blob','')))
            schema=body.get('task',{}).get('validation',{}).get('responseSchema',{})
            if (job['status']!='uncertain' or body.get('status')!='failed' or body.get('output')
                or not impossible_closed_schema(schema) or job.get('rejected_contract_retry')):
                raise Conflict('没有可明确证明的已结束协议拒绝，不能重新发送')
            p=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if p['active_job'] or p.get('trashed') or p['revision']!=job['base_revision']:
                raise Conflict('材料状态或版本已经变化')
            job['rejected_contract_retry']=call['id']
            job.pop('pending',None)
            job.update(status='queued',error=None)
            p.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        return job

    def finish(self, job, owner, status, publish=None):
        """Check ownership and commit project state and checkpoint in one transaction."""
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            control = cx.execute('SELECT * FROM production_control WHERE id=?',(job['id'],)).fetchone()
            if not control or control['owner']!=owner:
                raise Conflict('后台执行权已转移，旧进程不能覆盖结果')
            p = json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if publish:
                if control['cancel_requested'] or p.get('trashed'):
                    raise Conflict('材料已删除或任务已取消，未发布结果')
                if p.get('active_job')!=job['id']:
                    raise Conflict('当前材料已由另一任务接管，旧任务不能发布结果')
                if p['revision']!=job['base_revision'] or p['inventory']['digest']!=job['source_digest']:
                    raise Conflict('原材料版本已经变化，未覆盖新版本')
                before=json.dumps(p,ensure_ascii=False)
                cx.execute('INSERT INTO library_versions VALUES(?,?,?,?,?)', (identity(),p['id'],time.time(),'自动成稿前',before))
                p.update(publish)
                p['revision']+=1
                cx.execute('INSERT INTO revisions VALUES(?,?,?)', (p['id'],p['revision'],json.dumps(p['draft'],ensure_ascii=False)))
            job['status']=status
            job.pop('worker_owner',None)
            if status!='queued':
                job['finished']=time.time()
                if p.get('active_job')==job['id']:
                    p['active_job']=None
                    p['state']=status
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
            cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',(status,json.dumps(job,ensure_ascii=False),job['id']))
            cx.execute('UPDATE production_control SET status=?,owner=NULL,lease_until=0 WHERE id=?',(status,job['id']))

    def tree(self, trash=False):
        with self.store.connect() as cx:
            folders=[dict(r) for r in cx.execute('SELECT * FROM library_folders WHERE trashed=? ORDER BY name',(int(trash),))]
            fields=('id','title','folder','state','active_job','revision','library_revision','created','trashed')
            # SQLite parses the large project JSON once and returns only the
            # nine fields needed by the tree. Source objects stay in the project.
            expression='json_extract(body,'+','.join("'$."+field+"'" for field in fields)+')'
            rows=cx.execute('SELECT '+expression+' FROM projects WHERE COALESCE(json_extract(body,\'$.trashed\'),0)=? ORDER BY rowid DESC',(int(trash),))
            projects=[dict(zip(fields,json.loads(row[0]))) for row in rows]
        return {'folders':folders,'documents':projects}

    def folder(self, name, parent=None, fid=None, revision=None):
        if not str(name).strip() or len(name)>180:
            raise ValueError('文件夹名称需为 1 到 180 个字符')
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            if fid and revision is not None:
                row=cx.execute('SELECT revision,trashed FROM library_folders WHERE id=?',(fid,)).fetchone()
                if not row or row['revision']!=revision or row['trashed']:
                    raise Conflict('文件夹已变化，请刷新后再修改')
            ancestors=set()
            current=parent
            while current:
                if current==fid or current in ancestors:
                    raise Conflict('不能把文件夹移动到自身或其子文件夹中')
                ancestors.add(current)
                row=cx.execute('SELECT parent FROM library_folders WHERE id=? AND trashed=0',(current,)).fetchone()
                if not row:
                    raise KeyError(current)
                current=row[0]
            if fid:
                if not cx.execute('UPDATE library_folders SET name=?,parent=?,revision=revision+1 WHERE id=?',(name.strip(),parent,fid)).rowcount:
                    raise KeyError(fid)
            else:
                fid=identity()
                cx.execute('INSERT INTO library_folders(id,parent,name) VALUES(?,?,?)',(fid,parent,name.strip()))
        return {'id':fid,'name':name.strip(),'parent':parent}

    def delete_folder(self, fid):
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            if cx.execute('SELECT 1 FROM library_folders WHERE parent=?',(fid,)).fetchone() or cx.execute("SELECT 1 FROM projects WHERE json_extract(body,'$.folder')=?",(fid,)).fetchone():
                raise Conflict('先移动文件夹内的材料和子文件夹，再删除空文件夹')
            if not cx.execute('DELETE FROM library_folders WHERE id=?',(fid,)).rowcount:
                raise KeyError(fid)

    def edit_document(self, pid, revision, *, title=None, folder=None, move=False, trashed=None, budget_cny=None):
        def update(p):
            if budget_cny is not None:
                import math
                if isinstance(budget_cny,bool) or not isinstance(budget_cny,(int,float)) or not math.isfinite(budget_cny) or not 0<=budget_cny<=200:
                    raise ValueError('人民币付费上限需为 0 到 200 元')
                p['budget_cny']=budget_cny
            if title is not None:
                if not title.strip() or len(title)>180:
                    raise ValueError('材料名称需为 1 到 180 个字符')
                p['title']=title.strip()
            if move:
                if folder is not None:
                    with self.store.connect() as cx:
                        if not cx.execute('SELECT 1 FROM library_folders WHERE id=? AND trashed=0',(folder,)).fetchone():
                            raise KeyError(folder)
                p['folder']=folder
            if trashed is not None:
                p['trashed']=bool(trashed)
            # Library metadata has its own optimistic version, leaving production content intact.
            if p.get('library_revision',0)!=revision:
                raise Conflict('材料管理信息已变化，请刷新后再修改')
            p['library_revision']=revision+1
        p=self.store.change(pid,update)
        if trashed and p.get('active_job'):
            self.cancel(p['active_job'])
        return p

    def versions(self, pid):
        self.store.get(pid)
        with self.store.connect() as cx:
            return [dict(r) for r in cx.execute('SELECT id,created,reason FROM library_versions WHERE project=? ORDER BY created DESC',(pid,))]
