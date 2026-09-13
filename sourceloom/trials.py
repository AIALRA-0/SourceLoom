"""Persistent trial limits, independent of browser and worker restarts."""

import time

from .store import Conflict, digest


def check(store, config):
    campaign=config.get('trial_campaign')
    if not campaign:
        return
    with store.connect() as cx:
        cx.execute('''CREATE TABLE IF NOT EXISTS trial_campaigns(
            id TEXT PRIMARY KEY, started REAL NOT NULL)''')
        cx.execute('''CREATE TABLE IF NOT EXISTS trial_batches(
            campaign TEXT, id TEXT, started REAL NOT NULL, reason TEXT NOT NULL,
            last_fault TEXT, consecutive INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(campaign,id))''')
        cx.execute('BEGIN IMMEDIATE')
        now=time.time()
        first=cx.execute('SELECT min(created) FROM spending WHERE created>0').fetchone()[0]
        cx.execute('INSERT OR IGNORE INTO trial_campaigns VALUES(?,?)',
                   (campaign['id'],first or now))
        cx.execute('INSERT OR IGNORE INTO trial_batches(campaign,id,started,reason) VALUES(?,?,?,?)',
                   (campaign['id'],campaign['batch'],now,campaign['reason']))
        started=cx.execute('SELECT started FROM trial_campaigns WHERE id=?',(campaign['id'],)).fetchone()[0]
        batch=cx.execute('SELECT * FROM trial_batches WHERE campaign=? AND id=?',
                         (campaign['id'],campaign['batch'])).fetchone()
        # Settings can lower limits, never raise the approved first-campaign caps.
        if now-started>=min(config.get('trial_total_seconds',10800),10800):
            raise Conflict('累计真实测试已达到 180 分钟上限，未发送新请求')
        if now-batch['started']>=min(config.get('trial_batch_seconds',3600),3600):
            raise Conflict('本批真实测试已达到 60 分钟上限，未发送新请求')
        if batch['consecutive']>=2:
            raise Conflict('本批同类失败已连续发生两次，先修正原因，未发送新请求')


def outcome(store,config,job,status):
    campaign=config.get('trial_campaign')
    if not campaign or status not in {'completed','failed','uncertain','needs_attention'}:
        return
    fault=None if status=='completed' else digest([job.get('stage'),job.get('error',status)])
    with store.connect() as cx:
        if not cx.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trial_batches'").fetchone():
            return
        row=cx.execute('SELECT last_fault,consecutive FROM trial_batches WHERE campaign=? AND id=?',
                       (campaign['id'],campaign['batch'])).fetchone()
        if row:
            count=0 if fault is None else row['consecutive']+1 if row['last_fault']==fault else 1
            cx.execute('UPDATE trial_batches SET last_fault=?,consecutive=? WHERE campaign=? AND id=?',
                       (fault,count,campaign['id'],campaign['batch']))
