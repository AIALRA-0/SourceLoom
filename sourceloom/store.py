"""SQLite transactions coordinate immutable revisions and spending reservations."""

import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import contextmanager


class Conflict(ValueError):
    pass


def digest(value):
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def identity():
    return uuid.uuid4().hex


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "state.sqlite3"
        with self.connect() as cx:
            cx.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions(project TEXT, revision INTEGER, body TEXT NOT NULL,
                    PRIMARY KEY(project, revision));
                CREATE TABLE IF NOT EXISTS source_versions(project TEXT, version INTEGER, body TEXT NOT NULL,
                    PRIMARY KEY(project, version));
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, project TEXT, role TEXT, status TEXT,
                    created REAL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS spending(id TEXT PRIMARY KEY, project TEXT, reserved REAL,
                    actual REAL, status TEXT, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT, created REAL, kind TEXT, body TEXT);
            """)
            if 'created' not in {r[1] for r in cx.execute('PRAGMA table_info(spending)')}:
                cx.execute('ALTER TABLE spending ADD COLUMN created REAL NOT NULL DEFAULT 0')
            from .progress import install
            install(cx)
            # Classify jobs that predate the core-chain cutover exactly once.
            # A missing marker created after this migration is an error, not
            # implicit permission to run the legacy executor.
            if cx.execute('PRAGMA user_version').fetchone()[0] == 0:
                for row in cx.execute("SELECT id,body FROM jobs WHERE role='production'").fetchall():
                    job = json.loads(row['body'])
                    if (job.get('pipeline') in {'active_composition_v1', 'active_composition_v2'}
                            and 'core_chain_version' not in job):
                        job['core_chain_version'] = 0
                        cx.execute('UPDATE jobs SET body=? WHERE id=?',
                                   (json.dumps(job, ensure_ascii=False), row['id']))
                cx.execute('PRAGMA user_version=1')

    @contextmanager
    def connect(self):
        cx = sqlite3.connect(self.db, timeout=20)
        cx.row_factory = sqlite3.Row
        try:
            with cx:
                yield cx
        finally:
            cx.close()

    def create(self, title, mode="rewrite", budget=0.5, *, initialize=None):
        pid = identity()
        body = dict(id=pid, title=title, mode=mode, created=time.time(), revision=0,
                    state="collecting", inventory=None, plan=None, draft=None, review=None,
                    accepted_revision=None, repair_rounds=0, budget_usd=budget, max_calls=12,
                    active_job=None, goal="完整保留材料，并按必要前提循序讲清", research=None)
        with self.connect() as cx:
            if initialize is not None:
                cx.execute('BEGIN IMMEDIATE')
                initialize(body, cx)
            cx.execute("INSERT INTO projects VALUES(?,?)", (pid, json.dumps(body, ensure_ascii=False)))
        return body

    def get(self, pid):
        with self.connect() as cx:
            row = cx.execute("SELECT body FROM projects WHERE id=?", (pid,)).fetchone()
        if not row:
            raise KeyError(pid)
        return json.loads(row[0])

    def list(self):
        with self.connect() as cx:
            return [json.loads(r[0]) for r in cx.execute("SELECT body FROM projects ORDER BY rowid DESC")]

    def change(self, pid, update, expected=None):
        with self.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            row = cx.execute("SELECT body FROM projects WHERE id=?", (pid,)).fetchone()
            if not row:
                raise KeyError(pid)
            p = json.loads(row[0])
            if expected is not None and p["revision"] != expected:
                raise Conflict("版本已变化，已拒绝覆盖")
            update(p)
            cx.execute("UPDATE projects SET body=? WHERE id=?", (json.dumps(p, ensure_ascii=False), pid))
            if p.get("draft"):
                cx.execute("INSERT OR IGNORE INTO revisions VALUES(?,?,?)",
                           (pid, p["revision"], json.dumps(p["draft"], ensure_ascii=False)))
        return p

    def event(self, pid, kind, body):
        with self.connect() as cx:
            cx.execute("INSERT INTO events(project,created,kind,body) VALUES(?,?,?,?)",
                       (pid, time.time(), kind, json.dumps(body, ensure_ascii=False)))

    def revise_sources(self, pid, revision, inventory_digest, reason):
        from .versions import reopen
        with self.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM projects WHERE id=?',(pid,)).fetchone()
            if not row:raise KeyError(pid)
            p=json.loads(row[0])
            if p['revision']!=revision or not p.get('inventory') or p['inventory']['digest']!=inventory_digest:
                raise Conflict('补漏基线已变化，原版本未改变')
            if cx.execute("SELECT 1 FROM jobs WHERE project=? AND status IN ('uncertain','paused')",(pid,)).fetchone():
                raise Conflict('先处理原任务的未完成结果，再变更源清单')
            previous=json.dumps(p,ensure_ascii=False)
            version=p['inventory']['version']
            reopen(p,reason)
            cx.execute('INSERT INTO source_versions VALUES(?,?,?)',(pid,version,previous))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),pid))
        return p

    def source_versions(self, pid):
        with self.connect() as cx:
            return [json.loads(r[0]) for r in cx.execute('SELECT body FROM source_versions WHERE project=? ORDER BY version',(pid,))]

    def events(self, pid):
        with self.connect() as cx:
            return [dict(r) | {"body": json.loads(r["body"])} for r in
                    cx.execute("SELECT * FROM events WHERE project=? ORDER BY id DESC LIMIT 120", (pid,))]

    def put_job(self, job):
        with self.connect() as cx:
            if job.get('worker_owner'):
                cx.execute('BEGIN IMMEDIATE')
                row=cx.execute('SELECT owner FROM production_control WHERE id=?',(job['id'],)).fetchone()
                if not row or row[0]!=job['worker_owner']:
                    raise Conflict('后台执行权已变化，旧进程不能改写任务记录')
            cx.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,body=excluded.body",
                       (job["id"], job["project"], job["role"], job["status"], job["created"], json.dumps(job, ensure_ascii=False)))

    def begin_provider_dispatch(self, job, call_id, limit=3):
        """Persist one dispatch claim; retain old route budgets for old tasks.

        The claim commits before bytes leave the process. A crash after this
        point conservatively consumes a slot; it can never make a restart
        resend an already claimed request under a fresh identity.
        """
        from urllib.parse import urlsplit
        limit=max(1,int(limit))
        with self.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            if job.get('worker_owner'):
                owner=cx.execute('SELECT owner FROM production_control WHERE id=?',(job['id'],)).fetchone()
                if not owner or owner[0]!=job['worker_owner']:
                    raise Conflict('后台执行权已变化，未派发模型请求')
            row=cx.execute('SELECT body FROM jobs WHERE id=?',(job['id'],)).fetchone()
            if not row:
                raise Conflict('后台调用记录不存在，未派发模型请求')
            saved=json.loads(row[0])
            current=next((item for item in saved.get('calls',[]) if item.get('id')==call_id),None)
            if not current:
                raise Conflict('后台调用身份不存在，未派发模型请求')
            if current.get('dispatch_started'):
                raise Conflict('该调用身份已领取派发名额，禁止再次发送')
            step=current.get('step_key')
            if saved.get('core_chain_version')==1:
                logical_id=current.get('logical_request_id')
                if not logical_id:
                    raise Conflict('新版调用缺少稳定逻辑身份，未派发模型请求')
                prior=[item for item in saved.get('calls',[])
                       if item.get('id')!=call_id
                       and (item.get('logical_request_id')==logical_id
                            or item.get('step_key')==step)
                       and item.get('dispatch_started')
                       and item.get('status')!='rejected'
                       and item.get('dispatch_state')!='confirmed_not_sent']
                if prior:
                    raise Conflict('同一逻辑请求此前可能已送达，未确认前禁止再次生成')
            route_host=(urlsplit(str(current.get('upstream_base') or '')).hostname or '').casefold()
            recovery=(saved.get('transport_recovery_routes') or {}).get(step)
            legacy_excess=bool(recovery and recovery.get('recovery_protocol_version')
                               =='kuafu-responses-sse-v1' and recovery.get('legacy_excess'))
            is_active_v2=(saved.get('pipeline')=='active_composition_v2'
                          and saved.get('core_chain_version')==0 and step)
            same_step=[item for item in saved.get('calls',[])
                       if item.get('step_key')==step and item.get('dispatch_started')
                       and item.get('dispatch_state')!='confirmed_not_sent']
            kuafu=[item for item in same_step
                   if (urlsplit(str(item.get('upstream_base') or '')).hostname or '').casefold()
                   =='api.kuafushe.cc']
            is_sse=(bool(recovery and recovery.get('recovery_protocol_version')
                         =='kuafu-responses-sse-v1') and recovery.get('status')=='dispatching'
                    and current.get('protocol')=='responses' and current.get('streaming') is True)
            if is_active_v2 and route_host=='api.kuafushe.cc':
                prior_ids={item.get('provider_id') for item in kuafu}
                prior_sse=[item for item in kuafu if item.get('protocol')=='responses'
                           and item.get('streaming') is True]
                if is_sse:
                    saved_nonstream_error=any(item.get('protocol')=='responses'
                        and item.get('streaming') is False and item.get('status')=='uncertain'
                        and item.get('http_status') in {520,521,522,523,524}
                        and item.get('response_blob') for item in kuafu)
                    if prior_sse or not saved_nonstream_error:
                        raise Conflict('本阶段 Responses SSE 恢复已尝试或缺少已保存的非流式 52x 响应')
                    if len(same_step)>=limit and not legacy_excess:
                        raise Conflict('本阶段模型派发次数已用完，原件与已完成结果均保留')
                else:
                    if len(kuafu)>=2 or current.get('provider_id') in prior_ids:
                        raise Conflict('本阶段夸父社主备线路都已派发，禁止重新发送')
                    if len(same_step)>=2:
                        raise Conflict('本阶段夸父社主备派发次数已用完，等待唯一的 Responses SSE 恢复')
            current['dispatch_started']=True
            current['dispatch_state']='started'
            if is_active_v2 and route_host=='api.kuafushe.cc':
                prior_count=len(same_step)
                current['step_dispatch_ordinal']=prior_count+1
                budgets=saved.setdefault('step_dispatch_budgets',{})
                budget=budgets.setdefault(step,{'limit':limit,'call_ids':[]})
                budget['limit']=limit
                budget.setdefault('call_ids',[]).append(call_id)
                budget['dispatch_count']=prior_count+1
                if legacy_excess:
                    budget['legacy_excess']=True
                    budget['legacy_prior_dispatches']=prior_count
                if is_sse:
                    budget['sse_recovery_call_id']=call_id
            saved['calls']=[current if item.get('id')==call_id else item
                            for item in saved.get('calls',[])]
            cx.execute('UPDATE jobs SET status=?,body=? WHERE id=?',
                       (saved.get('status','running'),json.dumps(saved,ensure_ascii=False),job['id']))
        # Keep the in-memory recovery objects alive. ActiveComposition holds a
        # reference to transport_recovery_routes[step] while Provider runs;
        # replacing the whole dict here would leave it mutating a detached
        # state object after this durable dispatch claim.
        local_call=next((item for item in job.get('calls',[]) if item.get('id')==call_id),None)
        if local_call is not None:
            local_call.update(current)
        else:
            job.setdefault('calls',[]).append(current)
        if 'step_dispatch_budgets' in saved:
            job['step_dispatch_budgets']=saved['step_dispatch_budgets']

    def job(self, jid):
        with self.connect() as cx:
            r = cx.execute("SELECT body FROM jobs WHERE id=?", (jid,)).fetchone()
        if not r:
            raise KeyError(jid)
        return json.loads(r[0])

    def jobs(self, pid):
        with self.connect() as cx:
            return [json.loads(r[0]) for r in cx.execute("SELECT body FROM jobs WHERE project=? ORDER BY created", (pid,))]

    def reserve(self, pid, call_id, amount, body, daily_budget=None, daily_calls=80,
                total_budget=None, subscription_calls=None, job_record=None):
        if amount < 0:
            raise ValueError("费用预留不能为负数")
        with self.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            if job_record is not None:
                if job_record.get('project')!=pid:
                    raise Conflict('请求记录与项目不匹配，未预留费用')
                if job_record.get('worker_owner'):
                    control=cx.execute('SELECT owner,status,lease_until FROM production_control WHERE id=?',
                        (job_record['id'],)).fetchone()
                    if (not control or control['owner']!=job_record['worker_owner']
                            or control['status']!='running' or control['lease_until']<=time.time()):
                        raise Conflict('后台执行权已变化，未预留费用或派发请求')
            p = json.loads(cx.execute("SELECT body FROM projects WHERE id=?", (pid,)).fetchone()[0])
            previous = cx.execute("SELECT * FROM spending WHERE id=?", (call_id,)).fetchone()
            if previous:
                raise Conflict("已有调用身份，不得重复发送")
            subscription=body.get('channel') in {'router','codex-cli','subscription'}
            if not subscription and p.get('budget_cny') is not None:
                from .money import summary
                costs=[dict(r)|{'body':json.loads(r['body'])} for r in cx.execute('SELECT actual,body FROM spending WHERE project=?',(pid,))]
                native=summary(costs)
                if body.get('reserved_cny') is None:raise Conflict('当前通道未配置人民币价格，未按美元冒充人民币计费')
                if native['known_cny']+native['reserved_cny']+body['reserved_cny']>p['budget_cny']+1e-9:
                    raise Conflict(f"本篇人民币费用保护暂停，下一步预留后将超过 ¥{p['budget_cny']:.2f}，已有结果保留")
            paid="COALESCE(json_extract(body,'$.channel'),'') NOT IN ('router','codex-cli','subscription')"
            total, count = cx.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0), SUM(CASE WHEN "+paid+" THEN 1 ELSE 0 END) FROM spending WHERE project=?", (pid,)).fetchone()
            if not subscription and total + amount > p["budget_usd"] + 1e-9:
                raise Conflict(f"本篇费用保护暂停：已计费或预留 ${total:.4f}，下一步最多预留 ${amount:.4f}，本篇上限 ${p['budget_usd']:.2f}；这是本篇设置，不是模型账户余额耗尽")
            if not subscription and p.get("max_calls") is not None and (count or 0) >= p["max_calls"]:
                raise Conflict(f"本篇流程保护暂停：已记录 {count} 次请求，达到本篇设置的 {p['max_calls']} 次；这不是订阅账户额度耗尽，已有产物保留")
            day=int(time.time()//86400)*86400
            global_total,global_calls=cx.execute('SELECT COALESCE(SUM(COALESCE(actual,reserved)),0),SUM(CASE WHEN '+paid+' THEN 1 ELSE 0 END) FROM spending WHERE created>=?',(day,)).fetchone()
            if not subscription and daily_budget is not None and global_total+amount>daily_budget+1e-9:
                raise Conflict('今日接口费用保护暂停，已知费用与预留合计将超过当前设置，已有结果保留；不是订阅账户额度耗尽')
            if not subscription and daily_calls is not None and (global_calls or 0)>=daily_calls:
                raise Conflict('今日接口请求次数达到当前保护设置，已有结果保留；订阅请求不计入这个次数')
            all_spending=cx.execute('SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM spending').fetchone()[0]
            if not subscription and total_budget is not None and all_spending+amount>total_budget+1e-9:
                raise Conflict('累计测试预算已达上限，更换日期或项目不能重新获得额度')
            if subscription_calls is not None and body.get('channel') in {'router','codex-cli'}:
                # A confirmed pre-dispatch rejection did not consume a model
                # subscription call. Keep its ledger row and zero settlement,
                # but do not spend one of the user-authorized accepted calls.
                used=cx.execute("SELECT COUNT(*) FROM spending WHERE json_extract(body,'$.channel') "
                    "IN ('router','codex-cli','subscription') AND NOT "
                    "(COALESCE(actual,-1)=0 AND COALESCE(json_extract(body,'$.status'),'')='rejected')").fetchone()[0]
                if used>=subscription_calls:
                    raise Conflict('订阅测试请求次数已达累计上限')
            if job_record is not None:
                updated=cx.execute('UPDATE jobs SET status=?,body=? WHERE id=? AND project=?',
                    (job_record.get('status','running'),json.dumps(job_record,ensure_ascii=False),
                     job_record['id'],pid))
                if updated.rowcount!=1:
                    raise Conflict('后台请求记录不存在，未预留费用')
            cx.execute("INSERT INTO spending(id,project,reserved,actual,status,body,created) VALUES(?,?,?,?,?,?,?)", (call_id, pid, amount, None, "reserved", json.dumps(body),time.time()))

    def settle(self, call_id, actual, body):
        if actual is not None and actual < 0:
            raise ValueError("费用不能为负数")
        with self.connect() as cx:
            row = cx.execute("SELECT status,body FROM spending WHERE id=?", (call_id,)).fetchone()
            if not row or row[0] == "settled":
                raise Conflict("费用记录不存在或已结算")
            cx.execute("UPDATE spending SET actual=?,status=?,body=? WHERE id=?",
                       (actual, "settled" if actual is not None else "unknown", json.dumps(json.loads(row[1])|body), call_id))

    def costs(self, pid):
        with self.connect() as cx:
            return [dict(r) | {"body": json.loads(r["body"])} for r in cx.execute("SELECT * FROM spending WHERE project=?", (pid,))]

    def blob(self, raw):
        key = digest(raw)
        path = self.root / "blobs" / key
        path.parent.mkdir(exist_ok=True)
        if path.exists():
            if digest(path.read_bytes()) != key:
                raise Conflict("原件摘要冲突")
        else:
            with path.open("xb") as f:
                f.write(raw)
        return key

    def read_blob(self, key):
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("无效资源身份")
        raw = (self.root / "blobs" / key).read_bytes()
        if digest(raw) != key:
            raise Conflict("原件字节已经变化")
        return raw
