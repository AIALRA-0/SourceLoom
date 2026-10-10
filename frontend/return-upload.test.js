import test from 'node:test';
import assert from 'node:assert/strict';
import {uploadReturn} from '../sourceloom/static/processor_return_upload.js';
test('a lost write receipt is uncertain and never automatically retried',async()=>{
  let sends=0;
  globalThis.FormData=class {append(){}};
  globalThis.XMLHttpRequest=class {constructor(){this.upload={};}open(){}setRequestHeader(){}send(){sends++;this.status=200;this.responseText='<html>login</html>';this.onload();}};
  await assert.rejects(uploadReturn('/return',{}, {upload_id:'same'},()=>{}),e=>e.uncertain===true);
  assert.equal(sends,1);
});
test('upload distinguishes rejection from successful JSON acknowledgement',async()=>{
  globalThis.XMLHttpRequest=class {constructor(){this.upload={};}open(){}setRequestHeader(){}send(){this.status=409;this.responseText=JSON.stringify({error:'版本已经变化'});this.onload();}};
  await assert.rejects(uploadReturn('/return',{}, {},()=>{}),/版本已经变化/);
  globalThis.XMLHttpRequest=class {constructor(){this.upload={};}open(){}setRequestHeader(){}send(){this.status=200;this.responseText=JSON.stringify({version_id:'saved'});this.onload();}};
  assert.equal((await uploadReturn('/return',{}, {},()=>{})).version_id,'saved');
});
