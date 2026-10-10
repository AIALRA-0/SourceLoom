import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const code=fs.readFileSync(new URL('../sourceloom/static/processor_import.js',import.meta.url),'utf8').replace(/export /g,'');
const ctx={};vm.createContext(ctx);vm.runInContext(code,ctx);
test('preparation labels describe the current stage without inventing whole-task percentages',()=>{
  assert.equal(ctx.preparationLabel({phase:'pdf_pages',completed:14,total:30}),'准备原件页面 · 14/30 页');
  assert.equal(ctx.preparationLabel({phase:'queued'}),'原件已接收，等待准备');
  assert.equal(ctx.preparationLabel({phase:'failed'}),'材料准备未完成');
});
test('directory chooser reads every page and rejects a truncated directory listing',async()=>{
  let calls=0;
  const rows=await ctx.directoryOptions(async path=>{calls++;return calls===1?{nodes:[{id:'parent',kind:'folder'}],more:true}:{nodes:[{id:'child',kind:'folder',path:'parent'}],more:false};});
  assert.equal(rows.length,2);assert.equal(calls,2);
  await assert.rejects(()=>ctx.directoryOptions(async()=>({nodes:[],more:true})),/未完整返回/);
});
test('a successful HTTP response without a JSON receipt never means upload acceptance',async()=>{
  class XHR{constructor(){this.upload={};}open(){}setRequestHeader(){}send(){this.status=200;this.responseText='<html>login</html>';this.onload();}}
  ctx.XMLHttpRequest=XHR;
  await assert.rejects(()=>ctx.uploadFiles('/upload',{},()=>{}),/未取得有效回执/);
});
