"""Real bounded GET lifecycle, including response bodies that never settle."""
import json
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def test_preview_blob_policy_keeps_scripts_and_auth_restricted(tmp_path):
    from fastapi.testclient import TestClient
    from sourceloom.app import create_app
    with TestClient(create_app(dict(data_dir=str(tmp_path),provider='manual',auth_mode='local',external_worker=True))) as client:
        response=client.get('/')
        assert response.status_code==200
        directives=dict(part.strip().split(' ',1) for part in response.headers['content-security-policy'].split(';'))
        assert directives['img-src']=="'self' data: blob:"
        assert directives['script-src']=="'self'"
        assert directives['object-src']=="'none'"
        assert directives['connect-src']=="'self'"
    with TestClient(create_app(dict(data_dir=str(tmp_path/'proxy'),provider='manual',auth_mode='proxy',allowed_subject='test-user',external_worker=True))) as client:
        assert client.get('/').status_code==401

def test_read_deadline_auth_body_types_and_cancel():
    script=r'''
    const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
    const a=JSON.parse(fs.readFileSync(0,'utf8'));
    const source=fs.readFileSync(a.root+'/sourceloom/static/processor_read.js','utf8').replaceAll('export const','const');
    let call=()=>Promise.resolve(new Response('{"ok":true}',{headers:{'Content-Type':'application/json'}})),count=0;
    const ctx={AbortController,Error,Promise,setTimeout,clearTimeout,fetch:(...args)=>{count++;return call(...args);}};
    vm.createContext(ctx);vm.runInContext(source+'\nglobalThis.json=readJSON;globalThis.blob=readBlob;',ctx);
    const rejects=(task,code)=>assert.rejects(task,e=>e.code===code);
    (async()=>{
      assert.equal((await ctx.json('/material')).ok,true);
      call=()=>Promise.resolve(new Response('<html>login</html>',{status:401,headers:{'Content-Type':'text/html'}}));
      await rejects(ctx.json('/material'),'authentication');
      call=()=>Promise.resolve({status:200,ok:true,url:'https://site/_aialra_auth/sign-in',headers:new Headers({'content-type':'text/html'})});
      await rejects(ctx.blob('/source'),'authentication');
      call=()=>Promise.resolve(new Response('<html>error</html>',{headers:{'Content-Type':'text/html'}}));
      await rejects(ctx.json('/material'),'protocol');await rejects(ctx.blob('/source'),'protocol');
      call=()=>Promise.resolve(new Response('{broken',{headers:{'Content-Type':'application/json'}}));
      await rejects(ctx.json('/material'),'protocol');
      call=()=>Promise.resolve(new Response('',{status:404}));await rejects(ctx.json('/material'),'not_found');
      call=()=>Promise.reject(Error('offline'));await rejects(ctx.json('/material'),'network');
      let aborted=false;call=(_,o)=>{o.signal.addEventListener('abort',()=>aborted=true);return new Promise(()=>{});};
      await rejects(ctx.json('/material',{timeoutMs:10}),'timeout');assert.equal(aborted,true);
      call=()=>Promise.resolve({status:200,ok:true,url:'/material',headers:new Headers({'content-type':'application/json'}),json:()=>new Promise(()=>{})});
      await rejects(ctx.json('/material',{timeoutMs:10}),'timeout');
      call=()=>Promise.resolve({status:200,ok:true,url:'/source',headers:new Headers({'content-type':'image/png'}),blob:()=>new Promise(()=>{})});
      await rejects(ctx.blob('/source',{timeoutMs:10}),'timeout');
      call=()=>new Promise(()=>{});const cancel=new AbortController();const pending=ctx.json('/material',{signal:cancel.signal});cancel.abort();await rejects(pending,'aborted');
      const before=count;await rejects(ctx.json('/write',{method:'POST'}),'protocol');assert.equal(count,before);
      call=()=>Promise.resolve(new Response('png',{headers:{'Content-Type':'image/png'}}));assert.equal((await ctx.blob('/source')).size,3);
      call=()=>Promise.resolve(new Response('',{headers:{'Content-Type':'image/png'}}));await rejects(ctx.blob('/source'),'protocol');
      console.log('ok');
    })().catch(e=>{console.error(e);process.exit(1);});
    '''
    result=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT)}),text=True,capture_output=True,timeout=10,check=True)
    assert result.stdout.strip()=='ok'
