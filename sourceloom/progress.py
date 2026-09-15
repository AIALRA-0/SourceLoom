"""Observable stages, not a fabricated estimate of time remaining."""
import json
FIELDS=('id','status','stage','created','started','finished','error','quality_issues','repair_rounds','unit_index','inventory_group_index','inventory_group_count','style_part_index','style_part_count')


def install(cx):
    """Maintain a tiny snapshot in the same transaction as every job change."""
    if not cx.in_transaction:cx.execute('BEGIN IMMEDIATE')
    cx.execute('CREATE TABLE IF NOT EXISTS job_progress(id TEXT PRIMARY KEY,project TEXT,created REAL,body TEXT,call_count INTEGER,error_code TEXT,has_candidate INTEGER,unit_count INTEGER)')
    cx.execute('CREATE TABLE IF NOT EXISTS job_candidates(id TEXT PRIMARY KEY,draft TEXT,plan TEXT)')
    cx.execute('CREATE INDEX IF NOT EXISTS jobs_project_role_created ON jobs(project,role,created DESC)')
    cx.execute('CREATE TRIGGER IF NOT EXISTS production_progress_delete AFTER DELETE ON jobs BEGIN DELETE FROM job_progress WHERE id=OLD.id; END')
    cx.execute('CREATE TRIGGER IF NOT EXISTS production_candidate_delete AFTER DELETE ON jobs BEGIN DELETE FROM job_candidates WHERE id=OLD.id; END')
    for event in ('INSERT','UPDATE'):
        values=expressions('NEW.body')
        cx.execute(f'DROP TRIGGER IF EXISTS production_progress_{event.lower()}')
        cx.execute(f'DROP TRIGGER IF EXISTS production_progress_{event.lower()}_v2')
        cx.execute(f"""CREATE TRIGGER IF NOT EXISTS production_progress_{event.lower()}_v3 AFTER {event} ON jobs
            WHEN NEW.role='production' BEGIN
            INSERT INTO job_progress VALUES(NEW.id,NEW.project,NEW.created,{values})
            ON CONFLICT(id) DO UPDATE SET body=excluded.body,call_count=excluded.call_count,error_code=excluded.error_code,has_candidate=excluded.has_candidate,unit_count=excluded.unit_count;
            INSERT INTO job_candidates VALUES(NEW.id,json_extract(NEW.body,'$.draft'),json_extract(NEW.body,'$.plan'))
            ON CONFLICT(id) DO UPDATE SET draft=excluded.draft,plan=excluded.plan;
            END""")


def expressions(body):
    return ('json_extract('+body+','+','.join("'$."+f+"'" for f in FIELDS)+'),'
        +f"json_array_length({body},'$.calls'),json_extract({body},'$.calls[#-1].error_code'),json_array_length({body},'$.draft.blocks'),json_array_length({body},'$.plan.units')")


def latest(cx,pid):
    row=cx.execute("SELECT id FROM jobs WHERE project=? AND role='production' ORDER BY created DESC LIMIT 1",(pid,)).fetchone()
    if not row:return None
    jid=row[0]
    found=cx.execute('SELECT body,call_count,error_code,has_candidate,unit_count FROM job_progress WHERE id=?',(jid,)).fetchone()
    if found and len(json.loads(found[0]))==len(FIELDS):return found
    # Old jobs are migrated once when viewed, not rescanned on every poll or startup.
    cx.execute('INSERT OR REPLACE INTO job_progress SELECT id,project,created,'+expressions('body')+' FROM jobs WHERE id=?',(jid,))
    return cx.execute('SELECT body,call_count,error_code,has_candidate,unit_count FROM job_progress WHERE id=?',(jid,)).fetchone()


def candidate(cx,jid):
    """Read the saved draft without parsing its accumulated model history."""
    row=cx.execute('SELECT draft,plan FROM job_candidates WHERE id=?',(jid,)).fetchone()
    if row:return row
    # Old snapshots migrate only once; subsequent polls read the small table.
    cx.execute("INSERT OR IGNORE INTO job_candidates SELECT id,json_extract(body,'$.draft'),json_extract(body,'$.plan') FROM jobs WHERE id=? AND role='production'",(jid,))
    return cx.execute('SELECT draft,plan FROM job_candidates WHERE id=?',(jid,)).fetchone()


STEPS=['保存原件','清点信息','整理结构','改写正文','核对与修正','交付结果']
STAGES={'fetch':0,'intake':0,'received':1,'visual_extract':1,'visual_audit':1,'inventory':1,'inventory_audit':1,
        'planner':2,'plan_review':2,'writer':3,'style':4,'fidelity':4,'repair':4,'teaching':4,
        'teaching_replan':4,'teaching_replan_review':4,'publish':5}


def summary(job):
    stage=job.get('stage','');status=job.get('status');current=STAGES.get(stage,4)
    if status=='completed' and job.get('role')=='production':current=5
    detail={'fetch':'正在获取网页正文和附件','intake':'原始文件已收到，正在读取文字、图片和表格',
            'visual_extract':'正在识别原文中的图片和页面','visual_audit':'正在核对图片和页面的识别结果',
            'inventory':'正在清点原文中的信息','inventory_audit':'正在检查信息清单是否遗漏',
            'planner':'正在整理文章结构','plan_review':'正在核对文章结构',
            'style':'正在核对术语、缩写和写作格式','fidelity':'正在逐项对照原文与新稿',
            'repair':'正在修正核对发现的问题','teaching':'正在检查全文是否易读、连贯',
            'publish':'正在保存正文与阅读包'}.get(stage,'正在处理材料')
    units=job.get('unit_count') or len(job.get('plan',{}).get('units',[]))
    if stage=='writer':detail=f"正在改写第 {min(job.get('unit_index',0)+1,units)} / {units} 部分" if units else '正在生成改写正文'
    if stage=='inventory' and job.get('inventory_group_count'):detail=f"正在清点第 {min(job.get('inventory_group_index',0)+1,job['inventory_group_count'])} / {job['inventory_group_count']} 组原文"
    if stage=='style' and (job.get('style_part_count') or 0)>1:detail=f"正在核对第 {(job.get('style_part_index') or 0)+1} / {job['style_part_count']} 组要求，每组均阅读完整正文"
    if status=='queued':detail='任务已保存，正在等待后台处理'
    if status=='completed':detail='正文已保存，可以阅读和下载' if job.get('role')=='production' else '原件已保存'
    if status=='needs_attention':detail='已有结果已保存，部分内容仍需核对'
    if status in {'failed','uncertain','cancelled'}:detail='处理已暂停，已完成的结果仍保留'
    return {'steps':STEPS,'current':current,'label':detail,'started':job.get('started',job.get('created')),
            'active':status in {'queued','running'},'completed':status=='completed' and job.get('role')=='production'}
