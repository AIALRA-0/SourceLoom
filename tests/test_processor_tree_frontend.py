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
