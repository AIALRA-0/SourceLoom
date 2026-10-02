"""Lightweight, transactional organization of processor material identities.

Content remains in projects/blobs. The SQL-maintained projection is navigation
metadata, never a second material store or a generation pipeline.
"""
import json
import time
import unicodedata

from .library_store import Library
from .store import Conflict, digest, identity


def name(value):
    if not isinstance(value, str):
        raise ValueError('名称必须是文字')
    value = value.strip()
    if not value or len(value) > 180 or value in {'.', '..'} or any(ord(c) < 32 or c in '/\\' for c in value):
        raise ValueError('名称须为 1–180 字符，且不能包含路径分隔符或控制字符')
    return value


def name_key(value):
    return unicodedata.normalize('NFKC', value).casefold()


class ProcessorTree:
    def receipt(self, request_id):
        """Read a committed management result without repeating its mutation."""
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError('无效操作身份')
        with self.store.connect() as cx:
            row = cx.execute('SELECT result FROM processor_tree_receipts WHERE id=?', (request_id,)).fetchone()
        return {'request_id': request_id, 'status': 'completed', 'result': json.loads(row[0])} if row else {
            'request_id': request_id, 'status': 'not_found'}

    def __init__(self, store):
        self.store = getattr(store, 'personal', store)
        self.library = Library(self.store)
        with self.library.transaction() as cx:
            present = cx.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='processor_material_meta'").fetchone()
            if present:
                columns={r[1] for r in cx.execute('PRAGMA table_info(processor_material_meta)')}
                for column, definition in [('state','TEXT'),('content_revision','INTEGER NOT NULL DEFAULT 0')]:
                    if column not in columns:
                        cx.execute('ALTER TABLE processor_material_meta ADD COLUMN '+column+' '+definition)
            old_projection = cx.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name IN ('processor_meta_insert','processor_meta_insert_v2')").fetchone()
            cx.executescript('''
                DROP TRIGGER IF EXISTS processor_meta_insert;
                DROP TRIGGER IF EXISTS processor_meta_update;
                DROP TRIGGER IF EXISTS processor_meta_delete;
                DROP TRIGGER IF EXISTS processor_meta_insert_v2;
                DROP TRIGGER IF EXISTS processor_meta_update_v2;
                DROP TRIGGER IF EXISTS processor_meta_delete_v2;
                CREATE TABLE IF NOT EXISTS processor_material_meta(
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,parent TEXT,revision INTEGER NOT NULL,
                    archived INTEGER NOT NULL,trashed INTEGER NOT NULL,created REAL,trash_group TEXT,
                    trash_parent TEXT,trashed_at REAL,state TEXT,content_revision INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS processor_meta_parent ON processor_material_meta(parent,trashed,archived);
                CREATE INDEX IF NOT EXISTS processor_folder_parent ON library_folders(scope,parent,trashed);
                CREATE TABLE IF NOT EXISTS processor_tree_receipts(
                    id TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL,created REAL NOT NULL);
                CREATE TRIGGER IF NOT EXISTS processor_meta_insert_v3 AFTER INSERT ON projects
                    WHEN json_type(NEW.body,'$.processor')='object'
                    BEGIN
                        INSERT OR REPLACE INTO processor_material_meta VALUES(
                            NEW.id,COALESCE(json_extract(NEW.body,'$.library.display_name'),json_extract(NEW.body,'$.title')),json_extract(NEW.body,'$.folder'),
                            COALESCE(json_extract(NEW.body,'$.library_revision'),0),
                            COALESCE(json_extract(NEW.body,'$.library.archived'),0),
                            COALESCE(json_extract(NEW.body,'$.trashed'),0),json_extract(NEW.body,'$.created'),
                            json_extract(NEW.body,'$.trash_group'),json_extract(NEW.body,'$.trash_parent'),
                            json_extract(NEW.body,'$.trashed_at'),json_extract(NEW.body,'$.state'),COALESCE(json_extract(NEW.body,'$.revision'),0));
                    END;
                CREATE TRIGGER IF NOT EXISTS processor_meta_update_v3 AFTER UPDATE OF body ON projects
                    BEGIN
                        DELETE FROM processor_material_meta WHERE id=NEW.id AND json_type(NEW.body,'$.processor') IS NOT 'object';
                        INSERT OR REPLACE INTO processor_material_meta SELECT
                            NEW.id,COALESCE(json_extract(NEW.body,'$.library.display_name'),json_extract(NEW.body,'$.title')),json_extract(NEW.body,'$.folder'),
                            COALESCE(json_extract(NEW.body,'$.library_revision'),0),
                            COALESCE(json_extract(NEW.body,'$.library.archived'),0),
                            COALESCE(json_extract(NEW.body,'$.trashed'),0),json_extract(NEW.body,'$.created'),
                            json_extract(NEW.body,'$.trash_group'),json_extract(NEW.body,'$.trash_parent'),
                            json_extract(NEW.body,'$.trashed_at'),json_extract(NEW.body,'$.state'),COALESCE(json_extract(NEW.body,'$.revision'),0) WHERE json_type(NEW.body,'$.processor')='object';
                    END;
                CREATE TRIGGER IF NOT EXISTS processor_meta_delete_v3 AFTER DELETE ON projects
                    BEGIN DELETE FROM processor_material_meta WHERE id=OLD.id; END;
            ''')
            if not present or old_projection:
                cx.execute('''INSERT OR REPLACE INTO processor_material_meta
                    SELECT id,COALESCE(json_extract(body,'$.library.display_name'),json_extract(body,'$.title')),json_extract(body,'$.folder'),
                    COALESCE(json_extract(body,'$.library_revision'),0),COALESCE(json_extract(body,'$.library.archived'),0),
                    COALESCE(json_extract(body,'$.trashed'),0),json_extract(body,'$.created'),json_extract(body,'$.trash_group'),
                    json_extract(body,'$.trash_parent'),json_extract(body,'$.trashed_at'),json_extract(body,'$.state'),COALESCE(json_extract(body,'$.revision'),0)
                    FROM projects WHERE json_type(body,'$.processor')='object' ''')

    @staticmethod
    def _rows(cx):
        folders = {r['id']: dict(r) for r in cx.execute("SELECT * FROM library_folders WHERE scope='processor'")}
        docs = {r['id']: dict(r) for r in cx.execute('SELECT * FROM processor_material_meta')}
        return folders, docs

    @staticmethod
    def _paths(folders):
        paths = {None: ''}
        for key in folders:
            pending, seen, current = [], set(), key
            while current not in paths and current in folders and current not in seen:
                seen.add(current); pending.append(current); current = folders[current]['parent']
            base = paths.get(current, '')
            for fid in reversed(pending):
                base = (base + '/' + folders[fid]['name']).lstrip('/')
                paths[fid] = base
        return paths

    def material_summaries(self, archived=False, limit=None):
        with self.store.connect() as cx:
            sql='SELECT * FROM processor_material_meta WHERE trashed=0 AND archived=? ORDER BY created DESC,id'
            parameters=[int(bool(archived))]
            if limit is not None:
                sql+=' LIMIT ?';parameters.append(max(1,int(limit)))
            return [dict(id=r['id'],title=r['title'],state=r['state'],revision=r['content_revision'],
                         library_revision=r['revision'],created=r['created'],folder=r['parent'],trashed=False,
                         library=dict(kind='personal',archived=bool(r['archived']))) for r in cx.execute(sql,parameters)]

    @staticmethod
    def _children(cx, space, parent_id, offset, limit, sort):
        """Expand one directory without materializing unrelated tree metadata."""
        chain=[];seen=set();ancestor=parent_id
        while ancestor is not None and ancestor not in seen:
            seen.add(ancestor)
            row=cx.execute("SELECT id,parent,name FROM library_folders WHERE id=? AND scope='processor'",(ancestor,)).fetchone()
            if not row:break
            chain.append(row['name']);ancestor=row['parent']
        path='/'.join(reversed(chain))
        folder_where="scope='processor' AND parent=? AND trashed=?"
        folder_params=(parent_id,int(space=='trash'))
        folder_total=0 if space=='archive' else cx.execute('SELECT COUNT(*) FROM library_folders WHERE '+folder_where,folder_params).fetchone()[0]
        doc_where='parent=? AND '+('trashed=1' if space=='trash' else 'trashed=0 AND archived='+str(int(space=='archive')))
        doc_total=cx.execute('SELECT COUNT(*) FROM processor_material_meta WHERE '+doc_where,(parent_id,)).fetchone()[0]
        # Use the same NFKC/casefold folder order as root/search projections.
        cx.create_collation('PROCESSOR_NAME',lambda a,b:(name_key(a)>name_key(b))-(name_key(a)<name_key(b)))
        rows=[]
        if offset<folder_total:
            order='created DESC,id' if sort=='recent' else 'name COLLATE PROCESSOR_NAME,id'
            selected=cx.execute('SELECT * FROM library_folders WHERE '+folder_where+' ORDER BY '+order+' LIMIT ? OFFSET ?',(*folder_params,limit,offset))
            rows=[dict(id=f['id'],kind='folder',title=f['name'],parent_id=parent_id,path=path,revision=f['revision'],
                       archived=False,trashed=bool(f['trashed']),created=f['created'],child_count=0) for f in selected]
        wanted=limit-len(rows)
        if wanted:
            order='created DESC,id' if sort=='recent' else 'title COLLATE NOCASE,id'
            selected=cx.execute('SELECT * FROM processor_material_meta WHERE '+doc_where+' ORDER BY '+order+' LIMIT ? OFFSET ?',
                                (parent_id,wanted,max(0,offset-folder_total)))
            rows.extend(dict(id=d['id'],kind='document',title=d['title'],parent_id=parent_id,path=path,revision=d['revision'],
                             archived=bool(d['archived']),trashed=bool(d['trashed']),created=d['created'],child_count=0) for d in selected)
        selected_folders=[r['id'] for r in rows if r['kind']=='folder']
        if selected_folders:
            placeholders=','.join('?' for _ in selected_folders)
            counts={r[0]:r[1] for r in cx.execute("SELECT parent,COUNT(*) FROM library_folders WHERE scope='processor' AND trashed=? AND parent IN ("+
                    placeholders+') GROUP BY parent',(int(space=='trash'),*selected_folders))}
            child_doc_where='trashed=1' if space=='trash' else 'trashed=0 AND archived='+str(int(space=='archive'))
            for key,count in cx.execute('SELECT parent,COUNT(*) FROM processor_material_meta WHERE '+child_doc_where+' AND parent IN ('+placeholders+') GROUP BY parent',selected_folders):
                counts[key]=counts.get(key,0)+count
            for row in rows:
                if row['kind']=='folder':row['child_count']=counts.get(row['id'],0)
        total=folder_total+doc_total
        return dict(nodes=rows,total=total,more=offset+limit<total,offset=offset,limit=limit,space=space,
                    revision=digest([(r['id'],r['revision']) for r in rows]),parent_id=parent_id)

    def tree(self, space='mine', parent_id=None, offset=0, limit=200, q='', sort='name', folders_only=False):
        if space not in {'mine', 'archive', 'trash'}:
            raise ValueError('无效材料空间')
        limit=max(1,min(500,int(limit)));offset=max(0,int(offset))
        parent_id=None if parent_id in {None,'','root'} else parent_id
        query=str(q).strip().casefold()
        with self.store.connect() as cx:
            if parent_id is not None and not query and not folders_only:
                return self._children(cx,space,parent_id,offset,limit,sort)
            folders={r['id']:dict(r) for r in cx.execute("SELECT * FROM library_folders WHERE scope='processor'")}
            paths=self._paths(folders)
            visible_f={key for key,row in folders.items() if bool(row['trashed'])==(space=='trash')} if space!='archive' else set()
            folder_rows=[]
            for fid in visible_f:
                row=folders[fid]
                matches=query in (paths.get(row['parent'],'')+'/'+row['name']).casefold() if query else (
                    folders_only or row['parent']==parent_id or (parent_id is None and row['parent'] not in visible_f))
                if matches:
                    folder_rows.append(dict(id=fid,kind='folder',title=row['name'],parent_id=row['parent'],path=paths.get(row['parent'],''),
                                            revision=row['revision'],archived=False,trashed=bool(row['trashed']),created=row['created'],child_count=0))
            folder_rows.sort(key=lambda r: (-(r['created'] or 0) if sort=='recent' else name_key(r['title']),r['id']))
            doc_where='trashed=1' if space=='trash' else 'trashed=0 AND archived='+str(int(space=='archive'))
            counts={r[0]:r[1] for r in cx.execute('SELECT parent,COUNT(*) FROM processor_material_meta WHERE '+doc_where+' GROUP BY parent')}
            for fid in visible_f:
                parent=folders[fid]['parent'];counts[parent]=counts.get(parent,0)+1
            for row in folder_rows:row['child_count']=counts.get(row['id'],0)
            if folders_only:
                total=len(folder_rows);rows=folder_rows[offset:offset+limit]
            else:
                params=[]
                # Common ASCII/Chinese searches and ordering stay inside SQLite.
                # Non-ASCII cased queries retain Unicode folding where needed.
                fold='lower'
                if any(ord(c)>127 and c.lower()!=c.upper() for c in query):
                    cx.create_function('processor_casefold',1,lambda value:str(value or '').casefold(),deterministic=True)
                    fold='processor_casefold'
                if query:
                    clauses=[f'instr({fold}(title),?)>0'];search_params=[query]
                    whole_paths=[fid for fid,path in paths.items() if fid is not None and query in path.casefold()]
                    if whole_paths:
                        clauses.append('parent IN ('+','.join('?' for _ in whole_paths)+')');search_params.extend(whole_paths)
                    if '/' in query:
                        prefix,suffix=query.rsplit('/',1)
                        matching=[fid for fid,path in paths.items() if fid is not None and path.casefold().endswith(prefix)]
                        parent_clause='parent IN ('+','.join('?' for _ in matching)+')' if matching else '0'
                        if not prefix:parent_clause='('+parent_clause+' OR parent IS NULL)'
                        clauses.append('('+parent_clause+f' AND instr({fold}(title),?)=1)');search_params.extend(matching);search_params.append(suffix)
                    doc_where+=' AND ('+' OR '.join(clauses)+')';params.extend(search_params)
                elif parent_id is not None:
                    doc_where+=' AND parent=?';params.append(parent_id)
                elif space!='archive':
                    if visible_f:
                        doc_where+=' AND (parent IS NULL OR parent NOT IN ('+','.join('?' for _ in visible_f)+'))';params.extend(sorted(visible_f))
                doc_total=cx.execute('SELECT COUNT(*) FROM processor_material_meta WHERE '+doc_where,params).fetchone()[0]
                total=len(folder_rows)+doc_total
                rows=folder_rows[offset:offset+limit]
                wanted=limit-len(rows);doc_offset=max(0,offset-len(folder_rows))
                if wanted:
                    order='created DESC,id' if sort=='recent' else 'title COLLATE NOCASE,id'
                    docs=cx.execute('SELECT * FROM processor_material_meta WHERE '+doc_where+' ORDER BY '+order+' LIMIT ? OFFSET ?',(*params,wanted,doc_offset))
                    rows.extend(dict(id=d['id'],kind='document',title=d['title'],parent_id=d['parent'],path=paths.get(d['parent'],''),
                                     revision=d['revision'],archived=bool(d['archived']),trashed=bool(d['trashed']),created=d['created'],child_count=0) for d in docs)
        return dict(nodes=rows,total=total,more=offset+limit<total,offset=offset,limit=limit,space=space,
                    revision=digest([(r['id'],r['revision']) for r in rows]),parent_id=parent_id)

    def actions(self, payload):
        if not isinstance(payload,dict):
            raise ValueError('操作必须是对象')
        action = payload.get('action'); request_id = payload.get('request_id')
        if not isinstance(action,str) or action not in {'create_folder','rename','move','trash','preview_trash','restore','preview_purge','purge'}:
            raise ValueError('无效材料树操作')
        if request_id is not None and (not isinstance(request_id,str) or not 1 <= len(request_id) <= 128):
            raise ValueError('无效操作身份')
        if action not in {'preview_purge','preview_trash'} and (not isinstance(request_id,str) or not 1 <= len(request_id) <= 128):
            raise ValueError('操作需要独立 request_id')
        parent = payload.get('parent_id',payload.get('destination'))
        if parent is not None and not isinstance(parent,str):
            raise ValueError('目标目录身份必须是文字')
        parent = None if parent in {None,'','root'} else parent
        signature = digest(payload)
        with self.library.transaction() as cx:
            if request_id:
                receipt = cx.execute('SELECT digest,result FROM processor_tree_receipts WHERE id=?',(request_id,)).fetchone()
                if receipt:
                    if receipt['digest'] != signature:
                        raise Conflict('相同操作身份不能用于不同请求')
                    return dict(json.loads(receipt['result']), replayed=True)
            folders, docs = self._rows(cx)
            if parent is not None and (parent not in folders or folders[parent]['trashed']):
                raise Conflict('目标文件夹不存在或在回收站中')
            def siblings(destination, excluded):
                return [f for f in folders.values() if f['parent']==destination and not f['trashed'] and f['id'] not in excluded] + [d for d in docs.values() if d['parent']==destination and not d['trashed'] and not d['archived'] and d['id'] not in excluded]
            def available(title,destination,excluded, pending):
                key = name_key(title)
                if (destination,key) in pending or any(name_key(r.get('name',r.get('title','')))==key for r in siblings(destination,excluded)):
                    raise Conflict('目标位置存在同名项目，请改名后重试')
                pending.add((destination,key))
            if action == 'create_folder':
                title = name(payload.get('title')); available(title,parent,set(),set()); fid=identity()
                cx.execute("INSERT INTO library_folders(id,parent,name,scope,created) VALUES(?,?,?,'processor',?)",(fid,parent,title,time.time()))
                result = dict(action=action,node=dict(id=fid,kind='folder',title=title,parent_id=parent,revision=0),folders=1,documents=0)
            else:
                items = payload.get('items')
                if items is None:
                    node_ids = payload.get('node_ids',[])
                    if not isinstance(node_ids,list) or not all(isinstance(i,str) for i in node_ids):
                        raise ValueError('项目身份列表必须是文字数组')
                    revisions = payload.get('expected_revision',{})
                    if not isinstance(revisions,dict):
                        revisions = {i:revisions for i in node_ids}
                    items = [dict(id=i,kind='folder' if i in folders else 'document',revision=revisions.get(i)) for i in node_ids]
                if not isinstance(items,list) or not items or len(items)>10000:
                    raise ValueError('请选择材料，单次最多 10000 项')
                selected = set()
                for item in items:
                    if not isinstance(item,dict): raise ValueError('无效操作项目')
                    key=item.get('id'); kind=item.get('kind')
                    if not isinstance(kind,str):raise ValueError('无效项目类型')
                    rows=folders if kind=='folder' else docs if kind in {'document','material'} else {}
                    kind='document' if kind=='material' else kind
                    if not isinstance(key,str): raise ValueError('无效项目身份')
                    if key not in rows: raise Conflict('项目不存在、不是我的材料或为只读示例')
                    if (kind,key) in selected: raise ValueError('操作对象重复')
                    selected.add((kind,key))
                    if type(item.get('revision')) is not int or item['revision'] != rows[key]['revision']:
                        raise Conflict('材料库已被另一页面修改，请刷新后再操作')
                children={}
                for f in folders.values(): children.setdefault(f['parent'],[]).append(f['id'])
                def descendants(roots):
                    out=set(); pending=list(roots)
                    while pending:
                        fid=pending.pop()
                        if fid not in out: out.add(fid);pending.extend(children.get(fid,[]))
                    return out
                picked_folders={i for k,i in selected if k=='folder'}
                roots=set(picked_folders)
                for fid in picked_folders:
                    ancestor=folders[fid]['parent'];seen=set()
                    while ancestor in folders and ancestor not in seen:
                        if ancestor in picked_folders: roots.discard(fid);break
                        seen.add(ancestor);ancestor=folders[ancestor]['parent']
                direct={i for k,i in selected if k!='folder'}
                affected_folders=descendants(roots)
                affected_docs=direct|{d['id'] for d in docs.values() if d['parent'] in affected_folders}
                direct_roots={i for i in direct if docs[i]['parent'] not in affected_folders}
                changed_f,changed_d=set(),set(); fallback=[]; now=time.time()
                def doc_set(did, **updates):
                    paths=[]
                    for key,value in updates.items(): paths.extend(['$.'+key,value])
                    statement='UPDATE projects SET body=json_set(body,'+','.join('?' for _ in paths)+",'$.library_revision',COALESCE(json_extract(body,'$.library_revision'),0)+1) WHERE id=?"
                    cx.execute(statement,(*paths,did));changed_d.add(did)
                def folder_set(fid, **updates):
                    cx.execute('UPDATE library_folders SET '+','.join(k+'=?' for k in updates)+',revision=revision+1 WHERE id=?',(*updates.values(),fid));changed_f.add(fid)
                if action=='rename':
                    if len(selected)!=1: raise ValueError('一次只能重命名一个项目')
                    kind,key=next(iter(selected)); row=folders[key] if kind=='folder' else docs[key]
                    if row['trashed']: raise Conflict('请先恢复，再重命名')
                    title=name(payload.get('title'));available(title,row['parent'],{key},set())
                    folder_set(key,name=title) if kind=='folder' else doc_set(key,**{'library.display_name':title})
                elif action=='move':
                    if parent in affected_folders: raise Conflict('不能移动到自身或子文件夹')
                    if any(folders[i]['trashed'] for i in roots) or any(docs[i]['trashed'] for i in direct_roots):
                        raise Conflict('请先恢复，再移动')
                    excluded=roots|direct_roots;pending=set()
                    for fid in roots: available(folders[fid]['name'],parent,excluded,pending)
                    for did in direct_roots: available(docs[did]['title'],parent,excluded,pending)
                    for fid in roots: folder_set(fid,parent=parent)
                    for did in direct_roots: doc_set(did,folder=parent)
                elif action in {'trash','preview_trash'}:
                    group=identity()
                    for did in affected_docs:
                        row=cx.execute('SELECT body FROM projects WHERE id=?',(did,)).fetchone();body=json.loads(row[0])
                        if body.get('active_job') or any(r.get('status') in {'RUNNING','DISPATCHING','PENDING'} for r in body['processor'].get('requests',[])):
                            raise Conflict('材料仍有进行中的处理，请等待完成或先明确停止')
                    if action=='preview_trash':
                        active_f={i for i in affected_folders if not folders[i]['trashed']}
                        active_d={i for i in affected_docs if not docs[i]['trashed']}
                        return dict(action=action,impact=dict(folders=len(active_f),documents=len(active_d),remote_unchanged=True),
                                    affected_ids=sorted(active_f|active_d))
                    for fid in affected_folders:
                        if not folders[fid]['trashed']: folder_set(fid,trashed=1,trash_group=group,trash_parent=folders[fid]['parent'],trashed_at=now)
                    for did in affected_docs:
                        if not docs[did]['trashed']: doc_set(did,trashed=1,trash_group=group,trash_parent=docs[did]['parent'],trashed_at=now)
                elif action=='restore':
                    groups={folders[i]['trash_group'] for i in roots if folders[i]['trashed']}
                    restore_f={i for i in affected_folders if folders[i]['trashed'] and folders[i]['trash_group'] in groups}
                    restore_d={i for i in affected_docs if docs[i]['trashed'] and (i in direct or docs[i]['trash_group'] in groups)}
                    pending=set();excluded=restore_f|restore_d
                    destinations={}
                    explicit='parent_id' in payload or 'destination' in payload
                    for kind, keys in [('folder',restore_f),('document',restore_d)]:
                        for key in keys:
                            row=folders[key] if kind=='folder' else docs[key];dest=row['trash_parent']
                            if explicit and (key in roots or key in direct_roots): dest=parent
                            if dest and (dest not in folders or (folders[dest]['trashed'] and dest not in restore_f)):
                                dest=None;fallback.append(key)
                            if dest in descendants({key}) if kind=='folder' else False:
                                raise Conflict('不能恢复到自身或子文件夹')
                            available(row.get('name',row.get('title')),dest,excluded,pending);destinations[key]=dest
                    for fid in restore_f: folder_set(fid,trashed=0,trash_group=None,parent=destinations[fid])
                    for did in restore_d: doc_set(did,trashed=0,trash_group=None,folder=destinations[did])
                else:
                    if any(not folders[i]['trashed'] for i in affected_folders) or any(not docs[i]['trashed'] for i in affected_docs):
                        raise Conflict('只能彻底删除回收站内的项目')
                    content_versions = list(cx.execute("SELECT id,json_extract(body,'$.revision'),json_extract(body,'$.processor.active_version') FROM projects WHERE id IN ("+','.join('?' for _ in affected_docs)+")",tuple(sorted(affected_docs)))) if affected_docs else []
                    affected = sorted([('folder',i,folders[i]['revision']) for i in affected_folders]+[('document',i,docs[i]['revision']) for i in affected_docs])
                    token=digest(dict(action='purge',affected=affected,content_versions=[list(r) for r in content_versions]))
                    impact=dict(folders=len(affected_folders),documents=len(affected_docs),retained_blobs=True,remote_unchanged=True)
                    if action=='preview_purge':
                        return dict(action=action,confirmation_token=token,impact=impact,
                                    affected_ids=sorted(affected_folders|affected_docs))
                    if payload.get('confirm') is not True or payload.get('confirmation_token')!=token:
                        raise Conflict('永久删除需要当前影响预览后的独立确认')
                    for did in affected_docs:
                        if cx.execute("SELECT 1 FROM jobs WHERE project=? AND status IN ('running','queued','uncertain')",(did,)).fetchone():
                            raise Conflict('材料仍有运行或不确定请求，不能永久删除')
                        body=json.loads(cx.execute('SELECT body FROM projects WHERE id=?',(did,)).fetchone()[0])
                        if body.get('active_job') or any(r.get('status') in {'RUNNING','UNKNOWN','uncertain','DISPATCHING','PENDING'} for r in body['processor'].get('requests',[])):
                            raise Conflict('材料仍有运行或不确定请求，不能永久删除')
                    tables={r[0] for r in cx.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for did in affected_docs:
                        for table in ('projects','revisions','source_versions','jobs','events','production_control','library_versions'):
                            if table in tables:
                                cx.execute(f'DELETE FROM {table} WHERE '+('id' if table=='projects' else 'project')+'=?',(did,))
                    for fid in affected_folders: cx.execute('DELETE FROM library_folders WHERE id=?',(fid,))
                    changed_f=affected_folders;changed_d=affected_docs
                result=dict(action=action,folders=len(changed_f),documents=len(changed_d),moved_to_root=fallback,
                            affected_ids=sorted(changed_f|changed_d))
            if request_id:
                cx.execute('INSERT INTO processor_tree_receipts VALUES(?,?,?,?)',(request_id,signature,json.dumps(result,ensure_ascii=False),time.time()))
            return result
