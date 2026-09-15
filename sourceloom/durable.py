"""Persistent work ownership; browser lifetime has no execution authority."""

import json
import math
import time
from pathlib import Path

from .store import Conflict, identity, digest


def planning_policy_digest():
    roles=Path(__file__).parent/'roles'
    names=('rewrite_planner','rewrite_plan_review','rewrite_planner_repair','rewrite_scope','plan_decision')
    return digest({name:(roles/(name+'.md')).read_text(encoding='utf-8') for name in names})


class Queue:
    def __init__(self, store):
        self.store = store
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
    def estimate(inventory, max_calls=24):
        pages=sum(o['kind'] in {'page','image'} and bool(o.get('resource_id')) for o in inventory['objects'])
        visual_batches=math.ceil(pages/3)
        minimum_calls=8+visual_batches*2
        source_chars=sum(len(o.get('text','')) for o in inventory['objects'])
        return dict(source_chars=source_chars,source_objects=len(inventory['objects']),
                    visual_batches=visual_batches,minimum_calls=minimum_calls,
                    max_calls=max_calls,feasible=minimum_calls<=max_calls)

    def enqueue(self, pid, bundle):
        now = time.time()
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row = cx.execute('SELECT body FROM projects WHERE id=?', (pid,)).fetchone()
            if not row:
                raise KeyError(pid)
            p = json.loads(row[0])
            if p.get('trashed'):
                raise Conflict('先恢复材料，再开始生成')
            if p.get('active_job'):
                return self.store.job(p['active_job'])
            if not p.get('inventory'):
                raise Conflict('先上传文件或导入网页')
            estimate=self.estimate(p['inventory'])
            if not estimate['feasible']:
                raise Conflict(f"原件至少需要 {estimate['minimum_calls']} 次调用完成基本视觉与正文流程，超过单篇 {estimate['max_calls']} 次上限，请先缩小本次材料范围")
            if p.get('draft'):
                raise Conflict('已有正文，请建立材料副本或修改当前版本')
            if cx.execute("SELECT 1 FROM production_control WHERE project=? AND status='uncertain'", (pid,)).fetchone():
                raise Conflict('原请求结果尚不确定，请查询原任务，不能重新生成')
            if cx.execute("SELECT 1 FROM production_control WHERE project=?",(pid,)).fetchone():
                raise Conflict('本篇已有处理记录，请继续原任务或明确建立新的材料副本，不能重置本篇限制')
            jid = identity()
            job = dict(id=jid,project=pid,role='production',status='queued',created=now,calls=[],
                       stage='visual_extract' if any(o['kind'] in {'page','image'} and o.get('resource_id') for o in p['inventory']['objects']) else 'inventory',
                       results={},repair_rounds=0,planning_policy_digest=planning_policy_digest(),teaching_version=2,writing_contract_version=7,review_order='style_first',transformation_mode=p.get('mode','rewrite'),base_revision=p['revision'],
                       source_digest=p['inventory']['digest'],source=p['inventory'],goal=p['goal'],project_goal=p['goal'],
                       writing_skill={k:bundle[k] for k in ('root','package_digest','instruction_digest')})
            cx.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',
                       (jid,pid,'production','queued',now,json.dumps(job,ensure_ascii=False)))
            cx.execute('INSERT INTO production_control(id,project,status,created) VALUES(?,?,?,?)', (jid,pid,'queued',now))
            p.update(active_job=jid,state='queued',max_calls=24)
            cx.execute('UPDATE projects SET body=? WHERE id=?', (json.dumps(p,ensure_ascii=False),pid))
        return job

    def claim(self, owner, lease=45, project=None):
        now = time.time()
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            if cx.execute("SELECT count(*) FROM production_control WHERE status='running' AND lease_until>=?",(now,)).fetchone()[0]>=2:
                return None
            row = cx.execute("SELECT * FROM production_control WHERE (status='queued' OR (status='running' AND lease_until<?)) AND (? IS NULL OR project=?) ORDER BY created LIMIT 1", (now,project,project)).fetchone()
            if not row:
                return None
            cx.execute("UPDATE production_control SET status='running',owner=?,lease_until=? WHERE id=?", (owner,now+lease,row['id']))
            job = json.loads(cx.execute('SELECT body FROM jobs WHERE id=?',(row['id'],)).fetchone()[0])
            job['recovered_lease'] = row['status']=='running'
            job.setdefault('started',now)
            job['status']='running'
            job['worker_owner']=owner
            cx.execute("UPDATE jobs SET status='running',body=? WHERE id=?", (json.dumps(job,ensure_ascii=False),job['id']))
        return job

    def rewrite_existing(self,pid,bundle):
        """Reuse verified source inventory, while recording a new rewrite of the saved version."""
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM projects WHERE id=?',(pid,)).fetchone()
            if not row:raise KeyError(pid)
            p=json.loads(row[0])
            if p.get('active_job') or p.get('trashed') or not p.get('draft'):
                raise Conflict('当前材料没有可重新改写的已保存正文，或仍在处理中')
            prior=cx.execute("SELECT body FROM jobs WHERE project=? AND role='production' ORDER BY created DESC LIMIT 1",(pid,)).fetchone()
            if not prior and p.get('copied_from',{}).get('inventory_job'):
                prior=cx.execute("SELECT body FROM jobs WHERE id=? AND role='production'",(p['copied_from']['inventory_job'],)).fetchone()
            old=json.loads(prior[0]) if prior else {}
            if (old.get('status') in {'uncertain','running','queued'} or not old.get('facts') or
                    old.get('inventory',{}).get('digest')!=p.get('inventory',{}).get('digest')):
                raise Conflict('当前原件与已核对清单不一致，需要重新清点')
            if not p['inventory'].get('frozen') or not p['inventory'].get('inventory_review'):
                raise Conflict('原件清单尚未完成独立核对')
            jid=identity();now=time.time()
            job=dict(id=jid,project=pid,role='production',status='queued',created=now,calls=[],
                stage='planner',results={},repair_rounds=0,planning_policy_digest=planning_policy_digest(),teaching_version=2,writing_contract_version=7,review_order='style_first',transformation_mode='rewrite',
                base_revision=p['revision'],source_digest=p['inventory']['digest'],source=old['source'],
                inventory=p['inventory'],facts=old['facts'],reused_inventory_job=old['id'],
                project_goal=p['goal'],
                goal='保持原文主旨、作者意图与人称，完整保留信息，改善逻辑与可读性，不自行设计课程、情境、练习或拓展',
                writing_skill={k:bundle[k] for k in ('root','package_digest','instruction_digest')})
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
        with self.store.connect() as cx:
            if not cx.execute('UPDATE production_control SET cancel_requested=1 WHERE id=?',(jid,)).rowcount:
                raise KeyError(jid)
        return {'cancel_requested':True}

    def retry_validation(self, jid):
        """Continue a known returned artifact after a code fix, retaining all limits."""
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:
                raise KeyError(jid)
            job=json.loads(row[0])
            if job['role']!='production' or job['status']!='failed':
                raise Conflict('只能继续已经收到完整响应的校验失败任务')
            call=job['calls'][-1] if job['calls'] else {}
            # Migrate the known old checkpoint bug: the trial circuit rejected
            # the request before Provider created any call or dispatched bytes.
            no_call_preflight=(not job['calls'] and
                job.get('error')=='本批同类失败已连续发生两次，先修正原因，未发送新请求')
            if (job.get('pending') and call.get('step_key')!=job['pending'] and
                    job.get('error')=='本批同类失败已连续发生两次，先修正原因，未发送新请求'):
                job.setdefault('preflight_stops',[]).append(dict(key=job['pending'],dispatched=False,
                    reason='legacy_trial_circuit_before_provider_dispatch'))
                job.pop('pending',None)
            billing=cx.execute('SELECT actual,body FROM spending WHERE id=?',(call.get('id',''),)).fetchone()
            rejected=bool(billing and billing['actual']==0 and json.loads(billing['body']).get('status')=='rejected')
            if rejected:
                key=job.get('pending')
                local_preflight=json.loads(billing['body']).get('reason')=='local_preflight_before_dispatch'
                if not key or (key in job.get('rejected_resubmissions',{}) and not local_preflight):
                    raise Conflict('已明确拒绝的该阶段只允许修正配置后继续一次')
                if not local_preflight:
                    job.setdefault('rejected_resubmissions',{})[key]=call['id']
                job.pop('pending',None)
            returned_invalid=call.get('status')=='invalid' and call.get('finish_reason') in {'stop','tool_calls'} and bool(call.get('response_blob'))
            if (call.get('status') in {'truncated','reasoning_exhausted'} and billing and
                    billing['actual'] is not None and call.get('response_blob')):
                from .providers import reasoning_exhausted
                if reasoning_exhausted(json.loads(self.store.read_blob(call['response_blob']))):
                    call['status']='reasoning_exhausted'
                    returned_invalid=True
            if not rejected and not no_call_preflight and ((job.get('pending') and not returned_invalid) or (call.get('status') not in {'completed','recovered'} and not returned_invalid)):
                raise Conflict('原调用没有完整结果，不能重新发送')
            p=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if p['active_job'] or p.get('trashed') or p['revision']!=job['base_revision']:
                raise Conflict('原版本已变化或材料不可继续处理')
            job.setdefault('validation_stops',[]).append(dict(error=job.get('error'),at=job.get('finished')))
            job.update(status='queued',error=None)
            p.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        return job

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

    def repair_teaching_replan(self, jid):
        """One bounded new plan after a saved independent repair-plan rejection."""
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0])
            key=f"teaching-replan-review-{job.get('repair_rounds')}-route-v2"
            if (job.get('role')!='production' or job.get('status')!='needs_attention' or
                    job.get('stage')!='teaching_replan_review' or
                    not job.get('regeneration') or not job.get('quality_issues') or
                    job.get('teaching_replan_attempts') or key not in job.get('results',{}) or
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
            job['teaching_replan_attempts']=1
            job['teaching_replan_repair_issues']=job['quality_issues']
            job.update(status='queued',stage='teaching_replan_repair',quality_issues=[])
            project.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(project,ensure_ascii=False),project['id']))
        return job

    def retry_truncated_inventory(self, jid, new_output_limit):
        """One revised-cap attempt after a paid, known-truncated inventory call."""
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0])
            call=job['calls'][-1] if job.get('calls') else {}
            if (job.get('role')!='production' or job.get('status')!='failed' or
                    job.get('stage')!='inventory' or job.get('pending')!='inventory' or
                    job.get('inventory_cap_retry') or call.get('status')!='truncated' or
                    call.get('finish_reason')!='length' or not call.get('wire_request_blob')):
                raise Conflict('当前没有可按增大输出上限重试的已知截断清单')
            previous=json.loads(self.store.read_blob(call['wire_request_blob']))['max_tokens']
            if new_output_limit<=previous:raise Conflict('新的输出上限必须高于上次截断上限')
            paid=cx.execute('SELECT actual FROM spending WHERE id=?',(call['id'],)).fetchone()
            if not paid or paid['actual'] is None:raise Conflict('上次用量尚未结算，不能重新提交')
            row=cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()
            if not row:raise KeyError(job['project'])
            project=json.loads(row[0])
            if project.get('active_job') or project.get('trashed') or project['revision']!=job['base_revision']:
                raise Conflict('材料已变化，不能继续原任务')
            job['inventory_cap_retry']={'prior_call':call['id'],'prior_limit':previous,'new_limit':new_output_limit}
            job.pop('pending',None)
            job.update(status='queued',error=None)
            project.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(project,ensure_ascii=False),project['id']))
        return job

    def recover_original(self, jid, config):
        """Only query the original result. Resume requires the existing source version."""
        from .providers import Provider
        job=self.store.job(jid)
        if job['role']!='production' or job['status']!='uncertain' or not job.get('pending'):
            raise Conflict('当前任务没有等待查询的原请求')
        result=Provider(self.store,config).recover(job)
        if result is None:
            return {'recovered':False,'status':'uncertain'}
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            current=json.loads(cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()[0])
            p=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if current['status']!='uncertain' or p['active_job'] or p.get('trashed') or p['revision']!=job['base_revision']:
                raise Conflict('原请求已取回，但当前材料状态不允许继续；没有覆盖新版本')
            job['results'][job.pop('pending')]=result
            job.update(status='queued',error=None)
            p.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        return {'recovered':True,'status':'queued'}

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

    def edit_document(self, pid, revision, *, title=None, folder=None, move=False, trashed=None):
        def update(p):
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
