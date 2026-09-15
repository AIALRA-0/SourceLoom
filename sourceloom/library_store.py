"""Transactional folder/document actions with reversible grouped deletion."""

import json
import time
from .store import Conflict, identity


class Library:
    def __init__(self, store):
        self.store=store
        with store.connect() as cx:
            columns={r[1] for r in cx.execute('PRAGMA table_info(library_folders)')}
            for name,definition in [('trashed','INTEGER NOT NULL DEFAULT 0'),('trash_group','TEXT'),('revision','INTEGER NOT NULL DEFAULT 0')]:
                if name not in columns:
                    cx.execute(f'ALTER TABLE library_folders ADD COLUMN {name} {definition}')

    def apply(self, action, items, destination=None, confirm=False):
        if action not in {'move','trash','restore','purge'} or not items:
            raise ValueError('请选择材料和有效操作')
        with self.store.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            folders={r['id']:dict(r) for r in cx.execute('SELECT * FROM library_folders')}
            docs={r['id']:json.loads(r['body']) for r in cx.execute('SELECT id,body FROM projects')}
            changes={'folders':set(),'documents':set()}
            selected=set()
            for item in items:
                kind=item.get('kind');key=item.get('id')
                collection=folders if kind=='folder' else docs if kind=='document' else {}
                if key not in collection:raise KeyError(key)
                if (kind,key) in selected:raise ValueError('操作对象重复')
                selected.add((kind,key))
                version=collection[key].get('revision' if kind=='folder' else 'library_revision',0)
                if item.get('revision')!=version:raise Conflict('材料库已被另一页面修改，请刷新后再操作')
            def descendants(fid):
                result={fid};more=True
                while more:
                    extra={f['id'] for f in folders.values() if f['parent'] in result}-result
                    more=bool(extra);result|=extra
                return result
            folder_ids={key for kind,key in selected if kind=='folder'}
            roots={fid for fid in folder_ids if not any(fid in descendants(other) for other in folder_ids-{fid})}
            direct={key for kind,key in selected if kind=='document'}
            affected_folders=set().union(*(descendants(fid) for fid in roots)) if roots else set()
            affected_docs=direct|{d['id'] for d in docs.values() if d.get('folder') in affected_folders}
            fallback=[]
            if action=='move':
                if destination is not None and (destination not in folders or folders[destination]['trashed']):
                    raise Conflict('目标文件夹不存在或在回收站中')
                if destination in affected_folders:raise Conflict('不能移动到自身或子文件夹')
                for fid in roots:
                    if folders[fid]['trashed']:raise Conflict('先恢复文件夹，再移动')
                    folders[fid]['parent']=destination;changes['folders'].add(fid)
                for did in direct:
                    if docs[did].get('trashed'):raise Conflict('先恢复材料，再移动')
                    if docs[did].get('folder') not in affected_folders:
                        docs[did]['folder']=destination;changes['documents'].add(did)
            elif action=='trash':
                group=identity()
                for fid in affected_folders:
                    if not folders[fid]['trashed']:
                        folders[fid].update(trashed=1,trash_group=group);changes['folders'].add(fid)
                for did in affected_docs:
                    d=docs[did]
                    if not d.get('trashed'):
                        d.update(trashed=True,trash_group=group);changes['documents'].add(did)
                        if d.get('active_job'):
                            cx.execute('UPDATE production_control SET cancel_requested=1 WHERE id=?',(d['active_job'],))
                            row=cx.execute("SELECT body FROM jobs WHERE id=? AND role='intake' AND status IN ('queued','running')",(d['active_job'],)).fetchone()
                            if row:
                                job=json.loads(row[0]);job.update(status='cancelled',finished=time.time())
                                cx.execute("UPDATE jobs SET status='cancelled',body=? WHERE id=?",(json.dumps(job,ensure_ascii=False),job['id']))
                                cx.execute('UPDATE intake_control SET owner=NULL,lease_until=0 WHERE id=?',(job['id'],))
                                d.update(active_job=None,state='cancelled')
            elif action=='restore':
                groups={folders[fid]['trash_group'] for fid in roots if folders[fid]['trashed']}
                for fid in affected_folders:
                    if folders[fid]['trashed'] and folders[fid]['trash_group'] in groups:
                        folders[fid].update(trashed=0,trash_group=None);changes['folders'].add(fid)
                for fid in changes['folders']:
                    parent=folders[fid]['parent']
                    if parent and (parent not in folders or folders[parent]['trashed']):
                        folders[fid]['parent']=None;fallback.append(fid)
                for did in affected_docs:
                    d=docs[did]
                    if d.get('trashed') and (did in direct or d.get('trash_group') in groups):
                        d.update(trashed=False,trash_group=None);changes['documents'].add(did)
                        parent=d.get('folder')
                        if parent and (parent not in folders or folders[parent]['trashed']):
                            d['folder']=None;fallback.append(did)
            else:
                if confirm is not True:raise Conflict('彻底删除需要明确确认')
                if any(not folders[i]['trashed'] for i in affected_folders) or any(not docs[i].get('trashed') for i in affected_docs):
                    raise Conflict('只能彻底删除回收站内的材料')
                for did in affected_docs:
                    if docs[did].get('active_job') or cx.execute("SELECT 1 FROM jobs WHERE project=? AND status IN ('running','queued','uncertain')",(did,)).fetchone():
                        raise Conflict('材料仍有运行或结果不确定的请求，先停止并核对原任务')
                # Billing remains an audit record. It cannot be erased to reset spending limits.
                for did in affected_docs:
                    for table in ('projects','revisions','source_versions','jobs','events','production_control','library_versions'):
                        column='id' if table=='projects' else 'project'
                        cx.execute(f'DELETE FROM {table} WHERE {column}=?',(did,))
                for fid in affected_folders:cx.execute('DELETE FROM library_folders WHERE id=?',(fid,))
                return {'action':action,'documents':len(affected_docs),'folders':len(affected_folders),'moved_to_root':[]}
            for fid in changes['folders']:
                f=folders[fid]
                cx.execute('UPDATE library_folders SET parent=?,trashed=?,trash_group=?,revision=revision+1 WHERE id=?',
                    (f['parent'],f['trashed'],f['trash_group'],fid))
            for did in changes['documents']:
                d=docs[did];d['library_revision']=d.get('library_revision',0)+1
                cx.execute('UPDATE projects SET body=? WHERE id=?',(json.dumps(d,ensure_ascii=False),did))
            return {'action':action,'documents':len(changes['documents']),'folders':len(changes['folders']),'moved_to_root':fallback}
