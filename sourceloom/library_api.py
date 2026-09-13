"""Document-centered API, including durable production and saved revisions."""

import copy
import json
import time
import html
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import HTMLResponse, Response
from .checks import inspect_draft
from .durable import Queue
from .export import render, safe_html
from .skills import deploy_skill
from .store import Conflict, identity, digest
from .writing import canonical, available_draft


def register(app, store, config):
    queue=Queue(store)

    @app.get('/api/library')
    def tree(trash:bool=False):
        return queue.tree(trash)

    @app.post('/api/library/folders')
    def folder(body:dict):
        return queue.folder(str(body.get('name','')),body.get('parent'))

    @app.patch('/api/library/folders/{fid}')
    def change_folder(fid:str,body:dict):
        return queue.folder(str(body.get('name','')),body.get('parent'),fid)

    @app.delete('/api/library/folders/{fid}')
    def remove_folder(fid:str):
        queue.delete_folder(fid)
        return {'deleted':True}

    @app.patch('/api/library/documents/{pid}')
    def change_document(pid:str,body:dict):
        return queue.edit_document(pid,body['library_revision'],title=body.get('title'),
                                   folder=body.get('folder'),move='folder' in body,trashed=body.get('trashed'))

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
        jobs=[j for j in store.jobs(pid) if j['role']=='production']
        if not jobs:
            return {'status':'not_started'}
        j=jobs[-1]
        try:
            available=available_draft(j)
        except ValueError:
            available=None
        return {k:j.get(k) for k in ('id','status','stage','created','started','finished','error','quality_issues','repair_rounds')} | {
            'call_count':len(j['calls']),'has_output':bool(available),'formal':(p.get('production') or {}).get('status')=='completed'}

    @app.get('/api/projects/{pid}/output')
    def output(pid:str,format:str='html'):
        p=store.get(pid)
        if not p.get('draft'):
            jobs=[j for j in store.jobs(pid) if j['role']=='production']
            if not jobs:
                raise Conflict('尚未生成正文')
            j=jobs[-1]
            available=available_draft(j)
            if not available:
                raise Conflict('尚未生成正文')
            p=p|dict(draft=available,inventory=j['inventory'],plan=j['plan'])
        if format=='markdown':
            return Response(canonical(p['draft']).encode(),media_type='text/markdown',
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
    def original_view(pid:str,key:str):
        from markdown_it import MarkdownIt
        from bs4 import BeautifulSoup
        from .ingest import decode
        p=store.get(pid)
        original=next((x for x in p['inventory']['originals'] if x['sha256']==key),None)
        if original is None:
            raise KeyError(key)
        raw=store.read_blob(key)
        suffix=Path(original['name']).suffix.lower()
        if suffix=='.pdf':
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
            content='<p>此格式请下载原文件查看，下面只显示已提取的文字</p>'
            content+=''.join('<pre>'+html.escape(o['text'])+'</pre>' for o in p['inventory']['objects'])
        return HTMLResponse('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/article.css"></head><body>'+content+'</body></html>',
            headers={'Content-Security-Policy':"sandbox allow-same-origin; default-src 'none'; img-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'self'"})

    @app.get('/api/projects/{pid}/versions')
    def versions(pid:str):
        return queue.versions(pid)

    @app.post('/api/projects/{pid}/edit')
    def edit(pid:str,body:dict):
        with store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            row=cx.execute('SELECT body FROM projects WHERE id=?',(pid,)).fetchone()
            if not row:
                raise KeyError(pid)
            p=json.loads(row[0])
            if p['active_job'] or p['revision']!=body['revision'] or not p.get('draft'):
                raise Conflict('正文版本已变化或仍在处理中，未覆盖')
            before=json.dumps(p,ensure_ascii=False)
            blocks={b['id']:b for b in p['draft']['blocks']}
            if not body.get('edits'):
                raise ValueError('没有提交修改')
            seen=set()
            for e in body['edits']:
                if e['block_id'] not in blocks or e['block_id'] in seen:
                    raise ValueError('修改的正文块不存在或重复')
                seen.add(e['block_id'])
                blocks[e['block_id']]['markdown']=str(e['markdown'])
            if inspect_draft(p['inventory'],p['draft'],p['plan']):
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
        if inv:
            inv.update(frozen=False,inventory_review=None)
            inv['digest']=digest({k:v for k,v in inv.items() if k!='digest'})
        return store.change(new['id'],lambda n:n.update(inventory=inv,goal=p['goal'],folder=p.get('folder'),state='inventoried' if inv else 'collecting'))

    return queue
