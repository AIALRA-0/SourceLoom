"""Document-centered API, including durable production and saved revisions."""

import copy
import json
import time
import html
import re
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import HTMLResponse, Response
from .checks import inspect_draft
from .durable import Queue
from .export import render, safe_html
from .skills import deploy_skill
from .store import Conflict, identity, digest
from .writing import canonical, available_draft
from .reading import reader_summary, presentation


def source_locations(project):
    """Small, read-only hints for finding draft evidence in the saved original."""
    inventory=project['inventory']
    objects={o['id']:o for o in inventory['objects']}
    originals=inventory['originals']
    locations={}
    for block in project['draft']['blocks']:
        candidates=[e['source_id'] for e in block.get('evidence',[]) if e.get('source_id')]
        candidates.extend(block.get('object_ids',[]))
        for sid in dict.fromkeys(candidates):
            source=objects.get(sid)
            if not source:continue
            original=next((o for o in originals if source['locator']==o['name'] or source['locator'].startswith(o['name']+'/')),None)
            if not original:continue
            quote=next((e.get('quote','') for e in block.get('evidence',[]) if e.get('source_id')==sid and e.get('quote')),None)
            quote=quote or source.get('text','')
            quote=' '.join(quote.split())
            page=re.search(r'/page\[(\d+)\]',source['locator'])
            location={
                'source_id':sid,'quote':quote,'file':original['name'],
                'original_key':original['sha256'],'page':int(page[1]) if page else None,
                'format':Path(original['name']).suffix.lower().lstrip('.'),
            }
            if block['id'] not in locations:
                locations[block['id']]=location|{'alternatives':[]}
            locations[block['id']]['alternatives'].append(location)
    return locations


def register(app, store, config):
    queue=Queue(store)

    def latest_production(pid):
        with store.connect() as cx:
            row=cx.execute("SELECT body FROM jobs WHERE project=? AND role='production' ORDER BY created DESC LIMIT 1",(pid,)).fetchone()
        return json.loads(row[0]) if row else None

    @app.get('/api/projects/{pid}/reader')
    def reader(pid:str):
        return reader_summary(store.get(pid),store.costs(pid))

    @app.patch('/api/projects/{pid}/reading-settings')
    def reading_settings(pid:str,body:dict):
        value=body.get('heading_numbering')
        if value not in {'preserve','numbered','none'}:raise ValueError('标题编号选项无效')
        p=store.change(pid,lambda p:p.update(heading_numbering=value))
        return {'heading_numbering':p['heading_numbering']}

    @app.get('/api/library')
    def tree(trash:bool=False):
        return queue.tree(trash)

    @app.get('/api/projects/{pid}/estimate')
    def estimate(pid:str):
        p=store.get(pid)
        if not p.get('inventory'):
            raise Conflict('先保存原件才能估算处理规模')
        return Queue.estimate(p['inventory'])|{'document_budget_usd':p['budget_usd']}

    @app.post('/api/library/actions')
    def library_action(body:dict):
        return queue.library.apply(body.get('action'),body.get('items',[]),body.get('destination'),body.get('confirm',False))

    @app.post('/api/library/folders')
    def folder(body:dict):
        return queue.folder(str(body.get('name','')),body.get('parent'))

    @app.patch('/api/library/folders/{fid}')
    def change_folder(fid:str,body:dict):
        return queue.folder(str(body.get('name','')),body.get('parent'),fid,body.get('revision'))

    @app.delete('/api/library/folders/{fid}')
    def remove_folder(fid:str):
        queue.delete_folder(fid)
        return {'deleted':True}

    @app.patch('/api/library/documents/{pid}')
    def change_document(pid:str,body:dict):
        return queue.edit_document(pid,body['library_revision'],title=body.get('title'),
                                   folder=body.get('folder'),move='folder' in body,trashed=body.get('trashed'),budget_cny=body.get('budget_cny'))

    @app.post('/api/projects/{pid}/produce')
    def produce(pid:str):
        if config.get('generation_pause_reason'):
            raise Conflict(config['generation_pause_reason'])
        if config['provider']=='manual':
            raise Conflict('尚未配置自动生成通道')
        bundle=deploy_skill(config['writing_skill_dir'],store.root)
        return queue.enqueue(pid,bundle)

    @app.get('/api/projects/{pid}/production')
    def production(pid:str):
        p=store.get(pid)
        from .progress import summary
        from .intake_jobs import IntakeQueue
        incoming=IntakeQueue(store,config).latest(pid)
        with store.connect() as cx:
            later_production=bool(incoming and cx.execute("SELECT 1 FROM jobs WHERE project=? AND role='production' AND created>=? LIMIT 1",(pid,incoming['created'])).fetchone())
        if incoming and (p.get('active_job')==incoming['id'] or (not p.get('active_job') and not later_production and incoming['status']!='completed')):
            return {k:incoming.get(k) for k in ('id','status','stage','created','started','finished')}|{
                'error':incoming.get('error') or incoming.get('generation_error'),'progress':summary(incoming),
                'has_output':bool(p.get('draft')),'formal':False,'call_count':0,'intake':True,
                'received_files':[{'name':f['name'],'bytes':f['bytes'],'url':f"/api/projects/{pid}/intakes/{incoming['id']}/original/{f['sha256']}"} for f in incoming.get('files',[])]}
        # Poll progress without loading the complete, potentially large model history.
        from .progress import FIELDS,latest
        with store.connect() as cx:
            row=latest(cx,pid)
        j=dict(zip(FIELDS,json.loads(row[0]))) if row else None
        if j:
            j['call_count']=row[1];j['has_candidate']=bool(row[3]);j['unit_count']=row[4]
            messages={'codex_quota_exhausted':'转发服务报告本次所用通道额度耗尽，未返回正文；这不代表全部账号或订阅都不可用',
                      'chatgpt_delivery_uncertain':'无法确认聊天消息是否送达，已保留原请求，未自动重发'}
            if row[2] in messages:j['error']=messages[row[2]]
            if not p.get('draft') and j['has_candidate']:
                from .progress import candidate as saved_candidate
                with store.connect() as cx:
                    candidate=saved_candidate(cx,j['id'])
                j['draft']=json.loads(candidate[0]);j['plan']=json.loads(candidate[1]) if candidate[1] else None
            elif not p.get('draft') and j['stage']=='writer' and j['status'] in {'failed','needs_attention'}:
                # Only a returned but uncomposed writer result needs the full fallback path.
                j=latest_production(pid)
        if not j:
            return {'status':'not_started'}
        try:
            available=None if p.get('draft') else available_draft(j)
        except ValueError:
            available=None
        displayed=p.get('draft') or available
        output_text=canonical(displayed) if displayed else ''
        newer_candidate=(j['status'] in {'failed','uncertain','cancelled'} and
            bool(j.get('has_candidate') or j.get('draft',{}).get('blocks')) and
            (p.get('production') or {}).get('job')!=j['id'])
        candidate_url=f"/api/projects/{pid}/attempts/{j['id']}/output" if newer_candidate else None
        j['role']='production'
        return {k:j.get(k) for k in ('id','status','stage','created','started','finished','error','quality_issues','repair_rounds')} | {
            'call_count':j.get('call_count',len(j.get('calls',[]))),'has_output':bool(displayed),
            'output_chars':len(output_text),
            'output_digest':digest(output_text.encode()) if displayed else None,
            'candidate_url':candidate_url,
            'candidate_markdown_url':candidate_url+'?format=markdown' if candidate_url else None,
            'progress':summary(j),
            'formal':(p.get('production') or {}).get('status')=='completed'}

    @app.get('/api/projects/{pid}/intakes/{jid}/original/{key}')
    def intake_original(pid:str,jid:str,key:str):
        store.get(pid);job=store.job(jid)
        if job.get('project')!=pid or job.get('role')!='intake':raise KeyError(jid)
        file=next((f for f in job.get('files',[]) if f['sha256']==key),None)
        if not file:raise KeyError(key)
        return Response(store.read_blob(key),media_type='application/octet-stream',
            headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(file['name'])})

    @app.get('/api/projects/{pid}/attempts/{jid}/output')
    def attempt_output(pid:str,jid:str,format:str='html'):
        p=store.get(pid);job=store.job(jid)
        if job.get('project')!=pid or job.get('role')!='production':raise KeyError(jid)
        draft=available_draft(job)
        if not draft:raise Conflict('本次尚未生成可阅读正文')
        if format=='markdown':
            from .media import reading_draft
            return Response(canonical(reading_draft(p|{'draft':draft,'inventory':job['inventory']})).encode(),media_type='text/markdown',
                headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(p['title']+'-本次新稿.md')})
        if format!='html':raise ValueError('未知正文格式')
        candidate=p|dict(draft=draft,inventory=job['inventory'],plan=job['plan'],
                         accepted_revision=None,production={'status':'needs_review'})
        content=render(candidate,lambda key:f'/api/projects/{pid}/assets/{key}')
        return HTMLResponse('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/article.css"></head><body><p role="status">本次生成的新稿，尚未通过全部核对</p>'+content+'</body></html>',
            headers={'Content-Security-Policy':"sandbox allow-same-origin; default-src 'none'; img-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'"})

    @app.post('/api/projects/{pid}/rewrite')
    def rewrite(pid:str):
        if config.get('generation_pause_reason'):raise Conflict(config['generation_pause_reason'])
        if config['provider']=='manual':raise Conflict('尚未配置自动生成通道')
        return queue.rewrite_existing(pid,deploy_skill(config['writing_skill_dir'],store.root))

    @app.get('/api/projects/{pid}/editable')
    def editable(pid:str):
        p=store.get(pid)
        if not p.get('draft'):
            j=latest_production(pid)
            if j:
                p=p|dict(draft=available_draft(j),inventory=j.get('inventory'),plan=j.get('plan'))
        if not p.get('draft'):
            raise Conflict('尚无可编辑正文')
        return {k:p.get(k) for k in ('id','revision','draft','active_job')}

    @app.get('/api/projects/{pid}/source-locations')
    def locations(pid:str):
        p=store.get(pid)
        if not p.get('draft'):
            j=latest_production(pid)
            if j:
                p=p|dict(draft=available_draft(j))
        return source_locations(p) if p.get('draft') and p.get('inventory') else {}

    @app.get('/api/projects/{pid}/output')
    def output(pid:str,format:str='html',numbering:str|None=None):
        p=store.get(pid)
        if not p.get('draft'):
            j=latest_production(pid)
            if not j:
                raise Conflict('尚未生成正文')
            available=available_draft(j)
            if not available:
                raise Conflict('尚未生成正文')
            p=p|dict(draft=available,inventory=j['inventory'],plan=j['plan'])
        p=presentation(p,numbering)
        if format=='markdown':
            from .media import reading_draft
            return Response(canonical(reading_draft(p)).encode(),media_type='text/markdown',
                            headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(p['title']+'.md')})
        if format!='html':
            raise ValueError('未知正文格式')
        content=render(p,lambda key:f'/api/projects/{pid}/assets/{key}')
        return HTMLResponse('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/article.css"></head><body>'+content+'</body></html>',
            headers={'Content-Security-Policy':"sandbox allow-same-origin; default-src 'none'; img-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'"})

    @app.get('/api/projects/{pid}/original/{key}')
    def original(pid:str,key:str,name:str|None=None):
        p=store.get(pid)
        original=next((x for x in p['inventory']['originals'] if x['sha256']==key and (name is None or x['name']==name)),None)
        if not original:
            raise KeyError(key)
        return Response(store.read_blob(key),media_type='application/octet-stream',
                        headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(original['name'])})

    @app.get('/api/projects/{pid}/original-view/{key}')
    def original_view(pid:str,key:str,view:str='original'):
        from markdown_it import MarkdownIt
        from bs4 import BeautifulSoup
        from .ingest import decode
        p=store.get(pid)
        original=next((x for x in p['inventory']['originals'] if x['sha256']==key),None)
        if original is None:
            raise KeyError(key)
        raw=store.read_blob(key)
        suffix=Path(original['name']).suffix.lower()
        if suffix=='.pdf' and view!='text':
            return Response(raw,media_type='application/pdf',headers={'Content-Disposition':'inline'})
        if suffix in {'.md','.markdown','.html','.htm','.txt'}:
            text=decode(raw)
            if suffix in {'.md','.markdown'}:
                text=MarkdownIt('commonmark',{'html':True}).enable('table').render(text)
            elif suffix=='.txt':
                text='<pre>'+html.escape(text)+'</pre>'
            parsed=BeautifulSoup(text,'html.parser')
            assets={o.get('target'):o.get('resource_id') for o in p['inventory']['objects'] if o['kind']=='image'}
            for pic in parsed.find_all('img'):
                asset=assets.get(pic.get('src'))
                if asset:
                    pic['src']='assets/'+asset
            parsed=BeautifulSoup(safe_html(str(parsed)),'html.parser')
            for pic in parsed.select('img[src^="assets/"]'):
                pic['src']=f'/api/projects/{pid}/assets/'+pic['src'][7:]
            content=str(parsed)
        else:
            content='<p>下面显示本原件已提取的文字，可点击文字定位改写正文；原始排版请查看原始文件</p>'
            for o in p['inventory']['objects']:
                if not (o['locator']==original['name'] or o['locator'].startswith(original['name']+'/')):continue
                page=re.search(r'/page\[(\d+)\]',o['locator'])
                if page:content+='<h2>第 '+page[1]+' 页</h2>'
                content+='<pre data-source-id="'+html.escape(o['id'],quote=True)+'">'+html.escape(o.get('text',''))+'</pre>'
        return HTMLResponse('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/article.css"></head><body>'+content+'</body></html>',
            headers={'Content-Security-Policy':"sandbox allow-same-origin; default-src 'none'; img-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'"})

    @app.get('/api/projects/{pid}/versions')
    def versions(pid:str):
        return queue.versions(pid)

    @app.get('/api/projects/{pid}/versions/{vid}')
    def version(pid:str,vid:str):
        with store.connect() as cx:
            row=cx.execute('SELECT body FROM library_versions WHERE id=? AND project=?',(vid,pid)).fetchone()
        if not row:raise KeyError(vid)
        old=json.loads(row[0])
        return {'revision':old['revision'],'draft':old.get('draft'),'title':old['title']}

    @app.post('/api/projects/{pid}/preview-edit')
    def preview_edit(pid:str,body:dict):
        p=store.get(pid)
        if not p.get('draft'):raise Conflict('没有可预览的正文')
        blocks={b['id']:b for b in p['draft']['blocks']}
        for edit in body.get('edits',[]):
            if edit['block_id'] not in blocks:raise ValueError('正文块不存在')
            blocks[edit['block_id']]['markdown']=str(edit['markdown'])
        return {'html':render(presentation(p),lambda key:f'/api/projects/{pid}/assets/{key}')}

    @app.post('/api/projects/{pid}/edit')
    def edit(pid:str,body:dict):
        with store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM projects WHERE id=?',(pid,)).fetchone()
            if not row:
                raise KeyError(pid)
            p=json.loads(row[0])
            if p['active_job'] or p['revision']!=body['revision']:
                raise Conflict('正文版本已变化或仍在处理中，未覆盖')
            if not p.get('draft'):
                last=cx.execute("SELECT body FROM jobs WHERE project=? AND role='production' ORDER BY created DESC LIMIT 1",(pid,)).fetchone()
                j=json.loads(last[0]) if last else {}
                candidate=available_draft(j) if j else None
                if not candidate:raise Conflict('尚无可编辑正文')
                p.update(draft=candidate,inventory=j['inventory'],plan=j['plan'])
            before=json.dumps(p,ensure_ascii=False)
            existing_issues=inspect_draft(p['inventory'],p['draft'],p['plan'])
            blocks={b['id']:b for b in p['draft']['blocks']}
            if not body.get('edits'):
                raise ValueError('没有提交修改')
            seen=set()
            for e in body['edits']:
                if e['block_id'] not in blocks or e['block_id'] in seen:
                    raise ValueError('修改的正文块不存在或重复')
                seen.add(e['block_id'])
                blocks[e['block_id']]['markdown']=str(e['markdown'])
            # Partial candidates remain editable, but an edit must not create
            # any new structural or protected-object violation.
            if any(issue not in existing_issues for issue in inspect_draft(p['inventory'],p['draft'],p['plan'])):
                raise Conflict('修改破坏了原对象或来源关系，原版本已保留')
            cx.execute('INSERT INTO library_versions VALUES(?,?,?,?,?)',(identity(),pid,time.time(),'手动编辑前',before))
            p.update(revision=p['revision']+1,review=None,accepted_revision=None,state='edited',
                     production=dict(status='needs_review',automatic=False,manual_edits=p.get('production',{}).get('manual_edits',0)+1))
            cx.execute('INSERT INTO revisions VALUES(?,?,?)',(pid,p['revision'],json.dumps(p['draft'],ensure_ascii=False)))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),pid))
        return p

    @app.post('/api/projects/{pid}/versions/{vid}/restore')
    def restore(pid:str,vid:str,body:dict):
        with store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM projects WHERE id=?',(pid,)).fetchone()
            history=cx.execute('SELECT body FROM library_versions WHERE id=? AND project=?',(vid,pid)).fetchone()
            if not row or not history:
                raise KeyError(vid)
            p,old=json.loads(row[0]),json.loads(history[0])
            if p['active_job'] or p['revision']!=body['revision']:
                raise Conflict('材料正在处理或当前版本已变化')
            cx.execute('INSERT INTO library_versions VALUES(?,?,?,?,?)',(identity(),pid,time.time(),'恢复历史版本前',row[0]))
            for field in ('inventory','plan','draft','review','production'):
                p[field]=copy.deepcopy(old.get(field))
            p.update(revision=p['revision']+1,accepted_revision=None,review=None,state='restored',
                     production=dict(status='needs_review',automatic=False,manual_edits=1))
            if p['draft']:
                cx.execute('INSERT INTO revisions VALUES(?,?,?)',(pid,p['revision'],json.dumps(p['draft'],ensure_ascii=False)))
            cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(p,ensure_ascii=False),pid))
        return p

    @app.post('/api/projects/{pid}/duplicate')
    def duplicate(pid:str):
        p=store.get(pid)
        new=store.create(p['title']+' · 副本',p['mode'],p['budget_usd'])
        inv=copy.deepcopy(p.get('inventory'))
        if inv and not p.get('draft'):
            inv.update(frozen=False,inventory_review=None)
            inv['digest']=digest({k:v for k,v in inv.items() if k!='digest'})
        latest=latest_production(pid)
        provenance={'project':pid,'revision':p['revision'],'inventory_job':latest['id'] if latest else p.get('copied_from',{}).get('inventory_job')}
        return store.change(new['id'],lambda n:n.update(inventory=inv,goal=p['goal'],folder=p.get('folder'),
            budget_cny=p.get('budget_cny'),
            draft=copy.deepcopy(p.get('draft')),plan=copy.deepcopy(p.get('plan')),
            heading_numbering=p.get('heading_numbering','preserve'),copied_from=provenance,
            state='generated' if p.get('draft') else 'inventoried' if inv else 'collecting'))

    return queue
