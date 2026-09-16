"""Durable file intake; accepted uploads outlive their browser request."""
import json
import threading
import time

from .store import Conflict, identity, digest


class IntakeQueue:
    def __init__(self,store,config):
        self.store,self.config=store,config
        with store.connect() as cx:
            cx.execute('CREATE TABLE IF NOT EXISTS intake_control(id TEXT PRIMARY KEY, lease_until REAL NOT NULL DEFAULT 0, owner TEXT)')
            cx.execute('CREATE TABLE IF NOT EXISTS intake_requests(project TEXT,request_id TEXT,signature TEXT,job TEXT,PRIMARY KEY(project,request_id))')

    def enqueue(self,pid,uploads=None,url=None,generate=False,request_id=None):
        if bool(uploads)==bool(url):raise ValueError('请选择文件或网页中的一种')
        if generate and self.config['provider']=='manual':raise Conflict('尚未配置生成通道，可以先保存材料')
        files=[{'name':name,'sha256':self.store.blob(raw),'bytes':len(raw)} for name,raw in uploads or []]
        if request_id is not None and (not isinstance(request_id,str) or not 1<=len(request_id)<=128):
            raise ValueError('上传请求标识无效')
        signature=digest({'files':files,'url':url,'generate':generate})
        now=time.time();jid=identity()
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM projects WHERE id=?',(pid,)).fetchone()
            if not row:raise KeyError(pid)
            p=json.loads(row[0])
            if request_id:
                prior=cx.execute('SELECT signature,job FROM intake_requests WHERE project=? AND request_id=?',(pid,request_id)).fetchone()
                if prior:
                    if prior['signature']!=signature:raise Conflict('这次上传的内容已改变，请新建一次导入')
                    saved=json.loads(cx.execute('SELECT body FROM jobs WHERE id=?',(prior['job'],)).fetchone()[0])
                    return {'id':saved['id'],'status':saved['status'],'project':pid,'received_files':len(saved['files']),
                            'received_bytes':sum(f['bytes'] for f in saved['files']),'reused':True}
            if p.get('active_job') or p.get('trashed'):raise Conflict('材料正在处理或已在回收站')
            job=dict(id=jid,project=pid,role='intake',status='queued',stage='intake',created=now,
                     files=files,url=url,auto_generate=generate,base_revision=p['revision'],calls=[])
            cx.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)',(jid,pid,'intake','queued',now,json.dumps(job,ensure_ascii=False)))
            cx.execute('INSERT INTO intake_control(id) VALUES(?)',(jid,))
            if request_id:cx.execute('INSERT INTO intake_requests VALUES(?,?,?,?)',(pid,request_id,signature,jid))
            p.update(active_job=jid,state='queued')
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),pid))
        return {'id':jid,'status':'queued','project':pid,'received_files':len(files),'received_bytes':sum(f['bytes'] for f in files)}

    def latest(self,pid):
        with self.store.connect() as cx:
            row=cx.execute("SELECT body FROM jobs WHERE project=? AND role='intake' ORDER BY created DESC LIMIT 1",(pid,)).fetchone()
        return json.loads(row[0]) if row else None

    def cancel(self,jid):
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute("SELECT body FROM jobs WHERE id=? AND role='intake'",(jid,)).fetchone()
            if not row:raise KeyError(jid)
            job=json.loads(row[0])
            if job['status'] not in {'queued','running'}:return {'cancel_requested':False,'status':job['status']}
            job.update(status='cancelled',finished=time.time())
            cx.execute("UPDATE jobs SET status='cancelled',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),jid))
            cx.execute('UPDATE intake_control SET owner=NULL,lease_until=0 WHERE id=?',(jid,))
            p=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
            if p.get('active_job')==jid:
                p.update(active_job=None,state='cancelled');cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),job['project']))
        return {'cancel_requested':True}

    def save_owned(self,job,owner):
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            if not cx.execute('SELECT 1 FROM intake_control WHERE id=? AND owner=?',(job['id'],owner)).fetchone():raise Conflict('接入任务已停止')
            cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',(job['status'],json.dumps(job,ensure_ascii=False),job['id']))

    def run_once(self):
        from .parse_worker import isolated_intake
        from .network import fetch_bundle
        from .versions import append_intake
        owner=identity();now=time.time()
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            if cx.execute("SELECT 1 FROM intake_control c JOIN jobs j ON j.id=c.id WHERE j.status='running' AND c.lease_until>?",(now,)).fetchone():return False
            row=cx.execute("SELECT j.body FROM jobs j JOIN intake_control c ON c.id=j.id WHERE j.role='intake' AND (j.status='queued' OR (j.status='running' AND c.lease_until<?)) ORDER BY j.created LIMIT 1",(now,)).fetchone()
            if not row:return False
            job=json.loads(row[0]);job.update(status='running',started=job.get('started',now),stage='fetch' if job.get('url') else 'intake')
            cx.execute('UPDATE intake_control SET owner=?,lease_until=? WHERE id=?',(owner,now+45,job['id']))
            cx.execute("UPDATE jobs SET status='running',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),job['id']))
        stop=threading.Event()
        def heartbeat():
            while not stop.wait(10):
                with self.store.connect() as cx:
                    if not cx.execute('UPDATE intake_control SET lease_until=? WHERE id=? AND owner=?',(time.time()+45,job['id'],owner)).rowcount:return
        thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
        try:
            if job.get('inventory_saved'):
                inv=None
            elif job.get('url'):
                uploads,url,aliases,failures=fetch_bundle(job['url'])
                # Persist fetched originals before parsing, including failed parsing.
                job['files']=[{'name':n,'sha256':self.store.blob(raw),'bytes':len(raw)} for n,raw in uploads]
                job.update(stage='intake');self.save_owned(job,owner)
                inv=isolated_intake(self.store,uploads,source_url=url,asset_aliases=aliases)
                inv['web_snapshot']={'asset_aliases':aliases,'fetch_failures':failures,'scope':'complete supplied HTML body'}
            else:inv=isolated_intake(self.store,[(f['name'],self.store.read_blob(f['sha256'])) for f in job['files']])
            with self.store.connect() as cx:
                cx.execute('BEGIN IMMEDIATE')
                lease=cx.execute('SELECT owner FROM intake_control WHERE id=?',(job['id'],)).fetchone()
                p=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
                if lease[0]!=owner or p.get('trashed') or p.get('active_job')!=job['id'] or p['revision']!=job['base_revision']:raise Conflict('材料版本已变化，原件已保留，没有覆盖新版本')
                if inv is not None:
                    if p.get('inventory'):p.update(inventory=append_intake(p['inventory'],inv),revision=p['revision']+1)
                    else:p['inventory']=inv
                    job.update(inventory_saved=True,base_revision=p['revision'],source_objects=len(inv['objects']))
                p.update(active_job=job['id'] if job.get('auto_generate') else None,state='inventoried')
                job.update(status='running' if job.get('auto_generate') else 'completed',stage='received')
                if not job.get('auto_generate'):job['finished']=time.time()
                cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),job['project']))
                cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',(job['status'],json.dumps(job,ensure_ascii=False),job['id']))
            if job.get('auto_generate'):
                from .durable import Queue
                from .skills import deploy_skill
                try:
                    Queue(self.store,pipeline=self.config.get('generation_pipeline','legacy')).enqueue(job['project'],deploy_skill(self.config['writing_skill_dir'],self.store.root),expected_intake=job['id'])
                except (ValueError,Conflict) as exc:
                    job.update(generation_error=str(exc),status='needs_attention');self.save_owned(job,owner)
                    self.store.change(job['project'],lambda p:p.update(active_job=None) if p.get('active_job')==job['id'] else None)
        except Exception as exc:
            with self.store.connect() as cx:
                cx.execute('BEGIN IMMEDIATE')
                lease=cx.execute('SELECT owner FROM intake_control WHERE id=?',(job['id'],)).fetchone()
                if lease and lease[0]==owner:
                    job.update(status='failed',finished=time.time(),error=str(exc)[:300] if isinstance(exc,ValueError) else
                        ('接入材料失败，收到的原始文件已保存' if job.get('files') else '网页获取失败，地址已保存，尚未收到原始文件'))
                    cx.execute("UPDATE jobs SET status='failed',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),job['id']))
                    p=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(job['project'],)).fetchone()[0])
                    if p.get('active_job')==job['id']:
                        p.update(active_job=None,state='failed');cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),job['project']))
        finally:stop.set();thread.join(timeout=1)
        return True


def intake_worker(store,config,stop):
    queue=IntakeQueue(store,config)
    while not stop.is_set():
        if not queue.run_once():stop.wait(1)
