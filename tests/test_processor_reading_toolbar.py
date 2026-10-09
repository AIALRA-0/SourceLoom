"""Exercise layout transitions without rereading or replacing the PDF document."""
import json
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def test_showing_same_width_source_draws_visible_pages_and_toolbar_tracks_split():
    script=r'''
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const source=fs.readFileSync(arg.root+'/sourceloom/static/processor_reader.js','utf8').replace(/^import[^\n]*\n/gm,'').replace('export class LinkedReader','class LinkedReader');
    const styles={},nodes={};const node=id=>nodes[id]||(nodes[id]={value:'source',clientWidth:400,clientHeight:120,classList:{contains:()=>false},style:{setProperty(k,v){styles[id+':'+k]=v;}},setAttribute(k,v){this[k]=v;}});
    const ctx={document:{getElementById:node,body:{dataset:{}}},clamp:(n,a,b)=>Math.max(a,Math.min(b,n)),console};vm.createContext(ctx);vm.runInContext(source+'\nglobalThis.Reader=LinkedReader;',ctx);
    const r=Object.create(ctx.Reader.prototype),calls={geometry:0,restore:0,relayout:0,draw:0};
    Object.assign(r,{active:true,positions:{left:{page:12},right:{block:'b3'}},refreshGeometry(){calls.geometry++;},restore(){calls.restore++;},capture(){return this.positions;},persist(){},visible:()=>true,pdfView:()=>({relayout(){calls.relayout++;},renderVisible(){calls.draw++;}})});
    r.layoutChange(()=>{});r.setShare(61);
    if(calls.relayout!==1||calls.draw!==1||calls.restore!==1)throw Error('shown equal-width pane was not redrawn');
    if(styles['reading-controls:--left-share']!=='61%'||styles['result-layout:--left-share']!=='61%')throw Error('pane and toolbar splits diverged');
    r.visible=()=>false;r.layoutChange(()=>{});if(calls.draw!==1)throw Error('hidden pane should not allocate canvases');
    process.stdout.write('true');
    '''
    result=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT)}),text=True,encoding='utf-8',capture_output=True,check=True,timeout=10)
    assert result.stdout=='true'



def test_nested_reading_toolbar_keyboard_moves_once_and_keeps_its_own_group():
    script=r"""
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const source=fs.readFileSync(arg.root+'/sourceloom/static/processor_workbench.js','utf8');
    const binding=source.split('\n').find(line=>line.includes("for(const toolbar of document.querySelectorAll('.pane-toolbar,.reading-toolbar'))"));
    const calls=[];const toolbar=kind=>({kind,classList:{contains:name=>name===kind},setAttribute(){},addEventListener(name,fn){this.key=fn;},querySelectorAll(){return this.buttons;}});
    const outer=toolbar('draft-toolbar'),shared=toolbar('reading-toolbar');
    const button=(name,owner,disabled=false)=>({name,disabled,tagName:'BUTTON',getClientRects:()=>[{}],closest:selector=>selector==='[role=toolbar]'?owner:null,focus(){calls.push(name);}});
    const free=button('free',shared),link=button('link',shared),find=button('find',outer),disabled=button('disabled',outer,true),aa=button('aa',outer);
    shared.buttons=[free,link];outer.buttons=[free,link,find,disabled,aa];
    const ctx={document:{querySelectorAll:()=>[outer,shared]}};vm.createContext(ctx);vm.runInContext(binding,ctx);
    const event={key:'ArrowRight',target:free,preventDefault(){}};shared.key(event);outer.key(event);
    if(JSON.stringify(calls)!=='["link"]')throw Error('nested toolbar handled one key twice');
    outer.key({key:'ArrowRight',target:find,preventDefault(){}});
    if(JSON.stringify(calls)!=='["link","aa"]')throw Error('disabled or other toolbar controls entered focus sequence');
    shared.key({key:'ArrowLeft',target:free,preventDefault(){}});
    if(calls.at(-1)!=='link')throw Error('shared toolbar wrap no longer works');
    process.stdout.write('true');
    """
    result=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT)}),text=True,encoding='utf-8',capture_output=True,check=True,timeout=10)
    assert result.stdout=='true'



def test_narrow_source_tools_reuse_nodes_and_restore_order_without_reader_work():
    script=r"""
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const source=fs.readFileSync(arg.root+'/sourceloom/static/processor_workbench.js','utf8');
    const fit=source.slice(source.indexOf('    const sourceFind=this.controls[0][1]'),source.indexOf("    const thumb=el('dialog','thumbnail-dialog')"));
    const node=(name,width)=>({name,parentElement:null,getBoundingClientRect:()=>({width})});
    function holder(name){return {name,children:[],clientWidth:450,insertBefore(n,target){if(n.parentElement){n.parentElement.children.splice(n.parentElement.children.indexOf(n),1);}this.children.splice(target?this.children.indexOf(target):this.children.length,0,n);n.parentElement=this;},append(...nodes){for(const n of nodes)this.insertBefore(n,null);},prepend(...nodes){for(const n of nodes.reverse()){if(n.parentElement)n.parentElement.children.splice(n.parentElement.children.indexOf(n),1);this.children.unshift(n);n.parentElement=this;}}};}
    const left=holder('left'),panel=holder('panel'),originalTools={p:panel,d:node('menu',36)},group=node('view',62),find=node('find',36),pages=node('pages',140),zoom=node('zoom',154);
    left.children=[group,find,pages,zoom,originalTools.d];for(const n of left.children)n.parentElement=left;
    const compact=[],draft=holder('draft');draft.clientWidth=500;draft.classList={toggle(name,on){compact.push(on);}};const toolbar=holder('toolbar'),shared=node('shared',70);draft.append(shared);let narrow=false;const layout={classList:{contains:()=>false}};
    const workbench={controls:[[group,find,pages,zoom]]};let callback;
    const ctx={left,originalTools,layout,q:s=>s==='#reading-controls'?toolbar:s==='#shared-reading-tools'?shared:draft,getComputedStyle:()=>({display:narrow?'flex':'grid'}),ResizeObserver:class{constructor(fn){callback=fn;}observe(){}},workbench};vm.createContext(ctx);vm.runInContext('(function(){'+fit+'}).call(workbench)',ctx);
    left.clientWidth=500;callback();if(find.parentElement!==left||zoom.parentElement!==left)throw Error('ample source space should keep main tools');
    left.clientWidth=280;draft.clientWidth=259;callback();if(find.parentElement!==panel||zoom.parentElement!==panel||pages.parentElement!==left)throw Error('narrow source lost paging or failed to move existing secondary controls');
    callback();if(panel.children.length!==2)throw Error('observer added duplicate tools');
    left.clientWidth=500;draft.clientWidth=500;callback();if(left.children.map(n=>n.name).join(',')!=='view,find,pages,zoom,menu'||panel.children.length)throw Error('widening failed to restore the same control nodes');
    if(!compact.includes(true)||compact.at(-1)!==false)throw Error('narrow draft compact state did not restore');
    narrow=true;toolbar.clientWidth=400;callback();
    if(shared.parentElement!==toolbar||group.parentElement!==panel||pages.parentElement!==panel)throw Error('phone row failed to move original controls into reachable menu');
    const count=panel.children.length;callback();if(panel.children.length!==count)throw Error('narrow observer duplicated controls');
    toolbar.clientWidth=700;callback();if(pages.parentElement!==left)throw Error('roomier single-pane toolbar did not recover paging');
    narrow=false;left.clientWidth=500;callback();if(shared.parentElement!==draft||left.children.map(n=>n.name).join(',')!=='view,find,pages,zoom,menu'||panel.children.length)throw Error('wide split did not restore original nodes');
    narrow=true;toolbar.clientWidth=400;
    vm.runInContext('(function(){'+fit+'}).call(workbench)',ctx);callback();
    narrow=false;left.clientWidth=280;callback();
    if(find.parentElement!==panel||zoom.parentElement!==panel)throw Error('phone-first widening failed to measure actual source tool requirements');
    left.clientWidth=500;callback();if(panel.children.length)throw Error('phone-first widening did not restore ample column');
    process.stdout.write('true');
    """
    result=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT)}),text=True,encoding='utf-8',capture_output=True,check=True,timeout=10)
    assert result.stdout=='true'


def test_phone_editor_reveals_its_pane_and_closes_the_invoking_menu():
    script=r"""
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const source=fs.readFileSync(arg.root+'/sourceloom/static/processor.js','utf8');
    const method=source.slice(source.indexOf('function setCompareLeft(kind)'),source.indexOf('function setMobileCompareSide(side)'));
    const nodes=new Map(),get=id=>{if(!nodes.has(id))nodes.set(id,{hidden:false,value:'source',open:true,attributes:{},classList:{add(x){this[x]=true;}},setAttribute(k,v){this.attributes[k]=v;}});return nodes.get(id);};
    let narrow=true,changes=0;const ctx={$:get,document:{body:{classList:{toggle(){}}}},reader:{layoutChange(fn){changes++;fn();}},getComputedStyle:()=>({display:narrow?'flex':'grid'})};
    vm.createContext(ctx);vm.runInContext(method,ctx);
    ctx.setCompareLeft('editor');
    if(get('.editor-pane').hidden||!get('#compare-source').hidden||!get('#result-layout').classList['mobile-left'])throw Error('opening editor left the edited pane invisible');
    if(get('#document-menu').open||get('#mobile-compare-left').attributes['aria-selected']!=='true'||get('#mobile-compare-right').attributes['aria-selected']!=='false')throw Error('editor menu or selected pane is inconsistent');
    ctx.setCompareLeft('source');if(get('#compare-source').hidden||!get('.editor-pane').hidden)throw Error('returning to original source failed');
    narrow=false;delete get('#result-layout').classList['mobile-left'];ctx.setCompareLeft('editor');
    if(get('#result-layout').classList['mobile-left']||changes!==3)throw Error('wide editor forced single pane or repeated reader lifecycle');
    process.stdout.write('true');
    """
    result=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT)}),text=True,encoding='utf-8',capture_output=True,check=True,timeout=10)
    assert result.stdout=='true'
