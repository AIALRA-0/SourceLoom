"""Execute actual browser lifecycle methods to prevent metadata-only document rebuilds."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_hidden_resource_gallery_is_deferred_and_reopens_with_current_material():
    script = r'''
    const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
    const arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const src=fs.readFileSync(arg.root+'/sourceloom/static/processor.js','utf8');
    const show=src.slice(src.indexOf('function showTab('),src.indexOf('let packRead='));
    const draw=src.slice(src.indexOf('function drawResources('),src.indexOf('async function saveResourceUsages('));
    let createdImages=0,filtered=0;
    function node(tag='div',text){if(tag==='img')createdImages++;return {tag,text,children:[],hidden:false,dataset:{},
      classList:{toggle(){}},replaceChildren(...children){this.children=children;},
      append(...children){this.children.push(...children);},setAttribute(){},addEventListener(){}};}
    const nodes=new Map(),get=s=>{if(!nodes.has(s))nodes.set(s,node());return nodes.get(s);};
    get('#resource-view').value='grid';
    const ctx={project:{id:'p1'},currentTab:'result',resourceLimit:2,selectedResourceIds:new Set(),
      $:get,element:node,workbench:{tab(){}},ensurePack(){},renderPageInto(){},protect:f=>f,
      resourceItems:()=>[1,2,3].map(n=>({id:ctx.project.id+'-r'+n,kind:'image',sha256:'sha'+n,url:ctx.project.id+'/asset'+n})),
      filteredResources:()=>{filtered++;return ctx.resourceItems();},resourceUsage:()=> 'body',resourcePage:()=>0,
      safeURL:v=>v,pidPath:s=>ctx.project.id+s,showImage(){},copyText(){},operation(){},operationDone(){},saveResourceUsages(){}};
    vm.createContext(ctx);vm.runInContext(show+'\n'+draw,ctx);
    ctx.showTab('result');ctx.drawResources();
    assert.equal(get('#resource-list').children.length,0);assert.equal(createdImages,0);assert.equal(filtered,0);
    ctx.showTab('material');
    assert.equal(createdImages,2);assert.deepEqual(get('#resource-list').children.map(n=>n.dataset.resourceId),['p1-r1','p1-r2']);
    assert.equal(get('#resource-more').hidden,false);
    ctx.showTab('result');assert.equal(get('#resource-list').children.length,0);
    ctx.project={id:'p2'};ctx.drawResources();
    assert.equal(createdImages,2);assert.equal(filtered,1);
    ctx.showTab('material');
    assert.deepEqual(get('#resource-list').children.map(n=>n.dataset.resourceId),['p2-r1','p2-r2']);
    assert.equal(createdImages,4);
    ctx.resourceLimit=3;ctx.drawResources();assert.equal(get('#resource-list').children.length,3);
    assert.equal(get('#resource-more').hidden,true);
    process.stdout.write('true');
    '''
    result = subprocess.run([shutil.which('node'), '-e', script], input=json.dumps({'root': str(ROOT)}),
                            text=True, capture_output=True, check=True, timeout=10)
    assert result.stdout == 'true'


def test_opening_saved_material_marks_current_without_refreshing_catalog():
    script = r'''
    const fs=require('node:fs'),vm=require('node:vm');
    const arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const source=fs.readFileSync(arg.root+'/sourceloom/static/processor.js','utf8');
    const render=source.slice(source.indexOf('function render({'),source.indexOf('function sourceMapEntries('));
    const refresh=source.slice(source.indexOf('async function refreshProjects('),source.indexOf('\n',source.indexOf('async function refreshProjects(')));
    const count={mark:0,catalog:0,source:0};
    const ctx={project:{id:'p1',title:'Saved article',library:{}},pack:null,projects:[],base:'/api/processor',
      $:()=>({classList:{toggle(){}}}),processor:()=>({}),items:v=>v||[],versions:()=>[],requests:()=>[],
      documentStatus:()=>'',isUnknown:()=>false,library:{tree:{syncCurrent(){count.mark++;}}},
      drawList(){count.catalog++;},drawSource(){count.source++;},drawPack(){},drawVersions(){},
      drawRequests(){},drawChecks(){},drawChannels(){},api:async()=>[{id:'new-material'}]};
    vm.createContext(ctx);vm.runInContext(render+'\n'+refresh,ctx);
    (async()=>{
      ctx.render();ctx.render({source:false});
      if(count.mark!==2||count.catalog!==0||count.source!==1)throw Error('Opening reloaded material catalog');
      await ctx.refreshProjects();
      if(count.catalog!==1||ctx.projects[0].id!=='new-material')throw Error('Explicit catalog refresh stopped working');
      process.stdout.write('true');
    })().catch(e=>{console.error(e);process.exit(1)});
    '''
    result=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT)}),
                          text=True,capture_output=True,check=True,timeout=10)
    assert result.stdout=='true'


def run_case(case):
    script = r'''
    const fs=require('node:fs'),vm=require('node:vm');
    const arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const source=fs.readFileSync(arg.root+'/sourceloom/static/processor.js','utf8');
    const fn=source.slice(source.indexOf('async function loadVersion('),source.indexOf('async function refreshProjects('));
    const counts={detach:0,prepare:0,src:0,attach:0};
    const version={id:'v1',digest:'digest1',markdown:'real source-supported prose',source_map:[]};
    const frame={hidden:false,set src(v){counts.src++;this.url=v;}};
    let pending;
    const ctx={version,counts,console,openingProjectId:null,issueApplying:false,issueDrawer:{close(){}},
      openEpoch:1,versionEpoch:0,project:{id:'p1'},processor:()=>({source_digest:'source1'}),
      selectedVersion:'v1',loadedVersion:{...version},previewKey:JSON.stringify(['p1','source1','v1','digest1',version.markdown]),
      sourceMap:null,reader:{detach(o){counts.detach++;counts.preserveSource=o.preserveSource;},prepare(){counts.prepare++;},
      capture:()=>({left:'page3',right:'paragraph12'}),refreshGeometry(){},restore(){},attachPreview(){counts.attach++;}},
      api:async()=> arg.case==='stale'?new Promise(r=>pending=r):({...ctx.version}),
      pidPath:s=>s,$:s=>s==='#result-preview'?frame:{hidden:false},setMarkdown(){},drawChecks(){},renderAvailability(){},drawVersions(){},
      workbench:{refresh(){}},renderReadweaveStatus(){},loadReadweaveStatus:async()=>{if(arg.case==='delivery-pending')return new Promise(()=>{});}};
    if(arg.case==='new-version')ctx.version={...version,id:'v2'};
    if(arg.case==='changed-body')ctx.version={...version,digest:'digest2',markdown:'new prose'};
    if(arg.case==='same-version-checks')ctx.version={...version,representations:[{id:'confirmation'}]};
    vm.createContext(ctx);vm.runInContext(fn,ctx);
    (async()=>{
      if(arg.case==='stale'){const p=ctx.loadVersion('v1');ctx.openEpoch=2;pending({...version});await p;}
      else await ctx.loadVersion(ctx.version.id);
      process.stdout.write(JSON.stringify({counts,selected:ctx.selectedVersion,hidden:frame.hidden}));
    })().catch(e=>{console.error(e);process.exit(1);});
    '''
    result = subprocess.run([shutil.which('node'), '-e', script], input=json.dumps({'root':str(ROOT),'case':case}), text=True,capture_output=True,check=True,timeout=10)
    return json.loads(result.stdout)


@pytest.mark.parametrize('case',['same-version','same-version-checks'])
def test_same_visible_version_preserves_preview_and_pdf(case):
    assert run_case(case)['counts'] == dict(detach=0,prepare=0,src=0,attach=1)


@pytest.mark.parametrize('case',['new-version','changed-body'])
def test_true_content_change_refreshes_preview_but_reuses_source(case):
    counts=run_case(case)['counts']
    assert counts['detach']==counts['prepare']==counts['src']==1
    assert counts['preserveSource'] is True


def test_late_version_response_does_not_mutate_new_material():
    assert run_case('stale')['counts']==dict(detach=0,prepare=0,src=0,attach=0)


def test_pending_delivery_status_does_not_block_a_ready_version():
    assert run_case('delivery-pending')['counts']==dict(detach=0,prepare=0,src=0,attach=1)


def test_old_delivery_status_cannot_label_a_new_version_as_imported():
    script=r'''
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const s=fs.readFileSync(arg.root+'/sourceloom/static/processor.js','utf8');
    const code=s.slice(s.indexOf('async function loadReadweaveStatus('),s.indexOf('function drawChecks('));
    let pending;const applied=[];const ctx={project:{id:'p1'},openEpoch:1,selectedVersion:'old',api:()=>new Promise(r=>pending=r),pidPath:s=>s,renderReadweaveStatus:r=>applied.push(r),$:()=>({})};
    vm.createContext(ctx);vm.runInContext(code,ctx);
    (async()=>{const task=ctx.loadReadweaveStatus();ctx.selectedVersion='new';pending({status:'readback_passed',version_id:'old'});await task;if(applied.length)throw Error('old version receipt polluted new candidate');process.stdout.write('true')})().catch(e=>{console.error(e);process.exit(1)});
    '''
    r=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT)}),text=True,capture_output=True,check=True,timeout=10)
    assert r.stdout=='true'


def run_pdf_lease_case(case):
    script=r'''
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const s=fs.readFileSync(arg.root+'/sourceloom/static/processor_pdf.js','utf8');
    const code=s.slice(s.indexOf('const documents=new Map();'),s.indexOf('// A display adapter'));
    let clock=0,timerId=0,rejectNext;
    const timers=new Map(),tasks=[],unhandled=[],count={open:0,destroy:0};
    process.on('unhandledRejection',reason=>unhandled.push(reason));
    const ctx={Map,Date:{now:()=>clock},setTimeout(fn,ms){const id=++timerId;timers.set(id,{at:clock+ms,fn});return id;},clearTimeout(id){timers.delete(id);},
      pdfjs:{getDocument(options){
        count.open++;const task={options,destroyed:0,onProgress:()=>{},promise:options.fail?new Promise((_,reject)=>rejectNext=reject):Promise.resolve({}),
          destroy(){this.destroyed++;count.destroy++;return Promise.resolve();}};
        tasks.push(task);return task;
      }}};
    vm.createContext(ctx);vm.runInContext(code,ctx);
    const cache=vm.runInContext('documents',ctx),acquire=(key,options={})=>ctx.acquireDocument(key,options);
    const assert=(condition,problem)=>{if(!condition)throw Error(problem);};
    function advance(ms){const end=clock+ms;while(true){const due=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];if(!due)break;
      timers.delete(due[0]);clock=due[1].at;due[1].fn();}clock=end;}
    (async()=>{
      if(arg.case==='share'){
        const a=acquire('material/source1'),b=acquire('material/source1');
        assert(a.task===b.task&&count.open===1,'same original downloaded twice');
        a.release();assert(!count.destroy,'visible sibling document was destroyed');
        b.release();assert(!count.destroy&&a.task.onProgress===null,'idle document was destroyed or retained a view callback');
        advance(30_000);assert(count.destroy===1&&!cache.size,'idle source was not released');
        const c=acquire('material/source2');assert(count.open===2,'new source reused old PDF');c.release();advance(30_000);
      }else if(arg.case==='return'){
        const a=acquire('A');a.release();advance(1_000);const b=acquire('B');b.release();advance(1_000);
        const again=acquire('A');assert(again.task===a.task&&count.open===2&&!count.destroy,'A/B/A parsed original again');again.release();
        advance(30_000);assert(count.destroy===2&&!cache.size,'retained originals did not expire');
      }else if(arg.case==='identity'){
        const a=acquire('/material1/file:sha1'),b=acquire('/material1/file:sha2'),c=acquire('/material2/file:sha1');
        assert(count.open===3&&a.task!==b.task&&a.task!==c.task,'source digest or material URL ignored');
        a.release();b.release();c.release();advance(30_000);assert(count.destroy===3,'different sources not released');
      }else if(arg.case==='lru'){
        const a=acquire('A');a.release();advance(1);const b=acquire('B');b.release();advance(1);
        const again=acquire('A');again.release();const c=acquire('C');
        assert(b.task.destroyed===1&&!a.task.destroyed&&!c.task.destroyed&&cache.size===2&&!cache.has('B'),'oldest idle document not evicted');
        c.release();advance(30_000);assert(count.destroy===3,'LRU documents leaked');
      }else if(arg.case==='ttl'){
        const a=acquire('A');a.release();advance(29_999);assert(!count.destroy,'idle TTL ended early');advance(1);
        assert(a.task.destroyed===1&&!cache.size,'idle TTL did not end');const again=acquire('A');assert(again.task!==a.task&&count.open===2,'expired PDF was reused');again.release();
      }else if(arg.case==='clamped-timer'){
        const a=acquire('A');a.release();clock=30_001;const again=acquire('A');
        assert(a.task.destroyed===1&&again.task!==a.task&&count.open===2,'background-clamped expiration was ignored');again.release();
      }else if(arg.case==='failure'){
        const a=acquire('A',{fail:true}),b=acquire('A');rejectNext(Error('PDF decode failed'));await new Promise(setImmediate);
        assert(!cache.has('A')&&!a.task.destroyed,'failed active PDF was cached or destroyed early');const next=acquire('A');
        assert(next.task!==a.task&&count.open===2,'failed PDF was reused');a.release();assert(!a.task.destroyed,'failed shared PDF destroyed with active sibling');
        b.release();assert(a.task.destroyed===1&&cache.get('A').task===next.task&&!next.task.destroyed,'old failed lease changed replacement identity');
        next.release();advance(30_000);assert(count.destroy===2,'failed task or replacement leaked');await new Promise(setImmediate);assert(!unhandled.length,'unhandled task rejection');
      }else if(arg.case==='idle-failure'){
        const a=acquire('A',{fail:true});a.release();rejectNext(Error('PDF decode failed'));await new Promise(setImmediate);
        assert(a.task.destroyed===1&&!cache.size&&!timers.size&&!unhandled.length,'failed idle PDF or rejection retained');
      }else if(arg.case==='active'){
        const a=acquire('A'),b=acquire('B'),c=acquire('C');advance(60_000);
        assert(cache.size===3&&!count.destroy,'active leases evicted by limit or TTL');b.release();
        assert(b.task.destroyed===1&&cache.size===2&&!a.task.destroyed&&!c.task.destroyed,'capacity release destroyed active document');
        b.release();assert(count.destroy===1,'duplicate release destroyed twice');a.release();c.release();advance(30_000);assert(count.destroy===3,'active leases leaked after release');
      }else if(arg.case==='repeat-release'){
        const a=acquire('A'),b=acquire('A');a.release();a.release();advance(60_000);
        assert(!count.destroy&&cache.size===1,'duplicate release lost active sibling');b.release();b.release();
        assert(!count.destroy&&timers.size===1,'duplicate last release destroyed early or duplicated timer');advance(30_000);assert(count.destroy===1,'source destroyed more than once');
      }else if(arg.case==='late-timer'){
        const a=acquire('A');a.release();const oldCallback=[...timers.values()][0].fn;advance(1);const again=acquire('A');
        oldCallback();assert(!count.destroy&&again.task===a.task,'already queued expiration destroyed active reuse');again.release();oldCallback();
        assert(!count.destroy&&timers.size===1,'old callback destroyed renewed idle lease');advance(30_000);assert(count.destroy===1,'renewed expiration not applied');
      }else throw Error('unknown test');
      process.stdout.write(JSON.stringify(count));
    })().catch(e=>{console.error(e);process.exit(1)});
    '''
    r=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT),'case':case}),text=True,capture_output=True,check=True,timeout=10)
    return json.loads(r.stdout)


def test_same_original_pdf_is_parsed_once_and_released_after_both_views():
    assert run_pdf_lease_case('share')==dict(open=2,destroy=2)


@pytest.mark.parametrize('case',['return','identity','lru','ttl','clamped-timer','failure','idle-failure','active','repeat-release','late-timer'])
def test_pdf_documents_reuse_only_valid_identities_and_have_bounded_idle_lifetimes(case):
    run_pdf_lease_case(case)


def test_user_takeover_does_not_restore_a_stale_layout_bookmark():
    script=r'''
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const s=fs.readFileSync(arg.root+'/sourceloom/static/processor_reader.js','utf8');
    const method=s.slice(s.indexOf('  interact(side)'),s.indexOf('  scroller(side)'));
    const ctx={clearTimeout(){},cancelAnimationFrame(){},performance:{now:()=>1}};vm.createContext(ctx);
    vm.runInContext(`class Reader{${method}};this.Reader=Reader;`,ctx);
    const reader=new ctx.Reader();Object.assign(reader,{active:true,layoutPending:true,layoutBookmark:{old:'stale'},program:{right:9},trace:[],refreshGeometry(){},capture:()=>({right:'current'})});
    reader.interact('right');if(reader.layoutPending||reader.layoutBookmark||reader.positions.right!=='current'||reader.driver!=='right')throw Error('real input restored stale geometry');
    process.stdout.write('true');
    '''
    r=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT)}),text=True,capture_output=True,check=True,timeout=10)
    assert r.stdout=='true'


def test_layout_scroll_does_not_overwrite_the_pre_reflow_reading_anchor():
    script=r'''
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const s=fs.readFileSync(arg.root+'/sourceloom/static/processor_reader.js','utf8');
    const method=s.slice(s.indexOf('  scrolled(side)'),s.indexOf('  visible(side)'));
    const ctx={};vm.createContext(ctx);vm.runInContext(`class Reader{${method}};this.Reader=Reader;`,ctx);
    const reader=new ctx.Reader(),saved={right:{id:'middle',offset:.4}};
    Object.assign(reader,{active:true,layoutPending:true,layoutBookmark:saved,positions:saved,driver:'right',program:{},capture(){throw Error('layout scroll was mistaken for user input');}});
    reader.scrolled('right');if(reader.layoutBookmark!==saved||reader.positions!==saved)throw Error('pre-reflow bookmark replaced');
    process.stdout.write('true');
    '''
    r=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT)}),text=True,capture_output=True,check=True,timeout=10)
    assert r.stdout=='true'


@pytest.mark.parametrize('event',['save-result','markdown-file'])
def test_late_save_or_import_cannot_replace_a_different_material(event):
    script=r'''
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const s=fs.readFileSync(arg.root+'/sourceloom/static/processor.js','utf8');
    const start=s.indexOf(`$('#${arg.event}').addEventListener(`);
    const end=s.indexOf("\n$('#",start+1),code=s.slice(start,end);
    const handlers={},writes=[],values={},stored=new Map([['sourceloom-processor-edit:old','old draft']]);let pending;
    const ctx={openEpoch:1,project:{id:'old'},base:'/api/processor',selectedVersion:'v1',dirty:true,
      $:id=>values[id]??={value:'old draft',addEventListener:(_,f)=>handlers[id]=f},protect:f=>f,
      setLocked(){},operation(){},setMarkdown(){writes.push('markdown')},notice(){},operationDone(){writes.push('status')},render(){writes.push('render')},processor:()=>({}),versions:()=>[],loadVersion:async()=>{},refreshProjects:async()=>{},
      post:()=>new Promise(r=>pending=r),localStorage:{getItem:k=>stored.get(k),setItem(){writes.push('storage')},removeItem:k=>stored.delete(k)}};
    vm.createContext(ctx);
    const uploadSource=fs.readFileSync(arg.root+'/sourceloom/static/processor_return_upload.js','utf8').replace(/export /g,'');
    vm.runInContext(uploadSource+';this.ReturnUpload=ReturnUpload;',ctx);
    const uploader=Object.create(ctx.ReturnUpload.prototype);uploader.states=new Map();uploader.render=()=>{};uploader.showError=()=>{throw Error('unexpected input error')};
    ctx.crypto={randomUUID:()=> 'late-upload-fixture'};
    uploader.deps={context:()=>({id:ctx.project.id,editable:true,version:'v1',sourceDigest:'source-old'}),allowUpload:()=>true,
      upload:s=>{if(s.id!=='old'||s.version!=='v1')throw Error('upload target changed');return new Promise(r=>pending=r);},saved:()=>writes.push('saved')};
    let uploadPromise;ctx.returnUpload={submit:(...args)=>uploadPromise=uploader.submit(...args)};
    vm.runInContext(code,ctx);
    (async()=>{let promise;
      if(arg.event==='markdown-file'){handlers['#markdown-file']({target:{value:'file',files:[{name:'draft.txt',size:3}]}});promise=uploadPromise;}
      else promise=handlers['#save-result']();
      ctx.openEpoch=2;ctx.project={id:'new'};pending({project:{id:'old'},version_id:'saved-old'});await promise;
      if(ctx.project.id!=='new'||writes.length)throw Error('old asynchronous result polluted new material');
      process.stdout.write('true');})().catch(e=>{console.error(e);process.exit(1)});
    '''
    r=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT),'event':event}),text=True,capture_output=True,check=True,timeout=10)
    assert r.stdout=='true'


def test_reader_detach_and_attach_lifecycle_is_bounded():
    script=r'''
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const source=fs.readFileSync(arg.root+'/sourceloom/static/processor_reader.js','utf8');
    const detach=source.slice(source.indexOf('  detach('),source.indexOf('  prepare('));
    const attach=source.slice(source.indexOf('  attachPreview('),source.indexOf('  bindInput('));
    const counts={destroy:0,cleanup:0,observe:0,images:0},frame={hidden:false};
    const doc={location:{pathname:'/api/processor/projects/p1/preview',href:'http://localhost/api/processor/projects/p1/preview?version=v1'}};
    const ctx={URL,counts,frame,doc,$:()=>frame,cancelAnimationFrame(){},clearTimeout(){}};
    vm.createContext(ctx);vm.runInContext(`class Reader{${detach}${attach}};this.Reader=Reader;`,ctx);
    const reader=new ctx.Reader();Object.assign(reader,{active:true,token:1,persist(){},identity:()=> 'identity1',attachedIdentity:'identity1',doc,
      deps:{preview:()=>doc,context:()=>({projectId:'p1',versionId:'v1'})},pdfViews:new Map([['left',{destroy(){counts.destroy++;}}],['compare',{destroy(){counts.destroy++;}}]]),
      cleanups:[()=>counts.cleanup++],observer:{disconnect(){counts.observe++;}},previewImages:{disconnect(){counts.images++;}}});
    reader.attachPreview();if(counts.cleanup||counts.observe)throw Error('duplicate attachment changed active listeners');
    reader.detach({preserveSource:true});if(counts.destroy||reader.pdfViews.size!==2)throw Error('same-source version update destroyed source');
    reader.detach();if(counts.destroy!==2||reader.pdfViews.size!==0)throw Error('different material failed to release source');
    if(counts.images!==1||reader.previewImages!==null)throw Error('preview observer survived detach');
    process.stdout.write(JSON.stringify(counts));
    '''
    r=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT)}),text=True,capture_output=True,check=True,timeout=10)
    assert json.loads(r.stdout)['destroy']==2


@pytest.mark.parametrize('change,expected',[
    ('request-status',False),('issue-count',False),('version-metadata',False),
    ('source-digest',True),('source-resources',True),('source-inventory',True),('readonly',True),
])
def test_only_source_presentation_changes_rebuild_source_controls(change,expected):
    script=r"""
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const src=fs.readFileSync(arg.root+'/sourceloom/static/processor.js','utf8');
    const fn=src.slice(src.indexOf('function sourcePresentationKey('),src.indexOf('function render('));
    const ctx={};vm.createContext(ctx);vm.runInContext(fn,ctx);
    const old={id:'p1',library:{readonly:false},inventory:{originals:[{sha256:'source-sha'}]},processor:{source_digest:'source1',resources:[{id:'r1',usage:'body'}],requests:[{status:'running'}],versions:[{id:'v1'}]}};
    const next=JSON.parse(JSON.stringify(old));
    if(arg.change==='request-status')next.processor.requests[0].status='completed';
    if(arg.change==='issue-count')next.processor.checks={issues:[{message:'needs confirmation'}]};
    if(arg.change==='version-metadata')next.processor.versions[0].representations=[{id:'confirmation'}];
    if(arg.change==='source-digest')next.processor.source_digest='source2';
    if(arg.change==='source-resources')next.processor.resources[0].usage='reference';
    if(arg.change==='source-inventory')next.inventory.originals[0].sha256='new-source-sha';
    if(arg.change==='readonly')next.library.readonly=true;
    process.stdout.write(JSON.stringify(ctx.sourcePresentationKey(old)!==ctx.sourcePresentationKey(next)));
    """
    r=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT),'change':change}),text=True,capture_output=True,check=True,timeout=10)
    assert json.loads(r.stdout) is expected


@pytest.mark.parametrize('readonly,valid,status,disabled',[
    (False,True,'not_submitted',False),(True,True,'not_submitted',True),
    (False,False,'not_submitted',True),(False,True,'readback_passed',True),
])
def test_readweave_status_uses_current_material_readonly_scope(readonly,valid,status,disabled):
    script=r"""
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const src=fs.readFileSync(arg.root+'/sourceloom/static/processor.js','utf8');
    const fn=src.slice(src.indexOf('function renderReadweaveStatus('),src.indexOf('async function loadReadweaveStatus('));
    const nodes={};const ctx={project:{library:{readonly:arg.readonly}},readweaveReceipt:null,readweaveURL:'',capabilities:{},locked:false,
      isWebURL:v=>v,$:id=>(nodes[id]??={hidden:false,removeAttribute(){}}),activeChecks:()=>({}),activeVersion:()=>({id:'v1',mechanical_pass:arg.valid})};
    vm.createContext(ctx);vm.runInContext(fn,ctx);ctx.renderReadweaveStatus({status:arg.status});
    process.stdout.write(JSON.stringify(nodes['#send-readweave'].disabled));
    """
    r=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT),'readonly':readonly,'valid':valid,'status':status}),text=True,capture_output=True,check=True,timeout=10)
    assert json.loads(r.stdout) is disabled
