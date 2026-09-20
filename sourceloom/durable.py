"""Persistent work ownership; browser lifetime has no execution authority."""

import json
import copy
import math
import time
from pathlib import Path

from .store import Conflict, identity, digest


def planning_policy_digest():
    roles=Path(__file__).parent/'roles'
    names=('rewrite_planner','rewrite_plan_review','rewrite_planner_repair','rewrite_scope','plan_decision')
    return digest({name:(roles/(name+'.md')).read_text(encoding='utf-8') for name in names})


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
            job = dict(id=jid,project=pid,role='production',status='queued',created=now,calls=[],
                       joint_review_before_repair=True,fidelity_review_version=2,source_snapshot_digest=digest(p['inventory']),
                       stage='visual_extract' if any(o['kind'] in {'page','image'} and o.get('resource_id') for o in p['inventory']['objects']) else 'inventory',
                       results={},repair_rounds=0,planning_policy_digest=planning_policy_digest(),teaching_version=2,writing_contract_version=7,review_order='style_first',transformation_mode=p.get('mode','rewrite'),base_revision=p['revision'],
                       source_digest=p['inventory']['digest'],source=p['inventory'],goal=p['goal'],project_goal=p['goal'],
                       writing_skill={k:bundle[k] for k in ('root','package_digest','instruction_digest')})
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

    def claim(self, owner, lease=45, project=None):
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
            job=initialize(dict(id=jid,project=pid,role='production',status='queued',created=now,calls=[],
                results={},repair_rounds=0,base_revision=p['revision'],source_digest=p['inventory']['digest'],
                source_snapshot_digest=digest(p['inventory']),source=copy.deepcopy(p['inventory']),
                goal=p['goal'],project_goal=p['goal'],transformation_mode=p.get('mode','rewrite'),
                writing_skill={k:bundle[k] for k in ('root','package_digest','instruction_digest')}))
            previous=cx.execute("SELECT body FROM jobs WHERE project=? AND role='production' ORDER BY created DESC LIMIT 1",(pid,)).fetchone()
            old=json.loads(previous[0]) if previous else {}
            expected={o['id'] for o in p['inventory']['objects'] if o['kind'] in {'image','page'} and o.get('resource_id')}
            cards=old.get('visual_cards',[])
            if (expected and old.get('source_snapshot_digest')==job['source_snapshot_digest']
                    and old.get('writing_skill',{}).get('package_digest')==bundle['package_digest']
                    and {c['source_id'] for c in cards}==expected
                    and not any(c.get('blocking_uncertainty',c.get('uncertainty',[])) for c in cards)
                    and not old.get('source',{}).get('unknown')):
                job.update(source=copy.deepcopy(old['source']),visual_cards=copy.deepcopy(cards),
                    visual_index=len(cards),visual_count=len(cards),stage='active_visual',reused_visual_job=old['id'])
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
            if (job.get('pipeline') not in {'active_composition_v1','active_composition_v2'} or job['status']!='failed'
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
        last=(snapshot.get('calls') or [{}])[-1]
        if (config and snapshot.get('status')=='failed' and snapshot.get('pending')==last.get('step_key')
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
            billing=cx.execute('SELECT actual,body FROM spending WHERE id=?',(call.get('id',''),)).fetchone()
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
            continuation_rows=job.get('unqueryable_router_continuations',[])+job.get('unqueryable_subscription_continuations',[])
            software_resume=(not job.get('pending') and job.get('error')=='PermissionError'
                and any(item.get('original_call')==call.get('id') for item in continuation_rows))
            if not rejected and not no_call_preflight and not software_resume and ((job.get('pending') and not returned_invalid) or (call.get('status') not in {'completed','recovered'} and not returned_invalid)):
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
        if job['role']!='production' or job['status']!='uncertain' or not job.get('pending'):
            raise Conflict('当前任务没有等待查询的原请求')
        resume_error=False
        try:result=Provider(self.store,config).recover(job)
        except Uncertain:
            if job['pending'] in job.get('service_error_retries',{}) or not returned_subscription_service_error(self.store,job['calls'][-1]):raise
            result=None;resume_error=True
        if result is None and not resume_error:
            return {'recovered':False,'status':'uncertain'}
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            current=json.loads(cx.execute('SELECT body FROM jobs WHERE id=?',(jid,)).fetchone()[0])
            p=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if current['status']!='uncertain' or p['active_job'] or p.get('trashed') or p['revision']!=job['base_revision']:
                raise Conflict('原请求已取回，但当前材料状态不允许继续；没有覆盖新版本')
            if not resume_error:job['results'][job.pop('pending')]=result
            job.update(status='queued',error=None)
            p.update(active_job=jid,state='queued')
            cx.execute("UPDATE jobs SET status='queued',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute("UPDATE production_control SET status='queued',owner=NULL,lease_until=0 WHERE id=?",(jid,))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
        return {'recovered':not resume_error,'status':'queued','received_service_error':resume_error}

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
