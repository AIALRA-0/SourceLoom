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
        # No assistant-imposed campaign wall clock. Only explicit configured limits apply.
        if config.get('trial_total_seconds',0)>0 and now-started>=config['trial_total_seconds']:
            raise Conflict('达到明确配置的试验时间限制，记录保留')
        if config.get('trial_batch_seconds',0)>0 and now-batch['started']>=config['trial_batch_seconds']:
            raise Conflict('达到明确配置的批次时间限制，记录保留')
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
