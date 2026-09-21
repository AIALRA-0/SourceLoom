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

    @contextmanager
    def connect(self):
        cx = sqlite3.connect(self.db, timeout=20)
        cx.row_factory = sqlite3.Row
        try:
            with cx:
                yield cx
        finally:
            cx.close()

    def create(self, title, mode="rewrite", budget=0.5):
        pid = identity()
        body = dict(id=pid, title=title, mode=mode, created=time.time(), revision=0,
                    state="collecting", inventory=None, plan=None, draft=None, review=None,
                    accepted_revision=None, repair_rounds=0, budget_usd=budget, max_calls=12,
                    active_job=None, goal="完整保留材料，并按必要前提循序讲清", research=None)
        with self.connect() as cx:
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
                total_budget=None, subscription_calls=None):
        if amount < 0:
            raise ValueError("费用预留不能为负数")
        with self.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
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
