import test from 'node:test';
import assert from 'node:assert/strict';
import {layoutBudget,normalizeLayout,TemplateWorkspace} from '../sourceloom/static/processor_workspace.js';
test('template size budget preserves main region and panel constraints',()=>{
 for(const area of [900,1100,1400]){const b=layoutBudget(area,'wide',normalizeLayout({leftWidth:400,rightWidth:400}));assert.ok(b.lw>=216&&b.rw>=216);assert.ok(area-b.lw-b.rw-32>=360);}
});
test('template medium and narrow hide physical panels without losing config',()=>{
 const c=normalizeLayout({leftView:'inspector',leftWidth:350,rightWidth:300});
 assert.equal(layoutBudget(800,'medium',c).rw,0);assert.equal(layoutBudget(650,'narrow',c).lw,0);assert.equal(c.leftView,'inspector');assert.equal(c.rightWidth,300);
});
test('one hidden physical pane returns width budget to the main view',()=>{
 const b=layoutBudget(1100,'wide',normalizeLayout({hideLeft:true,rightWidth:400}));assert.equal(b.lw,0);assert.equal(b.rw,400);assert.equal(b.left,false);
});
test('invalid layout preference cannot inject panel identity or unbounded widths',()=>{
 assert.deepEqual(normalizeLayout({leftView:'bogus',leftWidth:9999,rightWidth:NaN}),{leftView:'explorer',hideLeft:false,hideRight:false,leftWidth:400,rightWidth:272});
});
test('broken legacy storage does not crash template initialization',()=>{
 const methods=['bind','mountPanels','applyLayout','renderTabs','updateInspector'];
 const originals=Object.fromEntries(methods.map(key=>[key,TemplateWorkspace.prototype[key]]));
 const globals={localStorage:globalThis.localStorage,document:globalThis.document,ResizeObserver:globalThis.ResizeObserver};
 try{
  globalThis.localStorage={getItem:key=>key==='sourceloom-workspace-layout/1'?null:'{broken'};
  globalThis.document={querySelector:()=>({})};globalThis.ResizeObserver=class{observe(){}};
  for(const key of methods)TemplateWorkspace.prototype[key]=()=>{};
  const workspace=new TemplateWorkspace({});
  assert.deepEqual(workspace.config,normalizeLayout({}));assert.deepEqual(workspace.tabs,[]);
  assert.deepEqual(normalizeLayout(null),normalizeLayout({}));
 }finally{
  Object.assign(TemplateWorkspace.prototype,originals);
  for(const [key,value] of Object.entries(globals)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}
 }
});
