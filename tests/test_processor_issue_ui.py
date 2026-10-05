"""Exercise actual browser-controller save boundaries without touching user data."""
import json
from pathlib import Path
import shutil
import subprocess


def _run(code):
    source = Path(__file__).parents[1] / 'sourceloom/static/processor_issues.js'
    node = shutil.which('node')
    assert node, 'Node is required for the issue workflow JavaScript regression'
    harness = """
import {readFileSync} from 'node:fs';
const readSource=readFileSync(SOURCE.replace('processor_issues.js','processor_read.js'),'utf8');
const readURL='data:text/javascript;base64,'+Buffer.from(readSource).toString('base64');
const issueSource=readFileSync(SOURCE,'utf8').replace("'./processor_read.js?v=loading-reliability-20261004'",JSON.stringify(readURL));
const {IssueDrawer}=await import('data:text/javascript;base64,'+Buffer.from(issueSource).toString('base64'));
const memory=new Map();
globalThis.localStorage={getItem:k=>memory.get(k),setItem:(k,v)=>memory.set(k,v),removeItem:k=>memory.delete(k)};
const node={disabled:false,focus:()=>{}};
globalThis.document={activeElement:null};
const host={querySelector:()=>node,querySelectorAll:()=>[],classList:{contains:()=>true},contains:()=>false};
const drawer=new IssueDrawer({host});
drawer.context={projectId:'project-a',versionId:'v1'};
drawer.data={source_digest:'source-a',draft_digest:'draft-a'};
drawer.updateFeedback=()=>{};drawer.render=()=>{};drawer.reveal=()=>{};
const receipt={preview_id:'a'.repeat(64),request:{issue_id:'cell',action:'repair_table',difference_index:0},completion:'已修正1格',undo_label:'撤销本次修格'};
CODE
""".replace('SOURCE', json.dumps(str(source))).replace('CODE', code)
    completed = subprocess.run([node, '--input-type=module', '-e', harness], capture_output=True, text=True, encoding='utf-8', check=True)
    return json.loads(completed.stdout)


def test_double_click_dispatches_one_save():
    result = _run("""
let calls=0,resolve;drawer.api=()=>{calls++;return new Promise(r=>resolve=r)};
drawer.acceptResult=async()=>{drawer.pending=null;drawer.busy=false};
const first=drawer.apply(receipt);await drawer.apply(receipt);
resolve({processor:{active_version:'v2'}});await first;
process.stdout.write(JSON.stringify({calls,pending:drawer.pending}));
""")
    assert result == {'calls': 1, 'pending': None}


def test_refresh_after_save_does_not_cancel_current_open_completion():
    result = _run("""
let renders=0;drawer.readingSnapshot={};drawer.isOpen=()=>true;
drawer.onApplied=async()=>{drawer.epoch++};drawer.refresh=async()=>{drawer.epoch++};drawer.render=()=>renders++;
await drawer.acceptResult({processor:{active_version:'v2'}},{version_id:'v2',completion:'one cell'});
process.stdout.write(JSON.stringify({renders,completion:drawer.completion.completion}));
""")
    assert result == {'renders': 1, 'completion': 'one cell'}


def test_close_during_save_refresh_does_not_render_or_focus_late_completion():
    result = _run("""
let renders=0,refreshes=0,opened=true;drawer.readingSnapshot={};drawer.isOpen=()=>opened;
drawer.onApplied=async()=>{opened=false;drawer.epoch++};drawer.refresh=async()=>refreshes++;drawer.render=()=>renders++;
await drawer.acceptResult({processor:{active_version:'v2'}},{version_id:'v2',completion:'one cell'});
process.stdout.write(JSON.stringify({renders,refreshes}));
""")
    assert result == {'renders': 0, 'refreshes': 0}


def test_actual_page_number_takes_precedence_over_generic_action_label():
    result = _run("""
const group={page:13,action_labels:{add_page_reference:'添加原页参考'}};
process.stdout.write(JSON.stringify({label:drawer.actionLabel(group,'add_page_reference')}));
""")
    assert result['label'] == '添加第 13 页原页参考'


def test_uncertain_save_keeps_original_identity_and_only_queries_it():
    result = _run("""
const paths=[];drawer.api=async(path,body)=>{paths.push({path,post:body!==undefined});if(body!==undefined)throw Error('response lost');if(path.includes('/issue-operations/'))return {status:'saved',version_id:'v2',active:true,preview_id:receipt.preview_id};return {processor:{active_version:'v2'}}};
await drawer.apply(receipt);await drawer.apply(receipt);
const pending=JSON.parse(memory.get(drawer.pendingKey()));
drawer.acceptResult=async()=>{drawer.pending=null};await drawer.queryPending();
process.stdout.write(JSON.stringify({paths,pending}));
""")
    assert len([p for p in result['paths'] if p['post']]) == 1
    assert result['pending']['preview_id'] == 'a' * 64
    assert result['pending']['source_digest'] == 'source-a'
    assert result['paths'][1]['path'].endswith('/issue-operations/' + 'a' * 64)
    assert all(not p['post'] for p in result['paths'][1:])


def test_late_save_does_not_overwrite_current_material():
    result = _run("""
let resolve,accepted=0;drawer.api=()=>new Promise(r=>resolve=r);drawer.acceptResult=async()=>accepted++;
const operation=drawer.apply(receipt);drawer.epoch++;drawer.context={projectId:'project-b',versionId:'b1'};
resolve({processor:{active_version:'v2'}});await operation;
process.stdout.write(JSON.stringify({accepted,project:drawer.context.projectId,retained:memory.has('sourceloom-issue-operation:project-a')}));
""")
    assert result == {'accepted': 0, 'project': 'project-b', 'retained': True}


def test_rejected_stale_preview_is_not_automatically_resubmitted():
    result = _run("""
let calls=0;drawer.api=async()=>{calls++;const error=Error('旧预览失效');error.status=409;throw error};
await drawer.apply(receipt);
process.stdout.write(JSON.stringify({calls,pending:drawer.pending,retained:memory.has(drawer.pendingKey()),message:drawer.feedback.message}));
""")
    assert result['calls'] == 1
    assert result['pending'] is None and not result['retained']
    assert result['message'] == '旧预览失效'


def test_uncertain_undo_queries_its_original_operation_without_resubmitting():
    result = _run("""
drawer.undoVersion='v2';drawer.completion={preview_id:receipt.preview_id};
const paths=[];drawer.api=async(path,body)=>{paths.push({path,post:body!==undefined});if(body!==undefined)throw Error('response lost');if(path.includes('/issue-operations/'))return {status:'undone',version_id:'v1',preview_id:receipt.preview_id};return {processor:{active_version:'v1'}}};
await drawer.undo();await drawer.undo();const pending=drawer.pending;
drawer.acceptResult=async(result,completion)=>{drawer.pending=null;drawer.completion=completion};await drawer.queryPending();
process.stdout.write(JSON.stringify({paths,pending,completion:drawer.completion}));
""")
    assert len([p for p in result['paths'] if p['post']]) == 1
    assert result['pending']['operation'] == 'undo'
    assert result['paths'][1]['path'].endswith('/issue-operations/' + 'a' * 64)
    assert result['completion']['status'] == 'undone'
    assert result['completion']['undo_available'] is False


def test_file_recovery_completion_never_offers_to_undo_bytes_to_missing():
    result = _run("""
drawer.refresh=async()=>{};
await drawer.acceptResult({processor:{active_version:'v1'}},{version_id:'v1',undo_available:false,completion:'已恢复原图文件'});
process.stdout.write(JSON.stringify({undoVersion:drawer.undoVersion,version:drawer.context.versionId,completion:drawer.completion.completion}));
""")
    assert result == {'undoVersion': None, 'version': 'v1', 'completion': '已恢复原图文件'}



_SOURCE_LOAD = r"""
const group={id:'src-00001',kind:'table',source_preview_url:'/saved-source-image',actions:['confirm_manual']};
drawer.data={source_digest:'source-a',draft_digest:'draft-a',issues:[group]};drawer.selected=group.id;
const image={dataset:{sourceUrl:'/saved-source-image'},hidden:true,naturalWidth:640,naturalHeight:960,removeAttribute(k){delete this[k];},decode:async()=>{}};
const retry={hidden:true},status={hidden:false,setAttribute(){},querySelector:s=>s==='p'?{textContent:''}:retry};
const pane={isConnected:true,dataset:{},querySelectorAll:s=>s==='img[data-source-url]'?[image]:[],querySelector:s=>s==='[data-source-retry]'?retry:s==='.issue-source-status'?status:null};
host.querySelector=s=>s==='.issue-source-pane'?pane:null;host.querySelectorAll=()=>[];
const updates=[];drawer.updateSource=()=>updates.push(drawer.sourceLoad?.state);
let created=0,revoked=0;URL.createObjectURL=()=>{created++;return 'blob:synthetic-'+created;};URL.revokeObjectURL=()=>revoked++;
"""


def test_table_confirmation_waits_for_actual_current_image_decode():
    result = _run(_SOURCE_LOAD+r"""
let finish,calls=0,decodeStarted=false;image.decode=()=>{decodeStarted=true;return new Promise(r=>finish=r);};globalThis.fetch=async()=>new Response('image-bytes',{headers:{'Content-Type':'image/png'}});drawer.api=async()=>calls++;
const loading=drawer.loadSource(group);while(!decodeStarted)await new Promise(r=>setImmediate(r));
const before={state:drawer.sourceLoad.state,ready:drawer.sourceReady(),hidden:image.hidden};await drawer.beginAction('confirm_manual');await drawer.preview('confirm_manual',{acknowledged:true});await drawer.apply({action:'confirm_manual'});
finish();await loading;const after={state:drawer.sourceLoad.state,ready:drawer.sourceReady(),hidden:image.hidden};drawer.cancelSource();
process.stdout.write(JSON.stringify({before,after,calls,revoked}));
""")
    assert result['before'] == {'state':'loading','ready':False,'hidden':True}
    assert result['after'] == {'state':'ready','ready':True,'hidden':False}
    assert result['calls'] == 0 and result['revoked'] == 1


def test_image_decode_failure_has_local_retry_and_never_becomes_ready():
    result = _run(_SOURCE_LOAD+r"""
let calls=0;globalThis.fetch=async()=>{calls++;return new Response('bad-image',{headers:{'Content-Type':'image/png'}})};
image.decode=async()=>{throw Error('image decoding failed')};await drawer.loadSource(group);
const failed={state:drawer.sourceLoad.state,ready:drawer.sourceReady(),hidden:image.hidden,urls:drawer.sourceLoad.urls.size};
image.decode=async()=>{};await drawer.loadSource(group,{retry:true});await drawer.loadSource(group);
const ready=drawer.sourceReady();drawer.cancelSource();process.stdout.write(JSON.stringify({failed,ready,calls,created,revoked}));
""")
    assert result['failed']=={'state':'failed','ready':False,'hidden':True,'urls':0}
    assert result['ready'] and result['calls']==2
    assert result['created']==2 and result['revoked']==2


def test_wrong_response_type_and_authentication_do_not_unlock_confirmation():
    result = _run(_SOURCE_LOAD+r"""
const failures=[];
for(const [status,type] of [[401,'application/json'],[404,'application/json'],[200,'text/html']]){
 globalThis.fetch=async()=>new Response('<html>login</html>',{status,headers:{'Content-Type':type}});await drawer.loadSource(group,{retry:true});failures.push({state:drawer.sourceLoad.state,ready:drawer.sourceReady(),message:drawer.sourceLoad.error});
}
process.stdout.write(JSON.stringify({failures,created,revoked}));
""")
    assert all(x['state']=='failed' and not x['ready'] for x in result['failures'])
    assert '登录' in result['failures'][0]['message']
    assert '不存在' in result['failures'][1]['message']
    assert '文件类型' in result['failures'][2]['message']
    assert result['created']==0 and result['revoked']==0


def test_cancelled_late_source_response_cannot_attach_to_another_material():
    result = _run(_SOURCE_LOAD+r"""
let finish;globalThis.fetch=()=>new Promise(r=>finish=r);const old=drawer.loadSource(group);
drawer.cancelSource();drawer.context={projectId:'project-b',versionId:'v2'};await old;
finish(new Response('late',{headers:{'Content-Type':'image/png'}}));await new Promise(r=>setImmediate(r));
process.stdout.write(JSON.stringify({load:drawer.sourceLoad,created,ready:drawer.sourceReady(),src:image.src||null}));
""")
    assert result=={'load':None,'created':0,'ready':False,'src':None}


def test_switch_during_decode_releases_blob_and_blocks_stale_readiness():
    result = _run(_SOURCE_LOAD+r"""
let finish,started=false;globalThis.fetch=async()=>new Response('image',{headers:{'Content-Type':'image/png'}});image.decode=()=>{started=true;return new Promise(r=>finish=r)};
const old=drawer.loadSource(group);while(!started)await new Promise(r=>setImmediate(r));drawer.cancelSource();drawer.context={projectId:'project-b',versionId:'v2'};await old;finish();await new Promise(r=>setImmediate(r));
process.stdout.write(JSON.stringify({created,revoked,ready:drawer.sourceReady(),hidden:image.hidden}));
""")
    assert result=={'created':1,'revoked':1,'ready':False,'hidden':True}


def test_unfinished_image_decode_reaches_a_bounded_failure():
    result = _run(_SOURCE_LOAD+r"""
const nativeTimer=setTimeout;let delay;
globalThis.setTimeout=(fn,ms)=>{delay=ms;return nativeTimer(fn,1)};
globalThis.fetch=async()=>new Response('image',{headers:{'Content-Type':'image/png'}});
image.decode=()=>new Promise(()=>{});await drawer.loadSource(group);
process.stdout.write(JSON.stringify({delay,state:drawer.sourceLoad.state,ready:drawer.sourceReady(),message:drawer.sourceLoad.error,created,revoked}));
""")
    assert result['delay']==15000
    assert result['state']=='failed' and not result['ready']
    assert '解码' in result['message']
    assert result['created']==1 and result['revoked']==1


def test_completed_decode_without_visible_image_pixels_cannot_confirm():
    result = _run(_SOURCE_LOAD+r"""
globalThis.fetch=async()=>new Response('image',{headers:{'Content-Type':'image/png'}});
image.naturalWidth=0;image.naturalHeight=0;await drawer.loadSource(group);
process.stdout.write(JSON.stringify({state:drawer.sourceLoad.state,ready:drawer.sourceReady(),created,revoked}));
""")
    assert result=={'state':'failed','ready':False,'created':1,'revoked':1}


def test_ready_preview_does_not_authorize_another_saved_version():
    result = _run(_SOURCE_LOAD+r"""
globalThis.fetch=async()=>new Response('image',{headers:{'Content-Type':'image/png'}});
await drawer.loadSource(group);const ready=drawer.sourceReady();drawer.context.versionId='v2';let calls=0;drawer.api=async()=>calls++;
await drawer.preview('confirm_manual',{acknowledged:true});await drawer.apply({action:'confirm_manual'});
process.stdout.write(JSON.stringify({before:ready,after:drawer.sourceReady(),calls}));
""")
    assert result=={'before':True,'after':False,'calls':0}


def test_close_clears_unsubmitted_manual_choice_before_reopening():
    result = _run(r"""
drawer.manualReady=true;drawer.isOpen=()=>false;host.classList.remove=()=>{};drawer.close();
process.stdout.write(JSON.stringify({manualReady:drawer.manualReady}));
""")
    assert result=={'manualReady':False}
