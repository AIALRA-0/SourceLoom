"""Persistent work ownership; browser lifetime has no execution authority."""

import json
import time

from .store import Conflict, identity


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
            if p.get('draft'):
                raise Conflict('已有正文，请建立材料副本或修改当前版本')
            if cx.execute("SELECT 1 FROM production_control WHERE project=? AND status='uncertain'", (pid,)).fetchone():
                raise Conflict('原请求结果尚不确定，请查询原任务，不能重新生成')
            if cx.execute("SELECT 1 FROM production_control WHERE project=?",(pid,)).fetchone():
                raise Conflict('本篇已有处理记录，请继续原任务或明确建立新的材料副本，不能重置本篇限制')
            jid = identity()
            job = dict(id=jid,project=pid,role='production',status='queued',created=now,calls=[],
                       stage='visual_extract' if any(o['kind'] in {'page','image'} and o.get('resource_id') for o in p['inventory']['objects']) else 'inventory',
                       results={},repair_rounds=0,base_revision=p['revision'],
                       source_digest=p['inventory']['digest'],source=p['inventory'],goal=p['goal'],
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

    def heartbeat(self, jid, owner, lease=45):
        with self.store.connect() as cx:
            return cx.execute("UPDATE production_control SET lease_until=? WHERE id=? AND owner=? AND status='running'", (time.time()+lease,jid,owner)).rowcount==1

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
            returned_invalid=call.get('status')=='invalid' and call.get('finish_reason')=='stop' and bool(call.get('response_blob'))
            if not rejected and ((job.get('pending') and not returned_invalid) or (call.get('status') not in {'completed','recovered'} and not returned_invalid)):
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
            folders=[dict(r) for r in cx.execute('SELECT * FROM library_folders ORDER BY name')]
        projects=[{k:p.get(k) for k in ('id','title','folder','state','active_job','revision','created','trashed')}
                  for p in self.store.list() if bool(p.get('trashed'))==trash]
        return {'folders':folders,'documents':projects}

    def folder(self, name, parent=None, fid=None):
        if not str(name).strip() or len(name)>180:
            raise ValueError('文件夹名称需为 1 到 180 个字符')
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            ancestors=set()
            current=parent
            while current:
                if current==fid or current in ancestors:
                    raise Conflict('不能把文件夹移动到自身或其子文件夹中')
                ancestors.add(current)
                row=cx.execute('SELECT parent FROM library_folders WHERE id=?',(current,)).fetchone()
                if not row:
                    raise KeyError(current)
                current=row[0]
            if fid:
                if not cx.execute('UPDATE library_folders SET name=?,parent=? WHERE id=?',(name.strip(),parent,fid)).rowcount:
                    raise KeyError(fid)
            else:
                fid=identity()
                cx.execute('INSERT INTO library_folders VALUES(?,?,?)',(fid,parent,name.strip()))
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
                        if not cx.execute('SELECT 1 FROM library_folders WHERE id=?',(folder,)).fetchone():
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
