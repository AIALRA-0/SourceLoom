"""Deferred iframe resources remain usable without enabling scripts in preview."""
import json
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def test_visible_images_folded_references_and_stale_document_are_isolated():
    script=r'''
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const source=fs.readFileSync(arg.root+'/sourceloom/static/processor_reader.js','utf8');
    const method=source.slice(source.indexOf('  attachPreviewImages('),source.indexOf('  bindInput('));
    const hydrated=[],observed=new Set();let callback,toggle,disconnects=0;
    const image=(id,url,closed=false)=>({id,closed,attrs:{'data-reader-src':url},getAttribute(k){return this.attrs[k]},removeAttribute(k){delete this.attrs[k]},closest(){return this.closed?{}:null},set src(url){hydrated.push([this.id,url]);}});
    const a=image('near','/api/processor/projects/p1/files/a'),b=image('far','/api/processor/projects/p1/files/b'),fold=image('fold','/api/processor/projects/p1/files/c',true),other=image('other','/api/processor/projects/p2/files/d'),external=image('external','https://external.invalid/image');
    const doc={baseURI:'http://localhost/api/processor/projects/p1/preview?version=v1&reader=true',querySelectorAll:()=>[a,b,fold,other,external]};
    const ctx={URL,location:{origin:'http://localhost'},IntersectionObserver:class{constructor(fn,opts){callback=fn;if(opts.root!==doc)throw Error('observer must use iframe document')}observe(img){observed.add(img)}unobserve(img){observed.delete(img)}disconnect(){disconnects++}}};
    vm.createContext(ctx);vm.runInContext(`class Reader{${method}};globalThis.Reader=Reader;`,ctx);
    let currentDoc=doc;const reader=new ctx.Reader();Object.assign(reader,{active:true,token:2,doc,attachedIdentity:'p1-v1',identity:()=> 'p1-v1',deps:{preview:()=>currentDoc,context:()=>({projectId:'p1'})}});
    reader.attachPreviewImages(doc,2,(target,event,fn)=>toggle=fn);
    if(observed.has(fold))throw Error('folded page eagerly observed');
    callback([{target:a,isIntersecting:true},{target:b,isIntersecting:false},{target:other,isIntersecting:true},{target:external,isIntersecting:true}]);
    if(hydrated.length!==1||hydrated[0][0]!=='near')throw Error('offscreen or unauthorized image fetched');
    fold.closed=false;toggle({target:{open:true,querySelectorAll:()=>[fold]}});if(!observed.has(fold))throw Error('expanded reference missing');callback([{target:fold,isIntersecting:true}]);if(hydrated.length!==2)throw Error('expanded reference unusable');
    currentDoc={};callback([{target:b,isIntersecting:true}]);if(hydrated.length!==2)throw Error('late observer polluted another document');
    currentDoc=doc;reader.token=3;callback([{target:b,isIntersecting:true}]);if(hydrated.length!==2)throw Error('late observer ignored token');
    process.stdout.write('true');
    '''
    result=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT)}),text=True,encoding='utf-8',capture_output=True,check=True,timeout=10)
    assert result.stdout=='true'


def test_image_load_without_reflow_does_not_cancel_user_scroll_or_restore_old_bookmark():
    script=r'''
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const source=fs.readFileSync(arg.root+'/sourceloom/static/processor_reader.js','utf8');
    const method=source.slice(source.indexOf('  geometryChanged()'),source.indexOf('  inspect()'));
    let scheduled=0,cancelled=0,timer,restored,refreshed=0,persisted=0;
    const ctx={clearTimeout(){},cancelAnimationFrame(){cancelled++},setTimeout(fn){scheduled++;timer=fn;return 1}};
    vm.createContext(ctx);vm.runInContext(`class Reader{${method}};globalThis.Reader=Reader;`,ctx);
    const r=new ctx.Reader(),old={right:{id:'first',offset:0},left:{page:1}},current={right:{id:'last',offset:1},left:{page:1}};
    let size='same';Object.assign(r,{active:true,token:1,geometrySize:'same',positions:old,scrollFrame:8,sizeSignature:()=>size,
      capture:()=>current,refreshGeometry:()=>{refreshed++;r.geometrySize=size},restore:s=>restored=s,persist:()=>persisted++});
    r.geometryChanged();r.geometryChanged();
    if(scheduled||cancelled||restored||r.layoutPending)throw Error('no-reflow image completion interrupted native scroll');
    r.positions=current;size='resized';r.geometryChanged();
    if(scheduled!==1||!r.layoutPending)throw Error('real reflow was not scheduled');
    timer();if(restored!==current||refreshed!==1||persisted!==1||r.layoutPending)throw Error('real reflow did not retain logical position');
    process.stdout.write('true');
    '''
    result=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT)}),text=True,encoding='utf-8',capture_output=True,check=True,timeout=10)
    assert result.stdout=='true'
