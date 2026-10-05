"""Run asynchronous PDF frame publication against controlled render promises."""
from pathlib import Path
import json
import shutil
import subprocess
import pytest

SCRIPT = r'''
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const source=fs.readFileSync(process.argv[1],'utf8');
const gate=()=>{let resolve,reject;let promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}};
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.parent=null;this.dataset={};this.style={setProperty(k,v){this[k]=v}};this.width=100;this.height=200;}
 append(n){n.parent=this;this.children.push(n)} prepend(n){n.parent=this;this.children.unshift(n)}
 remove(){if(this.parent){this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null}}
 replaceWith(n){const p=this.parent,i=p.children.indexOf(this);p.children[i]=n;n.parent=p;this.parent=null}
 replaceChildren(...nodes){this.children=[];nodes.forEach(n=>this.append(n))}
 querySelector(){return null} querySelectorAll(){return []} getContext(){return {}}
 getBoundingClientRect(){return this.box||{top:0,bottom:200}} setAttribute(){}
}
let textGate=null;
const pdfjs={AnnotationMode:{ENABLE:1},TextLayer:class{constructor({container}){this.container=container}async render(){if(textGate)await textGate.promise;this.container.ready=true}cancel(){}}};
const context={pdfjs,URL,setTimeout,clearTimeout,document:{createElement:tag=>new Element(tag)},location:{href:'http://localhost/'},window:{devicePixelRatio:1},Node:{TEXT_NODE:3}};vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('const element=')).replace('export class PDFDocumentView','class PDFDocumentView')+';globalThis.View=PDFDocumentView;globalThis.transform=viewportTransform;',context);
const viewport={width:100,height:200,scale:1,transform:[1,0,0,-1,0,200]};
function setup(){const drawn=gate(),sheet=new Element('section'),frame=new Element('div'),canvas=new Element('canvas');frame.append(canvas);sheet.append(frame);const row={page:1,sheet,frame,canvas,viewport,text:{items:[{str:'native text'}]},renderRevision:0,textLayer:{cancel(){}},pdfPage:{getViewport:()=>viewport,render(){return {promise:drawn.promise,cancel(){drawn.reject(Object.assign(new Error(),{name:'RenderingCancelledException'}))}}}}};const v=Object.create(context.View.prototype);Object.assign(v,{revision:1,rows:[row],doc:{},container:new Element('div'),valid:()=>true,captureSelection:()=>null,emit(){},paintHits(){},renderLinks:async()=>new Element('links')});return {v,row,drawn,old:frame,canvas};}
const result={};
{
 const {v,row,drawn,old,canvas}=setup();textGate=gate();const pending=v.renderPage(row);assert.equal(row.frame,old);assert.equal(row.sheet.children[0],old);drawn.resolve();await new Promise(r=>setImmediate(r));assert.equal(row.frame,old,'bitmap completion must await text');textGate.resolve();await pending;assert.notEqual(row.frame,old);assert.equal(row.sheet.children.length,1);assert.equal(row.frame.children.length,3);assert.equal(row.textLayer.container.ready,true);assert.equal(canvas.width,0);result.atomic_publish=true;textGate=null;
}
{
 const {v,row,drawn,old}=setup();const pending=v.renderPage(row);v.revision++;drawn.resolve();await pending;assert.equal(row.frame,old);assert.equal(row.sheet.children[0],old);result.stale_revision_preserves_old=true;
}
{
 const {v,row,drawn,old}=setup();const pending=v.renderPage(row);v.valid=()=>false;drawn.resolve();await pending;assert.equal(row.frame,old);result.stale_identity_preserves_old=true;
}
{
 const {v,row,drawn,old}=setup();const pending=v.renderPage(row);drawn.reject(new Error('drawing failed'));await pending;assert.equal(row.frame,old);assert.equal(row.canvas.width,100);result.render_error_preserves_old=true;
}
{
 const {v,row,drawn}=setup();const pending=v.renderPage(row);const second=v.renderPage(row);assert.equal(row.sheet.children.length,1);drawn.resolve();await Promise.all([pending,second]);assert.equal(row.sheet.children.length,1);result.concurrent_passes_share_frame=true;
}
{
 const from=viewport,to={transform:[0,2,2,0,0,0]};const matrix=context.transform(from,to);const point=[25,30];const mapped=[matrix[0]*point[0]+matrix[2]*point[1]+matrix[4],matrix[1]*point[0]+matrix[3]*point[1]+matrix[5]];assert.deepEqual(mapped,[340,50]);result.rotation_transforms_entire_frame=true;
}
{
 const {v,row}=setup();v.container.clientHeight=200;v.container.getBoundingClientRect=()=>({top:0,bottom:200});row.sheet.box={top:2000,bottom:2200};await v.renderVisible();assert.equal(row.frame,null);assert.equal(row.canvas,null);assert.equal(row.sheet.children.length,0);assert.equal(row.renderRevision,-1);result.distant_frame_reclaimed=true;
}
{
 const {v,row}=setup();v.container.clientHeight=200;v.container.getBoundingClientRect=()=>({top:0,bottom:200});row.sheet.box={top:2000,bottom:2200};context.window.getSelection=()=>({isCollapsed:false,containsNode:()=>true});await v.renderVisible();assert.ok(row.frame);assert.ok(row.textLayer);assert.equal(row.canvas,null);assert.equal(row.frame.children.length,0);delete context.window.getSelection;result.distant_selection_keeps_text_not_bitmap=true;
}
{
 const {v,row}=setup();Object.assign(v,{rows:[],scale:1,mode:'width',rotation:0,stack:new Element('div')});
 v.container.clientWidth=400;v.container.clientHeight=200;let layouts=0;
 v.beforeLayout=()=>layouts++;v.afterLayout=()=>layouts++;v.captureSelection=()=>{throw Error('selection captured before page proxies ready')};
 const resizeBody=source.slice(source.indexOf('this.resize=new ResizeObserver(()=>{')+'this.resize=new ResizeObserver(()=>{'.length,source.indexOf('});this.resize.observe(container);'));
 vm.runInContext(`globalThis.resizeWhileLoading=function(container){${resizeBody}}`,context);
 context.window.getSelection=()=>({isCollapsed:true});context.resizeWhileLoading.call(v,v.container);v.setScale(2);v.rotate();
 assert.equal(layouts,0);assert.equal(v.revision,1);assert.equal(v.scale,2);assert.equal(v.rotation,90);
 v.container.getBoundingClientRect=()=>{throw Error('geometry read before page proxies ready')};await v.renderVisible();
 result.pending_page_proxies_ignore_layout=true;
 v.rows=[row];row.viewport=null;row.pdfPage.getViewport=()=>viewport;v.captureSelection=()=>null;let rendered=0;v.renderVisible=async()=>rendered++;
 v.relayout();await new Promise(r=>setImmediate(r));assert.equal(layouts,2);assert.equal(rendered,1);assert.equal(v.revision,2);
 assert.equal(row.sheet.style.width,'100px');assert.equal(row.sheet.style.height,'200px');assert.equal(row.viewport,viewport);
 result.ready_page_proxies_resume_layout=true;delete context.window.getSelection;
}
{
 const {v,row}=setup();Object.assign(v,{scale:.75,mode:'custom',rotation:0,stack:new Element('div')});v.container.clientWidth=400;v.container.clientHeight=200;row.pdfPage.getViewport=()=>viewport;let captures=0;v.beforeLayout=()=>captures++;v.relayout();assert.equal(v.revision,1);assert.equal(captures,0);result.unchanged_geometry_skips_rebuild=true;
}
{
 const {v,row,drawn,old}=setup(),loaded=gate();const page=row.pdfPage;row.pdfPage=null;v.doc.getPage=()=>loaded.promise;v.wanted=new Set([row]);
 const pending=v.renderPage(row,{visibleOnly:true});v.wanted.clear();loaded.resolve(page);await pending;
 assert.equal(row.frame,old);assert.equal(row.pendingFrame,undefined,'late offscreen proxy must not allocate a frame');
 const explicit=v.renderPage(row);drawn.resolve();await explicit;assert.notEqual(row.frame,old,'explicit offscreen destinations must remain renderable');
 result.late_proxy_stays_offscreen_but_explicit_destination_renders=true;
}
{
 const {v,row,drawn,old}=setup();v.wanted=new Set([row]);const pending=v.renderPage(row,{visibleOnly:true});v.wanted.clear();drawn.resolve();await pending;
 assert.equal(row.frame,old);assert.equal(row.sheet.children[0],old);result.late_visible_bitmap_cannot_publish_offscreen=true;
}
console.log(JSON.stringify(result));
'''

@pytest.fixture(scope='module')
def pdf_regressions():
    node=shutil.which('node')
    assert node, 'Node is required for asynchronous browser regressions'
    source=Path(__file__).resolve().parents[1]/'sourceloom/static/processor_pdf.js'
    completed=subprocess.run([node,'--input-type=module','-e',SCRIPT,str(source)],capture_output=True,text=True,encoding='utf-8',timeout=15)
    assert completed.returncode==0,completed.stderr
    return json.loads(completed.stdout)

@pytest.mark.parametrize('case',[
    'atomic_publish','stale_revision_preserves_old','stale_identity_preserves_old',
    'render_error_preserves_old','concurrent_passes_share_frame','rotation_transforms_entire_frame',
    'distant_frame_reclaimed','distant_selection_keeps_text_not_bitmap','unchanged_geometry_skips_rebuild',
    'pending_page_proxies_ignore_layout','ready_page_proxies_resume_layout',
    'late_proxy_stays_offscreen_but_explicit_destination_renders','late_visible_bitmap_cannot_publish_offscreen',
])
def test_pdf_frame_lifecycle(pdf_regressions,case):
    assert pdf_regressions[case]


def test_search_highlights_only_measure_pages_with_hits_and_clear_stale_marks():
    script = r'''
    const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
    const source=fs.readFileSync(process.argv[1],'utf8');
    const method=source.slice(source.indexOf('  paintHits(row){'),source.indexOf('  async copy('));
    let measurements=0,spanQueries=0;
    function element(tag,cls){return {tag,className:cls,style:{},children:[],parent:null,
      append(n){n.parent=this;this.children.push(n);},
      remove(){if(this.parent){this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null;}}};}
    const context={element,Node:{TEXT_NODE:3},document:{createRange:()=>({setStart(){},setEnd(){},
      getClientRects:()=>[{left:30,top:125,width:40,height:12}]})}};
    vm.createContext(context);vm.runInContext('class View{'+method+'};globalThis.View=View;',context);
    const frame=element('div'),sheet={
      querySelector:()=>frame.children.find(n=>n.className==='pdf-search-highlights'),
      querySelectorAll(){spanQueries++;return [{firstChild:{nodeType:3},textContent:'native text'}];},
      getBoundingClientRect(){measurements++;return {left:20,top:100};}};
    const row={page:1,sheet,frame,textLayer:{},text:{items:[{str:'native text'}]}};
    const view=Object.create(context.View.prototype);view.hits=[];view.hitIndex=0;
    frame.append(element('div','pdf-search-highlights'));
    view.paintHits(row);assert.equal(frame.children.length,0);assert.equal(measurements,0);assert.equal(spanQueries,0);
    view.hits=[{page:2,pieces:[]}];view.paintHits(row);
    assert.equal(frame.children.length,0);assert.equal(measurements,0);assert.equal(spanQueries,0);
    view.hits.push({page:1,pieces:[{item:0,start:0,end:6}]});view.hitIndex=1;view.paintHits(row);
    assert.equal(measurements,1);assert.equal(spanQueries,1);assert.equal(frame.children.length,1);
    const mark=frame.children[0].children[0];
    assert.equal(mark.className,'pdf-search-hit pdf-search-hit-current');
    assert.deepEqual(mark.style,{left:'10px',top:'25px',width:'40px',height:'12px'});
    view.hits=[];view.paintHits(row);
    assert.equal(frame.children.length,0);assert.equal(measurements,1);assert.equal(spanQueries,1);
    process.stdout.write('true');
    '''
    node = shutil.which('node')
    source = Path(__file__).resolve().parents[1]/'sourceloom/static/processor_pdf.js'
    completed = subprocess.run([node, '-e', script, str(source)], capture_output=True,
                               text=True, encoding='utf-8', timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == 'true'
