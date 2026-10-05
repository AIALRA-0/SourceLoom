"""PDF first-page readiness and bounded failure use the actual view lifecycle."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = r'''
import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
const source=fs.readFileSync(process.argv[1],'utf8');
const gate=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject};};
class Element{
 constructor(tag,cls=''){Object.assign(this,{tag,className:cls,children:[],dataset:{},isConnected:true,clientWidth:500,clientHeight:300});this.style={setProperty(k,v){this[k]=v}};this.classList={add:cls=>this.className+=' '+cls};}
 append(n){this.children.push(n);n.parent=this;}prepend(n){this.children.unshift(n);n.parent=this;}
 replaceChildren(...nodes){this.children=[];nodes.forEach(n=>this.append(n));}remove(){if(this.parent)this.parent.children=this.parent.children.filter(n=>n!==this);}
 setAttribute(){}getBoundingClientRect(){return {top:0,bottom:300};}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 querySelectorAll(selector){const page=/data-page="(\d+)"/.exec(selector);return this.children.flatMap(n=>[n,...n.querySelectorAll(selector)]).filter(n=>selector==='img'?n.tag==='img':page?Number(n.dataset.page)===Number(page[1]):n.className.split(' ').includes(selector.slice(1)));}
}
class Viewport{constructor({viewBox=[0,0,612,792],userUnit=1,scale=1,rotation=0}={}){Object.assign(this,{viewBox,userUnit,scale,rotation});this.width=(viewBox[2]-viewBox[0])*scale;this.height=(viewBox[3]-viewBox[1])*scale;this.transform=[scale,0,0,-scale,0,this.height];}}
const proxy=n=>({pageNumber:n,view:[0,0,612,792],getViewport:o=>new Viewport(o),getTextContent:async()=>({items:[{str:'page '+n}]})});
let tasks=[];const timers=new Set();
const context={pdfjs:{getDocument(){const task={promise:Promise.resolve(context.doc),destroy:async()=>{task.destroyed=true;}};tasks.push(task);return task;}},
 URL,vendor:new URL('http://localhost/vendor/'),document:{createElement:tag=>new Element(tag)},location:{href:'http://localhost/'},window:{getSelection:()=>({isCollapsed:true})},
 setTimeout:(fn,ms)=>{const t=setTimeout(()=>{timers.delete(t);fn();},ms);timers.add(t);return t;},clearTimeout:t=>{clearTimeout(t);timers.delete(t);}};
vm.createContext(context);vm.runInContext(source.slice(source.indexOf('const element=')).replace('export class PDFDocumentView','class PDFDocumentView')+';globalThis.View=PDFDocumentView;globalThis.acquire=acquireDocument;',context);
function view(doc){context.doc=doc;const v=Object.create(context.View.prototype),container=new Element('div'),stack=new Element('div','source-document');container.append(stack);
 for(let n=1;n<=doc.numPages;n++){const s=new Element('section','source-sheet'),img=new Element('img');s.dataset.page=String(n);img.width=612;img.height=792;s.append(img);stack.append(s);}
 Object.assign(v,{container,url:'/original-'+Math.random(),identity:{sourceDigest:'frozen'},rows:[],revision:0,scale:1,rotation:0,mode:'width',hits:[],beforeLayout:()=>({left:{page:2}}),valid:()=>true,emit(){},captureSelection:()=>null,renderVisible:async()=>{},renderPage:async row=>{row.canvas={};v.drawn=row.page;}});return v;}
const results={};
{
 const distant=gate(),outline=gate(),calls=[];
 const doc={numPages:3,getPage(n){calls.push(n);return n===2?Promise.resolve(proxy(n)):distant.promise;},getOutline:()=>outline.promise};
 const v=view(doc);v.buildIndex=async()=>{};await v.open();
 assert.deepEqual(calls,[2]);assert.equal(v.drawn,2);assert.equal(v.rows.length,3);assert.ok(v.rows.every(r=>Number.parseFloat(r.sheet.style.height)>0));
 assert.equal(v.rows[0].pdfPage,null);assert.equal(v.outlineItems,undefined);results.bookmark_first_without_remote_pages_or_outline=true;
 v.valid=()=>false;outline.resolve([{title:'late other material'}]);await v.outlinePromise;assert.equal(v.outlineItems,undefined);results.late_outline_cannot_publish=true;
}
{
 const late=gate(),doc={numPages:1,getPage:()=>late.promise};const v=view(doc),row={page:1,pdfPage:null};v.doc=doc;const pending=v.ensurePage(row);v.valid=()=>false;late.resolve(proxy(1));await pending;assert.equal(row.pdfPage,null);results.late_page_cannot_publish=true;
}
{
 const never=gate(),doc={numPages:2,getPage:()=>never.promise},v=view(doc);v.waitTimeout=5;v.doc=doc;
 const row={page:1,pdfPage:null,sheet:new Element('section')};let failure;
 v.failure=(r,error,retry)=>{failure={r,error,retry};};v.viewport={};row.viewport=new Viewport();v.renderPage=context.View.prototype.renderPage;
 await v.renderPage(row);assert.equal(failure.r,row);assert.equal(failure.error.name,'PDFTimeoutError');assert.equal(row.failed,true);assert.equal(row.pagePromise,null);
 let retryPage;v.renderPage=r=>retryPage=r;failure.retry();assert.equal(retryPage,row);assert.equal(row.failed,false);
 never.resolve(proxy(1));await new Promise(r=>setImmediate(r));assert.equal(row.pdfPage,null);results.page_timeout_retry_is_local_and_late_result_ignored=true;
}
{
 const v=view({numPages:1}),row={page:1,sheet:new Element('section'),frame:{}};let retries=0;
 v.failure(row,{status:401,message:'transport'},()=>retries++);const status=row.sheet.querySelector('.page-load-state');
 assert.ok(status.children[0].textContent.includes('登录已失效'));assert.equal(status.children[2].href,v.url);assert.equal(status.style.inset,'auto 0 0');
 status.children[1].onclick();assert.equal(retries,1);
 v.failure(row,{name:'InvalidPDFException',message:'Invalid PDF structure'},()=>{});
 assert.ok(row.sheet.querySelector('.page-load-state').children[0].textContent.includes('不是可读取的 PDF'));results.failure_has_original_and_explicit_retry=true;
}
{
 context.doc={numPages:1};const a=context.acquire('shared',{url:'same'}),b=context.acquire('shared',{url:'same'}),task=a.task;
 a.release({invalidate:true});assert.equal(task.destroyed,undefined);const c=context.acquire('shared',{url:'same'});assert.notEqual(c.task,task);
 b.release();await Promise.resolve();assert.equal(task.destroyed,true);c.release({invalidate:true});results.failed_lease_retry_keeps_active_sibling=true;
}
for(const timer of timers)clearTimeout(timer);console.log(JSON.stringify(results));
'''


@pytest.fixture(scope='module')
def readiness_results():
    source = Path(__file__).resolve().parents[1] / 'sourceloom/static/processor_pdf.js'
    result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', SCRIPT, str(source)],
                            capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize('case', [
    'bookmark_first_without_remote_pages_or_outline', 'late_outline_cannot_publish',
    'late_page_cannot_publish', 'page_timeout_retry_is_local_and_late_result_ignored',
    'failure_has_original_and_explicit_retry', 'failed_lease_retry_keeps_active_sibling',
])
def test_pdf_current_page_readiness(readiness_results, case):
    assert readiness_results[case]
