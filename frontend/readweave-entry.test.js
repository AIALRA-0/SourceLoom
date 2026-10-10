import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const code=fs.readFileSync(new URL('../sourceloom/static/processor_readweave.js',import.meta.url),'utf8').replace(/export /g,'');
const ctx={};vm.createContext(ctx);vm.runInContext(code,ctx);
const saved={versionId:'v1',configured:true};
test('a saved draft can open import even when resource checks require attention',()=>{
  assert.equal(ctx.readweaveEntryState({...saved,mechanical_pass:false}).disabled,false);
  assert.equal(ctx.readweaveReadiness({status:'not_ready',message:'Missing resource'},{version:'v1',activeVersion:'v1'}).ready,false);
});
test('readonly, busy, missing draft and missing configuration retain explained restrictions',()=>{
  for(const extra of [{readonly:true},{locked:true},{versionId:null},{configured:false}]){
    const state=ctx.readweaveEntryState({...saved,...extra});assert.equal(state.disabled,true);assert.ok(state.reason);
  }
});
test('an existing or uncertain import cannot enable a second submission',()=>{
  for(const status of ['submitted','imported','readback_passed','readback_gaps']){
    assert.equal(ctx.readweaveEntryState({...saved,status}).disabled,true);
    assert.equal(ctx.readweaveReadiness({status},{version:'v1',activeVersion:'v1'}).ready,false);
  }
  assert.equal(ctx.readweaveReadiness({status:'receipt_unavailable'},{version:'v1',activeVersion:'v1'}).ready,false);
});
test('only an explicitly ready current candidate enables confirmation',()=>{
  assert.equal(ctx.readweaveReadiness({status:'not_submitted'},{version:'v1',activeVersion:'v1'}).ready,true);
  assert.equal(ctx.readweaveReadiness({status:'not_submitted'},{version:'old',activeVersion:'v1'}).ready,false);
  assert.equal(ctx.readweaveReadiness({status:'unknown'},{version:'v1',activeVersion:'v1'}).ready,false);
});
