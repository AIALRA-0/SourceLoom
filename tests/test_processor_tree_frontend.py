"""Navigation metadata must never become a reader/content lifecycle action."""
import json
import shutil
import subprocess
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]


def execute(body):
    script="""
    const fs=require('fs'),vm=require('vm'),assert=require('assert');
    let code=fs.readFileSync(process.argv[1]+'/sourceloom/static/processor_tree.js','utf8').replace(/export /g,'');
    const ctx={console,crypto:require('crypto').webcrypto,URLSearchParams,Set,Map,JSON,Error,setTimeout,clearTimeout,cancelAnimationFrame(){},requestAnimationFrame:()=>1,document:{querySelector:()=>({value:''})},localStorage:{setItem(){}}};
    vm.createContext(ctx);vm.runInContext(code+';globalThis.Tree=MaterialTree;globalThis.flatten=flattenTree;globalThis.range=selectionRange;globalThis.pick=selectedNodes;globalThis.moveRoots=moveRoots;',ctx);
    const tree=Object.create(ctx.Tree.prototype);Object.assign(tree,{deps:{api:async()=>({nodes:[]}),post:async()=>({}),current:()=>({id:'current'}),notify(){},isDirty:()=>false},mode:'mine',epoch:0,cache:new Map(),expanded:new Set(),selected:new Set(),spaceState:new Map(),host:{scrollTop:14,classList:{toggle(){}},setAttribute(){}},canvas:{style:{}},tools:{},status:{textContent:''},rows:[],renderWindow(){},persist(){},syncCurrent(){},restored:true,query:'',sort:'name'});
    (async()=>{BODY})().catch(e=>{console.error(e);process.exit(1)});
    """.replace('BODY',body)
    subprocess.run([shutil.which('node'),'-e',script,str(ROOT)],check=True,capture_output=True,text=True,timeout=10)


def test_hierarchy_and_load_more_are_not_multiselect_materials():
    execute("""
    tree.cache.set('root',{nodes:[{id:'f',kind:'folder',title:'folder'}],more:true});tree.cache.set('f',{nodes:[{id:'p',kind:'material',title:'prose'}]});tree.expanded.add('f');
    const rows=ctx.flatten(tree.cache,tree.expanded);assert.deepEqual(rows.map(n=>[n.id,n.depth]),[['f',1],['p',2],['more:root',1]]);
    assert.deepEqual(ctx.range(rows,'f','p'),['f','p']);assert.deepEqual(ctx.pick(rows,new Set(['more:root','p'])).map(n=>n.id),['p']);
    """)


def test_sort_keeps_children_with_parent():
    execute("""
    tree.cache.set('root',{nodes:[{id:'a',kind:'folder',title:'a'},{id:'z',kind:'folder',title:'z'}]});tree.cache.set('a',{nodes:[{id:'c',kind:'material',title:'child',parent_id:'a'}]});tree.expanded.add('a');tree.sort='reverse';tree.refreshRows();assert.deepEqual(tree.rows.map(n=>n.id),['z','a','c']);
    """)


def test_metadata_action_posts_exact_revision_without_document_open():
    execute("""
    tree.rows=[{id:'current',kind:'material',revision:7}];tree.selected.add('current');let request;tree.deps.open=()=>{throw Error('unexpected reader open')};tree.deps.post=async(url,body)=>{request=body;return {action:'rename',affected_ids:['current']}};tree.refresh=async()=>{};await tree.action('rename',{title:'managed name'});assert.deepEqual(request.items,[{id:'current',kind:'document',revision:7}]);assert(request.request_id);assert.equal(request.title,'managed name');
    """)


def test_dirty_current_subtree_is_not_deleted():
    execute("""
    tree.rows=[{id:'folder',kind:'folder',revision:2}];tree.selected.add('folder');tree.trashPreview={affected_ids:['current']};tree.deps.isDirty=()=>true;tree.deps.post=async()=>{throw Error('must not write')};await assert.rejects(tree.action('trash'),/未保存/);
    """)


def test_stale_tree_response_cannot_replace_new_space():
    execute("""
    let resolve;tree.deps.api=()=>new Promise(r=>resolve=r);const refresh=tree.refresh();tree.epoch++;resolve({nodes:[{id:'old',kind:'document'}]});await refresh;assert.equal(tree.cache.size,0);
    """)


def test_search_projection_preserves_obligation_independent_tree_structure():
    execute("""
    tree.query='result';tree.searchRows=[{id:'hit',kind:'material',title:'result',path:'papers/sub'}];tree.refreshRows();assert.equal(tree.rows[0].path,'papers/sub');assert.equal(tree.rowHeight,44);tree.searchRows=null;tree.cache.set('root',{nodes:[{id:'folder',kind:'folder',title:'saved expansion'}]});tree.refreshRows();assert.equal(tree.rows[0].id,'folder');
    """)


def test_non_content_tree_requests_are_small_metadata_only():
    execute("""
    let url;tree.deps.api=async(u)=>{url=u;return {nodes:[{id:'p',kind:'document',revision:3,title:'title'}],more:false,total:1}};await tree.load('parent');assert(url.includes('parent_id=parent'));assert(url.includes('limit=200'));assert.equal(tree.cache.get('parent').nodes[0].kind,'material');assert(!url.includes('preview')&&!url.includes('versions'));
    """)


def test_tree_structure_does_not_move_under_an_active_drag():
    execute("""
    tree.rows=[{id:'old',title:'visible'}];tree.cache.set('root',{nodes:[{id:'next',kind:'material',title:'updated'}]});tree.dragActive=true;tree.refreshRows();assert.equal(tree.rows[0].id,'old');assert.equal(tree.pendingRefresh,true);tree.dragActive=false;tree.refreshRows();assert.equal(tree.rows[0].id,'next');
    """)


def test_folder_pagination_accumulates_only_metadata():
    execute("""
    tree.cache.set('f',{nodes:[{id:'one',title:'one',kind:'material'}],more:true});tree.deps.api=async(url)=>{assert(url.includes('offset=1'));return {nodes:[{id:'two',kind:'document',title:'two'}],more:false,total:2}};await tree.load('f',tree.epoch,true);assert.equal(tree.cache.get('f').nodes.length,2);assert.equal(tree.cache.get('f').more,false);
    """)


def test_examples_tree_uses_readonly_catalog_and_never_management_api():
    execute("""
    tree.mode='examples';tree.deps.api=async(url)=>{assert.equal(url,'/api/processor/examples');return [{id:'sample-real',title:'真实只读稿'}]};tree.deps.post=async()=>{throw Error('example mutation')};await tree.refresh();assert.equal(tree.rows[0].kind,'example');tree.rename(tree.rows[0]);await tree.trash();assert.equal(tree.rows[0].id,'sample-real');let opened;tree.deps.open=id=>opened=id;tree.openExample(tree.rows[0]);assert.equal(opened,'sample-real');
    """)


def test_reopening_current_example_restores_workspace_without_reloading_document():
    execute("""
    const code=fs.readFileSync(process.argv[1]+'/sourceloom/static/processor_library.js','utf8').replace(/^import .*;$/m,'').replace(/export /g,'');vm.runInContext(code+';globalThis.Lib=MaterialLibrary;',ctx);
    const workspace={hidden:true},empty={hidden:false};ctx.document.querySelector=s=>s==='#workspace'?workspace:empty;let opened=0,tabs=0,selected=0;const library=Object.create(ctx.Lib.prototype);Object.assign(library,{epoch:0,view:{hidden:false},deps:{open:async()=>opened++,current:()=>({id:'same'}),tab:()=>tabs++},selected:()=>selected++});await library.open('same');assert.equal(workspace.hidden,false);assert.equal(empty.hidden,true);assert.equal(library.view.hidden,true);assert.equal(opened,1);assert.equal(tabs,1);assert.equal(selected,1);
    """)


def test_drag_parent_and_child_selection_moves_only_effective_roots():
    execute("""
    const parent={id:'f',kind:'folder',title:'Folder',path:''},child={id:'c',kind:'material',title:'Paper',parent_id:'f',path:'Folder'},nested={id:'n',kind:'folder',title:'Nested',path:'Folder/Child'},other={id:'o',kind:'material',title:'Other',path:'Else'};assert.deepEqual(ctx.moveRoots([parent,child,nested,other]).map(n=>n.id),['f','o']);
    """)


def test_drop_rejects_cycle_leaf_same_directory_and_readonly_before_post():
    execute("""
    tree.rows=[{id:'f',kind:'folder',title:'Folder',path:''},{id:'sub',kind:'folder',title:'Sub',path:'Folder'},{id:'leaf',kind:'material',title:'Leaf',path:''}];tree.drag={items:[tree.rows[0]]};assert(tree.dropReason('f').includes('自己'));assert(tree.dropReason('sub').includes('子文件夹'));assert(tree.dropReason('leaf').includes('文件夹'));assert(tree.dropReason(undefined));tree.mode='examples';assert(tree.dropReason(null).includes('只读'));tree.mode='mine';tree.drag.items=[{id:'p',kind:'material',parent_id:null}];assert(tree.dropReason(null).includes('已经'));tree.drag.items=[tree.rows[0]];let posts=0,cancelled;tree.deps.post=async()=>posts++;tree.cancelDrag=reason=>{cancelled=reason};await tree.drop('sub',{preventDefault(){},stopPropagation(){}});assert.equal(posts,0);assert(cancelled.includes('子文件夹'));
    """)


def test_move_transport_uncertainty_queries_original_receipt_once_without_repost():
    execute("""
    tree.rows=[{id:'p',kind:'material',title:'Paper',revision:3}];tree.selected=new Set(['p']);let posts=0,queries=0,request,metadata=0;tree.deps.post=async(url,body)=>{posts++;request=body;throw Error('Failed to fetch')};tree.deps.api=async url=>{queries++;assert(url.includes(request.request_id));return {status:'completed',result:{action:'move',affected_ids:['p']}}};tree.refresh=async()=>{};tree.deps.metadataChanged=()=>metadata++;const result=await tree.action('move',{parent_id:'f'});assert.equal(posts,1);assert.equal(queries,1);assert.equal(metadata,1);assert.equal(result.action,'move');assert.equal(tree.pendingMove,null);assert.equal(request.items[0].revision,3);
    """)


def test_unknown_move_is_not_repeated_while_receipt_remains_unconfirmed():
    execute("""
    tree.rows=[{id:'p',kind:'material',revision:3}];tree.selected=new Set(['p']);let posts=0,lookup=0;tree.deps.post=async()=>{posts++;throw Error('Failed to fetch')};tree.deps.api=async()=>({status:'not_found'});tree.offerMoveLookup=()=>lookup++;await assert.rejects(tree.action('move',{parent_id:'f'}),/待确认/);const id=tree.pendingMove.request_id;await assert.rejects(tree.action('move',{parent_id:'f'}),/查询/);assert.equal(posts,1);assert.equal(tree.pendingMove.request_id,id);assert.equal(lookup,2);
    """)


def test_cancel_drag_restores_selection_and_releases_all_visual_state():
    execute("""
    tree.canvas.querySelectorAll=()=>[];tree.selected=new Set(['temporary']);tree.focusId='temporary';tree.expanded=new Set(['prior-folder','hover-folder']);tree.drag={selectedBefore:new Set(['prior']),focusBefore:'prior',autoExpanded:new Set(['hover-folder'])};tree.dragActive=true;tree.pointerActive=true;let removed=0;tree.dragFeedback={remove:()=>removed++};tree.dragGhost={remove:()=>removed++};tree.cancelDrag('cancelled');assert.deepEqual([...tree.selected],['prior']);assert.equal(tree.focusId,'prior');assert.equal(tree.drag,null);assert.equal(tree.dragActive,false);assert.equal(tree.pointerActive,false);assert.equal(removed,2);assert.equal(tree.status.textContent,'cancelled');assert.deepEqual([...tree.expanded],['prior-folder']);
    """)


def test_cancelled_hover_cannot_republish_drag_layout_after_async_load():
    execute("""
    tree.drag={token:1};tree.hoverTarget='f';let release,repaints=0;tree.load=()=>new Promise(resolve=>release=resolve);tree.refreshRows=()=>repaints++;const pending=tree.hoverExpand({id:'f'},1);tree.drag=null;release();await pending;assert.equal(repaints,0);
    """)


def test_drag_controls_never_start_drag_and_edge_scroll_stays_in_tree():
    execute("""
    let prevented=0;tree.pointerOriginControl=true;tree.startDrag({id:'f'},{preventDefault:()=>prevented++,target:{closest:()=>null}});assert.equal(prevented,1);let callback;ctx.requestAnimationFrame=fn=>{callback=fn;return 1};tree.drag={token:1};tree.host.scrollTop=100;tree.host.getBoundingClientRect=()=>({top:50,bottom:450});tree.edgeScroll(449);assert(tree.edgeSpeed<=12);callback();assert(tree.host.scrollTop>100&&tree.host.scrollTop<=112);
    """)


def test_material_identity_change_cancels_drag_before_tree_sync():
    execute("""
    tree.drag={currentId:'old'};tree.deps.current=()=>({id:'new'});let reason;tree.cancelDrag=message=>reason=message;ctx.Tree.prototype.syncCurrent.call(tree);assert(reason.includes('切换材料'));
    """)


def test_choose_renders_once_preserving_selection_focus_without_open():
    execute("""
    const a={id:'a',kind:'material'},b={id:'b',kind:'material'};tree.rows=[a,b];tree.rowHeight=32;tree.host.clientHeight=200;tree.host.scrollTop=0;
    ctx.CSS={escape:x=>x};let paints=0,focused;tree.renderWindow=()=>paints++;tree.canvas.querySelector=()=>({focus:()=>focused=tree.focusId});tree.deps.open=()=>{throw Error('selection must not open reader')};
    tree.choose(a);assert.equal(paints,1);assert.equal(focused,'a');assert.deepEqual([...tree.selected],['a']);tree.choose(b,{ctrlKey:true});assert.equal(paints,2);assert.deepEqual([...tree.selected],['a','b']);
    tree.focus('a');assert.equal(paints,3);assert.deepEqual([...tree.selected],['a','b']);
    """)


def test_scroll_persistence_coalesces_but_explicit_flush_keeps_latest_position():
    execute("""
    let callback,scheduled=0,cancelled=[],writes=[];ctx.setTimeout=fn=>{callback=fn;scheduled++;return scheduled};ctx.clearTimeout=id=>cancelled.push(id);ctx.localStorage.setItem=(key,value)=>writes.push(JSON.parse(value));delete tree.persist;
    tree.schedulePersist();tree.host.scrollTop=83;tree.schedulePersist();assert.equal(scheduled,1);assert.equal(writes.length,0);callback();assert.equal(writes.length,1);assert.equal(writes[0].scroll,83);
    tree.schedulePersist();tree.host.scrollTop=127;tree.persist();assert.equal(writes.length,2);assert.equal(writes[1].scroll,127);assert.equal(tree.persistTimer,null);assert(cancelled.includes(2));
    """)


def test_move_refreshes_only_source_destination_and_parent_counts():
    execute("""
    const a={id:'a',kind:'folder',title:'A'},b={id:'b',kind:'folder',title:'B'},c={id:'c',kind:'folder',title:'C'},p={id:'p',kind:'material',title:'Paper',parent_id:'a',revision:2};
    tree.cache.set('root',{nodes:[a,b,c]});tree.cache.set('a',{nodes:[p]});tree.cache.set('b',{nodes:[]});const untouched={nodes:[{id:'other',title:'Other'}]};tree.cache.set('c',untouched);tree.expanded=new Set(['a','b','c']);tree.rows=[a,p,b,c];let urls=[];
    tree.deps.api=async url=>{const parent=new URLSearchParams(url.split('?')[1]).get('parent_id');urls.push(parent);if(parent==='c')throw Error('unrelated folder must not gate movement');return {nodes:parent==='root'?[a,b,c]:parent==='b'?[{...p,parent_id:'b',revision:3}]:[],more:false}};
    await tree.refreshChanged('move',{parent_id:'b'},[p],{affected_ids:['p']});assert.deepEqual([...urls].sort(),['a','b','root']);assert.strictEqual(tree.cache.get('c'),untouched);assert.equal(tree.cache.get('a').nodes.length,0);assert.equal(tree.cache.get('b').nodes[0].id,'p');
    """)


def test_folder_rename_updates_loaded_descendant_paths_without_other_branches():
    execute("""
    const f={id:'f',kind:'folder',title:'Old',parent_id:null,path:''},sub={id:'sub',kind:'folder',title:'Sub',parent_id:'f',path:'Old'};
    tree.cache.set('root',{nodes:[f,{id:'other',kind:'folder',title:'Other'}]});tree.cache.set('f',{nodes:[sub]});tree.cache.set('sub',{nodes:[{id:'p',kind:'material',title:'Paper',parent_id:'sub',path:'Old/Sub'}]});tree.cache.set('other',{nodes:[]});let fetched=[];
    tree.deps.api=async url=>{const parent=new URLSearchParams(url.split('?')[1]).get('parent_id');fetched.push(parent);return {nodes:parent==='root'?[{...f,title:'New'}]:parent==='f'?[{...sub,path:'New'}]:[{id:'p',kind:'material',title:'Paper',parent_id:'sub',path:'New/Sub'}],more:false}};
    await tree.refreshChanged('rename',{title:'New'},[f],{affected_ids:['f']});assert.deepEqual([...fetched].sort(),['f','root','sub']);assert.equal(tree.cache.get('sub').nodes[0].path,'New/Sub');
    """)


def test_refresh_shows_root_before_slow_expanded_folder_and_bounds_reads():
    execute("""
    tree.cacheContext=JSON.stringify(['mine','','name']);tree.cache.set('root',{nodes:[{id:'prior',title:'Prior',kind:'material'}]});let releases=[],active=0,maximum=0;
    tree.expanded=new Set(['a','b','c','d','e']);tree.deps.api=async url=>{const parent=new URLSearchParams(url.split('?')[1]).get('parent_id');if(parent==='root')return {nodes:[{id:'now',title:'Now',kind:'material'}]};active++;maximum=Math.max(maximum,active);return new Promise(resolve=>releases.push(()=>{active--;resolve({nodes:[]})}));};
    const pending=tree.refresh();await new Promise(resolve=>setImmediate(resolve));assert.equal(tree.rows[0].id,'now');assert.equal(active,4);assert.equal(maximum,4);
    while(releases.length){releases.shift()();await new Promise(resolve=>setImmediate(resolve));}await pending;assert.equal(maximum,4);
    """)


def test_refresh_preserves_loaded_branch_page_extent_and_load_more_offset():
    execute("""
    tree.cache.set('f',{nodes:Array.from({length:720},(_,i)=>({id:String(i),title:String(i),kind:'material'}))});let offsets=[];
    tree.deps.api=async url=>{const p=new URLSearchParams(url.split('?')[1]),offset=Number(p.get('offset')),limit=Number(p.get('limit'));assert(limit>0&&limit<=500);offsets.push(offset);return {nodes:Array.from({length:limit},(_,i)=>({id:String(offset+i),title:'Item',kind:'document'})),more:true};};
    await tree.load('f',tree.epoch,false,true);assert.equal(tree.cache.get('f').nodes.length,720);assert.deepEqual(offsets,[0,500]);await tree.load('f',tree.epoch,true);assert.equal(tree.cache.get('f').nodes.length,920);assert.equal(offsets[2],720);
    """)


def test_late_management_receipt_does_not_refresh_replacement_space():
    execute("""
    tree.rows=[{id:'p',kind:'material',revision:2}];tree.selected=new Set(['p']);let finish,refreshes=0,metadata=0;tree.deps.post=()=>new Promise(resolve=>finish=resolve);tree.refreshChanged=async()=>refreshes++;tree.deps.metadataChanged=()=>metadata++;
    const pending=tree.action('rename',{title:'Changed'});tree.epoch++;tree.mode='examples';finish({action:'rename',affected_ids:['p']});await pending;assert.equal(refreshes,0);assert.equal(metadata,0);
    """)


def test_restore_refreshes_trash_branch_without_unrelated_expansion():
    execute("""
    tree.mode='trash';const f={id:'f',kind:'folder',title:'Deleted',parent_id:null},other={id:'other',kind:'folder',title:'Other'};tree.cache.set('root',{nodes:[f,other]});tree.cache.set('f',{nodes:[{id:'child',title:'Child'}]});const kept={nodes:[]};tree.cache.set('other',kept);tree.expanded=new Set(['f','other']);let fetched=[];
    tree.deps.api=async url=>{const q=new URLSearchParams(url.split('?')[1]);assert.equal(q.get('space'),'trash');fetched.push(q.get('parent_id'));return {nodes:[other],more:false}};await tree.refreshChanged('restore',{},[f],{affected_ids:['f','child']});assert.deepEqual(fetched,['root']);assert(!tree.cache.has('f'));assert(!tree.expanded.has('f'));assert.strictEqual(tree.cache.get('other'),kept);
    """)


def test_stale_branch_error_does_not_collapse_new_space_expansion():
    execute("""
    const f={id:'f',kind:'folder',title:'Folder'};tree.focus=()=>{};let fail,notifications=0;tree.load=()=>new Promise((resolve,reject)=>fail=reject);tree.deps.notify=()=>notifications++;const pending=tree.toggle(f);tree.epoch++;tree.mode='trash';tree.expanded=new Set(['f']);fail(Error('stale read failed'));await pending;assert(tree.expanded.has('f'));assert.equal(notifications,0);
    """)


def test_open_target_feedback_is_immediate_and_old_completion_cannot_clear_new_target():
    execute("""
    tree.cancelDrag=()=>{};let completions=[];tree.deps.open=id=>new Promise(resolve=>completions.push(resolve));
    const first=tree.open({id:'a',kind:'material',title:'First'});assert.equal(tree.status.textContent,'正在打开：First');
    const second=tree.open({id:'b',kind:'material',title:'Second'});assert.equal(tree.status.textContent,'正在打开：Second');completions[0]();await first;assert.equal(tree.status.textContent,'正在打开：Second');completions[1]();await second;assert.equal(tree.status.textContent,'');
    """)


def test_local_management_refresh_paints_after_related_branches_together():
    execute("""
    tree.cache.set('root',{nodes:[]});tree.cache.set('a',{nodes:[{id:'p',kind:'material',title:'Paper',parent_id:'a'}]});tree.cache.set('b',{nodes:[]});let completions=[],paints=0;tree.refreshRows=()=>paints++;tree.deps.api=()=>new Promise(resolve=>completions.push(resolve));
    const pending=tree.refreshChanged('move',{parent_id:'b'},tree.cache.get('a').nodes,{affected_ids:['p']});assert.equal(completions.length,2);completions[0]({nodes:[]});await new Promise(resolve=>setImmediate(resolve));assert.equal(paints,0);completions[1]({nodes:[{id:'p',kind:'document',title:'Paper',parent_id:'b'}]});await pending;assert.equal(paints,1);
    """)
