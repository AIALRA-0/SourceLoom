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
const {IssueDrawer}=await import('data:text/javascript;base64,'+Buffer.from(readFileSync(SOURCE,'utf8')).toString('base64'));
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
