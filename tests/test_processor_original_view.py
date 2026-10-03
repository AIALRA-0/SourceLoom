"""Execute the shared original-view controller against its asynchronous boundary."""
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def run_case(case):
    script = r'''
    const fs=require('node:fs'),vm=require('node:vm');
    const arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const source=fs.readFileSync(arg.root+'/sourceloom/static/processor_workbench.js','utf8');
    class Node {
      constructor(){this.style={};this.dataset={};this.children=[];this.listeners={};this.hidden=false;
        this.clientWidth=800;this.clientHeight=400;this.naturalWidth=1000;this.naturalHeight=2000;
        this.classList={add(){},contains:()=>false};this.queries={};this.inert=false;this.isConnected=true;}
      append(...nodes){this.children.push(...nodes);} before(){} replaceWith(){} replaceChildren(){this.children=[];} setAttribute(k,v){this[k]=v;}
      removeAttribute(k){if(k==='src')this.src='';else delete this[k];}
      querySelector(s){return this.queries[s]||(this.queries[s]=new Node());} querySelectorAll(){return [];}
      addEventListener(k,f){this.listeners[k]=f;} showModal(){this.open=true;}
      close(){this.open=false;this.listeners.close?.();} focus(){this.focused=true;}
    }
    const nodes=new Map(),node=s=>{if(!nodes.has(s))nodes.set(s,new Node());return nodes.get(s);};
    let identity='first',restored=0,pending;
    const trigger=new Node();trigger.classList.contains=s=>s==='table-view-action';
    const context={console,AbortController,q:node,el:()=>new Node(),document:{querySelector:node,createElement:()=>new Node()},
      FileReader:class{readAsDataURL(){this.result='data:image/png;base64,AA==';this.onload();}},
      fetch:()=>arg.case==='late'?new Promise(r=>pending=r):Promise.resolve({ok:!arg.case.startsWith('failure'),blob:async()=>({}),headers:{get:()=>arg.case==='table'?'table':'page'}})};
    vm.createContext(context);vm.runInContext(source.slice(source.indexOf('export class DocumentLoupe')).replace('export class','class')+';this.DocumentLoupe=DocumentLoupe;',context);
    const deps={reader:{capture:()=>({left:'page12',right:'block9'}),identity:()=>identity,restore(){restored++;},jumpPage(){}},
      context:()=>({projectId:'project',versionId:'version'}),preview:()=>null,mappings:()=>[],representations:()=>[],resources:()=>[],materialTitle:()=> 'Example material'};
    if(arg.case==='unchanged-background'){const view={clientWidth:600,clientHeight:700,scrollHeight:20000,scrollTop:12093,scrollLeft:0};deps.reader.scroller=()=>view;}
    const loupe=new context.DocumentLoupe(deps);
    (async()=>{
      if(arg.case==='table-index'){
        const a={},b={},block={dataset:{blockId:'b1'},querySelectorAll:()=>[a,b]};b.closest=()=>block;b.ownerDocument={};
        deps.tableReferences=()=>[{block_id:'b1',table_index:0,page:3},{block_id:'b1',table_index:1,page:12,source_preview_precision:'page'}];
        const ref=loupe.tableReference(b);process.stdout.write(JSON.stringify({page:ref.page,precision:ref.precision}));return;
      }
      const info={page:12,url:'/saved/issue-image',pageURL:arg.case==='failure-fallback'?'/saved/page12':null,precision:'page',label:'表 1'};
      if(arg.case==='native'){info.html='<table><tr><th>Original header</th><td>42</td></tr></table>';info.precision='table';info.url=null;}
      if(arg.case==='failure-missing')info.url=null;
      const task=arg.case==='native-code'?loupe.openIssue({kind:'resource',source_kind:'code',source_preview_html:'<pre><code>print(42)</code></pre>',page:12},trigger):loupe.openReference(info,trigger);
      if(arg.case==='late'){identity='second';pending({ok:true,blob:async()=>({}),headers:{get:()=> 'table'}});}
      await task;if(loupe.image.src)loupe.image.onload();
      const result={title:node('#image-dialog-title').textContent,caption:node('#image-dialog-caption').textContent,
        src:loupe.image.src,hidden:loupe.image.hidden,status:loupe.status.textContent,role:loupe.status.role,
        button:trigger.textContent,zoom:loupe.zoom,readable:!loupe.image.hidden,htmlReadable:!loupe.htmlView.hidden,html:loupe.htmlView.innerHTML};
      loupe.dialog.close();result.restored=restored;result.focused=!!trigger.focused;result.cleared=!loupe.image.src;
      process.stdout.write(JSON.stringify(result));
    })().catch(e=>{console.error(e);process.exit(1);});
    '''
    result = subprocess.run([shutil.which('node'), '-e', script],
                            input=json.dumps({'root': str(ROOT), 'case': case}),
                            text=True, encoding='utf-8', capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout)


def test_full_page_fallback_is_not_mislabeled_as_a_table_crop():
    value = run_case('page')
    assert value['title'] == '原页 · Example material · 第 12 页'
    assert value['button'] == '查看原页'
    assert value['readable'] and value['zoom'] == .2
    assert value['restored'] == 1 and value['focused'] and value['cleared']


def test_trusted_region_response_can_offer_original_table():
    value = run_case('table')
    assert value['title'] == '表 1 · Example material · 第 12 页'
    assert value['button'] == '查看原表'
    assert '完整原表区域' in value['caption']


def test_failed_region_request_keeps_the_complete_saved_page_readable():
    value = run_case('failure-fallback')
    assert value['src'] == '/saved/page12'
    assert value['readable'] and value['button'] == '查看原页'


def test_absent_original_shows_recovery_instead_of_a_blank_dialog():
    value = run_case('failure-missing')
    assert value['hidden'] and value['role'] == 'alert'
    assert '检查已保存原件' in value['status']


def test_late_region_result_cannot_mutate_a_different_material():
    value = run_case('late')
    assert not value['src'] and not value['readable']
    assert value['restored'] == 0 and not value['focused']


def test_tables_in_same_block_use_their_specific_occurrence():
    assert run_case('table-index') == {'page': 12, 'precision': 'page'}


def test_native_source_table_keeps_its_original_grid_without_inventing_a_page_crop():
    value = run_case('native')
    assert value['htmlReadable'] and not value['readable']
    assert value['html'] == '<table><tr><th>Original header</th><td>42</td></tr></table>'
    assert value['title'] == '表 1 · Example material · 第 12 页'
    assert '已保存原生表格' in value['caption']


def test_native_code_resource_is_not_labeled_as_an_original_table():
    value = run_case('native-code')
    assert value['htmlReadable'] and value['html'] == '<pre><code>print(42)</code></pre>'
    assert value['title'] == '原始代码 · Example material · 第 12 页'
    assert '原生表格' not in value['caption']


def test_modal_close_does_not_approximate_an_unchanged_background_position():
    value = run_case('unchanged-background')
    assert value['restored'] == 0 and value['focused']


def test_issue_drawer_allows_modal_scrolling_but_stops_background_scroll():
    script = r'''
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const s=fs.readFileSync(arg.root+'/sourceloom/static/processor_issues.js','utf8');
    const code=s.slice(s.indexOf('export class IssueDrawer')).replace('export class','class');
    const ctx={};vm.createContext(ctx);vm.runInContext(code+';this.IssueDrawer=IssueDrawer;',ctx);
    const drawer=new ctx.IssueDrawer({host:{contains:()=>false,classList:{contains:()=>true}}});
    let prevented=0;drawer.blockWheel({target:{closest:()=>({})},preventDefault(){prevented++;}});
    if(prevented)throw Error('foreground modal was blocked');
    drawer.blockWheel({target:{closest:()=>null},preventDefault(){prevented++;}});
    if(prevented!==1)throw Error('background was allowed to scroll');process.stdout.write('true');
    '''
    result = subprocess.run([shutil.which('node'), '-e', script], input=json.dumps({'root': str(ROOT)}),
                            text=True, encoding='utf-8', capture_output=True, check=True, timeout=10)
    assert result.stdout == 'true'
