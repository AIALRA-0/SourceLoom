"""Tree metadata never grants authority to change source, draft or remote notes."""
import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from sourceloom import processor
from sourceloom.library_store import Library
from sourceloom.processor_library import LibraryStore, rename, set_archive
from sourceloom.processor_tree import ProcessorTree
from sourceloom.processor_tree_api import register_tree
from sourceloom.store import Conflict, Store, digest, identity


def setup(tmp_path):
    store=Store(tmp_path);tree=ProcessorTree(store)
    return store,tree


def folder(tree,title,parent=None,rid=None):
    return tree.actions(dict(action='create_folder',title=title,parent_id=parent,request_id=rid or identity()))['node']


def doc(store,title='文档',parent=None):
    p=processor.create(store,title)
    def update(p):
        p['folder']=parent;p['library_revision']=0
        p['inventory']={'source':'immutable source'}
        p['processor']['versions']=[{'id':'stable-version','markdown':'原中文正文\n\n{{resource:src-00001}}','raw_markdown':'returned bytes','representations':[]}]
        p['processor']['active_version']='stable-version'
        p['readweave']={'note_id':'remote-id','status':'saved'}
    return store.change(p['id'],update)


def item(tree,key,kind='document'):
    with tree.store.connect() as cx:
        table='library_folders' if kind=='folder' else 'processor_material_meta'
        r=cx.execute('SELECT revision FROM '+table+' WHERE id=?',(key,)).fetchone()
    return dict(id=key,kind=kind,revision=r[0])


def action(tree,which,items,**kw):
    return tree.actions(dict(action=which,items=items,request_id=identity(),**kw))


def content(p):
    return {k:v for k,v in p.items() if k not in {'folder','library','library_revision','trashed','trash_group','trash_parent','trashed_at'}}


def test_nested_rename_move_search_are_metadata_only(tmp_path):
    store,tree=setup(tmp_path);a=folder(tree,'文献');b=folder(tree,'论文',a['id']);p=doc(store,parent=b['id']);baseline=content(p)
    rename(store,p['id'],'中文稿');assert content(store.get(p['id']))==baseline
    assert store.get(p['id'])['library']['display_name']=='中文稿'
    action(tree,'move',[item(tree,p['id'])],parent_id=a['id'])
    assert content(store.get(p['id']))==baseline
    assert tree.tree(parent_id=a['id'])['nodes'][1]['title']=='中文稿'
    hit=tree.tree(q='文献/中文')['nodes'][0];assert hit['path']=='文献';assert hit['id']==p['id']
    assert tree.tree(parent_id=a['id'])['nodes'][0]['child_count']==0
    with store.connect() as cx: assert cx.execute('SELECT COUNT(*) FROM revisions').fetchone()[0]==0


def test_idempotency_collision_cycle_and_cross_scope(tmp_path):
    store,tree=setup(tmp_path);a=folder(tree,'A',rid='one');assert folder(tree,'A',rid='one')==a
    with pytest.raises(Conflict,match='不同请求'): folder(tree,'Other',rid='one')
    with pytest.raises(Conflict,match='同名'): folder(tree,'ａ')
    b=folder(tree,'B',a['id']);p=doc(store,'X');legacy=store.create('old')
    with pytest.raises(Conflict,match='自身'): action(tree,'move',[item(tree,a['id'],'folder')],parent_id=b['id'])
    with pytest.raises(Conflict): action(tree,'trash',[dict(id=legacy['id'],kind='document',revision=0)])
    with pytest.raises(Conflict): action(tree,'trash',[dict(id='sample-S5',kind='document',revision=0)])
    with pytest.raises(Conflict,match='处理器'): Library(store).apply('trash',[dict(id=p['id'],kind='document',revision=0)])
    with pytest.raises(ValueError): folder(tree,'../oops')


def test_concurrent_same_name_only_one_commit(tmp_path):
    store,tree=setup(tmp_path);barrier=threading.Barrier(2)
    def create():
        barrier.wait()
        try:return folder(tree,'同名')['id']
        except Conflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(lambda _:create(),range(2)))
    assert results.count('conflict')==1;assert len(tree.tree()['nodes'])==1


def test_concurrent_revision_conflict_is_atomic(tmp_path):
    store,tree=setup(tmp_path);p=doc(store,'A');q=doc(store,'B');selected=item(tree,p['id'])
    action(tree,'rename',[selected],title='A2')
    with pytest.raises(Conflict): action(tree,'trash',[item(tree,q['id']),selected])
    assert not store.get(q['id']).get('trashed')


def test_parent_child_selection_soft_delete_restores_group_only(tmp_path):
    store,tree=setup(tmp_path);a=folder(tree,'A');b=folder(tree,'B',a['id']);p=doc(store,'p',b['id']);old=doc(store,'old',b['id'])
    action(tree,'trash',[item(tree,old['id'])]);before=content(store.get(p['id']))
    result=action(tree,'trash',[item(tree,a['id'],'folder'),item(tree,b['id'],'folder'),item(tree,p['id'])])
    assert result['folders']==2 and result['documents']==1
    assert not tree.tree()['nodes'];assert len(tree.tree(space='trash')['nodes'])==1
    assert tree.tree(space='trash',parent_id=b['id'])['total']==2
    action(tree,'restore',[item(tree,a['id'],'folder')])
    assert not store.get(p['id'])['trashed'];assert store.get(old['id'])['trashed']
    assert content(store.get(p['id']))==before
    assert store.get(p['id'])['folder']==b['id']


def test_restore_missing_parent_and_explicit_destination(tmp_path):
    store,tree=setup(tmp_path);a=folder(tree,'A');b=folder(tree,'B');p=doc(store,'p',a['id'])
    action(tree,'trash',[item(tree,p['id'])])
    # An originally empty parent can be explicitly deleted separately.
    with store.connect() as cx:cx.execute('DELETE FROM library_folders WHERE id=?',(a['id'],))
    result=action(tree,'restore',[item(tree,p['id'])]);assert result['moved_to_root']==[p['id']]
    assert store.get(p['id'])['folder'] is None
    action(tree,'trash',[item(tree,p['id'])]);action(tree,'restore',[item(tree,p['id'])],parent_id=b['id'])
    assert store.get(p['id'])['folder']==b['id']


def test_restore_collision_and_cycle_rollback(tmp_path):
    store,tree=setup(tmp_path);a=folder(tree,'A');b=folder(tree,'B',a['id']);p=doc(store,'p',a['id'])
    action(tree,'trash',[item(tree,p['id'])]);doc(store,'p',a['id'])
    with pytest.raises(Conflict,match='同名'): action(tree,'restore',[item(tree,p['id'])])
    assert store.get(p['id'])['trashed']
    action(tree,'trash',[item(tree,a['id'],'folder')])
    # Destination is a deleted descendant and must be rejected.
    with pytest.raises(Conflict): action(tree,'restore',[item(tree,a['id'],'folder')],parent_id=b['id'])


def test_archive_trash_separate_and_examples_readonly(tmp_path):
    store,tree=setup(tmp_path);p=doc(store);set_archive(store,p['id'],True)
    assert not tree.tree()['nodes'];assert tree.tree(space='archive')['nodes'][0]['id']==p['id']
    action(tree,'trash',[item(tree,p['id'])]);assert not tree.tree(space='archive')['nodes']
    with pytest.raises(Conflict):set_archive(store,p['id'],False)
    action(tree,'restore',[item(tree,p['id'])]);assert tree.tree(space='archive')['nodes'][0]['id']==p['id']
    set_archive(store,p['id'],False);assert tree.tree()['nodes'][0]['id']==p['id']
    catalog=store.root/'library/examples';catalog.mkdir(parents=True)
    example=copy.deepcopy(p);example['id']='sample-test';example['library']={'kind':'example'}
    (catalog/'catalog.json').write_text(json.dumps({'projects':[example]}),encoding='utf-8')
    with pytest.raises(Conflict):rename(LibraryStore(store),'sample-test','other')
    with pytest.raises(Conflict):set_archive(LibraryStore(store),'sample-test',True)


def test_permanent_delete_separate_confirmation_shared_blobs_audit_retained(tmp_path):
    store,tree=setup(tmp_path);a=folder(tree,'A');p=doc(store,parent=a['id']);key=store.blob(b'shared immutable resource')
    with store.connect() as cx:
        cx.execute('INSERT INTO spending VALUES(?,?,?,?,?,?,?)',('cost',p['id'],1,1,'spent','{}',time.time()))
        cx.execute('INSERT INTO revisions VALUES(?,?,?)',(p['id'],1,json.dumps(p)))
    action(tree,'trash',[item(tree,a['id'],'folder')]);selected=[item(tree,a['id'],'folder')]
    with pytest.raises(Conflict):action(tree,'purge',selected,confirm=True)
    preview=action(tree,'preview_purge',selected);assert preview['impact']==dict(folders=1,documents=1,retained_blobs=True,remote_unchanged=True)
    assert preview['affected_ids']==sorted([a['id'],p['id']])
    result=action(tree,'purge',selected,confirm=True,confirmation_token=preview['confirmation_token'])
    assert result['documents']==1;assert store.read_blob(key)==b'shared immutable resource'
    with pytest.raises(KeyError):store.get(p['id'])
    with store.connect() as cx:assert cx.execute('SELECT COUNT(*) FROM spending').fetchone()[0]==1


def test_uncertain_receipts_block_purge_and_running_blocks_trash(tmp_path):
    store,tree=setup(tmp_path);p=doc(store)
    store.change(p['id'],lambda p:p['processor']['requests'].append(dict(id='running',status='RUNNING')))
    with pytest.raises(Conflict):action(tree,'trash',[item(tree,p['id'])])
    store.change(p['id'],lambda p:p['processor']['requests'][0].update(status='UNKNOWN'))
    action(tree,'trash',[item(tree,p['id'])]);selected=[item(tree,p['id'])];preview=action(tree,'preview_purge',selected)
    with pytest.raises(Conflict):action(tree,'purge',selected,confirm=True,confirmation_token=preview['confirmation_token'])
    # A late receipt updates its existing history but cannot restore navigation.
    store.change(p['id'],lambda p:p['processor']['requests'][0].update(status='SUCCESS'))
    assert not tree.tree()['nodes'];assert tree.tree(space='trash')['nodes'][0]['id']==p['id']


def test_api_metadata_permissions_and_idempotent_folder(tmp_path):
    store,tree=setup(tmp_path);p=doc(store);app=FastAPI();register_tree(app,store)
    with TestClient(app) as client:
        r=client.get('/api/processor/tree');assert r.status_code==200
        assert r.json()['nodes'][0]['id']==p['id'];assert 'processor' not in r.text and 'markdown' not in r.text
        req=dict(action='create_folder',title='文件夹',request_id='http-one')
        one=client.post('/api/processor/tree/actions',json=req).json();two=client.post('/api/processor/tree/actions',json=req).json()
        assert one['node']==two['node'];assert two['replayed']


@pytest.mark.parametrize('count',[100,1000,10000])
def test_scale_lightweight_tree_no_project_or_blob_reads(tmp_path,count,monkeypatch):
    store,tree=setup(tmp_path)
    with store.connect() as cx:
        for n in range(count):
            if n%5==0:
                cx.execute("INSERT INTO library_folders(id,name,scope) VALUES(?,?,'processor')",('f'+str(n),'文件夹'+str(n)))
            else:
                body=dict(id='p'+str(n),title='材料'+str(n),created=n,processor={},folder='f'+str(n-n%5))
                cx.execute('INSERT INTO projects VALUES(?,?)',(body['id'],json.dumps(body)))
    def forbidden(*args,**kw):raise AssertionError('Tree must not read a full material or blob')
    monkeypatch.setattr(store,'get',forbidden);monkeypatch.setattr(store,'list',forbidden);monkeypatch.setattr(store,'read_blob',forbidden)
    start=time.perf_counter();page=tree.tree(q='材料',limit=200);elapsed=time.perf_counter()-start
    assert page['total']==count*4//5;assert len(page['nodes'])==min(200,count*4//5)
    assert elapsed<2.0;assert len(json.dumps(page).encode())<150000
    assert tree.tree(parent_id='f0')['total']==4


def test_trash_preview_folders_only_and_legacy_subtree_scope(tmp_path):
    store,tree=setup(tmp_path);a=folder(tree,'A');b=folder(tree,'B',a['id']);p=doc(store,parent=b['id'])
    preview=tree.actions(dict(action='preview_trash',items=[item(tree,a['id'],'folder')]))
    assert preview['impact']['documents']==1 and preview['impact']['folders']==2
    assert set(preview['affected_ids'])=={a['id'],b['id'],p['id']}
    assert not store.get(p['id']).get('trashed')
    assert tree.tree(folders_only=True)['total']==2
    with store.connect() as cx:
        cx.execute("INSERT INTO library_folders(id,name) VALUES('legacy-parent','Legacy')")
        cx.execute("UPDATE library_folders SET parent='legacy-parent' WHERE id=?",(a['id'],))
    with pytest.raises(Conflict,match='处理器'):
        Library(store).apply('trash',[dict(id='legacy-parent',kind='folder',revision=0)])
    assert not store.get(p['id']).get('trashed')


def test_purge_preview_stale_when_content_version_changes(tmp_path):
    store,tree=setup(tmp_path);p=doc(store);action(tree,'trash',[item(tree,p['id'])])
    selected=[item(tree,p['id'])];preview=action(tree,'preview_purge',selected)
    store.change(p['id'],lambda p:p.update(revision=p['revision']+1))
    with pytest.raises(Conflict,match='独立确认'):
        action(tree,'purge',selected,confirm=True,confirmation_token=preview['confirmation_token'])


def test_management_keeps_source_return_pack_history_and_remote(tmp_path):
    store,tree=setup(tmp_path)
    p=processor.create(store,'Frozen task title')
    p=processor.prepare(store,p['id'],[('source.txt',b'Original source with unchanged words.')])
    p=processor.save_result(store,p['id'],'# Chinese candidate\n\nOriginal source with unchanged words.')
    p=store.change(p['id'],lambda p:p.update(readweave={'note_id':'unchanged-remote-id'}))
    original=copy.deepcopy(p);pack=processor.task_pack(store,p['id']);keys=list((store.root/'blobs').iterdir())
    before_hashes={path.name:digest(path.read_bytes()) for path in keys}
    a=folder(tree,'Folder');rename(store,p['id'],'New display only')
    move=dict(action='move',items=[item(tree,p['id'])],parent_id=a['id'],request_id='move-once')
    first=tree.actions(move);assert tree.actions(move)['replayed']
    assert item(tree,p['id'])['revision']==2
    set_archive(store,p['id'],True);set_archive(store,p['id'],False)
    action(tree,'trash',[item(tree,p['id'])]);action(tree,'restore',[item(tree,p['id'])])
    final=store.get(p['id']);assert content(final)==content(original)
    assert final['title']==original['title'];assert final['processor']==original['processor']
    assert processor.task_pack(store,p['id'])['digest']==pack['digest']
    assert before_hashes=={path.name:digest(path.read_bytes()) for path in keys}
    assert tree.material_summaries()[0]['title']=='New display only'
    assert first['documents']==1


def test_unicode_search_and_page_offsets_have_stable_identity(tmp_path):
    store,tree=setup(tmp_path);a=folder(tree,'Über');p=doc(store,'Älpha',a['id']);q=doc(store,'Beta',a['id'])
    assert tree.tree(q='älpha')['nodes'][0]['id']==p['id']
    assert any(r['id']==q['id'] for r in tree.tree(q='über')['nodes'])
    first=tree.tree(parent_id=a['id'],limit=1);second=tree.tree(parent_id=a['id'],offset=1,limit=1)
    assert first['more'];assert not second['more'];assert first['nodes'][0]['id']!=second['nodes'][0]['id']


@pytest.mark.parametrize('title',[None,[],{},23])
def test_invalid_names_rejected_without_mutation(tmp_path,title):
    store,tree=setup(tmp_path)
    with pytest.raises(ValueError):tree.actions(dict(action='create_folder',title=title,request_id='bad'))
    assert tree.tree()['total']==0


@pytest.mark.parametrize('items',[[None],[[]],[dict(id=[],kind='document',revision=0)],[dict(id='x',kind=[],revision=0)]])
def test_invalid_items_rejected_cleanly(tmp_path,items):
    store,tree=setup(tmp_path)
    with pytest.raises(ValueError):tree.actions(dict(action='trash',items=items,request_id='bad'))


def test_kind_alias_duplicates_and_folder_revision_conflict(tmp_path):
    store,tree=setup(tmp_path);a=folder(tree,'A');p=doc(store)
    selected=item(tree,p['id'])
    with pytest.raises(ValueError):action(tree,'trash',[selected,dict(selected,kind='material')])
    stale=item(tree,a['id'],'folder');action(tree,'rename',[stale],title='A2')
    with pytest.raises(Conflict):action(tree,'trash',[stale])
    assert not store.get(p['id']).get('trashed')


def test_purge_token_tracks_current_subtree(tmp_path):
    store,tree=setup(tmp_path);a=folder(tree,'A');b=folder(tree,'B',a['id']);p=doc(store,parent=b['id'])
    action(tree,'trash',[item(tree,a['id'],'folder')]);selected=[item(tree,a['id'],'folder')]
    parent_preview=action(tree,'preview_purge',selected)
    # An independently confirmed descendant purge changes the parent's preview.
    child=[item(tree,p['id'])];child_preview=action(tree,'preview_purge',child)
    action(tree,'purge',child,confirm=True,confirmation_token=child_preview['confirmation_token'])
    with pytest.raises(Conflict,match='独立确认'):
        action(tree,'purge',selected,confirm=True,confirmation_token=parent_preview['confirmation_token'])


def test_scoped_children_use_only_parent_ancestors_and_page_counts(tmp_path,monkeypatch):
    from contextlib import contextmanager
    store,tree=setup(tmp_path);a=folder(tree,'Grand');b=folder(tree,'Parent',a['id'])
    child=folder(tree,'Child',b['id']);d=doc(store,'Direct',b['id']);nested=doc(store,'Nested',child['id'])
    statements=[];original=store.connect
    @contextmanager
    def traced():
        with original() as cx:
            cx.set_trace_callback(statements.append)
            yield cx
    monkeypatch.setattr(store,'connect',traced)
    monkeypatch.setattr(tree,'_paths',lambda _: (_ for _ in ()).throw(AssertionError('Expanded unrelated ancestors')))
    page=tree.tree(parent_id=b['id'],limit=1)
    assert page['nodes'][0]['id']==child['id'];assert page['nodes'][0]['path']=='Grand/Parent'
    assert page['nodes'][0]['child_count']==1;assert page['more'];assert page['total']==2
    second=tree.tree(parent_id=b['id'],offset=1,limit=1)
    assert second['nodes'][0]['id']==d['id'];assert not second['more']
    assert all(' WHERE ' in statement.upper() for statement in statements if statement.lstrip().upper().startswith('SELECT'))
    assert all(' AND parent=' in statement for statement in statements if statement.startswith("SELECT * FROM library_folders WHERE scope='processor'"))
    assert not any('FROM projects' in statement for statement in statements)
    assert not any('GROUP BY parent' in statement and 'parent IN (' not in statement for statement in statements)


def test_scoped_children_preserve_unicode_sort_archive_and_trash(tmp_path):
    store,tree=setup(tmp_path);a=folder(tree,'Parent')
    wide=folder(tree,'Ｂ',a['id']);latin=folder(tree,'a',a['id']);unicode=folder(tree,'Ä',a['id'])
    active=doc(store,'Active',a['id']);arch=doc(store,'Archived',a['id']);set_archive(store,arch['id'],True)
    nodes=tree.tree(parent_id=a['id'])['nodes']
    assert [n['title'] for n in nodes[:3]]==['a','Ｂ','Ä'];assert nodes[-1]['id']==active['id']
    assert tree.tree(space='archive',parent_id=a['id'])['nodes'][0]['id']==arch['id']
    action(tree,'trash',[item(tree,a['id'],'folder')])
    assert tree.tree(space='trash')['nodes'][0]['id']==a['id']
    assert tree.tree(space='trash',parent_id=a['id'])['total']==5
    assert tree.tree(parent_id=a['id'])['total']==0


def test_scoped_children_ten_thousand_unrelated_nodes_are_not_scanned(tmp_path,monkeypatch):
    store,tree=setup(tmp_path);a=folder(tree,'Current');child=folder(tree,'Only child',a['id'])
    with store.connect() as cx:
        cx.executemany("INSERT INTO library_folders(id,name,scope) VALUES(?,?,'processor')", [('unused-'+str(n),'Unrelated '+str(n)) for n in range(10000)])
    from contextlib import contextmanager
    original=store.connect;steps=[]
    @contextmanager
    def measured():
        with original() as cx:
            cx.set_progress_handler(lambda: steps.append(1) or 0,100)
            yield cx
    monkeypatch.setattr(store,'connect',measured)
    page=tree.tree(parent_id=a['id'])
    assert page['total']==1 and page['nodes'][0]['id']==child['id']
    assert len(steps)<30  # indexed parent/ID probes, not a 10000-node scan
