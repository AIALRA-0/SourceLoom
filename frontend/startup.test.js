import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const html=fs.readFileSync(new URL('../sourceloom/static/processor.html',import.meta.url),'utf8');
const app=fs.readFileSync(new URL('../sourceloom/static/processor.js',import.meta.url),'utf8');
const early=fs.readFileSync(new URL('../sourceloom/static/processor_startup.js',import.meta.url),'utf8');
function prepaint(value,{mobile=false,denyStorage=false}={}){
  const styles={},classes=new Set(),events={};
  const status={hidden:false,role:'status',message:{textContent:''},querySelector(){return this.message;},setAttribute(k,v){if(k==='role')this.role=v;}};
  const context={localStorage:{getItem(key){if(denyStorage)throw Error('storage disabled');return key==='sourceloom-workspace-layout/1'?null:value;}},matchMedia:()=>({matches:!mobile}),document:{documentElement:{dataset:{},style:{setProperty(k,v){styles[k]=v;}}},body:{classList:{add(c){classes.add(c);}}},getElementById(id){return id==='processor-entry'?{addEventListener(k,fn){events[k]=fn;}}:status;}}};
  vm.runInNewContext(early,context);return {width:styles['--workspace-left-width']||'256px',classes,events,status};
}
test('stable shell and empty-state decision exist before module execution',()=>{
  assert.ok(early);assert.match(html,/<section id="empty" class="empty" hidden>/);
  assert.match(html,/<link id="processor-tree-styles"[^>]*rel="stylesheet"/);
  assert.match(html,/<section id="startup-status"[^>]*role="status"/);
  assert.equal((html.match(/class="library-nav activity-rail"/g)||[]).length,1);
  assert.equal((html.match(/class="tree-tools"/g)||[]).length,1);
});
for(const [name,value,width] of [['missing',null,'256px'],['saved',JSON.stringify({width:340}),'340px'],['upper bound',JSON.stringify({width:900}),'400px'],['lower bound',JSON.stringify({width:100}),'216px'],['corrupt','not-json','256px'],['non numeric',JSON.stringify({width:'oops'}),'256px']]){
  test('prepaint geometry: '+name,()=>assert.equal(prepaint(value).width,width));
}
test('template startup does not restore the removed sidebar class',()=>{
  assert.equal(prepaint('{"collapsed":true}').classes.has('sidebar-collapsed'),false);
  assert.equal(prepaint('{"collapsed":true}',{mobile:true}).classes.has('sidebar-collapsed'),false);
});
test('unavailable preference storage leaves a usable shell',()=>assert.equal(prepaint(null,{denyStorage:true}).width,'256px'));
test('entry module error has explicit failure text and not false empty state',()=>{
  const state=prepaint(null);state.events.error();assert.equal(state.status.role,'alert');assert.match(state.status.message.textContent,/未能加载/);
});
async function startup(search,projects=[],last=null){
  const nodes=Object.fromEntries(['#startup-status','#empty','#acceptance-banner'].map(k=>[k,{hidden:k==='#empty'}]));
  const calls=[];const c={base:"/api/processor",URLSearchParams,location:{search},localStorage:{getItem(k){return k==='sourceloom-processor-project'?last:null;}},setMode(){},setCompareLeft(){},setMobileCompareSide(){},refreshProjects:async()=>{},api:async()=>({}),capabilities:{},projects,$:s=>nodes[s],notice(){},library:{show:async space=>calls.push(['space',space])},open:async(...args)=>calls.push(['open',...args])};
  const body=app.slice(app.indexOf('async function boot()'),app.indexOf('boot().catch('));
  vm.createContext(c);vm.runInContext(body,c);await c.boot();return {calls,nodes};
}
test('library route does not open remembered article before switching space',async()=>{
  const r=await startup('?space=examples',[{id:'old',state:'candidate'}],'old');
  assert.equal(JSON.stringify(r.calls),JSON.stringify([['space','examples']]));
});
test('handoff route resolves before opening a document',async()=>{
  const r=await startup('?material=new&tab=handoff',[{id:'old'}],'old');
  assert.equal(JSON.stringify(r.calls),JSON.stringify([['open','new','handoff']]));
});
test('true empty library shows onboarding only after list completion',async()=>{
  const r=await startup('');assert.equal(r.nodes['#empty'].hidden,false);assert.equal(r.nodes['#startup-status'].hidden,true);assert.deepEqual(r.calls,[]);
});
